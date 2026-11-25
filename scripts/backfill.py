#!/usr/bin/env python3
"""
Manual Historical Data Backfill Script

Populates historical.db from ohlc.db — for days the 15:34 archive missed (the
app was closed early), or after historical.db was deleted. Each day goes
through daily.py's own archive code.

After every day is stored:
  - each day's official close is written from the NEXT trading day's first
    reading — the metrics thread's morning-step rule (the archive writes no
    close: a day's close only exists in the next morning's first reading);
  - the per-block snapshots (the shared store the rolling z-scores compare
    against) are rebuilt for each archived day from its readings, with the same
    function the live writer uses;
  - β is refitted (scripts/precompute_beta.py).

Today is refused until its session has stopped collecting (15:31): archived
earlier, its morning would be stored as the whole day, permanently.

Re-running over days already archived is safe — an existing row only gains the
fields it lacks, a stored block is kept — and the summary counts rows actually
written.

Usage:
    python scripts/backfill.py --days 100     # The newest 100 trading days in ohlc.db
    python scripts/backfill.py --start-date 2026-09-01 --end-date 2026-09-04
    python scripts/backfill.py --preview --days 100  # Preview without storing
    python scripts/backfill.py --all --allow-partial-coverage  # Include days the app ran only part of
    python scripts/backfill.py --all --prune  # Backfill everything, then prune ohlc.db
"""

import sys
import os
import argparse
import subprocess
from datetime import datetime, date, timedelta
from pathlib import Path
import pandas as pd
from tqdm import tqdm

# Project root directory (parent of scripts/)
PROJECT_ROOT = Path(__file__).parent.parent

# Add parent directory to path for imports
sys.path.insert(0, str(PROJECT_ROOT))

from daily import (DailyHistoricalExtractor, _print_prune_summary, prune_ohlc_database,
                   session_still_collecting)
from calculators.block_snapshots import block_end_values
from calculators.market_calendar import is_trading_day, previous_trading_day
from calculators.time_blocks import (FINAL_READINGS_CUTOFF, IST, SESSION_START, all_blocks,
                                     block_reading_end_time, block_start_time)
import logging
import logging.handlers

# Ensure logs directory exists
LOG_DIR = PROJECT_ROOT / 'data' / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Configure logging. A new file each day; the previous day's is renamed
# historical_backfill.log.YYYY-MM-DD and only the 30 most recent are kept.
logging.basicConfig(
    level=logging.WARNING,  # Reduce verbosity for bulk operations
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.handlers.TimedRotatingFileHandler(
            LOG_DIR / 'historical_backfill.log', when='midnight', backupCount=30),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class HistoricalBackfiller:
    """Backfills historical database from OHLC database."""

    def __init__(self, ohlc_db_path: str, historical_db_path: str):
        """
        Initialize backfiller.

        Args:
            ohlc_db_path: Path to ohlc.db (source)
            historical_db_path: Path to historical.db (destination)
        """
        self.extractor = DailyHistoricalExtractor(ohlc_db_path, historical_db_path)

    def get_available_dates_from_ohlc(self) -> list[date]:
        """
        Query ohlc.db to find all available trading dates.

        Returns:
            List of dates with data
        """
        try:
            with self.extractor.ohlc_db.get_read_connection() as conn:
                cursor = conn.cursor()

                # substr(), NOT DATE(). SQLite's date functions convert an
                # offset-bearing ISO timestamp to UTC before extracting, so
                # DATE('2026-08-31T00:30:00+05:30') is '2026-08-30'. substr()
                # is a lexical slice and reads IST wall-clock regardless of
                # whether the column stores an offset. scripts/daily.py resolves
                # dates the same way — the two MUST agree about which day a row
                # belongs to, or this enumerates days the extractor cannot find.
                query = """
                    SELECT DISTINCT substr(timestamp, 1, 10) as trade_date
                    FROM ohlc_data
                    WHERE timestamp IS NOT NULL
                    ORDER BY trade_date DESC
                """

                cursor.execute(query)
                rows = cursor.fetchall()

            dates = []
            for row in rows:
                try:
                    trade_date = datetime.strptime(row[0], '%Y-%m-%d').date()
                    # Skip weekends and listed market holidays
                    if is_trading_day(trade_date):
                        dates.append(trade_date)
                except Exception as e:
                    logger.warning(f"Skipping invalid date {row[0]}: {e}")
                    continue

            return sorted(dates)

        except Exception as e:
            logger.error(f"Error getting available dates: {e}")
            return []

    def get_date_range(self, start_date: date, end_date: date) -> list[date]:
        """
        Generate list of trading days between start and end: weekdays not
        listed in data/holidays.txt.

        Args:
            start_date: Start date
            end_date: End date

        Returns:
            List of dates (excluding weekends and listed market holidays)
        """
        dates = []
        current = start_date

        while current <= end_date:
            if is_trading_day(current):
                dates.append(current)
            current += timedelta(days=1)

        return dates

    def fill_closes_from_first_readings(self) -> int:
        """
        Write each archived day's official close, the way the morning step does.

        The archive writes every daily_ohlc row WITHOUT a close: a reading's
        close_price is the PREVIOUS trading day's official close, so a day's
        own close only exists in the next trading day's first reading. For
        every trading day D in ohlc.db, oldest first, D's first-reading closes
        are written into the row of D's previous trading day (a close-only row
        if that day has none) — the same rule as the metrics thread's morning
        step. Runs after all inserts, so a --force re-archive loses no close.
        The newest day's own close arrives with the next morning's step.

        Returns the number of closes written.
        """
        filled = 0
        for day in self.get_available_dates_from_ohlc():
            closes = self.extractor.ohlc_db.get_first_reading_closes(day)
            if closes.empty:
                continue
            filled += self.extractor.historical_db.fill_closes(previous_trading_day(day), closes)
        return filled

    def refit_beta(self) -> None:
        """Refit the β cache from the backfilled daily_ohlc (scripts/precompute_beta.py)."""
        try:
            result = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "scripts" / "precompute_beta.py")],
                capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                logger.error(f"precompute_beta.py failed (exit {result.returncode}): {result.stderr}")
        except Exception as e:
            logger.error(f"precompute_beta.py error: {e}")

    def _assets_with_readings(self, day: date) -> list:
        """(asset_id, segment_code, symbol) of every asset with a reading on `day`."""
        with self.extractor.ohlc_db.get_read_connection() as conn:
            return [tuple(r) for r in conn.execute(
                "SELECT asset_id, segment_code, MAX(symbol) FROM ohlc_data "
                "WHERE substr(timestamp, 1, 10) = ? GROUP BY asset_id, segment_code",
                (day.isoformat(),)).fetchall()]

    def rebuild_block_snapshots(self, days: list) -> int:
        """
        Rebuild the per-block snapshots — historical.db.intraday_time_block_metrics,
        the three values each asset had at each 15-minute block's end — for each
        of `days`, from its readings in ohlc.db.

        The values come from block_snapshots.block_end_values, the function the
        live writer uses (MetricsOrchestrator._maybe_write_intraday_block), over
        exactly the readings it uses: [block start, block reading end), block 25
        up to 15:31. So a rebuilt day and a live one are the same quantity.

        It stores what the live writer would have stored. A block gets rows only
        if the day has readings inside it — the live writer only runs while the
        app does — one row per asset seen that day, with empty values for an
        asset without a reading in that block. As live: price move and VWAP
        distance only when the block's last reading came within 30 s of its end,
        PV Divergence only when its readings ran without a gap over 30 s, never
        counting the day's first reading.

        The price move needs the close of the day's previous trading day, so this
        runs after fill_closes_from_first_readings. An existing row is replaced;
        rebuilt from the same readings, it holds the same values.

        Returns the number of rows written.
        """
        written = 0
        for day in days:
            closes = self.extractor.historical_db.get_daily_closes(previous_trading_day(day))
            day_start = datetime.combine(day, SESSION_START, tzinfo=IST)
            day_end = datetime.combine(day, FINAL_READINGS_CUTOFF, tzinfo=IST)
            readings, firsts = {}, {}
            for asset_id, segment_code, symbol in self._assets_with_readings(day):
                frame = self.extractor.ohlc_db.get_intraday_data_range(
                    asset_id, day_start, day_end, segment_code=segment_code)
                if frame is not None and not frame.empty:
                    readings[(asset_id, segment_code, symbol)] = frame
                    firsts[(asset_id, segment_code)] = self.extractor.ohlc_db.session_first_reading(
                        asset_id, segment_code, day)

            rows = []
            for block in all_blocks():
                # ISO text bounds, compared as text — as get_intraday_data_range
                # compares them for the live writer.
                start = datetime.combine(day, block_start_time(block), tzinfo=IST).isoformat()
                end = datetime.combine(day, block_reading_end_time(block), tzinfo=IST).isoformat()
                in_block = {key: frame[(frame['timestamp'] >= start) & (frame['timestamp'] < end)]
                            for key, frame in readings.items()}
                if all(frame.empty for frame in in_block.values()):
                    continue   # the app was not running during this block
                for (asset_id, segment_code, symbol), frame in readings.items():
                    rows.append({
                        'date': day.isoformat(),
                        'asset_id': asset_id,
                        'segment_code': segment_code,
                        'symbol': symbol,
                        'time_block': block,
                        # The whole day: block_end_values takes the block's own
                        # readings, and the 30 s before it for PV Divergence.
                        **block_end_values(frame, closes.get((asset_id, segment_code)),
                                           day, block, firsts[(asset_id, segment_code)]),
                    })
            if rows:
                written += self.extractor.historical_db.store_intraday_time_block_metrics(rows)
        return written

    def backfill_date_range(self, dates: list[date], preview: bool = False,
                           force: bool = False, allow_partial_coverage: bool = False,
                           now: datetime = None) -> dict:
        """
        Backfill historical data for a list of dates.

        A day already archived is processed again, deliberately: an existing
        row only gains the fields it lacks and a stored block is kept, so a
        re-run repairs a day (e.g. one holding only its close) and changes
        nothing else. The counts are rows actually written.

        Args:
            dates: List of dates to backfill
            preview: If True, don't store data
            force: If True, delete each day first and store it afresh (keeping
                no stored value), even if it fails the quality checks
            allow_partial_coverage: store a day even when under 70% of its
                15-minute blocks have volume (a day the app ran only part of);
                the other quality checks still apply. The 15:34 job does the same.
            now: the current time (IST), which decides whether today is still
                collecting; defaults to the clock.

        Returns:
            Dictionary with backfill statistics
        """
        stats = {
            'total_dates': len(dates),
            'successful': 0,
            'failed': 0,
            'skipped': 0,
            'ohlc_records': 0,
            'tb_records': 0,
            'closes_filled': 0,
            'snapshot_rows': 0,
        }

        print("=" * 60)
        print("Historical Data Backfill")
        if preview:
            print("Mode: PREVIEW (no data will be stored)")
        print("=" * 60)
        print()

        # Process each date with progress bar
        failed_dates = []
        archived = []   # days stored this run — their block snapshots are rebuilt below

        with tqdm(total=len(dates), desc="Backfilling", unit="day") as pbar:
            for target_date in dates:
                try:
                    # Today, while its readings are still coming in: archived now,
                    # its morning would be stored as the whole day, permanently.
                    if session_still_collecting(target_date, now):
                        stats['skipped'] += 1
                        pbar.update(1)
                        continue

                    # Extract data (suppress output)
                    ohlc_df = self.extractor.extract_daily_ohlc(target_date)
                    if ohlc_df.empty:
                        stats['failed'] += 1
                        failed_dates.append((target_date, "No OHLC data"))
                        pbar.update(1)
                        continue

                    tb_df = self.extractor.extract_time_block_volumes(target_date)
                    if tb_df.empty:
                        stats['failed'] += 1
                        failed_dates.append((target_date, "No time block data"))
                        pbar.update(1)
                        continue

                    # Drop any asset whose day lacks its open, high or low — the
                    # same per-asset check daily.py's run_extraction applies.
                    # validate_data_quality no longer does it itself.
                    ohlc_df = self.extractor.drop_incomplete_ohlc_rows(ohlc_df, target_date)

                    # Validate quality
                    quality_ok = self.extractor.validate_data_quality(
                        ohlc_df, tb_df, target_date, allow_partial_coverage=allow_partial_coverage)
                    if not quality_ok and not force:
                        stats['failed'] += 1
                        failed_dates.append((target_date, "Quality issues"))
                        pbar.update(1)
                        continue

                    # Store data (unless preview mode)
                    if not preview:
                        if force:
                            self.extractor.historical_db.delete_date_data(target_date)

                        ohlc_count = self.extractor.historical_db.bulk_insert_daily_ohlc(ohlc_df)
                        tb_count = self.extractor.historical_db.bulk_insert_time_block_volumes(tb_df)

                        stats['ohlc_records'] += ohlc_count
                        stats['tb_records'] += tb_count
                        archived.append(target_date)

                    stats['successful'] += 1
                    pbar.update(1)

                except Exception as e:
                    logger.error(f"Error processing {target_date}: {e}")
                    stats['failed'] += 1
                    failed_dates.append((target_date, str(e)))
                    pbar.update(1)

        # After every day is stored: each day's official close (from the next
        # trading day's first reading); then the stored days' block snapshots,
        # whose price move needs the previous day's close; then β from the
        # completed history.
        if not preview:
            stats['closes_filled'] = self.fill_closes_from_first_readings()
            stats['snapshot_rows'] = self.rebuild_block_snapshots(archived)
            self.refit_beta()

        # Print summary
        print()
        print("=" * 60)
        print("Backfill Summary")
        print("=" * 60)
        print(f"Dates processed: {stats['total_dates']}")
        print(f"Successful: {stats['successful']}")
        print(f"Failed: {stats['failed']}")
        print(f"Skipped (today — still collecting until {FINAL_READINGS_CUTOFF:%H:%M}): "
              f"{stats['skipped']}")

        if not preview:
            print()
            print(f"Daily OHLC rows added or completed: {stats['ohlc_records']:,}")
            print(f"Time block rows added: {stats['tb_records']:,}")
            print(f"Closes filled (from next-day first readings): {stats['closes_filled']:,}")
            print(f"Block snapshot rows written: {stats['snapshot_rows']:,}")

        if failed_dates:
            print()
            print("Failed dates:")
            for failed_date, reason in failed_dates[:10]:  # Show first 10
                print(f"  - {failed_date}: {reason}")
            if len(failed_dates) > 10:
                print(f"  ... and {len(failed_dates) - 10} more")

        if not preview:
            print()
            db_stats = self.extractor.historical_db.get_date_range_stats()
            if db_stats:
                ohlc_stats = db_stats.get('ohlc', {})
                print("Historical DB Stats:")
                print(f"- Total trading days: {ohlc_stats.get('trading_days', 0)}")
                print(f"- Date range: {ohlc_stats.get('min_date')} to {ohlc_stats.get('max_date')}")
                print(f"- Total records: {ohlc_stats.get('total_records', 0):,}")

        print("=" * 60)

        return stats


def parse_args(argv=None):
    """The command line. Exactly one of --days N, --all, or --start-date (with
    --end-date) chooses the days.

    The date range used to sit OUTSIDE that required choice: asked for alone it
    stopped with "one of the arguments --days --all is required", and with
    --all added the dates were silently ignored and every day was archived. It
    is now the third of the three choices."""
    parser = argparse.ArgumentParser(
        description="Backfill historical database from OHLC database"
    )

    # Which days — exactly one of these three.
    date_group = parser.add_mutually_exclusive_group(required=True)
    date_group.add_argument(
        '--days',
        type=int,
        help='The newest N trading days in ohlc.db'
    )
    date_group.add_argument(
        '--all',
        action='store_true',
        help='Every trading day in ohlc.db'
    )
    date_group.add_argument(
        '--start-date',
        type=str,
        help='First day of a range (YYYY-MM-DD); needs --end-date'
    )
    parser.add_argument(
        '--end-date',
        type=str,
        help='Last day of the range (YYYY-MM-DD); needs --start-date'
    )
    parser.add_argument(
        '--preview',
        action='store_true',
        help='Preview mode - validate but do not store data'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Delete each day first and store it afresh, even if it fails the quality checks'
    )
    parser.add_argument(
        '--allow-partial-coverage',
        action='store_true',
        help='Store a day the app ran only part of (under 70%% of its 15-minute blocks); '
             'the other quality checks still apply, as for the 15:34 job'
    )
    parser.add_argument(
        '--prune',
        action='store_true',
        help='Prune ohlc.db after backfilling, keeping only the latest N trading days'
    )
    parser.add_argument(
        '--keep-days',
        type=int,
        default=5,
        help='Number of most recent trading days to keep in ohlc.db (default: 5, used with --prune)'
    )

    args = parser.parse_args(argv)
    if bool(args.start_date) != bool(args.end_date):
        parser.error("--start-date and --end-date must be used together")
    # 0 read as "not given" and fell through to the date range, crashing on a
    # missing start date; a negative count took an odd slice of the days.
    if args.days is not None and args.days < 1:
        parser.error("--days must be 1 or more")
    return args


def select_dates(args, available_dates: list) -> list:
    """The days to backfill, out of the trading days found in ohlc.db. Raises
    ValueError for a --start-date / --end-date not in YYYY-MM-DD form."""
    if args.all:
        return available_dates
    if args.days:
        return available_dates[-args.days:] if len(available_dates) >= args.days else available_dates
    start_date = datetime.strptime(args.start_date, '%Y-%m-%d').date()
    end_date = datetime.strptime(args.end_date, '%Y-%m-%d').date()
    return [d for d in available_dates if start_date <= d <= end_date]


def main():
    """Main entry point for backfill script."""
    args = parse_args()

    # Ensure logs directory exists (already created at module level, but just in case)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Initialize backfiller with absolute paths
    ohlc_db_path = str(PROJECT_ROOT / "data" / "ohlc.db")
    historical_db_path = str(PROJECT_ROOT / "data" / "historical.db")

    if not Path(ohlc_db_path).exists():
        print(f"Error: OHLC database not found at {ohlc_db_path}")
        sys.exit(1)

    backfiller = HistoricalBackfiller(ohlc_db_path, historical_db_path)

    # Determine dates to backfill
    print("Scanning ohlc.db for available dates...")
    available_dates = backfiller.get_available_dates_from_ohlc()

    if not available_dates:
        print("Error: No data found in OHLC database")
        sys.exit(1)

    print(f"Found {len(available_dates)} trading days in OHLC database")
    print(f"Date range: {min(available_dates)} to {max(available_dates)}")
    print()

    # Select dates based on arguments
    try:
        target_dates = select_dates(args, available_dates)
    except ValueError:
        print("Error: Invalid date format. Use YYYY-MM-DD")
        sys.exit(1)
    if not target_dates:
        print("Error: No data available for the selected dates")
        sys.exit(1)

    print(f"Selected {len(target_dates)} dates for backfill")
    print()

    # Confirm before proceeding (unless preview mode)
    if not args.preview and not args.force:
        response = input("Proceed with backfill? [y/N]: ")
        if response.lower() != 'y':
            print("Backfill cancelled")
            sys.exit(0)

    # Run backfill
    start_time = datetime.now()
    stats = backfiller.backfill_date_range(target_dates, preview=args.preview, force=args.force,
                                           allow_partial_coverage=args.allow_partial_coverage)
    duration = (datetime.now() - start_time).total_seconds()

    print()
    print(f"Duration: {duration:.1f} seconds")

    backfill_success = stats['successful'] > 0

    if stats['failed'] > 0:
        print(f"Status: COMPLETED WITH ERRORS ⚠")
    else:
        print(f"Status: SUCCESS ✓")

    # Run pruning AFTER backfill completes (only if at least some backfilling succeeded)
    if args.prune:
        if not backfill_success:
            print("\nSkipping pruning - no data was backfilled. Data in ohlc.db is preserved.")
        elif args.preview:
            print("\nSkipping pruning - preview mode (no data was stored).")
        else:
            prune_stats = prune_ohlc_database(ohlc_db_path, keep_days=args.keep_days)
            _print_prune_summary(prune_stats)

    sys.exit(0 if stats['failed'] == 0 else 1)


if __name__ == "__main__":
    main()
