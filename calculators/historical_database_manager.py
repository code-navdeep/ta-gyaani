"""
Historical Database Manager

Manages permanent storage of historical market data for statistical analysis:
daily OHLC and 15-minute block volumes (kept indefinitely), the per-block
snapshot store (kept ~420 calendar days) and the beta cache.

Architecture:
- Read/write operations for ETL scripts (daily extraction)
- Read-only operations for calculators (analysis queries)
- Optimized schema with date-based indexing
- Prevents duplicate insertions with date-based primary keys

Database: historical.db (permanent storage)
Tables:
- daily_ohlc: Daily open, high, low, close, volume, vwap per asset
- time_block_volumes: 15-min time block volumes (1-25) per asset per day (09:15–15:30)
- intraday_time_block_metrics: shared per-block signal store — RAW OBSERVATIONS ONLY (session_price_move, vwap_displacement, pvd_raw) per (date, asset, 15-min block); written just after each block's readings end, from that block's own readings in ohlc.db (calculators/block_snapshots.py), and rebuilt by scripts/backfill.py
- beta_cache: Rolling β per (stock, benchmark) at 4 tiers (22/90/180/252d); populated daily by scripts/precompute_beta.py
"""

import logging
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
import threading
import hashlib
import json

from calculators.time_blocks import IST, seconds_until_next_block_end


#: Grace added to a block-aligned cache TTL, in seconds, so that an entry cached
#: just before a boundary outlives the shared store's write for the block that
#: just ended. Do not reduce it to 0 to make the expiry "exact".
#:
#: That row is written by MetricsOrchestrator._maybe_write_intraday_block on
#: the first metrics cycle after the block's readings end; cycles run ~34 s
#: apart (12-51 s measured on 2026-09-07), so 45 s covers a typical one. An
#: entry expiring exactly at the boundary could re-query before the write,
#: cache the answer without the new row, and keep it for the whole next block.
#:
#: What that would cost TODAY is small: every reader that compares against this
#: history either passes exclude_date (VWAP, Rolling Momentum, PV Divergence —
#: whose cache key also names the current block, so it changes at every
#: boundary anyway) or drops today's row by joining it with daily_ohlc, which
#: has no row for today until 15:34 (Momentum, Relative Strength). The grace
#: keeps it that way for any future reader that does see today's rows.
_BLOCK_TTL_EPSILON_SECONDS = 45.0


def _block_aligned_cache_ttl(now: Optional[datetime] = None) -> Optional[float]:
    """Seconds until the next 15-min block boundary, plus the write grace.

    Returns None after the final block (15:30), when there is no next boundary
    today — the caller then falls back to its flat default rather than caching
    until tomorrow morning.
    """
    remaining = seconds_until_next_block_end(now or datetime.now(IST))
    if remaining is None:
        return None
    return remaining + _BLOCK_TTL_EPSILON_SECONDS
import time

logger = logging.getLogger(__name__)


class DatabaseCache:
    """
    Thread-safe cache for database query results with TTL-based expiration.
    """

    def __init__(self, default_ttl_seconds: float = 45.0, max_size: int = 100):
        """
        Initialize cache with TTL and size limits.

        Args:
            default_ttl_seconds: Default time-to-live for cached entries
            max_size: Maximum number of entries to cache
        """
        self._cache = {}
        self._lock = threading.RLock()
        self.default_ttl = default_ttl_seconds
        self.max_size = max_size

    def _generate_key(self, method_name: str, *args, **kwargs) -> str:
        """Generate cache key from method name and arguments."""
        key_data = {
            'method': method_name,
            'args': str(args),
            'kwargs': str(sorted(kwargs.items()))
        }
        key_string = json.dumps(key_data, sort_keys=True)
        return hashlib.md5(key_string.encode()).hexdigest()

    def get(self, method_name: str, *args, **kwargs):
        """Retrieve cached result if valid."""
        key = self._generate_key(method_name, *args, **kwargs)

        with self._lock:
            if key in self._cache:
                entry = self._cache[key]
                if time.time() < entry['expires_at']:
                    return entry['data']
                else:
                    del self._cache[key]
        return None

    def set(self, method_name: str, data, ttl: float = None, *args, **kwargs):
        """Store result in cache with TTL."""
        key = self._generate_key(method_name, *args, **kwargs)
        ttl = ttl if ttl is not None else self.default_ttl

        with self._lock:
            # Enforce max size
            if len(self._cache) >= self.max_size:
                oldest_key = min(self._cache.keys(),
                               key=lambda k: self._cache[k]['expires_at'])
                del self._cache[oldest_key]

            self._cache[key] = {
                'data': data,
                'expires_at': time.time() + ttl,
                'created_at': time.time()
            }

    def clear(self):
        """Clear all cached entries."""
        with self._lock:
            self._cache.clear()

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            now = time.time()
            expired = sum(1 for e in self._cache.values() if now >= e['expires_at'])
            return {
                'total_entries': len(self._cache),
                'expired_entries': expired,
                'active_entries': len(self._cache) - expired,
                'max_size': self.max_size,
                'default_ttl': self.default_ttl
            }


class HistoricalDatabaseManager:
    """
    Base class for historical database operations.
    Provides connection management and schema initialization.
    """

    def __init__(self, db_path: str, read_only: bool = False):
        """
        Initialize historical database manager.

        Args:
            db_path: Path to historical.db file
            read_only: If True, open in read-only mode (for calculators)
        """
        self.db_path = db_path
        self.read_only = read_only
        self._local = threading.local()

        # Ensure database directory exists
        db_dir = Path(db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        # Initialize database schema if not exists
        if not read_only:
            self.initialize_database()

        logger.info(f"Historical database manager initialized: {db_path} (read_only={read_only})")

    def _get_connection(self) -> sqlite3.Connection:
        """
        Get thread-local database connection.

        Returns:
            Thread-safe SQLite connection
        """
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            if self.read_only:
                # Read-only mode with WAL for concurrent access
                uri = f"file:{self.db_path}?mode=ro"
                self._local.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            else:
                # Read-write mode
                self._local.conn = sqlite3.connect(self.db_path, check_same_thread=False)

            # Performance optimizations
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
            self._local.conn.execute("PRAGMA temp_store=MEMORY")

        return self._local.conn

    def initialize_database(self) -> None:
        """
        Initialize database schema with tables and indexes.
        Safe to call multiple times (CREATE IF NOT EXISTS).

        `created_at` carries NO DEFAULT, deliberately. `DEFAULT
        CURRENT_TIMESTAMP` evaluates as UTC, space-separated, which sorts
        before every 'T'-separated IST cutoff as TEXT and would be invisible to
        a `>=` comparison forever. Every writer passes
        `datetime.now(IST).isoformat()` explicitly; an omission now writes a
        loud NULL instead of a silently unmatchable value.
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # Table 1: Daily OHLC data
            # VOLUME SEMANTICS:
            # - volume = total daily volume for this asset
            # - Populated using MAX(volume) from ohlc_data (gets final cumulative value)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_ohlc (
                    date DATE NOT NULL,
                    asset_id INTEGER NOT NULL,
                    segment_code TEXT NOT NULL,
                    symbol TEXT,
                    open_price REAL,
                    high_price REAL,
                    low_price REAL,
                    close_price REAL,
                    volume INTEGER,
                    vwap REAL,
                    created_at TIMESTAMP,
                    PRIMARY KEY (date, asset_id, segment_code)
                )
            """)

            # Table 2: Time block volumes (25 15-min blocks per day)
            # VOLUME SEMANTICS:
            # - cumulative_volume = total volume within this specific 15-min block
            #   (NOT cumulative across day). Historical naming: "cumulative" is a legacy
            #   name from an earlier design; the value is actually the block-total sum.
            # - Populated using SUM(interval_volume) from all 5-second snapshots in
            #   this 15-min block.
            # - Time blocks are 1..25 covering 09:15–15:30 (375 min ÷ 15 = 25 exact).
            #   No CHECK constraint, deliberately — forward-compatible.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS time_block_volumes (
                    date DATE NOT NULL,
                    asset_id INTEGER NOT NULL,
                    segment_code TEXT NOT NULL,
                    symbol TEXT,
                    time_block INTEGER NOT NULL,
                    cumulative_volume INTEGER,
                    created_at TIMESTAMP,
                    PRIMARY KEY (date, asset_id, segment_code, time_block)
                )
            """)

            # Indexes for fast date-based queries
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_daily_ohlc_date
                ON daily_ohlc(date DESC)
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_daily_ohlc_asset
                ON daily_ohlc(asset_id, date DESC)
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_time_block_date
                ON time_block_volumes(date DESC)
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_time_block_asset
                ON time_block_volumes(asset_id, time_block, date DESC)
            """)

            # Table 3: Intraday time block metrics (shared per-block signal store)
            # Written by metrics.py just after each block's readings end, from the
            # block's own readings in ohlc.db (block_snapshots.block_end_values);
            # scripts/backfill.py rebuilds it the same way.
            # Columns, one per locked calculator that needs cross-day same-block history.
            #
            # INVARIANT: this table stores RAW OBSERVATIONS ONLY — never a value
            # normalised against history. A stored z is frozen against whatever
            # history existed the moment it was written and goes stale as soon as
            # more history is backfilled, and it can only ever represent one tier.
            #   - session_price_move → Rolling Momentum (every block), and Momentum
            #     and Relative Strength (block 1, to rebuild the 09:15→09:30 move)
            #   - vwap_displacement → VWAP adapter
            #   - pvd_raw → PV Divergence adapter (needs 5-second readings, so it
            #     cannot come from candles; scripts/backfill.py can rebuild it from
            #     the days ohlc.db still holds)
            #
            # `block_volume_z_90` used to sit here and was the sole exception —
            # a derived z, at one tier. Volume Dynamics now receives the ZQ
            # series in memory from Volume Block's same-cycle output at all four
            # tiers, so the column has no consumer and is gone.
            # Retention: 420 CALENDAR days (~284 trading days), sized so the
            # 252-TRADING-day top tier can actually fill with headroom to
            # spare. See cleanup_old_intraday_metrics for the unit conversion —
            # the prior "~280" figure added a buffer to 252 without converting
            # units and capped this store at ~192 trading days.
            # No CHECK constraint on time_block — forward-compatible.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS intraday_time_block_metrics (
                    date DATE NOT NULL,
                    asset_id INTEGER NOT NULL,
                    segment_code TEXT NOT NULL,
                    symbol TEXT,
                    time_block INTEGER NOT NULL,
                    session_price_move REAL,
                    vwap_displacement REAL,
                    pvd_raw REAL,
                    created_at TIMESTAMP,
                    PRIMARY KEY (date, asset_id, segment_code, time_block)
                )
            """)

            # Table 4: Beta cache (Relative Strength's β per tier)
            # Populated once per trading day by scripts/precompute_beta.py, run by the
            # metrics thread's morning step right after the previous close is filled.
            # One row per (asset, benchmark) pair; ~117 rows total. Overwritten daily, never pruned.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS beta_cache (
                    asset_id INTEGER NOT NULL,
                    segment_code TEXT NOT NULL,
                    benchmark_name TEXT NOT NULL,
                    beta_22d REAL,
                    beta_90d REAL,
                    beta_180d REAL,
                    beta_252d REAL,
                    computed_at TEXT,
                    PRIMARY KEY (asset_id, segment_code, benchmark_name)
                )
            """)

            # Index for intraday_time_block_metrics — supports the ROW_NUMBER OVER PARTITION BY
            # (asset_id, segment_code, time_block) ORDER BY date DESC query pattern that the
            # reader will use for bulk historical fetches per tier.
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_intraday_time_block_asset
                ON intraday_time_block_metrics(asset_id, segment_code, time_block, date DESC)
            """)

            conn.commit()
            logger.info("Historical database schema initialized successfully")

        except Exception as e:
            logger.error(f"Error initializing database schema: {e}")
            raise

    def get_date_range_stats(self) -> Dict[str, Any]:
        """
        Get statistics about available historical data.

        Returns:
            Dictionary with date range and record counts
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # Query OHLC statistics
            cursor.execute("""
                SELECT
                    MIN(date) as min_date,
                    MAX(date) as max_date,
                    COUNT(*) as total_records,
                    COUNT(DISTINCT date) as trading_days,
                    COUNT(DISTINCT asset_id) as unique_assets
                FROM daily_ohlc
            """)
            ohlc_stats = cursor.fetchone()

            # Query time block statistics
            cursor.execute("""
                SELECT
                    COUNT(*) as total_records,
                    COUNT(DISTINCT date) as trading_days
                FROM time_block_volumes
            """)
            tb_stats = cursor.fetchone()

            return {
                'ohlc': {
                    'min_date': ohlc_stats[0] if ohlc_stats[0] else None,
                    'max_date': ohlc_stats[1] if ohlc_stats[1] else None,
                    'total_records': ohlc_stats[2],
                    'trading_days': ohlc_stats[3],
                    'unique_assets': ohlc_stats[4]
                },
                'time_block': {
                    'total_records': tb_stats[0],
                    'trading_days': tb_stats[1]
                }
            }

        except Exception as e:
            logger.error(f"Error getting date range stats: {e}")
            return {}


class HistoricalDatabaseWriter(HistoricalDatabaseManager):
    """
    Write operations for historical database.
    Used by ETL scripts (daily extraction, backfill).
    """

    def __init__(self, db_path: str):
        """Initialize writer with read-write access."""
        super().__init__(db_path, read_only=False)

    def bulk_insert_daily_ohlc(self, df: pd.DataFrame, replace: bool = False) -> int:
        """
        Bulk insert daily OHLC data from DataFrame.

        Args:
            df: DataFrame with columns: date, asset_id, segment_code, symbol, open_price, high_price,
                low_price, close_price, volume, vwap
            replace: True only when the caller has already DELETED these rows' date
                (a --force re-archive): the rows are then simply appended.

        Returns:
            Rows actually written: added, or (without `replace`) an existing row
            that gained at least one value. A row already complete is not counted.
        """
        try:
            if df.empty:
                logger.warning("Empty DataFrame provided to bulk_insert_daily_ohlc")
                return 0

            conn = self._get_connection()

            # Validate required columns
            required_cols = ['date', 'asset_id', 'segment_code', 'open_price', 'high_price',
                           'low_price', 'close_price', 'volume']
            missing_cols = [col for col in required_cols if col not in df.columns]
            if missing_cols:
                raise ValueError(f"Missing required columns: {missing_cols}")

            # Add symbol if not present
            if 'symbol' not in df.columns:
                df['symbol'] = None

            # Add vwap if not present
            if 'vwap' not in df.columns:
                df['vwap'] = None

            # Convert date column to string format
            df['date'] = pd.to_datetime(df['date']).dt.date.astype(str)

            # Select and order columns
            insert_df = df[['date', 'asset_id', 'segment_code', 'symbol', 'open_price', 'high_price',
                          'low_price', 'close_price', 'volume', 'vwap']].copy()
            insert_df['created_at'] = datetime.now(IST).isoformat()

            if replace:
                # Plain append — NOT a replace of existing rows. Safe only
                # because a --force caller deletes the date first; were a row
                # still there, the primary key would reject the append.
                insert_df.to_sql('daily_ohlc', conn, if_exists='append',
                               index=False, method='multi')
                written = len(insert_df)
            else:
                # An existing row keeps every value it has; only its missing
                # fields are filled. Not INSERT OR IGNORE: the morning fill can
                # insert a row holding ONLY a close (the day's archive failed or
                # did not run), and IGNORE would then skip the day's re-archive
                # entirely — a close with no open, high or low, silently.
                #
                # The WHERE touches a row only when it GAINS a value, so the
                # count below is rows added or completed. Without it SQLite
                # counts every conflicting row as updated, and re-archiving a
                # complete day reported every row as written.
                cursor = conn.cursor()
                cursor.executemany("""
                    INSERT INTO daily_ohlc
                    (date, asset_id, segment_code, symbol, open_price, high_price, low_price,
                     close_price, volume, vwap, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date, asset_id, segment_code) DO UPDATE SET
                        symbol      = COALESCE(daily_ohlc.symbol,      excluded.symbol),
                        open_price  = COALESCE(daily_ohlc.open_price,  excluded.open_price),
                        high_price  = COALESCE(daily_ohlc.high_price,  excluded.high_price),
                        low_price   = COALESCE(daily_ohlc.low_price,   excluded.low_price),
                        close_price = COALESCE(daily_ohlc.close_price, excluded.close_price),
                        volume      = COALESCE(daily_ohlc.volume,      excluded.volume),
                        vwap        = COALESCE(daily_ohlc.vwap,        excluded.vwap)
                    WHERE (daily_ohlc.symbol      IS NULL AND excluded.symbol      IS NOT NULL)
                       OR (daily_ohlc.open_price  IS NULL AND excluded.open_price  IS NOT NULL)
                       OR (daily_ohlc.high_price  IS NULL AND excluded.high_price  IS NOT NULL)
                       OR (daily_ohlc.low_price   IS NULL AND excluded.low_price   IS NOT NULL)
                       OR (daily_ohlc.close_price IS NULL AND excluded.close_price IS NOT NULL)
                       OR (daily_ohlc.volume      IS NULL AND excluded.volume      IS NOT NULL)
                       OR (daily_ohlc.vwap        IS NULL AND excluded.vwap        IS NOT NULL)
                """, insert_df.values.tolist())
                written = cursor.rowcount
                conn.commit()

            logger.info(f"daily_ohlc: wrote {written} of {len(insert_df)} rows")
            return written

        except Exception as e:
            logger.error(f"Error in bulk_insert_daily_ohlc: {e}")
            raise

    def bulk_insert_time_block_volumes(self, df: pd.DataFrame) -> int:
        """
        Bulk insert time block volume data from DataFrame.

        A block already stored for that (date, asset, segment) is left as it
        is — never replaced. A --force re-archive deletes the day first
        (delete_date_data), which is what lets it write the day afresh.

        Args:
            df: DataFrame with columns: date, asset_id, segment_code, symbol, time_block,
                cumulative_volume

        Returns:
            Rows actually inserted; a block already present is not counted.
        """
        try:
            if df.empty:
                logger.warning("Empty DataFrame provided to bulk_insert_time_block_volumes")
                return 0

            conn = self._get_connection()

            # Validate required columns
            required_cols = ['date', 'asset_id', 'segment_code', 'time_block', 'cumulative_volume']
            missing_cols = [col for col in required_cols if col not in df.columns]
            if missing_cols:
                raise ValueError(f"Missing required columns: {missing_cols}")

            # Add symbol if not present
            if 'symbol' not in df.columns:
                df['symbol'] = None

            # Validate time_block values (25 15-min blocks / session).
            invalid_blocks = df[~df['time_block'].between(1, 25)]
            if not invalid_blocks.empty:
                raise ValueError(f"Invalid time_block values (must be 1-25): {invalid_blocks['time_block'].unique()}")

            # Convert date column to string format
            df['date'] = pd.to_datetime(df['date']).dt.date.astype(str)

            # Select and order columns
            insert_df = df[['date', 'asset_id', 'segment_code', 'symbol', 'time_block',
                          'cumulative_volume']].copy()
            insert_df['created_at'] = datetime.now(IST).isoformat()

            # INSERT OR IGNORE: a block already stored is kept, and not counted.
            cursor = conn.cursor()
            cursor.executemany("""
                INSERT OR IGNORE INTO time_block_volumes
                (date, asset_id, segment_code, symbol, time_block, cumulative_volume, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, insert_df.values.tolist())
            inserted = cursor.rowcount
            conn.commit()

            logger.info(f"time_block_volumes: inserted {inserted} of {len(insert_df)} rows")
            return inserted

        except Exception as e:
            logger.error(f"Error in bulk_insert_time_block_volumes: {e}")
            raise

    def delete_date_data(self, target_date: date) -> Tuple[int, int]:
        """
        Delete all data for a specific date (useful for re-extraction).

        Args:
            target_date: Date to delete

        Returns:
            Tuple of (ohlc_records_deleted, time_block_records_deleted)
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            date_str = target_date.isoformat()

            # Delete from daily_ohlc
            cursor.execute("DELETE FROM daily_ohlc WHERE date = ?", (date_str,))
            ohlc_deleted = cursor.rowcount

            # Delete from time_block_volumes
            cursor.execute("DELETE FROM time_block_volumes WHERE date = ?", (date_str,))
            tb_deleted = cursor.rowcount

            conn.commit()

            logger.info(f"Deleted data for {date_str}: {ohlc_deleted} OHLC, {tb_deleted} time blocks")
            return ohlc_deleted, tb_deleted

        except Exception as e:
            logger.error(f"Error deleting data for date {target_date}: {e}")
            raise

    def fill_closes(self, target_date: date, closes: pd.DataFrame) -> int:
        """
        Write each asset's official close into `daily_ohlc` for `target_date`.

        The day's close is not known when the day is archived (15:34): it
        arrives the next trading morning, as the `close_price` of that
        morning's first reading. So the archive writes each row without a close
        and this fills it. If the day has no row for an asset — the app did not
        run that day, or its archive failed — a row holding only the close is
        inserted, so the next session's previous close is still right.

        Args:
            target_date: the PREVIOUS TRADING day of the morning whose first
                readings supplied the closes (market_calendar.previous_trading_day).
            closes: DataFrame[asset_id, segment_code, symbol, close_price].

        Returns:
            Number of assets written.
        """
        if closes is None or closes.empty:
            return 0
        conn = self._get_connection()
        created_at = datetime.now(IST).isoformat()
        rows = [(target_date.isoformat(), int(r.asset_id), r.segment_code, r.symbol,
                 float(r.close_price), created_at)
                for r in closes.itertuples(index=False)]
        conn.executemany("""
            INSERT INTO daily_ohlc (date, asset_id, segment_code, symbol, close_price, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, asset_id, segment_code) DO UPDATE SET
                close_price = excluded.close_price
        """, rows)
        conn.commit()
        logger.info(f"Filled {len(rows)} closes for {target_date.isoformat()}")
        return len(rows)

    def get_daily_closes(self, target_date: date) -> Dict[Tuple[int, str], float]:
        """The closes already stored for `target_date`, keyed (asset_id,
        segment_code). A --force re-archive deletes the day's rows first; it
        uses this to keep the closes the next morning's fill wrote."""
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT asset_id, segment_code, close_price FROM daily_ohlc "
            "WHERE date = ? AND close_price IS NOT NULL",
            (target_date.isoformat(),))
        return {(int(a), s): float(c) for a, s, c in cursor.fetchall()}

    def get_last_extraction_date(self) -> Optional[date]:
        """
        Get the most recent date in the database.

        Returns:
            Most recent date or None if database is empty
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("SELECT MAX(date) FROM daily_ohlc")
            result = cursor.fetchone()

            if result[0]:
                return datetime.strptime(result[0], '%Y-%m-%d').date()
            return None

        except Exception as e:
            logger.error(f"Error getting last extraction date: {e}")
            return None

    def store_intraday_time_block_metrics(self, rows: List[Dict[str, Any]]) -> int:
        """
        Bulk insert intraday time block metric rows.

        Called by `metrics.py` just after each block's readings end, and by
        scripts/backfill.py when it rebuilds a day. One row per (date, asset_id,
        segment_code, time_block). A metric is None when its readings could not
        be trusted — see calculators/block_snapshots.py.

        Uses INSERT OR REPLACE, so a rewrite of the same block (a backfill
        rebuilding a day) overwrites cleanly with no primary-key conflict.

        Args:
            rows: List of dicts. Each dict has:
                - date (DATE, required)
                - asset_id (int, required)
                - segment_code (str, required)
                - symbol (str, optional)
                - time_block (int, required, 1..25)
                - session_price_move (float or None)
                - vwap_displacement (float or None)
                - pvd_raw (float or None)

        Returns:
            Number of rows written. 0 if input is empty or on error.
        """
        try:
            if not rows:
                logger.debug("store_intraday_time_block_metrics called with empty rows")
                return 0

            conn = self._get_connection()
            cursor = conn.cursor()

            values = [
                (
                    r.get('date'),
                    r.get('asset_id'),
                    r.get('segment_code'),
                    r.get('symbol'),
                    r.get('time_block'),
                    r.get('session_price_move'),
                    r.get('vwap_displacement'),
                    r.get('pvd_raw'),
                    datetime.now(IST).isoformat(),
                )
                for r in rows
            ]

            cursor.executemany("""
                INSERT OR REPLACE INTO intraday_time_block_metrics
                (date, asset_id, segment_code, symbol, time_block,
                 session_price_move, vwap_displacement, pvd_raw, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, values)
            conn.commit()

            n = len(values)
            logger.info(f"Stored {n} intraday_time_block_metrics rows")
            return n

        except Exception as e:
            logger.error(f"Error in store_intraday_time_block_metrics: {e}")
            return 0

    def cleanup_old_intraday_metrics(self, days_to_keep: int = 420) -> int:
        """
        Delete `intraday_time_block_metrics` rows older than `days_to_keep`.

        UNITS: `days_to_keep` is CALENDAR days. Every z-score tier is counted in
        TRADING days. Conflating the two is what made the previous 280 default
        wrong: it read as "252 trading days + 30 buffer", but 280 *calendar*
        days is only ~193 trading days, so this prune deleted samples before the
        252d tier could ever accumulate them. That tier was unreachable by
        construction, not by lack of runtime.

        The 252d top tier needs exactly 252 NON-NULL same-block samples at one
        per trading day, so what matters is the headroom above 252:

            420 calendar days x 5/7       = 300 weekdays
            - ~16 weekday market holidays = ~284 trading days  <- this default
            - 252                         = ~32 samples of headroom

        The previous 380 gave ~257 trading days — ~5 of headroom, so a handful
        of missed or NULL block writes in a year (a late engine start, a
        restart across a boundary) kept the 252d tier empty for that block,
        with no error: indistinguishable from "still accumulating". The
        FETCH_MULTIPLIER slack in statistics.py cannot help here; it only
        widens the fetch, and this prune is the ceiling on what exists.

        Storage at 420: ~2,150 rows/trading-day (86 assets x 25 blocks, indices
        included) x ~284 trading days = ~611,000 rows across three REAL metric
        columns plus keys.

        Called from `DailySchedulerThread` Job 2b.

        Args:
            days_to_keep: Rolling window in *calendar* days (not trading days) to preserve.

        Returns:
            Number of rows deleted. 0 on empty deletion or error.
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cutoff_date = (datetime.now(IST) - timedelta(days=days_to_keep)).date().isoformat()

            cursor.execute("""
                DELETE FROM intraday_time_block_metrics
                WHERE date < ?
            """, (cutoff_date,))
            deleted = cursor.rowcount
            conn.commit()

            logger.info(f"cleanup_old_intraday_metrics: deleted {deleted} rows older than {cutoff_date}")
            return deleted

        except Exception as e:
            logger.error(f"Error in cleanup_old_intraday_metrics: {e}")
            return 0

    def store_beta_cache(self, asset_id: int, segment_code: str, benchmark_name: str,
                         betas: Dict[str, Optional[float]], computed_at: str) -> None:
        """
        Insert-or-replace one row into beta_cache.

        Called from scripts/precompute_beta.py once per (stock, benchmark) pair
        during the metrics thread's morning step (β refit, right after the
        previous close is filled). Composite PK on
        (asset_id, segment_code, benchmark_name) — daily overwrite semantics.

        Args:
            asset_id: Stock asset ID.
            segment_code: Stock segment code (typically 'NSE_EQ').
            benchmark_name: One of 'Nifty', 'BankNifty', 'MidcapNifty', 'Sensex'.
            betas: Dict with keys 'beta_22d', 'beta_90d', 'beta_180d', 'beta_252d'
                   (each float or None if that tier has insufficient paired returns).
            computed_at: ISO timestamp of the precompute run.
        """
        conn = self._get_connection()
        conn.execute("""
            INSERT OR REPLACE INTO beta_cache
            (asset_id, segment_code, benchmark_name,
             beta_22d, beta_90d, beta_180d, beta_252d, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            asset_id,
            segment_code,
            benchmark_name,
            betas.get('beta_22d'),
            betas.get('beta_90d'),
            betas.get('beta_180d'),
            betas.get('beta_252d'),
            computed_at,
        ))


class HistoricalDatabaseReader(HistoricalDatabaseManager):
    """
    Read-only operations for historical database.
    Used by calculators for analysis queries.
    """

    def __init__(self, db_path: str, enable_cache: bool = True, cache_ttl: float = 60.0):
        """
        Initialize reader with read-only access and optional caching.

        Args:
            db_path: Path to historical.db file
            enable_cache: Enable query result caching
            cache_ttl: Cache time-to-live in seconds (default 60s for historical data)
        """
        super().__init__(db_path, read_only=True)
        self.cache_enabled = enable_cache
        # 512, not 100: Relative Strength alone issues ~246 distinct keys a
        # cycle, and this cache evicts the entry expiring soonest — at 100 the
        # per-stock entries were evicted before they could ever be hit.
        self.cache = DatabaseCache(default_ttl_seconds=cache_ttl, max_size=512) if enable_cache else None

    def _with_cache(self, method_name: str, query_func, *args,
                    _align_to_block: bool = False, **kwargs):
        """
        Execute query with caching if enabled.

        Args:
            method_name: Name of the method for cache key generation
            query_func: Function to execute if cache miss
            _align_to_block: expire at the next 15-min block boundary (plus the
                write grace) instead of on the flat TTL. For the two methods
                whose underlying data only changes at those boundaries.
            *args, **kwargs: Arguments for cache key and query function

        Returns:
            Query result (cached or fresh)

        `_align_to_block` is KEYWORD-ONLY and deliberately not a TTL number:

          * **kwargs feeds the cache key, so a `_ttl=...` keyword would become
            part of that key and silently fragment one logical entry into a
            separate cached copy per distinct TTL value — every call a miss.
          * the TTL has to be measured when the entry is STORED, not when the
            call is made. The block-25 fetch takes ~1 s; sizing the TTL before
            the query would leave the entry expiring that much past the
            boundary.
        """
        if not self.cache_enabled or self.cache is None:
            return query_func(*args, **kwargs)

        # Try cache first
        cached_result = self.cache.get(method_name, *args, **kwargs)
        if cached_result is not None:
            return cached_result

        # Cache miss - execute query
        result = query_func(*args, **kwargs)

        # TTL computed HERE, after the query, so the boundary alignment is not
        # skewed by query duration. None -> the cache's flat default.
        ttl = _block_aligned_cache_ttl() if _align_to_block else None

        # Note: args/kwargs are passed to set() only for cache key generation.
        # They follow `ttl` positionally to avoid colliding with it.
        self.cache.set(method_name, result, ttl, *args, **kwargs)

        return result

    def get_previous_close_data(self, asset_ids: List[int] = None, segment_codes: List[str] = None) -> pd.DataFrame:
        """
        Get previous trading day's closing prices for specified assets.

        Fast alternative to querying ohlc.db - uses pre-aggregated daily_ohlc data.

        Args:
            asset_ids: Optional list of asset IDs to filter by (None = all assets)
            segment_codes: Optional list of segment codes corresponding to asset_ids

        Returns:
            DataFrame with columns: asset_id, previous_close
        """
        def _execute_query(asset_ids, segment_codes):
            try:
                conn = self._get_connection()

                # Each asset's NEWEST row — not the second newest. During a
                # session that is the previous trading day's row, whose close
                # the morning step filled from today's first reading: exactly
                # the previous close. Taking the second newest would make it
                # two sessions old.
                #
                # From the 15:34 archive until the next morning's fill, the
                # newest row is TODAY's, archived without a close (the close is
                # only known the next morning), so this returns no close then.
                # No calculator runs in that window; the price chart does not
                # use this lookup (it asks get_close_on for the day before the
                # session it shows).
                base_query = """
                    WITH ranked_dates AS (
                        SELECT asset_id,
                               segment_code,
                               close_price as previous_close,
                               ROW_NUMBER() OVER (
                                   PARTITION BY asset_id, segment_code
                                   ORDER BY date DESC
                               ) as rn
                        FROM daily_ohlc
                """

                conditions = []
                params = []

                if asset_ids and segment_codes:
                    asset_placeholders = ','.join('?' * len(asset_ids))
                    segment_placeholders = ','.join('?' * len(segment_codes))
                    conditions.append(f"asset_id IN ({asset_placeholders})")
                    conditions.append(f"segment_code IN ({segment_placeholders})")
                    params.extend(asset_ids)
                    params.extend(segment_codes)

                if conditions:
                    base_query += " WHERE " + " AND ".join(conditions)

                query = base_query + """
                    )
                    SELECT asset_id, segment_code, previous_close
                    FROM ranked_dates
                    WHERE rn = 1
                """

                df = pd.read_sql_query(query, conn, params=params)
                logger.debug(f"Fetched previous close data for {len(df)} assets from historical.db")
                return df

            except Exception as e:
                logger.error(f"Error getting previous close data from historical.db: {e}")
                return pd.DataFrame()

        return self._with_cache("get_previous_close_data", _execute_query, asset_ids, segment_codes)

    def get_historical_gap_data(self,
                                asset_ids: List[int],
                                segment_codes: List[str],
                                days_back: int) -> pd.DataFrame:
        """
        Get historical daily gaps for Momentum's gap z-score.

        For each asset, pulls (days_back + 1) most-recent `daily_ohlc` rows via a
        ROW_NUMBER OVER PARTITION per-asset row cap (mirrors the pattern from
        `get_historical_time_block_volumes`), then computes gaps in pandas via
        per-asset `.shift()`:

            gap = (open_price - prev_close) / prev_close * 100

        The extra +1 row is needed to have a `prev_close` for the earliest date
        in the tier window. Days with a NaN prev_close (the earliest per asset)
        are dropped from the returned frame.

        Consumers: momentum_adapter (gap z at all four tiers, and with the block-1
        snapshots the opening-move history) and relative_strength_adapter (the
        opening-window return history).

        Args:
            asset_ids: List of asset identifiers.
            segment_codes: List of segment codes corresponding to asset_ids.
            days_back: Trading days of gap history per asset. Callers pass
                       statistics.FETCH_DAYS (504): the 252-day top tier plus
                       slack; each tier then takes exactly its own N.

        Returns:
            DataFrame with columns: date, asset_id, segment_code, symbol, gap.
            Empty if inputs are empty or the query fails.
        """
        def _execute_query(asset_ids, segment_codes, days_back):
            try:
                if not asset_ids or not segment_codes:
                    return pd.DataFrame()

                conn = self._get_connection()

                asset_placeholders = ','.join('?' * len(asset_ids))
                segment_placeholders = ','.join('?' * len(segment_codes))

                query = f"""
                    SELECT date, asset_id, segment_code, symbol,
                           open_price, close_price
                    FROM (
                        SELECT
                            date,
                            asset_id,
                            segment_code,
                            symbol,
                            open_price,
                            close_price,
                            ROW_NUMBER() OVER (
                                PARTITION BY asset_id, segment_code
                                ORDER BY date DESC
                            ) AS rn
                        FROM daily_ohlc
                        WHERE asset_id IN ({asset_placeholders})
                          AND segment_code IN ({segment_placeholders})
                    )
                    WHERE rn <= ?
                    ORDER BY asset_id, segment_code, date ASC
                """

                params_list = []
                params_list.extend(list(asset_ids) if not isinstance(asset_ids, list) else asset_ids)
                params_list.extend(list(segment_codes) if not isinstance(segment_codes, list) else segment_codes)
                params_list.append(int(days_back) + 1)  # +1 for prev_close of earliest tier-window day
                df = pd.read_sql_query(query, conn, params=params_list)

                if df.empty:
                    return df

                # Per-asset shift to get prev_close, then compute gap %.
                df['prev_close'] = df.groupby(
                    ['asset_id', 'segment_code']
                )['close_price'].shift(1)
                df['gap'] = ((df['open_price'] - df['prev_close']) / df['prev_close']) * 100

                # Drop the earliest row per asset (NaN prev_close → NaN gap).
                df = df.dropna(subset=['gap'])

                # Sort date-DESC (most-recent first) per asset — matches the
                # ordering convention used by get_historical_intraday_metrics
                # so downstream slicing is consistent across both readers.
                df = df.sort_values(
                    ['asset_id', 'segment_code', 'date'],
                    ascending=[True, True, False],
                ).reset_index(drop=True)

                return df[['date', 'asset_id', 'segment_code', 'symbol', 'gap']]

            except Exception as e:
                logger.error(f"Error getting historical gap data: {e}")
                return pd.DataFrame()

        return self._with_cache("get_historical_gap_data", _execute_query,
                                asset_ids, segment_codes, days_back)

    def get_historical_time_block_volumes(self, asset_ids: List[int],
                                         segment_codes: List[str],
                                         time_blocks: List[int],
                                         days_back: int) -> pd.DataFrame:
        """
        Get historical time block volumes for Z-score analysis.

        Args:
            asset_ids: List of asset identifiers
            segment_codes: List of segment codes corresponding to asset_ids
            time_blocks: List of 15-min time blocks (1-25)
            days_back: Number of trading days to retrieve

        Returns:
            DataFrame with columns: date, asset_id, segment_code, symbol,
            time_block, cumulative_volume — most recent first within each
            (asset, segment, block)
        """
        def _execute_query(asset_ids, segment_codes, time_blocks, days_back):
            try:
                if not asset_ids or not time_blocks or not segment_codes:
                    return pd.DataFrame()

                conn = self._get_connection()

                # Per-asset row cap via ROW_NUMBER() window. Each (asset_id,
                # segment_code, time_block) partition returns its `days_back`
                # most-recent rows independently — no reliance on uniform
                # per-day row count across the fleet, unlike a flat LIMIT.
                asset_placeholders = ','.join('?' * len(asset_ids))
                segment_placeholders = ','.join('?' * len(segment_codes))
                block_placeholders = ','.join('?' * len(time_blocks))

                # The ORDER BY inside ROW_NUMBER() only assigns `rn` — it does
                # NOT order the result set. The outer ORDER BY is what actually
                # guarantees each partition comes back MOST-RECENT-FIRST, which
                # every caller relies on when it slices `history[:n]` to build a
                # tier (see the ordering contract in calculators/statistics.py).
                # Do not remove it as redundant: without it the row order is
                # unspecified, and a tier would silently normalise against an
                # arbitrary N samples rather than the newest N.
                query = f"""
                    SELECT date, asset_id, segment_code, symbol, time_block, cumulative_volume
                    FROM (
                        SELECT
                            date,
                            asset_id,
                            segment_code,
                            symbol,
                            time_block,
                            cumulative_volume,
                            ROW_NUMBER() OVER (
                                PARTITION BY asset_id, segment_code, time_block
                                ORDER BY date DESC
                            ) AS rn
                        FROM time_block_volumes
                        WHERE asset_id IN ({asset_placeholders})
                          AND segment_code IN ({segment_placeholders})
                          AND time_block IN ({block_placeholders})
                    )
                    WHERE rn <= ?
                    ORDER BY asset_id, segment_code, time_block, date DESC
                """

                params_list = []
                params_list.extend(list(asset_ids) if not isinstance(asset_ids, list) else asset_ids)
                params_list.extend(list(segment_codes) if not isinstance(segment_codes, list) else segment_codes)
                params_list.extend(list(time_blocks) if not isinstance(time_blocks, list) else time_blocks)
                params_list.append(int(days_back))
                df = pd.read_sql_query(query, conn, params=params_list)

                return df

            except Exception as e:
                logger.error(f"Error getting historical time block volumes: {e}")
                return pd.DataFrame()

        # Block-aligned TTL: this table only gains rows at 15-min boundaries,
        # so a flat 60 s TTL expires on a schedule unrelated to when the data
        # changes — caching at 10:07 meant the 10:15 block arrived minutes late.
        return self._with_cache("get_historical_time_block_volumes", _execute_query,
                                asset_ids, segment_codes, time_blocks, days_back,
                                _align_to_block=True)

    def get_historical_intraday_metrics(self,
                                        asset_ids: List[int],
                                        segment_codes: List[str],
                                        time_blocks: List[int],
                                        days_back: int,
                                        columns: List[str],
                                        exclude_date: Optional[str] = None) -> pd.DataFrame:
        """
        Get historical intraday time block metrics for z-score analysis.

        Bulk historical read with per-asset row cap via ROW_NUMBER() OVER PARTITION.
        Each (asset_id, segment_code, time_block) partition returns its `days_back`
        most-recent rows independently — no reliance on uniform per-day row count
        across the fleet. Same pattern as get_historical_time_block_volumes above.

        Feeds the Momentum, Relative Strength, VWAP, PV Divergence and Rolling
        Momentum adapters. Consumers pass statistics.FETCH_DAYS (504) as
        `days_back` and the specific metric column(s) they need; each tier then
        takes exactly its own N.

        Args:
            asset_ids: List of asset identifiers.
            segment_codes: List of segment codes corresponding to asset_ids.
            time_blocks: List of time blocks (1..25 under the 15-min grid).
            days_back: Trading days to retrieve per (asset, block) partition.
            columns: Subset of metric columns to fetch. Valid values:
                     'session_price_move', 'vwap_displacement', 'pvd_raw'. Any other names are filtered out. Empty → empty DataFrame.
            exclude_date: ISO date to EXCLUDE from the history (normally today).
                     This table is written live just after each block ends, so
                     today's own observation is already present by the time a
                     calculator z-scores against it. Including a point in its own
                     comparison window pulls mu toward it and inflates sigma,
                     capping the achievable |z| at (n-1)/sqrt(n) — the reading is
                     compressed exactly when it is most extreme. Passed by VWAP,
                     Rolling Momentum and PV Divergence. NOT passed by Momentum
                     and Relative Strength, which read block 1's history and drop
                     today's row themselves by joining it with daily_ohlc (no
                     row for today until 15:34).

        Returns:
            DataFrame with columns: date, asset_id, segment_code, symbol, time_block,
            + requested metric columns. Empty DataFrame if inputs are empty or the
            query fails.
        """
        # Whitelist for SQL safety — never interpolate caller-provided names into SQL.
        _VALID_METRIC_COLUMNS = ('session_price_move', 'vwap_displacement', 'pvd_raw')
        columns_tuple = tuple(sorted(c for c in (columns or []) if c in _VALID_METRIC_COLUMNS))

        def _execute_query(asset_ids, segment_codes, time_blocks, days_back, columns_tuple,
                           exclude_date):
            try:
                if not asset_ids or not time_blocks or not segment_codes or not columns_tuple:
                    return pd.DataFrame()

                conn = self._get_connection()

                asset_placeholders = ','.join('?' * len(asset_ids))
                segment_placeholders = ','.join('?' * len(segment_codes))
                block_placeholders = ','.join('?' * len(time_blocks))
                metric_cols_sql = ', '.join(columns_tuple)  # safe: whitelisted above

                # IN-SAMPLE CONTAMINATION FILTER.
                #
                # This table is written LIVE at each 15-min block boundary, so
                # today's own observation is already in it by the time a
                # calculator z-scores against it. A point included in its own
                # comparison window pulls mu toward itself and inflates sigma,
                # which caps the achievable |z| at (n-1)/sqrt(n) — the reading is
                # compressed exactly when it is most extreme.
                #
                # It MUST sit inside the subquery, not outside: applied after
                # ROW_NUMBER(), the ranks would already have been assigned
                # including today's row, so `rn <= days_back` would silently
                # return one fewer historical sample than asked for.
                exclude_sql = "\n                          AND date <> ?" if exclude_date else ""

                # The ORDER BY inside ROW_NUMBER() only assigns `rn` — it does
                # NOT order the result set. The outer ORDER BY is what actually
                # guarantees each partition comes back MOST-RECENT-FIRST, which
                # every caller relies on when it slices `history[:n]` to build a
                # tier (see the ordering contract in calculators/statistics.py).
                # Do not remove it as redundant: without it the row order is
                # unspecified, and a tier would silently normalise against an
                # arbitrary N samples rather than the newest N.
                query = f"""
                    SELECT date, asset_id, segment_code, symbol, time_block, {metric_cols_sql}
                    FROM (
                        SELECT
                            date,
                            asset_id,
                            segment_code,
                            symbol,
                            time_block,
                            {metric_cols_sql},
                            ROW_NUMBER() OVER (
                                PARTITION BY asset_id, segment_code, time_block
                                ORDER BY date DESC
                            ) AS rn
                        FROM intraday_time_block_metrics
                        WHERE asset_id IN ({asset_placeholders})
                          AND segment_code IN ({segment_placeholders})
                          AND time_block IN ({block_placeholders}){exclude_sql}
                    )
                    WHERE rn <= ?
                    ORDER BY asset_id, segment_code, time_block, date DESC
                """

                # ORDER IS LOAD-BEARING. `exclude_date`'s placeholder sits inside
                # the subquery's WHERE, so it binds BEFORE the outer `rn <= ?`.
                # Appending it after days_back would swap the two values and
                # filter on the wrong one, with no error.
                params_list = []
                params_list.extend(list(asset_ids) if not isinstance(asset_ids, list) else asset_ids)
                params_list.extend(list(segment_codes) if not isinstance(segment_codes, list) else segment_codes)
                params_list.extend(list(time_blocks) if not isinstance(time_blocks, list) else time_blocks)
                if exclude_date:
                    params_list.append(exclude_date)
                params_list.append(int(days_back))
                df = pd.read_sql_query(query, conn, params=params_list)

                return df

            except Exception as e:
                logger.error(f"Error getting historical intraday metrics: {e}")
                return pd.DataFrame()

        # exclude_date participates in the cache key — otherwise a cached
        # contaminated result would be served to a caller that asked for the
        # filtered one, and vice versa.
        # Block-aligned TTL — same reasoning as get_historical_time_block_volumes:
        # the shared store is written at block boundaries by
        # _maybe_write_intraday_block, not continuously.
        return self._with_cache("get_historical_intraday_metrics", _execute_query,
                                asset_ids, segment_codes, time_blocks, days_back, columns_tuple,
                                exclude_date,
                                _align_to_block=True)

    def get_close_on(self, asset_id: int, segment_code: str, day: date) -> Optional[float]:
        """The official close stored for `day`, or None — no row for that day,
        or its close is not filled yet (it arrives with the next trading
        morning's first reading).

        For the price chart's PrevClose line, which needs the close of one
        specific day: the trading day before the session it shows. The newest
        row (get_previous_close_data) is wrong for a previous session, and
        after 15:34 it is the shown day's own row, archived without a close.
        """
        try:
            row = self._get_connection().execute(
                "SELECT close_price FROM daily_ohlc "
                "WHERE date = ? AND asset_id = ? AND segment_code = ?",
                (day.isoformat(), asset_id, segment_code)).fetchone()
        except Exception as e:
            logger.error(f"Error reading the {day} close for {asset_id}/{segment_code}: {e}")
            return None
        if row is None or row[0] is None:
            return None
        return float(row[0])

    def get_atr(self, asset_id: int, segment_code: str, before: date,
                period: int = 14) -> Optional[float]:
        """
        Average True Range over the `period` archived trading days BEFORE
        `before` — the ATR in force during that day's session.

        `before` is the session the price chart shows. After 15:34 that day is
        already archived; including it would widen the band by the very day
        being displayed, a range the session did not have while it traded.

        Requires period+1 days of daily OHLC (one extra for prev_close True Range calculation).
        Returns None if insufficient data is available.

        Args:
            asset_id: Asset identifier
            segment_code: Segment code (e.g. 'NSE_EQ', 'IDX_I')
            before: Only days strictly before this date count.
            period: Number of days to average True Range over (default 14)

        Returns:
            ATR value in price units, or None if insufficient data
        """
        try:
            conn = self._get_connection()
            df = pd.read_sql_query("""
                SELECT date, open_price, high_price, low_price, close_price
                FROM daily_ohlc
                WHERE asset_id = ? AND segment_code = ? AND date < ?
                ORDER BY date DESC
                LIMIT ?
            """, conn, params=(asset_id, segment_code, before.isoformat(), period + 5))

            if df.empty or len(df) < period + 1:
                return None

            df = df.sort_values('date').reset_index(drop=True)
            df['prev_close'] = df['close_price'].shift(1)
            df = df.dropna(subset=['prev_close'])

            if len(df) < period:
                return None

            df['tr'] = df.apply(lambda r: max(
                r['high_price'] - r['low_price'],
                abs(r['high_price'] - r['prev_close']),
                abs(r['low_price'] - r['prev_close'])
            ), axis=1)

            atr = df['tr'].tail(period).mean()
            return round(float(atr), 4) if not pd.isna(atr) else None

        except Exception as e:
            logger.error(f"Error computing ATR for asset {asset_id}/{segment_code}: {e}")
            return None

    def get_beta_cache(self, asset_ids: List[int], segment_codes: List[str]) -> pd.DataFrame:
        """
        Read precomputed beta rows for the given (asset_id, segment_code) pairs,
        across all benchmarks. Populated daily by scripts/precompute_beta.py.

        Returns:
            DataFrame with columns: asset_id, segment_code, benchmark_name,
            beta_22d, beta_90d, beta_180d, beta_252d, computed_at.
            Empty DataFrame if inputs are empty or the query fails.
        """
        def _execute_query(asset_ids, segment_codes):
            try:
                if not asset_ids or not segment_codes:
                    return pd.DataFrame()
                conn = self._get_connection()
                asset_placeholders = ','.join('?' * len(asset_ids))
                segment_placeholders = ','.join('?' * len(segment_codes))
                query = f"""
                    SELECT asset_id, segment_code, benchmark_name,
                           beta_22d, beta_90d, beta_180d, beta_252d, computed_at
                    FROM beta_cache
                    WHERE asset_id IN ({asset_placeholders})
                      AND segment_code IN ({segment_placeholders})
                """
                params = list(asset_ids) + list(segment_codes)
                return pd.read_sql_query(query, conn, params=params)
            except Exception as e:
                logger.error(f"Error getting beta cache: {e}")
                return pd.DataFrame()

        return self._with_cache("get_beta_cache", _execute_query, asset_ids, segment_codes)

    def get_daily_price_history(self, asset_ids: List[int], segment_codes: List[str],
                                 days_back: int) -> pd.DataFrame:
        """
        Daily open/close history for the given assets — the raw ingredient for
        session/intraday return reconstruction (Momentum's gap history already
        has its own dedicated method; this one is generic, used by Relative
        Strength for both the stock side and the benchmark side).

        Returns:
            DataFrame with columns: date, asset_id, segment_code, symbol,
            open_price, close_price. Most-recent `days_back` rows per
            (asset_id, segment_code), via the same ROW_NUMBER OVER PARTITION
            pattern used elsewhere in this file.
        """
        def _execute_query(asset_ids, segment_codes, days_back):
            try:
                if not asset_ids or not segment_codes:
                    return pd.DataFrame()
                conn = self._get_connection()
                asset_placeholders = ','.join('?' * len(asset_ids))
                segment_placeholders = ','.join('?' * len(segment_codes))
                query = f"""
                    SELECT date, asset_id, segment_code, symbol, open_price, close_price
                    FROM (
                        SELECT date, asset_id, segment_code, symbol, open_price, close_price,
                               ROW_NUMBER() OVER (
                                   PARTITION BY asset_id, segment_code
                                   ORDER BY date DESC
                               ) AS rn
                        FROM daily_ohlc
                        WHERE asset_id IN ({asset_placeholders})
                          AND segment_code IN ({segment_placeholders})
                    )
                    WHERE rn <= ?
                    ORDER BY asset_id, segment_code, date DESC
                """
                params = list(asset_ids) + list(segment_codes) + [int(days_back)]
                return pd.read_sql_query(query, conn, params=params)
            except Exception as e:
                logger.error(f"Error getting daily price history: {e}")
                return pd.DataFrame()

        return self._with_cache("get_daily_price_history", _execute_query,
                                asset_ids, segment_codes, days_back)


# Convenience factory functions
def create_writer(db_path: str = "data/historical.db") -> HistoricalDatabaseWriter:
    """Factory function to create database writer."""
    return HistoricalDatabaseWriter(db_path)


def create_reader(db_path: str = "data/historical.db") -> HistoricalDatabaseReader:
    """Factory function to create database reader."""
    return HistoricalDatabaseReader(db_path)
