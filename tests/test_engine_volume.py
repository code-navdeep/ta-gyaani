"""
Engine volume path — the same-day filter, and indices having no volume.

`_calculate_interval_volume` turns the API's cumulative daily volume into a
per-interval delta. Two properties are pinned here, both of which were broken and
both of which fail silently when broken:

  1. **The same-day filter.** `ohlc.db` retains 5 trading days and the exchange
     resets cumulative volume every morning. Without `substr(timestamp, 1, 10) = today` the
     lookup returns YESTERDAY'S CLOSING cumulative on the first tick of a session,
     so the subtraction goes hugely negative — and a `max(0, ...)` clamp used to
     turn that into `0`. That zeroed the FIRST reading of every session, the only
     one with no earlier same-day reading to subtract from, so block 1 lost
     whatever the exchange had already counted before that first poll — every
     day, for every asset. Measured on TCS 2026-08-28: ~66,818 of block 1's
     ~688,491 shares, about 10%. Not the whole block; every later reading
     subtracted correctly.

     That ~66,818 is volume the API attributes to before 09:15. Neither the
     exchange nor the broker reports it separately or says where it came from,
     so do not label it (no "auction match", no "odd-lot session").

     That clamp is now gone, so this test is the only thing standing between the
     codebase and a silent return of the bug.

  2. **Indices have no volume.** An index is a computed number, not a tradeable
     instrument. It gets NULL — never 0 — so "has no volume" stays distinguishable
     from "zero traded", and no time_block_volumes row is written at all.

The method uses only the connection it is handed, so it is exercised with a
stub `self` rather than a full OHLCEngine (which binds a ZMQ socket and builds an
API client).
"""
import sqlite3

import pytest

import engine as engine_mod
from calculators.segments import NON_VOLUME_SEGMENTS, has_volume


ASSET, SEG = 1333, 'NSE_EQ'


class _StubEngine:
    """Minimal stand-in — carries the database the tests open connections on."""

    def __init__(self, database):
        self.database = database


@pytest.fixture
def eng(tmp_path):
    db = engine_mod.OHLCDatabase(db_path=str(tmp_path / 'ohlc.db'))
    return _StubEngine(db)


def _insert_tick(eng, timestamp, volume, asset_id=ASSET, segment=SEG):
    with eng.database.get_connection() as conn:
        conn.execute(
            "INSERT INTO ohlc_data (timestamp, asset_id, symbol, segment_code, volume) "
            "VALUES (?, ?, ?, ?, ?)",
            (timestamp, asset_id, 'SYM', segment, volume))
        conn.commit()


def _interval(eng, current_volume, tick_timestamp, asset_id=ASSET, segment=SEG):
    """The interval half of `(volume, interval_volume)`."""
    with eng.database.get_connection() as conn:
        return engine_mod.OHLCEngine._calculate_interval_volume(
            eng, conn, asset_id, segment, current_volume, tick_timestamp)[1]


class TestSameDayFilter:
    """The bug: yesterday's cumulative leaking into today's first delta."""

    def test_first_tick_of_session_returns_full_opening_volume(self, eng):
        # Yesterday closed with a cumulative of 50,000,000.
        _insert_tick(eng, '2026-08-20T15:29:55', 50_000_000)

        # Today 09:15:05. The exchange has reset; cumulative reads 4,200,000 —
        # whatever it counted before 09:15, plus the first five seconds.
        result = _interval(eng, 4_200_000, '2026-08-21T09:15:05')

        assert result == 4_200_000, (
            "The first tick of a session must attribute the whole opening "
            "cumulative to this interval. Getting 0 means the same-day filter was "
            "removed: the lookup found yesterday's 50,000,000, the subtraction "
            "went to -45,800,000, and something clamped it. Getting a negative "
            "means the filter was removed and the clamp is (correctly) absent."
        )

    def test_result_is_never_negative_across_a_day_boundary(self, eng):
        _insert_tick(eng, '2026-08-20T15:29:55', 50_000_000)
        result = _interval(eng, 4_200_000, '2026-08-21T09:15:05')
        assert result >= 0, (
            f"got {result} — a negative interval volume is physically impossible "
            f"and means the day-boundary leak is back"
        )

    def test_within_session_delta_is_the_plain_difference(self, eng):
        _insert_tick(eng, '2026-08-21T09:15:05', 4_200_000)
        assert _interval(eng, 4_250_000, '2026-08-21T09:15:10') == 50_000

    def test_five_prior_sessions_present_still_resolves_to_today(self, eng):
        """ohlc.db retains 5 trading days — the filter must ignore all of them."""
        for day, vol in [('17', 41_000_000), ('18', 44_000_000), ('19', 47_000_000),
                         ('20', 50_000_000)]:
            _insert_tick(eng, f'2026-08-{day}T15:29:55', vol)
        assert _interval(eng, 3_100_000, '2026-08-21T09:15:05') == 3_100_000

    def test_no_zero_clamp_in_the_executable_code(self):
        """No `max(…, 0)` call in the function body.

        Inspected via AST, not by scanning source text — the docstring discusses
        the removed clamp in prose, and a text scan matches that instead of code.

        On its own a re-added clamp is harmless while the date filter is present
        (the value never goes negative). The dangerous combination is filter
        removed AND clamp added, which is what silently destroyed the opening
        burst before. `test_first_tick_of_session_returns_full_opening_volume`
        catches that pairing; this catches the clamp arriving on its own, before
        it can mask a later regression.
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(engine_mod.OHLCEngine._calculate_interval_volume)))

        clamps = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == 'max'
            and any(isinstance(a, ast.Constant) and a.value == 0 for a in node.args)
        ]
        assert not clamps, (
            f"{len(clamps)} `max(…, 0)` clamp(s) reappeared in "
            f"_calculate_interval_volume (line(s) {[c.lineno for c in clamps]})"
        )


class TestLateStart:
    """Decided behaviour: discard, because the distribution is unknowable."""

    def test_late_start_discards_the_mornings_volume(self, eng):
        # Engine's first tick of the day is 11:49 (this happened on 2026-08-11).
        result = _interval(eng, 38_000_000, '2026-08-11T11:49:05')
        assert result == 0, (
            "A late start has no same-day predecessor. The morning's accumulated "
            "volume is real, but attributing all of it to this single interval "
            "would be a fabrication — we cannot know how it was spread across the "
            "blocks we missed. Discarding is the decided behaviour."
        )

    def test_boundary_is_the_end_of_block_1(self, eng):
        """A first reading anywhere in block 1, [09:15, 09:30), carries block
        1's whole volume so far. At 09:30:00 it is block 2's reading, and the
        morning's total must not be filed under block 2 — strictly before."""
        assert _interval(eng, 5_000_000, '2026-08-21T09:25:00') == 5_000_000
        assert _interval(eng, 5_000_000, '2026-08-21T09:29:59') == 5_000_000
        assert _interval(eng, 5_000_000, '2026-08-21T09:30:00') == 0

    def test_uses_the_tick_timestamp_not_the_wall_clock(self, eng):
        """The branch must be decidable from the tick alone, so it is testable
        without freezing the clock — it previously read datetime.now()."""
        assert _interval(eng, 7_000_000, '2026-08-21T09:16:00') == 7_000_000


class TestIndicesHaveNoVolume:

    def test_index_returns_none_not_zero(self, eng):
        result = engine_mod.OHLCEngine._calculate_interval_volume(
            eng, None, 13, 'IDX_I', 999_999, '2026-08-21T11:00:00')
        assert result == (None, None), (
            "Indices are not tradeable and have no volume. NULL keeps 'has no "
            "volume' distinguishable from 'zero traded'; 0 does not."
        )

    def test_index_does_not_query_the_database(self, eng):
        """Returns before touching the DB — the connection is None, so any query
        would raise."""
        result = engine_mod.OHLCEngine._calculate_interval_volume(
            eng, None, 25, 'IDX_I', 12_345, '2026-08-21T11:00:00')
        assert result == (None, None)

    def test_segment_helpers(self):
        assert NON_VOLUME_SEGMENTS == frozenset({'IDX_I'})
        assert has_volume('NSE_EQ') is True
        assert has_volume('IDX_I') is False


class TestCompositeKeyIsolation:
    """asset_id 25 is BankNifty in IDX_I and ADANIENT in NSE_EQ.

    Filtering on asset_id alone would diff one entity's volume against the
    other's. The index side now short-circuits, but the equity side must still
    resolve against its own segment only.
    """

    def test_equity_ignores_a_same_id_index_row(self, eng):
        _insert_tick(eng, '2026-08-21T09:15:05', 1_000_000, asset_id=25, segment='NSE_EQ')
        # A stale IDX_I row for the same id, from a database predating this change.
        _insert_tick(eng, '2026-08-21T09:15:06', 999_999_999, asset_id=25, segment='IDX_I')

        result = _interval(eng, 1_050_000, '2026-08-21T09:15:10',
                           asset_id=25, segment='NSE_EQ')
        assert result == 50_000, (
            "ADANIENT's delta must come from ADANIENT's own rows, not from a "
            "BankNifty row that happens to share asset_id 25"
        )
