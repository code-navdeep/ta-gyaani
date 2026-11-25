"""
BREAKOUT_LOCKED_V1

Purpose:
    Structural breakout state calculator.

Important:
    This calculator does NOT claim breakout continuation alpha.
    It only measures market structure:
        - breakout location
        - fresh breakout event
        - acceptance
        - failure
        - whether price is outside the range right now

Locked philosophy:
    Breakout = event/state measurement,
    not a standalone trading signal.

Time:
    All calculations must use completed candles only.
"""

from dataclasses import dataclass
from typing import Optional

from calculators.statistics import log_return


@dataclass
class BreakoutResult:
    # None when no opening range was observed (status='invalid_range').
    # Every field below that is MEASURED AGAINST the range is undefined in
    # that case, and says so, rather than reporting a fabricated 0.0.
    range_high: Optional[float]
    range_low: Optional[float]
    range_width: Optional[float]

    breakout_position: Optional[float]

    # State flags stay non-Optional: 0 is a real value meaning "no breakout".
    fresh_breakout: int
    # The day's FIRST breakout: set once, never cleared. Accepted / failed
    # below describe that breakout.
    breakout_direction: int
    # Right now: +1 above the range, -1 below, on the current reading AND the
    # one before it; 0 otherwise. Clears when price comes back. This is what
    # "in breakout" means on the scanning surfaces (glyph, row highlight,
    # why-line, index counts).
    breakout_now: int

    accepted_breakout: int
    failed_breakout: int

    distance_from_range: Optional[float]

    status: str


def calculate_opening_range(
    opening_high: float,
    opening_low: float
):
    """
    Opening range from first 30 minutes.

    Example:
        09:15 - 09:45

    Returns:
        resistance
        support
    """

    if opening_high <= 0 or opening_low <= 0:
        return None, None

    if opening_high < opening_low:
        return None, None

    return opening_high, opening_low



def calculate_range_width(
    high: float,
    low: float
):
    """
    Log normalized opening range width.

    Avoids price-scale dependency.

    This IS `log_return(high, low)` — same expression, same non-positive guard.
    It keeps its own name because "range width" is what it means here, but the
    arithmetic and the guard live in one place. The name is why the first
    extraction audit missed it: that sweep matched function names, and this one
    is named for its domain meaning rather than its computation.
    """

    return log_return(high, low)



def calculate_breakout_position(
    price: float,
    high: float,
    low: float
):
    """
    Continuous breakout location.

    > 1:
        above range

    < -1:
        below range

    0:
        inside range
    """

    width = calculate_range_width(high, low)

    if width is None or width == 0:
        return None


    if price > high:

        return log_return(price, high) / width


    elif price < low:

        # -log(low/price) == log(price/low). Written as log_return(price, low)
        # so the sign is carried by the argument order rather than by a leading
        # minus, and so the non-positive guard is the shared one.
        return log_return(price, low) / width


    return 0.0



def detect_fresh_breakout(
    previous_price: float,
    current_price: float,
    high: float,
    low: float
):
    """
    Detect first crossing event.

    Uses previous completed price only.
    """

    bullish = (
        previous_price <= high
        and current_price > high
    )

    bearish = (
        previous_price >= low
        and current_price < low
    )


    if bullish:
        return 1, 1


    if bearish:
        return 1, -1


    return 0, 0



def detect_breakout_now(
    previous_price: float,
    current_price: float,
    high: float,
    low: float
):
    """
    Outside the opening range right now, confirmed by two consecutive
    readings.

    +1: both readings above the range high
    -1: both readings below the range low
     0: otherwise — inside, or only one reading outside

    Unlike breakout_direction, this clears the moment price comes back.
    """

    if previous_price > high and current_price > high:
        return 1

    if previous_price < low and current_price < low:
        return -1

    return 0



def detect_acceptance(
    prices_after_breakout,
    breakout_direction,
    high,
    low
):
    """
    Acceptance means price remains outside
    the opening range after breakout.

    Requires completed candles after breakout.

    Current rule:
        two consecutive closes outside range.
    """

    if len(prices_after_breakout) < 2:
        return 0


    if breakout_direction == 1:

        valid = all(
            p > high
            for p in prices_after_breakout[-2:]
        )

        return int(valid)


    if breakout_direction == -1:

        valid = all(
            p < low
            for p in prices_after_breakout[-2:]
        )

        return int(valid)


    return 0



def detect_failure(
    prices_after_breakout,
    breakout_direction,
    high,
    low
):
    """
    Failed breakout:

    bullish breakout:
        returns back below resistance

    bearish breakout:
        returns back above support
    """

    if len(prices_after_breakout) == 0:
        return 0


    current = prices_after_breakout[-1]


    if breakout_direction == 1:

        return int(current < high)


    if breakout_direction == -1:

        return int(current > low)


    return 0



def calculate_breakout_state(
    price: float,

    opening_high: float,
    opening_low: float,

    previous_price: float,

    prices_after_breakout=None,

    breakout_direction=0

):

    """
    Main breakout calculator.
    """


    high, low = calculate_opening_range(
        opening_high,
        opening_low
    )


    if high is None:
        # No opening range exists — e.g. the engine was not running during
        # 09:15-09:45, so the range was never observed. Every quantity MEASURED
        # AGAINST that range is therefore undefined, not zero.
        #
        # The previous all-zeros return made two false claims on every asset:
        # breakout_position=0.0 says "price sits exactly at the midpoint of the
        # range", and distance_from_range=0.0 says "price is exactly at the
        # boundary". Both describe a range that does not exist.
        #
        # The four state flags DO stay 0: there is genuinely no breakout, no
        # acceptance and no failure, and 0 is the true value for each of them.
        return BreakoutResult(
            range_high=None,
            range_low=None,
            range_width=None,
            breakout_position=None,
            fresh_breakout=0,
            breakout_direction=0,
            breakout_now=0,
            accepted_breakout=0,
            failed_breakout=0,
            distance_from_range=None,
            status="invalid_range",
        )


    width = calculate_range_width(
        high,
        low
    )


    position = calculate_breakout_position(
        price,
        high,
        low
    )


    fresh, direction = detect_fresh_breakout(
        previous_price,
        price,
        high,
        low
    )


    if breakout_direction == 0:
        breakout_direction = direction



    if prices_after_breakout is None:
        prices_after_breakout=[]



    accepted = detect_acceptance(
        prices_after_breakout,
        breakout_direction,
        high,
        low
    )


    failed = detect_failure(
        prices_after_breakout,
        breakout_direction,
        high,
        low
    )


    return BreakoutResult(

        range_high=high,

        range_low=low,

        range_width=width,

        breakout_position=position,

        fresh_breakout=fresh,

        breakout_direction=breakout_direction,

        breakout_now=detect_breakout_now(
            previous_price,
            price,
            high,
            low
        ),

        accepted_breakout=accepted,

        failed_breakout=failed,

        # None when position is None — a flat opening range (high == low) has
        # zero width, so where price sits relative to it is undefined.
        distance_from_range=abs(position) if position is not None else None,

        status="valid"
    )
