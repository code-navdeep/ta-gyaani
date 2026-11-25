"""
Typed row schemas — the single-source-of-truth contract for what each
calculator emits into `LATEST_METRICS` (and, via the same dict shape, into
`metrics.db` and the dashboard).

Rewritten for the locked-calculator generation (momentum_calculator.py,
breakout_calculator.py, vwap_deviation_calculator.py,
relative_strength_calculator.py, volume_block.py, volume_dynamics.py,
price_volume_divergence_calculator.py, price_alerts_calculator.py,
rolling_momentum_calculator.py). Each calculator's pure `calculate_*` function
returns a `Result` dataclass; the matching adapter (`calculators/*_adapter.py`)
wraps that `Result` plus identity/timing fields into the Row class here.

Design notes:
  - Every field is `Optional[...]` with a `None` default (or a default_factory
    for lists/dicts) — except Breakout's 0/1 state flags and IndexSummary's
    counters, which default to 0. A sample count that was never computed is
    None (shown as a hyphen), not 0, which would read as "no history".
  - Every z-scored signal carries all 4 tiers (22d/90d/180d/252d), all
    computed. A tier is None until it has exactly its own N samples of
    history (the exact-fill rule) — a schedule, not a fault.
  - VWAP intentionally carries no band/band_breach fields — dropped from the
    locked design, not part of this schema.
  - Dashboard uses the union of these dataclass field sets at import time
    (reflected via `dataclasses.fields()`) to assert that every field it
    reads is actually a field of some Row class. A field added to a
    calculator that forgets to update the dashboard's `_CONSUMED_FIELDS` set
    becomes a boot-time RuntimeError, not a silent "N/A" in the UI.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


# ============================================================================
# Momentum — MOMENTUM_LOCKED_V2. Single fixed opening window (09:15->09:30);
# gap + early_return are each z-scored at all four tiers.
# ============================================================================

@dataclass
class MomentumRow:
    # Identity
    asset_id: Optional[int] = None
    segment_code: Optional[str] = None
    symbol: Optional[str] = None
    # Raw prices
    current_price: Optional[float] = None
    open_price: Optional[float] = None
    previous_close: Optional[float] = None
    price_after_window: Optional[float] = None
    # Today's running day high/low (display-only — feeds the dashboard's Range
    # column; not used by any momentum math). Sourced from the same
    # PriceDataManager.get_current_prices() DataFrame open_price/current_price
    # already come from.
    high_price: Optional[float] = None
    low_price: Optional[float] = None
    # Timing
    timestamp: Optional[str] = None
    # Signals
    gap_return: Optional[float] = None
    gap_direction: Optional[int] = None
    early_return: Optional[float] = None
    continuation: Optional[float] = None
    acceleration: Optional[float] = None
    status: Optional[str] = None
    # Gap z-tier bundles (x4 tiers)
    gap_z_22d: Optional[float] = None
    gap_avg_22d: Optional[float] = None
    gap_std_22d: Optional[float] = None
    gap_data_points_22d: Optional[int] = None
    gap_z_90d: Optional[float] = None
    gap_avg_90d: Optional[float] = None
    gap_std_90d: Optional[float] = None
    gap_data_points_90d: Optional[int] = None
    gap_z_180d: Optional[float] = None
    gap_avg_180d: Optional[float] = None
    gap_std_180d: Optional[float] = None
    gap_data_points_180d: Optional[int] = None
    gap_z_252d: Optional[float] = None
    gap_avg_252d: Optional[float] = None
    gap_std_252d: Optional[float] = None
    gap_data_points_252d: Optional[int] = None
    # Early-return z-tier bundles (x4 tiers)
    early_return_z_22d: Optional[float] = None
    early_return_avg_22d: Optional[float] = None
    early_return_std_22d: Optional[float] = None
    early_return_data_points_22d: Optional[int] = None
    early_return_z_90d: Optional[float] = None
    early_return_avg_90d: Optional[float] = None
    early_return_std_90d: Optional[float] = None
    early_return_data_points_90d: Optional[int] = None
    early_return_z_180d: Optional[float] = None
    early_return_avg_180d: Optional[float] = None
    early_return_std_180d: Optional[float] = None
    early_return_data_points_180d: Optional[int] = None
    early_return_z_252d: Optional[float] = None
    early_return_avg_252d: Optional[float] = None
    early_return_std_252d: Optional[float] = None
    early_return_data_points_252d: Optional[int] = None


# ============================================================================
# Breakout — BREAKOUT_LOCKED_V1. Structural state only, no z-scoring.
# breakout_direction / fresh_breakout / accepted_breakout / failed_breakout
# are all signed ints: +1 up, -1 down, 0 none/inactive.
# breakout_direction is the day's FIRST breakout (never clears; the Breakout
# card's story). breakout_now is "outside the range right now, on two readings
# in a row" — what the glyph, row highlight, why-line and index counts use.
# ============================================================================

@dataclass
class BreakoutRow:
    asset_id: Optional[int] = None
    segment_code: Optional[str] = None
    symbol: Optional[str] = None
    current_price: Optional[float] = None
    timestamp: Optional[str] = None
    range_high: Optional[float] = None
    range_low: Optional[float] = None
    range_width: Optional[float] = None
    breakout_position: Optional[float] = None
    fresh_breakout: Optional[int] = 0
    breakout_direction: Optional[int] = 0
    breakout_now: Optional[int] = 0
    accepted_breakout: Optional[int] = 0
    failed_breakout: Optional[int] = 0
    distance_from_range: Optional[float] = None
    status: Optional[str] = None


# ============================================================================
# VWAP — VWAP_LOCKED_V1. No bands / band_breach (dropped from the locked
# design). vwap_z is computed at all four tiers.
# ============================================================================

@dataclass
class VwapRow:
    asset_id: Optional[int] = None
    segment_code: Optional[str] = None
    symbol: Optional[str] = None
    current_price: Optional[float] = None
    timestamp: Optional[str] = None
    vwap: Optional[float] = None
    displacement: Optional[float] = None
    side: Optional[int] = None
    acceptance: Optional[float] = None
    status: Optional[str] = None
    # vwap_z tier bundles (x4 tiers)
    vwap_z_22d: Optional[float] = None
    vwap_avg_22d: Optional[float] = None
    vwap_std_22d: Optional[float] = None
    vwap_data_points_22d: Optional[int] = None
    vwap_z_90d: Optional[float] = None
    vwap_avg_90d: Optional[float] = None
    vwap_std_90d: Optional[float] = None
    vwap_data_points_90d: Optional[int] = None
    vwap_z_180d: Optional[float] = None
    vwap_avg_180d: Optional[float] = None
    vwap_std_180d: Optional[float] = None
    vwap_data_points_180d: Optional[int] = None
    vwap_z_252d: Optional[float] = None
    vwap_avg_252d: Optional[float] = None
    vwap_std_252d: Optional[float] = None
    vwap_data_points_252d: Optional[int] = None


# ============================================================================
# Relative Strength — RELATIVE_STRENGTH_LOCKED_V1. One row per (stock,
# benchmark) pair per cycle, same as before. beta is a single value per tier
# (not itself z-scored); session_rs and intraday_rs are each z-scored at all
# four tiers.
# ============================================================================

@dataclass
class RelativeStrengthRow:
    # Identity
    asset_id: Optional[int] = None
    segment_code: Optional[str] = None
    symbol: Optional[str] = None
    benchmark_name: Optional[str] = None
    benchmark_id: Optional[int] = None
    # Raw prices
    current_price: Optional[float] = None
    open_price: Optional[float] = None
    # Timing
    timestamp: Optional[str] = None
    status: Optional[str] = None

    # SIGNALS ARE PER-TIER, because beta and the z-tier are ONE axis, not two.
    #
    # session_rs = ln(stock_open/prev) - beta * ln(bench_open/prev). beta is a
    # parameter of the MEASUREMENT; the tier is the lookback of the
    # DISTRIBUTION. Treating them independently would give 4 betas x 4 z-tiers
    # = 16 incoherent combinations, and a session_rs computed with beta_90 has
    # no business being z-scored against a 22d distribution built with beta_22.
    #
    # They were already coupled in the code before this: _reconstruct_history
    # takes beta as a fixed parameter and rebuilds the whole historical series
    # with it. Tier T now means: measured with beta_T, normalised against
    # exactly T samples that were themselves reconstructed with beta_T.
    session_rs_22d: Optional[float] = None
    session_rs_90d: Optional[float] = None
    session_rs_180d: Optional[float] = None
    session_rs_252d: Optional[float] = None

    intraday_rs_22d: Optional[float] = None
    intraday_rs_90d: Optional[float] = None
    intraday_rs_180d: Optional[float] = None
    intraday_rs_252d: Optional[float] = None

    # previous_intraday_rs * current_intraday_rs — a PRODUCT, so its SIGN is
    # the signal (negative = RS just flipped direction). Per-tier because the
    # previous value comes from that tier's own reconstructed history.
    #
    # Tiering this also removes a live collision: VwapRow also has a bare
    # `acceptance`, and because RS merges after VWAP in SIGNAL_REGISTRY order,
    # RS's value was overwriting VWAP's on the merged row — so the VWAP card's
    # "Widening / Reverting" reading was showing RS's number. The two mean
    # entirely different things (VWAP's is |current| - |previous|).
    acceptance_22d: Optional[float] = None
    acceptance_90d: Optional[float] = None
    acceptance_180d: Optional[float] = None
    acceptance_252d: Optional[float] = None

    # Beta per tier (regression coefficient — not z-scored itself)
    beta_22d: Optional[float] = None
    beta_90d: Optional[float] = None
    beta_180d: Optional[float] = None
    beta_252d: Optional[float] = None
    # Session RS z-tier bundles (x4 tiers)
    session_rs_z_22d: Optional[float] = None
    session_rs_avg_22d: Optional[float] = None
    session_rs_std_22d: Optional[float] = None
    session_rs_data_points_22d: Optional[int] = None
    session_rs_z_90d: Optional[float] = None
    session_rs_avg_90d: Optional[float] = None
    session_rs_std_90d: Optional[float] = None
    session_rs_data_points_90d: Optional[int] = None
    session_rs_z_180d: Optional[float] = None
    session_rs_avg_180d: Optional[float] = None
    session_rs_std_180d: Optional[float] = None
    session_rs_data_points_180d: Optional[int] = None
    session_rs_z_252d: Optional[float] = None
    session_rs_avg_252d: Optional[float] = None
    session_rs_std_252d: Optional[float] = None
    session_rs_data_points_252d: Optional[int] = None
    # Intraday RS z-tier bundles (x4 tiers)
    intraday_rs_z_22d: Optional[float] = None
    intraday_rs_avg_22d: Optional[float] = None
    intraday_rs_std_22d: Optional[float] = None
    intraday_rs_data_points_22d: Optional[int] = None
    intraday_rs_z_90d: Optional[float] = None
    intraday_rs_avg_90d: Optional[float] = None
    intraday_rs_std_90d: Optional[float] = None
    intraday_rs_data_points_90d: Optional[int] = None
    intraday_rs_z_180d: Optional[float] = None
    intraday_rs_avg_180d: Optional[float] = None
    intraday_rs_std_180d: Optional[float] = None
    intraday_rs_data_points_180d: Optional[int] = None
    intraday_rs_z_252d: Optional[float] = None
    intraday_rs_avg_252d: Optional[float] = None
    intraday_rs_std_252d: Optional[float] = None
    intraday_rs_data_points_252d: Optional[int] = None


# ============================================================================
# Volume Block — TRADE_VOLUME_BLOCK_LOCKED_V4. Replaces the old Time Block
# Volume calculator. Primary feature is block_volume_z (same-block history);
# context feature is session_cumulative_volume_z (reconstructed running
# total). Both are computed at all four tiers.
# ============================================================================

@dataclass
class VolumeBlockRow:
    asset_id: Optional[int] = None
    segment_code: Optional[str] = None
    symbol: Optional[str] = None
    timestamp: Optional[str] = None
    time_block: Optional[int] = None
    block_volume: Optional[float] = None
    session_cumulative_volume: Optional[float] = None
    # How many of today's blocks session_cumulative_volume sums: only the
    # complete ones, so it can be fewer than time_block.
    session_cumulative_blocks: Optional[int] = None
    status: Optional[str] = None
    # block_volume_z tier bundles (x4 tiers)
    block_volume_z_22d: Optional[float] = None
    block_volume_avg_22d: Optional[float] = None
    block_volume_std_22d: Optional[float] = None
    block_volume_data_points_22d: Optional[int] = None
    block_volume_z_90d: Optional[float] = None
    block_volume_avg_90d: Optional[float] = None
    block_volume_std_90d: Optional[float] = None
    block_volume_data_points_90d: Optional[int] = None
    block_volume_z_180d: Optional[float] = None
    block_volume_avg_180d: Optional[float] = None
    block_volume_std_180d: Optional[float] = None
    block_volume_data_points_180d: Optional[int] = None
    block_volume_z_252d: Optional[float] = None
    block_volume_avg_252d: Optional[float] = None
    block_volume_std_252d: Optional[float] = None
    block_volume_data_points_252d: Optional[int] = None
    # session_cumulative_volume_z tier bundles (x4 tiers)
    session_cumulative_volume_z_22d: Optional[float] = None
    session_cumulative_volume_avg_22d: Optional[float] = None
    session_cumulative_volume_std_22d: Optional[float] = None
    session_cumulative_volume_data_points_22d: Optional[int] = None
    session_cumulative_volume_z_90d: Optional[float] = None
    session_cumulative_volume_avg_90d: Optional[float] = None
    session_cumulative_volume_std_90d: Optional[float] = None
    session_cumulative_volume_data_points_90d: Optional[int] = None
    session_cumulative_volume_z_180d: Optional[float] = None
    session_cumulative_volume_avg_180d: Optional[float] = None
    session_cumulative_volume_std_180d: Optional[float] = None
    session_cumulative_volume_data_points_180d: Optional[int] = None
    session_cumulative_volume_z_252d: Optional[float] = None
    session_cumulative_volume_avg_252d: Optional[float] = None
    session_cumulative_volume_std_252d: Optional[float] = None
    session_cumulative_volume_data_points_252d: Optional[int] = None


# ============================================================================
# Volume Dynamics — VOLUME_DYNAMICS_LOCKED_V3. Derived from Volume Block's
# own z-score (volume_z is a pass-through, not independently z-scored). It
# tiers exactly as Volume Block does: one set of values per tier.
# ============================================================================

@dataclass
class VolumeDynamicsRow:
    """Volume Dynamics is a pure derivative of Volume Block's z-score, so it
    tiers exactly as Volume Block does.

    velocity = ZQ_t - ZQ_(t-1), where ZQ is Volume Block's abnormal-volume z.
    Once ZQ exists at four tiers there are four velocities, four accelerations
    and four expansion states — and a velocity built from the 90d ZQ would be a
    mismatched signal the moment the trader selects 22d.

    Cross-tier divergence is especially informative here: participation
    accelerating hard against the last 22 days while flat against 252 says
    something quite different from both tiers agreeing.

    NOTE these are NOT z-scores, so they do not carry the avg/std/data_points
    companions the z-scored signals do. `volume_z_*` is a pass-through of Volume
    Block's own z at that tier; its avg/std/n live on VolumeBlockRow.

    Indices emit no row at all — they are not tradeable, have no volume, and so
    have no Volume Block z to differentiate (see calculators/segments.py).
    """
    asset_id: Optional[int] = None
    segment_code: Optional[str] = None
    symbol: Optional[str] = None
    timestamp: Optional[str] = None
    time_block: Optional[int] = None
    status: Optional[str] = None

    # Pass-through of Volume Block's block_volume_z at each tier.
    volume_z_22d: Optional[float] = None
    volume_z_90d: Optional[float] = None
    volume_z_180d: Optional[float] = None
    volume_z_252d: Optional[float] = None

    # ZQ_t - ZQ_(t-1). Negative means abnormal participation is FADING — a
    # first-class signal, not an error.
    velocity_22d: Optional[float] = None
    velocity_90d: Optional[float] = None
    velocity_180d: Optional[float] = None
    velocity_252d: Optional[float] = None

    # velocity_t - velocity_(t-1).
    acceleration_22d: Optional[float] = None
    acceleration_90d: Optional[float] = None
    acceleration_180d: Optional[float] = None
    acceleration_252d: Optional[float] = None

    # Mean of the last 3 ZQ readings including the current one.
    persistence_22d: Optional[float] = None
    persistence_90d: Optional[float] = None
    persistence_180d: Optional[float] = None
    persistence_252d: Optional[float] = None

    # +2 expanding rapidly / +1 expanding / 0 neutral / -1 contracting /
    # -2 collapsing. Symmetric by design — contraction matters as much as
    # expansion.
    #
    # Defaults to None, NOT 0. 0 is a real state ("neutral"), so defaulting to
    # it would make a tier with too little history to produce a ZQ at all
    # indistinguishable from one that genuinely found volume steady — an
    # absence rendered as a substantive claim.
    expansion_state_22d: Optional[int] = None
    expansion_state_90d: Optional[int] = None
    expansion_state_180d: Optional[int] = None
    expansion_state_252d: Optional[int] = None


# ============================================================================
# Price-Volume Divergence — PVD_LOCKED_V3. Pearson correlation of price/volume
# deltas, Fisher-z transformed, with Spearman + Winsorized-Pearson diagnostic
# cross-checks. Requires true 5-second resolution, so its history is built live
# (or rebuilt by scripts/backfill.py from the days ohlc.db keeps); each of the
# four tiers fills at its own N.
# ============================================================================

@dataclass
class PriceVolumeDivergenceRow:
    asset_id: Optional[int] = None
    segment_code: Optional[str] = None
    symbol: Optional[str] = None
    timestamp: Optional[str] = None
    pearson_r: Optional[float] = None
    spearman_r: Optional[float] = None
    winsorized_r: Optional[float] = None
    fisher_value: Optional[float] = None
    sample_size: Optional[int] = None
    critical_r: Optional[float] = None
    reliable: Optional[int] = None
    # Mean of the SIGNS of pearson_r / spearman_r / winsorized_r. None when
    # fewer than 2 estimators survive — it previously reported a float
    # regardless, so a single surviving estimator yielded +/-1.0, reading as
    # "all three agree" when two of them did not exist.
    estimator_agreement: Optional[float] = None
    # How many of the three estimators produced a value, so the drawer can
    # render "1 of 3" instead of implying unanimity.
    estimators_available: Optional[int] = None
    price_volatility: Optional[float] = None
    status: Optional[str] = None
    # pvd_z tier bundles (x4 tiers)
    pvd_z_22d: Optional[float] = None
    pvd_avg_22d: Optional[float] = None
    pvd_std_22d: Optional[float] = None
    pvd_data_points_22d: Optional[int] = None
    pvd_z_90d: Optional[float] = None
    pvd_avg_90d: Optional[float] = None
    pvd_std_90d: Optional[float] = None
    pvd_data_points_90d: Optional[int] = None
    pvd_z_180d: Optional[float] = None
    pvd_avg_180d: Optional[float] = None
    pvd_std_180d: Optional[float] = None
    pvd_data_points_180d: Optional[int] = None
    pvd_z_252d: Optional[float] = None
    pvd_avg_252d: Optional[float] = None
    pvd_std_252d: Optional[float] = None
    pvd_data_points_252d: Optional[int] = None


# ============================================================================
# Price Levels — PRICE_LEVELS_LOCKED_V1. Replaces Price Alerts. Event/state
# engine, no z-scoring. One row per asset; `levels` holds one entry per
# tracked level name (local_high/local_low/global_high/global_low), each a
# dict shaped like LevelEventResult. `day_events` mirrors the old
# `day_breaches` persistence convention — level names crossed at any point
# today, never reset intraday.
# ============================================================================

@dataclass
class PriceLevelsRow:
    asset_id: Optional[int] = None
    segment_code: Optional[str] = None
    symbol: Optional[str] = None
    current_price: Optional[float] = None
    timestamp: Optional[str] = None
    levels: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    day_events: List[str] = field(default_factory=list)
    status: Optional[str] = None


# ============================================================================
# Rolling Momentum — ROLLING_MOMENTUM_LOCKED_V1. Continuously-updating
# counterpart to MomentumRow's fixed 09:15->09:30 window: re-evaluates at
# every completed 15-min block. session_return is z-scored at all four tiers.
# ============================================================================

@dataclass
class RollingMomentumRow:
    asset_id: Optional[int] = None
    segment_code: Optional[str] = None
    symbol: Optional[str] = None
    current_price: Optional[float] = None
    previous_close: Optional[float] = None
    timestamp: Optional[str] = None
    session_return: Optional[float] = None
    status: Optional[str] = None
    # session_return_z tier bundles (x4 tiers)
    session_return_z_22d: Optional[float] = None
    session_return_avg_22d: Optional[float] = None
    session_return_std_22d: Optional[float] = None
    session_return_data_points_22d: Optional[int] = None
    session_return_z_90d: Optional[float] = None
    session_return_avg_90d: Optional[float] = None
    session_return_std_90d: Optional[float] = None
    session_return_data_points_90d: Optional[int] = None
    session_return_z_180d: Optional[float] = None
    session_return_avg_180d: Optional[float] = None
    session_return_std_180d: Optional[float] = None
    session_return_data_points_180d: Optional[int] = None
    session_return_z_252d: Optional[float] = None
    session_return_avg_252d: Optional[float] = None
    session_return_std_252d: Optional[float] = None
    session_return_data_points_252d: Optional[int] = None


# ============================================================================
# Index Summary — the index-level derived summary, emitted by
# IndexAggregator.compute() in metrics.py. Since the previous schema generation
# it gained constituent_z (each constituent's z values, counted at display time
# against the z-slider), RS breadth counters per tier, and weighted volume z and
# velocity per tier.
# ============================================================================

@dataclass
class IndexSummary:
    # Identity + live quote
    index_name: Optional[str] = None
    index_price: Optional[float] = None
    intraday_pct: Optional[float] = None
    total_constituents: Optional[int] = 0
    # Breadth counters + denominators
    advancers: Optional[int] = 0
    decliners: Optional[int] = 0
    unchanged: Optional[int] = 0
    above_vwap: Optional[int] = 0
    below_vwap: Optional[int] = 0
    vwap_evaluated: Optional[int] = 0
    in_breakout_up: Optional[int] = 0
    in_breakout_down: Optional[int] = 0
    breakout_evaluated: Optional[int] = 0
    level_breached_today: Optional[int] = 0
    # ---- RS breadth, PER TIER ---------------------------------------------
    #
    # These count the SIGN of session_rs (> 0 vs < 0), and session_rs is
    # measured once per beta tier — so the count depends on which tier is being
    # shown. Pinned to 90d they answered a 90d question while every other row on
    # the same card followed the trader's selection. Silent: no error, just a
    # number quietly about something else.
    #
    # Stored per tier rather than derived at display time like the volume and
    # rolling-momentum spike counters, and the distinction is the principle
    # rather than an exception to it:
    #
    #   Counters that depend on a CONTINUOUS display setting (the z-threshold
    #   slider) must be derived at display time — you cannot precompute one per
    #   possible slider position, and a stored count cannot be re-thresholded
    #   for the historical breadth chart.
    #
    #   Counters that depend on a small FIXED ENUM may be stored, once per enum
    #   value. A sign count bakes no threshold; its only axis is the tier, and
    #   there are exactly four.
    #
    # Cost: ~36k integers/day, against ~54 MB over the 7-day retention had
    # session_rs been folded into constituent_z instead.
    positive_rs_22d: Optional[int] = 0
    positive_rs_90d: Optional[int] = 0
    positive_rs_180d: Optional[int] = 0
    positive_rs_252d: Optional[int] = 0
    negative_rs_22d: Optional[int] = 0
    negative_rs_90d: Optional[int] = 0
    negative_rs_180d: Optional[int] = 0
    negative_rs_252d: Optional[int] = 0
    rs_evaluated_22d: Optional[int] = 0
    rs_evaluated_90d: Optional[int] = 0
    rs_evaluated_180d: Optional[int] = 0
    rs_evaluated_252d: Optional[int] = 0
    # Weighted aggregates. weighted_volume_zscore is a weighted AVERAGE, not a
    # threshold count, so it stays server-side and simply gains its other three
    # tiers — unlike the spike counters below, nothing about it depends on a
    # display setting.
    #
    # weighted_volume_velocity is per tier for the same reason: it averages
    # VolumeDynamicsRow.velocity_<tier>, a change in a z-score, and each tier
    # has its own. As a single field it read `velocity`, which that row never
    # had, and was NULL on every row ever stored.
    weighted_intraday_return: Optional[float] = None
    weighted_volume_velocity_22d: Optional[float] = None
    weighted_volume_velocity_90d: Optional[float] = None
    weighted_volume_velocity_180d: Optional[float] = None
    weighted_volume_velocity_252d: Optional[float] = None
    weighted_volume_zscore_22d: Optional[float] = None
    weighted_volume_zscore_90d: Optional[float] = None
    weighted_volume_zscore_180d: Optional[float] = None
    weighted_volume_zscore_252d: Optional[float] = None
    weighted_vwap_deviation: Optional[float] = None
    # ---- Per-constituent z-values: counting moved to DISPLAY TIME -----------
    #
    # This replaces six stored counters — vol_spike_high/low +
    # zscore_90d_evaluated, and session_return_z_spike_high/low +
    # session_return_z_evaluated.
    #
    # Those counters baked `> 2.0` / `< -2.0` into the metrics thread, while the
    # scores table, row highlights and Signals glyphs all follow the
    # settings-drawer z-slider. Set the slider to 2.5 and the index card and the
    # scores table disagreed about what "unusual" means, on the same screen.
    #
    # IndexAggregator runs in the metrics thread and persists to metrics.db; it
    # cannot read a per-browser display setting. So it stores the OBSERVATIONS
    # and the dashboard does the counting.
    #
    # Storing the z-values rather than pre-counting is the ONLY route, not just
    # the tidier one. The counters feed two surfaces: the breadth grid (live
    # cache, which could have been recounted from LATEST_METRICS) and the
    # intraday breadth CHART, which plots breadth over time from index_metrics
    # HISTORY. A historical row can only be recounted at a new threshold if the
    # per-constituent values were stored on it.
    #
    # Shape — JSON, COLUMN-ORIENTED and self-describing. Built by
    # metrics._build_constituent_z, decoded by dashboard._count_z_spikes:
    #
    #     {"k":     ["<asset_id>:<segment_code>", ...],   # constituent order
    #      "sig":   ["block_volume_z", "session_return_z"],
    #      "tiers": ["22d", "90d", "180d", "252d"],
    #      "v":     [[bv22, bv90, bv180, bv252, sr22, sr90, sr180, sr252], ...]}
    #
    # Value index = signal_index * len(tiers) + tier_index. `null` at a position
    # means that tier had no value — "not evaluated", which is what the removed
    # *_evaluated denominators carried. A constituent with no value anywhere is
    # absent from `k`/`v` entirely. Empty overall serialises as {}.
    #
    # Keyed by the composite (asset_id, segment_code) — asset_id 25 is BankNifty
    # in IDX_I and ADANIENT in NSE_EQ, so asset_id alone would blend them.
    #
    # `sig`/`tiers` ride on EVERY row, not in a shared mapping. ~90 bytes each,
    # and it buys the property a shared mapping cannot: a row decodes from
    # itself, so changing TIERS or _CZ_SIGNALS later cannot silently misread
    # rows written under the old layout.
    #
    # Sizing — computed from the encoding, not measured: every stored value is
    # {} until a tier fills. Constituents per the CSV: NIFTY 50, SENSEX 30,
    # MIDCAP 25, BANKNIFTY 12 — 117. The previous nested-dict form
    # ({"1:NSE_EQ": {"block_volume_z": {"22d": -0.12345678901234, ...}, ...}})
    # cost ~291 bytes per constituent — ~55% float reprs, ~30% the signal and
    # tier names repeated once per constituent — so ~14.5 KB per NIFTY row and
    # ~34 KB per cycle across all four indices. Positional values plus 3-decimal
    # rounding (z is displayed to 2dp and thresholded on a 0.1-step slider, so
    # the further digits are pure payload) cut it to ~73 bytes per constituent
    # plus ~90 bytes of sig/tiers per row: a NIFTY row lands near 3.7 KB, UNDER
    # the 4096-byte page size, so these rows no longer spill into SQLite
    # overflow pages at all; ~8.9 KB per cycle, ~30 MB over the 7-day retention.
    constituent_z: Dict[str, Any] = field(default_factory=dict)
    # Cross-sectional signals
    cross_sectional_dispersion: Optional[float] = None
    ex_top5_intraday_return: Optional[float] = None
    # Leadership (each is a list of 5 dicts: symbol, weight_pct, intraday_pct, contribution_bps)
    top_contributors: List[Dict[str, Any]] = field(default_factory=list)
    bottom_detractors: List[Dict[str, Any]] = field(default_factory=list)
    # Timing
    timestamp: Optional[str] = None


# ============================================================================
# Reflection helper — used by dashboard's startup contract check to build the
# union of all emitted field names across every Row class.
# ============================================================================

ALL_ROW_CLASSES = (
    MomentumRow,
    BreakoutRow,
    VwapRow,
    RelativeStrengthRow,
    VolumeBlockRow,
    VolumeDynamicsRow,
    PriceVolumeDivergenceRow,
    PriceLevelsRow,
    RollingMomentumRow,
    IndexSummary,
)


def all_emitted_fields() -> set:
    """Union of every field name across every Row class. Used by dashboard's
    boot-time contract check to validate CONSUMED_FIELDS ⊆ this set."""
    import dataclasses
    result: set = set()
    for cls in ALL_ROW_CLASSES:
        result.update(f.name for f in dataclasses.fields(cls))
    return result
