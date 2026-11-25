"""
ROLLING_MOMENTUM_LOCKED_V1

Purpose:
    Measure whether the stock's session return, at whatever time of day it is
    right now, is unusual compared to what that same time-of-day slot has
    looked like historically.

Role:
    Continuously-updating counterpart to MOMENTUM_LOCKED_V2's fixed
    09:15->09:30 window. Momentum answers "was the open unusual" and then
    freezes for the rest of the session. This answers "is the move unusual
    right now" and keeps re-evaluating at every completed 15-min block.

NOT:
    A replacement for Momentum. A different question, same locked-calculator
    philosophy: no standalone alpha claim, point-in-time state only.

Formula:

    session_return =
        (current_price / previous_close - 1) * 100

    Same percent-return convention as the existing `session_price_move`
    column it is z-scored against (not a log return).

    z = (session_return - mean(history)) / std(history)

    history = session_return values recorded at this exact 15-min block on
    prior trading days.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from calculators.statistics import absent_tiers, zscore_tiers


# {tier: (z, mu, sigma, available_n)} — see calculators/statistics.py.
TierBundle = Dict[int, Tuple[Optional[float], Optional[float],
                             Optional[float], int]]


@dataclass
class RollingMomentumResult:
    # None on invalid input (no usable price or previous close) — unknown, not 0.
    session_return: Optional[float]

    # session_return z-scored against same-block history, all four tiers.
    # One bundle replaces the previous z / avg / std triple, which was computed
    # TWICE — once inside a local calculate_zscore for the z, then again inline
    # for the displayed mu/sigma. That is the same split that made Volume
    # Block's drawer irreconcilable, just co-located rather than across files.
    session_return_tiers: TierBundle

    status: str


def calculate_session_return(
    current_price: float,
    previous_close: float,
) -> Optional[float]:
    """
    Session return as a percent, matching the existing `session_price_move`
    shared-store column's own convention exactly (so z-scoring compares like
    with like).
    """
    if current_price is None or previous_close is None or previous_close <= 0:
        return None

    return (current_price / previous_close - 1) * 100


# The local calculate_zscore is gone. Its docstring said it was "duplicated
# from momentum_calculator.calculate_zscore rather than imported — matches the
# existing convention where every locked calculator owns its own copy of shared
# primitives". That convention is abandoned: six byte-identical copies meant the
# 20-sample floor and the zero-variance behaviour had to be changed correctly in
# six places at once. It now lives in calculators/statistics.py.


def calculate_rolling_momentum_state(
    current_price: float,
    previous_close: float,
    historical_session_returns: List[float],
) -> RollingMomentumResult:
    """
    Main Rolling Momentum V1 calculator.

    Inputs:

        current_price
            Live current price.

        previous_close
            Previous session close.

        historical_session_returns
            Session returns recorded at this exact 15-min block on prior
            trading days. MOST-RECENT-FIRST, and passed WHOLE — the caller must
            not truncate it. zscore_tiers owns the per-tier slicing because it
            is the only thing that knows each tier needs exactly its own N; a
            [:90] here would leave 180d and 252d permanently absent no matter
            how deep the adapter fetched.

    Returns:

        RollingMomentumResult
    """

    session_return = calculate_session_return(
        current_price,
        previous_close,
    )

    if session_return is None:
        return RollingMomentumResult(
            session_return=None,
            # An all-absent bundle, not None — consumers iterate this mapping,
            # so its shape must hold on the invalid-input path too.
            session_return_tiers=absent_tiers(len(historical_session_returns)),
            status="invalid_input",
        )

    return RollingMomentumResult(
        session_return=session_return,
        session_return_tiers=zscore_tiers(
            session_return,
            historical_session_returns,
        ),
        status="valid",
    )
