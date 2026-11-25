"""
Relative Strength — beta tier and z tier are ONE axis.

    session_rs = ln(stock_open/prev) - beta * ln(bench_open/prev)

Beta is a parameter of the MEASUREMENT; the tier is the lookback of the
DISTRIBUTION. Tier T must therefore be measured with beta_T *and* normalised
against T samples that were themselves reconstructed with beta_T. Every tier
used to use beta_90d, and the other three cached beta tiers were read from
`historical.db.beta_cache` and thrown away.

The failure this file exists to catch is not a crash. `_reconstruct_return_pairs`
is deliberately beta-INDEPENDENT — it returns (stock_return, bench_return) pairs
and each tier projects them through its own beta. That makes it tempting to hoist
the projection out of the tier loop "since the pairs are the same anyway", which
would z-score all four tiers against beta_90d's distribution. Nothing would raise.
Four plausible, wrong numbers would render.

So the betas below are chosen to straddle 1.0 — 0.5 / 1.0 / 1.5 / 2.0 — which
makes session_rs come out POSITIVE at 22d, exactly ZERO at 90d, and NEGATIVE at
180d and 252d. Any cross-tier mix-up changes a sign, not a decimal.

`run_relative_strength_analysis` had no test at all before this file (as
`run_volume_dynamics_analysis` had none; see tests/test_volume_segments.py).
"""
from datetime import date, datetime, timedelta
from math import exp, log
from zoneinfo import ZoneInfo

import dataclasses
import pandas as pd
import pytest

from calculators.relative_strength_adapter import (
    _project_history,
    _reconstruct_return_pairs,
    run_relative_strength_analysis,
)
from calculators.schemas import RelativeStrengthRow
from calculators.statistics import MAX_TIER, TIERS

IST = ZoneInfo("Asia/Kolkata")

STOCK_ID, STOCK_SEG = 1333, 'NSE_EQ'
BENCH_ID, BENCH_SEG, BENCH_NAME = 13, 'IDX_I', 'Nifty'

# Today's measurement inputs. Both gaps are deliberately the SAME log-return
# (ln(101/100) == ln(202/200)), so session_rs collapses to gap * (1 - beta) and
# the per-tier signature is unmistakable.
STOCK_PREV_CLOSE, STOCK_OPEN = 100.0, 101.0
BENCH_PREV_CLOSE, BENCH_OPEN = 200.0, 202.0
GAP = log(STOCK_OPEN / STOCK_PREV_CLOSE)

# Same trick for the 09:15->09:30 window: both move +1%, so intraday_rs is
# also 0.01 * (1 - beta).
WINDOW_RET = 0.01
STOCK_WINDOW_PRICE = STOCK_OPEN * exp(WINDOW_RET)
BENCH_WINDOW_PRICE = BENCH_OPEN * exp(WINDOW_RET)

BETAS = {22: 0.5, 90: 1.0, 180: 1.5, 252: 2.0}

# A time after the window closes, so _window_price actually queries.
NOW = datetime(2026, 8, 21, 11, 0, tzinfo=IST)


# --------------------------------------------------------------------------- #
# Deterministic history
# --------------------------------------------------------------------------- #

def _daily_returns(n):
    """n distinct daily log-returns per side, varying so sigma > 0."""
    stock = [0.001 * ((i % 7) - 3) for i in range(n)]
    bench = [0.002 * ((i % 5) - 2) for i in range(n)]
    return stock, bench


def _dates(n):
    """n past dates, oldest first. Never includes NOW's date — RS reads EOD
    `daily_ohlc`, so today cannot enter its own comparison window."""
    base = date(2026, 8, 20)
    return [(base - timedelta(days=n - i)).isoformat() for i in range(n)]


class _Stub:
    """Managers the adapter calls. Only the methods it actually reaches."""

    def __init__(self, n_days, betas=BETAS, window_prices=True):
        self.n_days = n_days
        self.betas = betas
        self.window_prices = window_prices
        self.stock_rets, self.bench_rets = _daily_returns(n_days)
        self.dates = _dates(n_days)

    # --- price_data_manager ------------------------------------------------ #
    def is_ready(self):
        return True

    def get_current_prices(self, ids, segments):
        return pd.DataFrame([
            {'asset_id': STOCK_ID, 'segment_code': STOCK_SEG,
             'open_price': STOCK_OPEN, 'last_price': 105.0},
            {'asset_id': BENCH_ID, 'segment_code': BENCH_SEG,
             'open_price': BENCH_OPEN, 'last_price': 210.0},
        ])

    # --- historical_db_manager --------------------------------------------- #
    def get_previous_close_data(self, ids, segments):
        return pd.DataFrame([
            {'asset_id': STOCK_ID, 'segment_code': STOCK_SEG,
             'previous_close': STOCK_PREV_CLOSE},
            {'asset_id': BENCH_ID, 'segment_code': BENCH_SEG,
             'previous_close': BENCH_PREV_CLOSE},
        ])

    def get_beta_cache(self, ids, segments):
        if self.betas is None:
            return pd.DataFrame()
        row = {'asset_id': STOCK_ID, 'segment_code': STOCK_SEG,
               'benchmark_name': BENCH_NAME}
        for t in TIERS:
            row[f'beta_{t}d'] = self.betas.get(t)
        return pd.DataFrame([row])

    def get_daily_price_history(self, ids, segments, days):
        """Close is held CONSTANT so prev_close is constant, and the whole
        daily return lands in open_price. That makes each date's log-return
        exactly the value in _daily_returns — no reconstruction arithmetic to
        second-guess in the assertions."""
        aid = ids[0]
        rets = self.stock_rets if aid == STOCK_ID else self.bench_rets
        close = 100.0 if aid == STOCK_ID else 200.0
        # One extra oldest row: _daily_returns_by_date shifts close_price by 1,
        # so N pairs need N+1 rows.
        rows = [{'date': (date(2026, 8, 20) - timedelta(days=self.n_days + 1)).isoformat(),
                 'open_price': close, 'close_price': close}]
        rows += [{'date': d, 'open_price': close * exp(r), 'close_price': close}
                 for d, r in zip(self.dates, rets)]
        return pd.DataFrame(rows)

    def get_historical_gap_data(self, ids, segments, days_back=None):
        return pd.DataFrame([{'date': d, 'gap': 0.0} for d in self.dates])

    def get_historical_intraday_metrics(self, ids, segments, time_blocks=None,
                                        days_back=None, columns=None):
        aid = ids[0]
        rets = self.stock_rets if aid == STOCK_ID else self.bench_rets
        # gap is 0, so session_price_move (percent) IS the window return.
        return pd.DataFrame([
            {'date': d, 'session_price_move': (exp(r) - 1.0) * 100.0}
            for d, r in zip(self.dates, rets)
        ])

    # --- ohlc_db_manager ---------------------------------------------------- #
    def get_intraday_data_range(self, asset_id, start, end, segment_code=None):
        """Both price columns the real reader returns. `close_price` is Dhan's
        previous-session close — a plausible, DIFFERENT value — so any code that
        reads it as "the price at 09:30" produces a visibly wrong intraday RS."""
        if not self.window_prices:
            return pd.DataFrame()
        if asset_id == STOCK_ID:
            px, prev_close = STOCK_WINDOW_PRICE, STOCK_PREV_CLOSE
        else:
            px, prev_close = BENCH_WINDOW_PRICE, BENCH_PREV_CLOSE
        return pd.DataFrame([{'last_price': px, 'close_price': prev_close}])


class _Loader:
    """Minimal AssetLoader stand-in — the adapter reads only `.assets_df`."""

    def __init__(self):
        self.assets_df = pd.DataFrame([
            {'asset_id': STOCK_ID, 'segment_code': STOCK_SEG,
             'symbol': 'RELIANCE', 'asset_name': 'RELIANCE', 'index': 'NIFTY'},
            {'asset_id': BENCH_ID, 'segment_code': BENCH_SEG,
             'symbol': 'NIFTY', 'asset_name': 'NIFTY', 'index': ''},
        ])


def _run(n_days=MAX_TIER, betas=BETAS, window_prices=True):
    stub = _Stub(n_days, betas=betas, window_prices=window_prices)
    rows = run_relative_strength_analysis(
        stub, stub, stub, asset_loader=_Loader(), now=NOW,
    )
    return stub, rows


def _only(rows):
    assert len(rows) == 1, f"expected one (stock, benchmark) row, got {len(rows)}"
    return rows[0]


# --------------------------------------------------------------------------- #
# The measurement: tier T uses beta_T
# --------------------------------------------------------------------------- #

class TestPerTierBeta:

    def test_session_rs_differs_per_tier_by_exactly_its_beta(self):
        """session_rs_T == GAP * (1 - beta_T), because both gaps are equal.

        Positive at 22d, zero at 90d, negative at 180d/252d. A tier mix-up
        flips a sign rather than nudging a decimal.
        """
        _, rows = _run()
        row = _only(rows)
        for t in TIERS:
            got = getattr(row, f'session_rs_{t}d')
            assert got == pytest.approx(GAP * (1.0 - BETAS[t])), (
                f"session_rs_{t}d was measured with the wrong beta"
            )
        assert row.session_rs_22d > 0
        assert row.session_rs_90d == pytest.approx(0.0, abs=1e-15)
        assert row.session_rs_180d < 0
        assert row.session_rs_252d < 0

    def test_intraday_rs_differs_per_tier_by_exactly_its_beta(self):
        _, rows = _run()
        row = _only(rows)
        for t in TIERS:
            got = getattr(row, f'intraday_rs_{t}d')
            assert got == pytest.approx(WINDOW_RET * (1.0 - BETAS[t]))

    def test_all_four_betas_are_stored_not_just_90d(self):
        """The cache has always held four; the adapter used to read one."""
        _, rows = _run()
        row = _only(rows)
        for t in TIERS:
            assert getattr(row, f'beta_{t}d') == BETAS[t]


# --------------------------------------------------------------------------- #
# The distribution: tier T is normalised against history built with beta_T
# --------------------------------------------------------------------------- #

def _expected_history(stub, beta, tier):
    """Tier `tier`'s session-RS history, built INDEPENDENTLY of the adapter.

    Deliberately calls neither `_reconstruct_return_pairs` nor
    `_project_history`. An oracle that shares code with the thing it checks
    only proves self-consistency: an earlier version of these tests used the
    adapter's own helpers, and a mutation that hoisted the beta projection out
    of the tier loop — the precise bug this class exists to catch — passed both
    of them.

    `stub.dates` is oldest-first; the ordering contract is most-recent-first,
    hence the reversal.
    """
    pairs = list(zip(reversed(stub.stock_rets), reversed(stub.bench_rets)))
    return [s - beta * b for s, b in pairs][:tier]


def _mean_sd(xs):
    """Population mean/std, spelled out — not imported from statistics.py,
    for the same independence reason."""
    n = len(xs)
    mu = sum(xs) / n
    return mu, (sum((x - mu) ** 2 for x in xs) / n) ** 0.5


class TestPerTierHistory:

    def test_z_is_computed_against_this_tiers_own_projected_history(self):
        """The one-axis rule (tier T's z is scored against history rebuilt
        with beta_T), stated as an equality against an independent oracle. If
        the projection were hoisted out of the tier loop, three of these four
        fail."""
        stub, rows = _run()
        row = _only(rows)

        for t in TIERS:
            hist = _expected_history(stub, BETAS[t], t)
            assert len(hist) == t
            mu, sd = _mean_sd(hist)
            value = GAP * (1.0 - BETAS[t])

            assert getattr(row, f'session_rs_avg_{t}d') == pytest.approx(mu)
            assert getattr(row, f'session_rs_std_{t}d') == pytest.approx(sd)
            assert getattr(row, f'session_rs_z_{t}d') == pytest.approx((value - mu) / sd), (
                f"session_rs_z_{t}d was normalised against the wrong beta's history"
            )

    def test_using_the_wrong_tiers_beta_would_be_visible(self):
        """Proves the previous test can actually discriminate.

        For every tier, the mu/sigma implied by ANOTHER tier's beta must differ
        from this tier's — otherwise the equality above would hold under a
        hoist and guard nothing. This is the assertion the earlier
        'all four sigmas differ' test was mistaken for: sigmas differ across
        tiers anyway, simply because each tier slices a different LENGTH of the
        same list. Length is not beta.
        """
        stub, _ = _run()
        for t in TIERS:
            correct = _mean_sd(_expected_history(stub, BETAS[t], t))
            for other in TIERS:
                if BETAS[other] == BETAS[t]:
                    continue
                # Same slice length, wrong beta — isolates beta from length.
                wrong = _mean_sd(_expected_history(stub, BETAS[other], t))
                assert correct != pytest.approx(wrong), (
                    f"tier {t}d's distribution is indistinguishable from the "
                    f"one beta_{other}d would produce — the test above cannot "
                    f"detect a hoisted projection"
                )


# --------------------------------------------------------------------------- #
# Exact fill: a tier populates at exactly its own N, or not at all
# --------------------------------------------------------------------------- #

class TestExactFill:

    @pytest.mark.parametrize('n_days,populated', [
        (21,  []),
        (22,  [22]),
        (89,  [22]),
        (90,  [22, 90]),
        (179, [22, 90]),
        (180, [22, 90, 180]),
        (251, [22, 90, 180]),
        (252, [22, 90, 180, 252]),
    ])
    def test_tier_populates_only_at_its_own_n(self, n_days, populated):
        """22 days means 22 days. No 20-sample floor standing in for a 90d
        label — a tier short of its own N shows a hyphen, because that is
        the truth."""
        _, rows = _run(n_days=n_days)
        row = _only(rows)
        for t in TIERS:
            z = getattr(row, f'session_rs_z_{t}d')
            if t in populated:
                assert z is not None, f"{t}d should populate at {n_days} samples"
            else:
                assert z is None, f"{t}d must be absent at {n_days} samples"

    def test_data_points_reports_samples_available_not_samples_used(self):
        """Under exact fill "used" is always N or 0 — one bit, useless. The
        available count is what makes a hyphen legible: 47/252 tells the
        trader when the tier will arrive."""
        _, rows = _run(n_days=100)
        row = _only(rows)
        for t in TIERS:
            assert getattr(row, f'session_rs_data_points_{t}d') == 100


# --------------------------------------------------------------------------- #
# Absent, not zero
# --------------------------------------------------------------------------- #

class TestAbsentBeta:

    def test_a_tier_without_beta_is_none_everywhere_not_zero(self):
        """A stock with only a 22d beta gets a real 22d row and hyphens above.

        The earlier behaviour was all-or-nothing: no beta_90d meant the
        whole row came back blank with status='insufficient_beta_history',
        discarding a genuinely computable 22d value.
        """
        _, rows = _run(betas={22: 0.5, 90: None, 180: None, 252: None})
        row = _only(rows)

        assert row.session_rs_22d == pytest.approx(GAP * 0.5)
        assert row.session_rs_z_22d is not None
        assert row.status == 'valid'

        for t in (90, 180, 252):
            for suffix in ('', '_z', '_avg', '_std'):
                name = (f'session_rs_{t}d' if suffix == ''
                        else f'session_rs{suffix}_{t}d')
                assert getattr(row, name) is None, f"{name} should be absent, not 0.0"
            assert getattr(row, f'intraday_rs_{t}d') is None
            assert getattr(row, f'acceptance_{t}d') is None
            assert getattr(row, f'beta_{t}d') is None

    def test_no_beta_at_any_tier_is_the_only_row_level_blank(self):
        _, rows = _run(betas=None)
        row = _only(rows)
        assert row.status == 'insufficient_beta_history'
        for t in TIERS:
            assert getattr(row, f'session_rs_{t}d') is None

    def test_a_missing_benchmark_quote_or_close_is_named_not_blamed_on_beta(self):
        """Both used to be reported as insufficient beta history, sending the
        reader to the beta cache for a quote that had simply not arrived."""
        def _without_bench(frame):
            return frame[(frame['asset_id'] != BENCH_ID) | (frame['segment_code'] != BENCH_SEG)]

        class _NoBenchQuote(_Stub):
            def get_current_prices(self, ids, segments):
                return _without_bench(super().get_current_prices(ids, segments))

        class _NoBenchClose(_Stub):
            def get_previous_close_data(self, ids, segments):
                return _without_bench(super().get_previous_close_data(ids, segments))

        for stub_class, status in ((_NoBenchQuote, 'insufficient_data'),
                                   (_NoBenchClose, 'no_previous_close')):
            stub = stub_class(MAX_TIER)
            rows = run_relative_strength_analysis(stub, stub, stub, asset_loader=_Loader(), now=NOW)
            assert _only(rows).status == status

    def test_missing_window_price_leaves_intraday_absent_but_session_intact(self):
        """The 09:15->09:30 window has not closed, or no ticks landed in it.
        That is a reason for intraday_rs to be absent — never a reason to
        discard session_rs, which needs only the open."""
        _, rows = _run(window_prices=False)
        row = _only(rows)
        for t in TIERS:
            assert getattr(row, f'session_rs_{t}d') is not None
            assert getattr(row, f'intraday_rs_{t}d') is None
            assert getattr(row, f'acceptance_{t}d') is None


# --------------------------------------------------------------------------- #
# Sign semantics
# --------------------------------------------------------------------------- #

class TestSigns:

    def test_negative_beta_is_a_real_case(self):
        """An inverse-correlated stock has beta < 0. Nothing may clamp it,
        and a negative beta ADDS the benchmark's move to the stock's."""
        _, rows = _run(betas={22: -0.9, 90: -0.9, 180: -0.9, 252: -0.9})
        row = _only(rows)
        assert row.beta_22d == -0.9
        assert row.session_rs_22d == pytest.approx(GAP * 1.9)
        assert row.session_rs_22d > GAP

    def test_acceptance_is_a_product_whose_sign_is_the_signal(self):
        """acceptance = previous_rs * current_rs. Negative means today's
        relative strength flipped against yesterday's — fading, not an error.
        It is per-tier because "previous" comes off that tier's own history."""
        _, rows = _run()
        row = _only(rows)
        values = [getattr(row, f'acceptance_{t}d') for t in TIERS]
        assert all(v is not None for v in values)
        # beta_90d == 1.0 makes today's intraday_rs exactly 0, so the product
        # is 0 there; the straddling betas put the others on both sides.
        assert row.acceptance_90d == pytest.approx(0.0, abs=1e-15)
        assert any(v < 0 for v in values), (
            "no tier produced a negative acceptance — a clamp or abs() may "
            "have been introduced"
        )


# --------------------------------------------------------------------------- #
# The `acceptance` collision
# --------------------------------------------------------------------------- #

class TestAcceptanceCollision:

    def test_relative_strength_declares_no_untiered_field(self):
        """VWAP also declares `acceptance`. RS is a multi_emission signal whose
        first benchmark row is merged FLAT onto the asset dict over the same
        key, and SIGNAL_REGISTRY orders VWAP before RS — so an untiered RS
        `acceptance` overwrites VWAP's, and the VWAP card's
        "Widening/Reverting" reports RS's number instead. Measured before the
        fix: VWAP -0.111, RS +0.999, merged 0.999.

        Reintroducing any of these three names would fail nothing else.
        """
        declared = {f.name for f in dataclasses.fields(RelativeStrengthRow)}
        for name in ('acceptance', 'session_rs', 'intraday_rs'):
            assert name not in declared, (
                f"RelativeStrengthRow re-declared untiered `{name}`. For "
                f"`acceptance` this silently hijacks the VWAP card; for the "
                f"other two it forces a silent choice of beta."
            )


# --------------------------------------------------------------------------- #
# The shared reconstruction
# --------------------------------------------------------------------------- #

class TestReconstruction:

    def test_projection_reproduces_the_locked_formula_exactly(self):
        """_project_history(pairs, beta) must equal what
        calculate_session_relative_strength computes for each historical date,
        since both reduce to `stock_return - beta * bench_return`."""
        pairs = [(0.01, 0.004), (-0.02, 0.011), (0.0, -0.003)]
        for beta in (-1.4, 0.0, 0.87, 2.3):
            assert _project_history(pairs, beta) == pytest.approx(
                [s - beta * b for s, b in pairs]
            )

    def test_pairs_are_most_recent_first_and_capped_at_max_tier(self):
        """The system-wide ordering contract: history is most-recent-first, so
        every consumer slices [:n]. If this came back oldest-first, every
        "90d" z would score against the OLDEST 90 samples with no error and
        no visible symptom."""
        n = MAX_TIER + 40
        stub = _Stub(n)
        stock = {d: (100.0 * exp(r), 100.0) for d, r in zip(stub.dates, stub.stock_rets)}
        bench = {d: (200.0 * exp(r), 200.0) for d, r in zip(stub.dates, stub.bench_rets)}

        session_pairs, _ = _reconstruct_return_pairs(stock, bench, {}, {})

        assert len(session_pairs) == MAX_TIER
        newest_date = max(stock)
        assert session_pairs[0][0] == pytest.approx(log(stock[newest_date][0] / 100.0))
