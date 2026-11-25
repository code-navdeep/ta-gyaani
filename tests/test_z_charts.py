"""
The tier charts in the charts drawer.

- The shaded "unusual" band on a z-chart must start at the z-slider's value —
  the same "unusual" the table, row highlights and glyphs use — and reach the
  chart's own axis edge. It used to run from a fixed 2 to a fixed 3.5, while
  the axis autoscales past 3.5.
- Every tier chart must emphasise the selected tier's line. The Volume Block
  and Rolling Momentum charts took the selected tier and ignored it; the
  existing emphasis tests checked the style helper alone and never called a
  builder, which is how that went unnoticed.
"""
import pandas as pd
import pytest

import dashboard_signals as DS

_TS = pd.date_range('2026-08-21 09:30', periods=6, freq='15min')


def _frame(prefix, peak=6.0, **extra):
    """Four tier columns `<prefix>_<tier>`, peaking at `peak` on the 22d line."""
    frame = pd.DataFrame({'timestamp': _TS})
    for i, t in enumerate(('22d', '90d', '180d', '252d')):
        frame[f'{prefix}_{t}'] = [0.5, 1.0, peak - i, 1.0, 0.5, 0.2]
    for key, value in extra.items():
        frame[key] = value
    return frame


_SHADED = {
    'early return':     (DS.build_momentum_chart, lambda: _frame('early_return_z')),
    'relative strength': (DS.build_rel_strength_chart,
                          lambda: _frame('intraday_rs_z', benchmark_name='Nifty')),
    'volume block':     (DS.build_volume_block_chart, lambda: _frame('block_volume_z')),
    'rolling momentum': (DS.build_rolling_momentum_chart, lambda: _frame('session_return_z')),
}

_TIER_CHARTS = {
    **_SHADED,
    'velocity':     (DS.build_velocity_chart, lambda: _frame('velocity')),
    'acceleration': (DS.build_acceleration_chart, lambda: _frame('acceleration')),
}


class TestShadingFollowsTheSlider:

    @pytest.mark.parametrize('name', sorted(_SHADED))
    def test_band_runs_from_the_threshold_to_the_axis_edge(self, name):
        builder, frame = _SHADED[name]
        fig = builder(frame(), z_threshold=2.5)
        edge = fig.layout.yaxis.range[1]
        assert edge > 6.0, "the axis must have grown past the 6.0 reading"
        bands = sorted((s.y0, s.y1) for s in fig.layout.shapes if s.type == 'rect')
        assert bands == [(-edge, -2.5), (2.5, edge)]


class TestSelectedTierIsEmphasised:

    @pytest.mark.parametrize('name', sorted(_TIER_CHARTS))
    def test_the_selected_tier_alone_is_heaviest(self, name):
        builder, frame = _TIER_CHARTS[name]
        fig = builder(frame(), active_tier='22d')
        widths = {trace.name: trace.line.width for trace in fig.data}
        heaviest = max(widths.values())
        assert [n for n, w in widths.items() if w == heaviest] in (['22d'], ['Z-Score 22d'])
