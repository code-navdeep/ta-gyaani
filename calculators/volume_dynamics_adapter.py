"""
Volume Dynamics adapter — the "shell" around VOLUME_DYNAMICS_LOCKED_V3's pure
`calculate_volume_dynamics`.

Volume Dynamics is a pure derivative of Volume Block: every one of its features
is built from ZQ, Volume Block's abnormal-volume z-score. It reads no database
at all — the ZQ series arrives in memory from the Volume Block adapter's
*this-cycle* output.

That in-memory handover replaced a query against
`historical.db.intraday_time_block_metrics.block_volume_z_90`. Two reasons:

  1. That column was the only DERIVED value in a table whose other three columns
     hold raw observations (session_price_move, vwap_displacement, pvd_raw).
     A stored z is frozen against whatever history existed the moment it was
     written, and goes stale as soon as more history is backfilled.
  2. It existed at ONE tier. Volume Block now emits four, and a velocity built
     from the 90d ZQ would be a mismatched signal the moment the trader selects
     22d.

Since the math is identical at every tier and only the input series differs, the
locked pure function is called once per tier and left untouched.

Indices emit nothing: they are not tradeable, have no volume, and therefore have
no Volume Block row to differentiate.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import AbstractSet, Dict, List, Optional, Tuple

from calculators.volume_dynamics import calculate_volume_dynamics
from calculators.schemas import VolumeDynamicsRow
from calculators.statistics import TIERS
from calculators.time_blocks import IST, current_completed_block

logger = logging.getLogger(__name__)



def run_volume_dynamics_analysis(
    zq_history: Dict[Tuple[int, str], Dict[int, Tuple[Optional[float], List[float]]]],
    *,
    incomplete_blocks: AbstractSet[Tuple[int, str]] = frozenset(),
    asset_loader=None,
    now: Optional[datetime] = None,
) -> List[VolumeDynamicsRow]:
    """Build one 4-tier VolumeDynamicsRow per asset that has a current ZQ, and a
    status-only row per asset whose current block is incomplete.

    Args:
        zq_history: from `run_volume_block_analysis`, shaped
            `{(asset_id, segment): {tier: (current_z, priors_chronological)}}`.
            Priors are oldest-first and are the unbroken run of blocks
            immediately before the current one, so priors[-1] is always block
            t-1: a missing block leaves velocity None rather than measuring a
            two-block change as one.
        incomplete_blocks: `(asset_id, segment)` pairs whose current block
            Volume Block reported as `incomplete_block` — no ZQ, so absent
            from `zq_history`, but they ran and get a row saying why.
    """
    now = (now or datetime.now(IST)).astimezone(IST)
    block = current_completed_block(now)
    if block is None or not (zq_history or incomplete_blocks):
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

    rows: List[VolumeDynamicsRow] = []

    for asset_id, segment in zip(ids, segments):
        if (asset_id, segment) in incomplete_blocks:
            # Its current block was not fully observed, so there is no ZQ to
            # differentiate: a row saying so, every tier absent.
            rows.append(VolumeDynamicsRow(
                asset_id=asset_id,
                segment_code=segment,
                symbol=symbol_map.get((asset_id, segment)),
                timestamp=now.isoformat(),
                time_block=block,
                status='incomplete_block',
            ))
            continue

        per_tier = zq_history.get((asset_id, segment))
        if not per_tier:
            # No Volume Block row this cycle — an index (no volume at all), or a
            # stock whose current block has not been written yet.
            continue

        tier_values: Dict[str, object] = {}
        emitted_any = False

        for tier in TIERS:
            current_z, priors = per_tier.get(tier, (None, []))
            if current_z is None:
                # This tier lacks the history to produce a ZQ, so there is
                # nothing to differentiate. Absent, not zero.
                continue

            # ORDERING: `priors` is CHRONOLOGICAL (oldest first), so [-1] is the
            # previous block and [-2] the one before — exactly what
            # velocity = ZQ_t - ZQ_(t-1) and its own first difference require.
            # This is the one deliberate exception to the most-recent-first
            # contract in calculators/statistics.py: it is a within-session
            # SEQUENCE, not a cross-session distribution. Reversing it would
            # silently flip the sign of every velocity and acceleration.
            previous_volume_z = priors[-1] if priors else None
            previous_velocity = (
                priors[-1] - priors[-2] if len(priors) >= 2 else None
            )
            recent_history = priors[-3:]

            result = calculate_volume_dynamics(
                current_volume_z=current_z,
                previous_volume_z=previous_volume_z,
                previous_velocity=previous_velocity,
                recent_volume_z_history=recent_history,
            )

            tier_values[f'volume_z_{tier}d'] = result.volume_z
            tier_values[f'velocity_{tier}d'] = result.velocity
            tier_values[f'acceleration_{tier}d'] = result.acceleration
            tier_values[f'persistence_{tier}d'] = result.persistence
            tier_values[f'expansion_state_{tier}d'] = result.expansion_state
            emitted_any = True

        # A stock WITH a Volume Block row but no ZQ at any tier yet still gets a
        # row — with every tier absent and a status saying why. Skipping it made
        # "ran, but no same-block history has accumulated" indistinguishable
        # from "did not run", and the health bar reads the difference as a
        # dead calculator. This is the normal state of a fresh install: it holds
        # until historical.db.time_block_volumes has 22 days of this block.
        #
        # (Assets with no per_tier entry at all are still skipped above — those
        # are the indices, which have no volume and so no ZQ to differentiate.)
        rows.append(VolumeDynamicsRow(
            asset_id=asset_id,
            segment_code=segment,
            symbol=symbol_map.get((asset_id, segment)),
            timestamp=now.isoformat(),
            time_block=block,
            status='valid' if emitted_any else 'insufficient_history',
            **tier_values,
        ))

    return rows
