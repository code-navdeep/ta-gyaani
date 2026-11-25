"""
MOMENTUM_LOCKED_V2

Purpose:
    Measure opening momentum state.

Research conclusion:
    Momentum is a directional/state calculator.
    It is NOT assumed to be universal standalone alpha.

Features:

    gap_return
    gap_direction
    early_return
    continuation
    acceleration
    gap_z            at 22 / 90 / 180 / 252 days
    early_return_z   at 22 / 90 / 180 / 252 days

All calculations are point-in-time.

Time assumptions:
    Market open: 09:15 IST
    First completed momentum window: 09:30 IST

No future candles are used.
"""

from dataclasses import dataclass
from typing import Dict, Optional, List, Tuple

from calculators.statistics import (
    absent_tiers, first_difference, log_return, sign, zscore_tiers,
)


# {tier: (z, mu, sigma, available_n)} — see calculators/statistics.py.
TierBundle = Dict[int, Tuple[Optional[float], Optional[float],
                             Optional[float], int]]


@dataclass
class MomentumResult:

    # Opening gap — None on invalid input (no usable open or previous close)
    gap_return: Optional[float]
    gap_direction: Optional[int]

    # Early session movement — None until the 09:15->09:30 window has closed
    early_return: Optional[float]

    # Whether early move agrees with gap — None with early_return
    continuation: Optional[float]

    # Speed change
    acceleration: Optional[float]

    # Normalized states — all four tiers, each with its own mu/sigma from the
    # same zscore_tiers call, so the drawer's numbers always reconcile.
    gap_tiers: TierBundle
    early_return_tiers: TierBundle

    status: str



def safe_log_return(
    current_price: float,
    previous_price: float
):

    """
    Log return.

    r = ln(Pt / Pt-1)

    Delegates to `statistics.log_return`, which is the same expression with the
    same non-positive guard. Kept under this name because call sites read it as
    "safe log return"; only the arithmetic is shared.
    """

    return log_return(current_price, previous_price)



def calculate_gap(
    open_price: float,
    previous_close: float
):

    """
    Overnight gap.

    G =
        ln(Open / Previous Close)
    """

    return safe_log_return(
        open_price,
        previous_close
    )



def calculate_direction(
    gap_return: float
):

    """
    Gap sign.

    +1 bullish gap
    -1 bearish gap
     0 flat

    This IS `sign(gap_return)`; the three-way comparison is shared with VWAP's
    side-of-VWAP and Price Levels' above/below-level.
    """

    return sign(gap_return)



def calculate_early_return(
    open_price: float,
    price_after_window: float
):

    """
    First momentum window return.

    Default:
        09:15 -> 09:30
    """

    return safe_log_return(
        price_after_window,
        open_price
    )



def calculate_continuation(
    gap_direction: int,
    early_return: Optional[float]
):

    """
    Measures whether price follows
    the opening gap direction.

    Positive:
        continuation

    Negative:
        reversal

    None when early_return is None (the window has not closed).
    """

    if early_return is None:
        return None

    return gap_direction * early_return



def calculate_acceleration(
    current_early_return: float,
    previous_early_return: Optional[float]
):

    """
    Change in early momentum.

    A_t =
        Momentum_t - Momentum_previous

    This IS `first_difference`, shared with Volume Dynamics' velocity (which
    differences ZQ) and its acceleration (which differences velocity). Kept
    under this name because "acceleration" is what it means here.
    """

    return first_difference(current_early_return, previous_early_return)



# The local calculate_zscore is gone — it was one of six byte-identical copies,
# and now lives in calculators/statistics.py as part of zscore_tiers.



def calculate_momentum_state(
    open_price: float,
    previous_close: float,

    price_after_window: Optional[float],

    previous_early_return: Optional[float],

    previous_gap_history: List[float],

    previous_early_history: List[float]

):

    """
    Main Momentum V2 calculator.

    Inputs:

        open_price
            Today's opening price

        previous_close
            Previous session close

        price_after_window
            Completed first momentum window price, or None when the window
            has not closed. Never a stand-in: the adapter used to pass the
            open, which made early_return ln(open/open) = 0 and z-scored that
            fabricated zero against real history. With None, early_return,
            continuation, acceleration and the early-return tiers are all
            None, and status says why.

        previous_early_return
            Previous comparable momentum value

        previous_gap_history
            Prior sessions' gaps, most recent first, passed whole — each
            tier takes exactly its own N (22 / 90 / 180 / 252)

        previous_early_history
            Prior sessions' early returns, same order and use


    Returns:

        MomentumResult
    """



    gap_return = calculate_gap(
        open_price,
        previous_close
    )


    if gap_return is None:

        # Keyword arguments, not positional. The Result gained a field here
        # (one z scalar became two tier bundles), so a positional call would
        # silently shift every value one slot along.
        return MomentumResult(
            gap_return=None,
            gap_direction=None,
            early_return=None,
            continuation=None,
            acceleration=None,
            gap_tiers=absent_tiers(len(previous_gap_history)),
            early_return_tiers=absent_tiers(len(previous_early_history)),
            status="invalid_input",
        )


    gap_direction = calculate_direction(
        gap_return
    )


    early_return = calculate_early_return(
        open_price,
        price_after_window
    )


    continuation = calculate_continuation(
        gap_direction,
        early_return
    )


    acceleration = calculate_acceleration(
        early_return,
        previous_early_return
    )


    # Histories are MOST-RECENT-FIRST and passed WHOLE — zscore_tiers owns the
    # per-tier slicing. A [:90] here would cap every tier at 90 samples and
    # leave 180d/252d permanently absent regardless of fetch depth.
    gap_tiers = zscore_tiers(
        gap_return,
        previous_gap_history
    )


    early_return_tiers = zscore_tiers(
        early_return,
        previous_early_history
    )


    return MomentumResult(

        gap_return=gap_return,

        gap_direction=gap_direction,

        early_return=early_return,

        continuation=continuation,

        acceleration=acceleration,

        gap_tiers=gap_tiers,

        early_return_tiers=early_return_tiers,

        status="valid" if price_after_window is not None else "window_not_closed"
    )
