"""
Indices carry no volume, end to end — plus the Volume Dynamics handover.

An index is a computed number, not a tradeable instrument, so it has no traded
volume ever. The rule is applied at five sites and each is tested here, because
a half-applied rule is worse than none: if the engine stores NULL but
`_store_time_block_volumes` still writes a row, `SUM(interval_volume)` returns
NULL, the existing `COALESCE(…, 0)` turns it into 0, and Volume Block z-scores a
constant-zero series whose sigma is 0.

Also covers `run_volume_dynamics_analysis`, which was rewritten wholesale when
Volume Dynamics gained tiers and had no test at all.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import engine as engine_mod
from calculators.segments import NON_VOLUME_SEGMENTS, has_volume
from calculators.statistics import TIERS
from calculators.volume_dynamics_adapter import run_volume_dynamics_analysis

IST = ZoneInfo("Asia/Kolkata")


class _Loader:
    """Minimal AssetLoader stand-in — adapters read only `.assets_df`."""

    def __init__(self, rows):
        self.assets_df = pd.DataFrame(rows)


class _StubEngine:
    def __init__(self, database, asset_loader):
        self.database = database
        self.asset_loader = asset_loader


@pytest.fixture
def eng(tmp_path):
    db = engine_mod.OHLCDatabase(db_path=str(tmp_path / 'ohlc.db'))
    loader = _Loader([
        {'asset_id': 1333, 'segment_code': 'NSE_EQ', 'asset_name': 'RELIANCE'},
        {'asset_id': 13,   'segment_code': 'IDX_I',  'asset_name': 'NIFTY'},
    ])
    return _StubEngine(db, loader)


class TestSegmentRule:

    def test_only_idx_i_is_volume_free(self):
        assert NON_VOLUME_SEGMENTS == frozenset({'IDX_I'})
        assert has_volume('NSE_EQ') and not has_volume('IDX_I')

    @pytest.mark.parametrize('missing', [None, float('nan'), ''])
    def test_a_missing_segment_is_refused_not_answered(self, missing):
        """It used to answer True — "has volume" — for a segment it knew nothing
        about (a blank CSV cell arrives as NaN)."""
        with pytest.raises(ValueError):
            has_volume(missing)

    def test_rule_lives_in_one_place(self):
        """Five sites consult this. Scattered `== 'IDX_I'` checks are how the
        dashboard came to believe the engine filtered index volume when it did
        not — the comment said so for as long as it was false."""
        import ast
        import pathlib
        offenders = []
        for f in ['engine.py', 'metrics.py', 'calculators/volume_block_adapter.py',
                  'calculators/volume_dynamics_adapter.py']:
            # Relative to the project folder, so this runs from any directory.
            tree = ast.parse((pathlib.Path(__file__).resolve().parent.parent / f).read_text())
            for n in ast.walk(tree):
                if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name):
                    for c in n.comparators:
                        if isinstance(c, ast.Constant) and c.value == 'IDX_I':
                            offenders.append(f"{f}:{n.lineno}")
        assert not offenders, (
            f"literal 'IDX_I' comparison(s) at {offenders} — use "
            f"NON_VOLUME_SEGMENTS / has_volume() from calculators.segments"
        )


class TestEngineStoresNullForIndices:

    def test_interval_volume_is_none_not_zero(self, eng):
        result = engine_mod.OHLCEngine._calculate_interval_volume(
            eng, None, 13, 'IDX_I', 999_999, '2026-08-21T11:00:00')
        assert result == (None, None), (
            "0 would be indistinguishable from 'zero traded'; NULL says "
            "'this instrument has no volume'"
        )

    def test_equity_still_gets_a_number(self, eng):
        with eng.database.get_connection() as conn:
            conn.execute(
                "INSERT INTO ohlc_data (timestamp, asset_id, symbol, segment_code, volume) "
                "VALUES (?, ?, ?, ?, ?)",
                ('2026-08-21T09:15:05', 1333, 'RELIANCE', 'NSE_EQ', 4_200_000))
            conn.commit()
            result = engine_mod.OHLCEngine._calculate_interval_volume(
                eng, conn, 1333, 'NSE_EQ', 4_250_000, '2026-08-21T09:15:10')
        assert result == (4_250_000, 50_000)

    def test_ohlc_data_columns_accept_null(self, eng):
        """The rule needs NULL to be storable — both columns are
        `INTEGER DEFAULT 0`, which permits NULL (DEFAULT is not NOT NULL)."""
        with eng.database.get_connection() as conn:
            conn.execute(
                "INSERT INTO ohlc_data (timestamp, asset_id, symbol, segment_code, "
                "volume, interval_volume) VALUES (?, ?, ?, ?, ?, ?)",
                ('2026-08-21T11:00:00', 13, 'NIFTY', 'IDX_I', None, None))
            conn.commit()
            row = conn.execute(
                "SELECT volume, interval_volume FROM ohlc_data WHERE segment_code='IDX_I'"
            ).fetchone()
        assert row[0] is None and row[1] is None, "index volume must persist as NULL, not 0"


class TestNoBlockVolumeRowForIndices:

    def test_store_time_block_volumes_skips_indices(self, eng):
        """Load-bearing, not cosmetic. interval_volume is NULL for indices, so
        SUM() returns NULL and the existing COALESCE(…, 0) would write a row of
        ZEROS — and Volume Block would then z-score a constant-zero series."""
        with eng.database.get_connection() as conn:
            for aid, seg, sym, vol in [(1333, 'NSE_EQ', 'RELIANCE', 1000),
                                       (13, 'IDX_I', 'NIFTY', None)]:
                conn.execute(
                    "INSERT INTO ohlc_data (timestamp, asset_id, symbol, segment_code, "
                    "volume, interval_volume) VALUES (?, ?, ?, ?, ?, ?)",
                    ('2026-08-21T09:20:00', aid, sym, seg, vol, vol))
            conn.commit()

        engine_mod.OHLCEngine._store_time_block_volumes(
            eng, '2026-08-21', 1, datetime(2026, 8, 21, 9, 30, tzinfo=IST))

        with eng.database.get_connection() as conn:
            segs = [r[0] for r in conn.execute(
                "SELECT segment_code FROM time_block_volumes")]
        assert 'IDX_I' not in segs, (
            f"an index got a time_block_volumes row ({segs}) — it would be a row "
            f"of zeros, which is exactly what the decision rejected"
        )
        assert 'NSE_EQ' in segs, "the equity row must still be written"


class TestPriceVolumeDivergenceHasNoZeroFill:
    """PV Divergence used to fill a missing interval_volume with 0.0 — turning an
    index's NULL volume into a constant-zero series, and (once a failed volume
    lookup stores NULL) a stock's missing reading into a fabricated zero. Indices
    now get their status row directly; a stock tick needs a positive price and
    a recorded volume to count.

    And the stretch must run without a gap over 30 s (time_blocks.
    BLOCK_EDGE_TOLERANCE), never counting the day's first reading: either puts
    far more than five seconds of trading into one interval."""

    NOW = datetime(2026, 8, 21, 11, 0, tzinfo=IST)       # window 10:45:00 - 11:00:00
    RELIANCE = {'asset_id': 1333, 'segment_code': 'NSE_EQ', 'asset_name': 'RELIANCE'}

    class _Ohlc:
        def __init__(self, ticks, day_first='2026-08-21T09:15:02+05:30'):
            self.ticks = ticks
            self.day_first = day_first
            self.reads = []

        def get_intraday_data_range(self, asset_id, start, end, segment_code=None):
            self.reads.append((asset_id, segment_code))
            return self.ticks

        def session_first_reading(self, asset_id, segment_code, day):
            return self.day_first

    class _Hist:
        def get_historical_intraday_metrics(self, *args, **kwargs):
            return pd.DataFrame()

    def _run(self, ohlc, rows):
        from calculators.price_volume_divergence_adapter import (
            run_price_volume_divergence_analysis)
        return run_price_volume_divergence_analysis(
            ohlc, self._Hist(), asset_loader=_Loader(rows), now=self.NOW)

    @staticmethod
    def _tick(seconds, price, interval_volume):
        """A reading `seconds` after 10:44:58, with a running total."""
        stamp = datetime(2026, 8, 21, 10, 44, 58, tzinfo=IST) + timedelta(seconds=seconds)
        return {'timestamp': stamp.isoformat(), 'last_price': price,
                'volume': 50_000 + seconds, 'interval_volume': interval_volume}

    def _readings(self, count, gap_before=None):
        """A lead-in at 10:44:58 — before the window, seen only as the reading
        before the first counted one — then `count` readings 5 s apart from
        10:45:03 (45 s before reading `gap_before`), prices and volumes varying."""
        out, seconds = [self._tick(0, 100.0, 300)], 0
        for i in range(count):
            seconds += 45 if i == gap_before else 5
            out.append(self._tick(seconds, 100.0 + 0.1 * i * (-1) ** i, 1000 + 97 * i))
        return out

    def test_an_index_gets_its_status_row_without_reading_ticks(self):
        ohlc = self._Ohlc(pd.DataFrame())
        rows = self._run(ohlc, [{'asset_id': 13, 'segment_code': 'IDX_I', 'asset_name': 'NIFTY'}])
        assert [r.status for r in rows] == ['no_volume_variation']
        assert ohlc.reads == []

    def test_a_zero_price_and_a_missing_volume_are_left_out(self):
        bad = [self._tick(55, 0.0, 500), self._tick(60, 101.0, None)]
        rows = self._run(self._Ohlc(pd.DataFrame(self._readings(10) + bad)), [self.RELIANCE])
        assert rows[0].sample_size == 10 - 1

    def test_a_gap_over_30_s_gives_an_interrupted_row(self):
        rows = self._run(self._Ohlc(pd.DataFrame(self._readings(10, gap_before=5))),
                         [self.RELIANCE])
        assert rows[0].status == 'interrupted'
        assert (rows[0].pearson_r, rows[0].pvd_z_22d) == (None, None)

    def test_the_days_first_reading_is_left_out_not_read_as_a_gap(self):
        """A late start: the day's first reading, 10:45:03, opens the window with
        nothing before it. It holds the running total so far — left out — and
        the reading after it is measured from it."""
        readings = self._readings(10)[1:]
        ohlc = self._Ohlc(pd.DataFrame(readings), day_first=readings[0]['timestamp'])
        rows = self._run(ohlc, [self.RELIANCE])
        assert rows[0].status != 'interrupted'
        assert rows[0].sample_size == 9 - 1

    def test_a_stock_with_too_few_readings_gets_a_row_saying_so(self):
        """No reading in the window, or a single one: a row with status
        'insufficient_data' — not a skipped stock, which would read as a
        stopped calculator on the health bar."""
        for ticks in (pd.DataFrame(), pd.DataFrame(self._readings(1))):
            rows = self._run(self._Ohlc(ticks), [self.RELIANCE])
            assert [r.status for r in rows] == ['insufficient_data']
            assert rows[0].pearson_r is None


class TestVolumeDynamicsHandover:
    """`run_volume_dynamics_analysis` was rewritten wholesale for tiers and had
    no test. It now reads NO database — the ZQ series arrives in memory from
    Volume Block at all four tiers."""

    LOADER = _Loader([{'asset_id': 1333, 'segment_code': 'NSE_EQ', 'asset_name': 'RELIANCE'}])
    NOW = datetime(2026, 8, 21, 11, 0, tzinfo=IST)

    @staticmethod
    def _history(current, priors):
        return {(1333, 'NSE_EQ'): {n: (current, list(priors)) for n in TIERS}}

    def test_velocity_and_acceleration_match_the_definitions(self):
        # priors chronological: [t-3, t-2, t-1]
        rows = run_volume_dynamics_analysis(
            self._history(0.4, [2.9, 2.8, 2.5]), asset_loader=self.LOADER, now=self.NOW)
        assert len(rows) == 1
        r = rows[0]
        assert r.velocity_90d == pytest.approx(0.4 - 2.5)              # ZQ_t - ZQ_(t-1)
        assert r.acceleration_90d == pytest.approx((0.4 - 2.5) - (2.5 - 2.8))
        assert r.persistence_90d == pytest.approx((2.8 + 2.5 + 0.4) / 3)

    def test_collapsing_volume_stays_negative(self):
        r = run_volume_dynamics_analysis(
            self._history(0.4, [2.9, 2.8, 2.5]), asset_loader=self.LOADER, now=self.NOW)[0]
        assert r.velocity_90d < 0 and r.acceleration_90d < 0
        assert r.expansion_state_90d == -2, "velocity<0 and acceleration<0 is 'collapsing'"

    def test_tiers_are_independent(self):
        """Each tier is computed from its own ZQ series. A shared value across
        tiers would hide exactly the divergence tiering exists to expose."""
        hist = {(1333, 'NSE_EQ'): {
            22: (2.0, [0.1, 0.2, 0.3]),
            90: (-1.0, [0.1, 0.2, 0.3]),
            180: (0.5, [0.4, 0.45, 0.5]),
            252: (None, []),
        }}
        r = run_volume_dynamics_analysis(hist, asset_loader=self.LOADER, now=self.NOW)[0]
        assert r.velocity_22d == pytest.approx(2.0 - 0.3)
        assert r.velocity_90d == pytest.approx(-1.0 - 0.3)
        assert r.velocity_180d == pytest.approx(0.5 - 0.5)
        assert r.velocity_22d != r.velocity_90d

    def test_absent_tier_is_absent_not_neutral(self):
        """A tier with no ZQ has nothing to differentiate. expansion_state must
        be None — 0 means 'neutral', a real reading, so defaulting to it would
        render an absence as the claim 'volume is steady'."""
        hist = {(1333, 'NSE_EQ'): {22: (1.0, [0.5]), 90: (None, []),
                                   180: (None, []), 252: (None, [])}}
        r = run_volume_dynamics_analysis(hist, asset_loader=self.LOADER, now=self.NOW)[0]
        assert r.velocity_22d is not None
        for tier in ('90d', '180d', '252d'):
            assert getattr(r, f'volume_z_{tier}') is None
            assert getattr(r, f'expansion_state_{tier}') is None, (
                f"expansion_state_{tier} must be None, not 0 — 0 is 'neutral'"
            )

    def test_no_priors_yields_no_velocity_but_still_a_row(self):
        """First completed block of the day: a ZQ exists, but nothing to
        difference it against."""
        r = run_volume_dynamics_analysis(
            self._history(1.2, []), asset_loader=self.LOADER, now=self.NOW)[0]
        assert r.volume_z_90d == pytest.approx(1.2)
        assert r.velocity_90d is None and r.acceleration_90d is None
        assert r.expansion_state_90d is None, (
            "with no earlier block the state is unknown — 0 would claim 'neutral'"
        )

    def test_index_absent_from_the_handover_yields_no_row(self):
        """Volume Block emits no row for an index, so it never appears in
        zq_history, so Volume Dynamics emits nothing for it."""
        loader = _Loader([
            {'asset_id': 1333, 'segment_code': 'NSE_EQ', 'asset_name': 'RELIANCE'},
            {'asset_id': 13, 'segment_code': 'IDX_I', 'asset_name': 'NIFTY'},
        ])
        rows = run_volume_dynamics_analysis(
            self._history(0.4, [2.9, 2.8, 2.5]), asset_loader=loader, now=self.NOW)
        assert [r.segment_code for r in rows] == ['NSE_EQ']

    def test_empty_handover_returns_nothing(self):
        assert run_volume_dynamics_analysis({}, asset_loader=self.LOADER, now=self.NOW) == []

    def test_before_the_first_block_returns_nothing(self):
        assert run_volume_dynamics_analysis(
            self._history(1.0, []), asset_loader=self.LOADER,
            now=datetime(2026, 8, 21, 9, 20, tzinfo=IST)) == []
