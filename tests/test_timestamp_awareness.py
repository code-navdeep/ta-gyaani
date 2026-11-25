"""
Every timestamp this system persists is timezone-aware IST.

WHY THIS EXISTS. Before this migration, seven of the eight metrics.db
`timestamp` writers emitted aware ISO strings while `index_metrics` emitted a
naive one:

    momentum          2026-08-31T15:23:26.220936+05:30
    index_metrics     2026-08-31T15:23:26.996517          <- no offset

Observed live 2026-08-31. That produced no wrong number, because query cutoffs
use `created_at` (uniformly naive on both sides at the time) and the only place
the two formats met was the startup hydration:

    hydration_dt = datetime.fromisoformat(max(all_max_ts))

`max()` over a list of mixed-format STRINGS is a lexicographic comparison, so
which table won — and therefore whether the parsed result was aware or naive —
depended on microseconds. `HYDRATION_TIMESTAMP` was non-deterministic in TYPE.
Its only consumer calls `.strftime()`, which accepts both, which is the sole
reason nothing crashed. Any arithmetic on it would have raised
`TypeError: can't subtract offset-naive and offset-aware datetimes` on roughly
half of container starts — a restart-dependent, intermittent failure.

These tests pin the uniformity that removes that class of bug.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from calculators.time_blocks import IST


class TestCanonicalIST:

    def test_fixed_offset_matches_zoneinfo_asia_kolkata(self):
        """The gating check for choosing a fixed offset over ZoneInfo.

        We store a UTC offset suffix in every timestamp, so the two definitions
        must render byte-identically or the migration would change stored data.
        India has observed no DST since 1945, so this holds for every instant.
        """
        zone = ZoneInfo("Asia/Kolkata")
        for month in (1, 4, 7, 10):          # across the year, in case of DST
            probe = datetime(2026, month, 15, 12, 0, 0)
            assert probe.replace(tzinfo=IST).isoformat() == \
                   probe.replace(tzinfo=zone).isoformat(), (
                f"IST and ZoneInfo('Asia/Kolkata') disagree in month {month}"
            )

    def test_ist_renders_the_expected_suffix(self):
        stamp = datetime(2026, 8, 31, 15, 23, 26, 220936, tzinfo=IST).isoformat()
        assert stamp.endswith('+05:30'), stamp

    def test_ist_offset_is_five_thirty(self):
        assert IST.utcoffset(None) == timedelta(hours=5, minutes=30)

    def test_there_is_exactly_one_ist_definition_in_use(self):
        """main.py and every adapter must import IST, not declare their own."""
        import main
        import metrics
        from calculators import (breakout_adapter, momentum_adapter,
                                 price_levels_adapter,
                                 price_volume_divergence_adapter,
                                 relative_strength_adapter,
                                 rolling_momentum_adapter, volume_block_adapter,
                                 volume_dynamics_adapter, vwap_adapter)
        modules = [main, metrics, breakout_adapter, momentum_adapter,
                   price_levels_adapter, price_volume_divergence_adapter,
                   relative_strength_adapter, rolling_momentum_adapter,
                   volume_block_adapter, volume_dynamics_adapter, vwap_adapter]
        for mod in modules:
            local = getattr(mod, 'IST', None)
            assert local is IST, (
                f"{mod.__name__}.IST is {local!r}, not the canonical object from "
                f"calculators.time_blocks. A second definition reintroduces the "
                f"drift this migration removed."
            )


class TestEntryPointModulesImport:
    """Catches signature and constant drift the unit tests would miss.

    Every module below now imports IST rather than declaring one, so a rename
    or a moved constant breaks at import time. Importing them here surfaces
    that in the suite instead of at container boot.
    """

    def test_engine_metrics_dashboard_and_main_import_cleanly(self):
        import dashboard
        import engine
        import main
        import metrics
        for mod in (engine, metrics, dashboard, main):
            assert mod is not None

    def test_entry_points_use_the_canonical_ist(self):
        import engine
        import main
        import metrics
        for mod in (engine, metrics, main):
            assert getattr(mod, 'IST', None) is IST, (
                f"{mod.__name__} does not hold the canonical IST"
            )


class TestAwareNow:
    """`datetime.now(IST)` is the only accepted form for a persisted stamp."""

    def test_aware_now_isoformat_carries_the_offset(self):
        assert datetime.now(IST).isoformat().endswith('+05:30')

    def test_fromisoformat_round_trips_to_an_aware_value(self):
        parsed = datetime.fromisoformat(datetime.now(IST).isoformat())
        assert parsed.tzinfo is not None, (
            "a persisted stamp must parse back aware — this is what makes "
            "HYDRATION_TIMESTAMP's type deterministic"
        )

    def test_mixing_aware_and_naive_still_raises(self):
        """The failure mode being designed out. Kept as a live reminder of why
        uniformity is load-bearing rather than cosmetic."""
        with pytest.raises(TypeError):
            _ = datetime.now() - datetime.now(IST)


class TestTheOriginalDefectIsPinned:
    """index_metrics.timestamp must be aware, like the other eight writers.

    This is THE regression this migration exists to prevent, so it runs the
    real IndexAggregator.compute() rather than asserting a constant. Reverting
    metrics.py's IndexSummary(timestamp=...) to a bare datetime.now() must fail
    this test — verified by mutation.
    """

    def test_index_summary_timestamp_is_timezone_aware(self):
        from metrics import IndexAggregator
        from tests.test_index_breadth import NIFTY, _Loader, _Prices, _latest_metrics

        out = IndexAggregator(_Loader()).compute(_latest_metrics(), _Prices())
        assert NIFTY in out, "aggregator produced no NIFTY summary"

        stamp = out[NIFTY].timestamp
        parsed = datetime.fromisoformat(stamp)
        assert parsed.tzinfo is not None, (
            f"IndexSummary.timestamp is {stamp!r} — naive. This is the exact "
            f"defect observed live on 2026-08-31: seven writers aware, this one "
            f"naive, making HYDRATION_TIMESTAMP's parsed type depend on which "
            f"table won a lexicographic max() by microseconds."
        )
        assert stamp.endswith('+05:30'), stamp
        assert parsed.utcoffset() == timedelta(hours=5, minutes=30)


class TestBreakoutHandlesAwareTicks:
    """_today_ticks must accept the shape ohlc_data actually stores.

    ohlc_data.timestamp is written IST-aware, so pd.to_datetime yields an
    aware Series and an unconditional .dt.tz_localize() raises
    `TypeError: Already tz-aware`. That call sits OUTSIDE _today_ticks'
    try/except, so the exception escapes into the orchestrator's per-calculator
    wrapper and breakout reads as a DEAD calculator on the health bar rather
    than as an error — silent, and easy to misdiagnose.

    Every other breakout test feeds naive strings, which the database no longer
    produces, so this is the only coverage of the realistic shape.
    """

    class _AwareOhlcDb:
        """Returns ticks exactly as ohlc_data now stores them: +05:30 strings."""

        def get_session_prices(self, asset_id, segment_code, now):
            import pandas as pd
            return pd.DataFrame({
                'timestamp': [
                    '2026-08-31T09:16:00.100000+05:30',
                    '2026-08-31T09:20:00.200000+05:30',
                    '2026-08-31T09:44:00.300000+05:30',
                ],
                'last_price': [100.0, 102.0, 101.0],
                'close_price': [100.0, 102.0, 101.0],
            })

    class _NaiveOhlcDb:
        """The pre-migration shape. Must keep working — the helper is meant to
        be format-independent, exactly like the substr() SQL convention."""

        def get_session_prices(self, asset_id, segment_code, now):
            import pandas as pd
            return pd.DataFrame({
                'timestamp': ['2026-08-31T09:16:00.100000',
                              '2026-08-31T09:20:00.200000'],
                'last_price': [100.0, 102.0],
                'close_price': [100.0, 102.0],
            })

    def _run(self, db):
        from calculators.breakout_adapter import _today_ticks
        now = datetime(2026, 8, 31, 11, 0, tzinfo=IST)
        return _today_ticks(db, 11536, 'NSE_EQ', now)

    def test_aware_ticks_do_not_raise_and_stay_aware(self):
        frame = self._run(self._AwareOhlcDb())
        assert not frame.empty, (
            "empty frame means the tz conversion raised and was swallowed — "
            "this is the silent dead-calculator failure"
        )
        assert frame['timestamp'].dt.tz is not None
        assert len(frame) == 3

    def test_naive_ticks_still_work(self):
        frame = self._run(self._NaiveOhlcDb())
        assert not frame.empty
        assert frame['timestamp'].dt.tz is not None, (
            "a naive column must be localised to IST, not left bare"
        )

    def test_both_formats_yield_the_same_instant(self):
        aware = self._run(self._AwareOhlcDb())['timestamp'].iloc[0]
        naive = self._run(self._NaiveOhlcDb())['timestamp'].iloc[0]
        assert aware == naive, (
            f"{aware} != {naive}; the same wall-clock reading must land on the "
            f"same instant regardless of how it was stored"
        )


class TestPersistedWriterStamps:
    """The metrics.db writers all stamp created_at the same way."""

    def test_created_at_helper_produces_an_aware_stamp(self):
        from calculators.metrics_database_manager import IST as MDM_IST
        assert MDM_IST is IST
        assert datetime.now(MDM_IST).isoformat().endswith('+05:30')

    @pytest.mark.parametrize('module_name', [
        'calculators.metrics_database_manager',
        'calculators.historical_database_manager',
        'engine',
    ])
    def test_no_created_at_column_carries_a_sql_default(self, module_name):
        """DEFAULT CURRENT_TIMESTAMP is UTC and space-separated, so a defaulted
        row would sort before every 'T'-separated IST cutoff and stay invisible
        to `WHERE created_at >= ?` forever.

        Matches the COLUMN DEFINITION only — the docstrings in these modules
        discuss the clause by name, and prose must not trip this.
        """
        import importlib
        import inspect
        import re

        src = inspect.getsource(importlib.import_module(module_name))
        offenders = re.findall(
            r'created_at\s+\w+\s+DEFAULT\s+CURRENT_TIMESTAMP', src)
        assert not offenders, (
            f"{module_name} reintroduced a created_at SQL default: {offenders}. "
            f"It writes a UTC, space-separated value no cutoff can match."
        )


class TestLexicographicOrderingSurvives:
    """Cutoffs are compared as SQLite TEXT, so ordering must hold on strings.

    Aware ISO cutoffs are compared against the timestamp column — by
    momentum_adapter and relative_strength_adapter via get_intraday_data_range,
    and for breakout_adapter and price_levels_adapter by the reader's in-memory
    store of today's readings (get_session_prices). Those comparisons
    are lexicographic, and they work because '+' (0x2B) sorts before '.'
    (0x2E) — a whole-second cutoff sorts before any same-second row carrying
    microseconds. That is load-bearing, so it is pinned here rather than left
    as folklore.
    """

    def test_whole_second_cutoff_sorts_before_same_second_microseconds(self):
        cutoff = '2026-08-31T09:15:00+05:30'
        row = '2026-08-31T09:15:00.123456+05:30'
        assert cutoff < row, (
            "a 09:15:00 cutoff must include the 09:15:00.123456 row; if '+' "
            "stopped sorting before '.', the opening tick would be dropped"
        )

    def test_ordering_is_chronological_within_one_format(self):
        earlier = '2026-08-31T09:15:00.000000+05:30'
        later = '2026-08-31T15:30:00.000000+05:30'
        assert earlier < later

    def test_date_prefix_dominates_the_offset_suffix(self):
        assert '2026-08-30T23:59:59.999999+05:30' < '2026-08-31T00:00:00.000000+05:30'
