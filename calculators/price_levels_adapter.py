"""
Price Levels adapter — the "shell" around PRICE_LEVELS_LOCKED_V1's pure
`evaluate_price_level` (the module formerly named price_alerts_calculator,
now merged with what was price_levels_locked_v1). Replaces the old stateful
PriceAlertsCalculator class. No bespoke in-memory state: today's crossing
events and `day_events` are both re-derived each cycle (Kind A) from the whole
day's readings, which the OHLC reader holds in memory and tops up with only
the new rows (`get_session_prices`).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from calculators.price_alerts_calculator import PriceLevel, evaluate_price_level
from calculators.schemas import PriceLevelsRow
from calculators.time_blocks import IST

logger = logging.getLogger(__name__)

_LEVEL_COLUMNS = ('local_high', 'global_high', 'local_low', 'global_low')


def _levels_for_row(csv_row) -> Dict[str, PriceLevel]:
    levels = {}
    for col in _LEVEL_COLUMNS:
        val = csv_row.get(col)
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        # A blank CSV cell arrives as NaN, which float() accepts and `<= 0`
        # lets through. Levels are entered by users and many assets have none;
        # an asset left with no levels gets no row (see the caller).
        if math.isnan(val) or val <= 0:
            continue
        level_type = 'resistance' if 'high' in col else 'support'
        levels[col] = PriceLevel(name=col, value=val, level_type=level_type,
                                  created_at=None, source='csv')
    return levels


def _today_prices(ohlc_db_manager, asset_id: int, segment_code: str, now: datetime) -> List[float]:
    # The whole day so far, 09:15 up to `now` — every crossing today counts.
    try:
        frame = ohlc_db_manager.get_session_prices(asset_id, segment_code, now)
    except Exception as exc:
        logger.error("price_levels_adapter: tick read failed for %s: %s", asset_id, exc)
        return []
    if frame is None or frame.empty:
        return []
    frame = frame.sort_values('timestamp')
    # last_price only. `close_price` is Dhan's PREVIOUS-session close, constant
    # all day — falling back to it charted yesterday's close as today's prices.
    for col in ('last_price',):
        if col in frame.columns:
            vals = pd.to_numeric(frame[col], errors='coerce').dropna()
            vals = vals[vals > 0]
            if not vals.empty:
                return vals.tolist()
    return []


def _day_crossed(prices: List[float], level_value: float) -> bool:
    """Whether `level_value` was crossed (either direction) at any point in
    `prices` today — the re-derived equivalent of the old persistent
    day_breach_state, computed fresh instead of remembered."""
    for i in range(1, len(prices)):
        prev_p, cur_p = prices[i - 1], prices[i]
        if (prev_p <= level_value < cur_p) or (prev_p >= level_value > cur_p):
            return True
    return False


def run_price_levels_analysis(
    ohlc_db_manager,
    *,
    asset_loader=None,
    now: Optional[datetime] = None,
) -> List[PriceLevelsRow]:
    now = (now or datetime.now(IST)).astimezone(IST)

    if asset_loader is None or getattr(asset_loader, 'assets_df', None) is None:
        return []
    frame = asset_loader.assets_df
    if frame.empty:
        return []

    rows: List[PriceLevelsRow] = []

    for _, csv_row in frame.iterrows():
        asset_id = int(csv_row['asset_id'])
        segment = csv_row['segment_code']
        symbol = csv_row.get('symbol') or csv_row.get('asset_name')

        levels = _levels_for_row(csv_row)
        if not levels:
            continue

        prices = _today_prices(ohlc_db_manager, asset_id, segment, now)
        if len(prices) < 2:
            continue
        current_price = prices[-1]
        previous_price = prices[-2]

        level_objs = {}
        previous_distances = {}
        day_events: List[str] = []

        for name, level in levels.items():
            # previous_distance: distance at the tick before the current one.
            # Both are positive by construction — _today_prices keeps only
            # prices > 0 and _levels_for_row only levels > 0.
            previous_distances[name] = math.log(previous_price / level.value)
            if _day_crossed(prices, level.value):
                day_events.append(name)
            level_objs[name] = level

        results = {
            name: evaluate_price_level(
                price=current_price,
                previous_price=previous_price,
                level=level,
                current_timestamp=now.isoformat(),
                previous_distance=previous_distances[name],
            )
            for name, level in level_objs.items()
        }

        levels_dict: Dict[str, Dict[str, Any]] = {
            name: {
                'level_value': r.level_value,
                'distance_pct': r.distance_pct,
                'distance_log': r.distance_log,
                'side': r.side,
                'approached': r.approached,
                'crossed': r.crossed,
                'status': r.status,
            }
            for name, r in results.items()
        }

        rows.append(PriceLevelsRow(
            asset_id=asset_id,
            segment_code=segment,
            symbol=symbol,
            current_price=current_price,
            timestamp=now.isoformat(),
            levels=levels_dict,
            day_events=day_events,
            status='valid',
        ))

    return rows
