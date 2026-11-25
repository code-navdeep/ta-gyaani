"""
Breakout adapter — the "shell" around BREAKOUT_LOCKED_V1's pure
`calculate_breakout_state`. No cross-day history needed at all. The
breakout moment and `prices_after_breakout` are re-derived every cycle from
the whole day's readings, which the OHLC reader holds in memory and tops up
with only the new rows each cycle (`get_session_prices`). A container restart
loses nothing: the reader refills from ohlc.db on its first cycle.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional, Tuple

import pandas as pd

from calculators.breakout_calculator import calculate_breakout_state
from calculators.schemas import BreakoutRow
from calculators.time_blocks import IST, block_start_time, block_end_time

logger = logging.getLogger(__name__)


# The opening range is blocks 1-2 of the shared 15-min grid: 09:15→09:45.
# Derived, not hardcoded — if session hours move, the grid moves and this range
# follows. This range defines every breakout for the whole session, so a stale
# window would mis-classify breakouts all day with nothing raised.
_RANGE_START = block_start_time(1)
_RANGE_END = block_end_time(2)


def _today_ticks(ohlc_db_manager, asset_id: int, segment_code: str, now: datetime) -> pd.DataFrame:
    # From 09:15 (the session start, which is also _RANGE_START) up to `now`.
    try:
        frame = ohlc_db_manager.get_session_prices(asset_id, segment_code, now)
    except Exception as exc:
        logger.error("breakout_adapter: tick read failed for %s: %s", asset_id, exc)
        return pd.DataFrame()
    if frame is None or frame.empty:
        return pd.DataFrame()
    frame = frame.sort_values('timestamp').reset_index(drop=True)
    # Handle BOTH shapes. `ohlc_data.timestamp` is written IST-aware, so
    # pd.to_datetime returns an aware Series and tz_localize would raise
    # `TypeError: Already tz-aware, use tz_convert to convert`. That call used
    # to be unconditional, and it sits outside the try/except above — so the
    # exception escaped into the orchestrator's per-calculator wrapper and
    # breakout would emit nothing while reading as a DEAD calculator on the
    # health bar rather than as an error. Comparisons at lines below are made
    # against aware pd.Timestamps, so the column must end up aware either way.
    ts = pd.to_datetime(frame['timestamp'], errors='coerce')
    frame['timestamp'] = ts.dt.tz_localize(IST) if ts.dt.tz is None else ts.dt.tz_convert(IST)
    return frame


def _price_series(frame: pd.DataFrame) -> pd.Series:
    # last_price only. `close_price` is Dhan's PREVIOUS-session close, constant
    # all day — falling back to it charted yesterday's close as today's prices.
    for col in ('last_price',):
        if col in frame.columns:
            vals = pd.to_numeric(frame[col], errors='coerce')
            if vals.notna().any():
                return vals
    return pd.Series(dtype=float)


def _find_breakout(prices: List[float], high: float, low: float) -> Tuple[Optional[int], List[float]]:
    """
    Scan the post-opening-range price series for the first fresh-breakout
    crossing (mirrors breakout_calculator.detect_fresh_breakout, applied
    walk-forward instead of one pair at a time). Returns
    (breakout_direction_or_None, prices_after_breakout).

    The caller passes the LAST OPENING-WINDOW reading first, then the readings
    from 09:45 on. Without it, a breakout on the very first reading after 09:45
    is never seen, because that reading has no neighbour before it to cross
    from: ASHOKLEY on 2026-09-04 went from 170.19 (09:44:57, inside a
    168.61-170.23 range) to 170.25 (09:45:02, above) and stayed above, and the
    first breakout was recorded only at 09:54:46, when price dipped inside for
    one reading and crossed out again. The leading reading is always inside the
    range or on its edge — the range is its own window's high and low — so it
    cannot produce a false crossing, and prices_after_breakout (prices[i:],
    i >= 1) never includes it.
    """
    for i in range(1, len(prices)):
        prev_p, cur_p = prices[i - 1], prices[i]
        if prev_p <= high and cur_p > high:
            return 1, prices[i:]
        if prev_p >= low and cur_p < low:
            return -1, prices[i:]
    return None, []


def run_breakout_analysis(
    ohlc_db_manager,
    price_data_manager,
    *,
    asset_loader=None,
    now: Optional[datetime] = None,
) -> List[BreakoutRow]:
    now = (now or datetime.now(IST)).astimezone(IST)

    if asset_loader is None or getattr(asset_loader, 'assets_df', None) is None:
        return []

    frame = asset_loader.assets_df
    ids = frame['asset_id'].tolist()
    segments = frame['segment_code'].tolist()
    if not ids:
        return []

    symbol_col = frame['symbol'] if 'symbol' in frame.columns else frame.get('asset_name')
    symbol_map = {}
    if symbol_col is not None:
        symbol_map = {(aid, seg): sym for aid, seg, sym in
                       zip(frame['asset_id'], frame['segment_code'], symbol_col)}

    rows: List[BreakoutRow] = []

    for asset_id, segment in zip(ids, segments):
        # One asset per `try`: an exception here drops this asset, logged by
        # name — not Breakout for every asset, which is what the orchestrator's
        # single wrapper around this whole function would otherwise do.
        try:
            ticks = _today_ticks(ohlc_db_manager, asset_id, segment, now)

            # Mask the frame, then extract prices AND timestamps from the same
            # rows. Filtering the prices alone and zipping them against the
            # unfiltered timestamp column put every price after the first
            # dropped tick on the wrong timestamp.
            prices = _price_series(ticks)
            keep = prices > 0
            if not keep.any():
                # No reading with a price today yet — the first cycle or two
                # after 09:15, a stock with no quote, or a failed read (logged
                # in _today_ticks). A row saying so, not no row: a missing row
                # reads as a stopped calculator on the health bar.
                # The flags are empty too, NOT 0: 0 claims "not in breakout",
                # and the index card would count the stock as checked.
                rows.append(BreakoutRow(
                    asset_id=int(asset_id),
                    segment_code=segment,
                    symbol=symbol_map.get((asset_id, segment)),
                    timestamp=now.isoformat(),
                    fresh_breakout=None,
                    breakout_direction=None,
                    breakout_now=None,
                    accepted_breakout=None,
                    failed_breakout=None,
                    status='insufficient_data',
                ))
                continue
            price_list = prices[keep].tolist()
            timestamps = ticks.loc[keep, 'timestamp'].tolist()
            current_price = price_list[-1]

            opening_mask = (ticks['timestamp'] >= pd.Timestamp(datetime.combine(now.date(), _RANGE_START, tzinfo=IST))) & \
                           (ticks['timestamp'] < pd.Timestamp(datetime.combine(now.date(), _RANGE_END, tzinfo=IST)))
            opening_prices = _price_series(ticks[opening_mask])
            opening_prices = opening_prices[opening_prices > 0]

            if opening_prices.empty or now.time() < _RANGE_END:
                # Opening range not yet formed.
                opening_high = opening_low = None
            else:
                opening_high = float(opening_prices.max())
                opening_low = float(opening_prices.min())

            range_end = pd.Timestamp(datetime.combine(now.date(), _RANGE_END, tzinfo=IST))
            post_range_prices = [p for p, ts in zip(price_list, timestamps) if ts >= range_end]

            if opening_high is None or len(post_range_prices) < 2:
                result = calculate_breakout_state(
                    price=current_price,
                    opening_high=0, opening_low=0,
                    previous_price=current_price,
                )
            else:
                # Scan from the last opening-window reading, so a breakout on
                # the first reading after 09:45 is seen (see _find_breakout).
                last_opening_price = float(opening_prices.iloc[-1])
                direction, prices_after = _find_breakout(
                    [last_opening_price] + post_range_prices, opening_high, opening_low)
                previous_price = post_range_prices[-2] if len(post_range_prices) >= 2 else current_price
                result = calculate_breakout_state(
                    price=current_price,
                    opening_high=opening_high,
                    opening_low=opening_low,
                    previous_price=previous_price,
                    prices_after_breakout=prices_after,
                    breakout_direction=direction or 0,
                )

            rows.append(BreakoutRow(
                asset_id=int(asset_id),
                segment_code=segment,
                symbol=symbol_map.get((asset_id, segment)),
                current_price=current_price,
                timestamp=now.isoformat(),
                range_high=result.range_high or None,
                range_low=result.range_low or None,
                # Passed through: None when no range exists, and a real 0.0 for
                # a flat range — `or None` used to turn that 0.0 into None.
                range_width=result.range_width,
                breakout_position=result.breakout_position,
                fresh_breakout=result.fresh_breakout,
                breakout_direction=result.breakout_direction,
                breakout_now=result.breakout_now,
                accepted_breakout=result.accepted_breakout,
                failed_breakout=result.failed_breakout,
                distance_from_range=result.distance_from_range,
                status=result.status,
            ))
        except Exception as exc:
            logger.error("breakout_adapter: asset %s/%s failed: %s", asset_id, segment, exc)

    return rows
