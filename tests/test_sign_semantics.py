"""
Sign-semantics guard — which calculator outputs can legitimately be negative.

This file IS the source of truth: it drives every calculator with inputs that
must produce negatives and checks that they do.

**Negative is the normal case across nearly this entire system.** The only
quantities that cannot go negative are raw volumes, standard deviations, sample
counts, and `abs()`-derived magnitudes. Everything else is signed, including
several that look like they should not be:

  - `beta` is negative for an inverse-correlated stock
  - RS `acceptance` is a PRODUCT, so its sign IS the signal (negative = RS just
    flipped direction)
  - `velocity` / `acceleration` / `expansion_state` go negative when volume
    participation is COLLAPSING, which matters as much as expansion
  - `estimator_agreement` is negative when all three PVD estimators agree the
    correlation is negative

Nothing else in the codebase stops someone adding an `abs()`, a `max(x, 0)`, or
an `if v < 0: return 0` to any of these. Every such change would be SILENT — a
suppressed negative reads as an ordinary number. Worse, clamping does not merely
hide a bug: ten fabricated zeros in a 180-sample log-volume window inflated
sigma 9.9x and turned a genuine z=+4.83 into z=+0.73 — no highlight, on the
most tradeable reading in the window.

Each calculator below is driven with inputs chosen to force negatives. If a
clamp is ever reintroduced anywhere, the matching assertion fails.
"""
import ast
import dataclasses
import pathlib

import pytest

from calculators.breakout_calculator import calculate_breakout_state
from calculators.momentum_calculator import calculate_momentum_state
from calculators.price_alerts_calculator import PriceLevel, evaluate_price_level
from calculators.price_volume_divergence_calculator import calculate_pvd_v3
from calculators.relative_strength_calculator import (
    calculate_acceptance, calculate_beta,
    calculate_intraday_relative_strength, calculate_session_relative_strength,
)
from calculators.rolling_momentum_calculator import calculate_rolling_momentum_state
from calculators.volume_block import calculate_trade_volume_state
from calculators.volume_dynamics import calculate_volume_dynamics
from calculators.vwap_deviation_calculator import calculate_vwap_state


# The project folder: source scans read files relative to it, so the suite
# passes from any working directory, not only the project root.
_ROOT = pathlib.Path(__file__).resolve().parent.parent

# A rising history, so a falling current value z-scores negative.
_RISING = [0.001 * i for i in range(120)]


def _assert_negative(result, fields, label):
    for f in fields:
        v = getattr(result, f)
        assert v is not None, f"{label}.{f} is None — the scenario failed to produce a value"
        assert v < 0, (
            f"{label}.{f} = {v!r}, expected NEGATIVE. This field is SIGNED BY "
            f"DESIGN — a non-negative value here means a clamp, an abs(), or a "
            f"sign-suppressing guard was introduced. The only non-negative "
            f"quantities in this system are raw volumes, standard deviations, "
            f"sample counts and abs()-derived magnitudes; returns, z-scores, "
            f"betas, velocities, accelerations and correlations are all signed."
        )


def _assert_non_negative(result, fields, label):
    for f in fields:
        v = getattr(result, f)
        if v is None:
            continue
        assert v >= 0, f"{label}.{f} = {v!r}, expected >= 0 — this quantity cannot be negative"


class TestMomentum:

    @staticmethod
    def _gap_down():
        return calculate_momentum_state(
            open_price=95.0, previous_close=100.0,      # -5% overnight gap
            price_after_window=93.0,                     # and it keeps falling
            previous_early_return=0.02,                  # yesterday it rose
            previous_gap_history=_RISING, previous_early_history=_RISING)

    def test_gap_down_produces_negative_fields(self):
        _assert_negative(self._gap_down(), ['gap_return', 'gap_direction',
                                            'early_return', 'acceleration'], 'Momentum')

    def test_both_bundles_negative_at_every_populated_tier(self):
        r = self._gap_down()
        for label, bundle in (('gap', r.gap_tiers),
                              ('early_return', r.early_return_tiers)):
            populated = {n: t for n, t in bundle.items() if t[0] is not None}
            assert populated, f"{label} produced no populated tier"
            for n, (z, _mu, _sd, _av) in populated.items():
                assert z < 0, f"{label} tier {n}d z={z!r}, expected negative"

    def test_tier_sigma_never_negative(self):
        r = self._gap_down()
        for bundle in (r.gap_tiers, r.early_return_tiers):
            for n, (_z, _mu, sd, _av) in bundle.items():
                if sd is not None:
                    assert sd >= 0, f"tier {n}d sigma={sd!r} cannot be negative"

    def test_continuation_negative_when_the_gap_fades(self):
        """continuation = gap_direction x early_return. It is negative when the
        post-open move FIGHTS the gap — the 'gap is fading' read, which is the
        whole reason the field exists."""
        r = calculate_momentum_state(
            open_price=102.0, previous_close=100.0,      # gapped UP
            price_after_window=99.0,                      # then gave it all back
            previous_early_return=0.0,
            previous_gap_history=_RISING, previous_early_history=_RISING)
        assert r.gap_direction == 1
        assert r.early_return < 0
        assert r.continuation < 0, "a faded gap must read negative"


class TestBreakout:

    def test_below_range_produces_negative_position_and_direction(self):
        r = calculate_breakout_state(
            price=90.0, opening_high=105.0, opening_low=98.0, previous_price=99.0,
            prices_after_breakout=[97.0, 94.0, 90.0], breakout_direction=-1)
        _assert_negative(r, ['breakout_position', 'breakout_direction'], 'Breakout')

    def test_below_range_now_is_negative(self):
        """breakout_now: -1 when this reading and the one before are both below
        the range low."""
        r = calculate_breakout_state(
            price=90.0, opening_high=105.0, opening_low=98.0, previous_price=94.0,
            prices_after_breakout=[97.0, 94.0, 90.0], breakout_direction=-1)
        _assert_negative(r, ['breakout_now'], 'Breakout')

    def test_width_and_distance_stay_non_negative(self):
        """range_width is ln(high/low) with high >= low; distance_from_range is
        an abs(). Both are magnitudes — the direction is carried separately by
        breakout_position's sign."""
        r = calculate_breakout_state(
            price=90.0, opening_high=105.0, opening_low=98.0, previous_price=99.0,
            prices_after_breakout=[97.0, 94.0, 90.0], breakout_direction=-1)
        _assert_non_negative(r, ['range_width', 'distance_from_range'], 'Breakout')


class TestVwap:

    def test_below_vwap_and_reverting(self):
        r = calculate_vwap_state(
            price=97.0, vwap=100.0,                # below VWAP
            previous_vwap_displacement=-0.05,      # was further below -> reverting
            previous_same_time_vwap_history=_RISING)
        _assert_negative(r, ['displacement', 'side', 'acceptance'], 'VWAP')

    def test_displacement_z_negative_at_every_populated_tier(self):
        r = calculate_vwap_state(
            price=97.0, vwap=100.0, previous_vwap_displacement=-0.05,
            previous_same_time_vwap_history=_RISING)
        populated = {n: t for n, t in r.displacement_tiers.items() if t[0] is not None}
        assert populated, "scenario produced no populated tier"
        for n, (z, _mu, _sd, _av) in populated.items():
            assert z < 0, f"vwap tier {n}d z={z!r}, expected negative below a rising VWAP history"

    def test_tier_sigma_never_negative(self):
        r = calculate_vwap_state(
            price=97.0, vwap=100.0, previous_vwap_displacement=-0.05,
            previous_same_time_vwap_history=_RISING)
        for n, (_z, _mu, sd, _av) in r.displacement_tiers.items():
            if sd is not None:
                assert sd >= 0, f"tier {n}d sigma={sd!r} — a standard deviation cannot be negative"

    # `absolute_vwap_z` was removed along with its test. It was a
    # dead field: computed as abs(z_90d) for all 82 assets every cycle and read
    # by nothing — vwap_adapter takes only displacement, side, acceptance,
    # status and displacement_tiers off the Result.


class TestRelativeStrength:

    def test_underperformance_is_negative(self):
        rs = calculate_session_relative_strength(
            stock_open=99.0, stock_previous_close=100.0,
            benchmark_open=101.0, benchmark_previous_close=100.0, beta=1.2)
        assert rs < 0, "a stock down while its benchmark is up must read negative"

    def test_intraday_underperformance_is_negative(self):
        rs = calculate_intraday_relative_strength(
            stock_current=98.0, stock_previous=99.0,
            benchmark_current=102.0, benchmark_previous=101.0, beta=1.2)
        assert rs < 0

    def test_acceptance_is_a_product_so_its_sign_is_the_signal(self):
        """acceptance = previous_rs * current_rs. Negative means RS FLIPPED
        direction between readings — that sign change is the entire point of
        the field, not an anomaly to suppress."""
        assert calculate_acceptance(previous_rs=0.002, current_rs=-0.001) < 0
        assert calculate_acceptance(previous_rs=0.002, current_rs=0.003) > 0

    def test_beta_is_negative_for_an_inverse_correlated_stock(self):
        """A genuinely inverse-correlated pair has negative beta. This is real,
        not an error — clamping it to 0 would silently turn RS into raw return
        for exactly the stocks where beta-adjustment matters most."""
        bench = [0.01, -0.02, 0.015, -0.005, 0.02, -0.01] * 5
        stock = [-0.9 * b for b in bench]
        beta = calculate_beta(stock, bench)
        assert beta == pytest.approx(-0.9)


class TestVolumeBlock:
    """The z-scores are signed; the raw volumes are not."""

    @staticmethod
    def _quiet_block():
        # History centred well above the current block, so z goes negative.
        history = [1_000_000.0 + 5_000 * i for i in range(120)]
        return calculate_trade_volume_state(
            block_volume=250_000.0,                      # unusually QUIET
            previous_same_block_volumes=history,
            completed_blocks_today=[250_000.0],
            previous_session_cumulative_volumes=history)

    def test_quiet_block_z_scores_negative_at_every_populated_tier(self):
        r = self._quiet_block()
        populated = {n: t for n, t in r.block_volume_tiers.items() if t[0] is not None}
        assert populated, "scenario produced no populated tier"
        for n, (z, _mu, _sd, _avail) in populated.items():
            assert z < 0, f"block_volume tier {n}d z={z!r}, expected negative for a quiet block"

    def test_raw_volumes_never_negative(self):
        r = self._quiet_block()
        _assert_non_negative(r, ['block_volume', 'session_cumulative_volume'], 'VolumeBlock')

    def test_sigma_never_negative(self):
        r = self._quiet_block()
        for n, (_z, _mu, sd, _a) in r.block_volume_tiers.items():
            if sd is not None:
                assert sd >= 0, f"tier {n}d sigma={sd!r} — a standard deviation cannot be negative"


class TestVolumeDynamics:
    """Collapsing participation is a first-class state, modelled symmetrically
    with expansion: fading participation matters as much as building."""

    @staticmethod
    def _collapsing():
        return calculate_volume_dynamics(
            current_volume_z=-1.4,          # participation now BELOW normal
            previous_volume_z=0.7,          # it was above
            previous_velocity=0.5,          # and had been rising
            recent_volume_z_history=[-1.9, -1.7, -1.5])

    def test_collapsing_volume_produces_negative_everything(self):
        r = self._collapsing()
        _assert_negative(r, ['volume_z', 'velocity', 'acceleration',
                             'persistence', 'expansion_state'], 'VolumeDynamics')

    def test_expansion_state_is_symmetric(self):
        """-2 collapsing mirrors +2 expanding rapidly. An implementation that
        floored the state at 0 would erase half the signal."""
        collapsing = calculate_volume_dynamics(
            current_volume_z=-1.4, previous_volume_z=0.7, previous_velocity=0.5,
            recent_volume_z_history=[-1.9, -1.7, -1.5])
        expanding = calculate_volume_dynamics(
            current_volume_z=1.4, previous_volume_z=-0.7, previous_velocity=-0.5,
            recent_volume_z_history=[1.9, 1.7, 1.5])
        assert collapsing.expansion_state == -2
        assert expanding.expansion_state == 2
        assert collapsing.velocity == pytest.approx(-expanding.velocity)


class TestPriceVolumeDivergence:

    @staticmethod
    def _diverging():
        # Price drifts up on steadily fading volume -> negative correlation.
        import random
        rng = random.Random(3)
        prices, volumes = [100.0], []
        for i in range(180):
            prices.append(prices[-1] + rng.gauss(0.02, 0.08))
            volumes.append(max(200.0, 60_000 - i * 250 + rng.gauss(0, 3_000)))
        return calculate_pvd_v3(prices=prices[1:], volumes=volumes,
                                previous_fisher_history=[0.01 * i for i in range(120)])

    def test_diverging_tape_produces_negative_correlations(self):
        r = self._diverging()
        _assert_negative(r, ['pearson_r', 'spearman_r', 'winsorized_r',
                             'fisher_value'], 'PVD')

    def test_z_negative_at_every_populated_tier(self):
        r = self._diverging()
        populated = {n: t for n, t in r.pvd_tiers.items() if t[0] is not None}
        assert populated, "scenario produced no populated tier"
        for n, (z, _mu, _sd, _av) in populated.items():
            assert z < 0, f"pvd tier {n}d z={z!r}, expected negative"

    def test_estimator_agreement_negative_when_all_three_agree_negative(self):
        """-1.0 means all three estimators agree the correlation is NEGATIVE.
        It is not a failure code."""
        r = self._diverging()
        assert r.estimator_agreement == pytest.approx(-1.0)
        assert r.estimators_available == 3, "all three must have survived here"

    def test_agreement_is_none_below_two_estimators(self):
        """Agreement between fewer than two things is not a concept. It used to
        report +/-1.0 from a single survivor, reading as unanimity among three
        when two did not exist."""
        flat = calculate_pvd_v3(prices=[100.0] * 60,
                                volumes=[50_000 - i * 400 for i in range(60)],
                                previous_fisher_history=_RISING)
        assert flat.status == 'no_price_variation', "a stock that never ticked is a signal"
        assert flat.estimator_agreement is None
        assert flat.estimators_available == 0

    def test_sample_size_critical_r_volatility_never_negative(self):
        r = self._diverging()
        _assert_non_negative(r, ['sample_size', 'critical_r', 'price_volatility'], 'PVD')


class TestRollingMomentum:

    @staticmethod
    def _down_day():
        return calculate_rolling_momentum_state(
            current_price=96.5, previous_close=100.0,      # -3.5% on the day
            historical_session_returns=[0.1 * i for i in range(300)])

    def test_down_day_is_negative(self):
        _assert_negative(self._down_day(), ['session_return'], 'RollingMomentum')

    def test_z_negative_at_every_populated_tier(self):
        r = self._down_day()
        populated = {n: t for n, t in r.session_return_tiers.items() if t[0] is not None}
        assert populated, "scenario produced no populated tier"
        for n, (z, _mu, _sd, _av) in populated.items():
            assert z < 0, f"session_return tier {n}d z={z!r}, expected negative on a down day"

    def test_tier_sigma_never_negative(self):
        for n, (_z, _mu, sd, _av) in self._down_day().session_return_tiers.items():
            if sd is not None:
                assert sd >= 0, f"tier {n}d sigma={sd!r} — a standard deviation cannot be negative"


class TestPriceLevels:

    def test_below_level_is_negative(self):
        level = PriceLevel(name='global_high', value=2600.0, level_type='resistance',
                           created_at=None, source=None)
        r = evaluate_price_level(
            price=2500.0, previous_price=2510.0, level=level,
            current_timestamp='2026-08-21T11:00:00', previous_distance=-0.03)
        _assert_negative(r, ['distance_pct', 'distance_log', 'side'], 'PriceLevels')


class TestNoClampReintroduced:
    """Cross-cutting: scan the source for sign-suppressing constructs.

    Complements the per-field assertions above — those catch a clamp only if the
    scenario happens to exercise that field. This catches the construct itself.
    """

    #: (module, function) pairs whose `abs()` is legitimate — a magnitude
    #: reported ALONGSIDE a separately-carried sign, not a suppressed one.
    #: Keyed by MODULE as well: `calculate_acceptance` exists in
    #: vwap_deviation_calculator (a magnitude) and in relative_strength_calculator
    #: (a product whose sign IS the signal), so a bare-name exemption would let an
    #: abs() added to the RS one pass.
    #:
    #: `calculate_vwap_state` is deliberately absent: its only abs() went with
    #: `absolute_vwap_z`, and a leftover exemption would let a new, genuinely
    #: sign-suppressing abs() there pass silently.
    LEGITIMATE_ABS = {
        ('breakout_calculator', 'calculate_breakout_state'),         # distance_from_range = abs(position)
        ('vwap_deviation_calculator', 'calculate_acceptance'),       # abs(current) - abs(previous)
        ('price_alerts_calculator', 'detect_approach'),              # proximity threshold on |distance|
        ('price_volume_divergence_calculator', 'calculate_pvd_v3'),  # reliable = |pearson| > critical_r
    }

    PURE_CALCULATORS = [
        'momentum_calculator', 'breakout_calculator', 'vwap_deviation_calculator',
        'relative_strength_calculator', 'volume_block', 'volume_dynamics',
        'price_volume_divergence_calculator', 'price_alerts_calculator',
        'rolling_momentum_calculator', 'statistics',
    ]

    #: Files outside the pure calculators that compute STORED values. The one
    #: clamp that ever did damage was in engine.py, not in a calculator.
    VALUE_WRITERS = [
        'engine.py', 'metrics.py', 'scripts/daily.py',
        'calculators/momentum_adapter.py', 'calculators/breakout_adapter.py',
        'calculators/vwap_adapter.py', 'calculators/relative_strength_adapter.py',
        'calculators/volume_block_adapter.py', 'calculators/volume_dynamics_adapter.py',
        'calculators/price_volume_divergence_adapter.py', 'calculators/price_levels_adapter.py',
        'calculators/rolling_momentum_adapter.py',
    ]

    #: max(..., 0) calls that clamp no value, by (file, exact source text).
    LEGITIMATE_MAX_ZERO = {
        ('engine.py', 'max(0, self.refresh_interval - elapsed)'),    # time left to sleep
    }

    @pytest.mark.parametrize('relpath', [f'calculators/{m}.py' for m in PURE_CALCULATORS]
                             + VALUE_WRITERS)
    def test_no_max_zero_clamp(self, relpath):
        """No `max(x, 0)` / `max(0, x)` that clamps a value.

        AST, not text scanning — several of these files discuss the REMOVED
        clamps in their docstrings, and a text scan matches the prose.
        """
        source = (_ROOT / relpath).read_text()
        tree = ast.parse(source)
        clamps = [
            n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == 'max'
            and any(isinstance(a, ast.Constant) and a.value == 0 for a in n.args)
            and (relpath, ast.get_source_segment(source, n)) not in self.LEGITIMATE_MAX_ZERO
        ]
        assert not clamps, (
            f"{relpath} has max(..., 0) clamp(s) at line(s) {clamps}. "
            f"Volume clamps were removed after they were measured SUPPRESSING "
            f"extreme signals: ten fabricated zeros in a 180-sample log-volume "
            f"window inflated sigma 9.9x, so a genuine 6x-median volume block "
            f"read z=+0.73 (no highlight) instead of z=+4.83 (row lights up). "
            f"Fix the code producing the impossible value, do not clamp it."
        )

    @pytest.mark.parametrize('module', PURE_CALCULATORS)
    def test_abs_only_where_a_magnitude_is_intended(self, module):
        tree = ast.parse((_ROOT / f'calculators/{module}.py').read_text())
        exempt = set()
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and (module, fn.name) in self.LEGITIMATE_ABS:
                exempt |= {id(n) for n in ast.walk(fn)}
        # The WHOLE module, not only function bodies — an abs() at module level
        # used to escape this scan.
        offenders = [
            f"line {n.lineno}" for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == 'abs' and id(n) not in exempt
        ]
        assert not offenders, (
            f"calculators/{module}.py calls abs() in {offenders}. If that is a "
            f"magnitude reported alongside a separately-carried sign, add the "
            f"function to LEGITIMATE_ABS. If it discards a sign, it is a bug: "
            f"negative is the normal case across this system."
        )
