"""
Price-Volume Divergence adapter — the "shell" around PVD_LOCKED_V3's pure
`calculate_pvd_v3` (Pearson correlation of price/volume deltas, Fisher-z
transformed, with Spearman + Winsorized-Pearson cross-check diagnostics).
Requires true 5-second resolution, which only ohlc.db's readings provide — so
its z-score history cannot come from 5-minute candles. scripts/backfill.py can
rebuild it from ohlc.db's own readings for the (at most 5) trading days ohlc.db
keeps; otherwise it accumulates from live sessions. Pearson/Spearman/Winsorized
correlations compute from the first live cycle; each z-tier stays None until
the shared store's pvd_raw column holds that tier's N sessions (22 for 22d).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

from calculators.price_volume_divergence_calculator import calculate_pvd_v3
from calculators.schemas import PriceVolumeDivergenceRow
from calculators.segments import NON_VOLUME_SEGMENTS
from calculators.statistics import FETCH_DAYS, absent_tiers, tier_fields
from calculators.time_blocks import BLOCK_EDGE_TOLERANCE, IST, current_completed_block

logger = logging.getLogger(__name__)

# LOOKBACK = 90 is retired. Tier depths live in calculators/statistics.py
# (TIERS / MAX_TIER / FETCH_DAYS); a local constant named "lookback" holding 90
# would misdescribe an adapter whose top tier is 252.
WINDOW_SAMPLES = 180  # 15 minutes at the engine's 5-second write cadence


def continuous_price_volume_series(
    readings: pd.DataFrame,
    since: str,
    day_first: Optional[str],
    tolerance: timedelta = BLOCK_EDGE_TOLERANCE,
) -> Optional[Tuple[List[float], List[float]]]:
    """The (prices, volumes) PV Divergence correlates — the readings stamped at
    or after `since` (ISO text, compared as stored), oldest first — or None when
    that stretch was interrupted.

    A reading counts only with a positive price AND a recorded interval volume:
    a stored 0 price would be a huge outlier in the price changes, and a missing
    volume filled with 0 a fabricated reading.

    A reading's interval volume is everything traded since the previous reading
    that recorded a running total, so it is an ordinary ~5-second amount only
    when that reading was recent:
      - `day_first`, the day's first reading with a running total, is left out:
        the engine has no earlier total to subtract, so it holds the whole total
        so far (or 0 after a late start).
      - every other counted reading must come within `tolerance` of the reading
        before it. One that does not holds a whole gap's trading in a single
        interval — a spike the correlation would be built around — so the
        stretch gets no value: None.

    `readings` must reach back `tolerance` before `since`, so that the reading
    before the first counted one is visible. Shared with the per-block snapshot
    (block_snapshots.py): the stored history and the live value it is compared
    against are computed the same way.
    """
    ordered = readings.sort_values('timestamp')
    # The readings that recorded a running total: each one's interval is
    # measured from the one before it in this chain.
    chain = ordered[pd.to_numeric(ordered['volume'], errors='coerce').notna()]
    chain_prices = pd.to_numeric(chain['last_price'], errors='coerce')
    chain_volumes = pd.to_numeric(chain['interval_volume'], errors='coerce')

    prices: List[float] = []
    volumes: List[float] = []
    previous: Optional[datetime] = None
    for stamp, price, volume in zip(chain['timestamp'], chain_prices, chain_volumes):
        moment = datetime.fromisoformat(stamp)
        if stamp >= since and stamp != day_first and price > 0 and not pd.isna(volume):
            if previous is None or moment - previous > tolerance:
                return None
            prices.append(float(price))
            volumes.append(float(volume))
        previous = moment
    return prices, volumes

# The 25-block 15-min grid lives in calculators/time_blocks.py. This module held
# a THIRD encoding of it — raw (hh, mm) tuples plus a function-local dt_time
# import, rather than the dt_time objects the other copies used. Three encodings
# of one grid across seven sites is exactly why it now lives in one place.
#
# `_mean_std` is gone: it computed the DISPLAYED mu/sigma separately from the z,
# the same split that made Volume Block's drawer irreconcilable. Both now come
# from one zscore_tiers call inside the calculator.


def run_price_volume_divergence_analysis(
    ohlc_db_manager,
    historical_db_manager,
    *,
    asset_loader=None,
    now: Optional[datetime] = None,
) -> List[PriceVolumeDivergenceRow]:
    now = (now or datetime.now(IST)).astimezone(IST)

    if asset_loader is None or getattr(asset_loader, 'assets_df', None) is None:
        return []
    frame = asset_loader.assets_df
    ids = frame['asset_id'].tolist()
    segments = frame['segment_code'].tolist()
    if not ids:
        return []
    symbol_map = {(aid, seg): sym for aid, seg, sym in zip(
        frame['asset_id'], frame['segment_code'], frame.get('symbol', frame.get('asset_name')))}

    block = current_completed_block(now)
    history_by_asset: Dict[Tuple[int, str], List[float]] = {}
    if block is not None:
        # FETCH_DAYS = MAX_TIER * 2 = 504; the old LOOKBACK*2 = 180 could not
        # supply the 252 samples the top tier needs.
        #
        # exclude_date drops TODAY. This table is written live at each block
        # boundary, so today's own fisher_value is already in it — and a point
        # inside its own comparison window pulls mu toward itself and inflates
        # sigma, capping achievable |z| at (n-1)/sqrt(n). PV Divergence is one
        # of the three calculators that read this store live (with VWAP and
        # Rolling Momentum).
        hist_frame = historical_db_manager.get_historical_intraday_metrics(
            ids, segments, time_blocks=[block], days_back=FETCH_DAYS,
            columns=['pvd_raw'],
            exclude_date=now.date().isoformat(),
        )
        if hist_frame is not None and not hist_frame.empty:
            hist_frame = hist_frame.dropna(subset=['pvd_raw'])
            for row in hist_frame.sort_values('date', ascending=False).itertuples(index=False):
                history_by_asset.setdefault((int(row.asset_id), row.segment_code), []).append(float(row.pvd_raw))

    window_start = now - timedelta(seconds=WINDOW_SAMPLES * 5)
    since = window_start.isoformat()

    rows: List[PriceVolumeDivergenceRow] = []

    for asset_id, segment in zip(ids, segments):
        # An index has no volume, so there is no price-volume relationship to
        # measure. Say so with a row, as VWAP does for the same four assets —
        # rather than feeding its NULL volume through as a series of zeros.
        if segment in NON_VOLUME_SEGMENTS:
            rows.append(PriceVolumeDivergenceRow(
                asset_id=asset_id,
                segment_code=segment,
                symbol=symbol_map.get((asset_id, segment)),
                timestamp=now.isoformat(),
                status='no_volume_variation',
            ))
            continue

        try:
            # From BLOCK_EDGE_TOLERANCE before the window: the reading before
            # the window's first one decides whether that one's volume is an
            # ordinary interval (continuous_price_volume_series).
            ticks = ohlc_db_manager.get_intraday_data_range(
                asset_id, window_start - BLOCK_EDGE_TOLERANCE, now, segment_code=segment,
            )
            day_first = ohlc_db_manager.session_first_reading(asset_id, segment, now.date())
        except Exception as exc:
            logger.error("pvd_adapter: tick read failed for %s: %s", asset_id, exc)
            continue
        # Most-recent-first, passed WHOLE — no [:LOOKBACK] truncation.
        # zscore_tiers owns the per-tier slicing.
        same_block_hist = history_by_asset.get((asset_id, segment), [])

        # No readings in the window (the first cycles after 09:15, a stock
        # with no quote, or a failed read — the reader logs it and returns an
        # empty frame) is not a reason for no row: with too few readings
        # calculate_pvd_v3 itself reports status 'insufficient_data'. A skipped
        # stock would read as a stopped calculator on the health bar.
        if ticks is None or ticks.empty or 'last_price' not in ticks.columns:
            series = ([], [])
        else:
            series = continuous_price_volume_series(ticks, since, day_first)
        if series is None:
            # A gap of more than BLOCK_EDGE_TOLERANCE in the window put a whole
            # gap's volume into one interval: no value, and a status saying why.
            rows.append(PriceVolumeDivergenceRow(
                asset_id=asset_id,
                segment_code=segment,
                symbol=symbol_map.get((asset_id, segment)),
                timestamp=now.isoformat(),
                status='interrupted',
                **tier_fields('pvd', absent_tiers(len(same_block_hist))),
            ))
            continue
        prices, volumes = series

        result = calculate_pvd_v3(prices=prices, volumes=volumes, previous_fisher_history=same_block_hist)

        rows.append(PriceVolumeDivergenceRow(
            asset_id=asset_id,
            segment_code=segment,
            symbol=symbol_map.get((asset_id, segment)),
            timestamp=now.isoformat(),
            pearson_r=result.pearson_r,
            spearman_r=result.spearman_r,
            winsorized_r=result.winsorized_r,
            fisher_value=result.fisher_value,
            sample_size=result.sample_size,
            critical_r=result.critical_r,
            reliable=result.reliable,
            estimator_agreement=result.estimator_agreement,
            estimators_available=result.estimators_available,
            price_volatility=result.price_volatility,
            status=result.status,
            # z, mu and sigma all arrive from one zscore_tiers call inside the
            # calculator, so mu/sigma cannot occupy a different unit space from
            # the z beside them.
            **tier_fields('pvd', result.pvd_tiers),
        ))

    return rows
