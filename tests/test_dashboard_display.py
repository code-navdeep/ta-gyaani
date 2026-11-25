"""
Display-layer guard: z-chart autoscaling.

The failure here is SILENT. A clipped chart looks exactly like a normal chart —
the extreme line simply is not there. Nothing raises, so only a test catches a
regression.

This file also covered format_metric_value's 'velocity' branch. That branch was
DELETED: nothing ever called it, and while unreachable it carried genuinely
wrong semantics — it formatted a signed sigma quantity as a MULTIPLIER (green
above 1.0, red below 0.5), so a velocity of 0 rendered red and +0.7 rendered
neutral grey. Recorded here because anyone re-adding velocity formatting should
know that inversion happened once already.
"""
import pandas as pd
import pytest

from dashboard_signals import _z_yaxis_range, _Z_AXIS_FLOOR, _Z_AXIS_HEADROOM


class TestZAxisAutoscale:
    """Autoscale with a floor — never a ceiling.

    These four charts previously hard-clipped to [-3.5, 3.5]. Measured against
    real stored history (344,722 z-scores over 2,050 asset/block partitions),
    that discarded 2,788 observations — 0.81%, up to |z| = 12.88, roughly 16 per
    trading day, and by construction the most tradeable readings in the dataset.
    """

    def test_quiet_chart_is_unchanged_from_the_old_fixed_range(self):
        """Ordinary days must look exactly as they did. The floor guarantees it,
        and also stops a flat chart zooming into noise and manufacturing the
        appearance of movement."""
        quiet = pd.DataFrame({'z_22d': [0.3, -0.8, 1.2], 'z_90d': [0.1, 0.4, -0.2]})
        assert _z_yaxis_range(quiet, ['z_22d', 'z_90d']) == [-3.5, 3.5]

    def test_extreme_value_is_inside_the_frame(self):
        """The whole point: |z| = 12.88 occurs in real data and must be visible."""
        extreme = pd.DataFrame({'z_22d': [0.3, 12.88, 1.2]})
        low, high = _z_yaxis_range(extreme, ['z_22d'])
        assert high > 12.88, f"12.88 still clipped: upper bound is {high}"
        assert high == pytest.approx(12.88 * _Z_AXIS_HEADROOM)

    def test_extreme_negative_is_inside_the_frame(self):
        """Volume collapsing / momentum reversing produce large negatives, and
        they matter as much as the positives."""
        neg = pd.DataFrame({'z_22d': [-9.4, 0.2]})
        low, high = _z_yaxis_range(neg, ['z_22d'])
        assert low < -9.4, f"-9.4 still clipped: lower bound is {low}"

    def test_range_is_symmetric_so_zero_stays_centred(self):
        """The |z|>2 shading band is a fixed visual reference; an asymmetric
        range would slide zero around as data arrives."""
        skewed = pd.DataFrame({'z_22d': [-0.1, 8.0]})
        low, high = _z_yaxis_range(skewed, ['z_22d'])
        assert low == -high

    @pytest.mark.parametrize('frame,cols,label', [
        (pd.DataFrame({'z': [None, None]}),          ['z'],    'all-None object dtype'),
        (pd.DataFrame({'z': [float('nan')] * 3}),    ['z'],    'all-NaN float dtype'),
        (pd.DataFrame({'z': [1.0]}),                 ['nope'], 'column absent'),
        (None,                                        ['z'],    'frame is None'),
        (pd.DataFrame(),                              ['z'],    'empty frame'),
    ])
    def test_degenerate_inputs_fall_back_to_the_floor(self, frame, cols, label):
        """An unpopulated tier column arrives from metrics.db as OBJECT dtype,
        not float — the frames are built from dicts. `.abs()` raises TypeError on
        that, and it is the normal state of 180d/252d until history accrues, so
        it would have blanked the chart for every asset."""
        assert _z_yaxis_range(frame, cols) == [-_Z_AXIS_FLOOR, _Z_AXIS_FLOOR], label

    def test_peak_is_taken_across_all_plotted_tiers(self):
        """One extreme tier must expand the axis even if the others are quiet —
        otherwise the extreme tier is the one clipped."""
        mixed = pd.DataFrame({'z_22d': [0.5, 1.0], 'z_252d': [7.0, 0.1]})
        _low, high = _z_yaxis_range(mixed, ['z_22d', 'z_252d'])
        assert high == pytest.approx(7.0 * _Z_AXIS_HEADROOM)

    def test_populated_and_null_tiers_together(self):
        mixed = pd.DataFrame({'z_22d': [4.9, 1.0], 'z_252d': [None, None]})
        _low, high = _z_yaxis_range(mixed, ['z_22d', 'z_252d'])
        assert high == pytest.approx(4.9 * _Z_AXIS_HEADROOM)
