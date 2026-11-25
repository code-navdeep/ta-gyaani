"""
On invalid input a calculator reports "unknown" (None), never a 0 — a 0 asserts
something about the market ("exactly at VWAP", "flat on the day", "no gap")
that was never measured. The adapters already report a missing input as a row
with a status rather than a number; this holds the calculators to the same rule.

These paths are unreachable from today's adapters, which check their inputs
first; only a direct test keeps them honest.
"""
from calculators.momentum_calculator import calculate_momentum_state
from calculators.rolling_momentum_calculator import calculate_rolling_momentum_state
from calculators.statistics import finite_positive
from calculators.vwap_deviation_calculator import calculate_vwap_state


def test_an_uncomputed_sample_count_is_unknown_not_zero():
    """A row emitted without a calculation (no quote yet, an interrupted PV
    Divergence window) used to show "n 0" — "no history" — for a count nobody
    computed."""
    from calculators.schemas import PriceVolumeDivergenceRow, VwapRow
    from dashboard_signals import _std_tier_cells
    assert VwapRow().vwap_data_points_22d is None
    pvd = PriceVolumeDivergenceRow()
    assert (pvd.pvd_data_points_252d, pvd.sample_size, pvd.reliable) == (None, None, None)
    assert _std_tier_cells({}, 'vwap', '22d')[-1] == 'N/A'
    assert _std_tier_cells({'raw_vwap_data_points_22d': 47}, 'vwap', '22d')[-1] == '47'


def test_a_missing_price_does_not_break_the_scores_table():
    """Formatting a present-but-None price raised, failing the table update for
    every stock; a default of 0 would have shown ₹0.00."""
    import dashboard
    row = {'asset_id': 1, 'segment_code': 'NSE_EQ', 'symbol': 'T',
           'current_price': None, 'day_events': [], 'all_benchmarks': []}
    (cell,) = dashboard.build_table_data([row], z_threshold=2.0, tier='22d')
    assert cell['Price'] == '-'


def test_a_price_given_as_text_is_not_a_price():
    """The validity gate for prices and volumes: float("5") succeeds, but text
    here means something upstream is broken."""
    assert not finite_positive("5") and not finite_positive(b"5")
    assert finite_positive(5) and finite_positive(5.0)


def test_vwap_without_a_usable_vwap():
    r = calculate_vwap_state(price=2334.0, vwap=0.0, previous_vwap_displacement=None,
                             previous_same_time_vwap_history=[])
    assert r.status == 'invalid_input'
    assert (r.displacement, r.side) == (None, None)


def test_rolling_momentum_without_a_previous_close():
    r = calculate_rolling_momentum_state(current_price=2334.0, previous_close=None,
                                         historical_session_returns=[])
    assert r.status == 'invalid_input'
    assert r.session_return is None


def test_momentum_without_an_open():
    r = calculate_momentum_state(open_price=None, previous_close=2304.0, price_after_window=None,
                                 previous_early_return=None, previous_gap_history=[],
                                 previous_early_history=[])
    assert r.status == 'invalid_input'
    assert (r.gap_return, r.gap_direction, r.early_return, r.continuation) == (None, None, None, None)
