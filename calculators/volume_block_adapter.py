"""
Volume Block adapter — the "shell" around TRADE_VOLUME_BLOCK_LOCKED_V4's
pure `calculate_trade_volume_state`. No cross-cycle memory: today's blocks
and cross-day same-block/session-cumulative history are both fresh queries
every cycle.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

from calculators.volume_block import (calculate_session_volume_tiers, calculate_trade_volume_state,
                                      reconstruct_session_volume)
from calculators.schemas import VolumeBlockRow
from calculators.statistics import (FETCH_DAYS, TIERS, absent_tiers, safe_log_volume, tier_fields,
                                    zscore_tiers)
from calculators.time_blocks import IST, current_completed_block, recent_completed_blocks
from calculators.segments import NON_VOLUME_SEGMENTS

#: How many recent completed blocks of ZQ to hand to Volume Dynamics.
#:
#: Exactly what its three features need:
#:   velocity      = ZQ_t - ZQ_(t-1)                  -> t, t-1
#:   prev velocity = ZQ_(t-1) - ZQ_(t-2)              -> t-1, t-2
#:   persistence   = mean(ZQ_(t-2), ZQ_(t-1), ZQ_t)   -> t-2, t-1, t
#:
#: No extra block to bridge a gap: a missing block must END the series, not be
#: stepped over (see the ZQ loop below).
_ZQ_HISTORY_BLOCKS = 3

logger = logging.getLogger(__name__)


# `_mean_std` is gone deliberately, not tidied away. It computed the DISPLAYED
# mu/sigma from RAW share counts while the calculator computed z from LOG
# volumes, so the detail drawer showed a z that could not be reconstructed from
# the mu/sigma beside it (a real row: z=+0.2033 vs an implied -0.1089). Both now
# come from one zscore_tiers call inside the calculator, in one unit space.
#
# The local 25-block grid is gone too — see calculators/time_blocks.py, which
# replaced six copies of it.


# Per asset, per tier: (current block's ZQ, prior blocks' ZQ chronological).
#
#   {(asset_id, segment): {22: (z_now, [z_t-3, z_t-2, z_t-1]), 90: (...), ...}}
#
# Priors are CHRONOLOGICAL (oldest first) — the ordering exception Volume
# Dynamics requires so that [-1] is the previous block and [-2] the one before,
# as velocity = ZQ_t - ZQ_(t-1) needs. See the note in volume_dynamics.py.
# Priors are the UNBROKEN run of blocks immediately before the current one, per
# tier: a block that was not written, or has no z at that tier, ends the run.
# So [-1] is always block t-1, never an earlier block standing in for a missing
# one — stepping over a gap made velocity a two-block change labelled as one.
#
# `current` is kept SEPARATE from `priors` rather than appended as the last
# element. With a single flat list, a tier where the current block's z is None
# (insufficient history at that tier) would silently promote the previous
# block's z into the current slot, and velocity would be computed against the
# wrong block with nothing raised.
ZQHistory = Dict[Tuple[int, str], Dict[int, Tuple[Optional[float], List[float]]]]


def run_volume_block_analysis(
    ohlc_db_manager,
    historical_db_manager,
    *,
    asset_loader=None,
    now: Optional[datetime] = None,
) -> Tuple[List[VolumeBlockRow], ZQHistory]:
    """Returns `(rows, zq_history)`.

    `zq_history` is the recent-block ZQ series Volume Dynamics needs, computed
    here and handed over in memory rather than round-tripped through
    `historical.db.intraday_time_block_metrics`.

    Why it moved: that table's other three columns hold raw OBSERVATIONS
    (session_price_move, vwap_displacement, pvd_raw). `block_volume_z_90` was the
    only DERIVED value in it — a z frozen against whatever history existed the
    moment it was written, which goes stale as soon as more history is
    backfilled, and which existed at one tier only. Recomputing it here from the
    same source Volume Block already reads keeps all four tiers mutually
    consistent and lets that column be deleted.

    It costs no extra query: `hist_by_asset_block` below already covers every
    block 1..current, because `session_cum_hist` needs them.
    """
    now = (now or datetime.now(IST)).astimezone(IST)
    block = current_completed_block(now)
    if block is None:
        return [], {}

    if asset_loader is None or getattr(asset_loader, 'assets_df', None) is None:
        return [], {}
    frame = asset_loader.assets_df
    ids = frame['asset_id'].tolist()
    segments = frame['segment_code'].tolist()
    if not ids:
        return [], {}
    symbol_map = {(aid, seg): sym for aid, seg, sym in zip(
        frame['asset_id'], frame['segment_code'], frame.get('symbol', frame.get('asset_name')))}

    # All of today's already-written blocks, one query (no time_block filter).
    # Today's date explicitly: an empty result before the engine writes its
    # first block is the correct answer, and returns nothing below.
    today_frame = ohlc_db_manager.get_volume_comparison_data(
        ids, segments, now.date().isoformat())
    if today_frame is None or today_frame.empty:
        return [], {}
    # A block stored with NO volume is one the engine judged incomplete: not
    # seen within BLOCK_EDGE_TOLERANCE of its start or its end
    # (time_blocks.block_complete), so its total could be wrong by a whole gap.
    # Kept apart from the blocks that have a volume — never read as 0.
    today_by_asset_block: Dict[Tuple[int, str, int], float] = {}
    incomplete_today: Set[Tuple[int, str, int]] = set()
    for row in today_frame.itertuples(index=False):
        key = (int(row.asset_id), row.segment_code, int(row.time_block))
        vol = row.cumulative_volume
        if vol is None or pd.isna(vol):
            incomplete_today.add(key)
        else:
            today_by_asset_block[key] = float(vol)

    needed_blocks = list(range(1, block + 1))
    # FETCH_DAYS = MAX_TIER * 2 = 504. The old LOOKBACK*2 = 180 could not
    # supply the 252 samples the top tier requires, so 252d would stay absent
    # even with retention fixed. The 2x slack absorbs rows dropped by the
    # dropna below.
    hist_frame = historical_db_manager.get_historical_time_block_volumes(
        asset_ids=ids, segment_codes=segments, time_blocks=needed_blocks, days_back=FETCH_DAYS,
    )
    hist_by_asset_block: Dict[Tuple[int, str, int], List[Tuple[str, float]]] = {}
    if hist_frame is not None and not hist_frame.empty:
        f = hist_frame.copy()
        f['cumulative_volume'] = pd.to_numeric(f['cumulative_volume'], errors='coerce')
        f = f.dropna(subset=['cumulative_volume'])
        for (aid, seg, tb), grp in f.groupby(['asset_id', 'segment_code', 'time_block']):
            hist_by_asset_block[(int(aid), seg, int(tb))] = list(zip(grp['date'].astype(str), grp['cumulative_volume']))

    rows: List[VolumeBlockRow] = []
    zq_history: ZQHistory = {}

    # Newest-first from the helper; reversed below so priors end up chronological.
    recent_blocks = recent_completed_blocks(now, _ZQ_HISTORY_BLOCKS)

    for asset_id, segment in zip(ids, segments):
        # Indices are not tradeable and have no volume — emit NO ROW, not a row of
        # zeros. The engine already writes no time_block_volumes row for them, so
        # this would skip by accident; stating it explicitly keeps the rule at the
        # calculator boundary rather than depending on an upstream absence, and
        # keeps the adapter correct against any pre-existing index rows.
        if segment in NON_VOLUME_SEGMENTS:
            continue

        block_volume = today_by_asset_block.get((asset_id, segment, block))
        incomplete = (asset_id, segment, block) in incomplete_today
        if block_volume is None and not incomplete:
            continue                                 # not written yet

        # Passed WHOLE — no [:LOOKBACK] truncation. zscore_tiers owns the
        # per-tier slicing; capping at 90 here would starve 180d and 252d.
        same_block_hist = [v for _, v in hist_by_asset_block.get((asset_id, segment, block), [])]

        # Session cumulative, LIKE FOR LIKE: today's complete blocks, against
        # the same blocks' sum on each prior day that has every one of them.
        # Summing whatever blocks a prior day had compared a short day (late
        # start, outage) as though it were whole, so today read as heavy.
        complete_today = [b for b in needed_blocks if (asset_id, segment, b) in today_by_asset_block]
        completed_blocks_today = [today_by_asset_block[(asset_id, segment, b)] for b in complete_today]
        per_date_blocks: Dict[str, Dict[int, float]] = {}
        for b in complete_today:
            for d, v in hist_by_asset_block.get((asset_id, segment, b), []):
                per_date_blocks.setdefault(d, {})[b] = v
        # sorted(reverse=True) keeps this most-recent-first, per the ordering
        # contract. Passed whole, same reason as same_block_hist above.
        session_cum_hist = [sum(per_date_blocks[d].values())
                            for d in sorted(per_date_blocks, reverse=True)
                            if len(per_date_blocks[d]) == len(complete_today)]

        if block_volume is not None:
            result = calculate_trade_volume_state(
                block_volume=block_volume,
                previous_same_block_volumes=same_block_hist,
                completed_blocks_today=completed_blocks_today,
                previous_session_cumulative_volumes=session_cum_hist,
            )
            status = result.status
            block_tiers = result.block_volume_tiers
            session_volume = result.session_cumulative_volume
            session_tiers = result.session_cumulative_volume_tiers
        else:
            # No block z: a z of an untrustworthy total would light the row,
            # the ■ glyph and the why-line for a block that was not observed.
            # The session cumulative still stands on today's complete blocks.
            status = 'incomplete_block'
            block_tiers = absent_tiers(len(same_block_hist))
            if completed_blocks_today:
                session_volume = reconstruct_session_volume(completed_blocks_today)
                session_tiers = calculate_session_volume_tiers(session_volume, session_cum_hist)
            else:
                session_volume, session_tiers = None, absent_tiers()

        rows.append(VolumeBlockRow(
            asset_id=asset_id,
            segment_code=segment,
            symbol=symbol_map.get((asset_id, segment)),
            timestamp=now.isoformat(),
            time_block=block,
            block_volume=block_volume,
            session_cumulative_volume=session_volume,
            session_cumulative_blocks=len(complete_today),
            status=status,
            # z, mu and sigma all arrive from the calculator's single
            # zscore_tiers call, so mu/sigma are guaranteed to be in the same
            # (log-volume) space as the z they sit beside.
            **tier_fields('block_volume', block_tiers),
            **tier_fields('session_cumulative_volume', session_tiers),
        ))

        # An incomplete block has no ZQ to hand over. Volume Dynamics learns
        # which blocks were incomplete from these rows' status (metrics.py).
        if block_volume is None:
            continue

        # ---- ZQ series for Volume Dynamics -------------------------------
        # Recompute each recent block's abnormality z at all four tiers. No
        # extra query: hist_by_asset_block already covers blocks 1..current
        # (session_cum_hist needs them) and today_by_asset_block holds every
        # block written so far today.
        # Walk back from block t-1, newest first. The first block that was not
        # written, or was written incomplete (no volume), ends the run for every
        # tier; the first block with no z at a tier ends the run for that tier.
        prior_by_tier: Dict[int, List[float]] = {n: [] for n in TIERS}
        run_ended: Dict[int, bool] = {n: False for n in TIERS}
        for b in recent_blocks[1:]:                 # t-1, t-2 — the current block is not a prior
            prior_volume = today_by_asset_block.get((asset_id, segment, b))
            if prior_volume is None:
                break
            prior_hist = [v for _, v in hist_by_asset_block.get((asset_id, segment, b), [])]
            prior_tiers = zscore_tiers(
                safe_log_volume(prior_volume),
                [safe_log_volume(x) for x in prior_hist],
            )
            for n in TIERS:
                z = prior_tiers[n][0]
                if run_ended[n] or z is None:
                    run_ended[n] = True
                    continue
                prior_by_tier[n].append(z)
        for n in TIERS:
            prior_by_tier[n].reverse()              # newest-first -> chronological

        zq_history[(asset_id, segment)] = {
            n: (result.block_volume_tiers[n][0], prior_by_tier[n]) for n in TIERS
        }

    return rows, zq_history
