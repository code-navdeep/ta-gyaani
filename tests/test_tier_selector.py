"""
Dashboard tier selector.

Every z-scored signal now computes four tiers (22/90/180/252d). The settings
drawer gained a pill group that selects which one the scanning surfaces show.

The property under test is AGREEMENT, not any single surface: the scores-table
values, the Signals-column glyphs, the row-highlight filter queries, the
why-line and the index card must all read the SAME tier at the same moment. A
surface left behind on 90d is the worst possible outcome — it does not error, it
shows a plausible number that answers a different question, and a row lit by a
90d rule while its cells display 22d values sends the trader to investigate
something the visible data does not support.

So the fixture row carries a DIFFERENT z at every tier, chosen to cross the
threshold at some tiers and not others. Any surface stuck on the wrong tier
produces a visibly wrong value or a wrongly-lit glyph, never a coincidentally
right one.

Deliberately NOT tested as tier-following: the detail-drawer tier grids, which
keep showing all four tiers at once. The drawer is where cross-tier divergence
is visible; collapsing it to one tier would destroy the only surface that
exposes it. Whether that exception should hold is an open design question —
if it is ever resolved the other way, this file's exclusion is what needs
revisiting first.
"""
import re

import pytest

import dashboard as D

THRESHOLD = 2.0

# z per tier, chosen so the threshold is crossed at 22d and 180d only — and
# with opposite SIGNS at those two, so a tier mix-up flips a sign.
_Z = {
    'early_return_z':   {'22d': 3.10, '90d': 0.40, '180d': -2.70, '252d': 1.10},
    'block_volume_z':   {'22d': 2.90, '90d': 0.20, '180d': -3.30, '252d': 0.50},
    'session_return_z': {'22d': 2.50, '90d': 0.10, '180d': 0.20,  '252d': 0.30},
    'pvd_z':            {'22d': 2.20, '90d': 0.10, '180d': 0.10,  '252d': 0.10},
    'vwap_z':           {'22d': 2.40, '90d': 0.10, '180d': 0.10,  '252d': 0.10},
    'gap_z':            {'22d': 0.10, '90d': 0.10, '180d': 0.10,  '252d': 0.10},
    'intraday_rs_z':    {'22d': 2.80, '90d': 0.30, '180d': -2.10, '252d': 0.90},
    # Volume Dynamics velocity — the ↗ glyph's predicate. A magnitude in
    # sigma (a difference of z-scores), so the same z-slider threshold
    # applies. Same cross-at-22d-and-180d shape as the rest.
    'velocity':         {'22d': 2.60, '90d': 0.30, '180d': -2.40, '252d': 0.40},
}
_RS_VALUE = {'22d': 0.004, '90d': 0.001, '180d': -0.003, '252d': 0.002}


@pytest.fixture
def row():
    r = {
        'asset_id': 1, 'segment_code': 'NSE_EQ', 'symbol': 'TESTCO',
        'current_price': 100.0, 'early_return': 0.012, 'session_return': 1.4,
        'day_events': [],
        'all_benchmarks': [{
            'benchmark_name': 'Nifty',
            **{f'intraday_rs_{t}': v for t, v in _RS_VALUE.items()},
            **{f'intraday_rs_z_{t}': v for t, v in _Z['intraday_rs_z'].items()},
        }],
    }
    for stem, per_tier in _Z.items():
        if stem == 'intraday_rs_z':
            continue
        for tier, v in per_tier.items():
            r[f'{stem}_{tier}'] = v
    # Volume Dynamics expansion state — non-z, but tier-suffixed.
    r.update({'expansion_state_22d': 2, 'expansion_state_90d': 0,
              'expansion_state_180d': 0, 'expansion_state_252d': 0})
    return r


def _cell(row, tier, column):
    return D.build_table_data([row], z_threshold=THRESHOLD, tier=tier)[0][column]


def _lit_glyphs(row, tier):
    """Glyphs rendered in a non-inactive colour, read from the OUTPUT.

    Parses the rendered HTML rather than matching against _SIGNAL_GLYPHS'
    colour entries, because a glyph's colour may be a callable — the ↗ slot
    resolves green/red from the sign of velocity. Reading the output is also
    what this helper actually means: "is this glyph lit on screen".
    """
    td = D.build_table_data([row], z_threshold=THRESHOLD, tier=tier)[0]
    lit = set()
    for colour, glyph in re.findall(r'color:(#[0-9a-fA-F]{6});[^>]*>(.)</span>',
                                    td['Signals']):
        if colour.lower() != D._GLYPH_INACTIVE.lower():
            lit.add(glyph)
    return lit


# --------------------------------------------------------------------------- #
# The default
# --------------------------------------------------------------------------- #

class TestDefaultTier:

    @pytest.mark.parametrize('n_days,expected', [
        (0,   '22d'),   # nothing yet -> the first tier that will arrive
        (21,  '22d'),
        (22,  '22d'),
        (89,  '22d'),
        (90,  '90d'),
        (179, '90d'),
        (180, '180d'),
        (251, '180d'),
        (252, '252d'),
        (900, '252d'),
    ])
    def test_longest_tier_the_system_has_data_for(self, monkeypatch, n_days, expected):
        """Global — one answer for the whole page, not per-row or per-signal.

        A fresh install opens on 22d rather than on four columns of hyphens; a
        mature one opens on 252d.
        """
        monkeypatch.setattr(D, 'get_historical_days_count', lambda: n_days)
        assert D.default_active_tier() == expected

    def test_all_four_pills_exist_and_none_is_disabled(self):
        """A tier with no data shows HYPHENS, it is not greyed out. The hyphens
        are the honest answer and the diagnostic: they say which signals have
        not accumulated that much history yet."""
        assert D._TIER_VALUES == ('22d', '90d', '180d', '252d')


# --------------------------------------------------------------------------- #
# The scores table
# --------------------------------------------------------------------------- #

class TestTableFollowsTier:

    @pytest.mark.parametrize('tier', ['22d', '90d', '180d', '252d'])
    def test_momentum_cell_shows_this_tiers_z(self, row, tier):
        assert f"z{_Z['early_return_z'][tier]:+.1f}" in _cell(row, tier, 'Mom')

    @pytest.mark.parametrize('tier', ['22d', '90d', '180d', '252d'])
    def test_block_volume_cell_shows_this_tiers_z(self, row, tier):
        assert _cell(row, tier, 'Block Volume') == f"{_Z['block_volume_z'][tier]:.1f}"

    @pytest.mark.parametrize('tier', ['22d', '90d', '180d', '252d'])
    def test_rolling_momentum_cell_shows_this_tiers_z(self, row, tier):
        assert f"z{_Z['session_return_z'][tier]:+.1f}" in _cell(row, tier, 'R.Mom')

    @pytest.mark.parametrize('tier', ['22d', '90d', '180d', '252d'])
    def test_relstr_pairs_the_value_with_its_OWN_tiers_z(self, row, tier):
        """Both from the same tier. RS is measured once per beta tier, so
        pairing intraday_rs_22d with intraday_rs_z_90d would show a number
        normalised against a distribution built from a different beta."""
        cell = _cell(row, tier, 'RelStr')
        assert f"{_RS_VALUE[tier] * 100:+.2f}%" in cell
        assert f"z {_Z['intraday_rs_z'][tier]:+.1f}" in cell

    def test_switching_tier_actually_changes_the_output(self, row):
        """Guards the whole mechanism: if `tier` were ignored, every assertion
        above could still pass against a coincidentally-equal fixture."""
        seen = {_cell(row, t, 'Mom') for t in D._TIER_VALUES}
        assert len(seen) == 4, f"tier had no effect on the Mom cell: {seen}"

    @pytest.mark.parametrize('bad', [None, '', '45d', 'ninety', 90])
    def test_unrecognised_tier_is_not_silently_used_as_a_suffix(self, row, bad, monkeypatch):
        """A bad value must not produce `early_return_z_45d` lookups that all
        miss and render a table of hyphens. Driven through the real scores-table
        callback: the tier it hands on must be the default, not the bad value."""
        seen = []
        real_build = D.build_table_data
        monkeypatch.setattr(D, 'get_current_metrics', lambda: [row])
        monkeypatch.setattr(D, 'default_active_tier', lambda: '180d')
        monkeypatch.setattr(D, 'build_table_data', lambda rows, z_threshold=None, tier='90d': (
            seen.append(tier) or real_build(rows, z_threshold=z_threshold, tier=tier)))
        D.update_table_data(0, THRESHOLD, bad)
        assert seen == ['180d']


# --------------------------------------------------------------------------- #
# Signals glyphs
# --------------------------------------------------------------------------- #

class TestGlyphsFollowTier:

    def test_glyphs_light_per_tier(self, row):
        """22d lights seven glyphs, 90d none, 180d four.

        Glyphs are read by POSITION, so a glyph lit from the wrong tier is
        invisible as an error — it just silently means something else.
        """
        assert _lit_glyphs(row, '22d') == {'■', '●', 'α', 'ρ', '↗', '‖', '↻'}
        assert _lit_glyphs(row, '90d') == set()
        # ↗ lights at 180d too: velocity is -2.40 there, and the glyph
        # marks a sharp shift in participation in EITHER direction —
        # colour, not shape, carries the sign.
        assert _lit_glyphs(row, '180d') == {'■', '●', 'α', '↗'}

    def test_glyphs_agree_with_the_displayed_cells(self, row):
        """The invariant that matters: whenever the block-volume glyph is lit,
        the Block Volume cell must itself exceed the threshold."""
        for tier in D._TIER_VALUES:
            lit = '■' in _lit_glyphs(row, tier)
            shown = abs(float(_cell(row, tier, 'Block Volume')))
            assert lit == (shown > THRESHOLD), (
                f"{tier}: glyph lit={lit} but cell shows {shown}"
            )


# --------------------------------------------------------------------------- #
# Row highlights
# --------------------------------------------------------------------------- #

class TestHighlightRulesFollowTier:

    @pytest.mark.parametrize('tier', ['22d', '90d', '180d', '252d'])
    def test_every_z_rule_targets_the_selected_tier(self, tier):
        rules = D._row_highlight_rules(z_threshold=THRESHOLD, tier=tier)
        queries = [r['if']['filter_query'] for r in rules if 'filter_query' in r.get('if', {})]
        z_queries = [q for q in queries if '_z_' in q]
        assert z_queries, "no z-driven highlight rules found"
        for q in z_queries:
            assert f'_z_{tier}' in q, f"rule still pinned to another tier: {q}"
            for other in set(D._TIER_VALUES) - {tier}:
                assert f'_z_{other}' not in q

    def test_structural_rules_ignore_the_tier(self):
        """Breakout direction and day-breach count are facts, not z-scores."""
        for tier in D._TIER_VALUES:
            queries = [r['if']['filter_query']
                       for r in D._row_highlight_rules(THRESHOLD, tier)
                       if 'filter_query' in r.get('if', {})]
            assert any('raw_breakout_now' in q for q in queries)
            assert any('raw_day_breach_count' in q for q in queries)

    def test_rs_rule_uses_the_untiered_aggregate_name(self):
        """raw_max_intraday_rs_z carries no suffix — it is a cross-benchmark
        aggregate already resolved at the selected tier upstream. A _90d suffix
        would name a tier it no longer necessarily holds."""
        queries = [r['if']['filter_query']
                   for r in D._row_highlight_rules(THRESHOLD, '22d')
                   if 'filter_query' in r.get('if', {})]
        rs = [q for q in queries if 'max_intraday_rs_z' in q]
        assert rs and 'raw_max_intraday_rs_z}' in rs[0]
        assert '_90d' not in rs[0]


# --------------------------------------------------------------------------- #
# Why-line
# --------------------------------------------------------------------------- #

def _all_text(node, out=None):
    out = [] if out is None else out
    ch = getattr(node, 'children', None)
    if isinstance(ch, str):
        out.append(ch)
    elif isinstance(ch, list):
        for c in ch:
            _all_text(c, out)
    elif ch is not None:
        _all_text(ch, out)
    return out


class TestWhyLineFollowsTier:

    def test_lists_this_tiers_firing_signals_with_this_tiers_labels(self, row):
        td = D.build_table_data([row], z_threshold=THRESHOLD, tier='22d')[0]
        text = ' '.join(_all_text(D._build_why_line(td, THRESHOLD, '22d')))
        assert 'Mom Early-return-z 22d +3.10' in text
        assert 'Block Vol Z 22d +2.90' in text
        assert 'RS Intraday-z 22d +2.80' in text
        assert '90d' not in text, "why-line label still names another tier"

    def test_hides_entirely_when_nothing_is_unusual_at_this_tier(self, row):
        td = D.build_table_data([row], z_threshold=THRESHOLD, tier='90d')[0]
        assert not _all_text(D._build_why_line(td, THRESHOLD, '90d'))

    def test_ranked_by_descending_absolute_z(self, row):
        """Ranking must survive the tier change — at 180d the largest |z| is
        block volume (-3.30), not momentum."""
        td = D.build_table_data([row], z_threshold=THRESHOLD, tier='180d')[0]
        text = _all_text(D._build_why_line(td, THRESHOLD, '180d'))
        joined = [t for t in text if 'Z' in t or 'z' in t]
        assert 'Block Vol Z 180d -3.30' in joined[0]


# --------------------------------------------------------------------------- #
# Chart emphasis
# --------------------------------------------------------------------------- #

class TestChartEmphasis:

    def test_selected_tier_is_drawn_heavier_than_the_others(self):
        from dashboard_signals import _tier_line_style, _TIER_EMPHASIS, _TIER_MUTED
        assert _tier_line_style('22d', '22d') == _TIER_EMPHASIS
        assert _tier_line_style('90d', '22d') == _TIER_MUTED
        assert _TIER_EMPHASIS['width'] > _TIER_MUTED['width']
        assert _TIER_EMPHASIS['opacity'] > _TIER_MUTED['opacity']

    def test_no_selection_renders_all_tiers_equally(self):
        """So any caller that does not pass a tier behaves exactly as before."""
        from dashboard_signals import _tier_line_style
        styles = {_tier_line_style(t, None)['width'] for t in D._TIER_VALUES}
        assert len(styles) == 1

    def test_charts_still_plot_all_four_tiers(self):
        """Emphasis, not filtering. These charts are a divergence surface —
        showing only the selected tier would remove the comparison that makes
        four tiers worth computing."""
        from dashboard_signals import _Z_TIER_PALETTE, _PLAIN_TIER_PALETTE
        assert len(_Z_TIER_PALETTE) == 4
        assert len(_PLAIN_TIER_PALETTE) == 4


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #

class TestCallbackWiring:

    @pytest.mark.parametrize('output_id', [
        'metrics-table.data',
        'metrics-table.style_data_conditional',
        'detail-panel.children',
        'indices-drawer-body.children',
    ])
    def test_surface_rerenders_when_the_tier_changes(self, output_id):
        """store-active-tier must be an INPUT on every scanning surface. As a
        State it would persist but never trigger, so the table would keep
        showing the old tier until some other input happened to fire."""
        spec = D.app.callback_map[output_id]
        ids = [i['id'] for i in spec['inputs']]
        assert 'store-active-tier' in ids, (
            f"{output_id} does not re-render on a tier change"
        )

    def test_open_drawer_rebuilds_when_the_table_refreshes(self):
        """As a State the table data never triggered a rebuild, so an open
        drawer kept the numbers from the moment of the click while the row
        behind it updated every cycle."""
        ids = [i['id'] for i in D.app.callback_map['detail-panel.children']['inputs']]
        assert 'metrics-table' in ids

    def test_charts_rerender_when_the_tier_changes(self):
        cid = next(k for k in D.app.callback_map if 'chart-velocity' in k)
        ids = [i['id'] for i in D.app.callback_map[cid]['inputs']]
        assert 'store-active-tier' in ids

    def test_tier_store_persists_across_restarts(self):
        """storage_type='local', matching the other two settings stores — the
        trader's choice must survive a container restart, not just a refresh."""
        import dash
        found = [c for c in _walk(D.app.layout) if getattr(c, 'id', None) == 'store-active-tier']
        assert found, 'store-active-tier is not in the layout'
        assert found[0].storage_type == 'local'


def _walk(node):
    yield node
    ch = getattr(node, 'children', None)
    if isinstance(ch, list):
        for c in ch:
            yield from _walk(c)
    elif ch is not None and not isinstance(ch, str):
        yield from _walk(ch)
