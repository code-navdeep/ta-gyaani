"""
β precompute — daily job that computes rolling β per (stock, benchmark) pair
at 4 tiers (22 / 90 / 180 / 252 trading days) and writes to
`historical.db.beta_cache`.

Invocation paths:
  1. Run as a subprocess by the metrics thread's morning step
     (MetricsOrchestrator._fill_previous_close_and_refit_beta), once per
     trading day, right after the previous trading day's close is filled from
     that morning's first reading — so β includes the newest daily return. It
     used to run at the 15:33 end-of-day, when that day's close did not exist
     yet.
  2. Standalone CLI:  `python scripts/precompute_beta.py`
     (useful right after a deploy so RS has β cache populated without
     waiting for the next morning).

β computation:
  Log close-to-close daily returns:
      r_stock_t = ln(close_t / close_{t-1})
  Log, because β is APPLIED to log returns: the RS adapter computes
  session_rs = ln(stock_open/prev) − β × ln(bench_open/prev). A slope fitted on
  simple returns would be a slightly different number from the one that
  equation needs.
  For each tier N: β_N = calculate_beta(r_stock, r_bench) over the last N
  paired daily returns — calls relative_strength_calculator.py's locked
  `calculate_beta` directly (not a reimplementation) so the daily cache and
  the live relative_strength_adapter can never compute β two different ways.

Data source:  historical.db.daily_ohlc's closes (written by the metrics
              thread's morning step from each morning's first reading;
              scripts/daily.py archives the rest of each row but no close).
Sink:         historical.db.beta_cache (one row per (asset, segment,
              benchmark), INSERT OR REPLACE'd here).

Behaviour on missing data:
  - If a pair has fewer than 23 paired returns available, β_22d = NULL
    (need N+1 daily rows to compute N returns).
  - Same graceful degradation per tier — deeper tiers require more history.
  - Log staleness at WARNING if a benchmark's most-recent daily_ohlc date is
    older than the previous trading day (the morning step has just filled that
    day's close, so anything older means an archive or a fill was missed).

Exit codes:  0 on success, 1 on any uncaught error.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

# Configure logging with a single point of setup so both CLI and
# morning-step paths get consistent formatting.
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HISTORICAL_DB_PATH = str(PROJECT_ROOT / "data" / "historical.db")

# Before the project imports below: the metrics thread's morning step runs this file as a
# subprocess (`python scripts/precompute_beta.py`), where only `scripts/` is on
# the path.
sys.path.insert(0, str(PROJECT_ROOT))

# The benchmark table and tiers are the RS adapter's own, not copies: this job
# exists to fill beta_cache for exactly the (stock, benchmark, tier) triples
# that adapter reads, and a second table could drift from it silently.
from calculators.market_calendar import previous_trading_day
from calculators.relative_strength_adapter import BENCHMARKS, benchmarks_for_stock
from calculators.statistics import TIERS, log_return


def _read_config_max_days(default: int = 253) -> int:
    """Read `relative_strength.beta_precompute_max_days` from config.json at
    project root. Falls back to `default` if the file, the
    key, or a valid int isn't present. Never raises.

    CLI `--max-days` argument (in main) takes precedence over this config value
    when set explicitly."""
    try:
        with open(PROJECT_ROOT / "config.json", "r") as f:
            cfg = json.load(f)
        return int(cfg.get('relative_strength', {}).get('beta_precompute_max_days', default))
    except Exception:
        return default


def _load_daily_close_series(hist, asset_id: int, segment_code: str, max_days: int
                              ) -> Optional[pd.DataFrame]:
    """Read last `max_days` rows of daily_ohlc for one (asset_id, segment_code).

    Returns DataFrame indexed by `date` (ascending), with `close_price` column.
    Returns None on error or empty.
    """
    try:
        conn = hist._get_connection()
        df = pd.read_sql_query("""
            SELECT date, close_price
            FROM daily_ohlc
            WHERE asset_id = ? AND segment_code = ?
            ORDER BY date DESC
            LIMIT ?
        """, conn, params=(asset_id, segment_code, max_days))
        if df.empty:
            return None
        df = df.sort_values('date').reset_index(drop=True)
        df['date'] = pd.to_datetime(df['date'])
        return df[['date', 'close_price']]
    except Exception as e:
        logger.error(f"Error reading daily_ohlc for asset {asset_id}/{segment_code}: {e}")
        return None


def _compute_beta_for_pair(stock_df: pd.DataFrame, bench_df: pd.DataFrame
                           ) -> Dict[str, Optional[float]]:
    """Compute β_22d/90d/180d/252d for one (stock, benchmark) pair, via the
    locked `calculate_beta` — same function the live RS adapter would call
    if it computed beta itself, so cache and live math can never diverge.

    Args:
        stock_df:  date-sorted DataFrame with 'date' and 'close_price' columns.
        bench_df:  same shape for the benchmark.

    Returns:
        Dict with keys 'beta_22d', 'beta_90d', 'beta_180d', 'beta_252d'.
        Any tier with insufficient paired returns is None.
    """
    from calculators.relative_strength_calculator import calculate_beta

    result: Dict[str, Optional[float]] = {f'beta_{n}d': None for n in TIERS}

    # Merge on date so we only work with days where both sides have data.
    merged = pd.merge(
        stock_df.rename(columns={'close_price': 'stock_close'}),
        bench_df.rename(columns={'close_price': 'bench_close'}),
        on='date',
        how='inner',
    ).sort_values('date').reset_index(drop=True)

    if merged.empty or len(merged) < 2:
        return result

    # Log close-to-close returns per side (see the module docstring). The first
    # row has no previous close, and a missing or non-positive close has no
    # return; each yields None/NaN and dropna removes the row. Nothing is
    # forward-filled — pct_change() did that, fabricating a 0% day followed by
    # a double-sized one around every missing close.
    for side in ('stock', 'bench'):
        closes = merged[f'{side}_close']
        merged[f'r_{side}'] = [log_return(c, p) for c, p in zip(closes, closes.shift(1))]
    returns = merged[['r_stock', 'r_bench']].dropna()

    if returns.empty:
        return result

    for n in TIERS:
        if len(returns) < n:
            continue  # insufficient paired returns for this tier
        tail = returns.tail(n)
        try:
            beta = calculate_beta(tail['r_stock'].tolist(), tail['r_bench'].tolist())
            if beta is not None:
                result[f'beta_{n}d'] = beta
        except Exception as e:
            logger.debug(f"β tier {n}d computation failed: {e}")

    return result


def precompute_beta(historical_db_path: str = HISTORICAL_DB_PATH,
                    max_days: int = 253) -> Dict[str, int]:
    """
    Run the β precompute end-to-end.

    Args:
        historical_db_path:  Path to historical.db. Default: data/historical.db.
        max_days:  Max daily_ohlc rows to pull per asset (252 top tier + 1,
                   since the first row has no previous close). Default 253.

    Returns:
        Dict with counters — {
            'pairs_total':   int,
            'pairs_written': int,
            'tier_nulls_22d': int,
            'tier_nulls_90d': int,
            'tier_nulls_180d': int,
            'tier_nulls_252d': int,
        }
    """
    from calculators.historical_database_manager import HistoricalDatabaseWriter
    from calculators.time_blocks import IST
    from engine import AssetLoader

    counters = {
        'pairs_total': 0,
        'pairs_written': 0,
        **{f'tier_nulls_{n}d': 0 for n in TIERS},
    }

    # Use the Writer (read-write) — the Reader is read-only, so
    # the beta_cache INSERT OR REPLACE calls below would fail on Reader.
    hist = HistoricalDatabaseWriter(historical_db_path)
    # Ensure schema exists (idempotent — creates beta_cache if this is a fresh
    # historical.db, no-op if already present).
    hist.initialize_database()

    asset_loader = AssetLoader(str(PROJECT_ROOT / "assets" / "assets_select.csv"))
    if not asset_loader.load_assets():
        logger.error("Failed to load assets — β precompute cannot continue")
        return counters
    assets_df = asset_loader.assets_df
    if assets_df is None or assets_df.empty:
        logger.error("No assets loaded — β precompute cannot continue")
        return counters

    # Pre-load benchmark daily close series once (4 benchmarks — cheap).
    bench_series: Dict[str, pd.DataFrame] = {}
    for name, asset_id, segment_code in BENCHMARKS.values():
        df = _load_daily_close_series(hist, asset_id, segment_code, max_days)
        if df is None or df.empty:
            logger.warning(f"Benchmark {name} (asset_id={asset_id}) has no daily_ohlc rows — "
                           f"β tiers for its stocks will all be NULL")
        else:
            bench_series[name] = df
            # Staleness check — against the previous TRADING day, which the
            # morning step has just filled. "Two calendar days ago" fired every
            # Monday (newest row: Friday) and after every holiday.
            latest = df['date'].max().date()
            if latest < previous_trading_day(datetime.now().date()):
                logger.warning(f"Benchmark {name} daily_ohlc is stale (latest date {latest}) — "
                               f"β will reflect data through that date")

    # Iterate stocks — the same `== 'NSE_EQ'` selection the RS adapter makes,
    # so the pairs cached here are exactly the pairs it reads.
    stocks_df = assets_df[assets_df['segment_code'] == 'NSE_EQ']
    computed_at = datetime.now(IST).isoformat()

    conn = hist._get_connection()
    for _, stock_row in stocks_df.iterrows():
        stock_asset_id = int(stock_row['asset_id'])
        stock_segment = str(stock_row['segment_code'])
        stock_symbol = str(stock_row.get('asset_name', 'Unknown'))
        stock_index_field = str(stock_row.get('index', ''))

        pairs = benchmarks_for_stock(stock_index_field)
        if not pairs:
            continue

        stock_df = _load_daily_close_series(hist, stock_asset_id, stock_segment, max_days)
        if stock_df is None or stock_df.empty:
            # No stock history at all — skip pair-by-pair; tier fields would all NULL anyway.
            for name, _, _ in pairs:
                counters['pairs_total'] += 1
                hist.store_beta_cache(
                    stock_asset_id, stock_segment, name,
                    {f'beta_{n}d': None for n in TIERS}, computed_at,
                )
                counters['pairs_written'] += 1
                for n in TIERS:
                    counters[f'tier_nulls_{n}d'] += 1
            continue

        for name, _, _ in pairs:
            counters['pairs_total'] += 1
            bench_df = bench_series.get(name)
            if bench_df is None:
                betas = {f'beta_{n}d': None for n in TIERS}
            else:
                betas = _compute_beta_for_pair(stock_df, bench_df)
            hist.store_beta_cache(
                stock_asset_id, stock_segment, name, betas, computed_at,
            )
            counters['pairs_written'] += 1
            for n in TIERS:
                if betas.get(f'beta_{n}d') is None:
                    counters[f'tier_nulls_{n}d'] += 1

    conn.commit()

    logger.info(
        "β precompute complete: %d pairs total, %d pairs written. "
        "Tier NULL counts — 22d: %d, 90d: %d, 180d: %d, 252d: %d.",
        counters['pairs_total'], counters['pairs_written'],
        counters['tier_nulls_22d'], counters['tier_nulls_90d'],
        counters['tier_nulls_180d'], counters['tier_nulls_252d'],
    )
    return counters


def _configure_cli_logging(verbose: bool) -> None:
    """Set up logging for CLI invocation. DailyScheduler path uses the app-wide logger."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute β cache from historical.db.daily_ohlc.")
    parser.add_argument('--historical-db', default=HISTORICAL_DB_PATH,
                        help=f'Path to historical.db (default: {HISTORICAL_DB_PATH})')
    parser.add_argument('--max-days', type=int, default=None,
                        help='Max daily_ohlc rows per asset. Defaults to '
                             '`relative_strength.beta_precompute_max_days` from config.json '
                             '(fallback 253 = 252 top tier + 1)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable debug logging')
    args = parser.parse_args()

    _configure_cli_logging(args.verbose)

    # Precedence: explicit CLI --max-days > config.json knob > hardcoded default.
    max_days = args.max_days if args.max_days is not None else _read_config_max_days()

    try:
        counters = precompute_beta(historical_db_path=args.historical_db,
                                    max_days=max_days)
        print("SUCCESS:", counters)
        return 0
    except Exception as e:
        logger.exception("β precompute failed: %s", e)
        print(f"FAILED: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main())
