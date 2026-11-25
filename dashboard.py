"""
Metrics Dashboard for Real-Time Trading Metrics
Displays ALL 86 assets with comprehensive metrics from all calculators
"""

import pandas as pd
import json
import math
import sqlite3
from dash import Dash, html, dcc, dash_table, Input, Output, State, no_update, ALL, ctx
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from datetime import datetime, timedelta
from typing import Optional
import logging
import os
import secrets
import time

logger = logging.getLogger(__name__)

# Sync dashboard refresh interval with metrics collection interval
try:
    from metrics import COLLECTION_INTERVAL_SECONDS as _INTERVAL
except ImportError:
    _INTERVAL = 30

# Assets path
ASSETS_CSV = "assets/assets_select.csv"

# Initialize MetricsDatabaseManager for chart queries
_metrics_db = None
def get_metrics_db():
    global _metrics_db
    if _metrics_db is None:
        try:
            from calculators.metrics_database_manager import MetricsDatabaseManager
            _metrics_db = MetricsDatabaseManager()
            logger.info("MetricsDatabaseManager initialized for charts")
        except Exception as e:
            logger.error(f"Failed to initialize MetricsDatabaseManager: {e}")
    return _metrics_db


# Load app config (config.json at project root)
_APP_CONFIG = {}
try:
    with open('config.json', 'r') as _f:
        _APP_CONFIG = json.load(_f)
except Exception:
    pass


# ============================================================================
# Config knobs — single deref site per knob at import time.
#
# Every config.json section this file reads is dereferenced ONCE here into a
# named module constant. Grepping for the constant reveals every use site.
# Editing config.json + docker-compose restart is the intended update path;
# no need to hunt through inline .get('...',default) calls scattered in
# callbacks or chart builders.
# ============================================================================
_EMA_SPAN_5MIN  = int(_APP_CONFIG.get('ema', {}).get('span_5min', 60))
_EMA_SPAN_15MIN = int(_APP_CONFIG.get('ema', {}).get('span_15min', 180))
_EMA_SPAN_30MIN = int(_APP_CONFIG.get('ema', {}).get('span_30min', 360))
_ATR_PERIOD     = int(_APP_CONFIG.get('atr', {}).get('period', 14))


# ============================================================================
# Startup contract check (typed schemas)
#
# For every field the dashboard reads from the LATEST_METRICS cache, we require
# it to be either (a) a field of some Row dataclass in calculators/schemas.py, or
# (b) a documented COLLIDING_FIELDS alias (dashboard-side rename to avoid
# cross-calculator collision — e.g. `status` → `breakout_status`).
#
# A field-rename in any calculator that forgets to update `_CONSUMED_FIELDS`
# below becomes a boot-time RuntimeError, not a silent "N/A" in the UI.
# ============================================================================

from calculators.market_calendar import previous_trading_day
from calculators.schemas import all_emitted_fields
from calculators.segments import has_volume
from calculators.statistics import TIERS
from calculators.time_blocks import IST, clamp_to_day_start

# Every cache field name the dashboard reads via .get() in the primary consumer
# paths (build_table_data, create_detail_panel, IndexAggregator consumers). Any
# NEW read against a cache-provided field must be added here.
_CONSUMED_FIELDS = frozenset({
    # Identity + timing (all Row classes)
    'asset_id', 'segment_code', 'symbol', 'timestamp',
    # Prices (multiple Rows — not aliased; all calculators report the same
    # live price for the same asset, so "whichever merged last" is fine)
    'current_price', 'open_price', 'previous_close',
    # Today's running day high/low (Momentum only) — feeds the Range column.
    'high_price', 'low_price',
    # Momentum
    'price_after_window', 'gap_return', 'gap_direction', 'early_return',
    'continuation', 'acceleration',
    'gap_z_22d', 'gap_avg_22d', 'gap_std_22d', 'gap_data_points_22d',
    'gap_z_90d', 'gap_avg_90d', 'gap_std_90d', 'gap_data_points_90d',
    'gap_z_180d', 'gap_avg_180d', 'gap_std_180d', 'gap_data_points_180d',
    'gap_z_252d', 'gap_avg_252d', 'gap_std_252d', 'gap_data_points_252d',
    'early_return_z_22d', 'early_return_avg_22d', 'early_return_std_22d', 'early_return_data_points_22d',
    'early_return_z_90d', 'early_return_avg_90d', 'early_return_std_90d', 'early_return_data_points_90d',
    'early_return_z_180d', 'early_return_avg_180d', 'early_return_std_180d', 'early_return_data_points_180d',
    'early_return_z_252d', 'early_return_avg_252d', 'early_return_std_252d', 'early_return_data_points_252d',
    # Breakout — breakout_direction / fresh_breakout / accepted_breakout /
    # failed_breakout are signed ints: +1 up, -1 down, 0 none.
    # breakout_now (outside the range right now) drives the scanning surfaces.
    'range_high', 'range_low', 'range_width', 'breakout_position',
    'fresh_breakout', 'breakout_direction', 'breakout_now', 'accepted_breakout',
    'failed_breakout', 'distance_from_range',
    # VWAP — no bands / band_breach (dropped from the locked design)
    'vwap', 'displacement', 'side', 'acceptance',
    'vwap_z_22d', 'vwap_avg_22d', 'vwap_std_22d', 'vwap_data_points_22d',
    'vwap_z_90d', 'vwap_avg_90d', 'vwap_std_90d', 'vwap_data_points_90d',
    'vwap_z_180d', 'vwap_avg_180d', 'vwap_std_180d', 'vwap_data_points_180d',
    'vwap_z_252d', 'vwap_avg_252d', 'vwap_std_252d', 'vwap_data_points_252d',
    # Relative Strength — session_rs / intraday_rs / acceptance are per-tier,
    # because each is MEASURED with that tier's beta (a tier's beta and its z
    # are one axis — see the tier loop in `run_relative_strength_analysis`,
    # relative_strength_adapter.py). There is deliberately no untiered
    # `session_rs` / `intraday_rs` / `acceptance` any more: an untiered value
    # would have to pick a beta silently, which is the incoherence this fixes.
    #
    # Dropping RS's untiered `acceptance` also resolves a real collision:
    # VWAP also declares `acceptance`, and `multi_emission` merges the first
    # benchmark row flat over the same key. SIGNAL_REGISTRY orders VWAP before
    # RS, so RS's value overwrote VWAP's and the VWAP card's
    # "Widening/Reverting" was showing RS's number (measured: VWAP -0.111,
    # RS +0.999, merged 0.999).
    'benchmark_name', 'benchmark_id',
    'beta_22d', 'beta_90d', 'beta_180d', 'beta_252d',
    'session_rs_22d', 'session_rs_90d', 'session_rs_180d', 'session_rs_252d',
    'intraday_rs_22d', 'intraday_rs_90d', 'intraday_rs_180d', 'intraday_rs_252d',
    'acceptance_22d', 'acceptance_90d', 'acceptance_180d', 'acceptance_252d',
    'session_rs_z_22d', 'session_rs_avg_22d', 'session_rs_std_22d', 'session_rs_data_points_22d',
    'session_rs_z_90d', 'session_rs_avg_90d', 'session_rs_std_90d', 'session_rs_data_points_90d',
    'session_rs_z_180d', 'session_rs_avg_180d', 'session_rs_std_180d', 'session_rs_data_points_180d',
    'session_rs_z_252d', 'session_rs_avg_252d', 'session_rs_std_252d', 'session_rs_data_points_252d',
    'intraday_rs_z_22d', 'intraday_rs_avg_22d', 'intraday_rs_std_22d', 'intraday_rs_data_points_22d',
    'intraday_rs_z_90d', 'intraday_rs_avg_90d', 'intraday_rs_std_90d', 'intraday_rs_data_points_90d',
    'intraday_rs_z_180d', 'intraday_rs_avg_180d', 'intraday_rs_std_180d', 'intraday_rs_data_points_180d',
    'intraday_rs_z_252d', 'intraday_rs_avg_252d', 'intraday_rs_std_252d', 'intraday_rs_data_points_252d',
    # Volume Block (replaces Time Block Volume)
    'time_block', 'block_volume', 'session_cumulative_volume', 'session_cumulative_blocks',
    'block_volume_z_22d', 'block_volume_avg_22d', 'block_volume_std_22d', 'block_volume_data_points_22d',
    'block_volume_z_90d', 'block_volume_avg_90d', 'block_volume_std_90d', 'block_volume_data_points_90d',
    'block_volume_z_180d', 'block_volume_avg_180d', 'block_volume_std_180d', 'block_volume_data_points_180d',
    'block_volume_z_252d', 'block_volume_avg_252d', 'block_volume_std_252d', 'block_volume_data_points_252d',
    'session_cumulative_volume_z_22d', 'session_cumulative_volume_avg_22d', 'session_cumulative_volume_std_22d', 'session_cumulative_volume_data_points_22d',
    'session_cumulative_volume_z_90d', 'session_cumulative_volume_avg_90d', 'session_cumulative_volume_std_90d', 'session_cumulative_volume_data_points_90d',
    'session_cumulative_volume_z_180d', 'session_cumulative_volume_avg_180d', 'session_cumulative_volume_std_180d', 'session_cumulative_volume_data_points_180d',
    'session_cumulative_volume_z_252d', 'session_cumulative_volume_avg_252d', 'session_cumulative_volume_std_252d', 'session_cumulative_volume_data_points_252d',
    # Volume Dynamics — a pure derivative of Volume Block's z, so it tiers
    # exactly as Volume Block does. These are not z-scores themselves, so they
    # carry no avg/std/data_points companions.
    'volume_z_22d', 'volume_z_90d', 'volume_z_180d', 'volume_z_252d',
    'velocity_22d', 'velocity_90d', 'velocity_180d', 'velocity_252d',
    'acceleration_22d', 'acceleration_90d', 'acceleration_180d', 'acceleration_252d',
    'persistence_22d', 'persistence_90d', 'persistence_180d', 'persistence_252d',
    'expansion_state_22d', 'expansion_state_90d', 'expansion_state_180d', 'expansion_state_252d',
    # Price-Volume Divergence
    'pearson_r', 'spearman_r', 'winsorized_r', 'fisher_value', 'sample_size',
    'estimators_available',
    'critical_r', 'reliable', 'estimator_agreement', 'price_volatility',
    'pvd_z_22d', 'pvd_avg_22d', 'pvd_std_22d', 'pvd_data_points_22d',
    'pvd_z_90d', 'pvd_avg_90d', 'pvd_std_90d', 'pvd_data_points_90d',
    'pvd_z_180d', 'pvd_avg_180d', 'pvd_std_180d', 'pvd_data_points_180d',
    'pvd_z_252d', 'pvd_avg_252d', 'pvd_std_252d', 'pvd_data_points_252d',
    # Price Levels (replaces Price Alerts)
    'levels', 'day_events',
    # Rolling Momentum — continuously re-evaluated (unlike momentum's fixed
    # block-1 window). current_price/previous_close already covered above.
    'session_return',
    'session_return_z_22d', 'session_return_avg_22d', 'session_return_std_22d', 'session_return_data_points_22d',
    'session_return_z_90d', 'session_return_avg_90d', 'session_return_std_90d', 'session_return_data_points_90d',
    'session_return_z_180d', 'session_return_avg_180d', 'session_return_std_180d', 'session_return_data_points_180d',
    'session_return_z_252d', 'session_return_avg_252d', 'session_return_std_252d', 'session_return_data_points_252d',
    # COLLIDING_FIELDS renames — every locked Row now carries a `status` field
    # (unlike the old schema, where only 3 did), so every signal gets one.
    # `acceleration` no longer needs an alias: Momentum's is a scalar while
    # Volume Dynamics' is now acceleration_22d/90d/180d/252d, so they no
    # longer collide.
    'momentum_status', 'breakout_status', 'vwap_status',
    'relative_strength_status', 'volume_block_status',
    'volume_dynamics_status',
    'price_volume_divergence_status', 'price_levels_status',
    'rolling_momentum_status',
})

# The dashboard-internal aliases must be recognized as legitimate (they aren't Row fields).
_ALIASES = frozenset({
    'momentum_status', 'breakout_status', 'vwap_status',
    'relative_strength_status', 'volume_block_status',
    'volume_dynamics_status',
    'price_volume_divergence_status', 'price_levels_status',
    'rolling_momentum_status',
})

_UNKNOWN = _CONSUMED_FIELDS - all_emitted_fields() - _ALIASES
if _UNKNOWN:
    raise RuntimeError(
        "dashboard.py consumes fields not declared by any Row dataclass in "
        f"calculators/schemas.py and not in COLLIDING_FIELDS aliases: {sorted(_UNKNOWN)}. "
        "Either add the field to the matching Row class, register it as an alias, "
        "or remove the read."
    )
logger.info(f"Dashboard contract check passed: {len(_CONSUMED_FIELDS)} consumed fields validated")


_ohlc_db = None
def get_ohlc_db():
    global _ohlc_db
    if _ohlc_db is None:
        try:
            from calculators.database_manager import OHLCDatabaseReader
            _ohlc_db = OHLCDatabaseReader('data/ohlc.db')
        except Exception as e:
            logger.error(f"OHLCDatabaseReader init failed: {e}")
    return _ohlc_db


_historical_db = None
def get_historical_db():
    global _historical_db
    if _historical_db is None:
        try:
            from calculators.historical_database_manager import HistoricalDatabaseReader
            _historical_db = HistoricalDatabaseReader('data/historical.db')
        except Exception as e:
            logger.error(f"HistoricalDatabaseReader init failed: {e}")
    return _historical_db


# Cold-start status strip. `historical.db.daily_ohlc` grows one
# row per trading day per asset, but for UX purposes we only need the DATE count
# (identical across assets).
#
# NOT queried at import time, and NOT cached forever. Both of those were wrong:
#
#   1. This module is imported by main.py BEFORE MetricsOrchestrator creates
#      historical.db. A read-only SQLite connection cannot create a missing
#      file, so the import-time read failed, 0 was cached permanently, and the
#      strip read "Historical: 0d" for the life of the container — even though
#      the file appeared 21 ms later. Caching only SUCCESSFUL reads removes the
#      race entirely, without depending on import or statement ordering that an
#      import-sorter is free to rewrite.
#   2. A successful read was also frozen for the life of the process, so the
#      count did not follow a backfill or the 15:34 EOD archive. A short TTL
#      makes it a live figure instead of a boot-time snapshot.
_HISTORICAL_DAYS_CACHE = None
_HISTORICAL_DAYS_CACHED_AT = 0.0
_HISTORICAL_DAYS_TTL_SECONDS = 300


def get_historical_days_count() -> int:
    """Number of distinct trading dates in historical.db.daily_ohlc.

    Trading days, not calendar days: every writer of that table is gated on
    the trading calendar (the archive refuses weekends and listed holidays, the
    morning step and the backfill write to `previous_trading_day`). A day the
    app did not run but whose close was filled the next morning counts too.

    Cached for _HISTORICAL_DAYS_TTL_SECONDS. A FAILED read is never cached —
    the usual failure is "the file does not exist yet", which resolves on its
    own within milliseconds of startup. Returns 0 on any error (safe default:
    the strip just shows 'Daily history: 0 trading days').
    """
    global _HISTORICAL_DAYS_CACHE, _HISTORICAL_DAYS_CACHED_AT
    now = time.monotonic()
    if (_HISTORICAL_DAYS_CACHE is not None
            and now - _HISTORICAL_DAYS_CACHED_AT < _HISTORICAL_DAYS_TTL_SECONDS):
        return _HISTORICAL_DAYS_CACHE
    try:
        db = get_historical_db()
        if db is None:
            return 0                      # not cached — may be ready next call
        with db._get_connection() as conn:
            row = conn.execute("SELECT COUNT(DISTINCT date) FROM daily_ohlc").fetchone()
        _HISTORICAL_DAYS_CACHE = int(row[0]) if row and row[0] is not None else 0
        _HISTORICAL_DAYS_CACHED_AT = now
    except Exception as e:
        logger.warning(f"get_historical_days_count query failed: {e}")
        return 0                          # not cached — retried on next call
    return _HISTORICAL_DAYS_CACHE


#: Tier pill values, as the `{N}d` suffix strings every raw_ field uses.
_TIER_VALUES = tuple(f'{t}d' for t in TIERS)


def default_active_tier() -> str:
    """The longest tier the SYSTEM AS A WHOLE has data for.

    Global, not per-row and not per-signal: one answer for the whole page,
    computed once at layout construction from the same `historical.db` day
    count the cold-start strip reads.

    A fresh install therefore opens on 22d rather than on four columns of
    hyphens, and a mature one opens on 252d. Below 22 days nothing has a tier
    yet, so it opens on 22d — the first that will arrive.

    This picks only the DEFAULT. All four pills stay selectable at all times;
    a signal with no data for the selected tier renders a hyphen, which is
    both the truth and the diagnostic — a column of hyphens at 252d says
    exactly which signals have not yet accumulated a year of history.
    """
    n_days = get_historical_days_count()
    best = TIERS[0]
    for tier in TIERS:
        if n_days >= tier:
            best = tier
    return f'{best}d'


def build_cold_start_strip():
    """Muted status strip below the header: the number of trading days in
    historical.db's daily archive (`daily_ohlc`), and nothing more.

    It makes no claim about which z-tiers are ready. Most z-scores draw on
    other tables — the block volumes, the per-block snapshots — each with its
    own depth, so a `daily_ohlc` count cannot say that. Hidden once the count
    reaches 252 (an empty html.Div renders nothing)."""
    n_days = get_historical_days_count()
    if n_days >= 252:
        return html.Div()

    return html.Div(
        f"Daily history: {n_days} trading day{'' if n_days == 1 else 's'}",
        style={
            'padding': '6px 22px 8px 22px',
            'backgroundColor': '#1a1410',
            'color': '#7a6e62',
            'fontSize': '11px',
            'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
            'letterSpacing': '0.5px',
            'borderBottom': '1px solid #2f2820',
        },
    )


def get_today_ohlc(asset_id: int, segment_code: str, hours: int = 8):
    """Query recent OHLC data from ohlc.db for the price chart.

    Returns `(df, is_fallback, fallback_date)`:
      - df: pandas DataFrame of OHLC rows.
      - is_fallback: True when today has no data and we fell back to the
        most-recent-available trading session.
      - fallback_date: the date of the fallback session (Python `date` object),
        or None on the live path.

    Fallback path: when the primary "last `hours` from now"
    query returns nothing (pre-market, weekend, or the engine simply hasn't
    run today), we query for the last `hours` of ticks ending at
    `MAX(timestamp)` for this asset. `ohlc.db` retains the last 5 trading
    days per the daily prune, so this shows yesterday's session at worst.
    """
    db = get_ohlc_db()
    if not db:
        return pd.DataFrame(), False, None
    try:
        now = datetime.now(IST)
        cutoff = clamp_to_day_start(now - timedelta(hours=hours), now).isoformat()
        with db.get_read_connection() as conn:
            df = pd.read_sql_query("""
                SELECT timestamp, last_price, open_price, high_price, low_price,
                       close_price, interval_volume
                FROM ohlc_data
                WHERE asset_id = ? AND segment_code = ? AND timestamp >= ?
                ORDER BY timestamp ASC
            """, conn, params=(asset_id, segment_code, cutoff))
            if not df.empty:
                return df, False, None

            # Fallback: find MAX(timestamp) for this asset, replay the last
            # `hours` window ending at that point. If asset has no rows at
            # all, return empty.
            row = conn.execute(
                "SELECT MAX(timestamp) FROM ohlc_data WHERE asset_id = ? AND segment_code = ?",
                (asset_id, segment_code),
            ).fetchone()
            if not row or not row[0]:
                return df, False, None

            max_ts = row[0]
            try:
                max_ts_dt = datetime.fromisoformat(max_ts)
                fallback_cutoff = clamp_to_day_start(
                    max_ts_dt - timedelta(hours=hours), max_ts_dt
                ).isoformat()
            except (ValueError, TypeError):
                fallback_cutoff = None
            if fallback_cutoff is None:
                return df, False, None

            df = pd.read_sql_query("""
                SELECT timestamp, last_price, open_price, high_price, low_price,
                       close_price, interval_volume
                FROM ohlc_data
                WHERE asset_id = ? AND segment_code = ?
                  AND timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp ASC
            """, conn, params=(asset_id, segment_code, fallback_cutoff, max_ts))

            fallback_date = None
            if not df.empty:
                try:
                    fallback_date = pd.to_datetime(df['timestamp'].iloc[-1]).date()
                except Exception:
                    fallback_date = None
            return df, True, fallback_date
    except Exception as e:
        logger.error(f"Error querying ohlc for price chart: {e}")
        return pd.DataFrame(), False, None


# Chart layout constants + line-trace helpers live in dashboard_signals.py so
# per-signal chart builders can compose them without a circular
# import back into dashboard.py. Imported here for the composite price chart
# (which crosses signals and stays inline in build_charts).
from dashboard_signals import (
    CHART_LAYOUT, _padded_xrange, _make_signed_line_traces,
    _Z_HIGHLIGHT,
    _row_divergence,
)


# Index-card local layout helpers (detail-panel primitives no longer apply
# here — index cards live in a different drawer with different width
# constraints, so use a plain label|value 2-col grid).
_INDEX_KV_ROW_STYLE = {
    'display': 'grid',
    'gridTemplateColumns': '160px minmax(0, 1fr)',
    'columnGap': '10px',
    'alignItems': 'baseline',
    'padding': '3px 0',
    'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
    'fontSize': '13px',
    'color': '#f5e9d3',
}
_INDEX_KV_LABEL_STYLE = {'color': '#8a7864'}
_INDEX_KV_VALUE_STYLE = {'textAlign': 'right', 'fontVariantNumeric': 'tabular-nums'}
_INDEX_SUBSECTION_HEADER_STYLE = {
    'fontSize': '10px',
    'letterSpacing': '1.4px',
    'textTransform': 'uppercase',
    'color': '#8a7864',
    'marginTop': '14px',
    'marginBottom': '6px',
    'paddingBottom': '3px',
    'borderBottom': '1px dashed #3a3128',
    'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
}


def _kv_row(label, value):
    return html.Div([
        html.Div(label, style=_INDEX_KV_LABEL_STYLE),
        html.Div(value, style=_INDEX_KV_VALUE_STYLE),
    ], style=_INDEX_KV_ROW_STYLE)


def _subsection(title, children):
    return html.Div([
        html.Div(title, style=_INDEX_SUBSECTION_HEADER_STYLE),
        *children,
    ], style={'marginBottom': '8px'})
from plotly.subplots import make_subplots


# CSV price levels overlay on the composite price chart.
# Cached at first-lookup because assets_select.csv is small (~86 rows) and
# static across a run. Returns None for indices or assets without levels.
_CSV_LEVELS_CACHE = None


def _load_csv_levels() -> dict:
    """Return {(asset_id, segment_code): {local_high, global_high, local_low, global_low}}
    parsed from assets_select.csv. Cached at module scope."""
    global _CSV_LEVELS_CACHE
    if _CSV_LEVELS_CACHE is not None:
        return _CSV_LEVELS_CACHE
    _CSV_LEVELS_CACHE = {}
    try:
        import csv
        with open(ASSETS_CSV) as f:
            for row in csv.DictReader(f):
                try:
                    key = (int(row['asset_id']), row['segment_code'])
                except (ValueError, KeyError):
                    continue
                levels = {}
                for name in ('local_high', 'global_high', 'local_low', 'global_low'):
                    raw = row.get(name, '').strip()
                    if raw:
                        try:
                            levels[name] = float(raw)
                        except ValueError:
                            pass
                if levels:
                    _CSV_LEVELS_CACHE[key] = levels
    except Exception as e:
        logger.warning(f"CSV levels load failed: {e}")
    return _CSV_LEVELS_CACHE


def get_csv_levels(asset_id: int, segment_code: str) -> dict:
    """Levels dict for one asset (empty dict if none defined). Only stocks have
    CSV levels; indices' CSV rows leave the level columns blank."""
    return _load_csv_levels().get((int(asset_id), segment_code), {})


# Composite price chart's own fixed view window — deliberately independent of
# the Settings chart-window pill (which still drives the other 8 charts via
# `_padded_xrange` in dashboard_signals.py). Keeping candle width and vertical
# scale stable all day requires a window that never changes shape.
_PRICE_CHART_TOTAL_HOURS = 3.0   # total fixed x-range width, margin included
_PRICE_CHART_RIGHT_MARGIN = 0.20  # fraction of the total width left blank on the right
_PRICE_CHART_Y_FLOOR = 0.75       # candle body always occupies exactly this fraction of height


def _price_chart_xrange(t_now):
    """Fixed-width rolling window for the composite price chart.

    The newest candle is always anchored at `1 - _PRICE_CHART_RIGHT_MARGIN`
    across the total width (a permanent ~20% blank margin on the right),
    applied uniformly regardless of time of day — no early-session special
    case. Early in the session this naturally means most of the window is
    blank pre-market space to the left of the few real candles, which still
    sit at that same fixed anchor point. Returns (t_min, t_right_edge).
    """
    core_hours = _PRICE_CHART_TOTAL_HOURS * (1 - _PRICE_CHART_RIGHT_MARGIN)
    margin_hours = _PRICE_CHART_TOTAL_HOURS * _PRICE_CHART_RIGHT_MARGIN
    t_min = t_now - timedelta(hours=core_hours)
    t_right_edge = t_now + timedelta(hours=margin_hours)
    return t_min, t_right_edge


def build_charts(asset_id, segment_code, show_atr=False, resample='30s', window_hours: int = 2,
                 rs_benchmark: Optional[str] = None, active_tier: Optional[str] = None,
                 z_threshold: Optional[float] = None):
    """
    Query metrics.db and build 9 Plotly figures for the selected asset.

    Returns list of 9 go.Figure objects:
    [price, velocity, acceleration, vwap_displacement, early_return,
     relative_strength, volume_block_z_scores, divergence, rolling_momentum]

    `window_hours`, from the settings drawer's chart-window pills, drives both
    the history-query bound (`get_asset_metrics_history(hours=...)`) and the
    per-chart x-range via `_padded_xrange(..., window_hours=...)`. The widest
    pill, "1d" (24h), is passed to the builders as None, which they hand
    through to `_padded_xrange` as "the full returned range" — the
    single-trading-day clamp inside the history-query functions already caps
    the data at one day, so that range is the whole session.
    """
    db = get_metrics_db()
    _chart_names = ['Price', 'Velocity', 'Acceleration', 'VWAP Displacement %',
                    'Early Return', 'Relative Strength', 'Volume Block Z-Scores',
                    'Price-Volume Divergence', 'Rolling Momentum Z-Scores']
    figures = [
        go.Figure(layout={**CHART_LAYOUT, 'title': dict(text=f'{name} — No data available', font=dict(size=13))})
        for name in _chart_names
    ]

    if not db:
        return figures

    # Bound history-query hours by the selected window. The widest pill ("1d"
    # = 24h) is always clamped down to the single trading day by
    # `clamp_to_day_start` inside the history-query functions themselves, so
    # no further capping is needed here.
    query_hours = max(int(window_hours), 1)
    _xrange_window = None if window_hours >= 24 else window_hours

    try:
        history, is_metrics_fallback, metrics_fallback_ts = db.get_asset_metrics_history(
            asset_id, segment_code=segment_code, hours=query_hours)
    except Exception as e:
        logger.error(f"Error querying metrics history: {e}")
        return figures

    # 0. Price line chart with VWAP, EMA, ATR overlays (index 0)
    # Every asset gets the same 2-row subplot (price on row 1, volume bars on
    # row 2) so the candlestick area is always the same proportion of the
    # chart regardless of what's selected. Indices don't get volume bars —
    # they are not tradeable and have NO VOLUME, so the engine stores NULL
    # (never 0) for their volume columns and writes no time_block_volumes row
    # at all. Row 2 is simply left empty for them rather than reshaping the
    # whole figure.
    #
    # NB this comment previously claimed "engine writes 0 for interval_volume
    # on IDX_I". No such mechanism existed — the engine stored whatever the
    # quote endpoint returned. The dashboard believed it was filtered and it
    # was not, and the two disagreed silently for as long as the comment stood.
    # The rule now lives in calculators/segments.py (NON_VOLUME_SEGMENTS).
    df, _price_is_fallback, _price_fallback_date = get_today_ohlc(asset_id, segment_code)
    if not df.empty:
        df['timestamp'] = pd.to_datetime(df['timestamp'], format='ISO8601')
        _is_stock = has_volume(segment_code)
        # The session this chart shows: today's, or — when there are no
        # readings today — the last recorded session (get_today_ohlc returns
        # exactly one day). The opening-range box, the PrevClose line and the
        # ATR band all belong to THIS session, not to the calendar's today.
        session_date = df['timestamp'].iloc[-1].date()

        # Resample 5-sec snapshots into 5-min OHLC candles.
        # `df` (5-sec granularity) is still used for EMA / VWAP / y-clamp
        # touchpoints below; `ohlc_5m` drives the candlestick trace + volume
        # subplot. Two frames coexist on the same shared x-axis; Plotly maps
        # each trace at its native timestamps.
        #
        # Built from `last_price`, not `open_price`/`high_price`/`low_price`/
        # `close_price` — those four are Dhan's quote-level session OHLC
        # (today's session open, running day high/low, and *yesterday's*
        # close), not per-bar tick data, so aggregating them per 5-min bucket
        # just re-reports the same slowly-changing/frozen numbers in every
        # bucket. `last_price` is the only field that actually moves tick to
        # tick, so it's the only valid source for real intrabar candles.
        try:
            ohlc_5m = (
                df.set_index('timestamp')
                  .resample('5min')
                  .agg(open_price=('last_price', 'first'),
                       high_price=('last_price', 'max'),
                       low_price=('last_price', 'min'),
                       close_price=('last_price', 'last'),
                       interval_volume=('interval_volume', 'sum'))
                  .dropna(how='all')
                  .reset_index()
            )
        except Exception as e:
            logger.error(f"5-min resample failed, falling back to 5-sec close: {e}",
                         exc_info=True)
            ohlc_5m = pd.DataFrame(columns=['timestamp', 'open_price', 'high_price',
                                             'low_price', 'close_price', 'interval_volume'])

        fig = make_subplots(
            rows=2, cols=1, shared_xaxes=True,
            row_heights=[0.72, 0.28], vertical_spacing=0.03,
        )

        # All overlays render on row=1 (price panel); volume bars on row=2.
        # Price trace is 5-min candlesticks. Falls back to a
        # 5-sec close-price line only if the resample produced no rows.
        if not ohlc_5m.empty:
            fig.add_trace(go.Candlestick(
                x=ohlc_5m['timestamp'],
                open=ohlc_5m['open_price'], high=ohlc_5m['high_price'],
                low=ohlc_5m['low_price'],   close=ohlc_5m['close_price'],
                increasing_line_color='#3ddc84',
                decreasing_line_color='#ff5670',
                increasing_fillcolor='#3ddc84',
                decreasing_fillcolor='#ff5670',
                name='Price', showlegend=True,
            ), row=1, col=1)
            # Candlesticks have a built-in rangeslider; hide it since this
            # chart shows its own fixed 3-hour window (see _price_chart_xrange).
            fig.update_xaxes(rangeslider_visible=False, row=1, col=1)
        else:
            fig.add_trace(go.Scatter(
                x=df['timestamp'], y=df['last_price'],
                mode='lines', name='Price',
                line=dict(color='#f5e9d3', width=2)
            ), row=1, col=1)

        # Opening-range shaded rectangle (09:15–09:45 IST) on price panel.
        # Anchors the breakout narrative visually. Stocks only (indices have no
        # meaningful opening-range analytic). On the SHOWN session's date: drawn
        # on today's, a previous session's chart had no box at all.
        if _is_stock:
            try:
                or_start = datetime.combine(session_date, datetime.min.time().replace(hour=9, minute=15))
                or_end = datetime.combine(session_date, datetime.min.time().replace(hour=9, minute=45))
                fig.add_vrect(
                    x0=or_start, x1=or_end,
                    fillcolor='rgba(184, 169, 155, 0.05)', line_width=0,
                    layer='below', row=1, col=1,
                )
            except Exception as e:
                logger.error(f"OR overlay error: {e}", exc_info=True)

        # Day high / low horizontal reference lines (computed from today's OHLC).
        try:
            day_high = df['high_price'].max()
            day_low = df['low_price'].min()
            if pd.notna(day_high) and pd.notna(day_low):
                fig.add_hline(
                    y=float(day_high), line_dash='dash', line_color='#5fb87c',
                    line_width=1, opacity=0.5,
                    annotation_text='H', annotation_position='right',
                    row=1, col=1,
                )
                fig.add_hline(
                    y=float(day_low), line_dash='dash', line_color='#d94560',
                    line_width=1, opacity=0.5,
                    annotation_text='L', annotation_position='right',
                    row=1, col=1,
                )
        except Exception as e:
            logger.error(f"Day H/L overlay error: {e}", exc_info=True)

        # CSV price levels (local/global high/low from assets_select.csv).
        # Stocks only; indices' CSV rows have blank levels.
        # Rendered as go.Scatter traces (not add_hline) so their labels
        # live in the chart legend, not on the y-axis. Keeps the left-of-axis
        # area clean — only price tick labels there.
        if _is_stock:
            try:
                levels = get_csv_levels(asset_id, segment_code)
                _level_style = [
                    ('local_high',  '#7a9cc4', 'dash',    'LH'),
                    ('global_high', '#7a9cc4', 'dashdot', 'GH'),
                    ('local_low',   '#e8a447', 'dash',    'LL'),
                    ('global_low',  '#e8a447', 'dashdot', 'GL'),
                ]
                _x_endpoints = [df['timestamp'].iloc[0], df['timestamp'].iloc[-1]]
                for name, color, dash, label in _level_style:
                    val = levels.get(name)
                    if val is not None:
                        fig.add_trace(go.Scatter(
                            x=_x_endpoints,
                            y=[float(val), float(val)],
                            mode='lines',
                            name=f'{label} · ₹{float(val):.2f}',
                            line=dict(color=color, dash=dash, width=1),
                            opacity=0.6,
                            showlegend=True,
                            hoverinfo='name',
                        ), row=1, col=1)
            except Exception as e:
                logger.error(f"CSV levels overlay error: {e}", exc_info=True)

        # VWAP overlay — line only. σ-bands dropped (not part of the locked
        # VWAP design; see VwapRow in calculators/schemas.py).
        try:
            vwap_df = history.get('vwap')
            if vwap_df is not None and not vwap_df.empty and 'vwap' in vwap_df.columns:
                vwap_df = vwap_df.sort_values('timestamp')
                vwap_df['timestamp'] = pd.to_datetime(vwap_df['timestamp'], format='ISO8601')
                fig.add_trace(go.Scatter(
                    x=vwap_df['timestamp'], y=vwap_df['vwap'],
                    mode='lines', name='VWAP',
                    line=dict(color='#d94560', width=1.2), opacity=0.85
                ), row=1, col=1)
        except Exception as e:
            logger.error(f"VWAP overlay error: {e}")

        # PrevClose reference line — the close of the trading day BEFORE the
        # shown session (weekends and listed holidays skipped). Not the
        # archive's newest row: that is wrong for a previous session, and
        # after 15:34 it is the shown day's own row, archived without a close,
        # so the line vanished every evening. Also the ATR band's centre below.
        prev_close = None
        try:
            hist_db = get_historical_db()
            if hist_db:
                prev_close = hist_db.get_close_on(
                    asset_id, segment_code, previous_trading_day(session_date))
                if prev_close is not None:
                    fig.add_hline(
                        y=prev_close, line_dash='dot', line_color='#a58fa8',
                        line_width=1, opacity=0.7,
                        annotation_text='PrevClose', annotation_position='right',
                        row=1, col=1,
                    )
        except Exception as e:
            logger.error(f"prev_close reference line error: {e}")

        # EMA overlays — spans centralized at module top (_EMA_SPAN_* constants).
        if len(df) > 10:
            try:
                ema_configs = [
                    (_EMA_SPAN_5MIN,  'EMA-5m',  '#5fb87c'),
                    (_EMA_SPAN_15MIN, 'EMA-15m', '#7a9cc4'),
                    (_EMA_SPAN_30MIN, 'EMA-30m', '#e8a447'),
                ]
                # Time-based EWM: `span` is expressed in 5-sec bars. For a span-N EWM
                # under uniform sampling, α = 2/(N+1); halflife in bars is exactly
                # ln(0.5) / ln(1 - α) ≈ N × ln(2)/2 (≈ 0.347·N for large N).
                # Convert to seconds: `halflife_seconds = span * 5 * ln(2) / 2`.
                # An earlier version used `span * 5 / 2` here,
                # which is 1/ln(2) ≈ 1.44× too smooth — every EMA effectively longer
                # than its label. Correct math restored below.
                # Using time-based EWM keeps the smoothing consistent across gaps in
                # the intraday feed (holidays, halts, missed 5-sec fetches).
                # pandas ≥ 2.0 requires `adjust=True` (the default) when `times=` is
                # supplied — the recursive `adjust=False` form applies a fixed α per
                # step and cannot handle non-uniform time gaps, so pandas rejects it.
                # `adjust=True` is also the mathematically correct choice for
                # irregular sampling: weights decay by elapsed wall-time, not step
                # count. Passing `adjust=False` here previously raised
                # `NotImplementedError: times is not supported with adjust=False.`
                ts_series = pd.to_datetime(df['timestamp'], format='ISO8601')
                for span, name, color in ema_configs:
                    # Intended early-session gate. Higher-span EMAs progressively
                    # appear as enough 5-sec bars accumulate — EMA-30m needs ~7.5 min
                    # of data before rendering. Not a bug; matches classical EMA
                    # settling behavior. If EMAs never appear intraday, check the
                    # `logger.error` line below with exc_info=True traceback.
                    if len(df) >= max(span // 4, 5):
                        halflife = pd.Timedelta(seconds=(span * 5) * math.log(2) / 2)
                        ema = df['last_price'].ewm(
                            halflife=halflife, times=ts_series
                        ).mean()
                        fig.add_trace(go.Scatter(
                            x=df['timestamp'], y=ema,
                            mode='lines', name=name,
                            line=dict(color=color, width=1), opacity=0.75
                        ), row=1, col=1)
            except Exception as e:
                # exc_info=True: capture the full traceback so live-market runs
                # surface the root cause (e.g. non-monotonic timestamps, tz mismatch)
                # instead of a bare one-line message.
                logger.error(f"EMA overlay error: {e}", exc_info=True)

        # ATR overlay (toggled) — the band as it stood during the SHOWN
        # session: centred on that session's PrevClose (above), falling back to
        # its open when there is none (new asset, thin historical.db), and as
        # wide as the ATR of the 14 archived days BEFORE it. After 15:34 the
        # shown day is already archived, and counting it would widen the band
        # by a range the session did not have while it traded.
        if show_atr:
            try:
                hist_db = get_historical_db()
                if hist_db:
                    # ATR period centralized at module top (_ATR_PERIOD).
                    atr_val = hist_db.get_atr(asset_id, segment_code, before=session_date,
                                              period=_ATR_PERIOD)
                    if atr_val is not None:
                        anchor_val = prev_close
                        anchor_label = 'PrevClose'

                        if anchor_val is None:
                            anchor_val = float(df.iloc[0]['open_price'])
                            anchor_label = 'Open'
                            logger.warning(
                                f"ATR fallback to Open anchor for asset {asset_id}/{segment_code} "
                                f"(prev_close unavailable)"
                            )

                        upper = anchor_val + atr_val
                        lower = anchor_val - atr_val
                        x_range = [df['timestamp'].iloc[0], df['timestamp'].iloc[-1]]
                        for y_val, atr_name in [
                            (upper, f'{anchor_label}+ATR({_ATR_PERIOD}d)'),
                            (lower, f'{anchor_label}-ATR({_ATR_PERIOD}d)'),
                        ]:
                            fig.add_trace(go.Scatter(
                                x=x_range, y=[y_val, y_val],
                                mode='lines', name=atr_name,
                                line=dict(color='#7a9cc4', width=1, dash='dashdot'),
                                opacity=0.8
                            ), row=1, col=1)
            except Exception as e:
                logger.error(f"ATR overlay error: {e}")

        # Volume bars (tradeable segments only) — row 2 always exists in the
        # subplot, but only instruments that HAVE volume get bars drawn into it.
        # Indices are not tradeable, so the engine stores NULL for their volume
        # columns and they leave row 2 blank. Histogram of interval_volume,
        # sign-coloured by price direction (green when last_price ≥ prev bar's
        # last_price, red otherwise).
        if _is_stock:
            try:
                # Volume bars are aggregated to 5-min buckets (one bar
                # per candlestick), sign-coloured by candle direction rather
                # than per-tick direction. ~24 bars per 2h window vs ~1440
                # previously — legible, aligned to candles.
                if not ohlc_5m.empty:
                    vol_colors = [
                        '#3ddc84' if c >= o else '#ff5670'
                        for o, c in zip(ohlc_5m['open_price'], ohlc_5m['close_price'])
                    ]
                    fig.add_trace(
                        go.Bar(
                            x=ohlc_5m['timestamp'], y=ohlc_5m['interval_volume'],
                            name='Volume',
                            marker=dict(color=vol_colors),
                            opacity=0.9,
                            showlegend=False,
                        ),
                        row=2, col=1,
                    )
                    fig.update_yaxes(title_text='Volume', row=2, col=1,
                                     gridcolor='#2f2820',
                                     tickfont=dict(color='#b8a99b', size=10))
            except Exception as e:
                logger.error(f"Volume subplot error: {e}", exc_info=True)

        # Fixed x-range for this chart (see _price_chart_xrange) — computed
        # once here so both the y-clamp below and the axis-range calls
        # further down use the same window.
        t_now = df['timestamp'].max()
        t_min, t_right_edge = _price_chart_xrange(t_now)

        # Y-axis scale: the candle body always occupies exactly
        # _PRICE_CHART_Y_FLOOR (75%) of the visible height, computed only
        # from candles inside the visible core window [t_min, t_now] — not
        # the whole fetched day. Overlays (VWAP / EMA / ATR / day H-L /
        # CSV levels) that fall outside the resulting range simply aren't
        # visible by default; they're still real traces, reachable by
        # panning or zooming.
        try:
            core_candles = ohlc_5m[(ohlc_5m['timestamp'] >= t_min) & (ohlc_5m['timestamp'] <= t_now)]
            if not core_candles.empty:
                price_min = float(core_candles['low_price'].min())
                price_max = float(core_candles['high_price'].max())
            else:
                core_ticks = df[(df['timestamp'] >= t_min) & (df['timestamp'] <= t_now)]
                price_min = float(core_ticks['last_price'].min())
                price_max = float(core_ticks['last_price'].max())
            price_span = max(price_max - price_min, abs(price_max) * 0.001)
            pad = price_span * (1 - _PRICE_CHART_Y_FLOOR) / (2 * _PRICE_CHART_Y_FLOOR)
            fig.update_yaxes(range=[price_min - pad, price_max + pad], row=1, col=1)
        except Exception as e:
            logger.warning(f"Price chart y-clamp skipped: {e}")

        # Fallback path: annotate title with the session date so the trader
        # never mistakes yesterday's ticks for today's.
        if _price_is_fallback and _price_fallback_date is not None:
            _price_title = f"Price — Last session: {_price_fallback_date.strftime('%Y-%m-%d')}"
        else:
            _price_title = 'Price'
        # Explicit outside-plot legend + wider right margin so all
        # traces (VWAP, EMAs, CSV levels, PrevClose, ATR) fit and
        # read without smushing.
        fig.update_layout(**{
            **CHART_LAYOUT,
            'height': 560,
            'title': dict(text=_price_title, font=dict(size=13)),
            'margin': dict(l=60, r=180, t=40, b=40),
            'legend': dict(
                x=1.02, y=1, xanchor='left', yanchor='top',
                bgcolor='rgba(35,27,21,0.85)',
                font=dict(size=12),
                itemwidth=80,
            ),
        })
        # Fixed 3h window (t_min/t_right_edge computed above) — independent
        # of the Settings chart-window pill, which only drives the other 8
        # charts. Applied to both rows so they stay aligned.
        fig.update_xaxes(range=[t_min, t_right_edge], row=1, col=1)
        fig.update_xaxes(range=[t_min, t_right_edge], row=2, col=1)
        # Strip the date from tick labels on both rows — matches the index
        # mini-charts' pattern; staleness already surfaces in the header.
        fig.update_xaxes(tickformat='%H:%M')
        figures[0] = fig

    # Signal-owned charts: iterate SIGNAL_REGISTRY and populate the
    # 8 non-price figures via each signal's chart_builders. Each entry is
    # {chart_id: (history_key, builder_fn)}. Composite price chart above stays
    # inline because it crosses signals (VWAP + EMA + ATR + PrevClose).
    # builder_fn receives window_hours so its _padded_xrange respects the
    # chart-window pill.
    from dashboard_signals import SIGNAL_REGISTRY, CHART_OUTPUT_ORDER
    _chart_index = {cid: i for i, cid in enumerate(CHART_OUTPUT_ORDER)}
    for signal in SIGNAL_REGISTRY.values():
        for chart_id, (history_key, builder_fn) in signal.chart_builders.items():
            history_slice = history.get(history_key)
            try:
                fig = builder_fn(
                    history_slice,
                    resample=resample,
                    window_hours=_xrange_window,
                    rs_benchmark=rs_benchmark,
                    # The tier charts keep plotting all four lines —
                    # they are a divergence surface — but emphasise the selected
                    # one so the chart is tied to the number that prompted it.
                    active_tier=active_tier,
                    # The z-slider: where the z-charts' "unusual" shading starts.
                    z_threshold=z_threshold,
                )
            # Every builder takes **_ignore, so an argument it does not use can
            # never fail here. There is deliberately no retry with fewer
            # arguments: it only ever caught a TypeError raised INSIDE a builder
            # and redrew the chart without the window, benchmark and tier.
            except Exception as e:
                logger.error(f"Error building {chart_id} for {asset_id}/{segment_code}: {e}")
                continue
            idx = _chart_index.get(chart_id)
            if idx is not None:
                figures[idx] = fig

    # Annotate non-price chart titles when we fell back to the last
    # available session. Price chart already annotates itself via
    # `_price_is_fallback` / `_price_fallback_date` earlier in this function.
    if is_metrics_fallback and metrics_fallback_ts is not None:
        _fallback_prefix = f"Last session: {metrics_fallback_ts.strftime('%Y-%m-%d')} · "
        for _f in figures[1:]:
            try:
                _cur_title = _f.layout.title.text or ''
                _f.update_layout(title=dict(
                    text=_fallback_prefix + _cur_title,
                    font=dict(size=13),
                ))
            except Exception:
                pass

    return figures


def format_metric_value(value, metric_type):
    """
    Format metric value and determine color coding.

    Args:
        value: The metric value
        metric_type: 'momentum' or 'deviation' — the only two that exist.

    Returns:
        Tuple of (formatted_string, color)

    Branches for 'velocity', 'acceleration', 'rel_strength' and 'price' were
    deleted: nothing called them. That is not merely tidiness — the 'velocity'
    branch had genuinely WRONG semantics, formatting a signed sigma quantity as
    a multiplier (green above 1.0, red below 0.5, so a velocity of 0 rendered
    red), and nobody noticed precisely because it was unreachable. The next
    person needing velocity formatting would have found it and used it in good
    faith. Write the formatter when there is a surface to put it on, against a
    real requirement.
    """
    if value is None or pd.isna(value):
        return ('-', '#7a6e62')

    try:
        if metric_type == 'momentum':
            val = float(value)
            if val > 2.0:
                return (f"{val:+.2f}%", '#5fb87c')  # Forest
            elif val > 0.5:
                return (f"{val:+.2f}%", '#5fb87c')  # Muted green
            elif val < -2.0:
                return (f"{val:+.2f}%", '#d94560')  # Crimson
            elif val < -0.5:
                return (f"{val:+.2f}%", '#d94560')  # Muted crimson
            else:
                return (f"{val:+.2f}%", '#b8a99b')  # Ink-soft

        elif metric_type == 'deviation':
            val = float(value)
            if abs(val) > 2.0:
                return (f"{val:+.2f}%", '#e8a447')  # Warm amber
            elif abs(val) > 1.0:
                return (f"{val:+.2f}%", '#5fb87c')
            else:
                return (f"{val:+.2f}%", '#b8a99b')

        else:
            return (str(value), '#b8a99b')

    except (ValueError, TypeError):
        return (str(value), '#b8a99b')


def get_current_metrics():
    """Get latest metrics from shared cache (in-memory)."""
    try:
        from metrics import get_latest_metrics, get_hydration_timestamp

        # Get cached metrics
        metrics = get_latest_metrics()

        # Accept hydrated-from-disk state as valid data too.
        # LATEST_METRICS['timestamp'] is only set by a live cycle; hydration
        # signals presence via HYDRATION_TIMESTAMP. Both null → truly no data.
        if not metrics or (not metrics.get('timestamp') and get_hydration_timestamp() is None):
            logger.warning("No metrics available in cache yet")
            return []

        # Merge all calculator results per (asset_id, segment_code) into a list of dicts.
        #   - No DataFrame roundtrip. The DataFrame layer used to fill missing per-row
        #     keys with NaN — the root of a PV Divergence merge gap and of crashes from
        #     iterating a NaN where a list was expected. Iterating dicts directly, `.get(key,
        #     default)` behaves the way it looks.
        #   - No hardcoded calculator-name list. We iterate every key in LATEST_METRICS
        #     whose value is a list-of-dicts. Adding a new calculator becomes zero-touch
        #     here; a calculator left out of a hard-coded key list (how PV Divergence
        #     once went missing from the merge) can no longer happen.
        # Merge driver: iterate every list-of-dicts key in LATEST_METRICS,
        # look up its Signal to derive per-signal behavior (field renames, multi-emission
        # collection into `all_benchmarks`). Unregistered keys still merge with default
        # behavior — a safety net that also logs so orphan calculator sections don't go
        # unnoticed.
        from dashboard_signals import SIGNAL_REGISTRY, signal_for

        merged_data = {}

        # Non-calculator keys that live in the same LATEST_METRICS dict but must be
        # skipped by the auto-discovery merge.
        _NON_CALC_KEYS = {'timestamp', 'cycle_number'}

        for key, items in metrics.items():
            if key in _NON_CALC_KEYS or not isinstance(items, list):
                continue
            sig = signal_for(key)
            if sig is None:
                logger.warning(
                    f"LATEST_METRICS contains unregistered calculator key '{key}' — "
                    f"merging with default behavior. Register a Signal in dashboard_signals.py."
                )
            for item in items:
                if not isinstance(item, dict):
                    continue  # defensive: the cache holds dicts (rows are serialized where they are stored)
                asset_id = item.get('asset_id')
                segment_code = item.get('segment_code', '')
                composite_key = (asset_id, segment_code)
                if composite_key not in merged_data:
                    merged_data[composite_key] = {'asset_id': asset_id, 'segment_code': segment_code}
                renames = sig.field_renames if sig else {}
                # Multi-emission calculators (currently: RS) collect each row into a list.
                # The first row also populates the top level for row-level display; subsequent
                # rows only append to the list. field_renames applies here too — every Row
                # now carries a `status` field, multi-emission or not.
                if sig is not None and sig.multi_emission:
                    list_key = 'all_benchmarks'  # RS-specific; generalize when a second multi-emission signal lands.
                    if list_key not in merged_data[composite_key]:
                        merged_data[composite_key][list_key] = []
                        if renames:
                            item_copy = dict(item)
                            for old_name, new_name in renames.items():
                                if old_name in item_copy:
                                    item_copy[new_name] = item_copy.pop(old_name)
                            merged_data[composite_key].update(item_copy)
                        else:
                            merged_data[composite_key].update(item)
                    merged_data[composite_key][list_key].append(item)
                    continue
                # Apply per-signal field renames (derived from Signal.field_renames).
                if renames:
                    item_copy = dict(item)
                    for old_name, new_name in renames.items():
                        if old_name in item_copy:
                            item_copy[new_name] = item_copy.pop(old_name)
                    merged_data[composite_key].update(item_copy)
                else:
                    merged_data[composite_key].update(item)

        rows = list(merged_data.values())
        logger.info(f"Fetched {len(rows)} asset metrics from cache (cycle #{metrics.get('cycle_number')})")
        return rows

    except Exception as e:
        logger.error(f"Error fetching metrics from cache: {e}")
        return []


# Signals column glyph builder. Fixed 9-position ordering ensures
# the trader can pattern-match a signal to its slot at a glance. Active glyphs
# use signal-specific hues; inactive glyphs render in the muted neutral colour
# (same shade as the empty-cell text). Rendered as inline <span> elements via
# DataTable markdown mode (see `markdown_options={'html': True}` in
# create_main_table).
def _abs_z_over(row, field_stem: str, tier: str, threshold: float) -> bool:
    """|z| > threshold for `{field_stem}_{tier}`, absent-safe.

    Key built by SUFFIX rather than branching per tier, so adding a fifth tier
    never touches this table.
    """
    v = row.get(f'{field_stem}_{tier}')
    return v is not None and abs(v) > threshold


def _velocity_glyph_colour(row, tier):
    """Green when participation is expanding, red when contracting.

    Derived from the SIGN OF VELOCITY — the same field the predicate tests — so
    the colour and the lighting can never disagree. Reading direction from
    `expansion_state` instead would reintroduce that gap.
    """
    v = row.get(f'velocity_{tier}')
    if v is None:
        return _GLYPH_INACTIVE
    return '#5fb87c' if v > 0 else '#d94560'


_SIGNAL_GLYPHS = (
    # (emit_key, is_active_fn(row, z_threshold, tier, max_abs_intraday_rs_z), colour_fn)
    #
    # The glyph and its colour come from `_HEALTH_GLYPH_MAP` (below), keyed by
    # emit_key. They used to be typed out here as well and agreed with the map
    # only by hand. `colour_fn` is None, or a (row, tier) -> colour callable for
    # a glyph whose hue carries information the shape cannot — only ↗ has one.
    #
    # Every predicate takes the same four arguments; only α uses the fourth,
    # the cross-benchmark RS aggregate. It used to be the only one given it,
    # picked out by its POSITION in this tuple, so reordering the slots would
    # have handed it the wrong arguments with nothing raised.
    #
    # Rewritten for the locked-calculator schema. VWAP band-breach and a
    # separate acceleration-z glyph (⇈) are dropped — VWAP bands no longer
    # exist, and Volume Dynamics produces no z-score of its own (velocity,
    # acceleration, persistence and expansion state, each per tier), so its
    # one glyph — ↗, on velocity — covers it.
    # Order is FIXED — traders pattern-match by slot position.
    #
    # Every rule that lights a row has a slot here, so a lit row always shows
    # which signal lit it. ‖ (VWAP z) and ↻ (Rolling Momentum z) were the two
    # rules without one — a row they lit showed only dark glyphs. They are
    # appended after ↗ so no existing slot moved.
    #
    # The z-driven predicates follow the selected tier. These nine
    # glyphs are the glance-level scan of the whole table; leaving them on 90d
    # while the numbers beside them showed 22d would have the column silently
    # answering a different question, and because glyphs are read by POSITION
    # rather than value, that mismatch is invisible.
    ('breakout',                lambda r, t, tier, _a: bool(r.get('breakout_now')), None),              # ▲ outside the opening range right now
    ('volume_block',            lambda r, t, tier, _a: _abs_z_over(r, 'block_volume_z', tier, t), None),  # ■ block-volume-z
    ('momentum',                lambda r, t, tier, _a: _abs_z_over(r, 'early_return_z', tier, t), None),  # ● momentum early-return-z
    ('relative_strength',       lambda r, t, tier, max_alpha: max_alpha is not None and abs(max_alpha) > t, None),  # α RS intraday-rs-z (max across benchmarks)
    ('price_volume_divergence', lambda r, t, tier, _a: _abs_z_over(r, 'pvd_z', tier, t), None),           # ρ pvd-z
    ('price_levels',            lambda r, t, tier, _a: bool(r.get('day_events')), None),                # ✖ any-crossing-today
    # Volume participation shifting sharply. Colour carries the DIRECTION —
    # green expanding, red contracting — because one arrow cannot.
    #
    # This slot used to fire on `bool(expansion_state)`, and measurement showed
    # that lit it 99.9% of the time: expansion_state returns 0 only when
    # velocity is EXACTLY 0.0, and velocity is a continuous float, so zero
    # essentially never occurs. A permanently-lit glyph carries no information
    # at all, in the one column whose entire purpose is glance-level pattern
    # matching.
    #
    # Thresholding expansion_state does not fix it — at |state| == 2 it still
    # lights 48.9%, because expansion_state is a SIGN CLASSIFICATION ("is
    # velocity positive?"), roughly a coin flip by construction. No threshold
    # applied to a coin flip makes it rare.
    #
    # `velocity` IS a magnitude — the block-over-block change in Volume Block's
    # own abnormality z — so a threshold on it marks a real, occasional event:
    # "participation shifted sharply this block". It is already measured in σ
    # (a difference of z-scores), so the settings-drawer z-slider applies
    # directly and no second threshold control is introduced.
    ('volume_dynamics',  lambda r, t, tier, _a: _abs_z_over(r, 'velocity', tier, t),
     _velocity_glyph_colour),                                                                   # ↗ volume participation shift
    ('vwap',             lambda r, t, tier, _a: _abs_z_over(r, 'vwap_z', tier, t), None),         # ‖ vwap-z
    ('rolling_momentum', lambda r, t, tier, _a: _abs_z_over(r, 'session_return_z', tier, t), None),  # ↻ rolling-momentum-z
)
_GLYPH_INACTIVE = '#3a3128'


# Single source of truth for the per-signal glyph + color, read by:
#   - the Signals-column slots (`_build_signals_cell`, for each `_SIGNAL_GLYPHS`
#     entry's emit_key — ↗ alone colours by direction instead);
#   - the DataTable column-header prefixes (`create_main_table`).
# Keyed by `emit_key` from `dashboard_signals.SIGNAL_REGISTRY`.
_HEALTH_GLYPH_MAP = {
    'breakout':                 ('▲', '#e8a447'),
    'volume_block':             ('■', '#7a9cc4'),
    'momentum':                 ('●', '#a78bfa'),
    'relative_strength':        ('α', '#5fb87c'),
    'price_volume_divergence':  ('ρ', '#a6a380'),
    'price_levels':             ('✖', '#d94560'),
    'vwap':                     ('‖', '#d94560'),
    'volume_dynamics':          ('↗', '#8a6b3b'),
    'rolling_momentum':         ('↻', '#c4a76e'),
}


def _build_signals_cell(row: dict, max_abs_intraday_rs_z, z_threshold: float,
                        tier: str = '90d') -> str:
    """Return an HTML string with 9 <span> glyphs, one per signal slot.
    Active = signal-specific hue; inactive = muted `_GLYPH_INACTIVE`.
    RS glyph uses the pre-computed `max_abs_intraday_rs_z` (cross-benchmark
    aggregate, already resolved at `tier` by the caller)."""
    parts = []
    for emit_key, is_active, colour_fn in _SIGNAL_GLYPHS:
        glyph, base_colour = _HEALTH_GLYPH_MAP[emit_key]
        # colour_fn, where present, is for a glyph whose hue carries direction
        # the shape cannot (↗).
        if not is_active(row, z_threshold, tier, max_abs_intraday_rs_z):
            color = _GLYPH_INACTIVE
        elif colour_fn is not None:
            color = colour_fn(row, tier)
        else:
            color = base_colour
        parts.append(f'<span style="color:{color};font-size:14px;line-height:1;margin-right:3px;">{glyph}</span>')
    return ''.join(parts)


def _auto_raw_fields(row):
    """Auto-generate the `raw_*` dict for a merged row via reflection on every
    registered Signal's Row class.

    For each declared field of each Row dataclass:
      - If the field has a `field_renames` mapping in its owning Signal, the
        source key is the RENAMED name (matching what get_current_metrics wrote).
        e.g. BreakoutRow.status → merged row has 'breakout_status' → we emit
        `raw_breakout_status = row.get('breakout_status')`.
      - Otherwise the source key is the field name itself.
      - For list/dict fields with a default_factory (e.g. PriceLevelsRow.day_events),
        we preserve the empty-collection default so downstream `for x in
        row['raw_day_events']` never trips on None.

    Shared fields (asset_id, segment_code, symbol, current_price, timestamp) are
    deduplicated across Rows — the first Row's mapping wins; subsequent Rows
    that would emit the same raw_ key are silently skipped.
    """
    import dataclasses
    from dashboard_signals import SIGNAL_REGISTRY

    raw = {}
    seen_raw_keys = set()
    for sig in SIGNAL_REGISTRY.values():
        for f in dataclasses.fields(sig.row_class):
            # Post-rename source key as stored by get_current_metrics.
            source_key = sig.field_renames.get(f.name, f.name)
            raw_key = f'{sig.raw_prefix}{source_key}'
            if raw_key in seen_raw_keys:
                continue
            seen_raw_keys.add(raw_key)
            # Preserve empty-collection defaults for list/dict-typed fields.
            if f.default_factory is not dataclasses.MISSING:
                default_val = f.default_factory()
            else:
                default_val = None
            raw[raw_key] = row.get(source_key, default_val)
    return raw


def build_table_data(rows, z_threshold: float = None, tier: str = '90d'):
    """Build table data list from merged-metrics rows.

    `rows` is a list of plain dicts (one per asset), not a
    pandas DataFrame. Iterating dicts directly makes `.get(key, default)`
    behave as intended — no NaN masking of `None` / `[]` / `{}` defaults, no
    hidden dtype coercions, no defensive isinstance guards needed at read time.

    raw_* fields are auto-generated via reflection on the
    Signal registry's Row classes. Computed / cross-row raw_* fields
    (raw_all_benchmarks, raw_max_intraday_rs_z, raw_day_breach_count) stay inline.

    `z_threshold` param (defaults to `_Z_HIGHLIGHT` from config)
    drives the Signals-column glyph activation semantics — kept in sync with the
    row-highlight rules that the user adjusts via the settings-drawer slider.

    `tier` (from `store-active-tier`) selects which z-tier every
    displayed value and glyph reads. The raw_* fields still carry ALL four
    tiers — reflection emits every declared field — so the detail drawer can
    keep showing the full bundle while the table shows one.
    """
    if z_threshold is None:
        z_threshold = _Z_HIGHLIGHT
    if not rows:
        return []

    table_data = []
    for row in rows:
        # Momentum cell — early_return (the 09:15->09:30 move, once that window
        # has completed) with its z at the selected tier; gap_return (the
        # overnight gap) until then.
        early_val = row.get('early_return')
        if early_val is not None:
            mom_str, _ = format_metric_value(early_val * 100.0, 'momentum')
            mom_z = row.get(f'early_return_z_{tier}')
        else:
            gap_val = row.get('gap_return')
            mom_str, _ = format_metric_value(gap_val * 100.0 if gap_val is not None else None, 'momentum')
            mom_z = row.get(f'gap_z_{tier}')
        mom_cell_str = f"{mom_str} (z{mom_z:+.1f})" if mom_z is not None else mom_str

        # Rolling Momentum cell — session_return is already a plain percent
        # (not a fractional/log return like early_return/gap_return), so no
        # *100.0 here.
        session_return_val = row.get('session_return')
        rmom_str, _ = format_metric_value(session_return_val, 'momentum')
        rmom_z = row.get(f'session_return_z_{tier}')
        rmom_cell_str = f"{rmom_str} (z{rmom_z:+.1f})" if rmom_z is not None else rmom_str

        vwap_disp = row.get('displacement')
        vwap_str, _ = format_metric_value(
            vwap_disp * 100.0 if vwap_disp is not None else None, 'deviation'
        )

        # Relative strength: show all benchmarks, one per line, with the
        # selected tier's intraday_rs_z suffix when available. Also compute
        # max|intraday_rs_z| across benchmarks for the row highlight rule.
        #
        # Both the value AND its z read the SAME tier: RS is measured once per
        # beta tier, so pairing intraday_rs_22d with intraday_rs_z_90d
        # would show a number normalised against a distribution built from a
        # different beta.
        all_benchmarks = row.get('all_benchmarks', [])
        max_abs_intraday_rs_z = None
        if all_benchmarks:
            parts = []
            for bm in all_benchmarks:
                bm_name = bm.get('benchmark_name', '?')
                bm_rs = bm.get(f'intraday_rs_{tier}')
                bm_z = bm.get(f'intraday_rs_z_{tier}')
                if bm_z is not None:
                    abs_z = abs(bm_z)
                    if max_abs_intraday_rs_z is None or abs_z > max_abs_intraday_rs_z:
                        max_abs_intraday_rs_z = abs_z
                if bm_rs is not None:
                    if bm_z is not None:
                        parts.append(f"{bm_name} {bm_rs * 100:+.2f}% (z {bm_z:+.1f})")
                    else:
                        parts.append(f"{bm_name} {bm_rs * 100:+.2f}%")
            rel_str = '\n'.join(parts) if parts else '-'
        else:
            rel_str = '-'

        # Breakout indicator — signed continuous position while price is
        # outside the opening range right now (breakout_now), the same rule as
        # the ▲ glyph beside it. Not breakout_direction: that is the day's
        # first breakout and never clears.
        direction = row.get('breakout_now') or 0
        if direction:
            position = row.get('breakout_position')
            breakout_str = f"{position:+.2f}" if position is not None else '-'
        else:
            breakout_str = '-'

        # Breach indicator — level names crossed at any point today (day-persistent).
        day_events = row.get('day_events', [])
        if isinstance(day_events, list) and day_events:
            high_count = sum(1 for lt in day_events if 'high' in lt.lower())
            low_count = sum(1 for lt in day_events if 'low' in lt.lower())
            if high_count and low_count:
                breach_str = f"{high_count}H {low_count}L"
            elif high_count:
                breach_str = f"{high_count}H"
            elif low_count:
                breach_str = f"{low_count}L"
            else:
                breach_str = str(len(day_events))
        else:
            breach_str = '-'

        # Volume block — show the primary (block-volume) z-score.
        block_z = row.get(f'block_volume_z_{tier}')
        block_vol_str = f"{block_z:.1f}" if block_z is not None else '-'

        # Range column: current price's distance from today's running day
        # high/low, sourced from Momentum's row (high_price/low_price come
        # from PriceDataManager.get_current_prices() — the engine's live
        # session high/low, not a metrics.db-derived value).
        current_px = row.get('current_price')
        day_high = row.get('high_price')
        day_low = row.get('low_price')
        if current_px is not None and day_high and day_low:
            dist_high_pct = (current_px - day_high) / day_high * 100.0
            dist_low_pct = (current_px - day_low) / day_low * 100.0
            range_str = f"H:{dist_high_pct:+.1f}%\nL:{dist_low_pct:+.1f}%"
        else:
            range_str = '-'

        raw_day_breach_count = len(day_events) if isinstance(day_events, list) else 0

        # Auto-generate raw_* fields via Signal registry reflection.
        raw_fields = _auto_raw_fields(row)

        # Cross-row / computed raw_ fields (not derived from a single Row dataclass field).
        raw_fields['raw_all_benchmarks'] = all_benchmarks
        # NOT `raw_max_intraday_rs_z_90d` any more: this holds whichever tier is
        # selected, so a _90d suffix would be an active lie. The row-highlight
        # α rule and the why-line's α entry both key off this exact name, so
        # renaming forces those two to be updated rather than silently
        # continuing to read a field that now means something else.
        raw_fields['raw_max_intraday_rs_z'] = (
            max(
                (bm.get(f'intraday_rs_z_{tier}') for bm in all_benchmarks
                 if bm.get(f'intraday_rs_z_{tier}') is not None),
                key=lambda v: abs(v) if v is not None else -1,
                default=None,
            ) if all_benchmarks else None
        )

        signals_cell = _build_signals_cell(row, max_abs_intraday_rs_z, z_threshold, tier)

        # Cross-tier divergence: max |z_22d − z_long| across this row's
        # z-scored signals. A FLOAT, not a formatted string — the DataTable's
        # native sort is lexicographic on text columns, so "10.5" would sort
        # before "2.1" and the column would confidently rank the LEAST
        # divergent stocks first.
        #
        # None renders as a blank cell rather than '-': a numeric column cannot
        # hold a dash, and a correct sort is worth more than the dash.
        row_div = _row_divergence(raw_fields)
        div_value = round(row_div, 1) if row_div is not None else None

        table_data.append({
            # Formatted strings for TABLE DISPLAY (columns reference these)
            'Symbol': row.get('symbol', 'Unknown'),
            'Signals': signals_cell,
            # A missing price renders '-': formatting None raised and failed the
            # whole table update, and a default of 0 showed a fabricated ₹0.00.
            'Price': (f"₹{row['current_price']:.2f}"
                      if row.get('current_price') is not None else '-'),
            'Mom': mom_cell_str,
            'R.Mom': rmom_cell_str,
            'Breakout': breakout_str,
            'VWAP': vwap_str,
            'RelStr': rel_str,
            'Block Volume': block_vol_str,
            'Breach': breach_str,
            'Range': range_str,
            'Div': div_value,
            'asset_id': row.get('asset_id'),
            'raw_day_breach_count': raw_day_breach_count,
            **raw_fields,
        })

    # Sort: indices (IDX_I) first alphabetically, then NSE_EQ alphabetically.
    segment_order = {'IDX_I': 0, 'NSE_EQ': 1}
    table_data.sort(key=lambda r: (
        segment_order.get(r.get('raw_segment_code'), 99),
        (r.get('Symbol') or '').upper(),
    ))

    return table_data


def _row_highlight_rules(z_threshold: float = None, tier: str = '90d'):
    """Build the DataTable's `style_data_conditional` list.

    Composed of:
      1. Column-scoped style for the Symbol column (kept inline; not a signal
         concern — it's typography, not a data-driven highlight).
      2. Per-signal row-highlight rules derived from
         `SIGNAL_REGISTRY[*].highlight_rule` — eight today: breakout now, level
         breach today, and |z| above the slider at the selected tier for early
         return, VWAP, RS (max across benchmarks), PV Divergence, block volume
         and Rolling Momentum. Adding a highlight to a future signal is a
         one-line change to its Signal registration.

    `highlight_rule` is a `Callable[[threshold, tier], (query, color)]`.
    `z_threshold` defaults to `_Z_HIGHLIGHT` (the config-driven baseline).
    The z-slider's callback (`_rebuild_highlight_rules`) re-invokes this function
    with the user-adjusted value to rebuild `style_data_conditional` live.

    `tier` is the second argument. The rules build DataTable
    filter_query strings against `raw_{field}_z_{tier}` names, so the row
    background answers "is this unusual" at the SAME tier the cell values and
    the Signals glyphs are showing. A row lit by a 90d rule while its Mom cell
    reads a 22d z would be actively misleading — the highlight is what draws
    the eye to the row in the first place.
    """
    from dashboard_signals import SIGNAL_REGISTRY

    if z_threshold is None:
        z_threshold = _Z_HIGHLIGHT

    # Symbol column font styling lives in style_cell_conditional (with Tier 1
    # sizing/weight for scan-anchoring). Only the font-family is set here so
    # column-specific style_data_conditional doesn't override the Tier 1
    # fontSize.
    rules = [
        {
            'if': {'column_id': 'Symbol'},
            'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
        },
    ]
    for sig in SIGNAL_REGISTRY.values():
        if sig.highlight_rule is None:
            continue
        filter_query, bg_color = sig.highlight_rule(z_threshold, tier)
        rules.append({
            'if': {'filter_query': filter_query},
            'backgroundColor': bg_color,
        })
    return rules


def create_main_table():
    """Create main metrics table component (called once in layout)."""

    # Define columns (asset_id kept in data but NOT in columns list).
    # 'Signals' column between Symbol and Price — 9 glyph slots. Uses markdown
    # presentation with html=True to allow per-glyph <span> coloring.
    # Signal-owning columns get a glyph prefix in their header so the
    # column-name doubles as a legend for the Signals glyph column. Same
    # glyphs come from `_HEALTH_GLYPH_MAP` — single source of truth.
    _hg = _HEALTH_GLYPH_MAP
    columns = [
        {'name': 'Symbol',                                        'id': 'Symbol', 'type': 'text'},
        {'name': 'Signals',                                       'id': 'Signals', 'type': 'text', 'presentation': 'markdown'},
        {'name': 'Price',                                         'id': 'Price', 'type': 'text'},
        {'name': 'Range',                                         'id': 'Range', 'type': 'text'},
        {'name': f"{_hg['momentum'][0]} Mom",                     'id': 'Mom', 'type': 'text'},
        {'name': f"{_hg['rolling_momentum'][0]} R.Mom",           'id': 'R.Mom', 'type': 'text'},
        {'name': f"{_hg['breakout'][0]} Breakout",                'id': 'Breakout', 'type': 'text'},
        {'name': f"{_hg['vwap'][0]} VWAP",                        'id': 'VWAP', 'type': 'text'},
        {'name': f"{_hg['relative_strength'][0]} RelStr",         'id': 'RelStr', 'type': 'text'},
        {'name': f"{_hg['volume_block'][0]} Block Volume",        'id': 'Block Volume', 'type': 'text'},
        {'name': f"{_hg['price_levels'][0]} Breach",              'id': 'Breach', 'type': 'text'},
        # Cross-tier divergence: max |z_22d - z_long| across this row's
        # z-scored signals. NUMERIC, unlike every other column here —
        # sort_action='native' sorts TEXT lexicographically, which would
        # put "10.5" before "2.1" and rank the LEAST divergent stocks
        # first. A narrow Tier-3 context column, not a signal column:
        # divergence is for FINDING candidates, not monitoring one, so it
        # earns a sortable header rather than continuous attention.
        {'name': 'Δ',                                             'id': 'Div', 'type': 'numeric'},
    ]

    return dash_table.DataTable(
        id='metrics-table',
        columns=columns,
        data=[],  # Empty initially, populated by callback
        sort_action='native',
        filter_action='native',
        page_action='none',
        persistence=True,
        persistence_type='session',
        persisted_props=['sort_by', 'filter_query'],
        # Enable HTML rendering inside markdown cells so per-glyph <span>
        # colors work in the Signals column. dangerously_allow_html is safe here
        # because glyph HTML is built server-side by build_table_data — never
        # from external / user-supplied content.
        markdown_options={'html': True},
        style_table={
            'overflowX': 'auto',
            'backgroundColor': '#1a1410',
        },
        style_cell={
            'textAlign': 'left',
            'verticalAlign': 'middle',
            'padding': '13px 14px',
            'fontSize': '13px',
            'lineHeight': '1.4',
            'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
            'cursor': 'pointer',
            'backgroundColor': '#1a1410',
            'color': '#f5e9d3',
            'border': 'none',
        },
        style_header={
            'backgroundColor': '#1a1410',
            'color': '#b8a99b',
            'fontWeight': '500',
            'fontSize': '11px',
            'letterSpacing': '1.3px',
            'textTransform': 'uppercase',
            'textAlign': 'center',
            'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
            'padding': '12px 13px',
        },
        style_data_conditional=_row_highlight_rules(),
        # Three-tier typography — Tier 1 (14px medium, high-
        # contrast) for primary signal anchors (Symbol, Mom, VWAP); Tier 2
        # (12px regular) for secondary (Breakout, RelStr, Block Volume); Tier 3
        # (11px muted colour) for context (Price, Range, Breach). Eye anchors
        # on Tier 1 across all rows, drops to Tier 2/3 selectively.
        style_cell_conditional=[
            # Tier 1 columns — primary anchors
            {
                'if': {'column_id': 'Symbol'},
                'width': '100px', 'minWidth': '100px', 'maxWidth': '100px',
                'fontSize': '14px', 'fontWeight': '500', 'color': '#f5e9d3',
            },
            {
                'if': {'column_id': 'Mom'},
                'textAlign': 'center',
                'width': '120px', 'minWidth': '120px', 'maxWidth': '120px',
                'fontSize': '14px', 'fontWeight': '500', 'color': '#f5e9d3',
            },
            {
                'if': {'column_id': 'VWAP'},
                'textAlign': 'center',
                'width': '90px', 'minWidth': '90px', 'maxWidth': '90px',
                'fontSize': '14px', 'fontWeight': '500', 'color': '#f5e9d3',
            },
            # Signals column — small fixed width, glyph-only
            {
                'if': {'column_id': 'Signals'},
                'textAlign': 'left',
                'width': '80px', 'minWidth': '80px', 'maxWidth': '80px',
                'padding': '13px 6px',
                'fontSize': '13px', 'letterSpacing': '1px',
            },
            # Tier 2 columns — secondary signals
            {
                'if': {'column_id': 'R.Mom'},
                'textAlign': 'center',
                'width': '90px', 'minWidth': '90px', 'maxWidth': '90px',
                'fontSize': '12px', 'fontWeight': '400', 'color': '#f5e9d3',
            },
            {
                'if': {'column_id': 'Breakout'},
                'textAlign': 'center',
                'width': '80px', 'minWidth': '80px', 'maxWidth': '80px',
                'fontSize': '12px', 'fontWeight': '400', 'color': '#f5e9d3',
            },
            {
                'if': {'column_id': 'RelStr'},
                'textAlign': 'center', 'whiteSpace': 'pre-line',
                'width': '120px', 'minWidth': '120px', 'maxWidth': '120px',
                'fontSize': '12px', 'fontWeight': '400', 'color': '#f5e9d3',
            },
            {
                'if': {'column_id': 'Block Volume'},
                'textAlign': 'center',
                'width': '90px', 'minWidth': '90px', 'maxWidth': '90px',
                'fontSize': '12px', 'fontWeight': '400', 'color': '#f5e9d3',
            },
            # Tier 3 columns — context / muted
            {
                'if': {'column_id': 'Price'},
                'textAlign': 'right',
                'width': '90px', 'minWidth': '90px', 'maxWidth': '90px',
                'fontSize': '11px', 'color': '#b8a99b',
            },
            {
                'if': {'column_id': 'Range'},
                'textAlign': 'right', 'whiteSpace': 'pre-line',
                'width': '100px', 'minWidth': '100px', 'maxWidth': '100px',
                'fontSize': '11px', 'color': '#b8a99b',
            },
            {
                'if': {'column_id': 'Div'},
                'textAlign': 'right',
                'width': '55px', 'minWidth': '55px', 'maxWidth': '55px',
                'fontSize': '11px', 'color': '#b8a99b',
            },
            {
                'if': {'column_id': 'Breach'},
                'textAlign': 'center',
                'width': '70px', 'minWidth': '70px', 'maxWidth': '70px',
                'fontSize': '11px', 'color': '#b8a99b',
            },
        ]
    )


# "Why" line builder. Answers "why is this row highlighted?" with every firing
# signal, shown BEFORE any detail card. Uses the same glyph set as the Signals
# column for visual continuity.
def _build_why_line(selected_row: dict, z_threshold: float = None,
                    tier: str = '90d') -> html.Div:
    """Return an html.Div stacked with one row per firing signal.

    Each candidate renders on its own row. Coverage: breach, breakout,
    plus the z-signals at the selected tier (block-volume / early-return /
    RS / PVD / VWAP / rolling-momentum) — exactly the rules that light a row,
    so the line never names a signal that lit nothing. Gap z is not here: it
    lights no row, and it is fixed at 09:15 for the whole day; it stays on
    the Momentum card. No top-N cap; every firing signal is shown.

    Ranking: breach > breakout > z-signals (by descending |z|).
    Empty div if nothing is unusual — no "Nothing to see here" clutter.
    """
    # Live slider value, falling back to the config default only when the
    # store has not hydrated yet. Was pinned to _Z_HIGHLIGHT, which made this
    # line disagree with the row highlight that prompted the trader to open
    # the drawer in the first place.
    z_thresh = float(z_threshold) if z_threshold is not None else _Z_HIGHLIGHT
    candidates = []  # (rank_tuple, glyph, color, label_text)

    # Breach (highest priority — state flag with count).
    day_breach_count = selected_row.get('raw_day_breach_count') or 0
    if day_breach_count > 0:
        candidates.append(((0, day_breach_count), '✖', '#d94560',
                           f'Level breach today · {day_breach_count} breached'))

    # State-flag signals (rank tier 1 — same as breakout since these are
    # boolean facts, not z-magnitudes). breakout_now is a signed int: outside
    # the opening range right now — the rule that lights ▲ and the row.
    direction = selected_row.get('raw_breakout_now') or 0
    if direction:
        position = selected_row.get('raw_breakout_position')
        pos_str = f'{position:+.2f}' if position is not None else ''
        dir_label = 'up' if direction == 1 else 'down'
        candidates.append(((1, 0), '▲', '#e8a447',
                           f'Breakout {dir_label} {pos_str}'.strip()))

    # z-signals: (raw_key, glyph, color, label_prefix). Rank tier 2 — sorted
    # within-tier by descending |z|.
    #
    # Keys AND labels follow the selected tier. This line answers
    # "why is this row highlighted?", so it has to read the same tier as the
    # highlight that prompted the question — pinned to 90d it would list
    # nothing for a row lit by a 22d rule, or name a different signal than the
    # one that lit it. The label carries the tier for the same reason: a
    # caption reading "90d" above a 22d number contradicts itself.
    #
    # raw_max_intraday_rs_z has no suffix — it is a cross-benchmark aggregate
    # already resolved at the selected tier by build_table_data.
    z_signals = [
        (f'raw_block_volume_z_{tier}',   '■', '#7a9cc4', f'Block Vol Z {tier}'),
        (f'raw_early_return_z_{tier}',   '●', '#a78bfa', f'Mom Early-return-z {tier}'),
        ('raw_max_intraday_rs_z',        'α', '#5fb87c', f'RS Intraday-z {tier}'),
        (f'raw_pvd_z_{tier}',            'ρ', '#a6a380', f'PVD-z {tier}'),
        (f'raw_vwap_z_{tier}',           '‖', '#d94560', f'VWAP-z {tier}'),
        (f'raw_session_return_z_{tier}', '↻', '#c4a76e', f'Rolling Mom Z {tier}'),
    ]
    for key, glyph, color, prefix in z_signals:
        val = selected_row.get(key)
        if val is not None and abs(val) > z_thresh:
            candidates.append(((2, -abs(val)), glyph, color, f'{prefix} {val:+.2f}'))

    # Sort by rank tuple ASC (breach first, then state-flags, then z-signals by desc |z|).
    candidates.sort(key=lambda x: x[0])

    if not candidates:
        return html.Div()  # nothing unusual — hide entirely

    header = html.Div('Unusual signals', style={
        'color': '#8a7864',
        'fontSize': '10px',
        'letterSpacing': '1.4px',
        'textTransform': 'uppercase',
        'marginBottom': '6px',
    })
    rows = [
        html.Div([
            html.Span(glyph + '  ', style={'color': color, 'fontWeight': '600',
                                            'display': 'inline-block', 'width': '20px'}),
            html.Span(label_text, style={'color': '#f5e9d3'}),
        ], style={'padding': '2px 0'})
        for (_, glyph, color, label_text) in candidates
    ]

    return html.Div([header, *rows], style={
        'padding': '8px 12px',
        'marginTop': '4px',
        'marginBottom': '12px',
        'backgroundColor': '#231b15',
        'borderLeft': '3px solid #4a3614',
        'fontSize': '12px',
        'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
        'letterSpacing': '0.3px',
    })


def create_detail_panel(selected_row, z_threshold: float = None,
                        tier: str = '90d'):
    """Create detail panel using data from selected_row (in-memory cache).

    `z_threshold` comes from the settings-drawer slider (`store-z-threshold`)
    and reaches both the why-line and every card builder. Before this, the
    drawer read the config constant `_Z_HIGHLIGHT` while the scores table,
    row highlights and Signals glyphs followed the slider — so at any slider
    position other than the configured default the why-line could report
    "nothing unusual" for a stock whose Volume Block card was simultaneously
    rendering an amber accent.

    The cards come from iteration over `dashboard_signals.SIGNAL_REGISTRY`.
    Each signal's `card_builder` (defined in dashboard_signals.py) receives
    `selected_row` and returns an html.Div. Shared styles (H5_STYLE, P_STYLE,
    card_style) also live in dashboard_signals.py.

    The panel opens with a "why" line — every firing signal, before the cards —
    so the trader gets a direct answer to "why is this row highlighted?"
    without scanning cards.

    Two-source design (deliberate, not a bug):
      This panel reads the LATEST_METRICS in-memory cache (populated at every
      30-sec metrics cycle, freshest data available). The charts drawer, in
      contrast, reads the persisted metrics.db (a history query over the
      chart-window setting, 1h–1d, clamped to one trading day). At cycle
      boundaries — the sub-second window between the metrics thread's DB store
      and the LATEST_METRICS cache update — the two surfaces can show numbers
      off by one cycle. This is deliberate:
        * Detail panel wants freshness (single-row lookup, zero query cost).
        * Charts want history (multi-row time series, one query per open).
      Unifying both to metrics.db would add an indexed SELECT to every detail-
      panel refresh (30-sec cadence × 82 assets) for a divergence window that
      is invisible in practice. Cost > benefit. Do NOT "fix" this without an
      explicit design conversation — the divergence is by design.
    """
    from dashboard_signals import SIGNAL_REGISTRY

    symbol = selected_row['Symbol']
    # The why-line follows the tier; the CARDS below deliberately do not — they
    # keep showing all four tiers at once, because the drawer is where
    # cross-tier divergence is visible and collapsing it to one tier would
    # destroy the only surface that exposes it. Whether that exception should
    # hold is an OPEN design question, not settled.
    why_line = _build_why_line(selected_row, z_threshold, tier)
    cards = [
        sig.card_builder(selected_row, z_threshold, tier)
        for sig in SIGNAL_REGISTRY.values()
        if sig.card_builder is not None
    ]

    # Legend line — bigger (13px), one row per group.
    legend_line = html.Div(
        [
            html.Div('z = z-score · μ = tier mean · σ = std dev · n = sample count'),
            html.Div('α = alpha · α-z = alpha z-score · β = beta'),
            html.Div('ρ = Pearson correlation'),
            html.Div('Critical r = critical correlation coefficient (Fisher-z 95% noise floor)'),
        ],
        style={
            'fontSize': '13px',
            'lineHeight': '1.6',
            'color': '#b8a99b',
            'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
            'padding': '12px 0',
            'marginBottom': '14px',
            'borderTop': '1px dashed #3a3128',
            'borderBottom': '1px dashed #3a3128',
        },
    )

    return html.Div([
        html.H4(symbol, style={
            'fontSize': '30px',
            'fontWeight': '500',
            'fontStyle': 'italic',
            'fontFamily': "'Fraunces', Georgia, serif",
            'marginBottom': '8px',
            'marginTop': '4px',
            'color': '#f5e9d3',
            'letterSpacing': '-0.5px',
        }),
        why_line,
        legend_line,
    ] + cards)


def _two_col_row(left_id, right_id, left_header=None):
    """Two-column chart row helper. If right_id is None, right half is an
    empty spacer. Optional `left_header` renders above the left cell's chart
    — used by the RS chart to host the benchmark-selector pill row.

    When a `left_header` is present, the right cell reserves
    matching vertical space via top padding — 34px matches the pill row's
    intrinsic height (`.chart-window-pill` = 11px font + 6px×2 padding + 1px
    border ≈ 24px, plus the row container's 2px×2 padding and 6px marginBottom
    ≈ 34px total). The previous approach — wrapping `left_header` into a hidden
    div on the right cell — put the same component (with its `id`) into the
    layout twice, tripping Dash's `DuplicateIdError` on initial validation."""
    left_children = []
    if left_header is not None:
        left_children.append(left_header)
    left_children.append(
        dcc.Graph(id=left_id, config={'displayModeBar': True, 'scrollZoom': True})
    )
    left = html.Div(left_children, style={'flex': '1', 'minWidth': '0'})

    right_style = {'flex': '1', 'minWidth': '0'}
    if left_header is not None:
        right_style['paddingTop'] = '34px'

    if right_id is None:
        right = html.Div(style=right_style)
    else:
        right = html.Div(
            [dcc.Graph(id=right_id, config={'displayModeBar': True, 'scrollZoom': True})],
            style=right_style,
        )
    return html.Div([left, right], style={'display': 'flex', 'gap': '12px'})


# ============================================================================
# Indices Drawer — per-index card builder
# ============================================================================

def get_index_breadth_history(index_name: str, hours: int = 8):
    """The last N hours of one index's index_metrics rows, for its six
    mini-charts. Returns `(frame, fallback_ts)`.

    Falls back like get_asset_metrics_history: when today's window is empty
    (pre-market, evenings, weekends) it re-queries the window ending at this
    index's newest row, `MAX(created_at)`, clamped to that row's day.
    `fallback_ts` is that anchor, so the charts can be labelled with the
    session they show; None when the window was today's. Without it the
    charts were blank outside market hours beneath a card showing the last
    session's numbers.
    """
    db = get_metrics_db()
    if db is None:
        return pd.DataFrame(), None

    def _select(conn, cutoff):
        return pd.read_sql_query("""
            SELECT timestamp, advancers, decliners,
                   in_breakout_up, in_breakout_down,
                   constituent_z,
                   level_breached_today,
                   cross_sectional_dispersion,
                   weighted_volume_velocity_22d,
                   weighted_volume_velocity_90d,
                   weighted_volume_velocity_180d,
                   weighted_volume_velocity_252d,
                   weighted_intraday_return,
                   ex_top5_intraday_return,
                   weighted_vwap_deviation
            FROM index_metrics
            WHERE index_name = ? AND created_at >= ?
            ORDER BY timestamp ASC
        """, conn, params=(index_name, cutoff))

    try:
        now = datetime.now(IST)
        with sqlite3.connect(db.db_path, timeout=10) as conn:
            df = _select(conn, clamp_to_day_start(now - timedelta(hours=hours), now).isoformat())
            if not df.empty:
                return df, None
            max_ts = conn.execute(
                "SELECT MAX(created_at) FROM index_metrics WHERE index_name = ?",
                (index_name,)).fetchone()[0]
            if not max_ts:
                return df, None
            anchor = datetime.fromisoformat(max_ts)
            cutoff = clamp_to_day_start(anchor - timedelta(hours=hours), anchor).isoformat()
            return _select(conn, cutoff), anchor
    except Exception as e:
        logger.error(f"Error querying index_metrics for {index_name}: {e}")
        return pd.DataFrame(), None


def _count_z_spikes(constituent_z, signal: str, tier: str, threshold: float):
    """Count |z| > threshold across an index's constituents, at display time.

    Returns `(high, low, evaluated)` — above +threshold, below -threshold, and
    how many constituents carried a value for this (signal, tier) at all.

    This replaced six stored counters on IndexSummary. They baked `> 2.0` into
    the metrics thread while the scores table, row highlights and Signals glyphs
    all follow the settings-drawer slider — so at any slider position other than
    2.0 the index card and the scores table disagreed about what "unusual"
    means, on the same screen.

    ONE function, deliberately, because two surfaces call it: the breadth grid
    (live cache) and the intraday breadth chart (index_metrics history). If they
    counted separately they could drift, and a card contradicting the chart
    directly beneath it is worse than the single wrong threshold this replaces.

    `evaluated` is the count of constituents present for this (signal, tier),
    which is exactly what the removed `*_evaluated` denominators carried — a
    constituent whose tier has not accumulated enough history is absent from
    the mapping rather than counted as a non-spike.

    Tolerates a raw JSON string, since the history frame delivers one.

    ENCODING. The payload is the compact, self-describing form built by
    metrics._build_constituent_z:

        {"k": [...], "sig": [...], "tiers": [...], "v": [[...], ...]}

    where value index = signal_index * len(tiers) + tier_index. The signal and
    tier order are read from the ROW rather than from TIERS, so a row written
    under a different layout still decodes against its own header instead of
    being silently misread.

    `null` at a position is a tier with no value — skipped, not counted as a
    non-spike. A constituent with no value anywhere is absent from `k`/`v`.
    """
    if isinstance(constituent_z, str):
        try:
            constituent_z = json.loads(constituent_z) if constituent_z else {}
        except (ValueError, TypeError):
            return 0, 0, 0
    if not isinstance(constituent_z, dict):
        return 0, 0, 0

    signals = constituent_z.get('sig') or []
    tiers = constituent_z.get('tiers') or []
    rows = constituent_z.get('v') or []
    if signal not in signals or tier not in tiers:
        return 0, 0, 0
    idx = signals.index(signal) * len(tiers) + tiers.index(tier)

    high = low = evaluated = 0
    for row in rows:
        if not isinstance(row, (list, tuple)) or idx >= len(row):
            continue
        z = row[idx]
        if z is None:
            continue
        try:
            zf = float(z)
        except (TypeError, ValueError):
            continue
        evaluated += 1
        if zf > threshold:
            high += 1
        elif zf < -threshold:
            low += 1
    return high, low, evaluated


def _index_xrange(ts, hours: int):
    """The x-range of an index mini-chart: exactly the chart-window pill's
    hours, or the whole fetched day for the 1d pill (24). These charts used to
    pass `None if hours > 8 else 2`, so 1h, 4h and 8h all drew 2 hours."""
    return _padded_xrange(ts, window_hours=None if hours >= 24 else hours)


# The six index mini-chart builders take the card's ONE history frame
# (get_index_breadth_history, queried once per render by build_index_card)
# and work on a copy of it — they add and drop columns.

def build_index_breadth_chart(history: pd.DataFrame, hours: int = 8,
                              z_threshold: float = None,
                              tier: str = '90d') -> go.Figure:
    """Small breadth time-series chart for one index card."""
    df = history.copy()
    if df.empty:
        # No chart-internal title. Outer _mini_chart(label, fig) shows
        # the label above the chart — chart-internal title is redundant.
        return go.Figure(layout={
            **CHART_LAYOUT,
            'height': 360,
            'margin': dict(l=40, r=10, t=10, b=24),
        })

    df['timestamp'] = pd.to_datetime(df['timestamp'], format='ISO8601')
    df['breakouts'] = (df['in_breakout_up'].fillna(0) + df['in_breakout_down'].fillna(0))

    # Recounted per historical row at the LIVE threshold, not read from a
    # stored count. This is the half of the change that made storing raw
    # per-constituent z necessary rather than merely tidier: the breadth grid
    # could have been recounted from LATEST_METRICS, but this chart plots
    # history, and a stored count cannot be re-thresholded after the fact.
    # Moving the slider now redraws this line too.
    thr = float(z_threshold) if z_threshold is not None else _Z_HIGHLIGHT
    df['vol_spikes'] = df['constituent_z'].apply(
        lambda cz: sum(_count_z_spikes(cz, 'block_volume_z', tier, thr)[:2])
    )

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df['timestamp'], y=df['advancers'],
        mode='lines', name='Adv',
        line=dict(color='#5fb87c', width=1.5),
    ))
    fig.add_trace(go.Scatter(
        x=df['timestamp'], y=df['decliners'],
        mode='lines', name='Dec',
        line=dict(color='#d94560', width=1.5),
    ))
    fig.add_trace(go.Scatter(
        x=df['timestamp'], y=df['breakouts'],
        mode='lines', name='Brk',
        line=dict(color='#e8a447', width=1.2),
    ))
    fig.add_trace(go.Scatter(
        x=df['timestamp'], y=df['vol_spikes'],
        mode='lines', name='VolSpk',
        line=dict(color='#7a9cc4', width=1.2),
    ))
    fig.add_trace(go.Scatter(
        x=df['timestamp'], y=df['level_breached_today'],
        mode='lines', name='Brch',
        line=dict(color='#a78bfa', width=1.2, dash='dot'),
    ))

    fig.update_layout(**{
        **CHART_LAYOUT,
        'height': 360,
        'margin': dict(l=40, r=10, t=10, b=24),
        'showlegend': True,
        'legend': dict(orientation='h', yanchor='bottom', y=1.02,
                       xanchor='right', x=1, font=dict(size=9)),
        'yaxis_title': '',
        'xaxis_range': _index_xrange(df['timestamp'], hours),
    })
    # Strip dates from index mini-chart x-axis — staleness surfaces in the
    # dashboard header, so the axis carries only time-of-day.
    fig.update_xaxes(tickformat='%H:%M', nticks=5)
    return fig


def build_index_dispersion_chart(history: pd.DataFrame, hours: int = 8) -> go.Figure:
    """Small chart of weight-aware cross-sectional dispersion for one index."""
    df = history.copy()
    if df.empty or 'cross_sectional_dispersion' not in df.columns:
        return go.Figure(layout={
            **CHART_LAYOUT,
            'height': 360,
            'margin': dict(l=40, r=10, t=10, b=24),
        })

    df = df.dropna(subset=['cross_sectional_dispersion'])
    if df.empty:
        return go.Figure(layout={
            **CHART_LAYOUT,
            'height': 360,
            'margin': dict(l=40, r=10, t=10, b=24),
        })

    df['timestamp'] = pd.to_datetime(df['timestamp'], format='ISO8601')

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df['timestamp'], y=df['cross_sectional_dispersion'],
        mode='lines', name='Dispersion',
        line=dict(color='#a78bfa', width=1.5),
        fill='tozeroy', fillcolor='rgba(90, 50, 134, 0.12)',
        showlegend=False,
    ))

    fig.update_layout(**{
        **CHART_LAYOUT,
        'height': 360,
        'margin': dict(l=40, r=10, t=10, b=24),
        'yaxis_title': '',
        'xaxis_range': _index_xrange(df['timestamp'], hours),
    })
    # Strip dates from index mini-chart x-axis — staleness surfaces in the
    # dashboard header, so the axis carries only time-of-day.
    fig.update_xaxes(tickformat='%H:%M', nticks=5)
    return fig


def build_index_wvv_chart(history: pd.DataFrame, hours: int = 8, resample: str = '30s',
                          tier: str = '90d') -> go.Figure:
    """Small chart of weighted volume velocity for one index, at `tier` —
    the settings drawer's selected z-tier, like the card row above it."""
    col = f'weighted_volume_velocity_{tier}'
    df = history.copy()
    if df.empty or col not in df.columns:
        return go.Figure(layout={
            **CHART_LAYOUT,
            'height': 360,
            'margin': dict(l=40, r=10, t=10, b=24),
        })

    df = df.dropna(subset=[col])
    if df.empty:
        return go.Figure(layout={
            **CHART_LAYOUT,
            'height': 360,
            'margin': dict(l=40, r=10, t=10, b=24),
        })

    df['timestamp'] = pd.to_datetime(df['timestamp'], format='ISO8601')
    if resample != '30s':
        freq = '1min' if resample == '1m' else '5min'
        df = (df
              .set_index('timestamp')
              .resample(freq)
              .mean(numeric_only=True)
              .reset_index()
              .dropna(subset=[col]))

    fig = go.Figure()
    # Velocity can go negative (net selling); use the existing pos/neg fill helper
    for trace in _make_signed_line_traces(df['timestamp'], df[col], 'WVV'):
        fig.add_trace(trace)

    fig.update_layout(**{
        **CHART_LAYOUT,
        'height': 360,
        'margin': dict(l=40, r=10, t=10, b=24),
        'yaxis_title': '',
        'xaxis_range': _index_xrange(df['timestamp'], hours),
    })
    # Strip dates from index mini-chart x-axis — staleness surfaces in the
    # dashboard header, so the axis carries only time-of-day.
    fig.update_xaxes(tickformat='%H:%M', nticks=5)
    return fig


# ============================================================================
# Three more index mini-chart builders: weighted intraday return, weighted vs
# ex-top5, weighted VWAP deviation. Layout wiring lives in build_index_card
# below.
# ============================================================================

def build_index_weighted_return_chart(history: pd.DataFrame, hours: int = 8) -> go.Figure:
    """Clean single-line view of `weighted_intraday_return` over time.
    Sign-coloured via the shared signed-line helper (green above zero, red below)."""
    df = history.copy()
    if df.empty or 'weighted_intraday_return' not in df.columns:
        return go.Figure(layout={
            **CHART_LAYOUT, 'height': 360,
            'margin': dict(l=40, r=10, t=10, b=24),
        })
    df = df.dropna(subset=['weighted_intraday_return'])
    if df.empty:
        return go.Figure(layout={
            **CHART_LAYOUT, 'height': 360,
            'margin': dict(l=40, r=10, t=10, b=24),
        })
    df['timestamp'] = pd.to_datetime(df['timestamp'], format='ISO8601')
    fig = go.Figure()
    for trace in _make_signed_line_traces(df['timestamp'], df['weighted_intraday_return'], 'WIR'):
        fig.add_trace(trace)
    fig.update_layout(**{
        **CHART_LAYOUT, 'height': 360,
        'margin': dict(l=40, r=10, t=10, b=24),
        'yaxis_title': '',
        'xaxis_range': _index_xrange(df['timestamp'], hours),
    })
    # Strip dates from index mini-chart x-axis — staleness surfaces in the
    # dashboard header, so the axis carries only time-of-day.
    fig.update_xaxes(tickformat='%H:%M', nticks=5)
    return fig


def build_index_weighted_return_ex_top5_chart(history: pd.DataFrame, hours: int = 8) -> go.Figure:
    """Overlay: `weighted_intraday_return` + `ex_top5_intraday_return`.
    Spread between the two lines reveals mega-cap dependence: a wide gap means
    the index move is dominated by the 5 heaviest constituents; a narrow gap
    means broad-based movement across the constituent set."""
    df = history.copy()
    if df.empty or 'weighted_intraday_return' not in df.columns:
        return go.Figure(layout={
            **CHART_LAYOUT, 'height': 360,
            'margin': dict(l=40, r=10, t=10, b=24),
        })
    df = df.dropna(subset=['weighted_intraday_return'])
    if df.empty:
        return go.Figure(layout={
            **CHART_LAYOUT, 'height': 360,
            'margin': dict(l=40, r=10, t=10, b=24),
        })
    df['timestamp'] = pd.to_datetime(df['timestamp'], format='ISO8601')
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df['timestamp'], y=df['weighted_intraday_return'],
        mode='lines', name='Weighted',
        line=dict(color='#f5e9d3', width=1.5),
    ))
    if 'ex_top5_intraday_return' in df.columns:
        fig.add_trace(go.Scatter(
            x=df['timestamp'], y=df['ex_top5_intraday_return'],
            mode='lines', name='Ex-Top5',
            line=dict(color='#a58fa8', width=1.5, dash='dash'),
        ))
    fig.add_hline(y=0, line_dash='dot', line_color='#7a6e62', line_width=1)
    fig.update_layout(**{
        **CHART_LAYOUT, 'height': 360,
        'margin': dict(l=40, r=10, t=10, b=24),
        'showlegend': False,  # legend info encoded in the outer mini-chart label
        'yaxis_title': '',
        'xaxis_range': _index_xrange(df['timestamp'], hours),
    })
    # Strip dates from index mini-chart x-axis — staleness surfaces in the
    # dashboard header, so the axis carries only time-of-day.
    fig.update_xaxes(tickformat='%H:%M', nticks=5)
    return fig


def build_index_weighted_vwap_dev_chart(history: pd.DataFrame, hours: int = 8) -> go.Figure:
    """`weighted_vwap_deviation` over time, sign-coloured. Positive =
    constituents collectively above their session VWAP (regime bias to upside);
    negative = below VWAP (regime bias to downside)."""
    df = history.copy()
    if df.empty or 'weighted_vwap_deviation' not in df.columns:
        return go.Figure(layout={
            **CHART_LAYOUT, 'height': 360,
            'margin': dict(l=40, r=10, t=10, b=24),
        })
    df = df.dropna(subset=['weighted_vwap_deviation'])
    if df.empty:
        return go.Figure(layout={
            **CHART_LAYOUT, 'height': 360,
            'margin': dict(l=40, r=10, t=10, b=24),
        })
    df['timestamp'] = pd.to_datetime(df['timestamp'], format='ISO8601')
    fig = go.Figure()
    for trace in _make_signed_line_traces(df['timestamp'], df['weighted_vwap_deviation'], 'WVD'):
        fig.add_trace(trace)
    fig.update_layout(**{
        **CHART_LAYOUT, 'height': 360,
        'margin': dict(l=40, r=10, t=10, b=24),
        'yaxis_title': '',
        'xaxis_range': _index_xrange(df['timestamp'], hours),
    })
    # Strip dates from index mini-chart x-axis — staleness surfaces in the
    # dashboard header, so the axis carries only time-of-day.
    fig.update_xaxes(tickformat='%H:%M', nticks=5)
    return fig


_INDEX_CARD_LABEL_STYLE = {
    'fontSize': '11px',
    'letterSpacing': '1.5px',
    'textTransform': 'uppercase',
    'color': '#b8a99b',
    'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
    'fontWeight': '500',
    'marginTop': '12px',
    'marginBottom': '7px',
}
_INDEX_CARD_LINE_STYLE = {
    'fontSize': '12px',
    'margin': '2px 0',
    'color': '#f5e9d3',
    'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
    'lineHeight': '1.5',
}


def _fmt_index_pct(v, sign=True):
    """Index-card percentage: optional explicit sign, 2 decimals, "—" if absent.

    Named apart from dashboard_signals._fmt_pct, which renders an absent value
    as "N/A" — same name, different output, was a trap for whoever called one
    expecting the other."""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    return (f"{f:+.2f}%" if sign else f"{f:.2f}%")


def _fmt_bps(v):
    if v is None:
        return "—"
    try:
        return f"{float(v):+.1f} bps"
    except (TypeError, ValueError):
        return "—"


# ---- Index card layout primitives (three outer columns, each with an inner
# grid of equal-width sub-columns; centered text; equal padding). ----

_INDEX_INNER_GRID_BASE = {
    'display': 'grid',
    'alignItems': 'center',
    'columnGap': '0',
    'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
    'fontVariantNumeric': 'tabular-nums',
}

_INDEX_CELL_BASE = {
    'textAlign': 'center',
    'padding': '6px 10px',
    'whiteSpace': 'nowrap',
    'overflow': 'hidden',
    'textOverflow': 'ellipsis',
}

_INDEX_LABEL_CELL_STYLE = {
    **_INDEX_CELL_BASE,
    'color': '#8a7864',
    'fontSize': '11px',
    'letterSpacing': '0.3px',
}

_INDEX_VALUE_CELL_STYLE = {
    **_INDEX_CELL_BASE,
    'color': '#f5e9d3',
    'fontSize': '13px',
}

_INDEX_HEADER_CELL_STYLE = {
    **_INDEX_CELL_BASE,
    'color': '#8a7864',
    'fontSize': '10.5px',
    'letterSpacing': '0.6px',
    'textTransform': 'uppercase',
    'fontWeight': '500',
    'borderBottom': '1px solid #3a3128',
    'paddingBottom': '6px',
    'marginBottom': '4px',
}


def _breadth_row(up_label, up_val, dn_label, dn_val):
    """4 equal sub-columns: up-label | up-value | down-label | down-value.
    Every cell uses the same style as Movers data rows (13px f5e9d3)."""
    return html.Div([
        html.Div(up_label, style=_INDEX_VALUE_CELL_STYLE),
        html.Div(str(up_val), style=_INDEX_VALUE_CELL_STYLE),
        html.Div(dn_label, style=_INDEX_VALUE_CELL_STYLE),
        html.Div(str(dn_val), style=_INDEX_VALUE_CELL_STYLE),
    ], style={**_INDEX_INNER_GRID_BASE,
              'gridTemplateColumns': 'repeat(4, minmax(0, 1fr))'})


def _movers_row(cells, *, is_header=False):
    """4 equal sub-columns: symbol | wt% | intraday% | bps."""
    cell_style = _INDEX_HEADER_CELL_STYLE if is_header else _INDEX_VALUE_CELL_STYLE
    return html.Div(
        [html.Div(c, style=cell_style) for c in cells],
        style={**_INDEX_INNER_GRID_BASE,
               'gridTemplateColumns': 'repeat(4, minmax(0, 1fr))'},
    )


def _movers_block(top_rows, bottom_rows):
    """Header + 5 contributor rows + dotted divider + 5 detractor rows.
    Each cell centered in its column; equal widths; equal padding."""
    def _fmt_rows(rows):
        out = []
        for r in rows:
            sym  = (r.get('symbol') or '')[:12]
            wt   = r.get('weight_pct')
            ipct = r.get('intraday_pct')
            bps  = r.get('contribution_bps')
            out.append(_movers_row([
                sym,
                f"{wt:.2f}%" if wt is not None else "—",
                _fmt_index_pct(ipct),
                _fmt_bps(bps),
            ]))
        return out

    children = [
        html.Div("Top contributors", style=_INDEX_SUBSECTION_HEADER_STYLE),
        _movers_row(["Symbol", "Wt%", "Δ%", "bps"], is_header=True),
    ]
    children.extend(_fmt_rows(top_rows or []))
    children.append(html.Div(style={
        'borderTop': '1px dashed #3a3128',
        'marginTop': '6px',
        'marginBottom': '6px',
    }))
    children.append(html.Div("Bottom detractors", style=_INDEX_SUBSECTION_HEADER_STYLE))
    children.append(_movers_row(["Symbol", "Wt%", "Δ%", "bps"], is_header=True))
    children.extend(_fmt_rows(bottom_rows or []))
    return html.Div(children)


def _weighted_row(label, value):
    """2 equal sub-columns: label | value.
    Every cell uses the same style as Movers data rows (13px f5e9d3)."""
    return html.Div([
        html.Div(label, style=_INDEX_VALUE_CELL_STYLE),
        html.Div(value, style=_INDEX_VALUE_CELL_STYLE),
    ], style={**_INDEX_INNER_GRID_BASE,
              'gridTemplateColumns': 'repeat(2, minmax(0, 1fr))'})


def build_index_card(summary, hours: int = 8, resample: str = '30s',
                     z_threshold: float = None, tier: str = '90d'):
    """Build a single index card from one IndexAggregator summary dict.

    Layout: three outer columns — Breadth-stats + Weighted-stats stacked
    (col 1) · Movers (col 2, header + 5 contributors + dotted line + 5
    detractors) · Breadth chart + Dispersion chart stacked (col 3). Below
    that, a 2x2 grid of the remaining "weighted" mini-charts (volume
    velocity, intraday return, weighted-vs-ex-top5, VWAP deviation). Card's
    internal header (name + price + intraday%) removed — pill toggle at
    drawer top identifies the index."""
    name = summary.get('index_name', '?')
    intraday = summary.get('intraday_pct')

    # Accent border by intraday direction (kept — card identity signal).
    if intraday is not None and intraday > 0:
        accent = '#5fb87c'
    elif intraday is not None and intraday < 0:
        accent = '#d94560'
    else:
        accent = '#3a3128'

    # Mini-chart helper — used both by stats_grid's column 3 (Breadth +
    # Dispersion charts) and by mini_grid below (the 4 weighted charts).
    _MINI_CHART_HEIGHT = 200

    def _mini_chart(label, fig: go.Figure) -> html.Div:
        """`label` may be a string OR an html.Div (for title-encoded legend
        cases like weighted-vs-ex-top5)."""
        fig.update_layout(height=_MINI_CHART_HEIGHT)
        if isinstance(label, str):
            label_node = html.Div(label, style=_INDEX_CARD_LABEL_STYLE)
        else:
            label_node = label
        return html.Div([
            label_node,
            dcc.Graph(
                figure=fig,
                config={'displayModeBar': False},
                style={'height': f'{_MINI_CHART_HEIGHT}px'},
            ),
        ], style={'minWidth': 0})

    # ONE query for all six mini-charts (it used to run once per chart). When
    # today has no rows it falls back to the last session, and every chart
    # label says which session that is — as the asset charts do.
    history, fallback_ts = get_index_breadth_history(name, hours=hours)
    _ls = f"Last session: {fallback_ts:%Y-%m-%d} · " if fallback_ts is not None else ""

    adv = summary.get('advancers') or 0
    dec = summary.get('decliners') or 0
    unch = summary.get('unchanged') or 0
    abv = summary.get('above_vwap') or 0
    blw = summary.get('below_vwap') or 0
    bk_up = summary.get('in_breakout_up') or 0
    bk_dn = summary.get('in_breakout_down') or 0
    # Counted HERE, at the live slider threshold, not read from a stored count.
    # The metrics thread cannot see a per-browser setting, so it stores each
    # constituent's z and the counting happens where the threshold is known.
    #
    # Rolling Momentum session-return z is the statistical counterpart to
    # advancers/decliners: those count direction, this counts how many moved
    # unusually far for this time of day.
    _thr = float(z_threshold) if z_threshold is not None else _Z_HIGHLIGHT
    _cz = summary.get('constituent_z') or {}
    vh, vl, _vol_evaluated = _count_z_spikes(_cz, 'block_volume_z', tier, _thr)
    srz_hi, srz_lo, _srz_evaluated = _count_z_spikes(_cz, 'session_return_z', tier, _thr)
    breached = summary.get('level_breached_today') or 0
    # Per tier: a stock's RS sign can differ between a 22-day beta and a
    # 252-day one. Pinned to 90d, this row answered a 90d question while
    # every other row on the card followed the trader's selection.
    prs = summary.get(f'positive_rs_{tier}') or 0
    nrs = summary.get(f'negative_rs_{tier}') or 0

    # Breadth block — 4 sub-columns × 7 rows.
    breadth = _subsection("Breadth", [
        _breadth_row("Advancers",     adv, "Decliners",     dec),
        _breadth_row("Above VWAP",    abv, "Below VWAP",    blw),
        _breadth_row("Breakout ↑",    bk_up, "Breakout ↓",  bk_dn),
        _breadth_row("Vol spike +",   vh, "Vol spike −",    vl),
        _breadth_row("R.Mom spike +", srz_hi, "R.Mom spike −", srz_lo),
        _breadth_row("Positive RS",   prs, "Negative RS",   nrs),
        _breadth_row("Unchanged",     unch, "Breached",     breached),
    ])

    # Movers block — 4 sub-columns × (header + 5 contributors + divider + 5 detractors).
    movers = _movers_block(
        summary.get('top_contributors') or [],
        summary.get('bottom_detractors') or [],
    )

    # Weighted block — 2 sub-columns × 5 rows.
    def _fmt_num(v):
        return f"{v:+.2f}" if v is not None else "—"

    wir = summary.get('weighted_intraday_return')
    # Tier-suffixed: weighted averages, so they stay server-side and simply
    # have four tiers. Follow the same `tier` the counts above use.
    wvv = summary.get(f'weighted_volume_velocity_{tier}')
    wvz = summary.get(f'weighted_volume_zscore_{tier}')
    wvd = summary.get('weighted_vwap_deviation')
    ex5 = summary.get('ex_top5_intraday_return')
    weighted = _subsection("Weighted", [
        _weighted_row("Intraday return", _fmt_index_pct(wir)),
        _weighted_row("Ex-Top5 return",  _fmt_index_pct(ex5)),
        _weighted_row("Volume velocity", _fmt_num(wvv)),
        _weighted_row("Volume z-score",  _fmt_num(wvz)),
        _weighted_row("VWAP deviation",  _fmt_index_pct(wvd)),
    ])

    # 3 outer columns: Breadth-stats + Weighted-stats | Movers | Breadth
    # chart + Dispersion chart, equal width.
    stats_grid = html.Div(
        [
            html.Div([breadth, weighted]),
            html.Div([movers]),
            html.Div([
                _mini_chart(f"{_ls}Breadth",
                            build_index_breadth_chart(
                                history, hours=hours, z_threshold=_thr, tier=tier)),
                _mini_chart(f"{_ls}Dispersion",
                            build_index_dispersion_chart(history, hours=hours)),
            ]),
        ],
        style={
            'display': 'grid',
            'gridTemplateColumns': 'repeat(3, minmax(0, 1fr))',
            'gap': '24px',
            'marginBottom': '14px',
        },
    )

    # Title-encoded legend for the weighted-vs-ex-top5 chart. The two
    # colored spans match the two Scatter traces so the trader reads which
    # line is which without wasting horizontal space on a Plotly side legend.
    _wvet_label = html.Div([
        html.Span(_ls, style={'color': '#7a6e62'}),
        html.Span('WEIGHTED', style={'color': '#f5e9d3'}),
        html.Span(' vs ', style={'color': '#7a6e62'}),
        html.Span('EX-TOP5', style={'color': '#a58fa8'}),
    ], style=_INDEX_CARD_LABEL_STYLE)

    # Mini-chart grid — 2x2, the 4 remaining "weighted" charts (Breadth and
    # Dispersion moved into stats_grid's column 3 above). Each tile is wider
    # than before now that it only shares the row with one other column.
    mini_grid = html.Div(
        [
            _mini_chart(f"{_ls}Wtd volume velocity",
                        build_index_wvv_chart(history, hours=hours, resample=resample,
                                              tier=tier)),
            _mini_chart(f"{_ls}Wtd intraday return",
                        build_index_weighted_return_chart(history, hours=hours)),
            _mini_chart(_wvet_label,
                        build_index_weighted_return_ex_top5_chart(history, hours=hours)),
            _mini_chart(f"{_ls}Wtd VWAP deviation",
                        build_index_weighted_vwap_dev_chart(history, hours=hours)),
        ],
        style={
            'display': 'grid',
            'gridTemplateColumns': 'repeat(2, minmax(0, 1fr))',
            'gap': '12px',
            'marginTop': '8px',
        },
    )

    # Internal header removed — pill toggle above the drawer body identifies
    # the selected index. Top accent border remains as the card-identity signal.
    return html.Div(
        [stats_grid, mini_grid],
        style={
            'padding': '17px 17px 15px 17px',
            'backgroundColor': '#231b15',
            'borderTop': f'2px solid {accent}',
            'minWidth': '0',
        },
    )


# Initialize Dash app. The Bootstrap stylesheet and both typefaces are bundled
# in assets/ (Dash loads every .css file there automatically), so the page
# fetches nothing from outside this app.
app = Dash(__name__, title='TA Gyaani', suppress_callback_exceptions=True)

# Required by dash_auth for signed session cookies — random per startup, in-memory only.
app.server.secret_key = secrets.token_hex(32)

# HTTP Basic Auth — credentials read from .env (DASHBOARD_USER, DASHBOARD_PASSWORD).
# Fail-closed: missing credentials raise at import time so the container refuses to start.
_DASHBOARD_USER = os.getenv("DASHBOARD_USER")
_DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
if not (_DASHBOARD_USER and _DASHBOARD_PASSWORD):
    raise RuntimeError("DASHBOARD_USER and DASHBOARD_PASSWORD must be set in .env to start the dashboard")
import dash_auth
dash_auth.BasicAuth(app, {_DASHBOARD_USER: _DASHBOARD_PASSWORD})
logger.info("Dashboard Basic Auth enabled")

# Security headers (defense-in-depth on top of loopback bind + Basic Auth).
@app.server.after_request
def _security_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "http://localhost:8054"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    response.headers.pop("Server", None)
    return response

app.index_string = '''<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>{%title%}</title>
        {%favicon%}
        {%css%}
        <style>
            :root {
                --paper: #1a1410;
                --surface: #231b15;
                --ink: #f5e9d3;
                --ink-soft: #b8a99b;
                --ink-faint: #7a6e62;
                --hairline: #3a3128;
                --hairline-soft: #2f2820;
                --crimson: #d94560;
                --crimson-ink: #a83548;
                --forest: #5fb87c;
                --row-breakout: #4a3614;
                --row-zscore: #1f2d44;
                --row-breach: #4a242c;
            }
            html, body, #react-entry-point, ._dash-app-content {
                background-color: var(--paper) !important;
                color: var(--ink);
                margin: 0;
                padding: 0;
                font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
                -webkit-font-smoothing: antialiased;
                -moz-osx-font-smoothing: grayscale;
            }
            .serif, .serif * { font-family: 'Fraunces', Georgia, 'Times New Roman', serif; }
            /* DataTable overrides */
            .dash-table-container .dash-spreadsheet-container {
                background-color: var(--paper) !important;
            }
            .dash-table-container .dash-spreadsheet-inner table {
                border-collapse: collapse !important;
            }
            .dash-table-container .dash-spreadsheet-inner th,
            .dash-table-container .dash-spreadsheet-inner td {
                border: none !important;
                border-bottom: 1px solid var(--hairline-soft) !important;
            }
            .dash-table-container .dash-spreadsheet-inner th {
                border-bottom: 1px solid var(--hairline) !important;
            }
            .dash-filter input { background-color: var(--paper) !important; color: var(--ink) !important; }
            /* Scrollbar */
            ::-webkit-scrollbar { width: 10px; height: 10px; }
            ::-webkit-scrollbar-track { background: var(--paper); }
            ::-webkit-scrollbar-thumb { background: var(--hairline); border-radius: 0; }
            ::-webkit-scrollbar-thumb:hover { background: var(--ink-faint); }
            /* Link hover */
            a:hover { color: var(--crimson-ink) !important; }
            /* Selection */
            ::selection { background: var(--crimson); color: var(--paper); }
        </style>
    </head>
    <body>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>'''

# App layout
app.layout = html.Div([
    # Session-persisted stores: selected asset, collapse open state, and chart settings
    dcc.Store(id='selected-asset-store', storage_type='session'),
    dcc.Store(id='charts-collapse-store', storage_type='session', data=False),
    dcc.Store(id='indices-collapse-store', storage_type='session', data=False),
    dcc.Store(id='velocity-resample-store', storage_type='session', data='30s'),

    # Auto-refresh interval (30 seconds)
    dcc.Interval(
        id='metrics-interval',
        interval=_INTERVAL * 1000,
        n_intervals=0
    ),

    # Header — editorial masthead: paper bg, hairline underneath, Fraunces italic title
    html.Div([
        # LEFT cell (1fr): Calculator health bar
        html.Div(id='health-bar', style={
            'fontSize': '11px',
            'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
            'padding': '3px 17px',
            'display': 'flex',
            'flexWrap': 'wrap',
            'alignItems': 'center',
            'rowGap': '5px',
            'columnGap': '16px',
            'letterSpacing': '0.5px',
            'textTransform': 'uppercase',
            'justifySelf': 'start',
        }),

        # CENTER cell (auto): Title — Fraunces italic masthead
        html.H1("TA Gyaani", style={
            'color': '#f5e9d3',
            'margin': '0',
            'fontSize': '24px',
            'fontWeight': '300',
            'fontStyle': 'italic',
            'letterSpacing': '-0.5px',
            'fontFamily': "'Fraunces', Georgia, 'Times New Roman', serif",
            'justifySelf': 'center',
            'lineHeight': '1',
        }),

        # RIGHT cell (1fr): staleness indicator + settings + link
        html.Div([
            html.Span(id='staleness-indicator', style={
                'fontSize': '11px',
                'marginRight': '14px',
                'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
                'letterSpacing': '0.5px',
                'textTransform': 'uppercase',
            }),
            # ⚙ icon opens the settings drawer (offcanvas below).
            html.Button(
                '⚙',
                id='settings-drawer-toggle',
                n_clicks=0,
                className='settings-gear-button',
                title='Settings',
            ),
            html.A("ChorBazaar",
                href="https://chorbazaar.co.in",
                target="_blank",
                style={
                    'color': '#d94560',
                    'fontSize': '20px',
                    'fontWeight': '500',
                    'fontStyle': 'italic',
                    'fontFamily': "'Fraunces', Georgia, serif",
                    'textDecoration': 'none',
                    'transition': 'color 0.2s ease',
                    'marginRight': '14px',
                    'letterSpacing': '-0.2px',
                })
        ], style={'display': 'flex', 'alignItems': 'center', 'justifySelf': 'end'})
    ], style={
        'flex': '0 0 auto',
        'backgroundColor': '#1a1410',
        'padding': '22px 17px 18px 17px',
        'marginBottom': '0',
        'borderBottom': '1px solid #3a3128',
        'display': 'grid',
        'gridTemplateColumns': '1fr auto 1fr',
        'alignItems': 'center',
    }),

    # Cold-start status strip. Auto-hides once historical.db
    # has >= 252 trading days.
    #
    # An EMPTY placeholder, filled by _refresh_cold_start_strip below. It used
    # to call build_cold_start_strip() directly here — but this layout is built
    # while dashboard.py is still being IMPORTED, which is before
    # MetricsOrchestrator has created historical.db, so the query always failed
    # on a fresh install and the strip was stuck at "Historical: 0d" forever.
    # Filling it from a callback moves the read to render time, when the file
    # exists, and lets the count follow a backfill instead of freezing at boot.
    html.Div(id='cold-start-strip'),

    # Main content: the scores table (the detail panel is the slide-in drawer below)
    html.Div([
        # The main table takes the full row width. The detail panel lives
        # inside the dbc.Offcanvas at layout root and opens as a right-side
        # overlay on asset click (or manually via the middle-vertical arrow
        # toggle).
        html.Div([
            # dcc.Store for the settings controls (localStorage persistence).
            dcc.Store(id='store-z-threshold', storage_type='local', data=_Z_HIGHLIGHT),
            dcc.Store(id='store-chart-window', storage_type='local', data=2),
            # Active z-score tier. `storage_type='local'` so the
            # trader's choice survives refresh and container restart, matching
            # the other two settings stores. Initial value is the longest tier
            # the system actually has data for.
            # data=None, NOT default_active_tier(): that call reads
            # historical.db, and this layout is built at IMPORT time, before
            # the file exists. Every consumer already resolves the default at
            # request time via `active_tier if active_tier in _TIER_VALUES
            # else default_active_tier()`, so None simply means "not chosen
            # yet" and the correct default is computed on first render.
            dcc.Store(id='store-active-tier', storage_type='local',
                      data=None),
            # Active benchmark for the RS chart's 4-tier α-z view.
            # Session-scoped so switching stocks (each with different
            # benchmark sets) resets cleanly; not local-persisted since the
            # right default depends on the current stock.
            dcc.Store(id='store-rs-benchmark', storage_type='session', data=None),
            # Which index card renders in the indices drawer. Local so it
            # survives page refresh + container restart.
            dcc.Store(id='store-selected-index', storage_type='local', data='NIFTY'),
            create_main_table(),
        ],
            id='main-table-container',
            style={
                'flex': '1',
                'minWidth': '0',
                'padding': '8px'
            }
        ),
    ], style={
        'display': 'flex',
        'gap': '18px',
        'padding': '10px',
        'paddingBottom': '120px',
        'overflow': 'visible'
    }),

    # Fixed-bottom drawer wrapper (Indices + Charts) — spans full viewport width
    html.Div(
        id='charts-drawer-wrapper',
        style={
            'position': 'fixed',
            'bottom': 0,
            'left': 0,
            'right': 0,
            'zIndex': 1000,
            'pointerEvents': 'none',
        },
        children=html.Div(
            style={'display': 'flex', 'gap': '16px', 'padding': '8px'},
            children=[
                # Drawer column — fills the full row.
                html.Div(
                    style={
                        'flex': '1',
                        'minWidth': 0,
                        'padding': '0 8px',
                        'pointerEvents': 'auto',
                        'display': 'flex',
                        'flexDirection': 'column',
                    },
                    children=[
                        # Indices drawer handle (DOM position #1 — sits above charts handle)
                        html.Div(
                            id='indices-collapse-toggle',
                            n_clicks=0,
                            children='▸ INDICES',
                            style={
                                'cursor': 'pointer',
                                'padding': '14px 18px',
                                'fontSize': '11px',
                                'fontWeight': 500,
                                'letterSpacing': '3px',
                                'color': '#1a1410',
                                'backgroundColor': '#5fb87c',
                                'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
                                'userSelect': 'none',
                                'borderBottom': '1px solid #3d8f5f',
                            },
                        ),
                        # Indices drawer body (DOM position #2)
                        dbc.Collapse(
                            id='indices-collapse',
                            is_open=False,
                            children=html.Div([
                                # Index-pill selector strip (NIFTY / BANKNIFTY / MIDCAP /
                                # SENSEX). Reuses `.chart-window-pill` CSS. Sits outside
                                # `indices-drawer-body` so the 30-sec rebuild doesn't reset the
                                # active state. `store-selected-index` drives which pill is
                                # active AND which card renders below.
                                html.Div([
                                    html.Button(
                                        name,
                                        id={'type': 'index-pill', 'name': name},
                                        n_clicks=0,
                                        className='chart-window-pill inactive',
                                    )
                                    for name in ('NIFTY', 'BANKNIFTY', 'MIDCAP', 'SENSEX')
                                ],
                                    className='chart-window-pill-group',
                                    style={
                                        'padding': '8px 16px',
                                        'backgroundColor': '#231b15',
                                        'borderBottom': '1px solid #3a3128',
                                        'gap': '6px',
                                        'display': 'flex',
                                    },
                                ),
                                # Dynamic content rebuilt every 30 sec (single index card).
                                # Height: 66vh (20% cut from prior 82vh) — panel no
                                # longer swallows the entire dashboard viewport.
                                # overflowX dropped — one card fills the width, no
                                # horizontal scroll ever.
                                html.Div(
                                    id='indices-drawer-body',
                                    style={
                                        'maxHeight': '66vh',
                                        'overflowY': 'auto',
                                        'padding': '16px',
                                        'backgroundColor': '#231b15',
                                        'boxShadow': '0 -8px 24px rgba(26, 26, 26, 0.08)',
                                        'borderTop': '1px solid #3a3128',
                                    },
                                ),
                            ], style={'backgroundColor': '#231b15'}),
                        ),
                        # Charts drawer handle (always visible; DOM position #3)
                        html.Div(
                            id='charts-collapse-toggle',
                            n_clicks=0,
                            children='▸ CHARTS',
                            style={
                                'cursor': 'pointer',
                                'padding': '14px 18px',
                                'fontSize': '11px',
                                'fontWeight': 500,
                                'letterSpacing': '3px',
                                'color': '#1a1410',
                                'backgroundColor': '#d94560',
                                'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
                                'userSelect': 'none',
                                'borderBottom': '1px solid #a83548',
                            },
                        ),
                        # Drawer body (DOM position #4)
                        dbc.Collapse(
                            id='charts-collapse',
                            is_open=False,
                            children=html.Div(
                                [
                                    # 3-tab chart drawer.
                                    # Tab 1 "Price" — full-width composite price chart.
                                    # Tab 2 "Signals" — velocity, acceleration, vwap-dev, momentum (2×2),
                                    #   then rolling momentum on a third row.
                                    # Tab 3 "Relative" — rel-strength + volume-zscores, then divergence
                                    #   on a second row.
                                    # Same 9 figures under the hood; user-visible display
                                    # switches per active tab.
                                    dcc.Store(id='chart-drawer-active-tab',
                                              storage_type='session', data='tab-price'),
                                    # Toolbar row: ATR toggle + Vel/Accel resolution toggle,
                                    # kept above the tabs since they apply globally to relevant charts.
                                    html.Div([
                                        dcc.Checklist(
                                            id='atr-toggle',
                                            options=[{'label': 'Show ATR Band', 'value': 'atr'}],
                                            value=[],
                                            inline=True,
                                            # Presentation lives on labelStyle, NOT on style.
                                            # dcc.Checklist renders
                                            #   <div style=style><label style=labelStyle><input .../>text</label></div>
                                            # so a colour set on `style` only reaches the text by
                                            # inheritance — and the Bootstrap theme sets a colour on
                                            # `label` directly, which beats any inherited value. The
                                            # label rendered in Bootstrap's colour against the dark
                                            # drawer and was effectively invisible. Mirrors the
                                            # velocity-resample-toggle below, which never had the bug
                                            # because it styles its labels the same way.
                                            style={'display': 'inline-flex', 'alignItems': 'center',
                                                   'verticalAlign': 'middle', 'marginRight': '18px'},
                                            labelStyle={'display': 'inline-flex', 'alignItems': 'center',
                                                        'gap': '3px',
                                                        'fontSize': '11px', 'color': '#b8a99b', 'cursor': 'pointer',
                                                        'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
                                                        'letterSpacing': '0.5px', 'textTransform': 'uppercase'},
                                            inputStyle={'margin': '0', 'cursor': 'pointer'},
                                        ),
                                        html.Span(
                                            'VEL/ACCEL RESOLUTION: ',
                                            style={'fontSize': '10px', 'color': '#b8a99b',
                                                   'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
                                                   'letterSpacing': '0.5px', 'verticalAlign': 'middle'}
                                        ),
                                        dcc.RadioItems(
                                            id='velocity-resample-toggle',
                                            options=[
                                                {'label': '30s', 'value': '30s'},
                                                {'label': '1m',  'value': '1m'},
                                                {'label': '5m',  'value': '5m'},
                                            ],
                                            value='30s',
                                            inline=True,
                                            style={'display': 'inline-flex', 'alignItems': 'center',
                                                   'verticalAlign': 'middle'},
                                            labelStyle={'display': 'inline-flex', 'alignItems': 'center',
                                                        'gap': '3px', 'marginRight': '14px',
                                                        'fontSize': '11px', 'color': '#b8a99b', 'cursor': 'pointer',
                                                        'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
                                                        'letterSpacing': '0.5px', 'textTransform': 'uppercase'},
                                            inputStyle={'margin': '0', 'cursor': 'pointer'},
                                        ),
                                    ], style={'marginBottom': '8px', 'paddingBottom': '6px',
                                              'borderBottom': '1px solid #3a3128'}),

                                    dbc.Tabs(
                                        id='chart-drawer-tabs',
                                        active_tab='tab-price',
                                        persistence=True,
                                        persistence_type='session',
                                        persisted_props=['active_tab'],
                                        children=[
                                            dbc.Tab(
                                                label='Price',
                                                tab_id='tab-price',
                                                children=html.Div([
                                                    dcc.Graph(id='chart-price',
                                                              config={'displayModeBar': True, 'scrollZoom': True}),
                                                ], style={'padding': '8px 0'}),
                                            ),
                                            dbc.Tab(
                                                label='Signals',
                                                tab_id='tab-signals',
                                                children=html.Div([
                                                    _two_col_row('chart-velocity', 'chart-acceleration'),
                                                    _two_col_row('chart-vwap-deviation', 'chart-momentum'),
                                                    _two_col_row('chart-rolling-momentum', None),
                                                ], style={'padding': '8px 0'}),
                                            ),
                                            dbc.Tab(
                                                label='Relative',
                                                tab_id='tab-relative',
                                                children=html.Div([
                                                    _two_col_row(
                                                        'chart-rel-strength', 'chart-zscores',
                                                        left_header=html.Div(
                                                            id='rs-benchmark-pill-row',
                                                            style={
                                                                'display': 'flex',
                                                                'gap': '6px',
                                                                'flexWrap': 'wrap',
                                                                'marginBottom': '6px',
                                                                'padding': '2px 4px',
                                                                'minHeight': '24px',
                                                            },
                                                        ),
                                                    ),
                                                    _two_col_row('chart-divergence', None),
                                                ], style={'padding': '8px 0'}),
                                            ),
                                        ],
                                    ),
                                ],
                                style={
                                    # Tuned so Relative tab (2 rows × 280px charts) and the
                                    # Price tab (560px) fit without vertical scroll; Signals
                                    # tab (3 rows, since Rolling Momentum was added) scrolls
                                    # via overflowY below.
                                    'maxHeight': '78vh',
                                    'overflowY': 'auto',
                                    'padding': '16px',
                                    'backgroundColor': '#231b15',
                                    'boxShadow': '0 -8px 24px rgba(26, 26, 26, 0.08)',
                                    'borderTop': '1px solid #3a3128',
                                },
                            ),
                        ),
                    ],
                ),
            ],
        ),
    ),

    # Settings drawer. Themed slider + pill toggle group, opened by
    # the ⚙ button in the header. All previously-inline header controls live
    # here now. Controls' persistence behavior is unchanged — same dcc.Store
    # IDs, same downstream callbacks; only the input widgets moved.
    dbc.Offcanvas(
        [
            # Row 1 — z-threshold slider (mechanism unchanged; only styling).
            html.Div([
                html.Div('Highlight z-threshold ±', className='settings-drawer-label'),
                html.Div(
                    dcc.Slider(
                        id='z-threshold-slider',
                        min=1.5, max=3.0, step=0.1,
                        value=_Z_HIGHLIGHT,
                        marks={float(v): {'label': f'{v}', 'style': {'color': '#7a6e62', 'fontSize': '10px'}}
                               for v in (1.5, 2.0, 2.5, 3.0)},
                        tooltip={'placement': 'top', 'always_visible': False},
                        updatemode='drag',
                        className='themed-slider',
                    ),
                    className='themed-slider-wrap',
                ),
            ], className='settings-drawer-row'),

            # Row 2 — chart-window pill toggle group. Pattern-matched IDs so a
            # single ALL callback resolves the clicked pill's value into
            # store-chart-window.
            html.Div([
                html.Div('Chart window', className='settings-drawer-label'),
                html.Div([
                    html.Button(
                        label,
                        id={'type': 'chart-window-pill', 'value': value},
                        n_clicks=0,
                        className='chart-window-pill',
                    )
                    for label, value in [('1h', 1), ('2h', 2), ('4h', 4), ('8h', 8), ('1d', 24)]
                ], className='chart-window-pill-group'),
            ], className='settings-drawer-row'),

            # Row 3 — z-score tier selector. Same pattern-matched
            # pill mechanism and the same CSS classes as the chart-window group
            # above, so the two read as one family of controls.
            #
            # All four stay enabled at every moment, deliberately. A tier with
            # no data yet shows hyphens rather than being greyed out — the
            # hyphens are the honest answer AND the diagnostic, telling the
            # trader which signals have not accumulated that much history.
            html.Div([
                html.Div('Z-score tier', className='settings-drawer-label'),
                html.Div([
                    html.Button(
                        tier,
                        id={'type': 'tier-pill', 'value': tier},
                        n_clicks=0,
                        className='chart-window-pill',
                    )
                    for tier in _TIER_VALUES
                ], className='chart-window-pill-group'),
            ], className='settings-drawer-row'),
        ],
        id='settings-drawer',
        title='Settings',
        is_open=False,
        placement='end',
        backdrop=True,
        scrollable=True,
        className='settings-drawer',
    ),

    # Right-side detail drawer. Opens automatically on asset click;
    # the middle-vertical arrow toggle button (rendered below at layout root)
    # opens/closes it manually. `backdrop=False` so the scores table stays
    # visible and interactive behind the drawer. `id='detail-panel'` is on
    # the inner body div so the existing `update_detail_panel` callback
    # continues to write into it unchanged.
    dbc.Offcanvas(
        html.Div(id='detail-panel'),
        id='detail-drawer',
        # No title — Bootstrap skips the header block entirely (no built-in X,
        # no wasted vertical space at the top). Panel body starts at drawer top.
        is_open=False,
        placement='end',
        backdrop=False,
        scrollable=True,
        className='detail-drawer',
    ),

    # Middle-vertical arrow toggle. Fixed to the right viewport edge;
    # click flips `detail-drawer.is_open`. Icon reflects current state via
    # the same callback that toggles.
    html.Button(
        '▸',
        id='detail-drawer-toggle',
        n_clicks=0,
        className='detail-drawer-toggle closed',
        title='Toggle detail panel',
    ),

], style={
    'margin': '0',
    'padding': '0'
})


@app.callback(
    Output('metrics-table', 'data'),
    [Input('metrics-interval', 'n_intervals'),
     Input('store-z-threshold', 'data'),
     Input('store-active-tier', 'data')]
)
def update_table_data(n_intervals, z_threshold, active_tier):
    """Update table data every 30 seconds (preserves selection/sorting).
    Also fires when the z-threshold slider changes, so the
    Signals column glyph activation stays in sync with the row-highlight
    threshold.
    And when the tier pill changes, so every displayed z and every
    glyph moves to the selected tier together."""
    rows = get_current_metrics()
    threshold = float(z_threshold) if z_threshold is not None else _Z_HIGHLIGHT
    tier = active_tier if active_tier in _TIER_VALUES else default_active_tier()
    return build_table_data(rows, z_threshold=threshold, tier=tier)


# Slider → localStorage persistence.
@app.callback(
    Output('store-z-threshold', 'data'),
    Input('z-threshold-slider', 'value'),
    prevent_initial_call=True,
)
def _persist_z_threshold(value):
    return value


# Pattern-matched pill toggle → localStorage persistence.
# ctx.triggered_id is the pill's id dict; its 'value' field is the pill's
# window-hours value (1/2/4/8/24). If none triggered, no update.
@app.callback(
    Output('store-chart-window', 'data'),
    Input({'type': 'chart-window-pill', 'value': ALL}, 'n_clicks'),
    prevent_initial_call=True,
)
def _persist_chart_window(_n_clicks_list):
    triggered = ctx.triggered_id
    if not triggered or not isinstance(triggered, dict):
        return no_update
    return triggered.get('value', no_update)


# Reflect current store value into per-pill active/inactive
# className so the visually-active pill matches the persisted state on
# page load and after every click.
@app.callback(
    Output({'type': 'chart-window-pill', 'value': ALL}, 'className'),
    Input('store-chart-window', 'data'),
    State({'type': 'chart-window-pill', 'value': ALL}, 'id'),
)
def _reflect_chart_window_active(current_value, pill_ids):
    return [
        'chart-window-pill active' if pid.get('value') == current_value
        else 'chart-window-pill inactive'
        for pid in pill_ids
    ]


# Tier pills — persist clicked pill's value, reflect active state.
# Deliberately identical in shape to _persist_chart_window /
# _reflect_chart_window_active; the tier selector is a sibling control, not a
# new mechanism.
@app.callback(
    Output('store-active-tier', 'data'),
    Input({'type': 'tier-pill', 'value': ALL}, 'n_clicks'),
    prevent_initial_call=True,
)
def _persist_active_tier(_n_clicks_list):
    triggered = ctx.triggered_id
    if not triggered or not isinstance(triggered, dict):
        return no_update
    return triggered.get('value', no_update)


@app.callback(
    Output({'type': 'tier-pill', 'value': ALL}, 'className'),
    Input('store-active-tier', 'data'),
    State({'type': 'tier-pill', 'value': ALL}, 'id'),
)
def _reflect_active_tier(current_value, pill_ids):
    # Resolve the default here rather than trusting the raw stored value: the
    # store now initialises to None (its old default called into historical.db
    # at import time), and comparing against None would leave every pill
    # inactive on a fresh browser. Same fallback idiom as the five other
    # consumers of store-active-tier.
    tier = current_value if current_value in _TIER_VALUES else default_active_tier()
    return [
        'chart-window-pill active' if pid.get('value') == tier
        else 'chart-window-pill inactive'
        for pid in pill_ids
    ]


@app.callback(
    Output('cold-start-strip', 'children'),
    Input('metrics-interval', 'n_intervals'),
)
def _refresh_cold_start_strip(_n_intervals):
    """Fill the cold-start strip at RENDER time, not import time.

    Driven by the existing 30-sec interval rather than rendered once, so the
    day count follows a backfill and the 15:34 EOD archive instead of being a
    snapshot of whatever was true when the container booted. The underlying
    query is TTL-cached in get_historical_days_count(), so this costs one
    lightweight COUNT(DISTINCT date) every 5 minutes, not every 30 seconds.
    """
    return build_cold_start_strip()


# Index-pill selector — persist clicked pill's name, reflect active state.
# Same pattern as `_persist_chart_window` / `_reflect_chart_window_active`.
@app.callback(
    Output('store-selected-index', 'data'),
    Input({'type': 'index-pill', 'name': ALL}, 'n_clicks'),
    prevent_initial_call=True,
)
def _persist_selected_index(_n_clicks_list):
    triggered = ctx.triggered_id
    if not triggered or not isinstance(triggered, dict):
        return no_update
    return triggered.get('name', no_update)


@app.callback(
    Output({'type': 'index-pill', 'name': ALL}, 'className'),
    Input('store-selected-index', 'data'),
    State({'type': 'index-pill', 'name': ALL}, 'id'),
)
def _reflect_selected_index(current_name, pill_ids):
    return [
        'chart-window-pill active' if pid.get('name') == current_name
        else 'chart-window-pill inactive'
        for pid in pill_ids
    ]


# ⚙ button toggles the offcanvas open/closed.
@app.callback(
    Output('settings-drawer', 'is_open'),
    Input('settings-drawer-toggle', 'n_clicks'),
    State('settings-drawer', 'is_open'),
    prevent_initial_call=True,
)
def _toggle_settings_drawer(_n_clicks, is_open):
    return not is_open


# Detail drawer — three separate callbacks now with a single source of truth
# for the arrow's visual state:
#   (a) manual arrow click → toggle is_open only
#   (b) asset-row click → open is_open (unless a bottom drawer is open)
#   (c) mirror callback → any is_open change (arrow, asset, X, etc.) →
#       updates the arrow button's className + glyph.
# The mirror ensures the arrow can't drift out of sync with the drawer
# regardless of which path opened or closed it.
@app.callback(
    Output('detail-drawer', 'is_open'),
    Input('detail-drawer-toggle', 'n_clicks'),
    State('detail-drawer', 'is_open'),
    prevent_initial_call=True,
)
def _toggle_detail_drawer(_n_clicks, is_open):
    return not is_open


@app.callback(
    Output('detail-drawer', 'is_open', allow_duplicate=True),
    Input('selected-asset-store', 'data'),
    State('indices-collapse-store', 'data'),
    State('charts-collapse-store', 'data'),
    prevent_initial_call=True,
)
def _open_detail_drawer_on_asset_select(selected_asset, indices_open, charts_open):
    if not selected_asset:
        return no_update
    if indices_open or charts_open:
        return no_update
    return True


# Single source of truth for the arrow's visual state — tracks the
# drawer's actual `is_open` no matter what path changed it.
@app.callback(
    Output('detail-drawer-toggle', 'className'),
    Output('detail-drawer-toggle', 'children'),
    Input('detail-drawer', 'is_open'),
)
def _mirror_drawer_state_onto_arrow(is_open):
    if is_open:
        return 'detail-drawer-toggle open', '◂'
    return 'detail-drawer-toggle closed', '▸'


# Store → metrics-table.style_data_conditional (live-rebuild highlight rules).
@app.callback(
    Output('metrics-table', 'style_data_conditional'),
    [Input('store-z-threshold', 'data'),
     # Same trigger as update_table_data, so the row backgrounds and
     # the values they are highlighting move together. If only one repainted,
     # rows would be lit against numbers they no longer match.
     Input('store-active-tier', 'data')],
)
def _rebuild_highlight_rules(z_threshold, active_tier):
    threshold = float(z_threshold) if z_threshold is not None else _Z_HIGHLIGHT
    tier = active_tier if active_tier in _TIER_VALUES else default_active_tier()
    return _row_highlight_rules(z_threshold=threshold, tier=tier)


@app.callback(
    Output('selected-asset-store', 'data'),
    Input('metrics-table', 'active_cell'),
    State('metrics-table', 'derived_virtual_data'),
    prevent_initial_call=True
)
def write_selected_asset(active_cell, table_data):
    """Translate an active-cell click into `asset_id|segment_code` written to the Store.
    Uses derived_virtual_data so sort/filter order is respected."""
    if not active_cell or not table_data:
        return no_update
    try:
        row = table_data[active_cell['row']]
    except (IndexError, TypeError):
        return no_update
    asset_id = row.get('asset_id')
    segment_code = row.get('raw_segment_code') or ''
    if asset_id is None:
        return no_update
    return f"{asset_id}|{segment_code}"


@app.callback(
    Output('detail-panel', 'children'),
    [Input('selected-asset-store', 'data'),
     # Input, not State: the why-line and the Volume Block accent are both
     # threshold-driven, so moving the slider must redraw an already-open
     # drawer rather than waiting for the next row click. Same for the tier.
     Input('store-z-threshold', 'data'),
     Input('store-active-tier', 'data'),
     # Input, not State: as a State the open drawer kept the numbers from the
     # moment of the click while the table row behind it refreshed every cycle.
     Input('metrics-table', 'data')],
)
def update_detail_panel(selected_key, z_threshold, active_tier, table_data):
    """Rebuild the detail panel on a row click, a slider or tier change, and
    every time the scores table refreshes."""
    placeholder = html.Div([
        html.H4("Click any asset row", style={
            'color': '#b8a99b',
            'textAlign': 'center',
            'marginTop': '60px',
            'fontFamily': "'Fraunces', Georgia, serif",
            'fontStyle': 'italic',
            'fontWeight': '400',
            'fontSize': '20px',
        })
    ])
    if not selected_key or not table_data:
        return placeholder
    try:
        aid_str, segment_code = selected_key.split('|', 1)
        asset_id = int(aid_str)
    except (ValueError, AttributeError):
        return placeholder
    for row in table_data:
        if row.get('asset_id') == asset_id and (row.get('raw_segment_code') or '') == segment_code:
            try:
                tier = active_tier if active_tier in _TIER_VALUES else default_active_tier()
                return create_detail_panel(row, z_threshold, tier)
            except Exception as e:
                logger.error(f"Error creating detail panel for asset {asset_id}: {e}")
                return html.Div(f"Error: {str(e)}", style={'color': '#d94560', 'textAlign': 'center', 'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace", 'fontSize': '12px'})
    return placeholder


@app.callback(
    [Output('chart-price', 'figure'),
     Output('chart-velocity', 'figure'),
     Output('chart-acceleration', 'figure'),
     Output('chart-vwap-deviation', 'figure'),
     Output('chart-momentum', 'figure'),
     Output('chart-rel-strength', 'figure'),
     Output('chart-zscores', 'figure'),
     Output('chart-divergence', 'figure'),
     Output('chart-rolling-momentum', 'figure')],
    [Input('selected-asset-store', 'data'),
     Input('metrics-interval', 'n_intervals'),
     Input('atr-toggle', 'value'),
     Input('velocity-resample-store', 'data'),
     Input('store-chart-window', 'data'),
     Input('store-rs-benchmark', 'data'),
     # Tier emphasis on the 4-line tier charts.
     Input('store-active-tier', 'data'),
     # The z-charts shade |z| > this — the same "unusual" the table uses.
     Input('store-z-threshold', 'data')]
)
def update_charts(selected_asset, n_intervals, atr_toggle, resample, chart_window,
                  rs_benchmark, active_tier, z_threshold):
    """Update all 9 charts when asset selection changes or on 30s interval refresh.
    Fires whether the collapse is open or closed so expand is instant.

    Also fires when the chart-window pills change — passes
    `window_hours` through to `build_charts` and downstream chart builders +
    history query. Also fires when the z-slider moves, which sets where the
    z-charts' "unusual" shading starts.
    Also fires when the RS benchmark pill changes — threads
    `rs_benchmark` through so the RS chart re-renders for the picked benchmark."""
    empty_price = go.Figure(layout={**{**CHART_LAYOUT, 'height': 560},
                                     'title': dict(text='Price — Select an asset',
                                                   font=dict(size=13, color='#7a6e62'))})
    empty_fig = go.Figure(layout={**{**CHART_LAYOUT, 'height': 280},
                                   'title': dict(text='Select an asset', font=dict(size=13, color='#7a6e62'))})

    if not selected_asset:
        return [empty_price] + [empty_fig] * 8

    try:
        parts = selected_asset.split('|')
        asset_id = int(parts[0])
        segment_code = parts[1]
    except (ValueError, IndexError):
        return [empty_price] + [empty_fig] * 8

    show_atr = 'atr' in (atr_toggle or [])
    window_hours = int(chart_window) if chart_window is not None else 2
    tier = active_tier if active_tier in _TIER_VALUES else default_active_tier()
    figures = build_charts(asset_id, segment_code, show_atr=show_atr,
                           resample=resample or '30s', window_hours=window_hours,
                           rs_benchmark=rs_benchmark, active_tier=tier,
                           z_threshold=z_threshold)
    # Price chart at 560px (candlesticks + volume subplot + overlays); other
    # 8 charts at 280px each — matches the empty-state placeholder heights
    # above so selecting an asset doesn't resize the drawer.
    if figures:
        try:
            figures[0].update_layout(height=560)
        except Exception:
            pass
        for i in range(1, len(figures)):
            try:
                figures[i].update_layout(height=280)
            except Exception:
                pass
    return figures


@app.callback(
    Output('velocity-resample-store', 'data'),
    Input('velocity-resample-toggle', 'value'),
    prevent_initial_call=True,
)
def update_velocity_resample_store(value):
    return value or '30s'


# ============================================================================
# RS chart benchmark selector callbacks
#   1. Populate the pill row when the selected asset changes.
#   2. Persist the clicked pill's benchmark into store-rs-benchmark.
#   3. Reflect the active-state className on each pill from the store.
# ============================================================================

def _benchmarks_for_asset(asset_id: int, segment_code: str):
    """Return a list of benchmark names the currently-selected asset is
    tagged with, in the order emitted by the RS calculator. Reads
    `LATEST_METRICS['relative_strength']` from the in-memory cache — no DB
    query, no schema dependency."""
    try:
        from metrics import get_latest_metrics
        cache = get_latest_metrics()
        rs_rows = cache.get('relative_strength') or []
        seen = []
        for row in rs_rows:
            if not isinstance(row, dict):
                continue
            if row.get('asset_id') != asset_id or (row.get('segment_code') or '') != segment_code:
                continue
            bm = row.get('benchmark_name')
            if bm and bm not in seen:
                seen.append(bm)
        return seen
    except Exception as e:
        logger.error(f"_benchmarks_for_asset failed: {e}")
        return []


@app.callback(
    [Output('rs-benchmark-pill-row', 'children'),
     Output('store-rs-benchmark', 'data')],
    [Input('selected-asset-store', 'data'),
     Input('store-rs-benchmark', 'data')],
    prevent_initial_call=False,
)
def _populate_rs_benchmark_pills(selected_asset, current_bm):
    """Render one pill per benchmark the current asset is tagged with. If the
    stored benchmark isn't in the current asset's list, reset to the first."""
    if not selected_asset:
        return [], current_bm
    try:
        aid_str, segment_code = selected_asset.split('|', 1)
        asset_id = int(aid_str)
    except (ValueError, AttributeError):
        return [], current_bm

    benchmarks = _benchmarks_for_asset(asset_id, segment_code)
    if not benchmarks:
        return [], None

    # Reset to first benchmark when the stored one isn't valid for this asset.
    if current_bm not in benchmarks:
        current_bm = benchmarks[0]

    pills = [
        html.Button(
            bm,
            id={'type': 'rs-benchmark-pill', 'name': bm},
            n_clicks=0,
            className=('chart-window-pill active' if bm == current_bm
                       else 'chart-window-pill inactive'),
        )
        for bm in benchmarks
    ]
    return pills, current_bm


@app.callback(
    Output('store-rs-benchmark', 'data', allow_duplicate=True),
    Input({'type': 'rs-benchmark-pill', 'name': ALL}, 'n_clicks'),
    prevent_initial_call=True,
)
def _persist_rs_benchmark(_n_clicks_list):
    """Pill click → write clicked benchmark name to store-rs-benchmark."""
    triggered = ctx.triggered_id
    if not triggered or not isinstance(triggered, dict):
        return no_update
    return triggered.get('name', no_update)


@app.callback(
    Output('charts-collapse-store', 'data'),
    Input('charts-collapse-toggle', 'n_clicks'),
    State('charts-collapse-store', 'data'),
    prevent_initial_call=True
)
def toggle_charts_collapse(n_clicks, current):
    """Flip the collapse-open bool on every button click; persisted in sessionStorage."""
    return not bool(current)


@app.callback(
    [Output('charts-collapse', 'is_open'),
     Output('charts-collapse-toggle', 'children')],
    Input('charts-collapse-store', 'data')
)
def sync_charts_collapse(is_open):
    """Drive the Collapse's is_open and the button label from the Store."""
    is_open = bool(is_open)
    return is_open, ('▾ CHARTS' if is_open else '▸ CHARTS')


@app.callback(
    Output('indices-collapse-store', 'data'),
    Input('indices-collapse-toggle', 'n_clicks'),
    State('indices-collapse-store', 'data'),
    prevent_initial_call=True
)
def toggle_indices_collapse(n_clicks, current):
    """Flip the indices-drawer open bool on every button click; persisted in sessionStorage."""
    return not bool(current)


@app.callback(
    [Output('indices-collapse', 'is_open'),
     Output('indices-collapse-toggle', 'children')],
    Input('indices-collapse-store', 'data')
)
def sync_indices_collapse(is_open):
    """Drive the indices Collapse's is_open and the button label from the Store."""
    is_open = bool(is_open)
    return is_open, ('▾ INDICES' if is_open else '▸ INDICES')


@app.callback(
    Output('indices-drawer-body', 'children'),
    [Input('metrics-interval', 'n_intervals'),
     Input('store-chart-window', 'data'),
     Input('velocity-resample-store', 'data'),
     Input('store-selected-index', 'data'),
     # The z-driven breadth spike counts are derived at DISPLAY time from
     # `constituent_z`, so the drawer must re-render when the trader moves the
     # z-slider. An Input, not a State — otherwise the counting moves to
     # display time but the display never learns the threshold.
     Input('store-z-threshold', 'data'),
     # And the tier, so the index card's spike counts and its
     # weighted volume z read the same tier as the scores table.
     Input('store-active-tier', 'data')]
)
def update_indices_drawer(n_intervals, chart_window, resample, selected_index,
                          z_threshold, active_tier):
    """Rebuild the indices drawer body every collection cycle.

    The time window is governed by the settings drawer's chart-window
    pill group (`store-chart-window`). 1/2/4/8/24 map 1-to-1 to hours. None →
    2h (default). The widest pill ("1d" = 24h) is clamped down to the single
    trading day by `clamp_to_day_start` inside `get_index_breadth_history`,
    so no extra capping is needed here.
    Renders ONE index card (the one identified by
    `store-selected-index`) at full drawer body width."""
    hours = int(chart_window) if chart_window is not None else 2
    resample = resample or '30s'

    try:
        from metrics import get_latest_index_metrics
        summaries = get_latest_index_metrics() or {}
    except Exception as e:
        logger.error(f"Error reading index metrics cache: {e}")
        summaries = {}

    if not summaries:
        return html.Div(
            "Awaiting first cycle…",
            style={
                'fontSize': '12px',
                'color': '#b8a99b',
                'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
                'padding': '8px',
            },
        )

    # Pick the summary for the selected index. Fall back to first available
    # if the persisted selection isn't present this cycle (e.g. an index
    # temporarily missing from the aggregator's output).
    summary = summaries.get(selected_index) if selected_index else None
    if summary is None:
        for k in ('NIFTY', 'BANKNIFTY', 'MIDCAP', 'SENSEX'):
            if k in summaries:
                summary = summaries[k]
                break
    if summary is None:
        return html.Div(
            "Awaiting first cycle…",
            style={
                'fontSize': '12px',
                'color': '#b8a99b',
                'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
                'padding': '8px',
            },
        )

    tier = active_tier if active_tier in _TIER_VALUES else default_active_tier()
    return build_index_card(summary, hours=hours, resample=resample,
                            z_threshold=z_threshold, tier=tier)


# The engine fetches every ~5 s, so 30 s is about six missed fetches.
_PRICE_FEED_STALE_SECONDS = 30


@app.callback(
    Output('staleness-indicator', 'children'),
    Input('metrics-interval', 'n_intervals')
)
def update_staleness_indicator(n_intervals):
    """Show small live/stale/hydrated-from-disk status in top-right header.

    When the caches were prefilled from metrics.db on startup
    (hydration) and no live cycle has overwritten them yet, show
    `⚠ STALE · data from YYYY-MM-DD HH:MM IST` so the trader knows they're
    looking at last-known values, not current ones. Flips to `● LIVE` on
    the first live cycle.

    A live metrics cycle is not live PRICES: when the engine stops fetching
    (expired token, network down) the calculators keep succeeding on the last
    quotes and the cycle timestamp keeps advancing. So LIVE also requires a
    quote newer than _PRICE_FEED_STALE_SECONDS; otherwise `⚠ PRICES STALE`."""
    try:
        from metrics import (
            get_latest_metrics,
            get_hydration_timestamp,
            get_price_feed_last_update,
            COLLECTION_INTERVAL_SECONDS,
        )
        metrics = get_latest_metrics()
        ts = metrics.get('timestamp')
        hydration_ts = get_hydration_timestamp()

        # Hydrated-from-disk path: no live cycle since boot, but we have DB data.
        if ts is None and hydration_ts is not None:
            stamp = hydration_ts.strftime('%Y-%m-%d %H:%M IST')
            return html.Span(
                f"⚠ STALE · data from {stamp}",
                style={'color': '#e8a447', 'fontWeight': '500'},
            )

        # Fresh install / empty DB / hydration failed and no live cycle yet.
        if ts is None:
            return html.Span("○ NO DATA", style={'color': '#e8a447'})

        # Live-cycle path (existing logic).
        age = (datetime.now(IST) - datetime.fromisoformat(ts)).total_seconds()
        if age > COLLECTION_INTERVAL_SECONDS * 2:
            age_str = f"{int(age // 60)}m{int(age % 60)}s"
            return html.Span(f"⚠ STALE ({age_str})", style={'color': '#d94560', 'fontWeight': '500'})

        # The cycle is live — are the prices it computed from?
        last_quote = get_price_feed_last_update()
        if last_quote is None:
            return html.Span("⚠ NO PRICES", style={'color': '#d94560', 'fontWeight': '500'})
        quote_age = (datetime.now(IST) - last_quote).total_seconds()
        if quote_age > _PRICE_FEED_STALE_SECONDS:
            age_str = f"{int(quote_age // 60)}m{int(quote_age % 60)}s"
            return html.Span(f"⚠ PRICES STALE ({age_str})",
                             style={'color': '#d94560', 'fontWeight': '500'})
        return html.Span("● LIVE", style={'color': '#5fb87c'})
    except Exception:
        return ""


# The health bar is label-only (see the docstring below): the legend for the Signals
# column is the glyph-prefixed column headers. All nine calculators have a
# Signals-column slot.
@app.callback(
    Output('health-bar', 'children'),
    Input('metrics-interval', 'n_intervals')
)
def update_health_bar(n_intervals):
    """Show per-calculator last-success timestamps in the header's left cell.

    Per-calculator staleness threshold. Continuous calcs stay on
    the default 90s (fires every 30s). Volume Block overrides to 1200s (15-min block
    interval + safety buffer) so the health bar doesn't flag it red between
    block boundaries. Threshold is read from each Signal's
    `stale_threshold_seconds` field (see dashboard_signals.py).

    Glyph prefix removed. Bar is label-only, single-line at any
    reasonable width. Glyphs now live on the DataTable column headers where
    Tier-1 typography renders them cleanly (see create_main_table).
    """
    try:
        from metrics import get_calculator_health
        from dashboard_signals import SIGNAL_REGISTRY
        health = get_calculator_health()
        now = datetime.now(IST)
        parts = []
        for sig in SIGNAL_REGISTRY.values():
            if not sig.health_label:
                continue
            ts = health.get(sig.emit_key)
            if ts is None:
                label_color = '#7a6e62'
                label_text = f"{sig.health_label} ·"
            else:
                age = (now - ts).total_seconds()
                age_str = f"{int(age)}s" if age < 60 else f"{int(age // 60)}m{int(age % 60)}s"
                label_color = '#d94560' if age > sig.stale_threshold_seconds else '#b8a99b'
                label_text = f"{sig.health_label} {age_str}"
            parts.append(html.Span(f"{label_text}   ", style={'color': label_color}))
        return parts
    except Exception:
        return ""


if __name__ == '__main__':
    logger.info("Starting Metrics Dashboard on port 8054")
    app.run(debug=False, host='127.0.0.1', port=8054)
