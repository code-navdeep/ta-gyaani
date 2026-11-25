"""
PVD_LOCKED_V3

Price Volume Divergence / Dynamics

Purpose:
    Robust price-volume relationship measurement.

Core signal preserved from production:

        Pearson correlation

        rho =
        Corr(delta_price, delta_volume)


Added diagnostics:

    1. Spearman correlation
    2. Winsorized Pearson
    3. Fisher transform
    4. Statistical noise floor
    5. Estimator agreement
    6. Price volatility context


Important:

This calculator does NOT generate trades.

It produces measurements.

"""


from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from calculators.statistics import (
    absent_tiers, critical_r as _critical_r, fisher_transform, pearson_corr,
    population_std, spearman_corr, winsorize, zscore_tiers,
)


# {tier: (z, mu, sigma, available_n)} — see calculators/statistics.py.
TierBundle = Dict[int, Tuple[Optional[float], Optional[float],
                             Optional[float], int]]



@dataclass
class PVDResult:

    # Original PVD

    pearson_r: Optional[float]


    # Robust alternatives

    spearman_r: Optional[float]

    winsorized_r: Optional[float]


    # Fisher normalized

    fisher_value: Optional[float]

    # fisher_value z-scored against same-block history, all four tiers.
    pvd_tiers: TierBundle


    # Statistics

    sample_size: int

    critical_r: Optional[float]


    reliable: int


    # Diagnostics

    # Mean of the SIGNS of pearson_r / spearman_r / winsorized_r.
    # +1 = all three agree positive, -1 = all agree negative; between means an
    # outlier tick is driving pearson_r rather than a genuine relationship.
    #
    # Optional, and None when fewer than 2 estimators survive. It previously
    # reported a float regardless — with pearson_r and winsorized_r both None it
    # returned 1.0 computed from spearman_r alone, which reads as "all three
    # agree" when two of the three did not exist.
    estimator_agreement: Optional[float]

    # How many of the three estimators actually produced a value, so the drawer
    # can render "1 of 3" rather than implying unanimity.
    estimators_available: int


    price_volatility: Optional[float]


    status: str




# mean / std / covariance / pearson_corr / rank_values / spearman_corr /
# winsorize / fisher_transform / inverse_fisher / calculate_critical_r /
# calculate_zscore all moved to calculators/statistics.py.
#
# This module held the largest single block of general statistics in the
# codebase — eleven functions, none of them specific to price-volume
# divergence. What remains here is PVD's own math: building the delta series
# and assembling the Result.
#
# `inverse_fisher` was dead (no callers anywhere) and is simply gone.



def calculate_pvd_v3(

    prices: List[float],

    volumes: List[float],

    previous_fisher_history: List[float]

):


    """
    Main PVD V3


    Inputs:

        prices:
            5 second last_price


        volumes:
            5 second interval_volume


        previous_fisher_history:
            same block previous sessions


    """



    # First, before any early return: a mismatch is a caller bug, and must fail
    # loudly on short inputs too, not only on those long enough to be used.
    if len(prices) != len(volumes):
        raise ValueError(
            "prices and volumes length mismatch — this formula needs one "
            "interval_volume per price tick (volumes[i]-volumes[i-1] is "
            "indexed the same way as prices[i]-prices[i-1]), unlike the old "
            "PVD_LOCKED_V2 formula which used a volumes array shifted by one."
        )


    if len(prices)<3:

        # Keyword arguments, not positional. The Result's shape changed here
        # (one z scalar became a tier bundle, and estimators_available was
        # added), so a positional call would silently shift every value along.
        return PVDResult(
            pearson_r=None,
            spearman_r=None,
            winsorized_r=None,
            fisher_value=None,
            pvd_tiers=absent_tiers(len(previous_fisher_history)),
            sample_size=len(prices),
            critical_r=None,
            reliable=0,
            estimator_agreement=None,
            estimators_available=0,
            price_volatility=None,
            status="insufficient_data",
        )


    price_changes=[]

    volume_changes=[]


    for i in range(1,len(prices)):

        price_changes.append(
            prices[i]-prices[i-1]
        )

        volume_changes.append(
            volumes[i]-volumes[i-1]
        )



    n=len(price_changes)



    # ZERO-VARIANCE DETECTION.
    #
    # pearson_corr divides by std(x)*std(y), so it returns None when either
    # delta series is constant. Overwhelmingly that means the PRICE NEVER
    # MOVED across the window: an illiquid stock with no trades for 15 minutes,
    # or a stock locked at a circuit limit.
    #
    # Both are signals in their own right, and both used to be reported as
    # status='valid' with every primary field None — which breaks the
    # system-wide contract that 'valid' means the computation SUCCEEDED, and
    # left them indistinguishable from "PVD has not warmed up yet". They are
    # named now.
    if population_std(price_changes) == 0:
        return PVDResult(
            pearson_r=None, spearman_r=None, winsorized_r=None,
            fisher_value=None,
            pvd_tiers=absent_tiers(len(previous_fisher_history)),
            sample_size=n, critical_r=_critical_r(n), reliable=0,
            estimator_agreement=None, estimators_available=0,
            price_volatility=0.0,
            status="no_price_variation",
        )

    if population_std(volume_changes) == 0:
        return PVDResult(
            pearson_r=None, spearman_r=None, winsorized_r=None,
            fisher_value=None,
            pvd_tiers=absent_tiers(len(previous_fisher_history)),
            sample_size=n, critical_r=_critical_r(n), reliable=0,
            estimator_agreement=None, estimators_available=0,
            price_volatility=population_std(price_changes),
            status="no_volume_variation",
        )


    pearson=pearson_corr(

        price_changes,

        volume_changes

    )


    spearman=spearman_corr(

        price_changes,

        volume_changes

    )


    winsorized_r=pearson_corr(

        winsorize(price_changes),

        winsorize(volume_changes)

    )



    fisher=fisher_transform(
        pearson
    )



    # History is MOST-RECENT-FIRST and passed WHOLE — zscore_tiers owns the
    # per-tier slicing. A [:90] here would leave 180d/252d permanently absent.
    tiers=zscore_tiers(

        fisher,

        previous_fisher_history

    )



    critical=_critical_r(
        n
    )


    # 1 only when there is a correlation AND a critical value AND the
    # correlation clears it.
    reliable = int(
        pearson is not None
        and critical is not None
        and abs(pearson) > critical
    )



    signs=[]


    for r in [
        pearson,
        spearman,
        winsorized_r
    ]:

        if r is None:
            continue

        # An estimator that returned exactly 0.0 DID produce a value — it saw
        # no relationship. It counts as available, with sign 0.
        if r>0:
            signs.append(1)

        elif r<0:
            signs.append(-1)

        else:
            signs.append(0)



    # Agreement needs at least TWO estimators to mean anything. This used to
    # accept `len(signs) > 0`, so a single survivor produced +/-1.0 — which
    # reads as "all three estimators agree" when two of them did not exist.
    # None is the honest answer; estimators_available carries the count so the
    # drawer can render "1 of 3".
    estimators_available=len(signs)

    agreement=(
        sum(signs)/len(signs)
        if estimators_available >= 2
        else None
    )



    volatility=population_std(
        price_changes
    )



    return PVDResult(

        pearson_r=pearson,

        spearman_r=spearman,

        winsorized_r=winsorized_r,

        fisher_value=fisher,

        pvd_tiers=tiers,

        sample_size=n,

        critical_r=critical,

        reliable=reliable,

        estimator_agreement=agreement,

        estimators_available=estimators_available,

        price_volatility=volatility,

        status="valid"

    )
