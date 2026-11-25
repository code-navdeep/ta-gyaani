"""
Guards for `scripts/precompute_beta.py` — the morning-step job that fills
`historical.db.beta_cache` for the Relative Strength adapter.

A wrong β produces plausible Relative Strength numbers with no exception and
no visible symptom, so each property below is pinned rather than trusted.
"""
import pandas as pd
import pytest

from calculators import relative_strength_adapter, statistics
from calculators.statistics import calculate_beta
from scripts import precompute_beta as P


class TestSharesTheAdaptersTables:
    """The job exists to fill beta_cache for exactly the (stock, benchmark,
    tier) triples the RS adapter reads. A private copy of either table could
    drift from the adapter's, leaving a pair's β NULL with no error."""

    def test_tiers_are_the_shared_constant(self):
        assert P.TIERS is statistics.TIERS

    def test_benchmarks_are_the_adapters(self):
        assert P.BENCHMARKS is relative_strength_adapter.BENCHMARKS
        assert P.benchmarks_for_stock is relative_strength_adapter.benchmarks_for_stock


# Skewed daily moves — large up days, small down days. With symmetric moves
# simple and log returns give almost the same slope, and the tests below could
# not tell the two apart.
_MOVES = [0.09, -0.01, 0.07, -0.02, 0.08, -0.01, 0.06, -0.03, 0.10, -0.02, 0.05]


def _bench_closes(n):
    closes = [100.0]
    for i in range(n - 1):
        closes.append(closes[-1] * (1 + _MOVES[i % len(_MOVES)]))
    return closes


def _frames(stock_closes, bench_closes):
    dates = pd.date_range('2026-01-01', periods=len(bench_closes), freq='D')
    return (pd.DataFrame({'date': dates, 'close_price': stock_closes}),
            pd.DataFrame({'date': dates, 'close_price': bench_closes}))


class TestBetaUsesLogReturns:
    """β is APPLIED to log returns — session_rs = ln(stock_open/prev) −
    β × ln(bench_open/prev) — so it must be FITTED on them."""

    def test_stock_moving_twice_the_benchmark_in_log_terms_has_beta_two(self):
        """stock = bench² / 100, so ln(stock_t / stock_t-1) is exactly
        2 × ln(bench_t / bench_t-1) on every day, and β on log returns is 2."""
        bench = _bench_closes(23)                     # 23 closes -> 22 returns
        stock = [c * c / 100.0 for c in bench]
        betas = P._compute_beta_for_pair(*_frames(stock, bench))
        assert betas['beta_22d'] == pytest.approx(2.0, abs=1e-9)
        assert betas['beta_90d'] is None              # 22 returns cannot fill 90

    def test_the_series_discriminates_simple_from_log(self):
        """Without this, the test above could also pass under pct_change()."""
        bench = pd.Series(_bench_closes(23))
        stock = bench * bench / 100.0
        simple = calculate_beta(stock.pct_change().dropna().tolist(),
                                bench.pct_change().dropna().tolist())
        assert abs(simple - 2.0) > 1e-3


class TestMissingCloseIsNotForwardFilled:
    def test_a_missing_close_costs_its_two_returns(self):
        """24 closes give 23 returns. One missing close has no return into it
        and none out of it, leaving 21 — short of 22, so β_22d is absent.
        pct_change() forward-filled the gap into a fabricated 0% day plus a
        double-sized one, kept all 23, and filled the tier."""
        bench = _bench_closes(24)
        stock = [c * c / 100.0 for c in bench]
        stock[10] = float('nan')
        betas = P._compute_beta_for_pair(*_frames(stock, bench))
        assert betas['beta_22d'] is None
