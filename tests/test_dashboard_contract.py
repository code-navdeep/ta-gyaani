"""
Dashboard architecture-contract tests — locked-calculator generation.

Every test here targets a specific invariant: the boot-time contract check,
the list-of-dicts merge path + auto-discovery, and the full render path
through create_detail_panel. If any test fails, one of those architectural
properties has regressed and needs a code fix, not a test relaxation.
"""
import pandas as pd
import pytest


# --------------------------------------------------------------------------- #
# Startup contract check
# --------------------------------------------------------------------------- #

def test_dashboard_imports_cleanly():
    """Importing dashboard executes the boot-time contract check
    (`_CONSUMED_FIELDS ⊆ all_emitted_fields() ∪ _ALIASES`). If the check raises,
    ImportError propagates and this test fails."""
    import dashboard  # noqa: F401


def test_schema_consumed_fields_are_subset_of_emitted():
    """Explicit assertion of the same invariant the boot-time check enforces,
    so failures point directly at the offending set rather than at a bare
    ImportError."""
    import dashboard
    from calculators.schemas import all_emitted_fields
    unknown = dashboard._CONSUMED_FIELDS - all_emitted_fields() - dashboard._ALIASES
    assert unknown == set(), (
        f"dashboard consumes fields not declared by any Row dataclass and not "
        f"registered as an alias: {sorted(unknown)}"
    )


# --------------------------------------------------------------------------- #
# List-of-dicts merge path + auto-discovery
# --------------------------------------------------------------------------- #

def test_get_current_metrics_returns_list_not_dataframe(monkeypatch, fixture_metrics):
    """get_current_metrics must return a list of dicts, never a DataFrame
    (a DataFrame reintroduces the NaN-in-Series crash class)."""
    import metrics
    import dashboard
    monkeypatch.setattr(metrics, 'get_latest_metrics', lambda: fixture_metrics)

    result = dashboard.get_current_metrics()

    assert isinstance(result, list), f"expected list, got {type(result).__name__}"
    assert not isinstance(result, pd.DataFrame), "get_current_metrics regressed to DataFrame"
    assert all(isinstance(r, dict) for r in result), "not every row is a plain dict"


def test_get_current_metrics_covers_both_segments(monkeypatch, fixture_metrics):
    """Fixture emits one NSE_EQ stock + one IDX_I index. Merged output must
    contain both."""
    import metrics
    import dashboard
    monkeypatch.setattr(metrics, 'get_latest_metrics', lambda: fixture_metrics)

    result = dashboard.get_current_metrics()
    seg_codes = {r.get('segment_code') for r in result}

    assert seg_codes == {'NSE_EQ', 'IDX_I'}, (
        f"expected both segments; got {seg_codes}"
    )


def test_auto_discover_skips_non_calculator_keys(monkeypatch, fixture_metrics):
    """Adding a non-list value or a NON_CALC key to LATEST_METRICS must not
    crash the merge."""
    fixture_metrics['some_scalar'] = 'not-a-list'
    fixture_metrics['some_dict'] = {'a': 1}

    import metrics
    import dashboard
    monkeypatch.setattr(metrics, 'get_latest_metrics', lambda: fixture_metrics)

    result = dashboard.get_current_metrics()

    assert len(result) == 2


def test_status_fields_do_not_collide_across_signals(monkeypatch, fixture_metrics):
    """Every locked Row now declares a `status` field. Without per-signal
    aliasing (Signal.field_renames), the last-merged calculator's status
    would silently clobber every earlier one's. Confirm each alias survives
    the merge for the stock row (which has all 9 signals)."""
    import metrics
    import dashboard
    monkeypatch.setattr(metrics, 'get_latest_metrics', lambda: fixture_metrics)

    result = dashboard.get_current_metrics()
    stock_row = next(r for r in result if r.get('segment_code') == 'NSE_EQ')

    for alias in ('momentum_status', 'breakout_status', 'vwap_status',
                  'relative_strength_status', 'volume_block_status',
                  'volume_dynamics_status', 'price_volume_divergence_status',
                  'price_levels_status', 'rolling_momentum_status'):
        assert stock_row.get(alias) == 'valid', f"{alias} missing or wrong on merged stock row"


def test_acceleration_does_not_collide_between_momentum_and_volume_dynamics(monkeypatch, fixture_metrics):
    """Momentum's `acceleration` and Volume Dynamics' are different signals and
    must not overwrite each other in the merged row.

    This used to require a `volume_dynamics_acceleration` alias. It no longer
    does: Volume Dynamics tiers, so its fields are `acceleration_22d/90d/...`
    while Momentum's stays a bare scalar. The names cannot collide, which is a
    stronger guarantee than an alias that has to be remembered — so this test
    now asserts the separation directly, and asserts the alias is gone.
    """
    import dataclasses
    import metrics
    import dashboard
    from calculators.schemas import MomentumRow, VolumeDynamicsRow

    mom_fields = {f.name for f in dataclasses.fields(MomentumRow)}
    vd_fields = {f.name for f in dataclasses.fields(VolumeDynamicsRow)}
    signal_overlap = (mom_fields & vd_fields) - {
        'asset_id', 'segment_code', 'symbol', 'timestamp', 'status'}
    assert not signal_overlap, (
        f"Momentum and Volume Dynamics share signal field(s) {signal_overlap}; "
        f"one will silently overwrite the other in the merged row"
    )

    monkeypatch.setattr(metrics, 'get_latest_metrics', lambda: fixture_metrics)
    result = dashboard.get_current_metrics()
    stock_row = next(r for r in result if r.get('segment_code') == 'NSE_EQ')

    assert stock_row.get('acceleration') == 0.001, "Momentum's acceleration was overwritten"
    assert stock_row.get('acceleration_90d') == 1.11, "Volume Dynamics' 90d acceleration missing/wrong"
    assert 'volume_dynamics_acceleration' not in stock_row, (
        "the alias should be gone — the fields no longer collide"
    )


# --------------------------------------------------------------------------- #
# build_table_data — every raw_* field lands in the DataTable row
# --------------------------------------------------------------------------- #

_EXPECTED_RAW_FIELDS = {
    # Identity / prices
    'raw_symbol', 'raw_current_price', 'raw_segment_code',
    # Momentum + tier
    'raw_gap_return', 'raw_early_return', 'raw_open_price', 'raw_previous_close',
    'raw_gap_z_90d', 'raw_early_return_z_90d',
    # Breakout
    'raw_breakout_direction', 'raw_breakout_now', 'raw_breakout_position', 'raw_range_high',
    'raw_range_low', 'raw_breakout_status',
    # VWAP
    'raw_vwap', 'raw_displacement', 'raw_vwap_z_90d', 'raw_vwap_status',
    # RS + tier. session_rs / intraday_rs / acceptance are per-tier now —
    # each measured with its own beta — so there is no untiered name
    # to assert. 90d is what the card and the RelStr column read today.
    'raw_session_rs_90d', 'raw_intraday_rs_90d', 'raw_acceptance_90d',
    'raw_beta_90d', 'raw_benchmark_name',
    # `raw_max_intraday_rs_z`, no tier suffix: it is a cross-benchmark
    # aggregate already resolved at the SELECTED tier by build_table_data, so a
    # _90d suffix would name a tier it no longer necessarily holds.
    'raw_all_benchmarks', 'raw_max_intraday_rs_z', 'raw_relative_strength_status',
    # Volume Block + tier
    'raw_time_block', 'raw_block_volume', 'raw_session_cumulative_volume',
    'raw_block_volume_z_90d', 'raw_volume_block_status',
    # Volume Dynamics
    # Volume Dynamics tiers like Volume Block; no alias needed any more since
    # acceleration_90d cannot collide with Momentum's bare `acceleration`.
    'raw_velocity_90d', 'raw_acceleration_90d', 'raw_expansion_state_90d',
    'raw_volume_z_90d', 'raw_persistence_90d',
    'raw_volume_dynamics_status',
    # Price-Volume Divergence
    'raw_pearson_r', 'raw_reliable', 'raw_pvd_z_90d', 'raw_price_volume_divergence_status',
    # Price Levels
    'raw_levels', 'raw_day_events', 'raw_price_levels_status', 'raw_day_breach_count',
    # Rolling Momentum
    'raw_session_return', 'raw_session_return_z_90d', 'raw_rolling_momentum_status',
}


def test_build_table_data_populates_every_raw_field(monkeypatch, fixture_metrics):
    """Every declared raw_* key must be present on every row of table_data.
    Missing keys cause dashboard-side .get() calls to return None and render
    'N/A' — the exact silent-failure mode the contract check prevents at the
    field-name level. This test extends the guarantee to the row-shape level."""
    import metrics
    import dashboard
    monkeypatch.setattr(metrics, 'get_latest_metrics', lambda: fixture_metrics)

    rows = dashboard.get_current_metrics()
    # 22d: the tier a fresh install opens on.
    table_data = dashboard.build_table_data(rows, tier='22d')

    assert len(table_data) == 2
    for row in table_data:
        missing = _EXPECTED_RAW_FIELDS - set(row.keys())
        assert not missing, (
            f"table_data row for {row.get('Symbol')} missing raw_ fields: {sorted(missing)}"
        )

    # Keys alone prove nothing: every raw_* key is written whether or not the
    # merged row had a value, so a merge that lost the data would still pass
    # the check above. One value per signal, from the fixture:
    stock = next(r for r in table_data if r.get('raw_segment_code') == 'NSE_EQ')
    expected = {
        'raw_symbol': 'RELIANCE',
        'raw_early_return_z_22d': 0.9,          # Momentum
        'raw_breakout_position': 0.5,           # Breakout
        'raw_vwap_z_22d': 1.0,                  # VWAP
        'raw_benchmark_name': 'Nifty',          # Relative Strength
        'raw_block_volume_z_22d': 1.4,          # Volume Block
        'raw_velocity_22d': 0.28,               # Volume Dynamics
        'raw_pvd_z_22d': 1.5,                   # PV Divergence
        'raw_day_events': ['local_high'],       # Price Levels
        'raw_session_return_z_22d': 1.3,        # Rolling Momentum
    }
    wrong = {k: (stock.get(k), v) for k, v in expected.items() if stock.get(k) != v}
    assert not wrong, f"merged values lost or wrong (got, expected): {wrong}"


# --------------------------------------------------------------------------- #
# create_detail_panel renders without exception
# --------------------------------------------------------------------------- #

def test_create_detail_panel_renders_for_stock(monkeypatch, fixture_metrics):
    """Full end-to-end: fixture → get_current_metrics → build_table_data → pick
    the stock row → create_detail_panel. Must return an html.Div and raise
    nothing — all 9 card builders execute, at 22d (a fresh install's tier)."""
    from dash import html
    import metrics
    import dashboard
    monkeypatch.setattr(metrics, 'get_latest_metrics', lambda: fixture_metrics)

    rows = dashboard.get_current_metrics()
    table_data = dashboard.build_table_data(rows, tier='22d')
    stock_row = next(r for r in table_data if r.get('raw_segment_code') == 'NSE_EQ')

    panel = dashboard.create_detail_panel(stock_row, tier='22d')

    assert isinstance(panel, html.Div)


def test_create_detail_panel_renders_for_index(monkeypatch, fixture_metrics):
    """Index rows carry no RS entries — verify the RS card's fallback path
    (empty all_benchmarks list) doesn't crash."""
    from dash import html
    import metrics
    import dashboard
    monkeypatch.setattr(metrics, 'get_latest_metrics', lambda: fixture_metrics)

    rows = dashboard.get_current_metrics()
    table_data = dashboard.build_table_data(rows, tier='22d')
    index_row = next(r for r in table_data if r.get('raw_segment_code') == 'IDX_I')

    panel = dashboard.create_detail_panel(index_row, tier='22d')

    assert isinstance(panel, html.Div)


def test_index_row_all_benchmarks_defaults_to_empty_list(monkeypatch, fixture_metrics):
    """An index row that never received an RS entry must have
    `all_benchmarks` either absent or an actual empty list — never NaN."""
    import metrics
    import dashboard
    monkeypatch.setattr(metrics, 'get_latest_metrics', lambda: fixture_metrics)

    rows = dashboard.get_current_metrics()
    index_row = next(r for r in rows if r.get('segment_code') == 'IDX_I')

    ab = index_row.get('all_benchmarks', [])
    assert isinstance(ab, list), f"expected list, got {type(ab).__name__}"
    assert ab == [], "index rows have no RS entries; all_benchmarks should be []"


# --------------------------------------------------------------------------- #
# Empty-cache path
# --------------------------------------------------------------------------- #

def test_get_current_metrics_empty_cache_returns_empty_list(monkeypatch):
    """When the metrics cache has no timestamp (uninitialized), the function
    must return an empty list, not an empty DataFrame or None."""
    import metrics
    import dashboard
    monkeypatch.setattr(metrics, 'get_latest_metrics', lambda: {})

    result = dashboard.get_current_metrics()

    assert result == []


def test_build_table_data_empty_input_returns_empty_list():
    """build_table_data(empty list) → empty list. Guards against a regression
    where the function assumes DataFrame semantics."""
    import dashboard
    assert dashboard.build_table_data([]) == []
