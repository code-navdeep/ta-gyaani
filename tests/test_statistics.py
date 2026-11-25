"""
Guards for `calculators/statistics.py` — the shared numeric core.

Two invariants in this file matter more than the rest, because violating
either produces plausible-looking numbers with no exception, no log line and
no visible symptom on the dashboard:

  1. **Ordering** — a tier must use the NEWEST N samples, never the oldest N.
  2. **Exact fill** — a tier labelled N must use exactly N samples, or be absent.

`TestOrderingContract` and `TestExactFillRule` exist specifically to fail loudly
if either is ever broken again.
"""
import math
import random

import pytest

from calculators import statistics as S


# ---------------------------------------------------------------------------
# Ordering — the landmine guard
# ---------------------------------------------------------------------------

class TestOrderingContract:
    """History is MOST-RECENT-FIRST; tiers take `history[:n]`.

    Seven pure calculators previously sliced `history[-90:]` — the oldest 90 —
    against most-recent-first lists. It was masked because adapters pre-sliced
    to exactly 90, making the negative slice a no-op. Passing a longer history
    (which multi-tier requires) silently z-scores against stale samples.
    """

    @staticmethod
    def _regime_split_history():
        """252 samples: newest 90 centred near 100, oldest 162 centred near 0.

        Any slice confusion changes the answer by orders of magnitude.
        """
        rng = random.Random(20260821)
        recent = [100.0 + rng.gauss(0, 1) for _ in range(90)]
        old = [0.0 + rng.gauss(0, 1) for _ in range(162)]
        return recent + old          # index 0 == newest

    def test_tier_uses_newest_samples_not_oldest(self):
        history = self._regime_split_history()
        value = 100.0

        tiers = S.zscore_tiers(value, history)
        z_90 = tiers[90][0]

        # Correct: value sits inside the recent regime, so z is near zero.
        assert abs(z_90) < 3.0, (
            f"90d tier z={z_90:+.3f}; a value drawn from the recent regime "
            f"should be unremarkable against it. A large |z| means the tier "
            f"sliced the OLD samples."
        )

        # And it must differ enormously from the buggy slice, or this test
        # would pass even with the bug present.
        buggy_window = history[-90:]
        buggy_z = (value - S.mean(buggy_window)) / S.population_std(buggy_window)
        assert abs(buggy_z) > 50.0, "fixture no longer discriminates; fix the fixture"

    def test_tier_mean_matches_newest_window_exactly(self):
        history = self._regime_split_history()
        tiers = S.zscore_tiers(1.0, history)

        for n in S.TIERS:
            expected_mu = sum(history[:n]) / n
            assert tiers[n][1] == pytest.approx(expected_mu), (
                f"tier {n}d mu came from the wrong window"
            )

    def test_longer_tiers_are_supersets_of_shorter_ones(self):
        """22d ⊂ 90d ⊂ 180d ⊂ 252d — each tier extends further into the past.

        Only true under the most-recent-first contract; under oldest-first the
        windows would extend into the *future* instead.
        """
        rng = random.Random(11)
        history = [rng.gauss(0, 1) for _ in range(252)]
        for shorter, longer in zip(S.TIERS, S.TIERS[1:]):
            assert history[:shorter] == history[:longer][:shorter]


# ---------------------------------------------------------------------------
# Exact fill — "22 means 22"
# ---------------------------------------------------------------------------

class TestExactFillRule:
    """Tier N populates at exactly N samples, never below. No substitute floor."""

    @pytest.mark.parametrize('available,expected_populated', [
        (0,   set()),
        (19,  set()),
        (21,  set()),            # the old floor was 20 — must NOT populate 22d
        (22,  {22}),
        (23,  {22}),
        (89,  {22}),
        (90,  {22, 90}),
        (179, {22, 90}),
        (180, {22, 90, 180}),
        (251, {22, 90, 180}),
        (252, {22, 90, 180, 252}),
        (400, {22, 90, 180, 252}),
    ])
    def test_populate_boundaries(self, available, expected_populated):
        rng = random.Random(available or 1)
        history = [rng.gauss(0, 1) for _ in range(available)]
        tiers = S.zscore_tiers(0.5, history)

        populated = {n for n, (z, _, _, _) in tiers.items() if z is not None}
        assert populated == expected_populated, (
            f"{available} samples produced tiers {sorted(populated)}, "
            f"expected {sorted(expected_populated)}"
        )

    def test_absent_tier_has_no_mu_or_sigma_either(self):
        """An absent tier reports nothing — not a z without stats, nor stats
        without a z. The detail drawer renders one hyphen, not a half-row."""
        tiers = S.zscore_tiers(1.0, [0.3] * 50)
        for n in (180, 252):
            z, mu, sigma, _ = tiers[n]
            assert (z, mu, sigma) == (None, None, None)

    def test_available_n_is_samples_on_hand_not_samples_used(self):
        """`available_n` is len(history), identical on every tier.

        Under exact fill, "samples used" is always N or nothing and carries no
        information. Available lets the drawer render `252d — (n 47/252)`.
        """
        history = [0.0] * 47
        tiers = S.zscore_tiers(1.0, history)
        assert all(entry[3] == 47 for entry in tiers.values())

    def test_value_none_yields_all_absent(self):
        tiers = S.zscore_tiers(None, [1.0] * 300)
        assert all(entry[0] is None for entry in tiers.values())
        assert all(entry[3] == 300 for entry in tiers.values())

    def test_absent_tiers_helper_shape_matches(self):
        assert S.absent_tiers(17) == S.zscore_tiers(None, [0.0] * 17)


# ---------------------------------------------------------------------------
# Zero variance
# ---------------------------------------------------------------------------

class TestZeroVariance:
    """Constant history → z is undefined, so it is None, not 0.0.

    The six replaced implementations all returned 0.0, which asserts
    "perfectly normal" — a claim the data cannot support.
    """

    def test_zscore_tiers_returns_none_for_z(self):
        z, mu, sigma, n = S.zscore_tiers(5.0, [3.0] * 100)[90]
        assert z is None
        assert mu == 3.0 and sigma == 0.0 and n == 100, (
            "mu/sigma are real and still worth showing; only z is undefined"
        )

    def test_single_window_zscore_also_none(self):
        assert S.zscore(5.0, [3.0] * 100) is None


# ---------------------------------------------------------------------------
# mu / sigma share the z's units — the Volume Block guard
# ---------------------------------------------------------------------------

class TestUnitConsistency:
    """z, mu and sigma come from one computation, so they cannot diverge.

    Volume Block previously computed z on log-volumes inside the calculator
    while its adapter computed mu/sigma on raw share counts. A real stored row
    reported z=+0.2033 while its displayed mu/sigma implied -0.1089 — opposite
    signs, on the same card.
    """

    def test_z_is_reconstructible_from_mu_and_sigma(self):
        rng = random.Random(3)
        raw_volumes = [rng.uniform(1e5, 5e6) for _ in range(120)]
        current_raw = 3.2e6

        logged_history = [S.safe_log_volume(v) for v in raw_volumes]
        current_logged = S.safe_log_volume(current_raw)

        z, mu, sigma, _ = S.zscore_tiers(current_logged, logged_history)[90]
        assert (current_logged - mu) / sigma == pytest.approx(z, abs=1e-12)

    def test_mixing_spaces_would_have_been_caught(self):
        """Sanity-check the guard itself: raw mu/sigma against a log-space z
        really does reconstruct to a different number."""
        rng = random.Random(4)
        raw = [rng.uniform(1e5, 5e6) for _ in range(120)]
        current_raw = 3.2e6
        z_log = S.zscore_tiers(S.safe_log_volume(current_raw),
                               [S.safe_log_volume(v) for v in raw])[90][0]
        wrong = (current_raw - S.mean(raw[:90])) / S.population_std(raw[:90])
        assert abs(z_log - wrong) > 0.05


# ---------------------------------------------------------------------------
# Elementary statistics
# ---------------------------------------------------------------------------

class TestElementaryStatistics:

    def test_population_std_divides_by_n_not_n_minus_1(self):
        values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        assert S.population_std(values) == pytest.approx(2.0)          # ÷n
        sample = math.sqrt(sum((x - 5.0) ** 2 for x in values) / (len(values) - 1))
        assert S.population_std(values) != pytest.approx(sample)       # not ÷(n-1)

    def test_empty_and_singleton_inputs(self):
        assert S.mean([]) == 0.0
        assert S.population_std([]) == 0.0
        assert S.population_std([42.0]) == 0.0
        assert S.covariance([], []) == 0.0

    def test_covariance_length_mismatch_returns_zero(self):
        assert S.covariance([1.0, 2.0, 3.0], [1.0, 2.0]) == 0.0

    def test_pearson_perfect_and_constant(self):
        assert S.pearson_corr([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)
        assert S.pearson_corr([1, 2, 3, 4], [8, 6, 4, 2]) == pytest.approx(-1.0)
        assert S.pearson_corr([1, 2, 3], [5, 5, 5]) is None

    def test_rank_values_is_ordinal_with_ties_in_index_order(self):
        # Documented behaviour: ties get distinct consecutive ranks, not an
        # averaged rank. Pinned so a future "fix" is a deliberate choice.
        assert S.rank_values([10.0, 30.0, 20.0]) == [1, 3, 2]
        assert S.rank_values([5.0, 5.0, 5.0]) == [1, 2, 3]

    def test_spearman_beats_pearson_on_a_monotone_nonlinear_series(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [1.0, 4.0, 9.0, 16.0, 25.0]
        assert S.spearman_corr(x, y) == pytest.approx(1.0)
        assert S.pearson_corr(x, y) < 1.0

    def test_winsorize_clips_outliers_and_passes_short_series_through(self):
        short = [1.0, 99.0, 2.0]
        assert S.winsorize(short) == short

        values = [0.0] * 50 + [1000.0]
        clipped = S.winsorize(values)
        assert max(clipped) < 1000.0

    def test_fisher_transform_clamps_at_unit_correlation(self):
        assert math.isfinite(S.fisher_transform(1.0))
        assert math.isfinite(S.fisher_transform(-1.0))
        assert S.fisher_transform(0.0) == pytest.approx(0.0)
        assert S.fisher_transform(None) is None

    def test_critical_r_shrinks_with_sample_size(self):
        assert S.critical_r(3) is None
        assert S.critical_r(10) > S.critical_r(180) > S.critical_r(504) > 0


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------

class TestTransforms:

    @pytest.mark.parametrize('value,expected', [
        (3.5, 1), (-3.5, -1), (0.0, 0), (None, None),
    ])
    def test_sign(self, value, expected):
        assert S.sign(value) == expected

    def test_log_return(self):
        assert S.log_return(110.0, 100.0) == pytest.approx(math.log(1.1))
        for bad in ((0.0, 100.0), (100.0, 0.0), (-5.0, 100.0), (None, 100.0), (100.0, None)):
            assert S.log_return(*bad) is None

    def test_first_difference_is_none_safe(self):
        assert S.first_difference(3.0, 1.0) == 2.0
        assert S.first_difference(3.0, None) is None
        assert S.first_difference(None, 1.0) is None

    def test_safe_log_volume_keeps_zero_as_a_real_observation(self):
        """A block where nothing traded is a genuine observation, not an absence.

        `ln(1 + 0) = 0` keeps it finite and keeps it in the distribution.
        """
        assert S.safe_log_volume(0) == 0.0
        assert S.safe_log_volume(None) is None
        assert S.safe_log_volume(999) == pytest.approx(math.log(1000))

    def test_safe_log_volume_does_not_launder_a_negative_into_zero(self):
        """Negative volume must surface, not be clamped.

        This previously read `max(float(volume), 0.0)`, which turned an
        impossible reading into a plausible 0.0 that then entered the
        distribution as if it were a real observation. Measured cost: ten
        fabricated zeros in a 180-sample window inflated sigma 9.9x, so a genuine
        6x-median volume block read z=+0.73 (no highlight) instead of z=+4.83.
        The clamp did not just hide a bug — it suppressed extreme signals.

        No explicit guard was added for this: `log()` already raises on a
        negative argument, so an impossible value surfaces on its own without a
        line of code written for a case that cannot occur (the upstream defect is
        fixed at source by engine.py's same-day filter).
        """
        with pytest.raises(ValueError):
            S.safe_log_volume(-500)


# ---------------------------------------------------------------------------
# Beta
# ---------------------------------------------------------------------------

class TestBeta:

    def test_recovers_a_known_slope(self):
        rng = random.Random(99)
        bench = [rng.gauss(0, 0.01) for _ in range(252)]
        stock = [1.4 * b for b in bench]
        assert S.calculate_beta(stock, bench) == pytest.approx(1.4)

    def test_no_internal_sample_floor(self):
        """Floors belong to callers. precompute_beta gates per tier with
        `len(returns) < n`; a second hidden floor here would silently swallow
        any tier below it."""
        assert S.calculate_beta([1.0, 2.0, 3.0, 4.0, 5.0],
                                [1.0, 2.0, 3.0, 4.0, 6.0]) is not None

    def test_zero_variance_benchmark_returns_none(self):
        assert S.calculate_beta([1.0, 2.0, 3.0], [5.0, 5.0, 5.0]) is None

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            S.calculate_beta([1.0, 2.0], [1.0])


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class TestTierConfiguration:

    def test_tiers_are_ascending_and_canonical(self):
        assert S.TIERS == (22, 90, 180, 252)
        assert list(S.TIERS) == sorted(S.TIERS)
        assert S.MAX_TIER == 252

    def test_fetch_depth_covers_the_top_tier_with_slack(self):
        """Rows are fetched, dropna'd, then sliced. Fetching exactly MAX_TIER
        would under-fill 252d whenever any fetched row has a NULL metric."""
        assert S.FETCH_DAYS == S.MAX_TIER * S.FETCH_MULTIPLIER == 504
