"""
RELATIVE_STRENGTH_LOCKED_V1

Purpose:
    Measure stock strength relative to benchmark.

Role:
    Context / leadership calculator.

NOT:
    Standalone alpha signal.

Formula:

Beta:

        Cov(stock, benchmark)
beta = -----------------------
             Var(benchmark)


Residual relative strength:

RS =
    Stock Return
    -
    Beta * Benchmark Return


Outputs (assembled per (stock, benchmark) by relative_strength_adapter into
RelativeStrengthRow), each at N = 22 / 90 / 180 / 252 days:

    beta_<N>d
    session_rs_<N>d          measured with that tier's own beta
    intraday_rs_<N>d         the 09:15->09:30 window, same beta
    acceptance_<N>d
    session_rs_z_<N>d
    intraday_rs_z_<N>d

All calculations:
    - point in time
    - previous valid sessions only
    - no future data

"""

from typing import Optional, List

from calculators.statistics import calculate_beta, log_return


# NOTE: there is no `RelativeStrengthResult` dataclass, and that is deliberate.
#
# Every other locked calculator returns a `Result` that its adapter unpacks.
# RS is the exception: because one stock emits one row PER BENCHMARK, and each
# tier needs its own beta, the adapter orchestrates the locked functions below
# directly and assembles RelativeStrengthRow itself. A Result would have to be
# a mapping-of-mappings keyed by (benchmark, tier) and would earn nothing.
#
# The old `RelativeStrengthResult` was produced only by the dead
# `calculate_relative_strength` wrapper and was deleted alongside it.


# log_return, calculate_beta and calculate_zscore moved to
# calculators/statistics.py.
#
# calculate_beta is re-exported above so `scripts/precompute_beta.py` keeps
# importing it from here — that script imports it deliberately, "so the daily
# cache and live math can never diverge". The guarantee is unchanged; only the
# definition moved.
#
# calculate_beta also LOST its internal `len(stock_returns) < 20` floor in the
# move. Floors belong to callers: precompute_beta gates per tier with
# `if len(returns) < n: continue`, which is the correct and only gate. A second
# hidden floor would silently swallow any tier below it.


def calculate_session_relative_strength(
    stock_open: float,
    stock_previous_close: float,

    benchmark_open: float,
    benchmark_previous_close: float,

    beta: float

):

    """
    Overnight/session relative strength.


    RS =
        Stock gap
        -
        beta * Benchmark gap

    """

    stock_return = log_return(
        stock_open,
        stock_previous_close
    )


    benchmark_return = log_return(
        benchmark_open,
        benchmark_previous_close
    )


    if stock_return is None:
        return None


    if benchmark_return is None:
        return None



    return (
        stock_return
        -
        beta *
        benchmark_return
    )



def calculate_intraday_relative_strength(
    stock_current: float,
    stock_previous: float,

    benchmark_current: float,
    benchmark_previous: float,

    beta: float

):

    """
    Intraday relative strength.

    Example:
        09:15 -> 09:30

    """

    stock_return = log_return(
        stock_current,
        stock_previous
    )


    benchmark_return = log_return(
        benchmark_current,
        benchmark_previous
    )


    if stock_return is None:
        return None


    if benchmark_return is None:
        return None


    return (
        stock_return
        -
        beta *
        benchmark_return
    )



def calculate_acceptance(
    previous_rs: float,
    current_rs: float
):

    """
    Measures persistence.

    Positive:
        strength continues

    Negative:
        strength fading
    """

    return (
        previous_rs *
        current_rs
    )



# `calculate_relative_strength` — the all-in-one wrapper — was DELETED here.
#
# It was confirmed dead: referenced only in a docstring in
# relative_strength_adapter.py, never called from anywhere. The adapter calls
# the individual locked functions directly with a cached beta instead, which is
# exactly what that docstring explains.
#
# It was also the last caller of the local `calculate_zscore`, and keeping a
# duplicate shared primitive alive purely to serve dead code would have been
# worse than deleting both. It also carried the last two `[-90:]` slices in the
# codebase, which had been left in place because their caller did not exist and
# their ordering contract could therefore not be verified.
