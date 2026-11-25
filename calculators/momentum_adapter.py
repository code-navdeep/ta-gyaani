"""
Momentum adapter — the "shell" around MOMENTUM_LOCKED_V2's pure
`calculate_momentum_state`. Fetches live + historical data, reconstructs the
two history series the locked formula needs (gap returns and early returns,
most recent first, up to 252 sessions each — the top tier), and returns
List[MomentumRow].

No cross-cycle state is held here (Momentum needs none — the 09:15->09:30
window is fixed and re-derivable fresh from ohlc.db every cycle).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from calculators.momentum_calculator import calculate_momentum_state
from calculators.schemas import MomentumRow
from calculators.statistics import FETCH_DAYS, MAX_TIER, finite_positive, tier_fields
from calculators.time_blocks import IST, block_start_time, block_end_time

logger = logging.getLogger(__name__)

# LOOKBACK = 90 is retired. Tier depths live in calculators/statistics.py
# (TIERS / MAX_TIER / FETCH_DAYS); a local constant named "lookback" holding 90
# would misdescribe an adapter whose top tier is 252.

# Fixed first momentum window — block 1 of the shared 15-min grid (09:15→09:30).
# Derived, not hardcoded: if session hours ever move, the grid moves with them
# and this window follows. Hardcoded, Momentum would keep measuring the old
# window — and since it freezes after the window closes, that error would be
# silent for the entire session.
_WINDOW_BLOCK = 1
_WINDOW_START = block_start_time(_WINDOW_BLOCK)
_WINDOW_END = block_end_time(_WINDOW_BLOCK)


# `_finite_positive` moved to calculators/statistics.py — it was byte-identical
# in three adapters and absent from the original extraction audit entirely.


def _pct_to_log(pct: float) -> float:
    """Convert a percent simple return (e.g. 0.5 meaning 0.5%) to a log return."""
    return math.log1p(pct / 100.0)


def _historical_gap_log_returns(gap_frame: pd.DataFrame) -> List[float]:
    """gap_frame.gap is a percent simple return (open vs prev close). Convert to
    log-return form, most-recent first, capped at MAX_TIER entries.

    The cap is applied while BUILDING, not while slicing. Momentum is
    the only calculator that truncates here rather than at the z-score, so
    raising the fetch depth or removing a `[:90]` elsewhere would not have been
    enough: the list simply never reached 252 entries, and 180d/252d would have
    stayed absent with nothing raised.
    """
    if gap_frame is None or gap_frame.empty or 'gap' not in gap_frame.columns:
        return []
    f = gap_frame.copy()
    f['gap'] = pd.to_numeric(f['gap'], errors='coerce')
    f = f.dropna(subset=['gap'])
    if 'date' in f.columns:
        f = f.sort_values('date', ascending=False)
    out: List[float] = []
    for g_pct in f['gap'].tolist():
        if g_pct <= -100.0:
            continue
        out.append(_pct_to_log(float(g_pct)))
        if len(out) == MAX_TIER:
            break
    return out


def _historical_early_returns(session_frame: pd.DataFrame, gap_frame: pd.DataFrame) -> List[float]:
    """
    Reconstruct historical early_return = ln(P_0930 / Open) without a dedicated
    stored field, from two percent series that already exist:

        session_price_move (block 1) = (P_0930 / PrevClose - 1) * 100
        gap                           = (Open / PrevClose - 1) * 100

        P_0930 / Open = (1 + s_simple) / (1 + g_simple)
    """
    if (
        session_frame is None or session_frame.empty
        or gap_frame is None or gap_frame.empty
        or 'session_price_move' not in session_frame.columns
        or 'gap' not in gap_frame.columns
        or 'date' not in session_frame.columns
        or 'date' not in gap_frame.columns
    ):
        return []

    s = session_frame[['date', 'session_price_move']].copy()
    g = gap_frame[['date', 'gap']].copy()
    s['session_price_move'] = pd.to_numeric(s['session_price_move'], errors='coerce')
    g['gap'] = pd.to_numeric(g['gap'], errors='coerce')

    joined = s.merge(g, on='date', how='inner').dropna()
    if joined.empty:
        return []
    joined = joined.sort_values('date', ascending=False)

    out: List[float] = []
    for row in joined.itertuples(index=False):
        s_simple = float(row.session_price_move) / 100.0
        g_simple = float(row.gap) / 100.0
        if s_simple <= -1.0 or g_simple <= -1.0:
            continue
        ratio = (1.0 + s_simple) / (1.0 + g_simple)
        if ratio <= 0.0:
            continue
        out.append(math.log(ratio))
        if len(out) == MAX_TIER:
            break
    return out


# `_mean_std` is gone. Its own docstring described the problem: "Population
# mean/std matching momentum_calculator.calculate_zscore's own internal formula
# EXACTLY, so the displayed mu/sigma stay consistent with the displayed z" —
# i.e. two separate computations that had to be hand-kept in agreement. Volume
# Block is what happens when they drift: its drawer showed z=+0.2033 beside a
# mu/sigma implying -0.1089. Both now come from one zscore_tiers call.
# Its `[:LOOKBACK]` slice and floor-of-20 would also have capped the displayed
# stats at 90 samples and reported stats for tiers that have no z.


def _completed_window_price(
    ohlc_db_manager, asset_id: int, segment_code: str, now: datetime,
) -> Optional[float]:
    """Price at the end of the fixed 09:15->09:30 window, today. None before
    09:30 or if no ticks exist yet for that window."""
    if now.time() < _WINDOW_END:
        return None
    start_dt = datetime.combine(now.date(), _WINDOW_START, tzinfo=IST)
    end_dt = datetime.combine(now.date(), _WINDOW_END, tzinfo=IST)
    try:
        frame = ohlc_db_manager.get_intraday_data_range(
            asset_id, start_dt, end_dt + timedelta(seconds=1), segment_code=segment_code,
        )
    except Exception as exc:
        logger.error("momentum_adapter: window-price read failed for %s: %s", asset_id, exc)
        return None
    if frame is None or frame.empty:
        return None
    # last_price only. `close_price` is Dhan's PREVIOUS-session close, constant
    # all day — preferring it made "the price at 09:30" yesterday's close.
    for col in ('last_price',):
        if col in frame.columns:
            vals = pd.to_numeric(frame[col], errors='coerce').dropna()
            vals = vals[vals > 0]
            if not vals.empty:
                return float(vals.iloc[-1])
    return None


def _previous_close_value(row: pd.Series) -> Optional[float]:
    if 'previous_close' in row.index and finite_positive(row.get('previous_close')):
        return float(row['previous_close'])
    return None


def run_momentum_analysis(
    ohlc_db_manager,
    historical_db_manager,
    price_data_manager,
    *,
    asset_loader=None,
    now: Optional[datetime] = None,
) -> List[MomentumRow]:
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

    prices = price_data_manager.get_current_prices(ids, segments)
    prev = historical_db_manager.get_previous_close_data(ids, segments)
    if prices is None or prices.empty:
        # No live quotes at all — nothing can be said about any asset.
        return []

    # An EMPTY previous-close set is not a reason to emit nothing. The previous
    # close is written into historical.db.daily_ohlc each morning by the metrics
    # thread's morning step, before any calculator runs; it can still be absent
    # (the fill failed, or that day's close was never filled), and on a fresh
    # install before that step it always was. Aborting here produced zero momentum rows for
    # the whole session — which the health bar reports as a dead calculator, and
    # which silently emptied the index card too (advancers/decliners and
    # weighted_intraday_return are both derived from this output).
    #
    # The merge is conditional because joining against a frame with no columns
    # raises on the join key rather than yielding NaN.
    if prev is not None and not prev.empty:
        # Both keys, always: asset_id 25 is BankNifty in IDX_I and ADANIENT in
        # NSE_EQ, so a merge on asset_id alone joins each to both closes.
        combined = prices.merge(prev, on=['asset_id', 'segment_code'], how='left', suffixes=('', '_hist'))
    else:
        combined = prices

    # FETCH_DAYS = MAX_TIER * 2 = 504; the old LOOKBACK*2 = 180 could not supply
    # the 252 samples the top tier needs.
    fetch_days = FETCH_DAYS

    gap_history = historical_db_manager.get_historical_gap_data(ids, segments, days_back=fetch_days)
    gap_by_asset: Dict[Tuple[Any, Any], pd.DataFrame] = {}
    if gap_history is not None and not gap_history.empty:
        for key, grp in gap_history.groupby(['asset_id', 'segment_code'], dropna=False):
            gap_by_asset[key] = grp.sort_values('date', ascending=False)

    # NO exclude_date here, deliberately — unlike VWAP, Rolling Momentum and PV
    # Divergence, which all pass it.
    #
    # Momentum is structurally immune. `_historical_early_returns` inner-joins
    # this block-1 series against the daily_ohlc-derived gaps, and TODAY's
    # daily_ohlc row is only written at 15:34 by scripts/daily.py (the morning
    # step writes only the previous trading day's row) — so today's date is absent from
    # the gap side, and the inner join drops today from the early-return history
    # on its own. The gap history comes from daily_ohlc directly and is immune
    # for the same reason.
    #
    # Do not "fix" this by adding exclude_date: it would be a no-op here, and
    # implying Momentum shares the contamination problem would be misleading.
    session_history = historical_db_manager.get_historical_intraday_metrics(
        ids, segments, time_blocks=[_WINDOW_BLOCK], days_back=fetch_days,
        columns=['session_price_move'],
    )
    session_by_asset: Dict[Tuple[Any, Any], pd.DataFrame] = {}
    if session_history is not None and not session_history.empty:
        for key, grp in session_history.groupby(['asset_id', 'segment_code'], dropna=False):
            session_by_asset[key] = grp.sort_values('date', ascending=False)

    rows: List[MomentumRow] = []

    for _, row in combined.iterrows():
        if pd.isna(row.get('asset_id')):
            continue
        asset_id = int(row['asset_id'])
        segment = str(row.get('segment_code', ''))
        symbol = str(row.get('symbol')) if row.get('symbol') is not None else None

        open_price = float(row['open_price']) if finite_positive(row.get('open_price')) else None
        current_price = float(row['last_price']) if finite_positive(row.get('last_price')) else None
        previous_close = _previous_close_value(row)
        high_price = float(row['high_price']) if finite_positive(row.get('high_price')) else None
        low_price = float(row['low_price']) if finite_positive(row.get('low_price')) else None

        # Emit a row naming WHICH input is missing rather than skipping. The two
        # causes need different fixes and used to render as the same blank:
        #   no_previous_close  -> the previous trading day's daily_ohlc row has no
        #                         close (the metrics thread's morning step writes
        #                         it from today's first reading)
        #   insufficient_data  -> no opening quote has arrived for this asset
        #
        # Skipping here also made status='window_missed' unreachable on exactly
        # the day it exists for: before the morning step existed, a late engine
        # start left daily_ohlc without a previous close AND the 09:15-09:30
        # window empty, and the first condition aborted before the second could
        # ever be reported.
        if open_price is None or previous_close is None:
            rows.append(MomentumRow(
                asset_id=asset_id,
                segment_code=segment,
                symbol=symbol,
                current_price=current_price,
                open_price=open_price,
                previous_close=previous_close,
                high_price=high_price,
                low_price=low_price,
                timestamp=now.isoformat(),
                status=('no_previous_close' if previous_close is None
                        else 'insufficient_data'),
            ))
            continue

        key = (asset_id, segment)
        gap_frame = gap_by_asset.get(key, pd.DataFrame())
        gap_hist = _historical_gap_log_returns(gap_frame)

        price_after_window = _completed_window_price(ohlc_db_manager, asset_id, segment, now)

        session_frame = session_by_asset.get(key, pd.DataFrame())
        early_hist = _historical_early_returns(session_frame, gap_frame)
        # early_hist is sorted most-recent-first, so its first entry is
        # yesterday's early_return — exactly what "previous" means for the
        # acceleration (change-in-early-momentum) calculation.
        previous_early_return = early_hist[0] if early_hist else None

        # The real window price or None — never a stand-in. Passing the open
        # here made early_return ln(open/open) = 0, which was then z-scored
        # against real history. With None the calculator returns every
        # window-dependent field empty and status 'window_not_closed'.
        result = calculate_momentum_state(
            open_price=open_price,
            previous_close=previous_close,
            price_after_window=price_after_window,
            previous_early_return=previous_early_return,
            previous_gap_history=gap_hist,
            previous_early_history=early_hist,
        )

        window_complete = price_after_window is not None

        # `price_after_window is None` has TWO structurally different causes,
        # and they were indistinguishable on screen:
        #
        #   1. it is not 09:30 yet          -> normal, resolves by itself
        #   2. the window PASSED with no ticks -> the engine missed the open,
        #      and this asset is blank for the rest of the session with no
        #      recovery path (observed live 2026-08-11, first tick 11:49)
        #
        # Both rendered as the same empty cell, so a trader seeing a blank
        # Momentum column at 14:00 could not tell "not ready yet" from "will
        # never be ready today".
        #
        # Deliberately NOT fabricating a substitute window: measuring
        # 11:49->12:04 and reporting it as early_return would answer a
        # different question under the same field name. These calculators ask
        # "was the OPEN unusual?", and that cannot be answered if the open was
        # not observed.
        #
        # Derived here rather than inside _completed_window_price so that
        # function keeps one job, and the pure calculator stays untouched —
        # engine start time is a shell concern, not calculator math.
        window_missed = (not window_complete) and now.time() >= _WINDOW_END

        rows.append(MomentumRow(
            asset_id=asset_id,
            segment_code=segment,
            symbol=symbol,
            current_price=current_price,
            open_price=open_price,
            previous_close=previous_close,
            price_after_window=price_after_window,
            high_price=high_price,
            low_price=low_price,
            timestamp=now.isoformat(),
            gap_return=result.gap_return,
            gap_direction=result.gap_direction,
            early_return=result.early_return,
            continuation=result.continuation,
            acceleration=result.acceleration,
            status='window_missed' if window_missed else result.status,
            # z, mu and sigma all arrive from the calculator's zscore_tiers
            # calls, so mu/sigma cannot end up in a different unit space from
            # the z beside them. Before 09:30 the early-return tiers are empty
            # by construction (no window price), with data_points still
            # reporting the history on hand.
            **tier_fields('gap', result.gap_tiers),
            **tier_fields('early_return', result.early_return_tiers),
        ))

    return rows
