"""
PRICE_LEVELS_LOCKED_V1

Purpose:
    Point-in-time price level event engine.

Role:
    Event/context calculator.

NOT:
    A standalone alpha generator.

Measures:
    - distance to levels
    - level approach
    - crossing events

Important:
    Levels must be generated BEFORE the evaluation timestamp.

No future information allowed.

Example levels:
    local_high
    local_low
    global_high
    global_low

"""

from dataclasses import dataclass
from typing import Optional

from calculators.statistics import log_return, sign



@dataclass
class PriceLevel:

    name: str

    value: float

    level_type: str

    created_at: Optional[str]

    source: Optional[str]



@dataclass
class LevelEventResult:

    level_name: str

    level_value: float

    distance_pct: float

    distance_log: float

    side: int

    approached: int

    crossed: int

    status: str



def validate_level(
    level: PriceLevel,
    current_timestamp: str
):
    """
    Ensures level existed before usage.

    In production:
        created_at <= current_timestamp

    """

    if level.value <= 0:
        return False


    if (
        level.created_at is not None
        and level.created_at > current_timestamp
    ):
        return False


    return True



def calculate_distance(
    price: float,
    level: float
):
    """
    Signed logarithmic distance.

    Positive:
        price above level

    Negative:
        price below level

    This IS `log_return(price, level)` — same expression, same non-positive
    guard. It keeps its own name because "distance from a level" is what it
    means here, but the arithmetic and the guard live in one place. The name is
    why the first extraction audit missed it: that sweep matched function names,
    and this one is named for its domain meaning rather than its computation.
    """

    return log_return(price, level)



def calculate_distance_percent(
    price: float,
    level: float
):

    if level <= 0:
        return None


    return (
        (price-level)
        /
        level
    ) * 100



def detect_side(
    price: float,
    level: float
):

    """
    +1 = above level
    -1 = below level
     0 = exactly at the level

    This IS `sign(price - level)`. Kept under its own name because that is what
    it means here; the three-way comparison itself is shared with Momentum's
    gap direction and VWAP's side-of-VWAP.
    """

    return sign(price - level)



def detect_approach(
    previous_distance: float,
    current_distance: float,

    threshold: float = 0.002
):

    """
    Detect price moving close to level.

    threshold:
        0.2%

    Used as a state label,
    not a trading threshold.
    """

    if abs(current_distance) > threshold:
        return 0


    if abs(current_distance) < abs(previous_distance):
        return 1


    return 0



def detect_crossing(
    previous_price: float,
    current_price: float,
    level: float
):

    """
    Fresh level crossing event.

    """

    upward = (
        previous_price <= level
        and current_price > level
    )


    downward = (
        previous_price >= level
        and current_price < level
    )


    if upward:
        return 1


    if downward:
        return -1


    return 0



def evaluate_price_level(
    price: float,

    previous_price: float,

    level: PriceLevel,

    current_timestamp: str,

    previous_distance: float

):

    """
    Main price level calculator.

    Returns event state.
    """

    if not validate_level(
        level,
        current_timestamp
    ):

        # By keyword, not position: a field added to the Result would otherwise
        # shift every value one slot along with nothing raised.
        return LevelEventResult(
            level_name=level.name,
            level_value=level.value,
            distance_pct=0,
            distance_log=0,
            side=0,
            approached=0,
            crossed=0,
            status="invalid_level"
        )



    distance_log = calculate_distance(
        price,
        level.value
    )


    distance_pct = calculate_distance_percent(
        price,
        level.value
    )


    side = detect_side(
        price,
        level.value
    )


    approached = detect_approach(
        previous_distance,
        distance_log
    )


    crossing = detect_crossing(
        previous_price,
        price,
        level.value
    )


    return LevelEventResult(

        level_name=level.name,

        level_value=level.value,

        distance_pct=distance_pct,

        distance_log=distance_log,

        side=side,

        approached=approached,

        crossed=crossing,

        status="valid"
    )
