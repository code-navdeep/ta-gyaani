"""
Block-boundary-aligned cache TTL.

`HistoricalDatabaseReader` cached every query for a flat 60 seconds. Two of its
methods read tables that only gain rows at 15-minute block boundaries, so a flat
TTL expires on a schedule with no relationship to when the data actually
changes: cache at 10:07, the 10:15 boundary writes a fresh row, and the entry
survives until its own 60 s elapses — the new block's z-score arrives minutes
late, every block, all session.

Those two now expire AT the boundary instead:

    ttl = seconds_until_next_block_end(now) + 45

The +45 is load-bearing, and the direction of the error matters. Expiring
exactly at 10:15:00 would be WORSE than the flat TTL, not better: the re-query
fires before `_maybe_write_intraday_block` has written the row (that write
happens on the first metrics cycle after the block's readings end; cycles run
~34 s apart), gets the stale answer, and caches THAT under a new block-aligned
TTL lasting until the next boundary — ~15 minutes of staleness instead of 60
seconds.

So the invariant is one-sided: an entry may expire late, never early.
"""
from datetime import datetime, timedelta

import pytest

from calculators.historical_database_manager import (
    _BLOCK_TTL_EPSILON_SECONDS,
    _block_aligned_cache_ttl,
)
from calculators.time_blocks import (
    BLOCK_COUNT,
    block_end_time,
    seconds_until_next_block_end,
)

_DAY = datetime(2026, 8, 22)          # any weekday; only the clock is used
_METRICS_CYCLE_SECONDS = 30           # MetricsOrchestrator's loop interval


def _at(h, m, s=0):
    return _DAY.replace(hour=h, minute=m, second=s)


# --------------------------------------------------------------------------- #
# The one-sided invariant
# --------------------------------------------------------------------------- #

class TestNeverExpiresEarly:

    @pytest.mark.parametrize('block', range(1, BLOCK_COUNT + 1))
    def test_entry_cached_just_before_a_boundary_outlives_it(self, block):
        """One second before each of the 25 boundaries — the worst case for a
        naive implementation, and the moment a flat TTL is most likely to
        straddle the write."""
        end = block_end_time(block)
        now = _DAY.replace(hour=end.hour, minute=end.minute, second=0) - timedelta(seconds=1)
        ttl = _block_aligned_cache_ttl(now)
        assert ttl is not None
        expires_at = now + timedelta(seconds=ttl)
        boundary = _DAY.replace(hour=end.hour, minute=end.minute, second=0)
        assert expires_at > boundary, (
            f"block {block}: entry expires at {expires_at.time()}, at or before "
            f"the {boundary.time()} boundary — it would re-query before the row "
            f"is written and cache the stale answer for a full block"
        )

    def test_expiry_clears_the_write_by_at_least_one_metrics_cycle(self):
        """The row lands on the first 30-second cycle after the boundary, so
        the grace must cover a whole cycle — not merely a moment past it."""
        assert _BLOCK_TTL_EPSILON_SECONDS >= _METRICS_CYCLE_SECONDS

        for h, m in ((9, 29), (10, 14), (12, 59), (15, 14)):
            now = _at(h, m, 59)
            # Boundary DERIVED, not hand-incremented: `m + 1` rolls wrong at
            # m == 59, which made the 12:59:59 case assert against 12:00 and
            # pass for the wrong reason.
            boundary = now + timedelta(seconds=seconds_until_next_block_end(now))
            expires_at = now + timedelta(seconds=_block_aligned_cache_ttl(now))
            assert (expires_at - boundary).total_seconds() >= _METRICS_CYCLE_SECONDS

    @pytest.mark.parametrize('h,m,s', [
        (9, 15, 0), (9, 16, 30), (9, 29, 59), (9, 30, 0), (9, 30, 1),
        (10, 7, 0), (11, 44, 59), (13, 0, 0), (15, 14, 59), (15, 29, 59),
    ])
    def test_ttl_is_exactly_remaining_plus_epsilon(self, h, m, s):
        now = _at(h, m, s)
        assert _block_aligned_cache_ttl(now) == pytest.approx(
            seconds_until_next_block_end(now) + _BLOCK_TTL_EPSILON_SECONDS
        )

    def test_ttl_is_always_positive_during_the_session(self):
        """A zero or negative TTL would make every call a cache miss —
        converting the cache into a no-op that shows up only as latency."""
        now = _at(9, 15)
        end = _at(15, 30)
        while now < end:
            assert _block_aligned_cache_ttl(now) > 0
            now += timedelta(seconds=37)     # co-prime with the grid, so this
                                             # lands inside blocks and on edges


# --------------------------------------------------------------------------- #
# After the close there is no next boundary
# --------------------------------------------------------------------------- #

class TestAfterFinalBlock:

    @pytest.mark.parametrize('h,m', [(15, 30), (15, 31), (16, 0), (20, 0), (23, 59)])
    def test_returns_none_so_the_caller_falls_back_to_the_flat_default(self, h, m):
        """Not a bug and not zero: there is no next boundary today, and caching
        until tomorrow's 09:30 would serve a stale frame across the EOD
        archival that rewrites exactly these tables."""
        assert _block_aligned_cache_ttl(_at(h, m)) is None

    @pytest.mark.parametrize('h,m', [(0, 0), (6, 0), (9, 0), (9, 14)])
    def test_before_the_open_the_first_boundary_is_still_ahead(self, h, m):
        ttl = _block_aligned_cache_ttl(_at(h, m))
        assert ttl is not None and ttl > 0


# --------------------------------------------------------------------------- #
# The flag must not reach the cache key
# --------------------------------------------------------------------------- #

class _FakeCache:
    """Records the (key-args, ttl) of every set, and serves gets from them."""

    def __init__(self):
        self.sets = []
        self.store = {}

    def _key(self, method_name, *args, **kwargs):
        return (method_name, args, tuple(sorted(kwargs.items())))

    def get(self, method_name, *args, **kwargs):
        return self.store.get(self._key(method_name, *args, **kwargs))

    def set(self, method_name, data, ttl=None, *args, **kwargs):
        self.sets.append({'key': self._key(method_name, *args, **kwargs), 'ttl': ttl})
        self.store[self._key(method_name, *args, **kwargs)] = data


class _Reader:
    """Just the _with_cache machinery, without a database."""

    def __init__(self):
        self.cache_enabled = True
        self.cache = _FakeCache()

    from calculators.historical_database_manager import HistoricalDatabaseReader as _H
    _with_cache = _H._with_cache


class TestFlagDoesNotLeakIntoTheKey:

    def test_aligned_and_unaligned_calls_share_one_cache_entry(self):
        """If `_align_to_block` reached **kwargs it would become part of the
        cache key, splitting one logical entry into a separate copy per flag
        value — every call a miss. That failure is invisible except as
        latency, which is why it is asserted rather than assumed.
        """
        r = _Reader()
        calls = []

        def q(*a, **k):
            calls.append(1)
            return 'RESULT'

        r._with_cache('m', q, 1, 2, _align_to_block=True)
        assert len(calls) == 1
        # Same positional args, flag flipped — must HIT the existing entry.
        r._with_cache('m', q, 1, 2, _align_to_block=False)
        assert len(calls) == 1, "the flag fragmented the cache key"

        keys = {s['key'] for s in r.cache.sets}
        assert len(keys) == 1
        assert not any('_align_to_block' in str(k) for k in keys)

    def test_aligned_call_stores_a_block_ttl_and_plain_call_stores_none(self):
        r = _Reader()
        r._with_cache('a', lambda *a, **k: 1, _align_to_block=True)
        r._with_cache('b', lambda *a, **k: 1)

        by_method = {s['key'][0]: s['ttl'] for s in r.cache.sets}
        # 'b' takes the cache's flat default, signalled by None.
        assert by_method['b'] is None
        # 'a' is either a real block TTL, or None after 15:30 — both correct,
        # and which one depends on when the suite runs.
        assert by_method['a'] is None or by_method['a'] > 0

    def test_cache_disabled_still_accepts_the_flag(self):
        """The flag must not become a required-cache assumption — enable_cache
        is a constructor option."""
        r = _Reader()
        r.cache_enabled = False
        r.cache = None
        assert r._with_cache('m', lambda *a, **k: 'X', _align_to_block=True) == 'X'


# --------------------------------------------------------------------------- #
# Scope: only the two block-driven methods opt in
# --------------------------------------------------------------------------- #

class TestScope:

    def test_exactly_the_two_block_history_methods_align(self):
        """Every other cached method keeps the flat TTL. Aligning, say,
        get_beta_cache would be wrong: it changes once a day at EOD, not at
        block boundaries."""
        import inspect
        from calculators import historical_database_manager as H

        src = inspect.getsource(H.HistoricalDatabaseReader)
        aligned = [ln.strip() for ln in src.splitlines() if '_align_to_block=True' in ln]
        assert len(aligned) == 2, f"expected 2 aligned call sites, found {len(aligned)}"

        # Confirm which two, by locating the flag inside each method's source.
        for name in ('get_historical_time_block_volumes', 'get_historical_intraday_metrics'):
            method_src = inspect.getsource(getattr(H.HistoricalDatabaseReader, name))
            assert '_align_to_block=True' in method_src, f"{name} is not aligned"

        for name in ('get_previous_close_data', 'get_historical_gap_data',
                     'get_beta_cache', 'get_daily_price_history'):
            method_src = inspect.getsource(getattr(H.HistoricalDatabaseReader, name))
            assert '_align_to_block' not in method_src, (
                f"{name} should keep the flat TTL — its data does not change "
                f"at block boundaries"
            )
