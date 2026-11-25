"""
OHLC Database Manager

Provides secure, read-only access to the OHLC database for price modules.
Implements WAL mode, proper timeouts, and query optimization.

TIMEZONE CONVENTION — every timestamp here is IST-AWARE.

`ohlc.db.ohlc_data.timestamp` is written as an ISO-8601 string carrying an
explicit `+05:30` offset, produced by `datetime.now(IST)` where `IST` is the
single canonical constant in `calculators/time_blocks.py`. Query cutoffs are
built the same way, so both sides of every comparison agree. Do NOT reintroduce
a bare `datetime.now()` here — it inherits the `TZ` environment variable, which
is an environment fact rather than a code fact, and a container without
`TZ=Asia/Kolkata` would silently write UTC and shift every timestamp by 5h30m.

TWO CONSEQUENCES WORTH KNOWING:

1. NEVER use SQLite's DATE()/TIME()/strftime() on a timestamp column. Those
   functions parse their argument and convert an offset-bearing value to UTC:
   strftime('%H:%M', '2026-08-31T09:15:00+05:30') returns '03:45', not '09:15'.
   Use a lexical slice instead — substr(timestamp, 1, 10) for the date,
   substr(timestamp, 12, 8) for the clock time. Characters 1-19 of an ISO
   timestamp are identical with or without an offset, so substr reads IST
   wall-clock regardless of format.

2. Cutoff comparisons are lexicographic on TEXT, and that is fine because the
   date/time prefix dominates. A whole-second cutoff ('...T09:15:00+05:30')
   sorts before a same-second row carrying microseconds
   ('...T09:15:00.123456+05:30') because '+' (0x2B) precedes '.' (0x2E) — so
   the opening tick is included, as intended.
"""

import bisect
import sqlite3
import numpy as np
import pandas as pd
import logging
import os
from array import array
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from calculators.time_blocks import IST, SESSION_START

logger = logging.getLogger(__name__)

# Held reading times are integer microseconds since this instant: exact, and
# independent of pandas' default datetime unit (which changed in pandas 3).
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MICROSECOND = timedelta(microseconds=1)

class OHLCDatabaseReader:
    """
    Secure, read-only database access for price modules.

    Features:
    - Read-only mode for safety
    - WAL mode for concurrent access
    - Proper timeouts and error handling
    """

    def __init__(self, db_path: str = None):
        """
        Initialize database reader.

        Args:
            db_path: Path to SQLite database file
        """
        if db_path is None:
            # Default to same path as OHLC engine
            data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
            db_path = os.path.join(data_dir, "ohlc.db")

        self.db_path = db_path
        self.connection_params = {
            'timeout': 30.0,
            'check_same_thread': False,
            'isolation_level': None  # autocommit mode
        }

        # Verify database exists
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(f"OHLC database not found at {self.db_path}")

        # Today's readings, held for get_session_prices (see there).
        self._session_day = None          # the date the store holds
        self._session_newest = None       # newest timestamp held, exactly as stored
        self._session_read_until = None   # the `now` the last read reached
        self._session = {}                # (asset_id, segment_code) -> (times, prices)
        # Each asset's first reading of the day with a running total, for
        # session_first_reading (see there).
        self._first_readings_day = None
        self._first_readings = {}         # (asset_id, segment_code) -> stamp, as stored

        logger.info(f"OHLC Database Reader initialized with path: {self.db_path}")

    @contextmanager
    def get_read_connection(self):
        """
        Get read-only database connection with proper configuration.

        Uses WAL mode for concurrent access without blocking the OHLC engine.
        """
        conn = None
        try:
            # Open in read-only mode for extra safety
            conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro",
                uri=True,
                **self.connection_params
            )

            # Configure for optimal read performance
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA query_only = 1")  # Extra safety - read-only
            conn.execute("PRAGMA cache_size = -64000")  # 64MB cache
            conn.execute("PRAGMA temp_store = MEMORY")

            # Enable row factory for better data access
            conn.row_factory = sqlite3.Row

            yield conn

        except sqlite3.Error as e:
            logger.error(f"Database error: {e}")
            raise
        finally:
            if conn:
                conn.close()

    def get_volume_comparison_data(self, asset_ids: List[int], segment_codes: List[str],
                                   session_date: str, time_block: int = None) -> pd.DataFrame:
        """
        Block volumes written for `session_date`.

        Args:
            asset_ids: Asset IDs to return.
            segment_codes: Segment codes, positionally paired with asset_ids —
                required, because some asset_ids are shared across segments
                (25 is BankNifty in IDX_I and ADANIENT in NSE_EQ).
            session_date: 'YYYY-MM-DD' — the day whose blocks to return; callers
                pass today. This used to select the newest date holding any
                rows, which on a morning before the engine had written a block
                was YESTERDAY — and yesterday's block N was shown as today's.
            time_block: One block, or None for every block written so far.

        Returns:
            DataFrame of that day's block volumes, including segment_code so
            callers can disambiguate colliding asset_ids. Empty when none are
            written yet, which is the correct answer at that point.

        Not cached. The table gains rows at each block boundary, and a result
        cached just before a boundary was reused for the first cycle after it —
        so the stored row for the new block was its partial total. The query
        runs once per metrics cycle, so a cache saved nothing.
        """
        try:
            with self.get_read_connection() as conn:
                pairs = list(zip(asset_ids, segment_codes))
                placeholders = ','.join(['(?,?)'] * len(pairs))
                query = f"""
                    SELECT
                        v.date,
                        v.asset_id,
                        v.symbol,
                        v.segment_code,
                        v.time_block,
                        v.cumulative_volume,
                        v.timestamp
                    FROM time_block_volumes v
                    WHERE v.date = ?
                      AND (v.asset_id, v.segment_code) IN ({placeholders})
                """
                params = [session_date] + [v for pair in pairs for v in pair]

                if time_block is not None:
                    query += " AND v.time_block = ?"
                    params.append(time_block)

                query += " ORDER BY v.asset_id, v.time_block"

                return pd.read_sql_query(query, conn, params=params)

        except Exception as e:
            logger.error(f"Error getting volume comparison data: {e}")
            return pd.DataFrame()

    def get_intraday_data_range(self, asset_id: int, start_time, end_time, segment_code: str) -> pd.DataFrame:
        """
        Get intraday data for a specific time range (for reference period analysis).

        Args:
            asset_id: Asset ID
            start_time: Start datetime
            end_time: End datetime
            segment_code: Segment code — required. Some asset_ids are shared across
                segments (e.g. 25 is BankNifty in IDX_I and ADANIENT in NSE_EQ);
                filtering on asset_id alone can return the wrong entity's data.

        Returns:
            DataFrame with OHLC data in the specified time range
        """
        try:
            with self.get_read_connection() as conn:
                query = """
                SELECT
                    timestamp, asset_id, symbol, segment_code,
                    last_price, open_price, high_price, low_price, close_price,
                    volume, interval_volume, average_price
                FROM ohlc_data
                WHERE asset_id = ? AND segment_code = ?
                AND timestamp >= ?
                AND timestamp < ?
                ORDER BY timestamp ASC
                """
                params = [asset_id, segment_code, start_time.isoformat(), end_time.isoformat()]

                df = pd.read_sql_query(query, conn, params=params)
                logger.debug(f"Fetched {len(df)} records for asset {asset_id}/{segment_code} from {start_time} to {end_time}")
                return df

        except Exception as e:
            logger.error(f"Error fetching intraday data range for asset {asset_id}/{segment_code}: {e}")
            return pd.DataFrame()

    def get_session_prices(self, asset_id: int, segment_code: str,
                           now: datetime) -> pd.DataFrame:
        """
        Today's readings for one asset — `timestamp` (IST-aware) and
        `last_price`, oldest first — from the session start (09:15) up to, NOT
        including, `now`. `now` must be IST-aware.

        Breakout and Price Levels need the whole day's prices every cycle. They
        used to read them from disk for every asset, every cycle — up to
        ~770,000 rows a cycle by the close (86 assets x ~4,500 readings, read
        by both), all but the last few seconds' worth already read the cycle
        before. The readings are now held here, and each new `now`
        reads only the rows newer than the newest one held, for all assets in
        one query.

        WHY READING ONLY NEWER ROWS CANNOT MISS ONE. The engine stamps every
        asset in a fetch with ONE timestamp, writes the whole fetch in ONE
        transaction, from ONE thread, one fetch after another. Nothing ever
        edits a stored reading, and the nightly prune deletes only days before
        today. So a fetch is seen whole or not at all, and a fetch not yet
        written when a cycle reads is newer than everything held — the next
        read picks it up.

          - Restart, late start, mid-day start: the store starts empty, so the
            first read takes the whole day so far (one query), then only new
            rows. Nothing is lost; every reading is on disk.
          - A new day: the store holds one date; the first read on another date
            empties it.
          - The same `now` twice (Breakout then Price Levels, one cycle): the
            second call reads nothing from disk.

        Held as integer microseconds and floats, ~16 bytes a reading (~6 MB at
        the close for 86 assets). For the metrics thread only — not thread-safe.
        """
        self._read_new_session_rows(now)
        held = self._session.get((asset_id, segment_code))
        if held is None:
            return pd.DataFrame(columns=['timestamp', 'last_price'])
        times, prices = held
        cut = bisect.bisect_left(times, (now - _EPOCH) // _MICROSECOND)
        return pd.DataFrame({
            'timestamp': pd.to_datetime(np.array(times[:cut], dtype='int64'),
                                        unit='us', utc=True).tz_convert(IST),
            'last_price': np.array(prices[:cut], dtype='float64'),
        })

    def _read_new_session_rows(self, now: datetime) -> None:
        """Add to the store every reading of `now`'s day newer than the newest
        held and older than `now` — one query, all assets."""
        day = now.date()
        if day != self._session_day:
            self._session_day = day
            self._session_newest = None
            self._session_read_until = None
            self._session = {}
        now_iso = now.isoformat()
        if self._session_read_until is not None and now_iso <= self._session_read_until:
            return  # already read up to this `now`
        if self._session_newest is None:
            lower, bound = "timestamp >= ?", datetime.combine(day, SESSION_START, tzinfo=IST).isoformat()
        else:
            lower, bound = "timestamp > ?", self._session_newest
        # Text comparisons, as everywhere on this column: every stored stamp
        # carries the same +05:30 offset, so text order is time order (see the
        # module docstring on '+' sorting before '.').
        with self.get_read_connection() as conn:
            rows = conn.execute(f"""
                SELECT timestamp, asset_id, segment_code, last_price
                FROM ohlc_data
                WHERE {lower} AND timestamp < ?
                ORDER BY timestamp
            """, (bound, now_iso)).fetchall()
        self._session_read_until = now_iso
        for stamp, asset_id, segment_code, price in rows:
            held = self._session.get((asset_id, segment_code))
            if held is None:
                held = self._session[(asset_id, segment_code)] = (array('q'), array('d'))
            held[0].append((datetime.fromisoformat(stamp) - _EPOCH) // _MICROSECOND)
            held[1].append(float('nan') if price is None else float(price))
        if rows:
            self._session_newest = rows[-1][0]

    def session_first_reading(self, asset_id: int, segment_code: str, day) -> Optional[str]:
        """
        The timestamp, exactly as stored, of the asset's first reading on `day`
        that carries a running total (`volume`) — None if it has none yet.

        That reading's interval_volume is not a five-second amount: the engine
        has no earlier total that day to subtract, so it holds the whole running
        total so far (before 09:30) or 0 (a late start). PV Divergence leaves it
        out. Found once per asset per day, then remembered — it cannot change;
        "none yet" is not remembered, so it is asked again next time. For the
        metrics thread only — not thread-safe.
        """
        if day != self._first_readings_day:
            self._first_readings_day = day
            self._first_readings = {}
        key = (asset_id, segment_code)
        if key not in self._first_readings:
            # Text bounds on the stamp, so the (asset_id, timestamp) index serves
            # it: every stamp of `day` sorts between 'YYYY-MM-DD' and the next day.
            with self.get_read_connection() as conn:
                row = conn.execute("""
                    SELECT timestamp FROM ohlc_data
                    WHERE asset_id = ? AND segment_code = ? AND volume IS NOT NULL
                      AND timestamp >= ? AND timestamp < ?
                    ORDER BY timestamp LIMIT 1
                """, (asset_id, segment_code, day.isoformat(),
                      (day + timedelta(days=1)).isoformat())).fetchone()
            if row is None:
                return None
            self._first_readings[key] = row[0]
        return self._first_readings[key]

    def get_first_reading_closes(self, day) -> pd.DataFrame:
        """
        Each asset's `close_price` from its FIRST reading on `day` that has one.

        During the session the quote's `close` is the exchange's PREVIOUS-day
        official close (TCS on 2026-09-07: ₹2,304.00, Friday's close, on every
        reading). The FIRST reading is used because Sensex's value starts moving
        from ~15:15 under the closing auction, and after settlement the field
        becomes the day's own close.

        Returns DataFrame[asset_id, segment_code, symbol, close_price]; empty if
        the day has no readings yet.
        """
        query = """
            SELECT o.asset_id, o.segment_code, o.symbol, o.close_price
            FROM ohlc_data o
            JOIN (
                SELECT asset_id, segment_code, MIN(timestamp) AS first_ts
                FROM ohlc_data
                WHERE substr(timestamp, 1, 10) = ? AND close_price IS NOT NULL
                GROUP BY asset_id, segment_code
            ) f ON o.asset_id = f.asset_id AND o.segment_code = f.segment_code
                AND o.timestamp = f.first_ts
        """
        with self.get_read_connection() as conn:
            return pd.read_sql_query(query, conn, params=[day.isoformat()])

    def get_first_reading_closes_for(self, day, assets) -> pd.DataFrame:
        """
        `get_first_reading_closes` for the given (asset_id, segment_code) pairs
        only — the morning step's retry for stocks missing from the day's first
        fetch. One indexed query per pair (text bounds on the stamp, as in
        session_first_reading), so it costs milliseconds per cycle rather than
        a scan of every stored reading. Pairs with no reading carrying a close
        yet are simply absent from the result.
        """
        rows = []
        with self.get_read_connection() as conn:
            for asset_id, segment_code in assets:
                row = conn.execute("""
                    SELECT asset_id, segment_code, symbol, close_price FROM ohlc_data
                    WHERE asset_id = ? AND segment_code = ? AND close_price IS NOT NULL
                      AND timestamp >= ? AND timestamp < ?
                    ORDER BY timestamp LIMIT 1
                """, (asset_id, segment_code, day.isoformat(),
                      (day + timedelta(days=1)).isoformat())).fetchone()
                if row is not None:
                    rows.append(tuple(row))
        return pd.DataFrame(rows, columns=['asset_id', 'segment_code', 'symbol', 'close_price'])

