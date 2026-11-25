"""
Shared numeric primitives for the locked calculators.

============================================================================
THE ORDERING CONTRACT — read this before touching any history slice
============================================================================

**Every history list in this system is MOST-RECENT-FIRST.**

    history[0]  = the newest observation
    history[-1] = the oldest observation

This matches what the `ORDER BY date DESC` in every
`historical_database_manager` query produces, and what every adapter already
assumes when it slices `history[:n]`.

Therefore: **take the first N with `[:n]`, never the last N with `[-n:]`.**

This is not pedantry. Before this module existed, seven pure calculators
sliced `history[-90:]` — the *oldest* 90 — while their adapters supplied
most-recent-first lists. It was harmless only by accident: the adapters
pre-sliced to exactly 90 first, so `[-90:]` was a no-op on a 90-element list.
The moment a longer list is passed (which multi-tier z-scoring requires), that
slice silently selects nine-month-old samples while the label still reads
"90d". No exception, no warning, no visible symptom on the dashboard.

The only legitimate `[-n:]` in this codebase is where a list really is
oldest-first and the code genuinely means "the last N in time order" —
Breakout's acceptance check (`breakout_calculator`, the last two readings
after the breakout) and Volume Dynamics' within-session block sequence
(`volume_dynamics`, `volume_dynamics_adapter`, the last three blocks).

============================================================================
THE TIER CONTRACT
============================================================================

A tier labelled `N` is computed from **exactly N samples, or not at all.**

22 populates at 22. 90 at 90. 180 at 180. 252 at 252. There is no substitute
floor, no partial fill, and no "close enough". A tier with 47 samples
available is *absent*, and the dashboard renders a hyphen — because that is
the truth, and showing a z labelled "252d" that was built from 47 samples
misrepresents the analysis to the person trading on it.

`scripts/precompute_beta.py` has always honoured this for β; every z-scored
signal now does too.
"""
import math
from math import atanh, log, sqrt, tanh
from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Tier configuration — the single source of truth for lookback windows.
# ---------------------------------------------------------------------------

TIERS: Tuple[int, ...] = (22, 90, 180, 252)
MAX_TIER: int = max(TIERS)

# Historical fetches pull MAX_TIER * FETCH_MULTIPLIER rows per partition.
# The slack absorbs NULLs in the metric column: rows are fetched, dropna'd,
# then sliced to the tier size. Fetching exactly MAX_TIER would under-fill the
# top tier whenever any fetched row carries a NULL for the metric in question.
#
# The slack is only real where the rows exist to fetch. daily_ohlc and
# time_block_volumes are never pruned, so for them it is. The shared store
# (intraday_time_block_metrics) is pruned to 420 calendar days, ~284 trading
# days, so its 252d tier has ~32 rows of headroom, not 252 — the prune is the
# ceiling there, not this multiplier. See cleanup_old_intraday_metrics.
FETCH_MULTIPLIER: int = 2
FETCH_DAYS: int = MAX_TIER * FETCH_MULTIPLIER      # 504

# Absent-tier sentinel: (z, mu, sigma, available_n) with the count filled in
# by the caller. Declared here so no consumer invents its own shape.
_ABSENT = (None, None, None)


# ---------------------------------------------------------------------------
# Elementary statistics
#
# `mean` and `population_std` carry NO sample-count floor. Floors are a policy
# decision belonging to the caller (see `zscore_tiers`), not to the arithmetic.
# ---------------------------------------------------------------------------

def mean(values: Sequence[float]) -> float:
    """Arithmetic mean. Empty input returns 0.0."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def population_std(values: Sequence[float]) -> float:
    """Population standard deviation (divide by n, NOT n-1).

    Population rather than sample variance is used consistently across every
    z-score in this system. Empty input returns 0.0; a single value returns
    0.0 naturally (zero variance).
    """
    if not values:
        return 0.0
    m = mean(values)
    return sqrt(sum((x - m) ** 2 for x in values) / len(values))


def covariance(x: Sequence[float], y: Sequence[float]) -> float:
    """Population covariance. Returns 0.0 on length mismatch."""
    if len(x) != len(y) or not x:
        return 0.0
    mx, my = mean(x), mean(y)
    return sum((x[i] - mx) * (y[i] - my) for i in range(len(x))) / len(x)


def pearson_corr(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    """Pearson product-moment correlation. None if either series is constant."""
    sx, sy = population_std(x), population_std(y)
    if sx == 0 or sy == 0:
        return None
    return covariance(x, y) / (sx * sy)


def rank_values(values: Sequence[float]) -> List[int]:
    """Ordinal ranks, 1-based, ties broken by original position.

    NOTE: this is an *ordinal* rank, not a tie-averaged (fractional) rank.
    With genuine ties — common in volume deltas, where runs of zero appear —
    tied observations receive distinct consecutive ranks in index order rather
    than sharing an averaged rank. Spearman is used here only as a robustness
    cross-check against `pearson_corr`, so the small bias this introduces is
    tolerable; it is documented rather than silently assumed.
    """
    ordered = sorted(enumerate(values), key=lambda pair: pair[1])
    ranks = [0] * len(values)
    for rank, (idx, _value) in enumerate(ordered):
        ranks[idx] = rank + 1
    return ranks


def spearman_corr(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    """Rank correlation — outlier-robust cross-check on `pearson_corr`."""
    return pearson_corr(rank_values(x), rank_values(y))


def winsorize(values: Sequence[float],
              lower: float = 0.025,
              upper: float = 0.975) -> List[float]:
    """Clip values to the [lower, upper] quantile band.

    Series shorter than 5 are returned unchanged — clipping a handful of
    points would distort more than the outliers it removes.
    """
    if len(values) < 5:
        return list(values)
    ordered = sorted(values)
    low = ordered[int(len(values) * lower)]
    high = ordered[min(int(len(values) * upper), len(values) - 1)]
    return [min(max(v, low), high) for v in values]


def fisher_transform(r: Optional[float]) -> Optional[float]:
    """Fisher z-transform `atanh(r)` — variance-stabilises a correlation.

    `r` is clamped just inside ±1 because `atanh(±1)` is infinite, and a
    perfect correlation over a short tick window is a real occurrence.
    """
    if r is None:
        return None
    return atanh(max(min(r, 0.999999), -0.999999))


def critical_r(n: int) -> Optional[float]:
    """95%-confidence correlation floor for sample size `n`.

    `|r|` below this is indistinguishable from zero at 95% confidence.
    None for n <= 3, where the statistic is undefined.
    """
    if n <= 3:
        return None
    return tanh(1.96 / sqrt(n - 3))


# ---------------------------------------------------------------------------
# Elementary transforms
# ---------------------------------------------------------------------------

def sign(value: Optional[float]) -> Optional[int]:
    """+1 / -1 / 0. None propagates.

    Backs three differently-named callers that all mean the same thing:
    Momentum's gap direction, VWAP's side-of-VWAP, and Price Levels'
    above/below-level (which passes `price - level`).
    """
    if value is None:
        return None
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def log_return(current_price: Optional[float],
               previous_price: Optional[float]) -> Optional[float]:
    """`ln(current / previous)`. None if either price is missing or <= 0."""
    if current_price is None or previous_price is None:
        return None
    if current_price <= 0 or previous_price <= 0:
        return None
    return log(current_price / previous_price)


def first_difference(current: Optional[float],
                     previous: Optional[float]) -> Optional[float]:
    """`current - previous`, None-safe.

    The shared primitive behind Momentum's day-over-day acceleration, Volume
    Dynamics' velocity (ΔZQ) and its acceleration (Δvelocity). Each keeps its
    own semantic name at its call site; only the arithmetic is shared.
    """
    if current is None or previous is None:
        return None
    return current - previous


def finite_positive(x) -> bool:
    """True if `x` is a real, finite, strictly-positive number.

    The validity gate for prices and volumes before they enter a calculation:
    None, NaN, inf and non-numeric all fail. A price of 0 or below is not a
    price, and NaN silently poisons every downstream mean and sigma it touches.

    Byte-identical copies of this lived in momentum_adapter,
    relative_strength_adapter and rolling_momentum_adapter. It was absent from
    the original extraction audit entirely — that sweep matched function names,
    and nothing groups a lone helper by name. The AST body sweep found it.

    Text is refused even when it spells a number: float("5") succeeds, but a
    price arriving as a string means something upstream is broken.
    """
    if isinstance(x, (str, bytes)):
        return False
    try:
        v = float(x)
    except (TypeError, ValueError):
        return False
    return x is not None and math.isfinite(v) and v > 0.0


def safe_log_volume(volume: Optional[float]) -> Optional[float]:
    """`ln(1 + volume)`.

    Volume distributions are heavily right-skewed; without the log transform a
    single block trade dominates the normalisation. The `1 +` keeps a zero-volume
    block finite (`ln(1) = 0`), which is a real observation and must be preserved.

    NO NEGATIVE CLAMP — deliberately. This used to read `max(float(volume), 0.0)`.
    Volume cannot be negative, so a negative arriving here means something upstream
    is broken, and the clamp turned that broken reading into a plausible `0.0` that
    then entered the distribution as if it were a real observation. Measured cost of
    that: ten fabricated zeros in a 180-sample window inflated sigma 9.9x, so a
    genuine 6x-median volume block read z=+0.73 (unremarkable, no highlight) instead
    of z=+4.83 (extreme, row lights up). The clamp did not merely hide a bug — it
    SUPPRESSED the extreme readings this system exists to surface.

    The upstream defect is fixed at source (engine.py's same-day filter in
    `_calculate_interval_volume`), so a negative is now unreachable. No guard is
    added for it either: `log()` already raises ValueError on a negative argument,
    so an impossible value surfaces loudly on its own without a line of code
    written for a case that cannot occur.
    """
    if volume is None:
        return None
    return log(1 + float(volume))


# ---------------------------------------------------------------------------
# Beta
# ---------------------------------------------------------------------------

def calculate_beta(stock_returns: Sequence[float],
                   benchmark_returns: Sequence[float]) -> Optional[float]:
    """`cov(stock, benchmark) / var(benchmark)` — the regression slope.

    NO minimum-sample floor: the caller decides how many paired returns
    constitute a valid tier. `scripts/precompute_beta.py` gates on
    `len(returns) < n` per tier, which is the correct and only gate.

    Returns None if the benchmark series has zero variance (undefined slope).
    Raises ValueError on length mismatch — a paired-series bug, not a data gap.
    """
    if len(stock_returns) != len(benchmark_returns):
        raise ValueError(
            f"Return series length mismatch: "
            f"{len(stock_returns)} vs {len(benchmark_returns)}"
        )
    if not stock_returns:
        return None

    mean_bench = mean(benchmark_returns)
    var_bench = sum((x - mean_bench) ** 2 for x in benchmark_returns)
    if var_bench == 0:
        return None

    mean_stock = mean(stock_returns)
    cov = sum(
        (stock_returns[i] - mean_stock) * (benchmark_returns[i] - mean_bench)
        for i in range(len(stock_returns))
    )
    # The 1/n normalisation cancels between covariance and variance, so the
    # raw sums give the same slope as the normalised forms.
    return cov / var_bench


# ---------------------------------------------------------------------------
# The tier engine
# ---------------------------------------------------------------------------

def zscore(value: Optional[float], history: Sequence[float]) -> Optional[float]:
    """Population z-score of `value` against `history`. No sample floor.

    Returns None when `value` is None, `history` is empty, or the history has
    zero variance. **Zero variance yields None, not 0.0** — z is genuinely
    undefined there, and returning 0.0 would assert "perfectly normal", a claim
    the data does not support.

    Callers normally want `zscore_tiers`; this exists as its building block and
    for the rare single-window case.
    """
    if value is None or not history:
        return None
    sigma = population_std(history)
    if sigma == 0:
        return None
    return (value - mean(history)) / sigma


def zscore_tiers(value: Optional[float],
                 history: Sequence[float],
                 tiers: Tuple[int, ...] = TIERS,
                 ) -> Dict[int, Tuple[Optional[float], Optional[float],
                                      Optional[float], int]]:
    """Population z-score of `value` at every tier.

    Args:
        value:   the current observation being normalised.
        history: prior observations, **MOST-RECENT-FIRST** (see the ordering
                 contract at the top of this module). Must already be cleaned
                 of NaN/None by the caller.
        tiers:   sample counts to evaluate. Defaults to (22, 90, 180, 252).

    Returns:
        `{N: (z, mu, sigma, available_n)}` for every N in `tiers`.

        Tier N uses **exactly** `history[:N]`. If fewer than N samples are on
        hand, that tier is `(None, None, None, available_n)` — absent, not
        approximated.

        `available_n` is `len(history)` — the samples ON HAND, identical across
        every tier. It is deliberately *not* the sample count used, which under
        the exact-fill rule is always either N or nothing and therefore carries
        no information. Reporting what is available lets the dashboard render
        `252d  —  (n 47/252)`, so a trader can see how far off a tier still is
        rather than only that it is missing.

        `mu` and `sigma` are returned from the SAME computation that produced
        `z`, and are therefore always in the same units as `z`. This is not a
        convenience: Volume Block previously computed z on log-volumes in the
        calculator while its adapter computed mu/sigma on raw share counts, so
        the detail drawer showed a z of +0.2033 beside a mu/sigma implying
        -0.1089. Splitting those computations is what made that possible.
    """
    available = len(history)
    out: Dict[int, Tuple[Optional[float], Optional[float],
                         Optional[float], int]] = {}

    for n in tiers:
        if value is None or available < n:
            out[n] = (*_ABSENT, available)
            continue

        window = history[:n]                    # newest n — see ordering contract
        mu = sum(window) / n
        sigma = sqrt(sum((x - mu) ** 2 for x in window) / n)

        if sigma == 0:
            # Zero variance: mu/sigma are real and worth showing, z is not.
            out[n] = (None, mu, 0.0, available)
        else:
            out[n] = ((value - mu) / sigma, mu, sigma, available)

    return out


def absent_tiers(available_n: int = 0,
                 tiers: Tuple[int, ...] = TIERS,
                 ) -> Dict[int, Tuple[Optional[float], Optional[float],
                                      Optional[float], int]]:
    """An all-absent tier mapping, for calculators short-circuiting on bad input.

    Keeps every emission site returning the same shape rather than each one
    hand-rolling a dict of Nones.
    """
    return {n: (*_ABSENT, available_n) for n in tiers}


def tier_fields(prefix: str,
                bundle: Optional[Dict[int, Tuple[Optional[float], Optional[float],
                                                 Optional[float], int]]],
                tiers: Tuple[int, ...] = TIERS,
                ) -> Dict[str, object]:
    """Flatten a tier bundle into the Row's field names.

        tier_fields('block_volume', bundle) ->
            {'block_volume_z_22d': ..., 'block_volume_avg_22d': ...,
             'block_volume_std_22d': ..., 'block_volume_data_points_22d': ...,
             ... same for 90d / 180d / 252d}

    Used as `Row(**identity, **tier_fields('gap', gap_bundle), ...)`.

    Every tiering adapter has to expand each bundle into 16 flat fields. Written
    out by hand that is ~16 lines per bundle across eight bundles, all with
    near-identical names — precisely where a transposition hides, since
    `block_volume_std_180d=<the 252d value>` reads perfectly well. Generating
    the names from one convention removes the opportunity.

    A `None` bundle yields all-absent fields, so a calculator that bailed early
    still produces the complete field set.
    """
    if bundle is None:
        bundle = absent_tiers(0, tiers)

    out: Dict[str, object] = {}
    for n in tiers:
        z, mu, sigma, available = bundle.get(n, (*_ABSENT, 0))
        out[f'{prefix}_z_{n}d'] = z
        out[f'{prefix}_avg_{n}d'] = mu
        out[f'{prefix}_std_{n}d'] = sigma
        out[f'{prefix}_data_points_{n}d'] = available
    return out
