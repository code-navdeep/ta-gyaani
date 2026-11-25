"""
Volume Block's view of TODAY's blocks, and the block series it hands to Volume
Dynamics.

- Today's blocks are read for today's date. The reader used to take the newest
  date holding any rows, so on a morning before the engine had written a block
  it returned YESTERDAY's block N and the dashboard showed it as today's.
- The reader is not cached. A total cached just before a block boundary was
  reused for the first cycle after it, and the stored row for the new block was
  its partial total.
- The prior-block series handed to Volume Dynamics is the unbroken run of blocks
  immediately before the current one. Stepping over a missing block made
  velocity a two-block change labelled and thresholded as a one-block change.
"""
import math
import sqlite3
from datetime import datetime

import pandas as pd
import pytest

import engine as engine_mod
from calculators.database_manager import OHLCDatabaseReader
from calculators.time_blocks import IST
from calculators.volume_block_adapter import run_volume_block_analysis
from calculators.volume_dynamics_adapter import run_volume_dynamics_analysis

ASSET, SEG = 1333, 'NSE_EQ'
TODAY = '2026-08-21'
YESTERDAY = '2026-08-20'
AT_1000 = datetime(2026, 8, 21, 10, 0, tzinfo=IST)     # block 3 (09:45-10:00) just completed


class _Loader:
    assets_df = pd.DataFrame([{'asset_id': ASSET, 'segment_code': SEG, 'symbol': 'TEST'}])


class _History:
    """25 prior days for blocks 1-3 — enough for the 22d tier, varied so sigma > 0."""

    def get_historical_time_block_volumes(self, asset_ids, segment_codes, time_blocks, days_back):
        rows = []
        for day in range(25):
            for block in time_blocks:
                rows.append({'date': f'2026-07-{day + 1:02d}', 'asset_id': ASSET,
                             'segment_code': SEG, 'time_block': block,
                             'cumulative_volume': 1000.0 + 37.0 * day + 11.0 * block})
        return pd.DataFrame(rows)


def _reader(tmp_path, blocks):
    """A real ohlc.db holding the given {(date, block): volume} rows."""
    db = engine_mod.OHLCDatabase(db_path=str(tmp_path / 'ohlc.db'))
    with sqlite3.connect(db.db_path) as conn:
        for (date, block), volume in blocks.items():
            conn.execute(
                "INSERT INTO time_block_volumes (date, asset_id, symbol, segment_code, "
                "time_block, cumulative_volume, timestamp, created_at) "
                "VALUES (?, ?, 'TEST', ?, ?, ?, ?, ?)",
                (date, ASSET, SEG, block, volume, f'{date}T10:00:00+05:30',
                 f'{date}T10:00:00+05:30'))
        conn.commit()
    return OHLCDatabaseReader(db_path=db.db_path)


class TestTodaysBlocksOnly:

    def test_yesterdays_blocks_are_not_shown_as_todays(self, tmp_path):
        reader = _reader(tmp_path, {(YESTERDAY, b): 5000.0 for b in (1, 2, 3)})
        rows, zq = run_volume_block_analysis(reader, _History(), asset_loader=_Loader(), now=AT_1000)
        assert rows == [] and zq == {}

    def test_todays_block_is_used_when_written(self, tmp_path):
        reader = _reader(tmp_path, {(YESTERDAY, 3): 5000.0, (TODAY, 3): 1400.0})
        rows, _ = run_volume_block_analysis(reader, _History(), asset_loader=_Loader(), now=AT_1000)
        assert [r.block_volume for r in rows] == [1400.0]


class TestNotCached:

    def test_an_updated_total_is_read_immediately(self, tmp_path):
        reader = _reader(tmp_path, {(TODAY, 3): 700.0})
        first = reader.get_volume_comparison_data([ASSET], [SEG], TODAY)
        with sqlite3.connect(reader.db_path) as conn:
            conn.execute("UPDATE time_block_volumes SET cumulative_volume = 1400")
            conn.commit()
        second = reader.get_volume_comparison_data([ASSET], [SEG], TODAY)
        assert first['cumulative_volume'].tolist() == [700]
        assert second['cumulative_volume'].tolist() == [1400]


class TestUnbrokenPriorRun:

    def test_a_missing_previous_block_leaves_no_priors_and_no_velocity(self, tmp_path):
        reader = _reader(tmp_path, {(TODAY, 1): 1500.0, (TODAY, 3): 1400.0})
        _, zq = run_volume_block_analysis(reader, _History(), asset_loader=_Loader(), now=AT_1000)
        current_z, priors = zq[(ASSET, SEG)][22]
        assert current_z is not None
        assert priors == [], "block 1 must not stand in for the missing block 2"

        row = run_volume_dynamics_analysis(zq, asset_loader=_Loader(), now=AT_1000)[0]
        assert row.velocity_22d is None

    def test_consecutive_blocks_give_the_two_previous_blocks(self, tmp_path):
        reader = _reader(tmp_path, {(TODAY, 1): 1500.0, (TODAY, 2): 1450.0, (TODAY, 3): 1400.0})
        _, zq = run_volume_block_analysis(reader, _History(), asset_loader=_Loader(), now=AT_1000)
        _, priors = zq[(ASSET, SEG)][22]
        assert len(priors) == 2
        row = run_volume_dynamics_analysis(zq, asset_loader=_Loader(), now=AT_1000)[0]
        assert row.velocity_22d is not None


class TestIncompleteCurrentBlock:
    """The engine stores a block it did not see within 30 s of its start or end
    with NO volume (time_blocks.block_complete). Volume Block reports it — a row
    with status incomplete_block and no block z — not a z of an untrustworthy
    total, and not no row (which the health bar reads as a dead calculator)."""

    def test_a_row_saying_so_with_no_block_z(self, tmp_path):
        reader = _reader(tmp_path, {(TODAY, 1): 1500.0, (TODAY, 2): 1450.0, (TODAY, 3): None})
        rows, zq = run_volume_block_analysis(reader, _History(), asset_loader=_Loader(), now=AT_1000)
        (row,) = rows
        assert row.status == 'incomplete_block'
        assert row.block_volume is None and row.block_volume_z_22d is None
        assert row.block_volume_data_points_22d == 25          # history on hand, still reported
        assert (row.session_cumulative_volume, row.session_cumulative_blocks) == (2950.0, 2)
        assert zq == {}, "an incomplete block has no ZQ to hand to Volume Dynamics"

    def test_no_complete_block_today_gives_no_session_total_and_volume_dynamics_reports_it(
            self, tmp_path):
        reader = _reader(tmp_path, {(TODAY, 3): None})
        rows, zq = run_volume_block_analysis(reader, _History(), asset_loader=_Loader(), now=AT_1000)
        (row,) = rows
        assert (row.session_cumulative_volume, row.session_cumulative_blocks) == (None, 0)
        incomplete = {(r.asset_id, r.segment_code) for r in rows if r.status == 'incomplete_block'}
        (dynamics,) = run_volume_dynamics_analysis(zq, incomplete_blocks=incomplete,
                                                   asset_loader=_Loader(), now=AT_1000)
        assert dynamics.status == 'incomplete_block' and dynamics.velocity_22d is None

    def test_an_incomplete_previous_block_ends_the_prior_run(self, tmp_path):
        reader = _reader(tmp_path, {(TODAY, 1): 1500.0, (TODAY, 2): None, (TODAY, 3): 1400.0})
        _, zq = run_volume_block_analysis(reader, _History(), asset_loader=_Loader(), now=AT_1000)
        assert zq[(ASSET, SEG)][22][1] == []


class _HistoryMissing(_History):
    """_History without the given (date, block) rows."""

    def __init__(self, missing):
        self.missing = missing

    def get_historical_time_block_volumes(self, *args, **kwargs):
        f = super().get_historical_time_block_volumes(*args, **kwargs)
        return f[[(d, b) not in self.missing for d, b in zip(f['date'], f['time_block'])]]


class TestSessionCumulativeLikeForLike:
    """Today's session cumulative sums only its complete blocks, and each prior
    day is summed over those same blocks — a prior day missing any of them is
    left out. Summing whatever a prior day had compared a short day (late
    start, outage) as though it were whole."""

    def test_only_todays_complete_blocks_on_both_sides(self, tmp_path):
        reader = _reader(tmp_path, {(TODAY, 1): 1500.0, (TODAY, 2): None, (TODAY, 3): 1400.0})
        (row,), _ = run_volume_block_analysis(reader, _History(), asset_loader=_Loader(), now=AT_1000)
        assert row.status == 'valid'
        assert (row.session_cumulative_volume, row.session_cumulative_blocks) == (2900.0, 2)
        # Prior day d holds (1000 + 37d + 11) + (1000 + 37d + 33) over blocks 1 and 3;
        # the 22d tier takes the 22 most recent days, d = 24 … 3.
        expected = sum(math.log1p(2044 + 74 * d) for d in range(3, 25)) / 22
        assert row.session_cumulative_volume_avg_22d == pytest.approx(expected)

    def test_a_prior_day_missing_one_of_those_blocks_is_left_out(self, tmp_path):
        reader = _reader(tmp_path, {(TODAY, 1): 1500.0, (TODAY, 2): 1450.0, (TODAY, 3): 1400.0})
        (row,), _ = run_volume_block_analysis(reader, _HistoryMissing({('2026-07-01', 2)}),
                                              asset_loader=_Loader(), now=AT_1000)
        assert row.session_cumulative_volume_data_points_22d == 24
