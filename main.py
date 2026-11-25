"""
Main entry point for the trading system.
Starts engine, metrics orchestrator, and dashboard in a single process.
"""

# Force IST timezone BEFORE any other imports touch datetime
import os
import time as _time
os.environ['TZ'] = 'Asia/Kolkata'
_time.tzset()

import datetime as dt
import json
import threading
import logging
import logging.handlers
import signal
import sys
import subprocess
import time

# Configure logging (single point — engine.py and dashboard.py do NOT call basicConfig)
# A new log file each midnight; the previous day's is renamed
# ohlc_engine.log.YYYY-MM-DD, and only the 30 most recent of those are kept.
os.makedirs('data/logs', exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.handlers.TimedRotatingFileHandler(
            'data/logs/ohlc_engine.log', when='midnight', backupCount=30)
    ]
)

# Silence Werkzeug's per-request access log. It logs EVERY HTTP request at INFO,
# and the dashboard's `metrics-interval` (30 sec) has 5 callbacks bound to it —
# Dash issues one POST per callback, so that is 10 requests/min, ~14,400 lines/day
# per open browser tab, forever (the Interval ticks outside market hours too).
# WARNING keeps Werkzeug's own startup/error output. Unhandled callback
# exceptions are unaffected: Flask logs those through the app logger at ERROR
# with a full traceback, which is a different logger and still reaches the terminal.
logging.getLogger('werkzeug').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Import system components
from engine import OHLCEngine
from metrics import MetricsOrchestrator, METRICS_POST_CLOSE_GRACE_SECONDS
from calculators.market_calendar import is_trading_day
from dashboard import app as dashboard_app

# The end-of-day jobs start after metrics has fully drained its post-close grace:
# the schedule's end_time + the grace + a 60 s safety gap (the last metrics cycle
# can start at end_time + grace and take 5-15 s to complete). DERIVED from the
# schedule — the engine's own schedule manager, i.e. config.json's end_time — not
# a fixed number: it used to be 30 + 2 + 1 with 15:30 written in as a constant, so
# moving end_time to 15:31 would have started the jobs while metrics was still
# running. With end_time 15:31: 15:31 + 2 min + 1 min = 15:34.
_EOD_SAFETY_GAP_SECONDS = 60


def _eod_start_time() -> dt.time:
    """When the end-of-day jobs may start, derived from config.json's end_time."""
    sched = engine.schedule_manager
    end = dt.datetime.combine(dt.date.today(), sched._parse_time(sched.schedule_config['end_time']))
    return (end + dt.timedelta(seconds=METRICS_POST_CLOSE_GRACE_SECONDS
                               + _EOD_SAFETY_GAP_SECONDS)).time()

# EXPLICIT IST. os.environ['TZ'] + tzset() above already make a naive
# datetime.now() return IST inside the container, but that is an ENVIRONMENT
# fact, not a code fact: if TZ is ever unset — a different base image, a script
# run outside the container, a compose file edited without the `environment`
# block — every naive call silently becomes UTC and every timestamp shifts by
# 5h30m with nothing raised. The scheduler decides whether a trading day's jobs
# have run, so a 5h30m shift moves that decision across a date boundary. This
# binds the zone in code so the env var cannot change the answer.
from calculators.time_blocks import IST

# Where the scheduler remembers which day it last ran.
#
# WHY IT IS ON DISK. This used to be a local variable, so it reset on every
# process start — and the trigger is "after 15:33 on a weekday", which is true
# for the whole rest of the day. Every container restart after 15:33 therefore
# re-ran all four EOD jobs, INCLUDING Job 4. Dhan invalidates the previous
# token the instant renewal succeeds, and the engine then read .env only at boot,
# so each restart left the freshly-started engine holding the token that the
# restart's own Job 4 had just killed. Observed live 2026-08-28: three
# rotations in one afternoon, and restarting could not escape it because each
# restart repeated the cycle one token further along.
#
# data/ is a directory bind mount, so this survives container recreation.
_SCHEDULER_STATE_PATH = os.path.join('data', 'daily_scheduler_state.json')


def _read_last_run_date():
    """The date the end-of-day jobs (1, 2 and 4) last completed, or None if never / unreadable.

    Degrades to None (i.e. "run them") rather than to today. A missing archive
    is the worse and quieter failure: it silently loses a trading day from
    historical.db, where an extra run is loud and recoverable.
    """
    try:
        with open(_SCHEDULER_STATE_PATH) as fh:
            raw = json.load(fh).get('last_run_date')
        return dt.date.fromisoformat(raw) if raw else None
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning(
            f"Daily scheduler state unreadable ({_SCHEDULER_STATE_PATH}: {e}) — "
            f"treating as not-yet-run for today."
        )
        return None


def _write_last_run_date(run_date):
    """Persist the latch. Temp file + os.replace so a crash mid-write cannot
    leave a truncated file that the next boot would treat as corrupt."""
    tmp = f"{_SCHEDULER_STATE_PATH}.tmp"
    try:
        with open(tmp, 'w') as fh:
            json.dump({'last_run_date': run_date.isoformat()}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, _SCHEDULER_STATE_PATH)
    except Exception as e:
        # Not fatal: the jobs already ran. Worst case is a repeat run on the
        # next restart, which is the behaviour this replaces — never worse.
        logger.error(f"Could not persist daily scheduler state: {e}")

# Global instances (so signal handler can access them)
engine = None
metrics_orchestrator = None
shutdown_in_progress = False

def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully"""
    global shutdown_in_progress

    if shutdown_in_progress:
        logger.warning("Force shutdown - killing process...")
        os._exit(1)

    shutdown_in_progress = True
    logger.info("\n=== Shutdown signal received ===")

    # Stop all components
    try:
        if metrics_orchestrator:
            logger.info("Stopping metrics orchestrator...")
            metrics_orchestrator.stop()

        if engine:
            logger.info("Stopping engine...")
            engine.stop()

        logger.info("=== All systems stopped ===")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")
    finally:
        # Force exit after cleanup attempt
        logger.info("Exiting...")
        sys.exit(0)

def run_engine():
    """Run engine in background thread"""
    try:
        logger.info("Starting OHLC Engine...")
        engine.start()  # Blocking call with internal loop

        # Keep thread alive while engine is running
        while engine.running:
            time.sleep(1)

    except Exception as e:
        logger.error(f"Engine thread error: {e}")

def run_metrics_orchestrator():
    """Run metrics orchestrator in background thread"""
    try:
        logger.info("Starting Metrics Orchestrator...")
        metrics_orchestrator.start()  # Blocking call with internal loop

        # Keep thread alive while orchestrator is running
        while metrics_orchestrator.running:
            time.sleep(1)

    except Exception as e:
        logger.error(f"Metrics orchestrator thread error: {e}")

def _renew_token_for_running_engine():
    """Job 4: renew the Dhan token, and give the new one to the running engine.

    renew_token() writes it to .env; the engine read .env once at start-up, so
    without the hand-over it would keep sending the token Dhan just invalidated.
    """
    from scripts.renew_token import renew_token
    engine.quote_fetcher.access_token = renew_token()


def run_daily_scheduler():
    """
    Runs once per trading day — a weekday not listed in data/holidays.txt — at
    the EOD start: config.json's end_time + the metrics grace + a 60 s gap, i.e.
    15:34 IST with end_time 15:31 (see _eod_start_time). On a listed holiday
    nothing runs, token renewal included.

    3 jobs (1, 2 and 4), executed in strict order.
    **Invariant: Job 4 (token renewal) MUST STAY LAST.** Dhan invalidates the
    previous token immediately on renewal — there is no rollback, so all
    pure-data operations must complete before the token is touched. Any future
    scheduler modification MUST preserve this invariant.

      Job 1: scripts/daily.py --prune --keep-days 5 --allow-partial-coverage
             — archives today's OHLC to historical.db, prunes ohlc.db to 5 days.
             — --allow-partial-coverage: a day under 70% block coverage (late
               start, incomplete blocks) is stored with a warning, not refused;
               this job does not retry, so refusing would lose the day.
             — prune runs inside daily.py only if extraction succeeded, so a
               failed archival leaves source rows intact for manual retry.

      Job 2: metrics-side cleanup (2 sub-jobs).
             a) metrics.db cleanup — keeps 7 days across all 10 tables (the 9
                per-asset tables + index_metrics).
             b) historical.db.intraday_time_block_metrics cleanup — keeps 420
                calendar days, ~284 trading days: the 252-trading-day top tier
                plus ~32 samples of headroom for missed or NULL block writes.

      Job 3 (β precompute) is NOT here any more: it runs in the morning, in the
             metrics thread, right after the previous trading day's close is
             filled from that morning's first readings
             (MetricsOrchestrator._fill_previous_close_and_refit_beta). At the
             EOD start today's close does not exist yet, so a β fitted here
             could never include the newest day's return.

      Job 4: scripts/renew_token.py — rotates DHAN_ACCESS_TOKEN in .env and
             hands the new token to the running engine's quote fetcher.
             — Runs even if Jobs 1/2 failed (token renewal is critical).

    Each job is wrapped in its own try/except; failures don't cascade. The
    scheduler latches `last_run_date = today` only after all jobs are attempted,
    so partial failures don't cause repeat runs within the same day.
    """
    logger.info("Daily scheduler thread started")
    eod_start = _eod_start_time()
    # Seeded from disk, NOT None: a restart after the EOD start must not re-run
    # jobs that already completed today. See _SCHEDULER_STATE_PATH.
    last_run_date = _read_last_run_date()
    if last_run_date is not None:
        logger.info(f"Daily scheduler: last completed run was {last_run_date}")

    while not shutdown_in_progress:
        # Explicit IST — see the IST constant. `today` and the EOD-start
        # comparison both come from this, and a 5h30m drift would move `today`
        # across a date boundary, either skipping a day's archive or repeating it.
        now = dt.datetime.now(IST)
        today = now.date()
        is_trading = is_trading_day(today)       # weekday, not a listed holiday
        is_after_close = now.time() >= eod_start

        if is_trading and is_after_close and last_run_date != today:
            logger.info(f"=== Daily scheduler running for {today} ===")

            # Job 1: Archive today's OHLC data to historical.db, then prune ohlc.db to 5 trading days.
            try:
                result = subprocess.run(
                    [sys.executable, "scripts/daily.py", "--prune", "--keep-days", "5",
                     "--allow-partial-coverage"],
                    capture_output=True, text=True, timeout=300
                )
                if result.returncode == 0:
                    logger.info(f"daily.py completed successfully for {today}")
                else:
                    logger.error(f"daily.py failed (exit {result.returncode}): {result.stderr}")
            except subprocess.TimeoutExpired:
                logger.error("daily.py timed out after 5 minutes")
            except Exception as e:
                logger.error(f"daily.py error: {e}")

            # Job 2a: Prune metrics.db (keep 7 days)
            try:
                from calculators.metrics_database_manager import MetricsDatabaseManager
                db = MetricsDatabaseManager()
                deleted = db.cleanup_old_metrics(days_to_keep=7)
                logger.info(f"metrics.db cleanup: removed {deleted} records older than 7 days")
            except Exception as e:
                logger.error(f"metrics.db cleanup error: {e}")

            # Job 2b: Prune historical.db.intraday_time_block_metrics.
            # Uses the Writer, not the Reader — the Reader is read-only.
            #
            # Retention is in CALENDAR days; tiers are counted in TRADING days.
            # The 252d tier needs exactly 252 non-NULL same-block samples, one
            # per trading day. See cleanup_old_intraday_metrics for the
            # arithmetic: 420 calendar days is ~284 trading days, ~32 spare.
            # 380 left ~5 spare, so a handful of missed block writes in a year
            # kept the tier empty. The earlier 280 was ~193 trading days and
            # could never fill it at all.
            try:
                from calculators.historical_database_manager import HistoricalDatabaseWriter
                hist = HistoricalDatabaseWriter('data/historical.db')
                intraday_deleted = hist.cleanup_old_intraday_metrics(days_to_keep=420)
                logger.info(
                    f"intraday_time_block_metrics cleanup: removed {intraday_deleted} "
                    f"records older than 420 calendar days (~252 trading days + ~32 headroom)"
                )
            except Exception as e:
                logger.error(f"intraday_time_block_metrics cleanup error: {e}")

            # Job 3 (β precompute) runs in the morning instead, right after the
            # previous trading day's close is filled — see the docstring.

            # Job 4 (LAST — token-last invariant):
            # Renew the Dhan access token. Dhan invalidates the old token the
            # moment renewal succeeds, so the new one goes straight to the
            # running engine as well as into .env — otherwise a container left
            # running overnight starts the next session on a dead token.
            try:
                _renew_token_for_running_engine()
            except Exception as e:
                logger.error(f"Dhan token renewal error: {e}")

            last_run_date = today
            # Persisted, not just held in memory — otherwise the next restart
            # today repeats all three jobs and rotates the Dhan token again.
            _write_last_run_date(today)
            logger.info(f"=== Daily scheduler done for {today} ===")

        time.sleep(60)

    logger.info("Daily scheduler thread stopped")

# How long the watchdog waits between stopping the components and ending the
# process. MetricsOrchestrator.stop() only clears a flag, so a metrics cycle
# already under way (~4 s of work) keeps running; this lets its writes finish.
_WATCHDOG_EXIT_DELAY_SECONDS = 5


def _watchdog_restart(reason):
    """Stop what can be stopped, then end the PROCESS so Docker restarts it.

    signal_handler ends in sys.exit(0), and sys.exit only raises SystemExit —
    which, in a non-main thread, ends that thread alone. Called from here it
    therefore left the main thread serving the dashboard: the process stayed
    up, `restart: unless-stopped` never fired, and the healthcheck (a raw TCP
    connect) kept passing — a live-looking dashboard over dead collection.
    os._exit ends the whole process from any thread.
    """
    logger.critical(f"WATCHDOG: {reason} — stopping so the container restarts")
    try:
        signal_handler(None, None)
    except SystemExit:
        pass
    time.sleep(_WATCHDOG_EXIT_DELAY_SECONDS)
    os._exit(1)


def run_watchdog(engine_thread, metrics_thread, daily_thread):
    """
    Monitors the threads the system cannot run without. If any has died, stops
    the components and ends the process, so Docker's `restart: unless-stopped`
    brings the container back up.

    Watched: EngineThread (run_engine), the engine's own fetch thread — which
    run_engine only waits on, so the fetch thread can die while EngineThread
    stays alive — MetricsThread, and DailySchedulerThread, whose death would
    otherwise stop the archive and the token renewal with no symptom beyond
    missing log lines at the EOD start (15:34).
    """
    logger.info("Watchdog thread started")
    time.sleep(30)  # Allow threads time to initialize before monitoring begins

    while not shutdown_in_progress:
        if not engine_thread.is_alive():
            _watchdog_restart("EngineThread has died")
            return
        if engine.fetch_thread is None or not engine.fetch_thread.is_alive():
            _watchdog_restart("the engine's fetch thread has died")
            return
        if not metrics_thread.is_alive():
            _watchdog_restart("MetricsThread has died")
            return
        if not daily_thread.is_alive():
            _watchdog_restart("DailySchedulerThread has died")
            return
        time.sleep(10)

    logger.info("Watchdog thread stopped")

def main():
    """Main entry point"""
    global engine, metrics_orchestrator

    logger.info("=== Trading System Starting ===")

    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # config.json is NOT loaded here any more. This block read the
    # file only to hand it to MetricsOrchestrator, which stored it and never
    # read it. Every actual consumer opens the file itself at its own point of
    # use: OHLCScheduleManager (engine.py and metrics.py, for `schedule`),
    # dashboard.py (_EMA_SPAN_*, _ATR_PERIOD), dashboard_signals.py
    # (_Z_HIGHLIGHT) and scripts/precompute_beta.py.

    # Create instances (BEFORE threads, so signal handler can access them)
    logger.info("Initializing components...")
    engine = OHLCEngine(
        csv_path="assets/assets_select.csv",
        refresh_interval=5
    )
    metrics_orchestrator = MetricsOrchestrator()

    # Start engine in background thread
    engine_thread = threading.Thread(target=run_engine, daemon=True, name="EngineThread")
    engine_thread.start()
    logger.info("✓ Engine thread started")

    # Wait for engine to initialize (5 seconds)
    logger.info("Waiting for engine initialization...")
    time.sleep(5)

    # Start metrics orchestrator in background thread
    metrics_thread = threading.Thread(target=run_metrics_orchestrator, daemon=True, name="MetricsThread")
    metrics_thread.start()
    logger.info("✓ Metrics orchestrator thread started")

    # Wait for metrics orchestrator to initialize (2 seconds)
    logger.info("Waiting for metrics orchestrator initialization...")
    time.sleep(2)

    # Daily scheduler: archives OHLC + prunes metrics.db at the EOD start (15:34
    # IST with end_time 15:31) on trading days — weekdays not listed in
    # data/holidays.txt (after metrics grace)
    daily_thread = threading.Thread(target=run_daily_scheduler, daemon=True, name="DailySchedulerThread")
    daily_thread.start()
    logger.info(f"✓ Daily scheduler thread started (runs at {_eod_start_time():%H:%M} IST "
                f"on trading days — weekdays not listed in data/holidays.txt)")

    # Watchdog: detects dead threads and ends the process for Docker auto-restart
    watchdog_thread = threading.Thread(
        target=run_watchdog,
        args=(engine_thread, metrics_thread, daily_thread),
        daemon=True,
        name="WatchdogThread"
    )
    watchdog_thread.start()
    logger.info("✓ Watchdog thread started")

    # Run dashboard in main thread (blocking)
    logger.info("✓ Starting Dashboard on port 8054...")
    logger.info("=== All Systems Running ===")
    logger.info("Press Ctrl+C to stop")

    try:
        dashboard_app.run(
            host='0.0.0.0',
            port=8054,
            debug=False,
            use_reloader=False  # Disable reloader to prevent double startup
        )
    except KeyboardInterrupt:
        logger.info("Dashboard interrupted")
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
    finally:
        # Cleanup (in case signal handler didn't run)
        if not shutdown_in_progress:
            signal_handler(None, None)

if __name__ == '__main__':
    main()
