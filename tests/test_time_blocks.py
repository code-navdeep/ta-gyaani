"""
Guards for `calculators/time_blocks.py` — the 25-block session grid.

The engine WRITES block volumes on a detection bracket; the calculators READ
block history keyed by block number. If those two ever disagree about which
15 minutes "block 7" means, the engine stores one window's volume under
another window's label and every z-score built on it is quietly wrong.

`TestEngineBracketParity` pins the engine's 25 brackets as literal expected
values so they remain verified after the six duplicated grid definitions are
deleted from the adapters, `engine.py` and `metrics.py`.
"""
from datetime import date, datetime, time as dt_time, timezone

import pytest

from calculators import time_blocks as TB


def at(hour, minute, second=0):
    """A datetime on a fixed weekday — only the time-of-day matters here."""
    return datetime(2026, 8, 21, hour, minute, second)


class TestGridDefinition:

    def test_session_divides_into_exactly_25_blocks(self):
        start = TB.SESSION_START.hour * 60 + TB.SESSION_START.minute
        end = TB.SESSION_END.hour * 60 + TB.SESSION_END.minute
        assert end - start == 375
        assert (end - start) % TB.BLOCK_MINUTES == 0
        assert (end - start) // TB.BLOCK_MINUTES == TB.BLOCK_COUNT == 25

    def test_first_and_last_block_bounds(self):
        assert TB.block_start_time(1) == dt_time(9, 15)
        assert TB.block_end_time(1) == dt_time(9, 30)
        assert TB.block_start_time(25) == dt_time(15, 15)
        assert TB.block_end_time(25) == dt_time(15, 30)

    def test_blocks_are_contiguous(self):
        for b in range(1, TB.BLOCK_COUNT):
            assert TB.block_end_time(b) == TB.block_start_time(b + 1)

    @pytest.mark.parametrize('bad', [0, -1, 26, 100])
    def test_out_of_range_block_rejected(self, bad):
        with pytest.raises(ValueError):
            TB.block_end_time(bad)

    def test_completion_marks_are_descending(self):
        blocks = [b for _t, b in TB.COMPLETION_MARKS]
        assert blocks == list(range(25, 0, -1))
        assert len(TB.COMPLETION_MARKS) == 25

    def test_all_blocks(self):
        assert TB.all_blocks() == tuple(range(1, 26))


class TestCurrentCompletedBlock:
    """Which block has most recently *finished*."""

    @pytest.mark.parametrize('when,expected,why', [
        (at(8, 0),         None, 'pre-market'),
        (at(9, 15),        None, 'session open — nothing completed yet'),
        (at(9, 29, 59),    None, 'one second before block 1 closes'),
        (at(9, 30),        1,    'block 1 closes exactly at 09:30'),
        (at(9, 44, 59),    1,    'still inside block 2'),
        (at(9, 45),        2,    'block 2 closes'),
        (at(11, 0),        7,    'mid-session'),
        (at(15, 29, 59),   24,   'one second before the close'),
        (at(15, 30),       25,   'final block closes at the bell'),
    ])
    def test_boundaries(self, when, expected, why):
        assert TB.current_completed_block(when) == expected, why

    def test_stays_at_25_through_the_post_close_grace(self):
        """Volume Block's block-25 row is written during the metrics post-close
        grace (~15:30:30 / ~15:31:00), which only works because this keeps
        reporting 25 after the bell."""
        for when in (at(15, 30, 30), at(15, 31), at(15, 33), at(23, 59)):
            assert TB.current_completed_block(when) == 25


class TestEngineBracketParity:
    """The engine's 25 detection brackets, pinned as literals.

    These values were read from `engine.py:_is_time_block_boundary` before its
    hardcoded dict was removed. They must not drift.
    """

    EXPECTED = {
        1:  (dt_time(9, 29),  dt_time(9, 31)),
        2:  (dt_time(9, 44),  dt_time(9, 46)),
        3:  (dt_time(9, 59),  dt_time(10, 1)),
        4:  (dt_time(10, 14), dt_time(10, 16)),
        5:  (dt_time(10, 29), dt_time(10, 31)),
        6:  (dt_time(10, 44), dt_time(10, 46)),
        7:  (dt_time(10, 59), dt_time(11, 1)),
        8:  (dt_time(11, 14), dt_time(11, 16)),
        9:  (dt_time(11, 29), dt_time(11, 31)),
        10: (dt_time(11, 44), dt_time(11, 46)),
        11: (dt_time(11, 59), dt_time(12, 1)),
        12: (dt_time(12, 14), dt_time(12, 16)),
        13: (dt_time(12, 29), dt_time(12, 31)),
        14: (dt_time(12, 44), dt_time(12, 46)),
        15: (dt_time(12, 59), dt_time(13, 1)),
        16: (dt_time(13, 14), dt_time(13, 16)),
        17: (dt_time(13, 29), dt_time(13, 31)),
        18: (dt_time(13, 44), dt_time(13, 46)),
        19: (dt_time(13, 59), dt_time(14, 1)),
        20: (dt_time(14, 14), dt_time(14, 16)),
        21: (dt_time(14, 29), dt_time(14, 31)),
        22: (dt_time(14, 44), dt_time(14, 46)),
        23: (dt_time(14, 59), dt_time(15, 1)),
        24: (dt_time(15, 14), dt_time(15, 16)),
        25: (dt_time(15, 29), dt_time(15, 31)),
    }

    @pytest.mark.parametrize('block', range(1, 26))
    def test_bracket_matches_the_engines_original(self, block):
        assert TB.detection_window(block) == self.EXPECTED[block]

    def test_brackets_never_overlap(self):
        """Two overlapping brackets would let one poll match two blocks."""
        windows = [TB.detection_window(b) for b in range(1, 26)]
        for earlier, later in zip(windows, windows[1:]):
            assert earlier[1] < later[0]

    @pytest.mark.parametrize('when,expected', [
        (at(9, 28, 59), None),
        (at(9, 29),     1),
        (at(9, 30),     1),
        (at(9, 31),     1),
        (at(9, 31, 1),  None),
        (at(12, 0),     11),
        (at(15, 30),    25),
        (at(15, 31, 1), None),
    ])
    def test_block_at_detection_window(self, when, expected):
        assert TB.block_at_detection_window(when) == expected

    def test_bracket_is_wide_enough_for_a_30_second_poll(self):
        """The engine polls every ~5 s (and a slow fetch can stretch that); a
        2-minute bracket cannot be stepped over."""
        for block in range(1, 26):
            start, end = TB.detection_window(block)
            width = ((end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)) * 60
            assert width >= 60, f"block {block} bracket only {width}s wide"


class TestSecondsUntilNextBlockEnd:
    """Drives the block-aligned cache TTL (historical_database_manager)."""

    @pytest.mark.parametrize('when,expected', [
        (at(9, 0),       30 * 60),   # pre-open → block 1 ends at 09:30
        (at(10, 7),       8 * 60),   # the worked example: started 10:07
        (at(10, 14, 30),       30),  # 30 s from the boundary
        (at(10, 14, 59),        1),
    ])
    def test_countdown(self, when, expected):
        assert TB.seconds_until_next_block_end(when) == pytest.approx(expected)

    def test_exact_boundary_rolls_to_the_following_block(self):
        """At exactly 10:15:00 the next boundary is 10:30, not 10:15 — a zero
        TTL would thrash the cache."""
        assert TB.seconds_until_next_block_end(at(10, 15)) == 15 * 60

    def test_none_after_the_final_block(self):
        """No boundary left today, so a TTL caller falls back to its flat
        default rather than caching until tomorrow morning."""
        for when in (at(15, 30), at(15, 31), at(20, 0)):
            assert TB.seconds_until_next_block_end(when) is None

    def test_never_exceeds_one_block_width(self):
        for minute in range(9 * 60 + 30, 15 * 60 + 30):
            remaining = TB.seconds_until_next_block_end(at(minute // 60, minute % 60))
            if remaining is not None:
                assert 0 < remaining <= TB.BLOCK_MINUTES * 60

    def test_timezone_aware_input_is_handled(self):
        aware = at(10, 7).replace(tzinfo=timezone.utc)
        assert TB.seconds_until_next_block_end(aware) == pytest.approx(8 * 60)


class TestRecentCompletedBlocks:
    """Volume Block takes the last three completed blocks (the current one and
    the two before it) so Volume Dynamics has enough ZQ history for velocity,
    acceleration and 3-block persistence. These tests ask for four, to exercise
    the helper past that."""

    def test_returns_newest_first(self):
        assert TB.recent_completed_blocks(at(11, 0), 4) == (7, 6, 5, 4)

    def test_truncates_near_the_open_rather_than_padding(self):
        assert TB.recent_completed_blocks(at(9, 45), 4) == (2, 1)
        assert TB.recent_completed_blocks(at(9, 30), 4) == (1,)

    def test_empty_before_the_first_block_completes(self):
        assert TB.recent_completed_blocks(at(9, 20), 4) == ()

    def test_never_returns_a_block_below_one(self):
        for minute in range(9 * 60 + 30, 15 * 60 + 31):
            blocks = TB.recent_completed_blocks(at(minute // 60, minute % 60), 4)
            assert all(1 <= b <= 25 for b in blocks)

    @pytest.mark.parametrize('count', [0, -1])
    def test_non_positive_count_is_empty(self, count):
        assert TB.recent_completed_blocks(at(11, 0), count) == ()
