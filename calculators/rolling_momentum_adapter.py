"""
Rolling Momentum adapter — the "shell" around ROLLING_MOMENTUM_LOCKED_V1's
pure `calculate_rolling_momentum_state`. Unlike Momentum (fixed 09:15->09:30
window, block 1 only), this re-evaluates at every completed 15-min block:
raw `session_return` is computed live every cycle from PriceDataManager, and
its z-score is compared against same-block `session_price_move` history
already accumulating in the shared store (populated by
metrics.py:_maybe_write_intraday_block at every block boundary, for every
asset — not just block 1). No cross-cycle state held here (Kind A).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd

from calculators.rolling_momentum_calculator import calculate_rolling_momentum_state
from calculators.schemas import RollingMomentumRow
from calculators.statistics import FETCH_DAYS, finite_positive, tier_fields
from calculators.time_blocks import IST, current_completed_block

logger = logging.getLogger(__name__)

# LOOKBACK = 90 is retired. Tier depths live in calculators/statistics.py
# (TIERS / MAX_TIER / FETCH_DAYS); a local constant named "lookback" holding 90
# would misdescribe an adapter whose top tier is 252.

# The 25-block 15-min grid lives in calculators/time_blocks.py — see the note
# in vwap_adapter.py for why it is no longer redeclared per module.
# `_finite_positive` moved to calculators/statistics.py: it was byte-identical
# in three adapters and absent from the original extraction audit entirely.


def run_rolling_momentum_analysis(
    price_data_manager,
    historical_db_manager,
    *,
    asset_loader=None,
    now: Optional[datetime] = None,
) -> List[RollingMomentumRow]:
    now = (now or datetime.now(IST)).astimezone(IST)

    if price_data_manager is None or not price_data_manager.is_ready():
        return []
    if asset_loader is None or getattr(asset_loader, 'assets_df', None) is None:
        return []

    frame = asset_loader.assets_df
    ids = frame['asset_id'].tolist()
    segments = frame['segment_code'].tolist()
    if not ids:
        return []
    symbol_map = {(aid, seg): sym for aid, seg, sym in zip(
        frame['asset_id'], frame['segment_code'], frame.get('symbol', frame.get('asset_name')))}

    prices = price_data_manager.get_current_prices(ids, segments)
    prev = historical_db_manager.get_previous_close_data(ids, segments)
    if prices is None or prices.empty:
        # No live quotes at all — nothing can be said about any asset.
        return []

    # An EMPTY previous-close set is not a reason to emit nothing. The previous
    # close is written into historical.db.daily_ohlc each morning by the metrics
    # thread's morning step, before any calculator runs; it can still be absent
    # (the fill failed, or that day's close was never filled), and on a fresh
    # install before that step it always was. Aborting here produced zero rows for the whole
    # session, which the health bar reports as a dead calculator — identical to
    # a crash. Each asset now gets a row below stating what is missing.
    #
    # The merge is conditional because joining against a frame with no columns
    # raises on the join key rather than yielding NaN.
    if prev is not None and not prev.empty:
        combined = prices.merge(prev, on=['asset_id', 'segment_code'], how='left', suffixes=('', '_hist'))
    else:
        combined = prices

    block = current_completed_block(now)
    history_by_asset: Dict[Tuple[int, str], List[float]] = {}
    if block is not None:
        # FETCH_DAYS = MAX_TIER * 2 = 504; the old LOOKBACK*2 = 180 could not
        # supply the 252 samples the top tier needs.
        #
        # exclude_date drops TODAY. This table is written live at each block
        # boundary, so today's own session_price_move is already in it — and a
        # point inside its own comparison window pulls mu toward itself and
        # inflates sigma, capping achievable |z| at (n-1)/sqrt(n). Rolling
        # Momentum is one of the three calculators that read this store live
        # (with VWAP and PV Divergence).
        hist_frame = historical_db_manager.get_historical_intraday_metrics(
            ids, segments, time_blocks=[block], days_back=FETCH_DAYS,
            columns=['session_price_move'],
            exclude_date=now.date().isoformat(),
        )
        if hist_frame is not None and not hist_frame.empty:
            hist_frame = hist_frame.dropna(subset=['session_price_move'])
            for row in hist_frame.sort_values('date', ascending=False).itertuples(index=False):
                history_by_asset.setdefault((int(row.asset_id), row.segment_code), []).append(
                    float(row.session_price_move)
                )

    rows: List[RollingMomentumRow] = []

    for row in combined.itertuples(index=False):
        asset_id = int(row.asset_id)
        segment = str(row.segment_code)
        last_price = getattr(row, 'last_price', None)
        prev_close = getattr(row, 'previous_close', None)

        # Emit a row stating WHICH input is missing rather than skipping. The
        # two causes need different fixes and used to look identical (a blank):
        #   no_previous_close  -> the previous trading day's daily_ohlc row has no
        #                         close (the metrics thread's morning step writes
        #                         it from today's first reading)
        #   insufficient_data  -> no live quote has arrived for this asset
        if not finite_positive(last_price) or not finite_positive(prev_close):
            rows.append(RollingMomentumRow(
                asset_id=asset_id,
                segment_code=segment,
                symbol=symbol_map.get((asset_id, segment)),
                current_price=float(last_price) if finite_positive(last_price) else None,
                previous_close=float(prev_close) if finite_positive(prev_close) else None,
                timestamp=now.isoformat(),
                status=('insufficient_data' if not finite_positive(last_price)
                        else 'no_previous_close'),
            ))
            continue

        current_price = float(last_price)
        previous_close = float(prev_close)

        # Most-recent-first, passed WHOLE — no [:LOOKBACK] truncation.
        # zscore_tiers owns the per-tier slicing; capping at 90 here would
        # starve 180d and 252d regardless of how deep the fetch went.
        same_block_hist = history_by_asset.get((asset_id, segment), [])

        result = calculate_rolling_momentum_state(
            current_price=current_price,
            previous_close=previous_close,
            historical_session_returns=same_block_hist,
        )

        rows.append(RollingMomentumRow(
            asset_id=asset_id,
            segment_code=segment,
            symbol=symbol_map.get((asset_id, segment)),
            current_price=current_price,
            previous_close=previous_close,
            timestamp=now.isoformat(),
            session_return=result.session_return,
            status=result.status,
            # z, mu and sigma all arrive from one zscore_tiers call, so mu/sigma
            # can no longer be computed separately from the z they sit beside.
            **tier_fields('session_return', result.session_return_tiers),
        ))

    return rows
