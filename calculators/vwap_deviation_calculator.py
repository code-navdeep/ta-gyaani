
"""
VWAP_LOCKED_V1

Purpose:
    Measure price location relative to VWAP.

Research conclusion:

    Signed VWAP deviation:
        weak directional information

    Absolute VWAP abnormality:
        useful movement magnitude state


Features:

    vwap_displacement

        ln(price / vwap)


    vwap_z at 22 / 90 / 180 / 252 days

        displacement normalised against the same block on prior sessions


    acceptance

        whether price remains away from VWAP


Important:

    Uses the live quote price and the exchange's own VWAP (the quote's
    `average_price`).

    VWAP source must match production feed.

"""



from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple

from calculators.statistics import absent_tiers, log_return, sign, zscore_tiers


# {tier: (z, mu, sigma, available_n)} — see calculators/statistics.py.
TierBundle = Dict[int, Tuple[Optional[float], Optional[float],
                             Optional[float], int]]



@dataclass
class VWAPResult:

    price: float

    vwap: float

    # None on invalid input (a non-positive price or VWAP) — unknown, not 0.
    displacement: Optional[float]

    # Displacement z-scored against same-block history, all four tiers.
    # mu and sigma arrive from the same zscore_tiers call as z, so they are
    # always in the same units as the z they sit beside.
    displacement_tiers: TierBundle

    side: Optional[int]

    acceptance: Optional[float]

    status: str



def calculate_displacement(
    price: float,

    vwap: float

):

    """
    Log distance from VWAP.

    Formula:

        ln(price / VWAP)

    This IS `log_return(price, vwap)` — same expression, same non-positive
    guard. It keeps its own name because "displacement from VWAP" is what it
    means here. The name is why the first extraction audit missed it: that sweep
    matched function names, and this one is named for its domain meaning rather
    than its computation.
    """

    return log_return(price, vwap)



def calculate_side(
    displacement: float
):

    """
    Price location.

    +1:
        above VWAP

    -1:
        below VWAP

    0:
        equal

    This IS `sign(displacement)`; the three-way comparison is shared with
    Momentum's gap direction and Price Levels' above/below-level.
    """

    return sign(displacement)



def calculate_acceptance(
    current_displacement: float,

    previous_displacement: Optional[float]

):

    """
    Measures whether VWAP separation persists.

    Positive:
        distance increasing

    Negative:
        reverting toward VWAP
    """

    if previous_displacement is None:
        return None



    return (
        abs(current_displacement)
        -
        abs(previous_displacement)
    )



def calculate_vwap_state(

    price: float,

    vwap: float,

    previous_vwap_displacement: Optional[float],

    previous_same_time_vwap_history: List[float]

):

    """
    Main VWAP calculator.


    Inputs:

        price:
            the live quote price


        vwap:
            production VWAP / average price


        previous_vwap_displacement:
            this asset's displacement on the previous metrics cycle today
            (~34 s earlier), or None at the session's first cycle


        previous_same_time_vwap_history:
            the same block on prior sessions, most recent first, passed whole


    """



    displacement = calculate_displacement(

        price,

        vwap

    )



    if displacement is None:

        return VWAPResult(

            price=price,

            vwap=vwap,

            displacement=None,

            # An all-absent bundle, not None — every consumer iterates this
            # mapping, so the shape must hold even on the invalid-input path.
            displacement_tiers=absent_tiers(len(previous_same_time_vwap_history)),

            side=None,

            acceptance=None,

            status="invalid_input"

        )



    # History is MOST-RECENT-FIRST and passed WHOLE — no truncation here.
    # zscore_tiers owns the per-tier slicing, because it is the only thing that
    # knows each tier needs exactly its own N. Capping at 90 here would leave
    # 180d and 252d permanently absent no matter how deep the adapter fetched.
    tiers = zscore_tiers(

        displacement,

        previous_same_time_vwap_history

    )



    side = calculate_side(

        displacement

    )



    acceptance = calculate_acceptance(

        displacement,

        previous_vwap_displacement

    )



    return VWAPResult(

        price=price,

        vwap=vwap,

        displacement=displacement,

        displacement_tiers=tiers,

        side=side,

        acceptance=acceptance,

        status="valid"

    )
