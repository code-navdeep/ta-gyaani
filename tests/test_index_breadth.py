"""
Index breadth counters moved from the metrics thread to display time.

IndexAggregator used to count `z > 2.0` / `z < -2.0` itself and store six
integers: vol_spike_high/low + zscore_90d_evaluated, and
session_return_z_spike_high/low + session_return_z_evaluated.

The dashboard's scores table, row highlights and Signals glyphs all follow the
settings-drawer z-slider. So at any slider position other than 2.0 the index
card and the scores table disagreed about what "unusual" means, on the same
screen, with no way for the trader to tell which one to believe.

IndexAggregator runs in the metrics thread and persists to metrics.db; it
cannot read a per-browser display setting. So it now stores the OBSERVATIONS —
each constituent's z at each tier — and the dashboard counts.

Storing raw z rather than pre-counting is the ONLY route, not just the tidier
one. The counters feed two surfaces: the breadth grid (live cache, which could
have been recounted from LATEST_METRICS) and the intraday breadth CHART, which
plots breadth over time from index_metrics HISTORY. A historical row cannot be
recounted from a count.

The load-bearing test here is `TestBehaviourPreserved`: at threshold 2.0 the
new display-time counts must equal what the old stored counters produced.
"""
import dataclasses
import json

import pytest

from calculators.schemas import VolumeDynamicsRow, VwapRow
from dashboard import _count_z_spikes
from metrics import (IndexAggregator, _INDEX_AGGREGATOR_READS,
                     _check_index_aggregator_reads)

NIFTY = 'NIFTY'

# Hand-picked to straddle both 2.0 and 2.5, so a threshold change MUST move the
# counts. Values sit either side of each boundary and one sits exactly on 2.0
# to pin the strict-inequality convention.
#
# (asset_id, segment_code, weight, block_volume_z_90d, session_return_z_90d)
CONSTITUENTS = [
    (1,  'NSE_EQ', 10.0,  3.10,  2.60),   # spike-high at both 2.0 and 2.5
    (2,  'NSE_EQ', 20.0,  2.30,  2.20),   # spike-high at 2.0 only
    (3,  'NSE_EQ', 15.0,  2.00,  0.10),   # exactly 2.0 -> NOT a spike (strict >)
    (4,  'NSE_EQ', 12.0,  0.40, -2.30),   # rmom spike-low at 2.0 only
    (5,  'NSE_EQ', 18.0, -2.90, -3.40),   # spike-low at both
    (6,  'NSE_EQ',  9.0,  None,  1.10),   # no volume z -> not evaluated for volume
    (7,  'NSE_EQ',  8.0,  1.20,  None),   # no rmom z  -> not evaluated for rmom
]

# Volume Dynamics velocity per constituent. Constituent 6 has no volume z, so
# Volume Dynamics has nothing to differentiate and emits no row for it.
VELOCITY = {1: 1.50, 2: -0.50, 3: 0.25, 4: 0.80, 5: -1.20, 7: 0.10}


def _slot(cz, key, signal, tier):
    """One constituent's value for (signal, tier) out of the positional payload.

    Mirrors dashboard._count_z_spikes' index arithmetic, and deliberately reads
    the layout from the ROW's own `sig`/`tiers` rather than from TIERS.
    """
    row = dict(zip(cz['k'], cz['v']))[key]
    return row[cz['sig'].index(signal) * len(cz['tiers']) + cz['tiers'].index(tier)]


class _Loader:
    def get_index_constituents(self, csv_key):
        if csv_key != 'NIFTY':
            return []
        return [(aid, seg, w) for aid, seg, w, _z, _r in CONSTITUENTS]


class _Prices:
    def is_ready(self):
        return True

    def get_current_prices(self, ids, segments):
        import pandas as pd
        return pd.DataFrame()


def _latest_metrics(tier_suffix='90d'):
    """Fake LATEST_METRICS in the shape _index_by_asset expects."""
    vb, rm, mom, vd = [], [], [], []
    for aid, seg, _w, zvol, zrm in CONSTITUENTS:
        mom.append({'asset_id': aid, 'segment_code': seg, 'symbol': f'S{aid}',
                    'timestamp': 't', 'current_price': 101.0, 'previous_close': 100.0})
        if zvol is not None:
            vb.append({'asset_id': aid, 'segment_code': seg, 'timestamp': 't',
                       f'block_volume_z_{tier_suffix}': zvol})
        if zrm is not None:
            rm.append({'asset_id': aid, 'segment_code': seg, 'timestamp': 't',
                       f'session_return_z_{tier_suffix}': zrm})
        if aid in VELOCITY:
            # Built from the real dataclass, so a field name this fixture got
            # wrong would raise here instead of quietly matching a wrong read.
            vd.append(dataclasses.asdict(VolumeDynamicsRow(
                asset_id=aid, segment_code=seg, timestamp='t',
                **{f'velocity_{tier_suffix}': VELOCITY[aid]})))
    return {'momentum': mom, 'volume_block': vb, 'rolling_momentum': rm,
            'volume_dynamics': vd}


@pytest.fixture
def summary():
    agg = IndexAggregator(_Loader())
    out = agg.compute(_latest_metrics(), _Prices())
    assert NIFTY in out
    return out[NIFTY]


# --------------------------------------------------------------------------- #
# What the aggregator now emits
# --------------------------------------------------------------------------- #

class TestConstituentZ:

    def test_the_six_stored_counters_are_gone(self, summary):
        """They encoded a threshold the metrics thread has no business owning."""
        for gone in ('vol_spike_high', 'vol_spike_low', 'zscore_90d_evaluated',
                     'session_return_z_spike_high', 'session_return_z_spike_low',
                     'session_return_z_evaluated'):
            assert not hasattr(summary, gone), (
                f"IndexSummary still carries {gone}. A stored count cannot be "
                f"re-thresholded, which is what breaks the historical chart."
            )

    def test_keyed_by_composite_asset_and_segment(self, summary):
        """asset_id 25 is BankNifty in IDX_I and ADANIENT in NSE_EQ. A key
        without the segment merges two different companies into one breadth
        count — silently, since both are plausible constituents."""
        for key in summary.constituent_z['k']:
            aid, _, seg = key.partition(':')
            assert aid.isdigit() and seg, f"key {key!r} is not asset_id:segment_code"
        assert '1:NSE_EQ' in summary.constituent_z['k']

    def test_absent_signal_is_a_null_not_a_zero(self, summary):
        """Constituent 6 has no volume z; constituent 7 has no rolling-momentum
        z. Absence is what the old *_evaluated denominators counted, and a
        fabricated 0.0 would read as 'perfectly normal' — a claim the data does
        not support — as well as inflating the `evaluated` denominator.

        In the positional encoding, absence is a null at that constituent's
        slot rather than a missing dict key."""
        assert _slot(summary.constituent_z, '6:NSE_EQ', 'block_volume_z', '90d') is None
        assert _slot(summary.constituent_z, '6:NSE_EQ', 'session_return_z', '90d') is not None
        assert _slot(summary.constituent_z, '7:NSE_EQ', 'session_return_z', '90d') is None
        assert _slot(summary.constituent_z, '7:NSE_EQ', 'block_volume_z', '90d') is not None

    def test_row_carries_its_own_layout(self, summary):
        """`sig` and `tiers` ride on every row instead of living in one shared
        mapping. That costs ~90 bytes and buys the property a shared mapping
        cannot: the row decodes from ITSELF, so changing TIERS or _CZ_SIGNALS
        later cannot silently misread rows written under the old layout."""
        cz = summary.constituent_z
        assert cz['sig'] == ['block_volume_z', 'session_return_z']
        assert cz['tiers'] == ['22d', '90d', '180d', '252d']
        assert len(cz['k']) == len(cz['v'])
        expected = len(cz['sig']) * len(cz['tiers'])
        for row in cz['v']:
            assert len(row) == expected

    def test_a_row_in_a_different_layout_still_decodes(self):
        """The point of the in-row header: a payload whose signals or tiers are
        ordered differently must still be read correctly, not positionally
        misread against whatever TIERS happens to say today."""
        reversed_layout = {
            'k': ['1:NSE_EQ'],
            'sig': ['session_return_z', 'block_volume_z'],   # swapped
            'tiers': ['252d', '180d', '90d', '22d'],          # reversed
            'v': [[0.0, 0.0, 0.0, 0.0,  0.0, 0.0, 3.0, 0.0]],
        }
        # block_volume_z is signal index 1, 90d is tier index 2 -> slot 1*4+2 = 6
        assert _count_z_spikes(reversed_layout, 'block_volume_z', '90d', 2.0) == (1, 0, 1)

    def test_json_serialisable(self, summary):
        """It is persisted as a JSON TEXT column, so it must survive a
        round-trip through json.dumps/loads unchanged."""
        assert json.loads(json.dumps(summary.constituent_z)) == summary.constituent_z


# --------------------------------------------------------------------------- #
# The regression that matters: same numbers at the old threshold
# --------------------------------------------------------------------------- #

class TestBehaviourPreserved:

    def test_counts_at_2_0_match_the_old_stored_counters(self, summary):
        """Hand-computed from CONSTITUENTS, not from the code under test.

        volume z: 3.10, 2.30 above 2.0; 2.00 is NOT (strict >); -2.90 below;
                  6 constituents evaluated (constituent 6 has no volume z).
        rmom  z: 2.60, 2.20 above; -2.30, -3.40 below; 6 evaluated.
        """
        vh, vl, vev = _count_z_spikes(summary.constituent_z, 'block_volume_z', '90d', 2.0)
        assert (vh, vl, vev) == (2, 1, 6)

        rh, rl, rev = _count_z_spikes(summary.constituent_z, 'session_return_z', '90d', 2.0)
        assert (rh, rl, rev) == (2, 2, 6)

    def test_threshold_is_strict_greater_than(self, summary):
        """Constituent 3 sits exactly at 2.00. It must not count at threshold
        2.0 and must count at 1.9 — pinning `>` rather than `>=`, which would
        be invisible in the grid but shifts every boundary case."""
        assert _count_z_spikes(summary.constituent_z, 'block_volume_z', '90d', 2.0)[0] == 2
        assert _count_z_spikes(summary.constituent_z, 'block_volume_z', '90d', 1.9)[0] == 3

    def test_raising_the_threshold_lowers_the_counts(self, summary):
        """The whole point: the same stored row answers differently as the
        trader moves the slider. Under the old design this number was frozen
        at whatever 2.0 produced."""
        at_20 = _count_z_spikes(summary.constituent_z, 'block_volume_z', '90d', 2.0)
        at_25 = _count_z_spikes(summary.constituent_z, 'block_volume_z', '90d', 2.5)
        at_30 = _count_z_spikes(summary.constituent_z, 'block_volume_z', '90d', 3.0)
        assert at_20[0] == 2 and at_25[0] == 1 and at_30[0] == 1
        # `evaluated` is a property of the DATA, not the threshold — it must not
        # move when the slider does.
        assert at_20[2] == at_25[2] == at_30[2] == 6

    def test_a_constituent_is_never_both_high_and_low(self, summary):
        for thr in (0.5, 1.0, 2.0, 3.0):
            hi, lo, ev = _count_z_spikes(summary.constituent_z, 'block_volume_z', '90d', thr)
            assert hi + lo <= ev


# --------------------------------------------------------------------------- #
# The helper's own edges
# --------------------------------------------------------------------------- #

class TestCountHelper:

    def test_accepts_a_raw_json_string(self):
        """The history frame delivers the column undecoded, so the chart path
        hands this function a string rather than a dict."""
        cz = {'k': ['1:NSE_EQ'],
              'sig': ['block_volume_z', 'session_return_z'],
              'tiers': ['22d', '90d', '180d', '252d'],
              'v': [[None, 3.0, None, None, None, None, None, None]]}
        assert _count_z_spikes(json.dumps(cz), 'block_volume_z', '90d', 2.0) == (1, 0, 1)

    @pytest.mark.parametrize('bad', [None, '', 'not json', 42, [], {}])
    def test_degrades_to_zero_rather_than_raising(self, bad):
        """This runs inside a per-row .apply over the history frame — one
        malformed row must not take the whole chart down."""
        assert _count_z_spikes(bad, 'block_volume_z', '90d', 2.0) == (0, 0, 0)

    def test_unpopulated_tier_counts_as_not_evaluated(self):
        """180d/252d are absent until enough history accumulates. That must
        read as 'no constituents evaluated', not 'no constituents unusual' —
        the difference between a hyphen and a confident zero."""
        cz = {'k': ['1:NSE_EQ'],
              'sig': ['block_volume_z', 'session_return_z'],
              'tiers': ['22d', '90d', '180d', '252d'],
              'v': [[3.0, 3.0, None, None, None, None, None, None]]}
        assert _count_z_spikes(cz, 'block_volume_z', '22d', 2.0) == (1, 0, 1)
        assert _count_z_spikes(cz, 'block_volume_z', '252d', 2.0) == (0, 0, 0)


# --------------------------------------------------------------------------- #
# Weighted volume z — stays server-side, but at four tiers
# --------------------------------------------------------------------------- #

class TestWeightedVolumeZ:

    def test_all_four_tiers_exist(self, summary):
        for tier in ('22d', '90d', '180d', '252d'):
            assert hasattr(summary, f'weighted_volume_zscore_{tier}')

    def test_populated_tier_is_the_weight_aware_mean(self, summary):
        """A weighted AVERAGE, not a threshold count — which is exactly why it
        stays server-side while the spike counts moved to display time."""
        pairs = [(w, z) for _a, _s, w, z, _r in CONSTITUENTS if z is not None]
        expected = sum(w * z for w, z in pairs) / sum(w for w, _z in pairs)
        assert summary.weighted_volume_zscore_90d == pytest.approx(round(expected, 4))

    def test_tier_with_no_data_is_none_not_zero(self, summary):
        """Only 90d was supplied. An unpopulated tier must be absent — 0.0
        would assert 'this index's volume is exactly average over 252 days',
        which nothing measured."""
        assert summary.weighted_volume_zscore_22d is None
        assert summary.weighted_volume_zscore_180d is None
        assert summary.weighted_volume_zscore_252d is None


# --------------------------------------------------------------------------- #
# Tier independence
# --------------------------------------------------------------------------- #

class TestTiers:

    def test_each_tier_is_collected_separately(self):
        """Feed the SAME z-values at 22d instead of 90d and confirm the counts
        follow the tier rather than a hardcoded suffix."""
        agg = IndexAggregator(_Loader())
        out = agg.compute(_latest_metrics(tier_suffix='22d'), _Prices())
        cz = out[NIFTY].constituent_z

        assert _count_z_spikes(cz, 'block_volume_z', '22d', 2.0) == (2, 1, 6)
        assert _count_z_spikes(cz, 'block_volume_z', '90d', 2.0) == (0, 0, 0)
        assert out[NIFTY].weighted_volume_zscore_22d is not None
        assert out[NIFTY].weighted_volume_zscore_90d is None
        assert out[NIFTY].weighted_volume_velocity_22d is not None
        assert out[NIFTY].weighted_volume_velocity_90d is None


# --------------------------------------------------------------------------- #
# Weighted volume velocity — was NULL on every row ever stored
# --------------------------------------------------------------------------- #

class TestWeightedVolumeVelocity:
    """IndexAggregator read `velocity`, a field VolumeDynamicsRow never had,
    so this aggregate was NULL on all 6,160 stored index_metrics rows and the
    index card's velocity row and mini-chart were always empty."""

    def test_populated_tier_is_the_weight_aware_mean(self, summary):
        """Hand-computed from CONSTITUENTS and VELOCITY."""
        weights = {aid: w for aid, _s, w, _z, _r in CONSTITUENTS}
        expected = (sum(weights[a] * v for a, v in VELOCITY.items())
                    / sum(weights[a] for a in VELOCITY))
        assert summary.weighted_volume_velocity_90d == pytest.approx(round(expected, 4))

    def test_tier_with_no_data_is_none_not_zero(self, summary):
        assert summary.weighted_volume_velocity_22d is None
        assert summary.weighted_volume_velocity_180d is None
        assert summary.weighted_volume_velocity_252d is None

    def test_the_untiered_field_is_gone(self, summary):
        assert 'weighted_volume_velocity' not in {
            f.name for f in dataclasses.fields(summary)}


class TestIndexAggregatorReadCheck:
    """IndexAggregator reads dicts with .get(), so a field name its Row does
    not carry is a silent None forever. The import-time check turns that into
    a startup failure."""

    def test_the_declared_reads_all_exist(self):
        _check_index_aggregator_reads(_INDEX_AGGREGATOR_READS)

    def test_the_original_bug_is_rejected(self):
        with pytest.raises(RuntimeError, match=r'VolumeDynamicsRow\.velocity'):
            _check_index_aggregator_reads({VolumeDynamicsRow: {'velocity'}})

    def test_checked_per_row_not_against_the_union(self):
        """`breakout_direction` exists — on BreakoutRow. Read from a VWAP row
        it would be a silent None, so it must be rejected there."""
        with pytest.raises(RuntimeError, match=r'VwapRow\.breakout_direction'):
            _check_index_aggregator_reads({VwapRow: {'breakout_direction'}})


def _texts(component):
    """Every string in a Dash component tree."""
    if isinstance(component, str):
        yield component
        return
    children = getattr(component, 'children', None)
    if children is None:
        return
    for child in children if isinstance(children, list) else [children]:
        yield from _texts(child)


class TestIndexCardShowsTheSelectedTier:

    def test_velocity_row_follows_the_tier(self, monkeypatch):
        import pandas as pd
        import dashboard
        # The card's mini-charts query index_metrics; no database here.
        monkeypatch.setattr(dashboard, 'get_index_breadth_history',
                            lambda *a, **k: (pd.DataFrame(), None))
        summary = {'index_name': NIFTY,
                   'weighted_volume_velocity_22d': 1.23,
                   'weighted_volume_velocity_90d': -0.45}
        assert '+1.23' in list(_texts(dashboard.build_index_card(summary, tier='22d')))
        assert '-0.45' in list(_texts(dashboard.build_index_card(summary, tier='90d')))
