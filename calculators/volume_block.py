"""
TRADE_VOLUME_BLOCK_LOCKED_V4

Purpose:
    Measure abnormal volume participation.

Important database correction:

    cumulative_volume field
    DOES NOT mean cumulative from market open.

    It stores:

        volume inside this completed 15-minute block


Old incorrect formula:

    shock = CV_current - CV_previous


Removed.


New definitions:


Block volume:

    Q_b = database cumulative_volume


Primary feature:

    ZQ_N =
        Z_N(
            log(1 + Q_b)
        )

    for N in (22, 90, 180, 252) — one z per tier, each computed from
    EXACTLY N prior same-block observations or absent entirely.


Context feature:


    SessionVolume_b =
        sum(Q_1 ... Q_b)


    ZCV_N =
        Z_N(
            log(1 + SessionVolume_b)
        )


Units note:

    Both features z-score the LOG of volume, and the mu/sigma reported
    alongside each z are in that same log space — they come from the same
    zscore_tiers call. Raw block_volume is reported separately, untransformed.


Role:

    Participation calculator.

Not:

    Standalone trading signal.

"""

from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple

from calculators.statistics import safe_log_volume, zscore_tiers


# {tier: (z, mu, sigma, available_n)} — see calculators/statistics.py.
TierBundle = Dict[int, Tuple[Optional[float], Optional[float],
                             Optional[float], int]]



@dataclass
class VolumeResult:

    # Actual 15 minute block volume
    block_volume: float

    # Abnormal block participation, all four tiers.
    #
    # UNITS: z, mu and sigma are all in LOG-VOLUME space, because that is what
    # the z is computed on (see calculate_block_volume_tiers). They come from a
    # single zscore_tiers call, so they cannot drift apart. Previously the z was
    # computed here on log volumes while the adapter computed mu/sigma on raw
    # share counts — a real stored row reported z=+0.2033 beside a mu/sigma
    # implying -0.1089, opposite signs, on the same card.
    block_volume_tiers: TierBundle


    # Reconstructed volume since open
    session_cumulative_volume: float


    # Abnormal accumulated participation, all four tiers. Same log-space units.
    session_cumulative_volume_tiers: TierBundle


    status: str



# safe_log_volume and calculate_zscore now live in calculators/statistics.py.
# calculate_zscore here was one of six byte-identical copies; keeping a local
# one would let this module's floor and zero-variance behaviour drift away from
# the shared definition, which is the drift the consolidation exists to stop.



def reconstruct_session_volume(
    completed_blocks: List[float]
):

    """
    Reconstruct volume since market open.

    Example:

    blocks:

        09:15 = 10000
        09:30 = 12000
        09:45 = 9000


    session volume:

        31000

    """

    # No max(x, 0) clamp — deliberately. A block volume cannot be negative; the
    # clamp that used to sit here would have silently folded an impossible value
    # in as 0, understating the session total and corrupting the
    # session_cumulative_volume_z distribution without a trace. The upstream
    # defect is fixed at source (engine.py's same-day filter), so the input
    # cannot be negative.
    return sum(
        float(x)
        for x in completed_blocks
    )



def calculate_block_volume_tiers(
    current_block_volume: float,

    previous_same_block_volumes: List[float]

) -> TierBundle:

    """
    Primary feature.

    Compare this block against
    same-time historical blocks,
    at all four tiers.


    History is MOST-RECENT-FIRST and is passed WHOLE — no truncation here.
    zscore_tiers owns the slicing, because it is the only thing that knows
    each tier needs exactly its own N. Truncating to 90 here would leave the
    180d and 252d tiers permanently absent no matter how deep the adapter
    fetched.
    """

    current_log = safe_log_volume(
        current_block_volume
    )


    history = [

        safe_log_volume(x)

        for x in previous_same_block_volumes

    ]


    return zscore_tiers(
        current_log,
        history
    )



def calculate_session_volume_tiers(
    current_session_volume: float,

    previous_session_volumes: List[float]

) -> TierBundle:

    """
    Context feature.

    Compares accumulated daily
    participation against history,
    at all four tiers.
    """

    current_log = safe_log_volume(
        current_session_volume
    )


    history = [

        safe_log_volume(x)

        for x in previous_session_volumes

    ]


    return zscore_tiers(
        current_log,
        history
    )



def calculate_trade_volume_state(

    block_volume: float,

    previous_same_block_volumes: List[float],

    completed_blocks_today: List[float],

    previous_session_cumulative_volumes: List[float]

):

    """
    Main Volume V4 calculator.


    Inputs:

    block_volume:
        current completed 15 minute volume


    previous_same_block_volumes:
        same block from previous sessions


    completed_blocks_today:
        all completed blocks today including current


    previous_session_cumulative_volumes:
        historical accumulated session volumes


    """



    session_volume = reconstruct_session_volume(
        completed_blocks_today
    )


    block_tiers = calculate_block_volume_tiers(

        block_volume,

        previous_same_block_volumes

    )


    session_tiers = calculate_session_volume_tiers(

        session_volume,

        previous_session_cumulative_volumes

    )


    return VolumeResult(

        block_volume=block_volume,

        block_volume_tiers=block_tiers,

        session_cumulative_volume=session_volume,

        session_cumulative_volume_tiers=session_tiers,

        status="valid"

    )
