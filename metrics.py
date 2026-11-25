"""
Metrics Orchestrator System
Collects and caches metrics from the 9 per-asset calculators, plus the index
aggregator, for real-time dashboard display
"""

import copy
import dataclasses
import math
import time
import logging
import os
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta
from typing import List, Dict, Any, Optional

from calculators.schemas import (
    BreakoutRow,
    IndexSummary,
    MomentumRow,
    PriceLevelsRow,
    RelativeStrengthRow,
    RollingMomentumRow,
    VolumeBlockRow,
    VolumeDynamicsRow,
    VwapRow,
)
from calculators.block_snapshots import block_end_values
from calculators.market_calendar import previous_trading_day
from calculators.statistics import TIERS, finite_positive
from calculators.time_blocks import (
    BLOCK_EDGE_TOLERANCE, IST, block_reading_end_time, block_start_time, current_completed_block,
)



def _serialize_rows(rows: list) -> list:
    """
    Convert a list of typed calculator Row instances to plain dicts, in order.
    Used at every LATEST_METRICS storage boundary and every metrics.db store
    call: calculators emit dataclass instances, the cache
    + persistence layer speak dicts. `dataclasses.asdict()` serializes only declared
    fields — runtime routing tags added via attribute access (type, message) are
    dropped intentionally.
    """
    if not rows:
        return []
    return [dataclasses.asdict(r) if dataclasses.is_dataclass(r) else r for r in rows]


# Post-close grace for the metrics orchestrator only (engine stays strict at end_time).
# Lets the final 30-sec metrics cycle fire after end_time so calculators gated on the
# last-completed time block (e.g. Volume Block's block 25) can write their row.
METRICS_POST_CLOSE_GRACE_SECONDS = 120

# The shared intraday store records each block's value AT ITS END, read from the
# stored readings, so it is written only after the block's readings are complete:
# on the first metrics cycle within this long after the block's reading end
# (the block end; for block 25, 15:31 — see time_blocks.block_reading_end_time).
# Two minutes at ~34-second cycles gives at least three chances, so one slow
# cycle cannot skip a block.
_SHARED_STORE_WRITE_WINDOW = timedelta(minutes=2)


# Import momentum adapter (wraps momentum_calculator.py's locked pure function)
try:
    from calculators.momentum_adapter import run_momentum_analysis
    from calculators.database_manager import OHLCDatabaseReader
    MOMENTUM_SYSTEM_AVAILABLE = True
except ImportError as e:
    logging.warning(f"Momentum System not available: {e}")
    MOMENTUM_SYSTEM_AVAILABLE = False

# Import VWAP adapter (wraps vwap_deviation_calculator.py's locked pure function;
# persistent-instance style like price_levels, since VWAP holds Kind-B state)
try:
    from calculators.vwap_adapter import create_calculator as create_vwap_calculator
    VWAP_SYSTEM_AVAILABLE = True
except ImportError as e:
    logging.warning(f"VWAP System not available: {e}")
    VWAP_SYSTEM_AVAILABLE = False

# Import breakout adapter (wraps breakout_calculator.py's locked pure function)
try:
    from calculators.breakout_adapter import run_breakout_analysis
    BREAKOUT_SYSTEM_AVAILABLE = True
except ImportError as e:
    logging.warning(f"Breakout System not available: {e}")
    BREAKOUT_SYSTEM_AVAILABLE = False

# Import relative strength adapter (wraps relative_strength_calculator.py's locked pure functions)
try:
    from calculators.relative_strength_adapter import run_relative_strength_analysis
    RELATIVE_STRENGTH_SYSTEM_AVAILABLE = True
except ImportError as e:
    logging.warning(f"Relative Strength System not available: {e}")
    RELATIVE_STRENGTH_SYSTEM_AVAILABLE = False

# Import volume block adapter — replaces the old Time Block Volume calculator
# (wraps volume_block.py's locked pure function, TRADE_VOLUME_BLOCK_LOCKED_V4)
try:
    from calculators.volume_block_adapter import run_volume_block_analysis
    VOLUME_BLOCK_SYSTEM_AVAILABLE = True
except ImportError as e:
    logging.warning(f"Volume Block System not available: {e}")
    VOLUME_BLOCK_SYSTEM_AVAILABLE = False

# Import volume dynamics adapter (wraps volume_dynamics.py's locked pure function,
# VOLUME_DYNAMICS_LOCKED_V3 — consumes Volume Block's same-cycle output directly)
try:
    from calculators.volume_dynamics_adapter import run_volume_dynamics_analysis
    VOLUME_DYNAMICS_SYSTEM_AVAILABLE = True
except ImportError as e:
    logging.warning(f"Volume Dynamics System not available: {e}")
    VOLUME_DYNAMICS_SYSTEM_AVAILABLE = False

# Import price-volume divergence adapter (wraps price_volume_divergence_calculator.py's
# locked pure function, PVD_LOCKED_V3 — runs cold until enough live 5-second history
# accumulates; cannot be backfilled)
try:
    from calculators.price_volume_divergence_adapter import run_price_volume_divergence_analysis
    PRICE_VOLUME_DIVERGENCE_SYSTEM_AVAILABLE = True
except ImportError as e:
    logging.warning(f"Price-Volume Divergence System not available: {e}")
    PRICE_VOLUME_DIVERGENCE_SYSTEM_AVAILABLE = False

# Import price levels adapter — replaces the old Price Alerts calculator
# (wraps price_alerts_calculator.py's locked pure functions, PRICE_LEVELS_LOCKED_V1)
try:
    from calculators.price_levels_adapter import run_price_levels_analysis
    PRICE_LEVELS_SYSTEM_AVAILABLE = True
except ImportError as e:
    logging.warning(f"Price Levels System not available: {e}")
    PRICE_LEVELS_SYSTEM_AVAILABLE = False

# Import rolling momentum adapter (wraps rolling_momentum_calculator.py's locked
# pure function, ROLLING_MOMENTUM_LOCKED_V1 — continuously re-evaluated at every
# completed 15-min block, unlike momentum's fixed 09:15->09:30 window)
try:
    from calculators.rolling_momentum_adapter import run_rolling_momentum_analysis
    ROLLING_MOMENTUM_SYSTEM_AVAILABLE = True
except ImportError as e:
    logging.warning(f"Rolling Momentum System not available: {e}")
    ROLLING_MOMENTUM_SYSTEM_AVAILABLE = False

# Setup logging
logger = logging.getLogger(__name__)

# ============================================================================
# Shared Cache for Dashboard (In-Memory Storage)
# ============================================================================

# Module-level cache for latest metrics (shared with dashboard)
LATEST_METRICS = {
    'timestamp': None,
    'cycle_number': 0,
    'momentum': [],
    'breakout': [],
    'vwap': [],
    'relative_strength': [],
    'volume_block': [],
    'volume_dynamics': [],
    'price_volume_divergence': [],
    'price_levels': [],
    'rolling_momentum': []
}

# Thread lock for cache updates
CACHE_LOCK = threading.Lock()

# Collection interval — exported so dashboard can sync its refresh rate
COLLECTION_INTERVAL_SECONDS = 30

# Per-calculator last-success timestamps (None = never succeeded since startup).
#
# "Success" includes the metrics.db write: each stamp sits after the store
# call, and the store methods re-raise on failure. A failed write is therefore
# NOT distinguished from a calculator crash on the health bar — both surface as
# that calculator going red once its Signal.stale_threshold_seconds passes
# (90 s; Volume Block 1200 s). Meanwhile the scores table looks normal, since
# LATEST_METRICS still updates from the in-memory results; what is lost is
# history (the charts drawer, and the next restart's hydration). The log line
# "Error storing <calculator> metrics: ..." is what tells the two apart.
CALCULATOR_HEALTH = {
    'momentum': None,
    'breakout': None,
    'vwap': None,
    'relative_strength': None,
    'volume_block': None,
    'volume_dynamics': None,
    'price_volume_divergence': None,
    'price_levels': None,
    'rolling_momentum': None,
}

def get_calculator_health() -> dict:
    """Return a snapshot of per-calculator last-success timestamps."""
    with CACHE_LOCK:
        return copy.deepcopy(CALCULATOR_HEALTH)


# Hydration state. Set once at MetricsOrchestrator startup after
# _hydrate_cache_from_db() prefills the caches from disk; cleared on the first
# live-cycle write via update_metrics_cache(). Dashboard reads this to render
# a "STALE · data from ..." indicator whenever the caches are running on
# hydrated-from-disk values rather than a fresh live cycle.
HYDRATION_TIMESTAMP: Optional[datetime] = None


def get_hydration_timestamp() -> Optional[datetime]:
    """Return the timestamp the current cache values were hydrated from (or
    None once a live cycle has overwritten them)."""
    with CACHE_LOCK:
        return HYDRATION_TIMESTAMP


# The live price feed (PriceDataManager), registered by MetricsOrchestrator so
# the dashboard header can show when the last quote arrived. The metrics
# cycle's own timestamp cannot show that: calculators keep succeeding on the
# last prices when the feed stops (expired token, network down), so the cycle
# stays "LIVE" over frozen prices.
_PRICE_FEED = None


def _register_price_feed(price_data_manager) -> None:
    global _PRICE_FEED
    _PRICE_FEED = price_data_manager


def get_price_feed_last_update() -> Optional[datetime]:
    """When the newest quote reached the price feed, or None if none has."""
    feed = _PRICE_FEED
    if feed is None:
        return None
    last = feed.get_status().get('last_update_time')
    return datetime.fromisoformat(last) if last else None


def set_hydration_timestamp(dt: Optional[datetime]) -> None:
    """Set the hydration timestamp under CACHE_LOCK. Called once by
    MetricsOrchestrator._hydrate_cache_from_db() with the max timestamp
    observed across the hydrated tables."""
    global HYDRATION_TIMESTAMP
    with CACHE_LOCK:
        HYDRATION_TIMESTAMP = dt

def get_latest_metrics() -> dict:
    """
    Get latest metrics from cache (called by dashboard).
    Thread-safe read operation.

    Returns:
        Dictionary with all latest metrics for all assets
    """
    with CACHE_LOCK:
        return copy.deepcopy(LATEST_METRICS)

def update_metrics_cache(metrics_dict: dict) -> None:
    """
    Update metrics cache (called by collect_metrics).
    Thread-safe write operation.

    Args:
        metrics_dict: Dictionary containing metrics from all calculators
    """
    global LATEST_METRICS, HYDRATION_TIMESTAMP
    with CACHE_LOCK:
        LATEST_METRICS.update(metrics_dict)
        LATEST_METRICS['timestamp'] = datetime.now(IST).isoformat()
        LATEST_METRICS['cycle_number'] += 1
        # The first live-cycle write clears the hydration flag. From this
        # point on, dashboard indicator flips from STALE-from-disk to LIVE.
        if HYDRATION_TIMESTAMP is not None:
            HYDRATION_TIMESTAMP = None
        logger.debug(f"Cache updated: cycle #{LATEST_METRICS['cycle_number']}")


# ============================================================================
# Per-Index Aggregate Cache (populated by IndexAggregator each cycle)
# ============================================================================

# Module-level cache for latest per-index aggregate summaries (shared with dashboard)
# Shape: {'NIFTY': {summary_dict}, 'BANKNIFTY': {...}, 'MIDCAP': {...}, 'SENSEX': {...}}
LATEST_INDEX_METRICS = {}


def get_latest_index_metrics() -> dict:
    """Thread-safe read of the per-index aggregate cache (called by dashboard)."""
    with CACHE_LOCK:
        return copy.deepcopy(LATEST_INDEX_METRICS)


def update_index_metrics_cache(summaries: dict) -> None:
    """Thread-safe replace of the per-index aggregate cache (called by collect_metrics)."""
    global LATEST_INDEX_METRICS
    with CACHE_LOCK:
        LATEST_INDEX_METRICS = summaries


# ============================================================================
# Index Aggregator
# ============================================================================

#: Signal order inside each constituent_z value row. Written into every stored
#: row's own `sig` header, so a change here cannot silently misdecode rows that
#: were written under the old order.
_CZ_SIGNALS = ('block_volume_z', 'session_return_z')

#: Stored z-scores are rounded to this many decimals. They are displayed to 2dp
#: and thresholded against a slider whose finest step is 0.1, so the 14 further
#: digits of a float repr are pure payload — over half the bytes of the old
#: encoding. Rounding is a SERIALISATION concern only: the weighted-average
#: accumulators below consume the full-precision values.
_CZ_ROUND = 3


def _tier_z_values(source: Dict[str, Any], stem: str) -> List[Optional[float]]:
    """The four tier z-values of one signal, as a positional list.

    `None` marks a tier with no value — the encoding's way of saying "not
    evaluated", which is what the removed `*_evaluated` denominators carried.
    A fabricated 0.0 would read as "perfectly normal", a claim the data does
    not support.
    """
    out: List[Optional[float]] = []
    for tier in TIERS:
        raw = source.get(f'{stem}_{tier}d')
        if raw is None:
            out.append(None)
            continue
        try:
            out.append(float(raw))
        except (TypeError, ValueError):
            out.append(None)
    return out


def _build_constituent_z(keys: List[str],
                         values: List[List[Optional[float]]]) -> Dict[str, Any]:
    """Assemble the compact, SELF-DESCRIBING constituent_z payload.

        {"k":     ["1:NSE_EQ", ...],                  # constituent, in row order
         "sig":   ["block_volume_z", "session_return_z"],
         "tiers": ["22d", "90d", "180d", "252d"],
         "v":     [[bv22, bv90, bv180, bv252, sr22, sr90, sr180, sr252], ...]}

    Value index = signal_index * len(tiers) + tier_index. `null` means the tier
    had no value; a constituent with no value anywhere is absent from `k`/`v`
    entirely, which is the same "absence means not evaluated" contract the
    previous nested-dict form carried.

    WHY THIS SHAPE. The old form was
    `{"1:NSE_EQ": {"block_volume_z": {"22d": -0.12345678901234, ...}, ...}}`
    — about 291 bytes per constituent, so ~14.5 KB per row for NIFTY's 50,
    every 30 seconds. Roughly 55% of that was float reprs and 30% was the
    signal and tier names repeated once per constituent. Positional values plus
    3-decimal rounding cut it to ~73 bytes each, so a NIFTY row lands near
    3.7 KB — under the 4096-byte page size, meaning the row no longer spills
    into SQLite overflow pages at all.

    `sig` and `tiers` are written into EVERY row rather than held in one shared
    mapping. That costs ~90 bytes and buys the thing a shared mapping cannot:
    a row is decodable from itself, so changing TIERS or _CZ_SIGNALS later
    cannot silently misread rows written under the old layout.
    """
    if not keys:
        return {}
    return {
        'k': keys,
        'sig': list(_CZ_SIGNALS),
        'tiers': [f'{t}d' for t in TIERS],
        'v': values,
    }


# ---------------------------------------------------------------------------
# Boot-time check of IndexAggregator's reads
# ---------------------------------------------------------------------------
# Every per-asset field IndexAggregator reads, by the Row class that emits it.
# It reads LATEST_METRICS dicts with `.get()`, so naming a field the Row does
# not carry is not an error — it is a silent None, forever. That is how
# weighted volume velocity was lost: it read `velocity`, which VolumeDynamicsRow
# never had, and was NULL on every index_metrics row ever stored. The
# dashboard's _CONSUMED_FIELDS check does not cover this class; this is its
# counterpart. Checked per class rather than against the union of all fields,
# so a name cannot pass merely because some OTHER Row carries it.
#
# Any new read in IndexAggregator must be added here.
_ROW_KEYS = ('asset_id', 'segment_code', 'timestamp')
_INDEX_AGGREGATOR_READS = {
    MomentumRow:         {*_ROW_KEYS, 'symbol', 'current_price', 'previous_close'},
    VwapRow:             {*_ROW_KEYS, 'displacement'},
    BreakoutRow:         {*_ROW_KEYS, 'breakout_now'},
    VolumeBlockRow:      {*_ROW_KEYS, *(f'block_volume_z_{t}d' for t in TIERS)},
    VolumeDynamicsRow:   {*_ROW_KEYS, *(f'velocity_{t}d' for t in TIERS)},
    PriceLevelsRow:      {*_ROW_KEYS, 'day_events'},
    RollingMomentumRow:  {*_ROW_KEYS, *(f'session_return_z_{t}d' for t in TIERS)},
    RelativeStrengthRow: {'asset_id', 'segment_code', 'benchmark_name',
                          *(f'session_rs_{t}d' for t in TIERS)},
}


def _check_index_aggregator_reads(reads: Dict[type, set]) -> None:
    """Raise at import if IndexAggregator names a field its Row does not emit."""
    unknown = sorted(
        f'{cls.__name__}.{name}'
        for cls, names in reads.items()
        for name in names - {f.name for f in dataclasses.fields(cls)}
    )
    if unknown:
        raise RuntimeError(
            f"IndexAggregator reads field(s) its Row does not emit: {unknown}. "
            f"Fix the read in IndexAggregator and _INDEX_AGGREGATOR_READS in metrics.py."
        )


_check_index_aggregator_reads(_INDEX_AGGREGATOR_READS)


class IndexAggregator:
    """
    Aggregate per-asset calculator outputs into per-index breadth + leadership +
    weighted summaries. Pure function over the shared cache + live price feed —
    no DB reads, no API calls.

    None values from individual calculators (e.g., breakout pre-9:45, z-scores
    before 22 trading days of history) are treated as 'not contributing' and
    surfaced via *_evaluated denominators alongside each counter.
    """

    # (asset_id, segment_code) for the live index quote in PriceDataManager
    INDEX_QUOTE = {
        'NIFTY':     (13,  'IDX_I'),
        'BANKNIFTY': (25,  'IDX_I'),
        'MIDCAP':    (442, 'IDX_I'),
        'SENSEX':    (51,  'IDX_I'),
    }

    # Substring used in the CSV `index` column for each index, passed to
    # AssetLoader.get_index_constituents.
    CSV_KEY = {
        'NIFTY':     'NIFTY',
        'BANKNIFTY': 'BankNifty',
        'MIDCAP':    'Midcap',
        'SENSEX':    'SENSEX',
    }

    # The benchmark_name string written by the RS calculator for each index.
    RS_BENCHMARK_NAME = {
        'NIFTY':     'Nifty',
        'BANKNIFTY': 'BankNifty',
        'MIDCAP':    'MidcapNifty',
        'SENSEX':    'Sensex',
    }

    def __init__(self, asset_loader):
        self.asset_loader = asset_loader
        self.constituents = {}
        if asset_loader is not None:
            for index_name, csv_key in self.CSV_KEY.items():
                self.constituents[index_name] = asset_loader.get_index_constituents(csv_key)
            for k, v in self.constituents.items():
                logger.info(f"IndexAggregator: {k} has {len(v)} constituents")
        else:
            logger.warning("IndexAggregator initialized without AssetLoader — no aggregates will be produced")

    @staticmethod
    def _index_by_asset(latest_metrics: dict) -> dict:
        """Build {(asset_id, segment_code): {calc_name: row, ...}} for O(1) lookup."""
        by_asset = {}
        keys = ('momentum', 'vwap', 'breakout', 'volume_block',
                'volume_dynamics', 'price_levels', 'rolling_momentum')
        for calc_name in keys:
            for row in latest_metrics.get(calc_name, []) or []:
                aid = row.get('asset_id')
                seg = row.get('segment_code', '')
                if aid is None:
                    continue
                slot = by_asset.setdefault((aid, seg), {})
                # If multiple rows exist for one calculator (e.g., volume_block
                # writes one row per block per asset), keep the latest by timestamp.
                existing = slot.get(calc_name)
                if existing is None or (row.get('timestamp') or '') >= (existing.get('timestamp') or ''):
                    slot[calc_name] = row
        # relative_strength: list of rows keyed by (asset, benchmark_name) — keep all
        rs_by_asset = {}
        for row in latest_metrics.get('relative_strength', []) or []:
            aid = row.get('asset_id')
            seg = row.get('segment_code', '')
            if aid is None:
                continue
            rs_by_asset.setdefault((aid, seg), {})[row.get('benchmark_name')] = row
        for k, v in rs_by_asset.items():
            by_asset.setdefault(k, {})['relative_strength_by_benchmark'] = v
        return by_asset

    def compute(self, latest_metrics: dict, price_data_manager) -> Dict[str, IndexSummary]:
        """
        Returns: {'NIFTY': IndexSummary(...), 'BANKNIFTY': IndexSummary(...), ...}
        Typed rows; the caller serializes via dataclasses.asdict at the
        LATEST_INDEX_METRICS / metrics.db storage boundary.
        Indices with empty constituent lists or unavailable price feed are skipped.
        """
        result = {}
        if not self.constituents:
            return result
        by_asset = self._index_by_asset(latest_metrics)

        # Pull live index quotes in one batch query
        index_prices = self._fetch_index_prices(price_data_manager)

        for index_name, constituents in self.constituents.items():
            if not constituents:
                continue
            summary = self._compute_one(index_name, constituents, by_asset, index_prices)
            if summary is not None:
                result[index_name] = summary
        return result

    def _fetch_index_prices(self, price_data_manager) -> dict:
        """Return {index_name: (last_price, intraday_pct)} from the live feed."""
        prices = {}
        if price_data_manager is None or not price_data_manager.is_ready():
            return prices
        try:
            asset_ids = [aid for aid, _ in self.INDEX_QUOTE.values()]
            seg_codes = [seg for _, seg in self.INDEX_QUOTE.values()]
            df = price_data_manager.get_current_prices(asset_ids, seg_codes)
            if df is None or df.empty:
                return prices
            for index_name, (aid, seg) in self.INDEX_QUOTE.items():
                row = df[(df['asset_id'] == aid) & (df['segment_code'] == seg)]
                if row.empty:
                    continue
                r = row.iloc[0]
                last = r.get('last_price')
                opn = r.get('open_price')
                try:
                    if last is not None and opn is not None and float(opn) > 0:
                        intraday_pct = (float(last) - float(opn)) / float(opn) * 100.0
                        prices[index_name] = (float(last), float(intraday_pct))
                except (TypeError, ValueError):
                    continue
        except Exception as e:
            logger.warning(f"IndexAggregator: failed to fetch index prices: {e}")
        return prices

    def _compute_one(self, index_name: str, constituents: list,
                     by_asset: dict, index_prices: dict) -> Optional[IndexSummary]:
        # Counters
        advancers = decliners = unchanged = 0
        above_vwap = below_vwap = vwap_evaluated = 0
        in_breakout_up = in_breakout_down = breakout_evaluated = 0
        level_breached_today = 0
        # Per-tier: a stock's RS sign can differ between a 22-day beta and a
        # 252-day one, so each tier gets its own count.
        positive_rs = {t: 0 for t in TIERS}
        negative_rs = {t: 0 for t in TIERS}
        rs_evaluated = {t: 0 for t in TIERS}

        # Per-constituent z-values, for counting at DISPLAY time against the
        # live z-slider. See the `constituent_z` comment on IndexSummary for
        # why this replaced six stored counters — briefly: the metrics thread
        # cannot see a per-browser setting, and the breadth CHART reads history,
        # so a historical row can only be recounted if the raw values are on it.
        # Accumulated column-wise, then assembled into the compact self-describing
        # form after the constituent loop. See _build_constituent_z below.
        cz_keys: List[str] = []
        cz_values: List[List[Optional[float]]] = []

        # Weighted accumulators: (numerator, denominator-weight-sum)
        wir_num = wir_den = 0.0
        wvd_num = wvd_den = 0.0
        # Volume z and volume velocity are weighted per tier — weighted
        # averages, unlike the spike counts, so they stay server-side.
        wvz_num = {t: 0.0 for t in TIERS}
        wvz_den = {t: 0.0 for t in TIERS}
        wvv_num = {t: 0.0 for t in TIERS}
        wvv_den = {t: 0.0 for t in TIERS}

        # (weight, session_momentum) pairs — used for dispersion + ex-top-5.
        # The session frame — returns since the previous close — is the anchor
        # everywhere.
        intraday_pairs = []

        # Leadership candidates
        contrib_rows = []  # list of dicts with symbol, weight_pct, intraday_pct, contribution_bps

        rs_benchmark = self.RS_BENCHMARK_NAME.get(index_name)

        for asset_id, segment_code, weight in constituents:
            data = by_asset.get((asset_id, segment_code), {})

            # Momentum / advancers-decliners + weighted return + leadership.
            # MomentumRow (locked schema) has no single "move since prev close"
            # field — it stores the gap/early-return/continuation pieces
            # separately — so this is computed here from current_price/
            # previous_close, which the row does carry.
            mom = data.get('momentum') or {}
            symbol = mom.get('symbol') or ''
            session = None
            _cp, _pc = mom.get('current_price'), mom.get('previous_close')
            if _cp is not None and _pc:
                try:
                    if float(_pc) > 0:
                        session = (float(_cp) / float(_pc) - 1.0) * 100.0
                except (TypeError, ValueError, ZeroDivisionError):
                    session = None
            if session is not None:
                try:
                    iv = float(session)
                    if iv > 0:
                        advancers += 1
                    elif iv < 0:
                        decliners += 1
                    else:
                        unchanged += 1
                    wir_num += weight * iv
                    wir_den += weight
                    intraday_pairs.append((weight, iv))
                    contrib_rows.append({
                        'symbol': symbol,
                        'weight_pct': round(weight, 2),
                        # `intraday_pct` is kept as the field name for
                        # downstream dashboard/index-drawer consumers that
                        # already read it; the value is now session_momentum.
                        'intraday_pct': round(iv, 3),
                        # Contribution in basis points: weight (%) * move (%) / 100
                        'contribution_bps': round(weight * iv, 2),
                    })
                except (TypeError, ValueError):
                    pass

            # VWAP — above/below + weighted deviation. VwapRow stores a log-
            # return `displacement`, not a percent — approximated to percent
            # (ln(1+x) ~ x for the small deviations this signal produces).
            vwap = data.get('vwap') or {}
            dev = vwap.get('displacement')
            if dev is not None:
                try:
                    dv = float(dev) * 100.0
                    vwap_evaluated += 1
                    if dv > 0:
                        above_vwap += 1
                    elif dv < 0:
                        below_vwap += 1
                    wvd_num += weight * dv
                    wvd_den += weight
                except (TypeError, ValueError):
                    pass

            # Breakout — direction + denominator. BreakoutRow.breakout_now: +1
            # above the opening range right now, -1 below, 0 inside (two
            # readings in a row). Not breakout_direction — that is the day's
            # first breakout and never clears, so by the afternoon it counted
            # nearly every constituent, including those back inside the range.
            brk = data.get('breakout') or {}
            direction = brk.get('breakout_now')
            if direction is not None:
                breakout_evaluated += 1
                if direction == 1:
                    in_breakout_up += 1
                elif direction == -1:
                    in_breakout_down += 1

            # Volume dynamics — weighted velocity, per tier. VolumeDynamicsRow
            # has no plain `velocity`; reading one made this NULL on every row.
            vd = data.get('volume_dynamics') or {}
            for tier, vel in zip(TIERS, _tier_z_values(vd, 'velocity')):
                if vel is not None:
                    wvv_num[tier] += weight * vel
                    wvv_den[tier] += weight

            # ---- The two z-driven breadth signals --------------------------
            #
            # Volume block z: how unusual this constituent's volume is for this
            # 15-min block. Rolling Momentum session-return z: the statistical
            # counterpart to advancers/decliners — those count DIRECTION (did
            # it move up or down), this counts UNUSUALNESS (did it move
            # unusually far for this time of day).
            #
            # Neither is counted here any more. Both are RECORDED, per tier,
            # and counted at display time against whatever the trader has the
            # z-slider set to. The hardcoded `> 2.0` that used to live here
            # made the index card disagree with the scores table whenever the
            # slider was not at 2.0.
            #
            # A tier absent from the dict means "not evaluated at that tier" —
            # which is what the old *_evaluated denominators carried, now
            # expressed as the absence of a key rather than a separate count.
            vb = data.get('volume_block') or {}
            rm = data.get('rolling_momentum') or {}
            # Positional, in _CZ_SIGNALS order. Kept FULL PRECISION here — the
            # weighted-volume-z accumulator just below consumes bv_vals, and
            # rounding is a serialisation concern, not a maths one.
            bv_vals = _tier_z_values(vb, 'block_volume_z')
            sr_vals = _tier_z_values(rm, 'session_return_z')

            if any(v is not None for v in bv_vals + sr_vals):
                # Composite key: asset_id 25 is BankNifty in IDX_I and ADANIENT
                # in NSE_EQ. Keying on asset_id alone would blend the two.
                cz_keys.append(f'{asset_id}:{segment_code}')
                cz_values.append([
                    None if v is None else round(v, _CZ_ROUND)
                    for v in bv_vals + sr_vals
                ])

            # Weighted volume z stays server-side (a weighted average, not a
            # threshold count) — now at every tier rather than 90d alone.
            for tier, zt in zip(TIERS, bv_vals):
                if zt is not None:
                    wvz_num[tier] += weight * zt
                    wvz_den[tier] += weight

            # Price levels — day events (replaces the old Price Alerts day_breaches)
            pl = data.get('price_levels') or {}
            day_events = pl.get('day_events')
            try:
                if day_events and len(day_events) > 0:
                    level_breached_today += 1
            except TypeError:
                pass

            # Relative strength vs THIS index's benchmark
            rs_map = data.get('relative_strength_by_benchmark') or {}
            rs_row = rs_map.get(rs_benchmark) if rs_benchmark else None
            if rs_row is not None:
                # Counted PER TIER. RS is measured once per beta tier, so the
                # sign of session_rs depends on which tier is being shown — a
                # stock can outperform on a 22-day beta and underperform on a
                # 252-day one, and that is a real disagreement, not noise.
                #
                # This was pinned to session_rs_90d, which meant that once the
                # tier selector shipped, this one row of the index card answered
                # a 90d question while every other row on the same card followed
                # the trader's selection. No error, no visible symptom.
                #
                # Stored per tier rather than derived at display time because it
                # counts a SIGN, not a threshold crossing — see the
                # `positive_rs_*` comment on IndexSummary for why that
                # distinction is the principle rather than an exception to it.
                for tier in TIERS:
                    rs_val = rs_row.get(f'session_rs_{tier}d')
                    if rs_val is None:
                        continue
                    try:
                        rsv = float(rs_val)
                    except (TypeError, ValueError):
                        continue
                    rs_evaluated[tier] += 1
                    if rsv > 0:
                        positive_rs[tier] += 1
                    elif rsv < 0:
                        negative_rs[tier] += 1

        # Leadership: top 5 / bottom 5 by contribution_bps. bottom_start is
        # clamped to never go below 5 (where top_contributors ends), so the
        # two slices never overlap — with fewer than 10 total constituents
        # this naturally shrinks bottom_detractors (down to empty) instead of
        # letting the same stock appear in both lists.
        contrib_rows.sort(key=lambda r: r['contribution_bps'], reverse=True)
        top_contributors = contrib_rows[:5]
        bottom_start = max(5, len(contrib_rows) - 5)
        bottom_detractors = list(reversed(contrib_rows[bottom_start:]))

        # Cross-sectional dispersion (weight-aware std dev of intraday returns)
        if intraday_pairs and wir_den > 0:
            mean_w = wir_num / wir_den
            var_num = sum(w * (iv - mean_w) ** 2 for w, iv in intraday_pairs)
            var_w = var_num / wir_den
            try:
                dispersion = math.sqrt(var_w) if var_w >= 0 else None
            except ValueError:
                dispersion = None
        else:
            dispersion = None

        # Ex-top-5 weighted intraday return (drop the 5 highest-weighted constituents)
        if intraday_pairs:
            pairs_by_weight = sorted(intraday_pairs, key=lambda p: p[0], reverse=True)
            ex_pairs = pairs_by_weight[5:]
            if ex_pairs:
                ex_num = sum(w * iv for w, iv in ex_pairs)
                ex_den = sum(w for w, _ in ex_pairs)
                ex_top5 = (ex_num / ex_den) if ex_den > 0 else None
            else:
                ex_top5 = None
        else:
            ex_top5 = None

        # Live price + intraday %
        index_price, intraday_pct = index_prices.get(index_name, (None, None))

        def _ratio(num, den):
            if den is None or den == 0:
                return None
            return round(num / den, 4)

        return IndexSummary(
            index_name=index_name,
            index_price=index_price,
            intraday_pct=round(intraday_pct, 3) if intraday_pct is not None else None,
            total_constituents=len(constituents),

            advancers=advancers,
            decliners=decliners,
            unchanged=unchanged,

            above_vwap=above_vwap,
            below_vwap=below_vwap,
            vwap_evaluated=vwap_evaluated,

            in_breakout_up=in_breakout_up,
            in_breakout_down=in_breakout_down,
            breakout_evaluated=breakout_evaluated,

            level_breached_today=level_breached_today,

            **{f'positive_rs_{t}d': positive_rs[t] for t in TIERS},
            **{f'negative_rs_{t}d': negative_rs[t] for t in TIERS},
            **{f'rs_evaluated_{t}d': rs_evaluated[t] for t in TIERS},

            weighted_intraday_return=_ratio(wir_num, wir_den),
            **{f'weighted_volume_velocity_{t}d': _ratio(wvv_num[t], wvv_den[t])
               for t in TIERS},
            **{f'weighted_volume_zscore_{t}d': _ratio(wvz_num[t], wvz_den[t])
               for t in TIERS},
            weighted_vwap_deviation=_ratio(wvd_num, wvd_den),

            constituent_z=_build_constituent_z(cz_keys, cz_values),

            cross_sectional_dispersion=round(dispersion, 4) if dispersion is not None else None,
            ex_top5_intraday_return=round(ex_top5, 4) if ex_top5 is not None else None,

            top_contributors=top_contributors,
            bottom_detractors=bottom_detractors,

            # IST-aware, like every other timestamp writer. This single line
            # was naive while the other eight were aware, which made
            # HYDRATION_TIMESTAMP's parsed TYPE depend on which table won a
            # lexicographic max() by microseconds — see _hydrate_cache_from_db.
            timestamp=datetime.now(IST).isoformat(),
        )


class MetricsConfig:
    """Simple configuration for metrics system"""

    def __init__(self):
        # A `calculators` dict of {'name': {'enabled': True}} used to live here.
        # Deleted: nothing ever consulted it, so it read as a live
        # enable/disable switch that would have silently done nothing. It was
        # also stale — 8 entries, missing rolling_momentum — which is what a
        # never-read config always becomes.
        self.timing = {
            'check_interval': COLLECTION_INTERVAL_SECONDS,
            'startup_delay': 5
        }

class MetricsOrchestrator:
    """Main metrics system orchestrator - collects and caches metrics from all calculators"""

    # `app_config: dict = None` / `self.app_config` were removed —
    # assigned and never read. main.py loaded config.json solely to pass it
    # here; every real consumer (OHLCScheduleManager, dashboard's _EMA_SPAN_*,
    # _ATR_PERIOD, _Z_HIGHLIGHT, precompute_beta) opens the file itself.
    def __init__(self, config: MetricsConfig = None, config_path: str = "config.json"):
        self.config = config or MetricsConfig()
        self.ohlc_db_manager = None
        self.metrics_db_manager = None
        self.running = False

        # VWAP is the one calculator that holds cross-cycle state (Kind B —
        # previous_vwap_displacement updates every cycle, not at block
        # boundaries) — persistent adapter instance, mirrors the old
        # price_alerts_calc pattern. Created once metrics_db_manager +
        # asset_loader are both ready, below.
        self.vwap_calc = None

        # Initialize PriceDataManager for real-time ZMQ price feed
        self.price_data_manager = None

        # Initialize AssetLoader for asset information
        self.asset_loader = None

        # Aggregator that derives per-index breadth + leadership from the per-asset cache
        self.index_aggregator = None

        # Track last written time_block per asset to prevent redundant DB writes.
        # The calculator returns the same block's z-score every 30-sec cycle until the
        # next block completes; writing it each cycle produces meaningless repeated rows.
        self._last_written_time_block = {}  # key: (asset_id, segment_code) → block number

        # The trading day whose morning step (previous-close fill + beta refit)
        # has run — see _fill_previous_close_and_refit_beta. In memory: after a
        # restart it runs again, which is harmless (same first readings, same
        # closes, same beta).
        self._close_filled_date = None
        # (asset_id, segment_code) pairs that had no reading with a close when
        # the morning step ran — a quote missing from the day's first fetch.
        # Retried every cycle until each arrives; see _fill_late_closes.
        self._closes_missing = set()

        # ============================================================
        # Shared intraday history store block-boundary state
        # ============================================================
        # The 25-block grid lives in calculators/time_blocks.py. A 25-entry
        # dict used to be redeclared here, and its own comment said it "mirrors
        # the pattern in engine.py's boundary detector", i.e. it was written by
        # copying. Four verbatim copies of the same brackets is how the engine's
        # writes and the calculators' reads could have drifted apart silently.
        # Set of (date_iso, block) tuples already written this session. Prevents
        # duplicate writes when multiple 30-sec metrics cycles fall within the
        # same write window (_SHARED_STORE_WRITE_WINDOW after a block end). The
        # composite PK on the shared-store
        # table already enforces idempotency at the DB layer via INSERT OR
        # REPLACE, but this guard avoids the redundant DB round-trip.
        self._written_intraday_blocks = set()

        # Initialize schedule manager (reads the "schedule" section of the
        # same config.json — same file engine.py's OHLCScheduleManager reads)
        self.schedule_manager = None
        try:
            from engine import OHLCScheduleManager
            self.schedule_manager = OHLCScheduleManager(config_path)
            logger.info(f"Schedule manager initialized: enabled={self.schedule_manager.schedule_config['enabled']}")
        except Exception as e:
            logger.warning(f"Schedule manager initialization failed: {e}. Metrics will run continuously.")

        # Initialize historical database managers first — every adapter that reads
        # cross-day history depends on the Reader; the Writer handles shared-store
        # writes and β cache upserts.
        try:
            from calculators.historical_database_manager import (
                HistoricalDatabaseReader,
                HistoricalDatabaseWriter,
            )
            self.historical_db_manager = HistoricalDatabaseReader('data/historical.db')
            self.historical_db_writer = HistoricalDatabaseWriter('data/historical.db')
            logger.info("Historical database managers initialized successfully (reader + writer)")
        except Exception as e:
            logger.warning(f"Historical database manager initialization failed: {e}")
            self.historical_db_manager = None
            self.historical_db_writer = None

        # Initialize metrics database manager for persistent storage — created before
        # the OHLC block below so the VWAP adapter can hydrate its Kind-B state from
        # the most recent stored row on startup.
        try:
            from calculators.metrics_database_manager import MetricsDatabaseManager
            self.metrics_db_manager = MetricsDatabaseManager()
            logger.info("Metrics database manager initialized successfully")
        except Exception as e:
            logger.warning(f"Metrics database manager initialization failed: {e}")
            self.metrics_db_manager = None

        # Initialize OHLC database system
        if MOMENTUM_SYSTEM_AVAILABLE:
            try:
                self.ohlc_db_manager = OHLCDatabaseReader('data/ohlc.db')
                logger.info("OHLC database system initialized successfully")

                # Initialize PriceDataManager for real-time price data
                from calculators.price_data_manager import PriceDataManager
                self.price_data_manager = PriceDataManager()
                _register_price_feed(self.price_data_manager)
                logger.info("PriceDataManager initialized successfully")

                # Initialize AssetLoader for asset information
                from engine import AssetLoader
                self.asset_loader = AssetLoader("assets/assets_select.csv")
                if self.asset_loader.load_assets():
                    logger.info(f"AssetLoader initialized with {len(self.asset_loader.assets_df)} assets")
                else:
                    logger.warning("AssetLoader failed to load assets")
                    self.asset_loader = None

                # VWAP adapter as a persistent instance (Kind-B state), hydrated
                # from metrics_db_manager on construction.
                if VWAP_SYSTEM_AVAILABLE:
                    self.vwap_calc = create_vwap_calculator(
                        self.ohlc_db_manager, self.historical_db_manager,
                        metrics_db_manager=self.metrics_db_manager, asset_loader=self.asset_loader,
                        # VWAP is the exchange's own `average_price`, read from
                        # the in-memory price feed — not reconstructed from ticks.
                        price_data_manager=self.price_data_manager,
                    )
                    logger.info("VWAP adapter initialized (persistent instance)")

            except Exception as e:
                logger.warning(f"OHLC database system initialization failed: {e}")
                self.ohlc_db_manager = None
                self.price_data_manager = None
                self.asset_loader = None
                self.vwap_calc = None
        else:
            logger.warning("OHLC database system not available")

        # Initialize the per-index aggregator (depends on asset_loader being loaded)
        try:
            self.index_aggregator = IndexAggregator(self.asset_loader)
            logger.info("IndexAggregator initialized successfully")
        except Exception as e:
            logger.warning(f"IndexAggregator initialization failed: {e}")
            self.index_aggregator = None


    def _fill_previous_close_and_refit_beta(self, now: datetime) -> None:
        """
        The morning step, once per trading day, on the first cycle that has
        prices — BEFORE any calculator runs:

          1. each asset's close_price from today's FIRST reading — the
             exchange's official close of the previous trading day;
          2. written into that day's daily_ohlc row (a close-only row if the
             day has none), dated market_calendar.previous_trading_day —
             never simply "yesterday";
          3. beta refitted from the archive (scripts/precompute_beta.py) —
             after the fill, so today's session uses the newest daily return;
          4. the historical reader's remembered answers dropped, so this very
             cycle reads the new previous close and beta.

        The 15:34 archive writes each day's row WITHOUT a close — the official
        close is not known until the next morning's first reading. Done here,
        in the metrics thread, there is no blank at the open: the engine stores
        each reading before publishing it, so by the time prices reach this
        thread today's first readings are on disk. Done by the once-a-minute
        scheduler instead, Momentum, Relative Strength and Rolling Momentum
        would show no previous close for ~1-2½ minutes after the first reading.

        A stock with no reading in that first batch — its quote dropped as
        malformed, or late — is remembered in `_closes_missing` and retried on
        every later cycle (`_fill_late_closes`). Without the retry it showed no
        previous close all day, and its previous day's close was never written.
        """
        if self.price_data_manager is None or not self.price_data_manager.is_ready():
            return
        if not self.ohlc_db_manager or not self.historical_db_writer:
            return
        today = now.date()
        if self._close_filled_date == today:
            self._fill_late_closes(today)
            return
        try:
            closes = self.ohlc_db_manager.get_first_reading_closes(today)
            if closes.empty:
                return  # no reading with a close yet — try next cycle
            target = previous_trading_day(today)
            self.historical_db_writer.fill_closes(target, closes)
            logger.info(f"Morning step: {len(closes)} closes for {target} "
                        f"from {today}'s first readings")
            self._refit_beta(today)
            self._forget_cached_history()
            self._close_filled_date = today
            self._closes_missing = self._all_assets() - {
                (int(a), s) for a, s in zip(closes['asset_id'], closes['segment_code'])}
            if self._closes_missing:
                logger.warning(f"Morning step: no reading with a close yet for "
                               f"{len(self._closes_missing)} asset(s) — retried every cycle: "
                               f"{sorted(self._closes_missing)}")
        except Exception as e:
            logger.error(f"Morning step (previous-close fill) failed: {e}")

    def _fill_late_closes(self, today: date) -> None:
        """The morning step for the assets that had no reading with a close when
        it ran: fill any whose first reading has since arrived, refit beta and
        drop the reader's remembered answers, so their previous close is in use
        within a cycle of their first reading. Costs one indexed query per
        missing asset per cycle, and nothing once none is missing."""
        if not self._closes_missing:
            return
        try:
            late = self.ohlc_db_manager.get_first_reading_closes_for(
                today, sorted(self._closes_missing))
            if late.empty:
                return
            target = previous_trading_day(today)
            self.historical_db_writer.fill_closes(target, late)
            self._closes_missing -= {
                (int(a), s) for a, s in zip(late['asset_id'], late['segment_code'])}
            logger.info(f"Morning step: {len(late)} late close(s) for {target}; "
                        f"{len(self._closes_missing)} asset(s) still missing")
            self._refit_beta(today)
            self._forget_cached_history()
        except Exception as e:
            logger.error(f"Morning step (late closes) failed: {e}")

    def _all_assets(self) -> set:
        """Every (asset_id, segment_code) in the asset list; empty without one."""
        frame = getattr(self.asset_loader, 'assets_df', None)
        if frame is None:
            return set()
        return {(int(a), s) for a, s in zip(frame['asset_id'], frame['segment_code'])}

    def _refit_beta(self, today: date) -> None:
        """Refit the beta cache from the archive (scripts/precompute_beta.py)."""
        try:
            result = subprocess.run([sys.executable, "scripts/precompute_beta.py"],
                                    capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                logger.info(f"Morning step: beta refitted for {today}")
            else:
                logger.error(f"precompute_beta.py failed (exit {result.returncode}): "
                             f"{result.stderr}")
        except Exception as e:
            logger.error(f"precompute_beta.py error: {e}")

    def _forget_cached_history(self) -> None:
        """Drop the historical reader's remembered answers, so this very cycle
        reads the new previous close and beta."""
        if self.historical_db_manager is not None and self.historical_db_manager.cache:
            self.historical_db_manager.cache.clear()

    def collect_metrics(self) -> None:
        """Run all calculator systems and collect metrics from each"""

        # Single timestamp for the whole cycle — passed to every calculator and
        # to _maybe_write_intraday_block() below, so they all agree on which
        # 15-min block is "current" even if this cycle straddles a boundary
        # while running (was previously each independently calling
        # datetime.now(), which could disagree mid-cycle and write one
        # calculator's data under the wrong block).
        now = datetime.now(IST)

        # Before any calculator: the previous trading day's close must be in
        # the archive when Momentum, RS and Rolling Momentum read it.
        self._fill_previous_close_and_refit_beta(now)

        # Initialize result variables for cache update
        momentum_results = []
        vwap_results = []
        breakout_results = []
        rel_strength_results = []
        volume_block_results = []
        # Recent-block ZQ series produced by Volume Block and consumed by Volume
        # Dynamics in the same cycle — see run_volume_block_analysis' docstring.
        volume_block_zq_history = {}
        volume_dynamics_results = []
        price_volume_divergence_results = []
        price_levels_results = []
        rolling_momentum_results = []

        try:
            # Momentum
            if MOMENTUM_SYSTEM_AVAILABLE and self.ohlc_db_manager:
                try:
                    momentum_results = run_momentum_analysis(
                        self.ohlc_db_manager,
                        self.historical_db_manager,
                        self.price_data_manager,
                        asset_loader=self.asset_loader,
                        now=now,
                    )
                    if momentum_results and self.metrics_db_manager:
                        self.metrics_db_manager.store_momentum_metrics(_serialize_rows(momentum_results))
                        with CACHE_LOCK:
                            CALCULATOR_HEALTH['momentum'] = datetime.now(IST)
                except Exception as e:
                    logger.error(f"Error in momentum system: {e}")

            # Breakout
            if BREAKOUT_SYSTEM_AVAILABLE and self.ohlc_db_manager:
                try:
                    breakout_results = run_breakout_analysis(
                        self.ohlc_db_manager, self.price_data_manager,
                        asset_loader=self.asset_loader,
                        now=now,
                    )
                    if breakout_results and self.metrics_db_manager:
                        self.metrics_db_manager.store_breakout_metrics(_serialize_rows(breakout_results))
                        with CACHE_LOCK:
                            CALCULATOR_HEALTH['breakout'] = datetime.now(IST)
                except Exception as e:
                    logger.error(f"Error in breakout system: {e}")

            # VWAP — persistent adapter instance (Kind-B state)
            if VWAP_SYSTEM_AVAILABLE and self.vwap_calc:
                try:
                    vwap_results = self.vwap_calc.calculate_vwap_analysis(now=now)
                    if vwap_results and self.metrics_db_manager:
                        self.metrics_db_manager.store_vwap_metrics(_serialize_rows(vwap_results))
                        with CACHE_LOCK:
                            CALCULATOR_HEALTH['vwap'] = datetime.now(IST)
                except Exception as e:
                    logger.error(f"Error in VWAP system: {e}")

            # Relative Strength
            if RELATIVE_STRENGTH_SYSTEM_AVAILABLE and self.ohlc_db_manager:
                try:
                    rel_strength_results = run_relative_strength_analysis(
                        self.ohlc_db_manager,
                        self.historical_db_manager,
                        self.price_data_manager,
                        asset_loader=self.asset_loader,
                        now=now,
                    )
                    if rel_strength_results and self.metrics_db_manager:
                        self.metrics_db_manager.store_relative_strength_metrics(_serialize_rows(rel_strength_results))
                        with CACHE_LOCK:
                            CALCULATOR_HEALTH['relative_strength'] = datetime.now(IST)
                except Exception as e:
                    logger.error(f"Error in relative strength system: {e}")

            # Volume Block (replaces the old Time Block Volume calculator)
            if VOLUME_BLOCK_SYSTEM_AVAILABLE and self.ohlc_db_manager and self.historical_db_manager:
                try:
                    volume_block_results, volume_block_zq_history = run_volume_block_analysis(
                        self.ohlc_db_manager, self.historical_db_manager,
                        asset_loader=self.asset_loader,
                        now=now,
                    )
                    if volume_block_results:
                        if self.metrics_db_manager:
                            # Only write rows whose time_block changed since the last write —
                            # avoids accumulating identical rows every 30-sec cycle.
                            # And only once the block's readings are complete: block 25
                            # takes readings until 15:31 (block_reading_end_time), so
                            # its row, written once, waits until then — a row stored
                            # at 15:30:xx would never see an auction result arriving
                            # before 15:31. Blocks 1-24 end before they are reported.
                            new_block_results = [
                                r for r in volume_block_results
                                if self._last_written_time_block.get((r.asset_id, r.segment_code)) != r.time_block
                                and r.time_block is not None
                                and now >= datetime.combine(now.date(), block_reading_end_time(r.time_block),
                                                            tzinfo=IST)
                            ]
                            if new_block_results:
                                # Record AFTER the store: a failed write must be
                                # retried next cycle, not marked done — otherwise
                                # that cycle finds nothing new, stamps health, and
                                # the block's row is lost with the bar green.
                                self.metrics_db_manager.store_volume_block_metrics(_serialize_rows(new_block_results))
                                for r in new_block_results:
                                    self._last_written_time_block[(r.asset_id, r.segment_code)] = r.time_block
                        with CACHE_LOCK:
                            CALCULATOR_HEALTH['volume_block'] = datetime.now(IST)
                except Exception as e:
                    logger.error(f"Error in volume block system: {e}")

            # Volume Dynamics — a pure derivative of Volume Block, reading no
            # database at all. The recent-block ZQ series arrives in memory from
            # Volume Block's *this-cycle* output, at all four tiers.
            #
            # It used to re-query historical.db.intraday_time_block_metrics for
            # prior blocks' `block_volume_z_90`. That column was the only DERIVED
            # value in a table of raw observations, was frozen against whatever
            # history existed when it was written, and existed at one tier only.
            # It is gone; this handover replaced it.
            #
            # A stock whose current block is incomplete has no ZQ, so it is not in
            # the series; Volume Dynamics is told separately, to report it.
            volume_block_incomplete = {(r.asset_id, r.segment_code) for r in volume_block_results
                                       if r.status == 'incomplete_block'}
            if VOLUME_DYNAMICS_SYSTEM_AVAILABLE and (volume_block_zq_history or volume_block_incomplete):
                try:
                    volume_dynamics_results = run_volume_dynamics_analysis(
                        volume_block_zq_history,
                        incomplete_blocks=volume_block_incomplete,
                        asset_loader=self.asset_loader,
                        now=now,
                    )
                    if volume_dynamics_results and self.metrics_db_manager:
                        self.metrics_db_manager.store_volume_dynamics_metrics(_serialize_rows(volume_dynamics_results))
                        with CACHE_LOCK:
                            CALCULATOR_HEALTH['volume_dynamics'] = datetime.now(IST)
                except Exception as e:
                    logger.error(f"Error in volume dynamics system: {e}")

            # Price-Volume Divergence — starts cold, by design; raw pvd
            # computes every cycle from live 5-second ticks, z-tiers stay None
            # until enough live sessions accumulate in the shared store.
            if PRICE_VOLUME_DIVERGENCE_SYSTEM_AVAILABLE and self.ohlc_db_manager and self.historical_db_manager:
                try:
                    price_volume_divergence_results = run_price_volume_divergence_analysis(
                        self.ohlc_db_manager, self.historical_db_manager,
                        asset_loader=self.asset_loader,
                        now=now,
                    )
                    if price_volume_divergence_results and self.metrics_db_manager:
                        self.metrics_db_manager.store_price_volume_divergence_metrics(
                            _serialize_rows(price_volume_divergence_results)
                        )
                        with CACHE_LOCK:
                            CALCULATOR_HEALTH['price_volume_divergence'] = datetime.now(IST)
                except Exception as e:
                    logger.error(f"Error in price-volume divergence system: {e}")

            # Price Levels (replaces the old Price Alerts calculator)
            if PRICE_LEVELS_SYSTEM_AVAILABLE and self.ohlc_db_manager:
                try:
                    price_levels_results = run_price_levels_analysis(
                        self.ohlc_db_manager, asset_loader=self.asset_loader,
                        now=now,
                    )
                    if price_levels_results and self.metrics_db_manager:
                        self.metrics_db_manager.store_price_levels_metrics(_serialize_rows(price_levels_results))
                        with CACHE_LOCK:
                            CALCULATOR_HEALTH['price_levels'] = datetime.now(IST)
                except Exception as e:
                    logger.error(f"Error in price levels system: {e}")

            # Rolling Momentum — continuously re-evaluated at every completed
            # 15-min block (unlike momentum, which is pinned to block 1).
            if ROLLING_MOMENTUM_SYSTEM_AVAILABLE and self.price_data_manager and self.historical_db_manager:
                try:
                    rolling_momentum_results = run_rolling_momentum_analysis(
                        self.price_data_manager, self.historical_db_manager,
                        asset_loader=self.asset_loader,
                        now=now,
                    )
                    if rolling_momentum_results and self.metrics_db_manager:
                        self.metrics_db_manager.store_rolling_momentum_metrics(_serialize_rows(rolling_momentum_results))
                        with CACHE_LOCK:
                            CALCULATOR_HEALTH['rolling_momentum'] = datetime.now(IST)
                except Exception as e:
                    logger.error(f"Error in rolling momentum system: {e}")

        except Exception as e:
            logger.error(f"Error during alert check: {e}")

        # Update shared cache with ALL results (for dashboard).
        # Serialize every typed row list to plain dicts at this boundary — dashboard
        # + downstream consumers speak dicts.
        cycle_metrics = {}
        try:
            cycle_metrics = {
                'momentum': _serialize_rows(momentum_results),
                'breakout': _serialize_rows(breakout_results),
                'vwap': _serialize_rows(vwap_results),
                'relative_strength': _serialize_rows(rel_strength_results),
                'volume_block': _serialize_rows(volume_block_results),
                'volume_dynamics': _serialize_rows(volume_dynamics_results),
                'price_volume_divergence': _serialize_rows(price_volume_divergence_results),
                'price_levels': _serialize_rows(price_levels_results),
                'rolling_momentum': _serialize_rows(rolling_momentum_results),
            }
            update_metrics_cache(cycle_metrics)
            logger.debug(f"Cache updated with metrics for all calculators")
        except Exception as e:
            logger.error(f"Error updating metrics cache: {e}")

        # Aggregate per-asset metrics into per-index breadth + leadership summaries.
        # IndexAggregator.compute() returns Dict[str, IndexSummary].
        # Serialize to dicts via dataclasses.asdict at the storage boundary so the cache
        # + metrics.db consumers keep receiving the same dict shape they always did.
        #
        # Fed THIS CYCLE'S rows, the very ones just published — not
        # get_latest_metrics(), which deep-copies the whole cache (up to ~750 rows) to
        # protect callers that might modify it. IndexAggregator only reads (every
        # access a .get(); it builds structures of its own), so the copy bought
        # nothing, every 30 seconds. If the rows could not be built, it is
        # skipped and the index cards keep their previous values, as on any
        # aggregation failure.
        try:
            if self.index_aggregator and cycle_metrics:
                index_summary_typed = self.index_aggregator.compute(
                    cycle_metrics, self.price_data_manager
                )
                index_summary = {
                    k: dataclasses.asdict(v) for k, v in index_summary_typed.items()
                }
                update_index_metrics_cache(index_summary)
                if self.metrics_db_manager and index_summary:
                    self.metrics_db_manager.store_index_metrics(index_summary)
                logger.debug(f"Index summaries computed for {len(index_summary)} indices")
        except Exception as e:
            logger.error(f"Error in index aggregation: {e}")

        # Shared intraday history store — one row per asset for the block that
        # has just ended. The values come from that block's own readings in
        # ohlc.db, not from LATEST_METRICS; see _maybe_write_intraday_block.
        try:
            self._maybe_write_intraday_block(now)
        except Exception as e:
            logger.error(f"Error in intraday block-boundary writer: {e}")

    def _maybe_write_intraday_block(self, now: datetime) -> None:
        """
        Write one row per asset to `historical.db.intraday_time_block_metrics`
        for the block that has just ended — on the first metrics cycle within
        `_SHARED_STORE_WRITE_WINDOW` after its end — once per (date, block).

        `now` is the same timestamp passed to every calculator this cycle (see
        collect_metrics()) — using one shared value instead of an independent
        datetime.now() call here prevents this writer from disagreeing with the
        calculators about which block just completed when a cycle straddles a
        boundary while running.

        Row contents — each block's value AT ITS END, read from the block's own
        stored readings in ohlc.db (see `_block_end_values`):
          - `session_price_move`: (the block's last price / previous close − 1) × 100.
          - `vwap_displacement`: ln(last price / the exchange's VWAP), same reading.
          - `pvd_raw`: PV Divergence's Fisher value over the block's readings —
            the same 15 minutes the live value covers.
        RAW OBSERVATIONS ONLY — never a value normalised against history, since
        a stored z is frozen against whatever history existed when it was
        written.

        These used to come from this cycle's calculator output, on the first
        cycle inside a bracket CENTRED on the block end. With cycles ~34 s
        apart that cycle fell in the bracket's first half: on 2026-09-07 every
        block was written 21-58 seconds before it ended, from prices read
        earlier still — and every calculator that z-scores against this store
        was comparing today with values from before the block closed. Read from
        the readings, the value is exact and can be rebuilt from ohlc.db for as
        long as it keeps them (5 trading days).

        The `_written_intraday_blocks` set guards against duplicate writes when
        several cycles fall inside one write window. The DB layer's INSERT OR
        REPLACE would handle duplicates safely regardless, but this avoids the
        redundant round-trip. Set entries older than the current calendar day
        are dropped opportunistically.
        """
        if (not self.historical_db_writer or not self.asset_loader
                or self.asset_loader.assets_df is None or not self.ohlc_db_manager):
            return

        current_date = now.date().isoformat()

        # Drop stale (date, block) entries from prior calendar days.
        stale = {entry for entry in self._written_intraday_blocks if entry[0] != current_date}
        if stale:
            self._written_intraday_blocks -= stale

        # The block that has most recently ENDED — written only while `now` is
        # within the write window after its reading end. For blocks 1-24 that
        # is the block end; block 25 also takes the session's final readings,
        # so it waits until 15:31 and its stored price is the closing price.
        matched_block = current_completed_block(now)
        if matched_block is None:
            return  # before the first block ends
        block_end = datetime.combine(now.date(), block_reading_end_time(matched_block), tzinfo=IST)
        if now < block_end:
            return  # block 25's final readings may still arrive
        if now - block_end > _SHARED_STORE_WRITE_WINDOW:
            return  # not just after a block end

        if (current_date, matched_block) in self._written_intraday_blocks:
            return  # already wrote this block today

        try:
            # The previous close is fixed for the day; the momentum rows carry it.
            previous_close_by_key: Dict[tuple, float] = {}
            try:
                for m in (get_latest_metrics().get('momentum') or []):
                    aid, seg, pc = m.get('asset_id'), m.get('segment_code'), m.get('previous_close')
                    if aid is not None and seg is not None and finite_positive(pc):
                        previous_close_by_key[(aid, seg)] = float(pc)
            except Exception as e:
                logger.error(f"Error reading previous closes for the shared store: {e}")

            rows = []
            for _, asset_row in self.asset_loader.assets_df.iterrows():
                asset_id_int = int(asset_row['asset_id'])
                segment_code = asset_row['segment_code']
                rows.append({
                    'date': current_date,
                    'asset_id': asset_id_int,
                    'segment_code': segment_code,
                    'symbol': asset_row.get('asset_name'),
                    'time_block': matched_block,
                    **self._block_end_values(
                        asset_id_int, segment_code, now.date(), matched_block,
                        previous_close_by_key.get((asset_id_int, segment_code)),
                    ),
                })

            written = self.historical_db_writer.store_intraday_time_block_metrics(rows)
            if written:
                self._written_intraday_blocks.add((current_date, matched_block))
                logger.info(
                    f"Shared intraday store: wrote {written} rows for "
                    f"(date={current_date}, block={matched_block})"
                )
        except Exception as e:
            logger.error(
                f"Error writing intraday_time_block_metrics for block {matched_block}: {e}"
            )

    def _block_end_values(self, asset_id: int, segment_code: str, day: date, block: int,
                          previous_close: Optional[float]) -> Dict[str, Optional[float]]:
        """The three shared-store observations for one asset at one block's end.

        Values come only from readings stamped inside the block — [block start,
        block reading end) — by block_snapshots.block_end_values, the one
        function scripts/backfill.py also uses to rebuild a day, so the live
        history and a rebuilt one are the same quantity. The read starts
        BLOCK_EDGE_TOLERANCE before the block: PV Divergence's value needs the
        reading before the block's first one (continuous_price_volume_series).
        """
        block_start = datetime.combine(day, block_start_time(block), tzinfo=IST)
        block_end = datetime.combine(day, block_reading_end_time(block), tzinfo=IST)
        try:
            readings = self.ohlc_db_manager.get_intraday_data_range(
                asset_id, block_start - BLOCK_EDGE_TOLERANCE, block_end, segment_code=segment_code)
            day_first = self.ohlc_db_manager.session_first_reading(asset_id, segment_code, day)
        except Exception as e:
            logger.error(f"Shared store: reading block {block_start:%H:%M}-{block_end:%H:%M} "
                         f"failed for {asset_id}/{segment_code}: {e}")
            readings, day_first = None, None
        return block_end_values(readings, previous_close, day, block, day_first)

    def _hydrate_cache_from_db(self) -> None:
        """
        Prefill LATEST_METRICS + LATEST_INDEX_METRICS from metrics.db
        so the dashboard is never blank outside market hours. Runs once at
        MetricsOrchestrator.start(), before the main loop.

        Contract: the dashboard takes current values only from these in-memory
        caches, never from the database, and that stays intact — this only
        fills the caches. A first live cycle later overwrites the
        hydrated values and clears HYDRATION_TIMESTAMP via update_metrics_cache.
        Wrapped in try/except: any failure (empty DB, corruption, schema drift)
        logs and leaves the caches empty.
        """
        try:
            if self.metrics_db_manager is None:
                logger.info("Hydration skipped: metrics_db_manager not available")
                return

            db = self.metrics_db_manager
            hydration_map = {
                'momentum':                 db.get_latest_momentum,
                'breakout':                 db.get_latest_breakout,
                'vwap':                     db.get_latest_vwap,
                'relative_strength':        db.get_latest_relative_strength,
                'volume_block':             db.get_latest_volume_block,
                'volume_dynamics':          db.get_latest_volume_dynamics,
                'price_volume_divergence':  db.get_latest_price_volume_divergence,
                'price_levels':             db.get_latest_price_levels,
                'rolling_momentum':         db.get_latest_rolling_momentum,
            }

            all_max_ts: list = []
            populated: dict = {}
            for cache_key, reader in hydration_map.items():
                try:
                    rows, max_ts = reader()
                except Exception as e:
                    logger.warning(f"Hydration read failed for {cache_key}: {e}")
                    rows, max_ts = [], None
                populated[cache_key] = rows
                if max_ts:
                    all_max_ts.append(max_ts)

            # Index metrics — dict shape, not list.
            try:
                index_summaries, index_ts = db.get_latest_index_metrics()
            except Exception as e:
                logger.warning(f"Hydration read failed for index_metrics: {e}")
                index_summaries, index_ts = {}, None
            if index_ts:
                all_max_ts.append(index_ts)

            # Publish under CACHE_LOCK. Do NOT touch cycle_number or timestamp
            # (those track live cycles). Downstream reads see the exact same
            # dict shape a live cycle would produce.
            with CACHE_LOCK:
                for cache_key, rows in populated.items():
                    LATEST_METRICS[cache_key] = rows
                global LATEST_INDEX_METRICS
                LATEST_INDEX_METRICS = index_summaries

            # Convert the max observed timestamp string to a datetime for the
            # dashboard's staleness banner. Metric-DB timestamps are stored as
            # ISO strings via datetime.now().isoformat(); fromisoformat parses
            # them cleanly. Fall back to now() only if nothing was found.
            hydration_dt = None
            if all_max_ts:
                try:
                    hydration_dt = datetime.fromisoformat(max(all_max_ts))
                except (ValueError, TypeError):
                    hydration_dt = None

            if hydration_dt is None:
                logger.info("Hydration: metrics.db empty — dashboard will show 'NO DATA' until first live cycle")
                return

            set_hydration_timestamp(hydration_dt)

            total = sum(len(v) for v in populated.values()) + len(index_summaries)
            # NOTE: this strftime is DISPLAY-ONLY — it renders an already-parsed
            # datetime for a human reading the terminal. It is NOT a query cutoff.
            # Every other '%Y-%m-%d %H:%M:%S' in this codebase used to be one, and
            # all of them were bugs: created_at is stored as ISO-8601 with a 'T'
            # separator, so a space-separated string compares wrong as SQLite TEXT.
            # Query cutoffs must use .isoformat(); parsing must use fromisoformat().
            logger.info(
                f"Hydration complete: {total} rows loaded from metrics.db "
                f"(latest timestamp: {hydration_dt.strftime('%Y-%m-%d %H:%M:%S')})"
            )
        except Exception as e:
            logger.error(f"Cache hydration failed: {e}", exc_info=True)

    def start(self):
        """Start the metrics orchestrator system"""
        logger.info("=== Metrics Orchestrator Starting ===")
        available_calculators = [name for name, available in {
            'momentum': MOMENTUM_SYSTEM_AVAILABLE,
            'vwap': VWAP_SYSTEM_AVAILABLE,
            'breakout': BREAKOUT_SYSTEM_AVAILABLE,
            'relative_strength': RELATIVE_STRENGTH_SYSTEM_AVAILABLE,
            'volume_block': VOLUME_BLOCK_SYSTEM_AVAILABLE,
            'volume_dynamics': VOLUME_DYNAMICS_SYSTEM_AVAILABLE,
            'price_volume_divergence': PRICE_VOLUME_DIVERGENCE_SYSTEM_AVAILABLE,
            'price_levels': PRICE_LEVELS_SYSTEM_AVAILABLE,
            'rolling_momentum': ROLLING_MOMENTUM_SYSTEM_AVAILABLE,
        }.items() if available]
        logger.info(f"Available calculators: {available_calculators}")
        logger.info(f"Collection interval: {self.config.timing['check_interval']} seconds")

        # Log schedule configuration
        if self.schedule_manager:
            config = self.schedule_manager.schedule_config
            logger.info("Schedule Configuration:")
            logger.info(f"  - Enabled: {config['enabled']}")
            logger.info(f"  - Schedule Type: {config['schedule_type']}")
            logger.info(f"  - Time Window: {config['start_time']} - {config['end_time']}")
            if not config['enabled']:
                logger.warning("Scheduling is DISABLED - metrics will run continuously 24/7")
        else:
            logger.warning("No schedule manager - metrics will run continuously 24/7")

        # Start PriceDataManager background thread
        if self.price_data_manager:
            self.price_data_manager.start()
            logger.info("PriceDataManager background thread started")

        # Hydrate LATEST_METRICS + LATEST_INDEX_METRICS from
        # metrics.db so the dashboard shows the last-known snapshot immediately.
        # Runs BEFORE the startup delay so a page load during the 2-second
        # settle window still gets populated caches.
        self._hydrate_cache_from_db()

        # Startup delay
        time.sleep(self.config.timing['startup_delay'])

        self.running = True

        try:
            while self.running:
                # Check if we're within collection window (with post-close grace so the
                # final cycles catch calculators gated on end-of-day boundaries — e.g.
                # Volume Block's block 25 is reported from 15:30 and stored from 15:31).
                # Engine has its own strict window and is unaffected by this grace.
                if self.schedule_manager:
                    config = self.schedule_manager.schedule_config
                    if config['enabled']:
                        # All three of now_dt, start_dt and end_dt_with_grace
                        # must carry the same awareness: datetime.combine()
                        # returns NAIVE unless given tzinfo, so converting
                        # now_dt alone would make the comparison below raise
                        # TypeError — and that comparison is the gate deciding
                        # whether metrics collects at all.
                        now_dt = datetime.now(IST)
                        sched_mgr = self.schedule_manager
                        in_window = False
                        if sched_mgr._is_scheduled_day(now_dt):
                            start_t = sched_mgr._parse_time(config['start_time'])
                            end_t = sched_mgr._parse_time(config['end_time'])
                            start_dt = datetime.combine(now_dt.date(), start_t, tzinfo=IST)
                            end_dt_with_grace = (datetime.combine(now_dt.date(), end_t, tzinfo=IST)
                                                 + timedelta(seconds=METRICS_POST_CLOSE_GRACE_SECONDS))
                            in_window = start_dt <= now_dt <= end_dt_with_grace
                        if not in_window:
                            logger.debug("Outside collection window - skipping metrics collection (cache frozen)")
                            # Sleep for 1 minute when outside window (keep cache frozen)
                            time.sleep(60)
                            continue

                # Collect metrics from all calculators and update cache
                self.collect_metrics()

                # Sleep in small intervals to allow immediate Ctrl+C handling
                sleep_interval = self.config.timing['check_interval']
                elapsed = 0
                while elapsed < sleep_interval and self.running:
                    time.sleep(min(0.5, sleep_interval - elapsed))  # Sleep max 0.5 seconds at a time
                    elapsed += 0.5

        except KeyboardInterrupt:
            logger.info("Metrics orchestrator stopped by user")
        except Exception as e:
            logger.error(f"Metrics orchestrator error: {e}")
        finally:
            self.stop()

    def stop(self):
        """Stop the metrics orchestrator"""
        self.running = False
        logger.info("=== Metrics Orchestrator Stopped ===")

    def get_status(self) -> Dict[str, Any]:
        """Get system status"""
        status = {
            'running': self.running,
            'calculators_available': {
                'momentum': MOMENTUM_SYSTEM_AVAILABLE,
                'vwap': VWAP_SYSTEM_AVAILABLE,
                'breakout': BREAKOUT_SYSTEM_AVAILABLE,
                'relative_strength': RELATIVE_STRENGTH_SYSTEM_AVAILABLE,
                'volume_block': VOLUME_BLOCK_SYSTEM_AVAILABLE,
                'volume_dynamics': VOLUME_DYNAMICS_SYSTEM_AVAILABLE,
                'price_volume_divergence': PRICE_VOLUME_DIVERGENCE_SYSTEM_AVAILABLE,
                'price_levels': PRICE_LEVELS_SYSTEM_AVAILABLE,
                'rolling_momentum': ROLLING_MOMENTUM_SYSTEM_AVAILABLE,
            },
            'database_connectivity': {
                'ohlc_database': self.ohlc_db_manager is not None,
                'historical_database': self.historical_db_manager is not None,
                'metrics_database': self.metrics_db_manager is not None
            },
            'check_interval': self.config.timing['check_interval']
        }

        return status

def main():
    """Main entry point for standalone execution"""
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    try:
        # Create and start metrics orchestrator
        config = MetricsConfig()
        metrics_orchestrator = MetricsOrchestrator(config)

        print("Metrics Orchestrator initialized with calculators:")
        calculator_status = metrics_orchestrator.get_status()['calculators_available']
        for calc_name, available in calculator_status.items():
            status = "AVAILABLE" if available else "UNAVAILABLE"
            print(f"  - {calc_name}: {status}")
        print()

        metrics_orchestrator.start()

    except Exception as e:
        logger.error(f"Failed to start metrics orchestrator: {e}")
        return 1

    return 0

if __name__ == "__main__":
    import sys
    sys.exit(main())