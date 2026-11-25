"""
VOLUME_DYNAMICS_LOCKED_V3

Purpose:
    Measure the change in abnormal participation.

Depends on:
    TRADE_VOLUME_BLOCK_LOCKED_V4

Important:

Volume V4 corrected the database meaning:

    cumulative_volume
        =
    volume inside completed 15 minute block


Therefore:

DO NOT USE:

    CV_t - CV_(t-1)


Instead use:

    ZQ_t

where:

    ZQ_t =
        abnormal block volume


Features:

1. velocity

    Velocity_t =
        ZQ_t - ZQ_(t-1)


2. acceleration

    Acceleration_t =
        Velocity_t - Velocity_(t-1)


3. persistence

    Average abnormal volume state
"""



from dataclasses import dataclass
from typing import Optional, List



@dataclass
class VolumeDynamicsResult:

    volume_z: float

    velocity: Optional[float]

    acceleration: Optional[float]

    persistence: Optional[float]

    expansion_state: Optional[int]

    status: str



def calculate_velocity(
    current_volume_z: float,
    previous_volume_z: Optional[float]
):

    """
    First derivative of abnormal participation.

    Positive:
        volume abnormality increasing

    Negative:
        volume abnormality declining
    """

    if previous_volume_z is None:
        return None


    return (
        current_volume_z
        -
        previous_volume_z
    )



def calculate_acceleration(
    current_velocity: Optional[float],

    previous_velocity: Optional[float]

):

    """
    Second derivative.

    Positive:
        volume expansion is accelerating

    Negative:
        expansion slowing
    """

    if current_velocity is None:
        return None


    if previous_velocity is None:
        return None


    return (
        current_velocity
        -
        previous_velocity
    )



def calculate_persistence(
    recent_volume_z_history: List[float],

    window: int = 3

):

    """
    Measures whether abnormal participation
    persists.

    Example:

    Last 3 blocks:

        2.0
        1.5
        1.8


    persistence = 1.77

    """

    if len(recent_volume_z_history) < window:
        return None


    # ORDERING EXCEPTION — do NOT "fix" this to [:window].
    #
    # Volume Dynamics is the one module whose history is CHRONOLOGICAL
    # (oldest-first), not most-recent-first like every cross-session
    # distribution in calculators/statistics.py. Its input is a within-session
    # block sequence: volume_block_adapter walks back from the previous block,
    # newest first, then reverses the list into time order, precisely so
    # that `[-1]` is the previous block and `[-2]` the one before — which is
    # what velocity = ZQ_t - ZQ_(t-1) and acceleration require.
    #
    # Reversing this series would silently flip the sign of every velocity and
    # acceleration reading. The arithmetic would still succeed; only the
    # answers would be wrong.
    recent = (
        recent_volume_z_history[-window:]
    )


    return (
        sum(recent)
        /
        len(recent)
    )



def calculate_expansion_state(
    velocity: Optional[float],

    acceleration: Optional[float]

):

    """
    State classification.

    Returns:

        +2:
            expanding rapidly

        +1:
            expanding

         0:
            neutral

        -1:
            contracting

        -2:
            collapsing

    """

    # None, not 0: with no velocity (the first completed block of a session has
    # no earlier block to difference) the state is unknown, and 0 would claim
    # "neutral" — a substantive reading the data does not support.
    if velocity is None:
        return None


    if acceleration is None:

        if velocity > 0:
            return 1

        if velocity < 0:
            return -1

        return 0



    if velocity > 0 and acceleration > 0:

        return 2


    if velocity > 0:

        return 1


    if velocity < 0 and acceleration < 0:

        return -2


    if velocity < 0:

        return -1


    return 0



def calculate_volume_dynamics(

    current_volume_z: float,

    previous_volume_z: Optional[float],

    previous_velocity: Optional[float],

    recent_volume_z_history: List[float]

):

    """
    Main Volume Dynamics V3 calculator.

    Inputs:

        current_volume_z:
            ZQ from Volume V4


        previous_volume_z:
            previous block ZQ


        previous_velocity:
            previous volume velocity


        recent_volume_z_history:
            recent ZQ values


    """



    velocity = calculate_velocity(

        current_volume_z,

        previous_volume_z

    )


    acceleration = calculate_acceleration(

        velocity,

        previous_velocity

    )


    persistence = calculate_persistence(

        recent_volume_z_history + [current_volume_z]

    )


    state = calculate_expansion_state(

        velocity,

        acceleration

    )


    return VolumeDynamicsResult(

        volume_z=current_volume_z,

        velocity=velocity,

        acceleration=acceleration,

        persistence=persistence,

        expansion_state=state,

        status="valid"

    )
