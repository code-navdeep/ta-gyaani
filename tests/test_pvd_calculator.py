"""
PV Divergence's pure calculator (`calculate_pvd_v3`): input checking and how
the three estimators (Pearson, Spearman, winsorized Pearson) are counted.
"""
import pytest

import calculators.price_volume_divergence_calculator as pvd


def test_a_length_mismatch_is_refused_even_on_a_short_input():
    """The mismatch check used to sit after the fewer-than-3 early return, so a
    short mismatched input came back as 'insufficient_data' and hid the bug."""
    with pytest.raises(ValueError, match='length mismatch'):
        pvd.calculate_pvd_v3(prices=[100.0, 100.5], volumes=[10, 20, 30],
                             previous_fisher_history=[])


def test_an_estimator_returning_exactly_zero_counts_as_available(monkeypatch):
    """0.0 is a value — "no relationship" — not a missing estimator. It used to
    be dropped from the count, so the card read "2 of 3" and agreement +1.00."""
    monkeypatch.setattr(pvd, 'spearman_corr', lambda x, y: 0.0)
    prices = [100.0 + 0.1 * i + (0.05 if i % 3 == 0 else 0.0) for i in range(40)]
    volumes = [1000 + 10 * i + (400 if i % 3 == 0 else 0) for i in range(40)]
    result = pvd.calculate_pvd_v3(prices=prices, volumes=volumes, previous_fisher_history=[])
    assert result.pearson_r > 0 and result.winsorized_r > 0
    assert result.estimators_available == 3
    assert result.estimator_agreement == pytest.approx(2 / 3)
