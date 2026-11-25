"""
Metrics Database Manager

Provides secure database access for metrics storage and retrieval.
Stores raw metrics from all 9 per-asset calculators and the index aggregator.
"""

import sqlite3
import pandas as pd
import json
import logging
import os
import time
import threading
import hashlib
from contextlib import contextmanager
from typing import List, Dict, Optional, Any, Tuple
from datetime import datetime, timedelta

from .statistics import TIERS
from .time_blocks import IST, clamp_to_day_start

logger = logging.getLogger(__name__)


def _json_compact(obj) -> str:
    """json.dumps with no padding whitespace, for the JSON TEXT columns.

    Default separators are ', ' and ': ' — two bytes where one does, on every
    separator of every JSON column on every write. Measured on this codebase's
    real payloads: -12% on constituent_z, -11% on levels, -8% on
    top_contributors.

    Whitespace-only: the emitted JSON is semantically identical and every
    consumer reads these columns through json.loads, so nothing downstream can
    observe the difference. Use this for ALL JSON columns — a new column added
    with a bare json.dumps would silently revert to the padded form.
    """
    return json.dumps(obj, separators=(',', ':'))


class MetricsDatabaseManager:
    """
    Database manager for metrics storage and retrieval.

    Features:
    - Stores raw metrics from the 9 per-asset calculators and the index aggregator
    - Efficient querying with indexes
    - Same architecture as OHLCDatabaseReader

    Every `store_*_metrics` method logs a failure and RE-RAISES it. The caller
    in metrics.py stamps CALCULATOR_HEALTH only after the store returns, so a
    failed write must not look like a successful one. Returning a count instead
    would not do: rows are counted before `commit()`, and a failed commit rolls
    all of them back.
    """

    def __init__(self, db_path: str = None):
        """
        Initialize metrics database manager.

        Args:
            db_path: Path to SQLite database file
        """
        if db_path is None:
            # Default to metrics database path
            data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
            db_path = os.path.join(data_dir, "metrics.db")

        self.db_path = db_path
        self.connection_params = {
            'timeout': 30.0,
            'check_same_thread': False,
            # '' = DEFERRED (the Python DB-API default), NOT None.
            #
            # None is autocommit: SQLite commits every individual statement and
            # conn.commit() becomes a no-op. Every store_* function in this file
            # already ends with conn.commit() — i.e. they were all WRITTEN to
            # batch — and autocommit silently defeated all twelve of them. The
            # real cost: store_price_levels_metrics was 86 transactions rather
            # than 1, and a full metrics cycle committed ~600 times per 30 sec.
            # By comparison ohlc.db commits 0.2 times/sec (engine.py executemany,
            # one commit per 5-sec fetch) and historical.db ~0.001 times/sec —
            # and those two are the databases that have never corrupted, despite
            # ohlc.db writing far more ROWS.
            #
            # With '' Python opens a transaction before the first INSERT and the
            # existing commit() closes it, so each store function is one
            # transaction: ~10 commits per cycle instead of ~600.
            'isolation_level': ''
        }

        # Ensure data directory exists
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        # Initialize database schema
        self._initialize_database()
        self._initialize_metrics_tables()

        logger.info(f"Metrics Database Manager initialized with path: {self.db_path}")

    def _initialize_database(self):
        """Initialize metrics database schema"""
        try:
            with sqlite3.connect(self.db_path, **self.connection_params) as conn:
                # Enable WAL mode for better concurrent access
                conn.execute("PRAGMA journal_mode=WAL")

                conn.commit()
                logger.info("Metrics database schema initialized successfully")

        except Exception as e:
            logger.error(f"Error initializing metrics database: {e}")
            raise

    def _initialize_metrics_tables(self):
        """Initialize metrics tables for storing all calculator results.

        Rewritten for the locked-calculator generation. No ALTER TABLE, no
        migration blocks — schema changes ship as an off-disk delete of
        data/metrics.db* followed by clean recreation from these CREATE
        TABLE statements on next startup.

        `created_at` CARRIES NO DEFAULT, DELIBERATELY. It used to be
        `DEFAULT CURRENT_TIMESTAMP`, which SQLite evaluates as UTC in
        'YYYY-MM-DD HH:MM:SS' form — space-separated, no offset. Every cutoff
        in this codebase is 'T'-separated IST, and ' ' (0x20) sorts before
        'T' (0x54) as TEXT, so a defaulted row would sort before every
        same-day cutoff and stay invisible to `WHERE created_at >= ?`
        permanently. The default was unreachable (every INSERT names the
        column) but armed: the first INSERT to omit it would have written a
        plausible value no query could ever match. Without the default such an
        omission writes NULL, which is loud. Every writer passes
        `datetime.now(IST).isoformat()`.
        """
        try:
            with sqlite3.connect(self.db_path, **self.connection_params) as conn:
                # Breakout Metrics Table (BREAKOUT_LOCKED_V1 — no z-scoring;
                # breakout_direction / fresh_breakout / accepted_breakout /
                # failed_breakout are signed ints: +1 up, -1 down, 0 none).
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS breakout_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        asset_id INTEGER NOT NULL,
                        segment_code TEXT NOT NULL,
                        symbol TEXT,
                        timestamp TEXT NOT NULL,
                        current_price REAL,
                        range_high REAL,
                        range_low REAL,
                        range_width REAL,
                        breakout_position REAL,
                        fresh_breakout INTEGER DEFAULT 0,
                        breakout_direction INTEGER DEFAULT 0,
                        breakout_now INTEGER DEFAULT 0,
                        accepted_breakout INTEGER DEFAULT 0,
                        failed_breakout INTEGER DEFAULT 0,
                        distance_from_range REAL,
                        status TEXT,
                        created_at TEXT,
                        UNIQUE(asset_id, segment_code, timestamp)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_breakout_asset_id ON breakout_metrics(asset_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_breakout_timestamp ON breakout_metrics(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_breakout_created ON breakout_metrics(created_at DESC)")

                # Price Levels Metrics Table (PRICE_LEVELS_LOCKED_V1 — replaces the old
                # Price Alerts table). `levels` is a JSON dict keyed by level name
                # (local_high/local_low/global_high/global_low), each a LevelEventResult
                # dict. `day_events` is a JSON list of level names crossed at any point
                # today — re-derived fresh from ohlc.db ticks each cycle, not remembered
                # in process memory (Kind-A state).
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS price_levels_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        asset_id INTEGER NOT NULL,
                        segment_code TEXT NOT NULL,
                        symbol TEXT,
                        timestamp TEXT NOT NULL,
                        current_price REAL,
                        levels TEXT,
                        day_events TEXT,
                        status TEXT,
                        created_at TEXT,
                        UNIQUE(asset_id, segment_code, timestamp)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_price_levels_asset_id ON price_levels_metrics(asset_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_price_levels_created ON price_levels_metrics(created_at DESC)")

                # Momentum Metrics Table (MOMENTUM_LOCKED_V2). Fixed 09:15->09:30 window;
                # gap and early_return each carry all four z tiers; a tier is NULL
                # until it has exactly its own N samples of history.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS momentum_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        asset_id INTEGER NOT NULL,
                        segment_code TEXT NOT NULL,
                        symbol TEXT,
                        timestamp TEXT NOT NULL,
                        current_price REAL,
                        open_price REAL,
                        -- Session running high/low, sourced from
                        -- PriceDataManager.get_current_prices(). Not used by any
                        -- momentum computation; carried so the dashboard's Range
                        -- column survives a restart. Without these columns the
                        -- start-up cache hydration rebuilds LATEST_METRICS from this
                        -- table with both keys absent, and Range renders blank
                        -- until the next live cycle (indefinitely, off-hours).
                        high_price REAL,
                        low_price REAL,
                        previous_close REAL,
                        price_after_window REAL,
                        gap_return REAL,
                        gap_direction INTEGER,
                        early_return REAL,
                        continuation REAL,
                        acceleration REAL,
                        status TEXT,
                        gap_z_22d REAL, gap_avg_22d REAL, gap_std_22d REAL, gap_data_points_22d INTEGER,
                        gap_z_90d REAL, gap_avg_90d REAL, gap_std_90d REAL, gap_data_points_90d INTEGER,
                        gap_z_180d REAL, gap_avg_180d REAL, gap_std_180d REAL, gap_data_points_180d INTEGER,
                        gap_z_252d REAL, gap_avg_252d REAL, gap_std_252d REAL, gap_data_points_252d INTEGER,
                        early_return_z_22d REAL, early_return_avg_22d REAL, early_return_std_22d REAL, early_return_data_points_22d INTEGER,
                        early_return_z_90d REAL, early_return_avg_90d REAL, early_return_std_90d REAL, early_return_data_points_90d INTEGER,
                        early_return_z_180d REAL, early_return_avg_180d REAL, early_return_std_180d REAL, early_return_data_points_180d INTEGER,
                        early_return_z_252d REAL, early_return_avg_252d REAL, early_return_std_252d REAL, early_return_data_points_252d INTEGER,
                        created_at TEXT,
                        UNIQUE(asset_id, segment_code, timestamp)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_momentum_asset_id ON momentum_metrics(asset_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_momentum_created ON momentum_metrics(created_at DESC)")

                # VWAP Metrics Table (VWAP_LOCKED_V1). No bands / band_breach — dropped
                # from the locked design. vwap_z is stored at all four tiers.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS vwap_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        asset_id INTEGER NOT NULL,
                        segment_code TEXT NOT NULL,
                        symbol TEXT,
                        timestamp TEXT NOT NULL,
                        current_price REAL,
                        vwap REAL,
                        displacement REAL,
                        side INTEGER,
                        acceptance REAL,
                        status TEXT,
                        vwap_z_22d REAL, vwap_avg_22d REAL, vwap_std_22d REAL, vwap_data_points_22d INTEGER,
                        vwap_z_90d REAL, vwap_avg_90d REAL, vwap_std_90d REAL, vwap_data_points_90d INTEGER,
                        vwap_z_180d REAL, vwap_avg_180d REAL, vwap_std_180d REAL, vwap_data_points_180d INTEGER,
                        vwap_z_252d REAL, vwap_avg_252d REAL, vwap_std_252d REAL, vwap_data_points_252d INTEGER,
                        created_at TEXT,
                        UNIQUE(asset_id, segment_code, timestamp)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_vwap_asset_id ON vwap_metrics(asset_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_vwap_created ON vwap_metrics(created_at DESC)")

                # Relative Strength Metrics Table (RELATIVE_STRENGTH_LOCKED_V1).
                #
                # session_rs, intraday_rs and acceptance are stored PER TIER,
                # not once. beta is a parameter of the measurement itself
                #     session_rs = ln(stock_open/prev) - beta * ln(bench_open/prev)
                # so tier T's value is measured with beta_T and normalised
                # against T samples reconstructed with beta_T. There is no
                # single "the" session_rs to store. See the tier loop in
                # `run_relative_strength_analysis`, relative_strength_adapter.py.
                #
                # All four beta_* columns now carry real values from the daily
                # cache (scripts/precompute_beta.py has always computed all
                # four; the adapter used to read beta_90d and discard the rest).
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS relative_strength_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        asset_id INTEGER NOT NULL,
                        segment_code TEXT NOT NULL,
                        symbol TEXT,
                        timestamp TEXT NOT NULL,
                        benchmark_name TEXT,
                        benchmark_id INTEGER,
                        current_price REAL,
                        open_price REAL,
                        status TEXT,
                        beta_22d REAL, beta_90d REAL, beta_180d REAL, beta_252d REAL,
                        session_rs_22d REAL, session_rs_90d REAL, session_rs_180d REAL, session_rs_252d REAL,
                        intraday_rs_22d REAL, intraday_rs_90d REAL, intraday_rs_180d REAL, intraday_rs_252d REAL,
                        acceptance_22d REAL, acceptance_90d REAL, acceptance_180d REAL, acceptance_252d REAL,
                        session_rs_z_22d REAL, session_rs_avg_22d REAL, session_rs_std_22d REAL, session_rs_data_points_22d INTEGER,
                        session_rs_z_90d REAL, session_rs_avg_90d REAL, session_rs_std_90d REAL, session_rs_data_points_90d INTEGER,
                        session_rs_z_180d REAL, session_rs_avg_180d REAL, session_rs_std_180d REAL, session_rs_data_points_180d INTEGER,
                        session_rs_z_252d REAL, session_rs_avg_252d REAL, session_rs_std_252d REAL, session_rs_data_points_252d INTEGER,
                        intraday_rs_z_22d REAL, intraday_rs_avg_22d REAL, intraday_rs_std_22d REAL, intraday_rs_data_points_22d INTEGER,
                        intraday_rs_z_90d REAL, intraday_rs_avg_90d REAL, intraday_rs_std_90d REAL, intraday_rs_data_points_90d INTEGER,
                        intraday_rs_z_180d REAL, intraday_rs_avg_180d REAL, intraday_rs_std_180d REAL, intraday_rs_data_points_180d INTEGER,
                        intraday_rs_z_252d REAL, intraday_rs_avg_252d REAL, intraday_rs_std_252d REAL, intraday_rs_data_points_252d INTEGER,
                        created_at TEXT,
                        UNIQUE(asset_id, segment_code, benchmark_name, timestamp)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_rel_strength_asset_id ON relative_strength_metrics(asset_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_rel_strength_created ON relative_strength_metrics(created_at DESC)")

                # Volume Dynamics Metrics Table (VOLUME_DYNAMICS_LOCKED_V3). Derived
                # from Volume Block's own z-score (volume_z is a pass-through), at
                # each of the four tiers.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS volume_dynamics_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        asset_id INTEGER NOT NULL,
                        segment_code TEXT NOT NULL,
                        symbol TEXT,
                        timestamp TEXT NOT NULL,
                        time_block INTEGER,
                        status TEXT,
                        -- Volume Dynamics is a pure derivative of Volume Block's z,
                        -- so it tiers exactly as Volume Block does. These are NOT
                        -- z-scores themselves, so they carry no avg/std/data_points
                        -- companions — volume_z_* is a pass-through of Volume
                        -- Block's own z, whose stats live on volume_block_metrics.
                        volume_z_22d REAL, volume_z_90d REAL, volume_z_180d REAL, volume_z_252d REAL,
                        velocity_22d REAL, velocity_90d REAL, velocity_180d REAL, velocity_252d REAL,
                        acceleration_22d REAL, acceleration_90d REAL, acceleration_180d REAL, acceleration_252d REAL,
                        persistence_22d REAL, persistence_90d REAL, persistence_180d REAL, persistence_252d REAL,
                        -- No DEFAULT 0: 0 is a real state ("neutral"), so a
                        -- defaulted 0 would be indistinguishable from a tier
                        -- that had too little history to produce a ZQ at all.
                        expansion_state_22d INTEGER, expansion_state_90d INTEGER,
                        expansion_state_180d INTEGER, expansion_state_252d INTEGER,
                        created_at TEXT,
                        UNIQUE(asset_id, segment_code, timestamp)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_volume_dynamics_asset_id ON volume_dynamics_metrics(asset_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_volume_dynamics_created ON volume_dynamics_metrics(created_at DESC)")

                # Volume Block Metrics Table (TRADE_VOLUME_BLOCK_LOCKED_V4 — replaces
                # the old Time Block Volume table). block_volume_z is the primary
                # feature (same-block history); session_cumulative_volume_z is the
                # reconstructed-running-total context feature. Both stored at all
                # four tiers.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS volume_block_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        asset_id INTEGER NOT NULL,
                        segment_code TEXT NOT NULL,
                        symbol TEXT,
                        timestamp TEXT NOT NULL,
                        time_block INTEGER,
                        block_volume REAL,
                        session_cumulative_volume REAL,
                        session_cumulative_blocks INTEGER,
                        status TEXT,
                        block_volume_z_22d REAL, block_volume_avg_22d REAL, block_volume_std_22d REAL, block_volume_data_points_22d INTEGER,
                        block_volume_z_90d REAL, block_volume_avg_90d REAL, block_volume_std_90d REAL, block_volume_data_points_90d INTEGER,
                        block_volume_z_180d REAL, block_volume_avg_180d REAL, block_volume_std_180d REAL, block_volume_data_points_180d INTEGER,
                        block_volume_z_252d REAL, block_volume_avg_252d REAL, block_volume_std_252d REAL, block_volume_data_points_252d INTEGER,
                        session_cumulative_volume_z_22d REAL, session_cumulative_volume_avg_22d REAL, session_cumulative_volume_std_22d REAL, session_cumulative_volume_data_points_22d INTEGER,
                        session_cumulative_volume_z_90d REAL, session_cumulative_volume_avg_90d REAL, session_cumulative_volume_std_90d REAL, session_cumulative_volume_data_points_90d INTEGER,
                        session_cumulative_volume_z_180d REAL, session_cumulative_volume_avg_180d REAL, session_cumulative_volume_std_180d REAL, session_cumulative_volume_data_points_180d INTEGER,
                        session_cumulative_volume_z_252d REAL, session_cumulative_volume_avg_252d REAL, session_cumulative_volume_std_252d REAL, session_cumulative_volume_data_points_252d INTEGER,
                        created_at TEXT,
                        UNIQUE(asset_id, segment_code, timestamp)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_volume_block_asset_id ON volume_block_metrics(asset_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_volume_block_created ON volume_block_metrics(created_at DESC)")

                # Per-Index Aggregate Metrics Table — one row per index per cycle
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS index_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        index_name TEXT NOT NULL,

                        index_price REAL,
                        intraday_pct REAL,
                        total_constituents INTEGER,

                        advancers INTEGER,
                        decliners INTEGER,
                        unchanged INTEGER,
                        above_vwap INTEGER,
                        below_vwap INTEGER,
                        vwap_evaluated INTEGER,
                        in_breakout_up INTEGER,
                        in_breakout_down INTEGER,
                        breakout_evaluated INTEGER,
                        level_breached_today INTEGER,
                        -- RS breadth per tier: these count the SIGN of
                        -- session_rs, which is measured once per beta tier,
                        -- so the count differs by tier. Stored rather than
                        -- derived at display time because a sign count bakes
                        -- no threshold — its only axis is the tier, and there
                        -- are exactly four of those.
                        positive_rs_22d INTEGER, positive_rs_90d INTEGER,
                        positive_rs_180d INTEGER, positive_rs_252d INTEGER,
                        negative_rs_22d INTEGER, negative_rs_90d INTEGER,
                        negative_rs_180d INTEGER, negative_rs_252d INTEGER,
                        rs_evaluated_22d INTEGER, rs_evaluated_90d INTEGER,
                        rs_evaluated_180d INTEGER, rs_evaluated_252d INTEGER,

                        weighted_intraday_return REAL,
                        weighted_volume_velocity_22d REAL,
                        weighted_volume_velocity_90d REAL,
                        weighted_volume_velocity_180d REAL,
                        weighted_volume_velocity_252d REAL,
                        weighted_volume_zscore_22d REAL,
                        weighted_volume_zscore_90d REAL,
                        weighted_volume_zscore_180d REAL,
                        weighted_volume_zscore_252d REAL,
                        weighted_vwap_deviation REAL,

                        cross_sectional_dispersion REAL,
                        ex_top5_intraday_return REAL,

                        top_contributors TEXT,
                        bottom_detractors TEXT,
                        -- Per-constituent z-values as JSON, replacing six
                        -- stored spike counters (vol_spike_high/low +
                        -- zscore_90d_evaluated, session_return_z_spike_high/low
                        -- + session_return_z_evaluated).
                        --
                        -- Those counters baked a `> 2.0` threshold into the
                        -- metrics thread while the dashboard follows a live
                        -- slider. Storing the observations instead lets the
                        -- breadth grid AND the historical breadth chart both
                        -- recount at whatever threshold the trader picks —
                        -- a historical row cannot be recounted from a count.
                        constituent_z TEXT,

                        created_at TEXT,
                        UNIQUE(index_name, timestamp)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_index_metrics_lookup ON index_metrics(index_name, timestamp DESC)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_index_metrics_created ON index_metrics(created_at DESC)")

                # Price-Volume Divergence Metrics Table (PVD_LOCKED_V3). Its history
                # needs 5-second readings, so it cannot come from candles: it builds
                # up from live sessions (scripts/backfill.py can rebuild the days
                # ohlc.db still holds), and each pvd_z_* tier stays NULL until it has
                # its own N sessions.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS price_volume_divergence_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        asset_id INTEGER NOT NULL,
                        segment_code TEXT NOT NULL,
                        symbol TEXT,
                        timestamp TEXT NOT NULL,
                        pearson_r REAL,
                        spearman_r REAL,
                        winsorized_r REAL,
                        fisher_value REAL,
                        sample_size INTEGER,
                        critical_r REAL,
                        reliable INTEGER,
                        estimator_agreement REAL,
                        -- How many of the three estimators produced a value.
                        -- estimator_agreement is NULL below 2, so this says
                        -- whether that is "none survived" or "only one did".
                        estimators_available INTEGER,
                        price_volatility REAL,
                        status TEXT,
                        pvd_z_22d REAL, pvd_avg_22d REAL, pvd_std_22d REAL, pvd_data_points_22d INTEGER,
                        pvd_z_90d REAL, pvd_avg_90d REAL, pvd_std_90d REAL, pvd_data_points_90d INTEGER,
                        pvd_z_180d REAL, pvd_avg_180d REAL, pvd_std_180d REAL, pvd_data_points_180d INTEGER,
                        pvd_z_252d REAL, pvd_avg_252d REAL, pvd_std_252d REAL, pvd_data_points_252d INTEGER,
                        created_at TEXT,
                        UNIQUE(asset_id, segment_code, timestamp)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_pv_divergence_asset_id ON price_volume_divergence_metrics(asset_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_pv_divergence_created ON price_volume_divergence_metrics(created_at DESC)")

                # Rolling Momentum Metrics Table (ROLLING_MOMENTUM_LOCKED_V1). Continuously
                # re-evaluated at every completed 15-min block (unlike momentum_metrics,
                # which is pinned to block 1). session_return_z is stored at all four
                # tiers.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS rolling_momentum_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        asset_id INTEGER NOT NULL,
                        segment_code TEXT NOT NULL,
                        symbol TEXT,
                        timestamp TEXT NOT NULL,
                        current_price REAL,
                        previous_close REAL,
                        session_return REAL,
                        status TEXT,
                        session_return_z_22d REAL, session_return_avg_22d REAL, session_return_std_22d REAL, session_return_data_points_22d INTEGER,
                        session_return_z_90d REAL, session_return_avg_90d REAL, session_return_std_90d REAL, session_return_data_points_90d INTEGER,
                        session_return_z_180d REAL, session_return_avg_180d REAL, session_return_std_180d REAL, session_return_data_points_180d INTEGER,
                        session_return_z_252d REAL, session_return_avg_252d REAL, session_return_std_252d REAL, session_return_data_points_252d INTEGER,
                        created_at TEXT,
                        UNIQUE(asset_id, segment_code, timestamp)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_rolling_momentum_asset_id ON rolling_momentum_metrics(asset_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_rolling_momentum_created ON rolling_momentum_metrics(created_at DESC)")

                conn.commit()
                logger.info("Metrics database schema initialized successfully")

        except Exception as e:
            logger.error(f"Error initializing metrics tables: {e}")
            raise

    def get_asset_metrics_history(self, asset_id: int, segment_code: str, hours: int = 6) -> Tuple[Dict[str, pd.DataFrame], bool, Optional[datetime]]:
        """
        Get historical metrics for a specific asset across all calculators.

        Mirrors `dashboard.get_today_ohlc`'s fallback pattern. If the
        primary window (last `hours` hours from now) is empty, re-runs against
        `[MAX(created_at) - hours, MAX(created_at)]` — i.e. the last session
        that has any data. Returns `(result_dict, is_fallback, ref_ts)` so
        the dashboard can annotate charts when they're showing stale data.

        Args:
            asset_id: Asset ID to get history for
            segment_code: Segment code — required. Some asset_ids are shared
                across segments (e.g. 25 is BankNifty in IDX_I and ADANIENT in
                NSE_EQ); filtering on asset_id alone can blend the wrong entity's
                history in.
            hours: Number of hours to look back

        Returns:
            (result, is_fallback, ref_ts) — result is the calculator→DataFrame
            dict; is_fallback True means we fell back to the last-available
            window; ref_ts is the MAX(created_at) that anchored the fallback
            window (None when result is empty even in fallback).
        """
        try:
            def _run_selects(conn, cutoff_time: str) -> Dict[str, pd.DataFrame]:
                where_clause = "asset_id = ? AND segment_code = ? AND created_at >= ?"
                params = (asset_id, segment_code, cutoff_time)
                return self._run_history_selects(conn, where_clause, params)

            with sqlite3.connect(self.db_path, **self.connection_params) as conn:
                now = datetime.now(IST)
                cutoff_time = clamp_to_day_start(now - timedelta(hours=hours), now).isoformat()
                result = _run_selects(conn, cutoff_time)

                if result:
                    return result, False, None

                # Fallback: find the most recent timestamp in momentum_metrics
                # (writes every cycle for every asset — most complete anchor)
                # and re-run with a window ending there.
                max_ts_row = conn.execute(
                    "SELECT MAX(created_at) FROM momentum_metrics WHERE asset_id = ? AND segment_code = ?",
                    (asset_id, segment_code),
                ).fetchone()

                max_ts_str = max_ts_row[0] if max_ts_row else None
                if not max_ts_str:
                    return {}, False, None

                max_ts = datetime.fromisoformat(max_ts_str)
                fallback_cutoff = clamp_to_day_start(
                    max_ts - timedelta(hours=hours), max_ts
                ).isoformat()
                result = _run_selects(conn, fallback_cutoff)
                return result, True, max_ts

        except Exception as e:
            logger.error(f"Error getting asset metrics history for asset {asset_id}: {e}")
            return {}, False, None

    def _run_history_selects(self, conn, where_clause: str, params: tuple) -> Dict[str, pd.DataFrame]:
        """Run the 9 per-calculator history SELECTs with the given WHERE + params.
        Extracted from `get_asset_metrics_history` so the fallback branch can
        reuse the exact same SELECTs without duplication."""
        result: Dict[str, pd.DataFrame] = {}
        df_momentum = pd.read_sql_query(f"""
            SELECT timestamp, current_price, open_price, previous_close, price_after_window,
                   gap_return, gap_direction, early_return, continuation, acceleration,
                   status,
                   gap_z_22d, gap_avg_22d, gap_std_22d, gap_data_points_22d,
                   gap_z_90d, gap_avg_90d, gap_std_90d, gap_data_points_90d,
                   gap_z_180d, gap_avg_180d, gap_std_180d, gap_data_points_180d,
                   gap_z_252d, gap_avg_252d, gap_std_252d, gap_data_points_252d,
                   early_return_z_22d, early_return_avg_22d, early_return_std_22d, early_return_data_points_22d,
                   early_return_z_90d, early_return_avg_90d, early_return_std_90d, early_return_data_points_90d,
                   early_return_z_180d, early_return_avg_180d, early_return_std_180d, early_return_data_points_180d,
                   early_return_z_252d, early_return_avg_252d, early_return_std_252d, early_return_data_points_252d
            FROM momentum_metrics
            WHERE {where_clause}
            ORDER BY created_at DESC
        """, conn, params=params)
        if not df_momentum.empty:
            result['momentum'] = df_momentum

        df_breakout = pd.read_sql_query(f"""
            SELECT timestamp, current_price, range_high, range_low, range_width,
                   breakout_position, fresh_breakout, breakout_direction,
                   accepted_breakout, failed_breakout, distance_from_range, status
            FROM breakout_metrics
            WHERE {where_clause}
            ORDER BY created_at DESC
        """, conn, params=params)
        if not df_breakout.empty:
            result['breakout'] = df_breakout

        df_vwap = pd.read_sql_query(f"""
            SELECT timestamp, current_price, vwap, displacement, side, acceptance, status,
                   vwap_z_22d, vwap_avg_22d, vwap_std_22d, vwap_data_points_22d,
                   vwap_z_90d, vwap_avg_90d, vwap_std_90d, vwap_data_points_90d,
                   vwap_z_180d, vwap_avg_180d, vwap_std_180d, vwap_data_points_180d,
                   vwap_z_252d, vwap_avg_252d, vwap_std_252d, vwap_data_points_252d
            FROM vwap_metrics
            WHERE {where_clause}
            ORDER BY created_at DESC
        """, conn, params=params)
        if not df_vwap.empty:
            result['vwap'] = df_vwap

        df_rel_strength = pd.read_sql_query(f"""
            SELECT timestamp, current_price, benchmark_name, benchmark_id,
                   status,
                   beta_22d, beta_90d, beta_180d, beta_252d,
                   session_rs_22d, session_rs_90d, session_rs_180d, session_rs_252d,
                   intraday_rs_22d, intraday_rs_90d, intraday_rs_180d, intraday_rs_252d,
                   acceptance_22d, acceptance_90d, acceptance_180d, acceptance_252d,
                   session_rs_z_22d, session_rs_avg_22d, session_rs_std_22d, session_rs_data_points_22d,
                   session_rs_z_90d, session_rs_avg_90d, session_rs_std_90d, session_rs_data_points_90d,
                   session_rs_z_180d, session_rs_avg_180d, session_rs_std_180d, session_rs_data_points_180d,
                   session_rs_z_252d, session_rs_avg_252d, session_rs_std_252d, session_rs_data_points_252d,
                   intraday_rs_z_22d, intraday_rs_avg_22d, intraday_rs_std_22d, intraday_rs_data_points_22d,
                   intraday_rs_z_90d, intraday_rs_avg_90d, intraday_rs_std_90d, intraday_rs_data_points_90d,
                   intraday_rs_z_180d, intraday_rs_avg_180d, intraday_rs_std_180d, intraday_rs_data_points_180d,
                   intraday_rs_z_252d, intraday_rs_avg_252d, intraday_rs_std_252d, intraday_rs_data_points_252d
            FROM relative_strength_metrics
            WHERE {where_clause}
            ORDER BY created_at DESC
        """, conn, params=params)
        if not df_rel_strength.empty:
            result['relative_strength'] = df_rel_strength

        df_volume_dynamics = pd.read_sql_query(f"""
            SELECT timestamp, time_block, status,
                   volume_z_22d, volume_z_90d, volume_z_180d, volume_z_252d,
                   velocity_22d, velocity_90d, velocity_180d, velocity_252d,
                   acceleration_22d, acceleration_90d, acceleration_180d, acceleration_252d,
                   persistence_22d, persistence_90d, persistence_180d, persistence_252d,
                   expansion_state_22d, expansion_state_90d, expansion_state_180d, expansion_state_252d
            FROM volume_dynamics_metrics
            WHERE {where_clause}
            ORDER BY created_at DESC
        """, conn, params=params)
        if not df_volume_dynamics.empty:
            result['volume_dynamics'] = df_volume_dynamics

        df_pv_divergence = pd.read_sql_query(f"""
            SELECT timestamp, pearson_r, spearman_r, winsorized_r, fisher_value,
                   sample_size, critical_r, reliable, estimator_agreement,
                   estimators_available, price_volatility, status,
                   pvd_z_22d, pvd_avg_22d, pvd_std_22d, pvd_data_points_22d,
                   pvd_z_90d, pvd_avg_90d, pvd_std_90d, pvd_data_points_90d,
                   pvd_z_180d, pvd_avg_180d, pvd_std_180d, pvd_data_points_180d,
                   pvd_z_252d, pvd_avg_252d, pvd_std_252d, pvd_data_points_252d
            FROM price_volume_divergence_metrics
            WHERE {where_clause}
            ORDER BY created_at DESC
        """, conn, params=params)
        if not df_pv_divergence.empty:
            result['price_volume_divergence'] = df_pv_divergence

        df_volume_block = pd.read_sql_query(f"""
            SELECT timestamp, time_block, block_volume, session_cumulative_volume, status,
                   block_volume_z_22d, block_volume_avg_22d, block_volume_std_22d, block_volume_data_points_22d,
                   block_volume_z_90d, block_volume_avg_90d, block_volume_std_90d, block_volume_data_points_90d,
                   block_volume_z_180d, block_volume_avg_180d, block_volume_std_180d, block_volume_data_points_180d,
                   block_volume_z_252d, block_volume_avg_252d, block_volume_std_252d, block_volume_data_points_252d,
                   session_cumulative_volume_z_22d, session_cumulative_volume_avg_22d, session_cumulative_volume_std_22d, session_cumulative_volume_data_points_22d,
                   session_cumulative_volume_z_90d, session_cumulative_volume_avg_90d, session_cumulative_volume_std_90d, session_cumulative_volume_data_points_90d,
                   session_cumulative_volume_z_180d, session_cumulative_volume_avg_180d, session_cumulative_volume_std_180d, session_cumulative_volume_data_points_180d,
                   session_cumulative_volume_z_252d, session_cumulative_volume_avg_252d, session_cumulative_volume_std_252d, session_cumulative_volume_data_points_252d
            FROM volume_block_metrics
            WHERE {where_clause}
            ORDER BY created_at DESC
        """, conn, params=params)
        if not df_volume_block.empty:
            result['volume_block'] = df_volume_block

        df_price_levels = pd.read_sql_query(f"""
            SELECT timestamp, current_price, levels, day_events, status
            FROM price_levels_metrics
            WHERE {where_clause}
            ORDER BY created_at DESC
        """, conn, params=params)
        if not df_price_levels.empty:
            result['price_levels'] = df_price_levels

        df_rolling_momentum = pd.read_sql_query(f"""
            SELECT timestamp, current_price, previous_close, session_return, status,
                   session_return_z_22d, session_return_avg_22d, session_return_std_22d, session_return_data_points_22d,
                   session_return_z_90d, session_return_avg_90d, session_return_std_90d, session_return_data_points_90d,
                   session_return_z_180d, session_return_avg_180d, session_return_std_180d, session_return_data_points_180d,
                   session_return_z_252d, session_return_avg_252d, session_return_std_252d, session_return_data_points_252d
            FROM rolling_momentum_metrics
            WHERE {where_clause}
            ORDER BY created_at DESC
        """, conn, params=params)
        if not df_rolling_momentum.empty:
            result['rolling_momentum'] = df_rolling_momentum

        return result

    def store_momentum_metrics(self, metrics: List[Dict]) -> int:
        """Store momentum metrics for all assets (MOMENTUM_LOCKED_V2 schema).

        Any field may be None (e.g. a tier with fewer than its own N sessions
        of history, or pre-09:30 window-dependent fields). NULLs land cleanly
        in the DB.
        """
        if not metrics:
            return 0

        cols = [
            'asset_id', 'segment_code', 'symbol', 'timestamp',
            'current_price', 'open_price', 'high_price', 'low_price',
            'previous_close', 'price_after_window',
            'gap_return', 'gap_direction', 'early_return', 'continuation', 'acceleration',
            'status',
        ]
        for prefix in ('gap', 'early_return'):
            for tier in TIERS:
                cols += [f'{prefix}_z_{tier}d', f'{prefix}_avg_{tier}d',
                         f'{prefix}_std_{tier}d', f'{prefix}_data_points_{tier}d']

        stored_count = 0
        try:
            with sqlite3.connect(self.db_path, **self.connection_params) as conn:
                for metric in metrics:
                    created_at = datetime.now(IST).isoformat()
                    values = [metric.get(c) for c in cols] + [created_at]
                    placeholders = ', '.join(['?'] * len(values))
                    cursor = conn.execute(f"""
                        INSERT OR REPLACE INTO momentum_metrics
                        ({', '.join(cols)}, created_at)
                        VALUES ({placeholders})
                    """, values)
                    if cursor.rowcount > 0:
                        stored_count += 1
                conn.commit()
                logger.debug(f"Stored {stored_count} momentum metrics")
        except Exception as e:
            logger.error(f"Error storing momentum metrics: {e}")
            raise

        return stored_count

    def store_breakout_metrics(self, metrics: List[Dict]) -> int:
        """Store breakout metrics for all assets (BREAKOUT_LOCKED_V1 schema)."""
        if not metrics:
            return 0

        stored_count = 0
        try:
            with sqlite3.connect(self.db_path, **self.connection_params) as conn:
                for metric in metrics:
                    timestamp = metric.get('timestamp', datetime.now(IST).isoformat())
                    created_at = datetime.now(IST).isoformat()

                    cursor = conn.execute("""
                        INSERT OR REPLACE INTO breakout_metrics
                        (asset_id, segment_code, symbol, timestamp, current_price,
                         range_high, range_low, range_width, breakout_position,
                         fresh_breakout, breakout_direction, breakout_now, accepted_breakout,
                         failed_breakout, distance_from_range, status, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        metric.get('asset_id'),
                        metric.get('segment_code', ''),
                        metric.get('symbol'),
                        timestamp,
                        metric.get('current_price'),
                        metric.get('range_high'),
                        metric.get('range_low'),
                        metric.get('range_width'),
                        metric.get('breakout_position'),
                        metric.get('fresh_breakout', 0),
                        metric.get('breakout_direction', 0),
                        metric.get('breakout_now', 0),
                        metric.get('accepted_breakout', 0),
                        metric.get('failed_breakout', 0),
                        metric.get('distance_from_range'),
                        metric.get('status'),
                        created_at
                    ))

                    if cursor.rowcount > 0:
                        stored_count += 1

                conn.commit()
                logger.debug(f"Stored {stored_count} breakout metrics")

        except Exception as e:
            logger.error(f"Error storing breakout metrics: {e}")
            raise

        return stored_count

    def store_price_levels_metrics(self, metrics: List[Dict]) -> int:
        """Store price levels metrics for all assets (PRICE_LEVELS_LOCKED_V1 schema
        — replaces the old store_price_alerts_metrics)."""
        if not metrics:
            return 0

        stored_count = 0
        try:
            with sqlite3.connect(self.db_path, **self.connection_params) as conn:
                for metric in metrics:
                    timestamp = metric.get('timestamp', datetime.now(IST).isoformat())
                    created_at = datetime.now(IST).isoformat()

                    levels = _json_compact(metric.get('levels', {}))
                    day_events = _json_compact(metric.get('day_events', []))

                    cursor = conn.execute("""
                        INSERT OR REPLACE INTO price_levels_metrics
                        (asset_id, segment_code, symbol, timestamp, current_price,
                         levels, day_events, status, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        metric.get('asset_id'),
                        metric.get('segment_code'),
                        metric.get('symbol'),
                        timestamp,
                        metric.get('current_price'),
                        levels,
                        day_events,
                        metric.get('status'),
                        created_at
                    ))

                    if cursor.rowcount > 0:
                        stored_count += 1

                conn.commit()
                logger.debug(f"Stored {stored_count} price levels metrics")

        except Exception as e:
            logger.error(f"Error storing price levels metrics: {e}")
            raise

        return stored_count

    def store_vwap_metrics(self, metrics: List[Dict]) -> int:
        """Store VWAP metrics for all assets (VWAP_LOCKED_V1 schema — no bands)."""
        if not metrics:
            return 0

        stored_count = 0
        try:
            with sqlite3.connect(self.db_path, **self.connection_params) as conn:
                for metric in metrics:
                    timestamp = metric.get('timestamp', datetime.now(IST).isoformat())
                    created_at = datetime.now(IST).isoformat()

                    cursor = conn.execute("""
                        INSERT OR REPLACE INTO vwap_metrics
                        (asset_id, segment_code, symbol, timestamp, current_price, vwap,
                         displacement, side, acceptance, status,
                         vwap_z_22d, vwap_avg_22d, vwap_std_22d, vwap_data_points_22d,
                         vwap_z_90d, vwap_avg_90d, vwap_std_90d, vwap_data_points_90d,
                         vwap_z_180d, vwap_avg_180d, vwap_std_180d, vwap_data_points_180d,
                         vwap_z_252d, vwap_avg_252d, vwap_std_252d, vwap_data_points_252d,
                         created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        metric.get('asset_id'),
                        metric.get('segment_code', ''),
                        metric.get('symbol'),
                        timestamp,
                        metric.get('current_price'),
                        metric.get('vwap'),
                        metric.get('displacement'),
                        metric.get('side'),
                        metric.get('acceptance'),
                        metric.get('status'),
                        metric.get('vwap_z_22d'), metric.get('vwap_avg_22d'),
                        metric.get('vwap_std_22d'), metric.get('vwap_data_points_22d'),
                        metric.get('vwap_z_90d'), metric.get('vwap_avg_90d'),
                        metric.get('vwap_std_90d'), metric.get('vwap_data_points_90d'),
                        metric.get('vwap_z_180d'), metric.get('vwap_avg_180d'),
                        metric.get('vwap_std_180d'), metric.get('vwap_data_points_180d'),
                        metric.get('vwap_z_252d'), metric.get('vwap_avg_252d'),
                        metric.get('vwap_std_252d'), metric.get('vwap_data_points_252d'),
                        created_at
                    ))

                    if cursor.rowcount > 0:
                        stored_count += 1

                conn.commit()
                logger.debug(f"Stored {stored_count} VWAP metrics")

        except Exception as e:
            logger.error(f"Error storing VWAP metrics: {e}")
            raise

        return stored_count

    def store_relative_strength_metrics(self, metrics: List[Dict]) -> int:
        """Store relative strength metrics for all assets
        (RELATIVE_STRENGTH_LOCKED_V1 schema). All four beta tiers come from
        the precomputed daily cache, and session_rs / intraday_rs /
        acceptance are stored once per tier because each is measured with
        that tier's beta.

        Any tier field may be None — insufficient history at that tier, or no
        cached beta at that tier (a stock or benchmark without enough daily
        history). NULLs land cleanly, and a NULL tier is the truth: it
        renders as a hyphen rather than a fabricated zero.
        """
        if not metrics:
            return 0

        cols = [
            'asset_id', 'segment_code', 'symbol', 'timestamp',
            'benchmark_name', 'benchmark_id', 'current_price', 'open_price',
            'status',
        ]
        cols += [f'beta_{tier}d' for tier in TIERS]
        # Per-tier measured values...
        for prefix in ('session_rs', 'intraday_rs', 'acceptance'):
            cols += [f'{prefix}_{tier}d' for tier in TIERS]
        # ...and the z bundle for the two that are z-scored. `acceptance` is a
        # product whose SIGN is the signal, not a magnitude to normalise
        # against history, so it carries no z bundle.
        for prefix in ('session_rs', 'intraday_rs'):
            for tier in TIERS:
                cols += [f'{prefix}_z_{tier}d', f'{prefix}_avg_{tier}d',
                         f'{prefix}_std_{tier}d', f'{prefix}_data_points_{tier}d']

        stored_count = 0
        try:
            with sqlite3.connect(self.db_path, **self.connection_params) as conn:
                for metric in metrics:
                    created_at = datetime.now(IST).isoformat()
                    values = [metric.get(c) for c in cols] + [created_at]
                    placeholders = ', '.join(['?'] * len(values))
                    cursor = conn.execute(f"""
                        INSERT OR REPLACE INTO relative_strength_metrics
                        ({', '.join(cols)}, created_at)
                        VALUES ({placeholders})
                    """, values)
                    if cursor.rowcount > 0:
                        stored_count += 1
                conn.commit()
                logger.debug(f"Stored {stored_count} relative strength metrics")
        except Exception as e:
            logger.error(f"Error storing relative strength metrics: {e}")
            raise

        return stored_count

    def store_volume_dynamics_metrics(self, metrics: List[Dict]) -> int:
        """Store volume dynamics metrics (VOLUME_DYNAMICS_LOCKED_V3, 4 tiers).

        Any field may be None — block-1 has no velocity and blocks 1-2 have no
        acceleration (they need 2-3 prior blocks), and a tier with too little
        history to produce a ZQ has nothing to differentiate at all. NULLs land
        cleanly in the DB.

        The column list is GENERATED from the tier convention rather than
        hand-written. Twenty near-identical tier fields as a positional tuple is
        exactly where a transposition hides — `persistence_180d` receiving the
        252d value reads perfectly well — and generating both halves from one
        list makes the columns and the params impossible to desynchronise.
        """
        if not metrics:
            return 0

        cols = ['asset_id', 'segment_code', 'symbol', 'timestamp', 'time_block', 'status']
        for prefix in ('volume_z', 'velocity', 'acceleration', 'persistence', 'expansion_state'):
            for tier in TIERS:
                cols.append(f'{prefix}_{tier}d')

        stored_count = 0
        try:
            with sqlite3.connect(self.db_path, **self.connection_params) as conn:
                for metric in metrics:
                    metric = dict(metric)
                    metric.setdefault('timestamp', datetime.now(IST).isoformat())
                    values = [metric.get(c) for c in cols] + [datetime.now(IST).isoformat()]
                    placeholders = ', '.join(['?'] * len(values))
                    cursor = conn.execute(f"""
                        INSERT OR REPLACE INTO volume_dynamics_metrics
                        ({', '.join(cols)}, created_at)
                        VALUES ({placeholders})
                    """, values)

                    if cursor.rowcount > 0:
                        stored_count += 1

                conn.commit()
                logger.debug(f"Stored {stored_count} volume dynamics metrics")

        except Exception as e:
            logger.error(f"Error storing volume dynamics metrics: {e}")
            raise

        return stored_count

    def store_price_volume_divergence_metrics(self, metrics: List[Dict]) -> int:
        """Store price-volume divergence metrics for all assets (PVD_LOCKED_V3
        schema). Each pvd_z_* tier stays None until its history has that
        tier's N sessions. NULLs land cleanly.
        """
        if not metrics:
            return 0

        # Column list GENERATED, not hand-written. This INSERT previously
        # maintained 31 positional placeholders against a 30-name column list
        # across three separate blocks — the exact shape where a miscount
        # shifts every value one column left and still succeeds.
        cols = [
            'asset_id', 'segment_code', 'symbol', 'timestamp',
            'pearson_r', 'spearman_r', 'winsorized_r', 'fisher_value', 'sample_size',
            'critical_r', 'reliable', 'estimator_agreement', 'estimators_available',
            'price_volatility', 'status',
        ]
        for tier in TIERS:
            cols += [f'pvd_z_{tier}d', f'pvd_avg_{tier}d',
                     f'pvd_std_{tier}d', f'pvd_data_points_{tier}d']

        stored_count = 0
        try:
            with sqlite3.connect(self.db_path, **self.connection_params) as conn:
                for metric in metrics:
                    metric = dict(metric)
                    metric.setdefault('timestamp', datetime.now(IST).isoformat())
                    metric.setdefault('segment_code', '')
                    values = [metric.get(c) for c in cols] + [datetime.now(IST).isoformat()]
                    placeholders = ', '.join(['?'] * len(values))
                    cursor = conn.execute(f"""
                        INSERT OR REPLACE INTO price_volume_divergence_metrics
                        ({', '.join(cols)}, created_at)
                        VALUES ({placeholders})
                    """, values)

                    if cursor.rowcount > 0:
                        stored_count += 1

                conn.commit()
                logger.debug(f"Stored {stored_count} price-volume divergence metrics")

        except Exception as e:
            logger.error(f"Error storing price-volume divergence metrics: {e}")
            raise

        return stored_count

    def store_rolling_momentum_metrics(self, metrics: List[Dict]) -> int:
        """Store rolling momentum metrics for all assets (ROLLING_MOMENTUM_LOCKED_V1
        schema). Re-evaluated at every completed 15-min block."""
        if not metrics:
            return 0

        stored_count = 0
        try:
            with sqlite3.connect(self.db_path, **self.connection_params) as conn:
                for metric in metrics:
                    timestamp = metric.get('timestamp', datetime.now(IST).isoformat())
                    created_at = datetime.now(IST).isoformat()

                    cursor = conn.execute("""
                        INSERT OR REPLACE INTO rolling_momentum_metrics
                        (asset_id, segment_code, symbol, timestamp,
                         current_price, previous_close, session_return, status,
                         session_return_z_22d, session_return_avg_22d, session_return_std_22d, session_return_data_points_22d,
                         session_return_z_90d, session_return_avg_90d, session_return_std_90d, session_return_data_points_90d,
                         session_return_z_180d, session_return_avg_180d, session_return_std_180d, session_return_data_points_180d,
                         session_return_z_252d, session_return_avg_252d, session_return_std_252d, session_return_data_points_252d,
                         created_at)
                        VALUES (?, ?, ?, ?,
                                ?, ?, ?, ?,
                                ?, ?, ?, ?,
                                ?, ?, ?, ?,
                                ?, ?, ?, ?,
                                ?, ?, ?, ?,
                                ?)
                    """, (
                        metric.get('asset_id'),
                        metric.get('segment_code', ''),
                        metric.get('symbol'),
                        timestamp,
                        metric.get('current_price'),
                        metric.get('previous_close'),
                        metric.get('session_return'),
                        metric.get('status'),
                        metric.get('session_return_z_22d'),
                        metric.get('session_return_avg_22d'),
                        metric.get('session_return_std_22d'),
                        metric.get('session_return_data_points_22d'),
                        metric.get('session_return_z_90d'),
                        metric.get('session_return_avg_90d'),
                        metric.get('session_return_std_90d'),
                        metric.get('session_return_data_points_90d'),
                        metric.get('session_return_z_180d'),
                        metric.get('session_return_avg_180d'),
                        metric.get('session_return_std_180d'),
                        metric.get('session_return_data_points_180d'),
                        metric.get('session_return_z_252d'),
                        metric.get('session_return_avg_252d'),
                        metric.get('session_return_std_252d'),
                        metric.get('session_return_data_points_252d'),
                        created_at,
                    ))

                    if cursor.rowcount > 0:
                        stored_count += 1

                conn.commit()
                logger.debug(f"Stored {stored_count} rolling momentum metrics")

        except Exception as e:
            logger.error(f"Error storing rolling momentum metrics: {e}")
            raise

        return stored_count

    def store_volume_block_metrics(self, metrics: List[Dict]) -> int:
        """Store volume block metrics for all assets (TRADE_VOLUME_BLOCK_LOCKED_V4
        schema — replaces the old store_time_block_volume_metrics)."""
        if not metrics:
            return 0

        cols = ['asset_id', 'segment_code', 'symbol', 'timestamp', 'time_block',
                'block_volume', 'session_cumulative_volume', 'session_cumulative_blocks', 'status']
        for prefix in ('block_volume', 'session_cumulative_volume'):
            for tier in TIERS:
                cols += [f'{prefix}_z_{tier}d', f'{prefix}_avg_{tier}d',
                         f'{prefix}_std_{tier}d', f'{prefix}_data_points_{tier}d']

        stored_count = 0
        try:
            with sqlite3.connect(self.db_path, **self.connection_params) as conn:
                for metric in metrics:
                    created_at = datetime.now(IST).isoformat()
                    values = [metric.get(c) for c in cols] + [created_at]
                    placeholders = ', '.join(['?'] * len(values))
                    cursor = conn.execute(f"""
                        INSERT OR REPLACE INTO volume_block_metrics
                        ({', '.join(cols)}, created_at)
                        VALUES ({placeholders})
                    """, values)
                    if cursor.rowcount > 0:
                        stored_count += 1
                conn.commit()
                logger.debug(f"Stored {stored_count} volume block metrics")
        except Exception as e:
            logger.error(f"Error storing volume block metrics: {e}")
            raise

        return stored_count

    def store_index_metrics(self, summaries: Dict[str, Dict]) -> int:
        """
        Store per-index aggregate summaries — one row per index per cycle.

        Args:
            summaries: {'NIFTY': {summary_dict}, 'BANKNIFTY': {...}, ...}

        Returns: number of rows inserted/replaced.
        """
        if not summaries:
            return 0

        stored_count = 0
        try:
            with sqlite3.connect(self.db_path, **self.connection_params) as conn:
                created_at = datetime.now(IST).isoformat()
                for index_name, s in summaries.items():
                    if not isinstance(s, dict):
                        continue
                    timestamp = s.get('timestamp') or created_at

                    # Columns derived, placeholders derived from the values.
                    # The previous form hand-wrote 33 `?` characters; an
                    # off-by-one there does not raise — it INSERTs successfully
                    # with every value shifted one column left, which reads as
                    # entirely plausible data. Nothing to miscount here.
                    cols = [
                        'timestamp', 'index_name',
                        'index_price', 'intraday_pct', 'total_constituents',
                        'advancers', 'decliners', 'unchanged',
                        'above_vwap', 'below_vwap', 'vwap_evaluated',
                        'in_breakout_up', 'in_breakout_down', 'breakout_evaluated',
                        'level_breached_today',
                        'weighted_intraday_return',
                    ]
                    for stem in ('positive_rs', 'negative_rs', 'rs_evaluated'):
                        cols += [f'{stem}_{t}d' for t in TIERS]
                    cols += [f'weighted_volume_velocity_{t}d' for t in TIERS]
                    cols += [f'weighted_volume_zscore_{t}d' for t in TIERS]
                    cols += [
                        'weighted_vwap_deviation',
                        'cross_sectional_dispersion', 'ex_top5_intraday_return',
                    ]
                    _JSON_COLS = ('top_contributors', 'bottom_detractors', 'constituent_z')
                    cols += list(_JSON_COLS)

                    values = []
                    for c in cols:
                        if c == 'timestamp':
                            values.append(timestamp)
                        elif c == 'index_name':
                            values.append(index_name)
                        elif c in _JSON_COLS:
                            # constituent_z is a dict; the other two are lists.
                            empty = {} if c == 'constituent_z' else []
                            values.append(_json_compact(s.get(c) if s.get(c) is not None else empty))
                        else:
                            values.append(s.get(c))
                    values.append(created_at)

                    cursor = conn.execute(f"""
                        INSERT OR REPLACE INTO index_metrics
                        ({', '.join(cols)}, created_at)
                        VALUES ({', '.join(['?'] * len(values))})
                    """, values)
                    if cursor.rowcount > 0:
                        stored_count += 1

                conn.commit()
                logger.debug(f"Stored {stored_count} index_metrics rows")

        except Exception as e:
            logger.error(f"Error storing index metrics: {e}")
            raise

        return stored_count

    # ========================================================================
    # Startup hydration readers
    # ------------------------------------------------------------------------
    # One "latest row per (asset_id, segment_code)" helper per calculator table
    # + one for index_metrics. Called once by MetricsOrchestrator on startup to
    # prefill LATEST_METRICS / LATEST_INDEX_METRICS so the dashboard is never
    # blank outside market hours.
    #
    # Each returns (list_of_dicts, max_timestamp | None). Dict shape matches
    # the calculator's Row.asdict() shape byte-for-byte for the persisted
    # columns; a Row field with no metrics.db column would simply be absent
    # from the loaded dict — dashboard's `.get(key)` handles the missing key.
    # JSON columns (levels, day_events, top_contributors, bottom_detractors,
    # constituent_z) round-trip through json.loads.
    # ========================================================================

    def _latest_rows_per_asset(self, table: str, drop_columns=('id', 'created_at')) -> list:
        """Shared helper: SELECT the most-recent row per (asset_id, segment_code)
        from `table`. Returns list-of-dicts (empty list if table is empty)."""
        try:
            with sqlite3.connect(self.db_path, **self.connection_params) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(f"""
                    SELECT * FROM {table}
                    WHERE (asset_id, segment_code, timestamp) IN (
                        SELECT asset_id, segment_code, MAX(timestamp)
                        FROM {table}
                        GROUP BY asset_id, segment_code
                    )
                """)
                rows = []
                for row in cursor:
                    d = dict(row)
                    for c in drop_columns:
                        d.pop(c, None)
                    rows.append(d)
                return rows
        except Exception as e:
            logger.error(f"_latest_rows_per_asset({table}) failed: {e}")
            return []

    def _max_ts(self, rows: list) -> Optional[str]:
        """Return the max timestamp string across the returned rows (or None)."""
        return max((r.get('timestamp') for r in rows if r.get('timestamp')), default=None)

    def get_latest_momentum(self):
        rows = self._latest_rows_per_asset('momentum_metrics')
        return rows, self._max_ts(rows)

    def get_latest_breakout(self):
        rows = self._latest_rows_per_asset('breakout_metrics')
        return rows, self._max_ts(rows)

    def get_latest_vwap(self):
        rows = self._latest_rows_per_asset('vwap_metrics')
        return rows, self._max_ts(rows)

    def get_latest_relative_strength(self):
        """RS has a 4-column composite key (asset_id, segment_code, benchmark_name, timestamp).
        Multi-benchmark stocks emit N rows per cycle — hydrate all of them."""
        try:
            with sqlite3.connect(self.db_path, **self.connection_params) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("""
                    SELECT * FROM relative_strength_metrics
                    WHERE (asset_id, segment_code, benchmark_name, timestamp) IN (
                        SELECT asset_id, segment_code, benchmark_name, MAX(timestamp)
                        FROM relative_strength_metrics
                        GROUP BY asset_id, segment_code, benchmark_name
                    )
                """)
                rows = []
                for row in cursor:
                    d = dict(row)
                    d.pop('id', None); d.pop('created_at', None)
                    rows.append(d)
                return rows, self._max_ts(rows)
        except Exception as e:
            logger.error(f"get_latest_relative_strength failed: {e}")
            return [], None

    def get_latest_volume_dynamics(self):
        rows = self._latest_rows_per_asset('volume_dynamics_metrics')
        return rows, self._max_ts(rows)

    def get_latest_price_volume_divergence(self):
        rows = self._latest_rows_per_asset('price_volume_divergence_metrics')
        return rows, self._max_ts(rows)

    def get_latest_volume_block(self):
        rows = self._latest_rows_per_asset('volume_block_metrics')
        return rows, self._max_ts(rows)

    def get_latest_price_levels(self):
        rows = self._latest_rows_per_asset('price_levels_metrics')
        for r in rows:
            for col, empty in (('levels', {}), ('day_events', [])):
                v = r.get(col)
                if isinstance(v, str) and v:
                    try:
                        r[col] = json.loads(v)
                    except Exception:
                        r[col] = empty
                elif v is None:
                    r[col] = empty
        return rows, self._max_ts(rows)

    def get_latest_rolling_momentum(self):
        rows = self._latest_rows_per_asset('rolling_momentum_metrics')
        return rows, self._max_ts(rows)

    def get_latest_index_metrics(self):
        """Return `{index_name: dict}` — one row per index (latest timestamp)."""
        try:
            with sqlite3.connect(self.db_path, **self.connection_params) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("""
                    SELECT * FROM index_metrics
                    WHERE (index_name, timestamp) IN (
                        SELECT index_name, MAX(timestamp)
                        FROM index_metrics
                        GROUP BY index_name
                    )
                """)
                result = {}
                latest_ts = None
                for row in cursor:
                    d = dict(row)
                    d.pop('id', None); d.pop('created_at', None)
                    # constituent_z is a MAPPING; the other two are lists, so
                    # their empty values differ. Decoding matters more than it
                    # looks: an undecoded constituent_z is a JSON *string*, and
                    # the breadth grid would iterate its characters instead of
                    # its constituents — truthy, no exception, silently wrong
                    # counts on every off-hours page load.
                    for col, empty in (('top_contributors', []),
                                       ('bottom_detractors', []),
                                       ('constituent_z', {})):
                        v = d.get(col)
                        if isinstance(v, str) and v:
                            try:
                                d[col] = json.loads(v)
                            except Exception:
                                d[col] = empty
                        elif v is None:
                            d[col] = empty
                    idx_name = d.get('index_name')
                    if idx_name:
                        result[idx_name] = d
                        ts = d.get('timestamp')
                        if ts and (latest_ts is None or ts > latest_ts):
                            latest_ts = ts
                return result, latest_ts
        except Exception as e:
            logger.error(f"get_latest_index_metrics failed: {e}")
            return {}, None

    def cleanup_old_metrics(self, days_to_keep: int = 7) -> int:
        """
        Delete metrics older than specified days from all metrics tables.

        Args:
            days_to_keep: Number of days of metrics to retain (default: 7)

        Returns:
            Total number of records deleted across all tables
        """
        try:
            cutoff_time = (datetime.now(IST) - timedelta(days=days_to_keep)).isoformat()
            total_deleted = 0

            with sqlite3.connect(self.db_path, **self.connection_params) as conn:
                tables = [
                    'momentum_metrics',
                    'breakout_metrics',
                    'price_levels_metrics',
                    'vwap_metrics',
                    'relative_strength_metrics',
                    'volume_dynamics_metrics',
                    'volume_block_metrics',
                    'index_metrics',
                    'price_volume_divergence_metrics',
                    'rolling_momentum_metrics',
                ]

                for table in tables:
                    cursor = conn.execute(f"""
                        DELETE FROM {table}
                        WHERE created_at < ?
                    """, (cutoff_time,))
                    deleted_count = cursor.rowcount
                    total_deleted += deleted_count
                    logger.debug(f"Deleted {deleted_count} old records from {table}")

                conn.commit()
                logger.info(f"Cleanup complete: deleted {total_deleted} total records older than {days_to_keep} days")

            return total_deleted

        except Exception as e:
            logger.error(f"Error cleaning up old metrics: {e}")
            return 0


# Factory function for easy integration
def create_metrics_db_manager(db_path: str = None) -> MetricsDatabaseManager:
    """Create MetricsDatabaseManager instance"""
    return MetricsDatabaseManager(db_path)