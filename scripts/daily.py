#!/usr/bin/env python3
"""
Daily Historical Data Extraction Script

Extracts EOD data from ohlc.db and stores in historical.db for long-term analysis.
The app runs it by itself at 15:34 IST on every trading day (with --prune and
--allow-partial-coverage). Run it by hand for a day that was missed, while
ohlc.db still holds that day (it keeps the last 5 trading days); a day is
refused until its readings stop at 15:31.

Usage:
    python scripts/daily.py              # Extract today's data
    python scripts/daily.py --date 2025-01-15  # Extract specific date
    python scripts/daily.py --dry-run    # Validate without storing
    python scripts/daily.py --force      # Overwrite existing data
    python scripts/daily.py --stats-only # Show DB stats only
    python scripts/daily.py --prune      # Extract today + prune ohlc.db to 5 trading days
    python scripts/daily.py --prune --keep-days 7  # Custom retention
"""

import sys
import os
import argparse
import logging
import logging.handlers
import sqlite3
from datetime import datetime, date, time, timedelta
from pathlib import Path
import pandas as pd

# Project root directory (parent of scripts/)
PROJECT_ROOT = Path(__file__).parent.parent

# Add parent directory to path for imports
sys.path.insert(0, str(PROJECT_ROOT))

from calculators.database_manager import OHLCDatabaseReader
from calculators.historical_database_manager import HistoricalDatabaseWriter
from calculators.market_calendar import is_market_holiday
from calculators.segments import NON_VOLUME_SEGMENTS
from calculators.time_blocks import (BLOCK_COUNT, FINAL_READINGS_CUTOFF, IST, all_blocks,
                                     block_complete, block_reading_end_time, block_start_time,
                                     reading_clock)

LOG_DIR = PROJECT_ROOT / 'data' / 'logs'
logger = logging.getLogger(__name__)


def session_still_collecting(target_date: date, now: datetime = None) -> bool:
    """True while `target_date` is today and its readings are still coming in —
    before FINAL_READINGS_CUTOFF (15:31), the last moment a reading counts.

    Archiving a day before then stores its morning as the whole day: the high,
    low, total volume and VWAP as of that moment, and the block in progress
    short (run at 12:05, block 12 holds 5 of its 15 minutes). The 15:34 archive
    only fills EMPTY fields and never replaces a stored block, so none of it is
    ever corrected — in tables that are never pruned. Both daily.py and
    backfill.py refuse such a day; the 15:34 job runs after 15:31 and is
    unaffected.
    """
    now = now or datetime.now(IST)
    return target_date == now.date() and now.time() < FINAL_READINGS_CUTOFF


def _configure_logging() -> None:
    """Log to data/logs/historical_extraction.log and stdout.

    A new file each day: the previous day's is renamed
    historical_extraction.log.YYYY-MM-DD and only the 30 most recent are kept.

    Called from main(), not at import, so importing this module (the tests do)
    never attaches a handler to the real archive log.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.handlers.TimedRotatingFileHandler(
                LOG_DIR / 'historical_extraction.log', when='midnight', backupCount=30),
            logging.StreamHandler(sys.stdout)
        ]
    )


class DailyHistoricalExtractor:
    """Extracts daily historical data from OHLC database."""

    def __init__(self, ohlc_db_path: str, historical_db_path: str):
        """
        Initialize extractor with database paths.

        Args:
            ohlc_db_path: Path to ohlc.db (source)
            historical_db_path: Path to historical.db (destination)
        """
        self.ohlc_db = OHLCDatabaseReader(ohlc_db_path)
        self.historical_db = HistoricalDatabaseWriter(historical_db_path)

    def validate_market_day(self, target_date: date) -> bool:
        """
        Check if target date is a valid market day.

        Args:
            target_date: Date to validate

        Returns:
            True if valid market day, False otherwise
        """
        # Skip weekends
        if target_date.weekday() >= 5:  # Saturday=5, Sunday=6
            logger.warning(f"{target_date} is a weekend - skipping")
            return False

        if is_market_holiday(target_date):
            logger.warning(f"{target_date} is a listed market holiday - skipping")
            return False

        return True

    def extract_daily_ohlc(self, target_date: date) -> pd.DataFrame:
        """
        Extract daily OHLC data for all assets for target date.

        Args:
            target_date: Date to extract

        Returns:
            DataFrame with columns: date, asset_id, symbol, open, high, low, close, volume, vwap
        """
        try:
            logger.info(f"Extracting daily OHLC for {target_date}")

            # Convert date to datetime range for query
            start_datetime = datetime.combine(target_date, time(9, 15))
            end_datetime = datetime.combine(target_date, time(15, 30))

            with self.ohlc_db.get_read_connection() as conn:
                # Query all assets' OHLC data for the day
                #
                # VOLUME SEMANTICS:
                # - ohlc_data.volume = cumulative daily volume up to that timestamp
                # - ohlc_data.interval_volume = volume traded in that specific 5-second interval
                # - We use MAX(volume) to get the final cumulative value = total day volume
                # - We use LAST average_price to get the final daily VWAP from API
                # substr(), NOT DATE()/strftime() ON THE BARE COLUMN. SQLite's
                # date functions parse the string and convert to UTC when it
                # carries an offset, so strftime('%H:%M', '...T09:15:00+05:30')
                # returns '03:45'. The CASE arm below selects the session's
                # opening rows by matching a literal IST clock string — under a
                # converted value it would match NOTHING, and every open_price
                # in daily_ohlc would archive as NULL, silently, with no error.
                # daily_ohlc feeds momentum gap-z, beta precompute, ATR and
                # prev-close.
                #
                # substr() is a lexical slice: characters 1-19 of an ISO
                # timestamp are identical with or without a trailing offset, so
                # this reads IST wall-clock regardless of storage format.
                #
                # NO CLOSE. A reading's close_price is the PREVIOUS day's close
                # all session, so taking it here filed each day under the
                # previous day's close. The day's own official close only exists
                # in the NEXT trading morning's first reading; the metrics
                # thread's morning step writes it then (fill_closes).
                query = """
                    SELECT
                        ? as date,
                        asset_id,
                        segment_code,
                        symbol,
                        MIN(CASE WHEN strftime('%H:%M', substr(timestamp, 1, 19)) = '09:15' THEN open_price END) as open_price,
                        MAX(high_price) as high_price,
                        MIN(low_price) as low_price,
                        NULL as close_price,
                        MAX(volume) as volume,
                        (SELECT average_price FROM ohlc_data o2
                         WHERE o2.asset_id = ohlc_data.asset_id
                           AND o2.segment_code = ohlc_data.segment_code
                           AND substr(o2.timestamp, 1, 10) = ?
                         ORDER BY o2.timestamp DESC LIMIT 1) as vwap
                    FROM ohlc_data
                    WHERE substr(timestamp, 1, 10) = ?
                    GROUP BY asset_id, segment_code, symbol
                    HAVING COUNT(*) > 0
                """

                df = pd.read_sql_query(
                    query,
                    conn,
                    params=(target_date.isoformat(), target_date.isoformat(), target_date.isoformat())
                )

                # Edge case: if no 09:15 open was found, use the first available open
                if df.empty:
                    logger.warning(f"No OHLC data found for {target_date}")
                    return pd.DataFrame()

                for idx, row in df.iterrows():
                    if pd.isna(row['open_price']):
                        conn_cursor = conn.cursor()
                        conn_cursor.execute("""
                            SELECT open_price
                            FROM ohlc_data
                            WHERE asset_id = ? AND segment_code = ? AND substr(timestamp, 1, 10) = ?
                            ORDER BY timestamp ASC
                            LIMIT 1
                        """, (row['asset_id'], row['segment_code'], target_date.isoformat()))
                        first_row = conn_cursor.fetchone()
                        if first_row:
                            df.at[idx, 'open_price'] = first_row[0]

            logger.info(f"Extracted {len(df)} OHLC records")
            return df

        except Exception as e:
            logger.error(f"Error extracting daily OHLC: {e}")
            return pd.DataFrame()

    def extract_time_block_volumes(self, target_date: date) -> pd.DataFrame:
        """
        Extract time block volumes for all assets for target date.

        Args:
            target_date: Date to extract

        Returns:
            DataFrame with columns: date, asset_id, symbol, time_block, cumulative_volume, vwap
        """
        try:
            logger.info(f"Extracting time block volumes for {target_date}")

            with self.ohlc_db.get_read_connection() as conn:
                block_frames = []
                incomplete_blocks = 0

                # The grid is time_blocks.py's, not a copy: this loop decides
                # which block number every archived volume is filed under, and
                # Volume Block z-scores each block against that history. Each
                # block is half-open, [start, end), in the SQL below. The end is
                # the block's READING end: block 25 also takes the session's
                # final readings, up to 15:31 — the same window the engine's
                # live total uses (block_reading_end_time).
                for block_num in all_blocks():
                    start_time = block_start_time(block_num)
                    end_time = block_reading_end_time(block_num)
                    # Build time range query
                    #
                    # VOLUME SEMANTICS:
                    # - ohlc_data.volume = cumulative daily volume up to that timestamp
                    # - ohlc_data.interval_volume = volume traded in that specific 5-second interval
                    # - We use SUM(interval_volume) to sum all 5-second snapshots within this time block
                    # - Result stored as "cumulative_volume" (historical naming - actually means "block total volume")
                    # - Note: "cumulative_volume" is confusing but matches historical.db schema and calculator expectations
                    # INDICES ARE EXCLUDED, exactly as engine.py:_store_time_block_volumes
                    # excludes them before writing. An index is not tradeable and has
                    # no volume, so the engine writes NO time_block_volumes row for one
                    # and ohlc.db correctly holds zero.
                    #
                    # This query does not COPY that table — it RE-DERIVES blocks from
                    # raw ohlc_data, which is the right call for stocks (it recovers
                    # blocks whose boundary detection was missed). But indices DO have
                    # ohlc_data rows, with NULL volume, so without this filter
                    # SUM(interval_volume) returns NULL and a row is written anyway —
                    # putting back exactly the rows the engine refused to write, 4 per
                    # block per day, into a table that is never pruned.
                    #
                    # Half-applying the rule is worse than not applying it: a later
                    # reader gets NULL, the existing COALESCE(..., 0) turns it into 0,
                    # and Volume Block would z-score a constant-zero series whose sigma
                    # is 0. Only the placeholder COUNT is interpolated; the values are
                    # bound parameters.
                    excluded = tuple(NON_VOLUME_SEGMENTS)
                    seg_placeholders = ','.join('?' * len(excluded))
                    # Besides the sum, the three readings that decide whether the
                    # block is complete — the last good reading before its start,
                    # its first and last good readings — exactly as the engine's
                    # live write reads them. "Good" = running total stored.
                    query = f"""
                        SELECT
                            ? as date,
                            asset_id,
                            segment_code,
                            symbol,
                            ? as time_block,
                            SUM(CASE WHEN substr(timestamp, 12, 8) >= ? THEN interval_volume END)
                                as cumulative_volume,
                            MAX(CASE WHEN substr(timestamp, 12, 8) < ?
                                      AND volume IS NOT NULL THEN timestamp END) as prev_reading,
                            MIN(CASE WHEN substr(timestamp, 12, 8) >= ?
                                      AND volume IS NOT NULL THEN timestamp END) as first_reading,
                            MAX(CASE WHEN substr(timestamp, 12, 8) >= ?
                                      AND volume IS NOT NULL THEN timestamp END) as last_reading
                        FROM ohlc_data
                        WHERE substr(timestamp, 1, 10) = ?
                          AND substr(timestamp, 12, 8) < ?
                          AND segment_code NOT IN ({seg_placeholders})
                        GROUP BY asset_id, segment_code, symbol
                    """

                    start = start_time.strftime('%H:%M:%S')
                    df_block = pd.read_sql_query(
                        query,
                        conn,
                        params=(
                            target_date.isoformat(),
                            block_num,
                            start, start, start, start,
                            target_date.isoformat(),
                            end_time.strftime('%H:%M:%S'),
                            *excluded,
                        )
                    )
                    if df_block.empty:
                        continue

                    # ONLY COMPLETE BLOCKS ARE ARCHIVED — both edges observed
                    # within BLOCK_EDGE_TOLERANCE (time_blocks.block_complete). An
                    # incomplete block gets no row: this table is never pruned,
                    # and every future z-score for the block compares against it.
                    # 2026-09-02, TCS: a gap from 10:37:22 to 11:13:02 left block 6
                    # short and put ~2.5 blocks' worth into block 8.
                    complete = pd.Series([
                        block_complete(block_num, reading_clock(prev), reading_clock(first),
                                       reading_clock(last))
                        for prev, first, last in zip(df_block['prev_reading'],
                                                     df_block['first_reading'],
                                                     df_block['last_reading'])
                    ], index=df_block.index)
                    keep = complete & df_block['cumulative_volume'].notna()
                    incomplete_blocks += int((~keep).sum())
                    df_block = df_block[keep].drop(
                        columns=['prev_reading', 'first_reading', 'last_reading'])
                    if not df_block.empty:
                        block_frames.append(df_block)

                if incomplete_blocks:
                    logger.info(f"{target_date}: {incomplete_blocks} block(s) not fully observed "
                                f"(an edge unobserved beyond the tolerance) — not archived")

                if not block_frames:
                    logger.warning(f"No time block volume data found for {target_date}")
                    return pd.DataFrame()

                result_df = pd.concat(block_frames, ignore_index=True)

            # A block cannot have negative volume — refused, not archived. This
            # table is never pruned, and on 2026-09-07 block 25 was archived
            # negative for 81 of 82 stocks (IDEA -684,393,121 shares), which then
            # failed Volume Block at every close. The engine now stores a falling
            # running total as NULL, so this should never fire; it is the guard.
            negative = result_df['cumulative_volume'] < 0
            for r in result_df[negative].itertuples(index=False):
                logger.warning(f"{target_date}: not archiving negative volume "
                               f"{r.cumulative_volume} for {r.symbol} "
                               f"({r.asset_id}/{r.segment_code}) block {r.time_block}")
            result_df = result_df[~negative]

            logger.info(f"Extracted {len(result_df)} time block volume records")
            return result_df

        except Exception as e:
            logger.error(f"Error extracting time block volumes: {e}")
            return pd.DataFrame()

    # Not close_price: the archive writes no close — the day's official close
    # only arrives in the next trading morning's first reading (fill_closes).
    _OHLC_FIELDS = ('open_price', 'high_price', 'low_price')

    def drop_incomplete_ohlc_rows(self, ohlc_df: pd.DataFrame,
                                  target_date: date) -> pd.DataFrame:
        """Drop each asset whose daily row lacks its open, high or low, logging it.

        Per ASSET, not per day. This used to be a day-wide check in
        validate_data_quality: one NULL anywhere failed the whole fleet, and
        since the 15:34 job does not pass --force, one thin stock with no valid
        open lost that day's daily_ohlc AND time_block_volumes for all 86
        assets, and skipped the ohlc.db prune.

        The row is dropped rather than stored with a NULL: daily_ohlc feeds
        gap-z, beta, ATR and previous close. Its time_block_volumes rows are
        kept — they are volume, and do not depend on the missing price.
        """
        missing = ohlc_df[list(self._OHLC_FIELDS)].isnull()
        incomplete = missing.any(axis=1)
        for idx in ohlc_df.index[incomplete]:
            row = ohlc_df.loc[idx]
            fields = ', '.join(f for f in self._OHLC_FIELDS if missing.at[idx, f])
            logger.warning(
                f"{target_date}: not archiving daily_ohlc for {row['symbol']} "
                f"({row['asset_id']}/{row['segment_code']}) — missing {fields}"
            )
        return ohlc_df[~incomplete]

    def validate_data_quality(self, ohlc_df: pd.DataFrame, tb_df: pd.DataFrame,
                             target_date: date, allow_partial_coverage: bool = False) -> bool:
        """
        Validate extracted data quality before storage.

        Missing OHLC fields are not checked here: drop_incomplete_ohlc_rows has
        already removed those assets, one by one, before this runs.

        Args:
            ohlc_df: Daily OHLC DataFrame
            tb_df: Time block volumes DataFrame
            target_date: Date being validated
            allow_partial_coverage: When True, low time-block coverage (Check 2)
                is logged as a warning but does not block storage on its own —
                a day where the engine started late has legitimately incomplete
                but not corrupt data, and the alternative (permanently losing
                the whole day, with no retry) is worse. Checks 1/3/4 (asset
                coverage, volume sanity, no negative volume) still block
                regardless — those indicate actual corruption, not a partial day. Defaults to
                False so the standalone/manual CLI path keeps its original
                strict all-or-nothing behavior.

        Returns:
            True if data passes quality checks, False otherwise
        """
        issues = []

        # Check 1: Minimum asset coverage
        if len(ohlc_df) < 10:
            issues.append(f"Low asset coverage: {len(ohlc_df)} assets (expected at least 10)")

        # Check 2: Time block coverage
        expected_blocks = len(ohlc_df) * BLOCK_COUNT
        actual_blocks = len(tb_df)
        coverage_ratio = actual_blocks / expected_blocks if expected_blocks > 0 else 0

        if coverage_ratio < 0.7:
            coverage_msg = f"Low time block coverage: {coverage_ratio:.1%} (expected 70%+)"
            if allow_partial_coverage:
                logger.warning(f"{coverage_msg} — storing anyway (partial day, e.g. late engine start)")
            else:
                issues.append(coverage_msg)

        # Check 3: Volume sanity
        zero_volume_count = (ohlc_df['volume'] == 0).sum()
        if zero_volume_count > len(ohlc_df) * 0.1:
            issues.append(f"Too many zero volume assets: {zero_volume_count}")

        # Check 4: No negative volume. extract_time_block_volumes already drops
        # negative blocks and the engine stores a falling running total as NULL,
        # so this is the last guard before a never-pruned table.
        negative_blocks = int((tb_df['cumulative_volume'] < 0).sum())
        negative_days = int((ohlc_df['volume'] < 0).sum())
        if negative_blocks or negative_days:
            issues.append(f"Negative volume: {negative_blocks} block(s), {negative_days} daily row(s)")

        # Report issues
        if issues:
            logger.warning(f"Data quality issues for {target_date}:")
            for issue in issues:
                logger.warning(f"  - {issue}")
            return False

        logger.info(f"Data quality validation passed for {target_date}")
        return True

    def run_extraction(self, target_date: date, dry_run: bool = False,
                      force: bool = False, allow_partial_coverage: bool = False) -> bool:
        """
        Run complete extraction workflow for target date.

        Args:
            target_date: Date to extract
            dry_run: If True, validate but don't store
            force: If True, overwrite existing data
            allow_partial_coverage: If True, low time-block coverage alone
                doesn't block storage — see validate_data_quality's docstring.

        Returns:
            True if extraction successful, False otherwise
        """
        try:
            print("=" * 60)
            print("Daily Historical Data Extraction")
            print(f"Date: {target_date}")
            print("=" * 60)
            print()

            # Step 1: Validate market day
            print("[1/5] Validating market day...", end=" ")
            if not self.validate_market_day(target_date):
                print("✗ (not a trading day)")
                return False
            if session_still_collecting(target_date):
                print(f"✗ (today's session is still collecting until "
                      f"{FINAL_READINGS_CUTOFF:%H:%M} — archive it after then)")
                logger.warning(f"{target_date}: not archived — the session is still collecting "
                               f"until {FINAL_READINGS_CUTOFF:%H:%M}; an archive now would store "
                               f"the morning as the whole day, permanently")
                return False
            print("✓")

            # Step 2: Extract daily OHLC
            print("[2/5] Extracting daily OHLC...", end=" ")
            ohlc_df = self.extract_daily_ohlc(target_date)
            if ohlc_df.empty:
                print("✗ (no data)")
                return False
            print(f"✓ ({len(ohlc_df)} assets)")

            # Step 3: Extract time block volumes
            print("[3/5] Extracting time block volumes...", end=" ")
            tb_df = self.extract_time_block_volumes(target_date)
            if tb_df.empty:
                print("✗ (no data)")
                return False
            print(f"✓ ({len(tb_df)} records)")

            # Step 4: Validate data quality
            print("[4/5] Validating data quality...", end=" ")
            ohlc_df = self.drop_incomplete_ohlc_rows(ohlc_df, target_date)
            if not self.validate_data_quality(ohlc_df, tb_df, target_date,
                                              allow_partial_coverage=allow_partial_coverage):
                print("⚠ (quality issues - check logs)")
                if not force:
                    print("\nUse --force to store data despite quality issues")
                    return False
            print("✓")

            # Step 5: Store in historical database
            if dry_run:
                print("[5/5] Storing in historical.db... ⊘ (dry-run mode)")
                print("\nDry-run complete - no data stored")
                return True

            print("[5/5] Storing in historical.db...", end=" ")

            # Delete existing data if force mode — keeping any close already
            # filled for this day. The archive writes no close; the next
            # trading morning's fill writes it, and that morning will not come
            # again, so deleting it here would lose it for good.
            if force:
                kept = self.historical_db.get_daily_closes(target_date)
                ohlc_df['close_price'] = [
                    kept.get((int(a), s)) for a, s in zip(ohlc_df['asset_id'], ohlc_df['segment_code'])
                ]
                self.historical_db.delete_date_data(target_date)

            # Insert data
            ohlc_count = self.historical_db.bulk_insert_daily_ohlc(ohlc_df, replace=force)
            tb_count = self.historical_db.bulk_insert_time_block_volumes(tb_df)

            print(f"✓ ({ohlc_count} OHLC, {tb_count} time blocks)")
            print()

            # Display summary
            self._print_summary(target_date, ohlc_count, tb_count)

            return True

        except Exception as e:
            logger.error(f"Error in extraction workflow: {e}")
            print(f"\n✗ Extraction failed: {e}")
            return False

    def _print_summary(self, target_date: date, ohlc_count: int, tb_count: int) -> None:
        """Print extraction summary."""
        print("=" * 60)
        print("Extraction Summary")
        print("=" * 60)
        print(f"Date: {target_date}")
        print(f"Daily OHLC records: {ohlc_count}")
        print(f"Time block records: {tb_count}")
        print(f"Status: SUCCESS ✓")
        print()

        # Get historical DB stats
        stats = self.historical_db.get_date_range_stats()
        if stats:
            ohlc_stats = stats.get('ohlc', {})
            print("Historical DB Stats:")
            print(f"- Total trading days: {ohlc_stats.get('trading_days', 0)}")
            print(f"- Date range: {ohlc_stats.get('min_date', 'N/A')} to {ohlc_stats.get('max_date', 'N/A')}")
            print(f"- Total OHLC records: {ohlc_stats.get('total_records', 0):,}")
            print(f"- Total time block records: {stats.get('time_block', {}).get('total_records', 0):,}")
        print("=" * 60)

    def print_stats_only(self) -> None:
        """Print historical database statistics without extraction."""
        print("=" * 60)
        print("Historical Database Statistics")
        print("=" * 60)
        print()

        stats = self.historical_db.get_date_range_stats()
        if not stats:
            print("No data in historical database")
            return

        ohlc_stats = stats.get('ohlc', {})
        tb_stats = stats.get('time_block', {})

        print(f"Date Range: {ohlc_stats.get('min_date', 'N/A')} to {ohlc_stats.get('max_date', 'N/A')}")
        print(f"Trading Days: {ohlc_stats.get('trading_days', 0)}")
        print(f"Unique Assets: {ohlc_stats.get('unique_assets', 0)}")
        print()
        print(f"Daily OHLC Records: {ohlc_stats.get('total_records', 0):,}")
        print(f"Time Block Records: {tb_stats.get('total_records', 0):,}")
        print()

        last_date = self.historical_db.get_last_extraction_date()
        if last_date:
            print(f"Last Extraction: {last_date}")
            days_ago = (date.today() - last_date).days
            if days_ago == 0:
                print("Status: Up to date ✓")
            elif days_ago == 1:
                print("Status: Missing yesterday's data ⚠")
            else:
                print(f"Status: Missing {days_ago} days of data ⚠")

        print("=" * 60)


def prune_ohlc_database(ohlc_db_path: str, keep_days: int = 5, dry_run: bool = False) -> dict:
    """
    Prune ohlc.db to keep only the latest N trading days of data.

    Trading days are identified by distinct dates present in ohlc_data,
    so weekends and holidays are handled automatically (no data = not counted).

    Prunes: ohlc_data, time_block_volumes, fetch_metadata — every table ohlc.db has.

    Args:
        ohlc_db_path: Path to ohlc.db file
        keep_days: Number of most recent trading days to keep (default: 5)
        dry_run: If True, report what would be deleted without deleting

    Returns:
        Dictionary with pruning statistics
    """
    stats = {
        'trading_days_before': 0,
        'trading_days_after': 0,
        'cutoff_date': None,
        'dates_removed': [],
        'ohlc_data_deleted': 0,
        'time_block_volumes_deleted': 0,
        'fetch_metadata_deleted': 0,
    }

    try:
        conn = sqlite3.connect(ohlc_db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.cursor()

        # Get distinct trading days from ohlc_data, most recent first
        cursor.execute("""
            SELECT DISTINCT substr(timestamp, 1, 10) as trade_date
            FROM ohlc_data
            WHERE timestamp IS NOT NULL
            ORDER BY trade_date DESC
        """)
        trading_days = [row[0] for row in cursor.fetchall()]
        stats['trading_days_before'] = len(trading_days)

        if len(trading_days) <= keep_days:
            logger.info(f"Only {len(trading_days)} trading days in ohlc.db "
                       f"(keep_days={keep_days}) - nothing to prune")
            conn.close()
            return stats

        # The cutoff: oldest date to KEEP
        cutoff_date = trading_days[keep_days - 1]
        dates_to_remove = trading_days[keep_days:]
        stats['cutoff_date'] = cutoff_date
        stats['dates_removed'] = dates_to_remove

        if dry_run:
            # Count what would be deleted without deleting
            cursor.execute(
                "SELECT COUNT(*) FROM ohlc_data WHERE substr(timestamp, 1, 10) < ?",
                (cutoff_date,))
            stats['ohlc_data_deleted'] = cursor.fetchone()[0]

            cursor.execute(
                "SELECT COUNT(*) FROM time_block_volumes WHERE date < ?",
                (cutoff_date,))
            stats['time_block_volumes_deleted'] = cursor.fetchone()[0]

            cursor.execute(
                "SELECT COUNT(*) FROM fetch_metadata WHERE substr(timestamp, 1, 10) < ?",
                (cutoff_date,))
            stats['fetch_metadata_deleted'] = cursor.fetchone()[0]

            stats['trading_days_after'] = keep_days
            conn.close()
            return stats

        # Delete old data from all tables
        cursor.execute(
            "DELETE FROM ohlc_data WHERE substr(timestamp, 1, 10) < ?",
            (cutoff_date,))
        stats['ohlc_data_deleted'] = cursor.rowcount

        cursor.execute(
            "DELETE FROM time_block_volumes WHERE date < ?",
            (cutoff_date,))
        stats['time_block_volumes_deleted'] = cursor.rowcount

        cursor.execute(
            "DELETE FROM fetch_metadata WHERE substr(timestamp, 1, 10) < ?",
            (cutoff_date,))
        stats['fetch_metadata_deleted'] = cursor.rowcount

        conn.commit()

        # Verify
        cursor.execute("SELECT COUNT(DISTINCT substr(timestamp, 1, 10)) FROM ohlc_data")
        stats['trading_days_after'] = cursor.fetchone()[0]

        # Try VACUUM to reclaim disk space (may fail if engine is running)
        try:
            logger.info("Running VACUUM to reclaim disk space...")
            conn.execute("VACUUM")
            logger.info("VACUUM completed successfully")
        except Exception as e:
            logger.warning(f"VACUUM skipped (database may be in use by engine): {e}")

        conn.close()
        return stats

    except Exception as e:
        logger.error(f"Error pruning ohlc.db: {e}")
        raise


def _print_prune_summary(stats: dict, dry_run: bool = False) -> None:
    """Print pruning results summary."""
    print()
    print("=" * 60)
    if dry_run:
        print("OHLC Database Pruning (DRY RUN)")
    else:
        print("OHLC Database Pruning")
    print("=" * 60)

    if not stats['cutoff_date']:
        print(f"Nothing to prune - only {stats['trading_days_before']} trading day(s) in ohlc.db")
        print("=" * 60)
        return

    action = "Would delete" if dry_run else "Deleted"

    print(f"Trading days before: {stats['trading_days_before']}")
    print(f"Cutoff date: {stats['cutoff_date']} (keeping this date and newer)")
    print()

    print(f"Dates {action.lower()}:")
    for d in stats['dates_removed']:
        print(f"  - {d}")
    print()

    print(f"Records {action.lower()}:")
    print(f"  ohlc_data:          {stats['ohlc_data_deleted']:,}")
    print(f"  time_block_volumes: {stats['time_block_volumes_deleted']:,}")
    print(f"  fetch_metadata:     {stats['fetch_metadata_deleted']:,}")
    total = (stats['ohlc_data_deleted'] +
             stats['time_block_volumes_deleted'] + stats['fetch_metadata_deleted'])
    print(f"  TOTAL:              {total:,}")

    if not dry_run:
        print()
        print(f"Trading days after: {stats['trading_days_after']}")

    print("=" * 60)


def main():
    """Main entry point for daily extraction script."""
    parser = argparse.ArgumentParser(
        description="Extract daily historical data from ohlc.db to historical.db"
    )
    parser.add_argument(
        '--date',
        type=str,
        help='Date to extract (YYYY-MM-DD format, default: today)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Validate without storing data'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Overwrite existing data for the date'
    )
    parser.add_argument(
        '--stats-only',
        action='store_true',
        help='Show database statistics only (no extraction)'
    )
    parser.add_argument(
        '--prune',
        action='store_true',
        help='Prune ohlc.db after extraction, keeping only the latest N trading days'
    )
    parser.add_argument(
        '--keep-days',
        type=int,
        default=5,
        help='Number of most recent trading days to keep in ohlc.db (default: 5, used with --prune)'
    )
    parser.add_argument(
        '--allow-partial-coverage',
        action='store_true',
        help='Store the day even if time-block coverage is low (e.g. late engine start); '
             'the other quality checks (asset coverage, volume sanity, no negative volume) still block'
    )

    args = parser.parse_args()

    _configure_logging()

    # Determine target date
    if args.date:
        try:
            target_date = datetime.strptime(args.date, '%Y-%m-%d').date()
        except ValueError:
            print(f"Error: Invalid date format '{args.date}'. Use YYYY-MM-DD")
            sys.exit(1)
    else:
        target_date = date.today()

    # Initialize extractor with absolute paths
    ohlc_db_path = str(PROJECT_ROOT / "data" / "ohlc.db")
    historical_db_path = str(PROJECT_ROOT / "data" / "historical.db")

    if not Path(ohlc_db_path).exists():
        print(f"Error: OHLC database not found at {ohlc_db_path}")
        sys.exit(1)

    extractor = DailyHistoricalExtractor(ohlc_db_path, historical_db_path)

    # Stats-only mode
    if args.stats_only:
        extractor.print_stats_only()
        sys.exit(0)

    # Run extraction
    start_time = datetime.now()
    success = extractor.run_extraction(target_date, dry_run=args.dry_run, force=args.force,
                                       allow_partial_coverage=args.allow_partial_coverage)
    duration = (datetime.now() - start_time).total_seconds()

    if success:
        logger.info(f"Extraction completed successfully in {duration:.1f}s")
    else:
        logger.error(f"Extraction failed after {duration:.1f}s")

    # Run pruning if requested (only after successful extraction)
    if args.prune:
        if not success:
            print("\nSkipping pruning - extraction failed. Data in ohlc.db is preserved.")
        else:
            prune_stats = prune_ohlc_database(
                ohlc_db_path, keep_days=args.keep_days, dry_run=args.dry_run)
            _print_prune_summary(prune_stats, dry_run=args.dry_run)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
