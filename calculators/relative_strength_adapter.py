"""
Relative Strength adapter — the "shell" around RELATIVE_STRENGTH_LOCKED_V1.

Beta comes from the precomputed daily `historical.db.beta_cache` (see
scripts/precompute_beta.py, which calls `calculate_beta` from
calculators.statistics — the same function this adapter's math reduces to, so
the daily cache and the live path can never diverge). This adapter composes the
small locked functions directly rather than through a single all-in-one call,
because beta must be injected per tier.

BETA TIER AND Z TIER ARE ONE AXIS — the central invariant of this module.

    session_rs = ln(stock_open/prev) - beta * ln(bench_open/prev)

Beta is a parameter of the MEASUREMENT; the tier is the lookback of the
DISTRIBUTION. Treating them as independent would give 4 betas x 4 tiers = 16
incoherent combinations. Tier T is therefore measured with beta_T and
normalised against exactly T samples that were themselves reconstructed with
beta_T. Before this, every tier used beta_90d and the other three cached beta
tiers were read from the DB and discarded.

`previous_rs` is yesterday's intraday_rs — a cross-day value derivable from
the same history this adapter already reconstructs for z-scoring, not
per-cycle state (same pattern as Momentum's `acceleration`). It is per-tier
for the same reason everything else here is: it comes off that tier's own
reconstructed history.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

from calculators.relative_strength_calculator import (
    calculate_session_relative_strength,
    calculate_intraday_relative_strength,
    calculate_acceptance,
)
from calculators.schemas import RelativeStrengthRow
from calculators.statistics import (
    FETCH_DAYS,
    MAX_TIER,
    TIERS,
    finite_positive,
    log_return,
    tier_fields,
    zscore_tiers,
)
from calculators.time_blocks import IST, block_start_time, block_end_time

logger = logging.getLogger(__name__)

# `LOOKBACK = 90` is retired. It was this adapter's only notion of "how far
# back", and at 90 it could not have fed the 180d or 252d tier even once the
# slicing was fixed. History depth is now MAX_TIER * FETCH_MULTIPLIER, declared
# once in statistics.py.

# Fixed intraday RS window — block 1 of the shared 15-min grid (09:15→09:30).
# Derived rather than hardcoded, for the same reason as momentum_adapter: if
# session hours move, this window must move with them, and RS freezes after the
# window closes so a stale window would be silent for the whole session.
_WINDOW_BLOCK = 1
_WINDOW_START = block_start_time(_WINDOW_BLOCK)
_WINDOW_END = block_end_time(_WINDOW_BLOCK)

# CSV `index` column substring -> (benchmark_name, asset_id, segment_code).
# A benchmark without enough history leaves that pair's tiers empty (None), like
# any other short history — no special case per benchmark.
BENCHMARKS: Dict[str, Tuple[str, int, str]] = {
    'NIFTY': ('Nifty', 13, 'IDX_I'),
    'BankNifty': ('BankNifty', 25, 'IDX_I'),
    'Midcap': ('MidcapNifty', 442, 'IDX_I'),
    'SENSEX': ('Sensex', 51, 'IDX_I'),
}


def benchmarks_for_stock(index_col_value: Optional[str]) -> List[Tuple[str, int, str]]:
    """(benchmark_name, asset_id, segment_code) for each benchmark whose CSV
    substring appears in a stock's `index` field. Public because
    scripts/precompute_beta.py enumerates its β pairs with this same lookup."""
    if not index_col_value:
        return []
    return [v for k, v in BENCHMARKS.items() if k in index_col_value]


def _window_price(ohlc_db_manager, asset_id: int, segment_code: str, now: datetime) -> Optional[float]:
    if now.time() < _WINDOW_END:
        return None
    start_dt = datetime.combine(now.date(), _WINDOW_START, tzinfo=IST)
    end_dt = datetime.combine(now.date(), _WINDOW_END, tzinfo=IST)
    try:
        frame = ohlc_db_manager.get_intraday_data_range(
            asset_id, start_dt, end_dt + timedelta(seconds=1), segment_code=segment_code,
        )
    except Exception as exc:
        logger.error("relative_strength_adapter: window-price read failed for %s: %s", asset_id, exc)
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


def _daily_returns_by_date(price_frame: pd.DataFrame) -> Dict[str, Tuple[float, float]]:
    """{date_str: (open_price, prev_close)} from a get_daily_price_history frame,
    computed via a per-row shift on close_price."""
    if price_frame is None or price_frame.empty:
        return {}
    f = price_frame.sort_values('date').copy()
    f['prev_close'] = f['close_price'].shift(1)
    f = f.dropna(subset=['prev_close', 'open_price'])
    out = {}
    for row in f.itertuples(index=False):
        out[str(row.date)] = (float(row.open_price), float(row.prev_close))
    return out


def _window_returns_by_date(gap_frame: pd.DataFrame, session_frame: pd.DataFrame) -> Dict[str, Tuple[float, float]]:
    """{date_str: (open, price_at_window_end)} reconstructed from stored
    percent series, same trick as momentum_adapter._historical_early_returns
    but returning prices (via prev_close) rather than a single log-return,
    since RS needs open/window-price pairs to feed
    calculate_intraday_relative_strength directly."""
    if (gap_frame is None or gap_frame.empty or session_frame is None or session_frame.empty
            or 'gap' not in gap_frame.columns or 'session_price_move' not in session_frame.columns):
        return {}
    g = gap_frame[['date', 'gap']].copy()
    s = session_frame[['date', 'session_price_move']].copy()
    joined = g.merge(s, on='date', how='inner').dropna()
    out = {}
    for row in joined.itertuples(index=False):
        out[str(row.date)] = (float(row.gap), float(row.session_price_move))
    return out


# `_mean_std` is gone: it computed the DISPLAYED mu/sigma separately from the
# z, the split that made Volume Block's drawer irreconcilable. Both now come
# from one zscore_tiers call. Its [:LOOKBACK] slice and floor-of-20 would also
# have capped the displayed stats at 90 samples.


def _reconstruct_return_pairs(
    stock_daily: Dict[str, Tuple[float, float]],
    bench_daily: Dict[str, Tuple[float, float]],
    stock_window: Dict[str, Tuple[float, float]],
    bench_window: Dict[str, Tuple[float, float]],
) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    """Walk shared historical dates ONCE, returning `(stock_return,
    bench_return)` pairs — most-recent-first, capped at MAX_TIER.

    THIS IS BETA-INDEPENDENT, and that is the point.

        session_rs = ln(stock_open/prev) - beta * ln(bench_open/prev)

    Across tiers only the beta multiplier changes; the underlying return pair
    for each date is identical. So the expensive part — joining dates, deriving
    price levels, taking logs — is done once here, and each tier's history is a
    cheap `s - beta_T * b` projection (see `_project_history`).

    Memoising the four reconstructions instead would rest on the assumption
    that shorter tiers are prefixes of the 252-tier series. They are NOT: a different beta yields genuinely different RS values at every date,
    so the four series differ elementwise. Factoring out the beta is the
    correct optimisation, and it is also exact rather than approximate.
    """
    session_pairs: List[Tuple[float, float]] = []
    intraday_pairs: List[Tuple[float, float]] = []

    shared_dates = sorted(set(stock_daily) & set(bench_daily), reverse=True)
    for d in shared_dates:
        s_open, s_prev = stock_daily[d]
        b_open, b_prev = bench_daily[d]
        s_ret = log_return(s_open, s_prev)
        b_ret = log_return(b_open, b_prev)
        if s_ret is not None and b_ret is not None:
            session_pairs.append((s_ret, b_ret))
        if len(session_pairs) >= MAX_TIER:
            break

    shared_window_dates = sorted(set(stock_window) & set(bench_window), reverse=True)
    for d in shared_window_dates:
        s_gap_pct, s_move_pct = stock_window[d]
        b_gap_pct, b_move_pct = bench_window[d]
        # Reconstruct price levels off an arbitrary base of 100 — only the
        # RATIO (current/previous) matters, which percent-derived levels
        # preserve exactly.
        s_prev_base, b_prev_base = 100.0, 100.0
        s_ret = log_return(s_prev_base * (1.0 + s_move_pct / 100.0),
                           s_prev_base * (1.0 + s_gap_pct / 100.0))
        b_ret = log_return(b_prev_base * (1.0 + b_move_pct / 100.0),
                           b_prev_base * (1.0 + b_gap_pct / 100.0))
        if s_ret is not None and b_ret is not None:
            intraday_pairs.append((s_ret, b_ret))
        if len(intraday_pairs) >= MAX_TIER:
            break

    return session_pairs, intraday_pairs


def _project_history(pairs: List[Tuple[float, float]], beta: float) -> List[float]:
    """Apply one tier's beta to the shared return pairs.

    `s - beta*b` is exactly what calculate_session_relative_strength and
    calculate_intraday_relative_strength compute, since both reduce to
    `log_return(stock) - beta * log_return(bench)`.
    """
    return [s - beta * b for s, b in pairs]


def run_relative_strength_analysis(
    ohlc_db_manager,
    historical_db_manager,
    price_data_manager,
    *,
    asset_loader=None,
    now: Optional[datetime] = None,
) -> List[RelativeStrengthRow]:
    now = (now or datetime.now(IST)).astimezone(IST)

    if asset_loader is None or getattr(asset_loader, 'assets_df', None) is None:
        return []
    if price_data_manager is None or not price_data_manager.is_ready():
        return []

    frame = asset_loader.assets_df
    stock_frame = frame[frame['segment_code'] == 'NSE_EQ']
    if stock_frame.empty:
        return []

    stock_ids = stock_frame['asset_id'].tolist()
    stock_segments = stock_frame['segment_code'].tolist()

    fetch_days = FETCH_DAYS
    bench_ids = [v[1] for v in BENCHMARKS.values()]
    bench_segments = [v[2] for v in BENCHMARKS.values()]

    prices = price_data_manager.get_current_prices(stock_ids + bench_ids, stock_segments + bench_segments)
    prev_close = historical_db_manager.get_previous_close_data(stock_ids + bench_ids, stock_segments + bench_segments)
    beta_rows = historical_db_manager.get_beta_cache(stock_ids, stock_segments)
    if prices is None or prices.empty:
        # No live quotes at all — nothing can be said about any pair.
        return []

    # An EMPTY previous-close set is not a reason to emit nothing. The previous
    # close is written into historical.db.daily_ohlc each morning by the metrics
    # thread's morning step; it can still be absent (the fill failed, or that
    # day's close was never filled). Aborting here produced zero RS rows for the
    # whole session, which the health bar reports as a dead calculator. The map
    # is simply left empty and the per-stock path below reports the gap.
    prev_close_map = {}
    if prev_close is not None and not prev_close.empty:
        for row in prev_close.itertuples(index=False):
            prev_close_map[(row.asset_id, row.segment_code)] = float(row.previous_close)

    price_map = {}
    for row in prices.itertuples(index=False):
        price_map[(row.asset_id, row.segment_code)] = row

    # ALL FOUR beta tiers, not just 90d. scripts/precompute_beta.py has always
    # computed and stored beta_22d/90d/180d/252d; the adapter previously read
    # only beta_90d and discarded the other three. Tier T is measured with
    # beta_T (see the tier loop below).
    beta_map: Dict[Tuple[int, str, str], Dict[int, Optional[float]]] = {}
    if beta_rows is not None and not beta_rows.empty:
        for row in beta_rows.itertuples(index=False):
            per_tier: Dict[int, Optional[float]] = {}
            for tier in TIERS:
                v = getattr(row, f'beta_{tier}d', None)
                per_tier[tier] = float(v) if v is not None and not pd.isna(v) else None
            beta_map[(row.asset_id, row.segment_code, row.benchmark_name)] = per_tier

    # Per-benchmark data, fetched once and shared across every stock using it.
    bench_daily_cache: Dict[Tuple[int, str], Dict[str, Tuple[float, float]]] = {}
    bench_window_cache: Dict[Tuple[int, str], Dict[str, Tuple[float, float]]] = {}
    bench_window_price_cache: Dict[Tuple[int, str], Optional[float]] = {}
    for name, bid, bseg in BENCHMARKS.values():
        daily_hist = historical_db_manager.get_daily_price_history([bid], [bseg], fetch_days)
        bench_daily_cache[(bid, bseg)] = _daily_returns_by_date(daily_hist)
        gap_hist = historical_db_manager.get_historical_gap_data([bid], [bseg], days_back=fetch_days)
        session_hist = historical_db_manager.get_historical_intraday_metrics(
            [bid], [bseg], time_blocks=[_WINDOW_BLOCK], days_back=fetch_days,
            columns=['session_price_move'],
        )
        bench_window_cache[(bid, bseg)] = _window_returns_by_date(gap_hist, session_hist)
        bench_window_price_cache[(bid, bseg)] = _window_price(ohlc_db_manager, bid, bseg, now)

    rows: List[RelativeStrengthRow] = []

    for _, srow in stock_frame.iterrows():
        asset_id = int(srow['asset_id'])
        segment = srow['segment_code']
        symbol = srow.get('symbol') or srow.get('asset_name')
        benchmarks = benchmarks_for_stock(srow.get('index'))
        if not benchmarks:
            continue

        price_row = price_map.get((asset_id, segment))
        stock_previous_close = prev_close_map.get((asset_id, segment))
        stock_open = (float(price_row.open_price)
                      if price_row is not None and finite_positive(price_row.open_price)
                      else None)
        current_price = (float(price_row.last_price)
                         if price_row is not None and finite_positive(price_row.last_price)
                         else None)

        # Emit one row PER BENCHMARK naming what is missing, rather than
        # skipping the stock outright. RS is emitted per (stock, benchmark)
        # pair, so a stock-level skip silently removed EVERY pair for that
        # stock — two rows at once for a stock listed in both NIFTY and SENSEX.
        # Same shape as the 'insufficient_beta_history' blank row below.
        if price_row is None or stock_previous_close is None or stock_open is None:
            blank_status = ('no_previous_close' if stock_previous_close is None
                            else 'insufficient_data')
            for bench_name, bench_id, _bench_seg in benchmarks:
                rows.append(RelativeStrengthRow(
                    asset_id=asset_id, segment_code=segment, symbol=symbol,
                    benchmark_name=bench_name, benchmark_id=bench_id,
                    current_price=current_price, open_price=stock_open,
                    timestamp=now.isoformat(), status=blank_status,
                ))
            continue

        stock_daily_hist = historical_db_manager.get_daily_price_history([asset_id], [segment], fetch_days)
        stock_daily = _daily_returns_by_date(stock_daily_hist)
        stock_gap_hist = historical_db_manager.get_historical_gap_data([asset_id], [segment], days_back=fetch_days)
        stock_session_hist = historical_db_manager.get_historical_intraday_metrics(
            [asset_id], [segment], time_blocks=[_WINDOW_BLOCK], days_back=fetch_days,
            columns=['session_price_move'],
        )
        stock_window = _window_returns_by_date(stock_gap_hist, stock_session_hist)
        stock_window_price = _window_price(ohlc_db_manager, asset_id, segment, now)

        # Same two-causes-one-blank problem as momentum_adapter: a None
        # window price means either 'not 09:30 yet' or 'the engine missed
        # the open, and this is blank for the rest of the session'. Only
        # the second is a fault, and they looked identical. No substitute
        # window is measured — that would answer a different question
        # under the same field name.
        window_missed = (stock_window_price is None
                         and now.time() >= _WINDOW_END)

        for bench_name, bench_id, bench_seg in benchmarks:
            bench_price_row = price_map.get((bench_id, bench_seg))
            bench_prev_close = prev_close_map.get((bench_id, bench_seg))
            betas = beta_map.get((asset_id, segment, bench_name)) or {}

            # Row-level blank ONLY when nothing at all can be computed: no
            # benchmark quote, or not a single beta tier available.
            #
            # Previously this fired whenever beta_90d alone was missing, which
            # threw away a genuinely computable 22d value for any stock with
            # ~100 days of history. An absent tier is now expressed as
            # absent TIER FIELDS (hyphens on the card) — RelativeStrengthRow
            # carries one status, not four, so partial availability is carried
            # by the tier bundle rather than by the status string.
            #
            # The status names the input that is missing — a missing benchmark
            # quote or close used to be reported as insufficient beta history.
            if bench_price_row is None:
                blank_status = 'insufficient_data'
            elif bench_prev_close is None:
                blank_status = 'no_previous_close'
            elif not any(v is not None for v in betas.values()):
                blank_status = 'insufficient_beta_history'
            else:
                blank_status = None
            if blank_status is not None:
                rows.append(RelativeStrengthRow(
                    asset_id=asset_id, segment_code=segment, symbol=symbol,
                    benchmark_name=bench_name, benchmark_id=bench_id,
                    current_price=current_price, open_price=stock_open,
                    timestamp=now.isoformat(), status=blank_status,
                ))
                continue

            bench_open = float(bench_price_row.open_price) if finite_positive(bench_price_row.open_price) else None
            if bench_open is None:
                continue

            # Beta-independent reconstruction, done ONCE for all four tiers.
            # Across tiers only the beta multiplier changes; the underlying
            # return pair per date is identical.
            session_pairs, intraday_pairs = _reconstruct_return_pairs(
                stock_daily, bench_daily_cache[(bench_id, bench_seg)],
                stock_window, bench_window_cache[(bench_id, bench_seg)],
            )

            bench_window_price = bench_window_price_cache.get((bench_id, bench_seg))

            tier_values: Dict[str, object] = {}
            session_bundle: Dict[int, tuple] = {}
            intraday_bundle: Dict[int, tuple] = {}

            for tier in TIERS:
                beta_t = betas.get(tier)

                if beta_t is None:
                    # No beta at this tier — nothing can be measured OR
                    # normalised here. Absent, not zero.
                    session_bundle[tier] = (None, None, None, len(session_pairs))
                    intraday_bundle[tier] = (None, None, None, len(intraday_pairs))
                    continue

                # MEASURED with this tier's beta...
                session_rs_t = calculate_session_relative_strength(
                    stock_open, stock_previous_close, bench_open, bench_prev_close, beta_t,
                )
                intraday_rs_t = None
                if stock_window_price is not None and bench_window_price is not None:
                    intraday_rs_t = calculate_intraday_relative_strength(
                        stock_window_price, stock_open,
                        bench_window_price, bench_open, beta_t,
                    )

                # ...and NORMALISED against history reconstructed with the SAME
                # beta. This is the whole point of tiering RS: beta and the
                # z-tier are one axis. A session_rs measured with beta_90 has no
                # business being z-scored against a 22d distribution built with
                # beta_22.
                session_hist_t = _project_history(session_pairs, beta_t)
                intraday_hist_t = _project_history(intraday_pairs, beta_t)

                # Single-tier zscore_tiers call: reuses the exact-fill rule
                # (exactly T samples or absent) rather than re-implementing it.
                session_bundle[tier] = zscore_tiers(
                    session_rs_t, session_hist_t, tiers=(tier,))[tier]
                intraday_bundle[tier] = zscore_tiers(
                    intraday_rs_t, intraday_hist_t, tiers=(tier,))[tier]

                # acceptance = previous * current, a PRODUCT whose sign is the
                # signal. "Previous" comes from this tier's own history, so it
                # is per-tier too.
                previous_rs_t = intraday_hist_t[0] if intraday_hist_t else None
                acceptance_t = (
                    calculate_acceptance(previous_rs_t, intraday_rs_t)
                    if previous_rs_t is not None and intraday_rs_t is not None
                    else None
                )

                tier_values[f'session_rs_{tier}d'] = session_rs_t
                tier_values[f'intraday_rs_{tier}d'] = intraday_rs_t
                tier_values[f'acceptance_{tier}d'] = acceptance_t

            rows.append(RelativeStrengthRow(
                asset_id=asset_id, segment_code=segment, symbol=symbol,
                benchmark_name=bench_name, benchmark_id=bench_id,
                current_price=current_price, open_price=stock_open,
                timestamp=now.isoformat(),
                status='window_missed' if window_missed else 'valid',
                **{f'beta_{t}d': betas.get(t) for t in TIERS},
                **tier_values,
                **tier_fields('session_rs', session_bundle),
                **tier_fields('intraday_rs', intraday_bundle),
            ))

    return rows
