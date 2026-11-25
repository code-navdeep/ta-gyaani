"""
Display-surface decisions — the governing principle and its consequences.

> The tier toggle COLLAPSES where a surface can hold exactly one number, and
> EMPHASISES where it can hold four.

The detail drawer can hold four, so it keeps showing all four tiers and the
toggle emphasises the selected row. The scores table cannot, so it collapses.
Charts already worked this way; this is that rule applied everywhere.

The failure this file guards against is the drawer quietly becoming a
collapse-surface — someone "simplifying" the tier block to render only the
selected tier. That would remove the only place cross-tier divergence can be
read, which is the entire reason four tiers are computed.
"""
import pytest

import dashboard as D
import dashboard_signals as DS


def _z_row(**overrides):
    """A merged row with a distinct z at every tier of every z-scored signal.

    Distinct per tier so a surface reading the wrong one produces a visibly
    wrong number rather than a coincidentally right one.
    """
    row = {
        'Symbol': 'TESTCO', 'raw_symbol': 'TESTCO', 'raw_current_price': 100.0,
        'raw_segment_code': 'NSE_EQ', 'raw_all_benchmarks': [],
        'raw_day_events': [], 'raw_levels': {}, 'raw_breaches': [],
        'raw_day_breach_count': 0,
    }
    for stem in ('gap', 'early_return', 'vwap', 'block_volume',
                 'session_cumulative_volume', 'pvd', 'session_return'):
        for i, t in enumerate(('22d', '90d', '180d', '252d')):
            row[f'raw_{stem}_z_{t}'] = 1.0 + i          # 1.0 / 2.0 / 3.0 / 4.0
            row[f'raw_{stem}_avg_{t}'] = 0.1
            row[f'raw_{stem}_std_{t}'] = 0.5
            row[f'raw_{stem}_data_points_{t}'] = 100
    row.update(overrides)
    return row


def _styles(node, out=None):
    out = [] if out is None else out
    st = getattr(node, 'style', None)
    if st is not None:
        out.append(st)
    ch = getattr(node, 'children', None)
    if isinstance(ch, list):
        for c in ch:
            _styles(c, out)
    elif ch is not None and not isinstance(ch, str):
        _styles(ch, out)
    return out


def _texts(node, out=None):
    out = [] if out is None else out
    ch = getattr(node, 'children', None)
    if isinstance(ch, str):
        out.append(ch)
    elif isinstance(ch, list):
        for c in ch:
            _texts(c, out)
    elif ch is not None:
        _texts(ch, out)
    return out


# --------------------------------------------------------------------------- #
# The drawer emphasises, it does not collapse
# --------------------------------------------------------------------------- #

class TestDrawerEmphasises:

    @pytest.mark.parametrize('tier', ['22d', '90d', '180d', '252d', None])
    def test_a_tier_block_always_renders_all_four_tier_rows(self, tier):
        """THE load-bearing assertion of this file.

        The drawer must never collapse to the selected tier. If it did, the
        divergence that four tiers exist to expose would have nowhere to be
        seen — and the loss would be silent, because the remaining number would
        look perfectly reasonable.

        Asserted against `_tier_block` DIRECTLY, not by scanning the rendered
        panel for tier labels. The first version of this test did the latter and
        passed under a collapse mutation, because '22d'/'90d' also appear in the
        Volume Dynamics card's own grid and the RS matrix header — the assertion
        was satisfied by unrelated parts of the page. Row count makes collapse
        unrepresentable.
        """
        rows = DS._tier_block(_z_row(), 'gap', "Gap Z-Scores", tier)
        assert len(rows) == 7, (
            "expected title + header + 4 tier rows + Δ row; a different count "
            "means a tier row was dropped or added"
        )

    @pytest.mark.parametrize('tier', ['22d', '90d', '180d', '252d'])
    def test_every_tier_label_appears_in_the_block(self, tier):
        rows = DS._tier_block(_z_row(), 'gap', "Gap Z-Scores", tier)
        labels = [t for r in rows for t in _texts(r)]
        for t in ('22d', '90d', '180d', '252d'):
            assert t in labels, f"tier {t} vanished at selection {tier}"

    def test_exactly_one_row_per_block_is_emphasised(self):
        """Seven tier blocks plus the Volume Dynamics card's tier grid -> eight
        emphasised rows, twenty-four muted. The Volume Dynamics grid was the one
        that never took part: its loop variable overwrote its `tier` argument."""
        styles = _styles(D.create_detail_panel(_z_row(), 2.0, '22d'))
        active = sum(1 for s in styles if s == DS._GRID_ACTIVE_ROW_STYLE)
        muted = sum(1 for s in styles if s == DS._GRID_MUTED_ROW_STYLE)
        assert (active, muted) == (8, 24)

    def test_no_selection_renders_every_tier_neutrally(self):
        """So any caller that does not pass a tier is pixel-identical to before
        the emphasis mechanism existed."""
        styles = _styles(D.create_detail_panel(_z_row(), 2.0, None))
        assert not any(s == DS._GRID_ACTIVE_ROW_STYLE for s in styles)
        assert not any(s == DS._GRID_MUTED_ROW_STYLE for s in styles)

    def test_emphasis_is_visually_distinguishable_from_muted(self):
        """A 'distinction' both sides of which render identically is not one."""
        assert DS._GRID_ACTIVE_ROW_STYLE['color'] != DS._GRID_MUTED_ROW_STYLE['color']

    def test_volume_block_keeps_its_log_header(self):
        """Volume Block is the only calculator z-scoring a log-transformed
        quantity, so its mu/sigma are labelled 'log'. Collapsing all seven
        blocks through one helper must not flatten that away — before the label
        existed, a stored row reported z=+0.2033 while its own displayed
        mu/sigma implied -0.1089."""
        texts = _texts(D.create_detail_panel(_z_row(), 2.0, '90d'))
        assert 'μ log' in texts and 'σ log' in texts
        assert 'μ' in texts, "the non-log blocks lost their plain header"


class TestCardsFollowTierAndSlider:
    """Card accents and colours answer the same question as the row highlight:
    |z| at the SELECTED tier against the z-slider."""

    _AMBER, _RED, _GREEN_CELL = '#e8a447', '#d94560', 'rgba(95, 184, 124, 0.25)'

    def test_volume_block_accent_reads_the_selected_tier(self):
        row = _z_row(raw_block_volume_z_22d=3.1, raw_block_volume_z_90d=0.2)
        assert self._AMBER in DS.build_volume_block_card(row, 2.0, '22d').style['borderTop']
        assert self._AMBER not in DS.build_volume_block_card(row, 2.0, '90d').style['borderTop']

    def test_an_unusually_quiet_block_accents_the_card(self):
        """The row rule is |z| > threshold. A signed `z > threshold` lit the
        row for z = -2.6 and left this card plain."""
        row = _z_row(raw_block_volume_z_22d=-2.6)
        assert self._AMBER in DS.build_volume_block_card(row, 2.0, '22d').style['borderTop']

    def test_volume_dynamics_marks_and_follows_the_selected_tier(self):
        row = {'raw_expansion_state_22d': -1, 'raw_expansion_state_90d': 2}
        card = DS.build_volume_dynamics_card(row, 2.0, '22d')
        active = [c for c in card.children
                  if getattr(c, 'style', None) == DS._GRID_ACTIVE_ROW_STYLE]
        assert len(active) == 1 and _texts(active[0])[0] == '22d'
        assert self._RED in card.style['borderTop']       # 22d contracting, not 90d expanding

    def test_rs_cell_follows_the_slider(self):
        row = _z_row(raw_all_benchmarks=[{'benchmark_name': 'Nifty', 'intraday_rs_z_22d': 2.2}])

        def coloured(threshold):
            styles = _styles(DS.build_relative_strength_card(row, threshold, '22d'))
            return any(s.get('backgroundColor') == self._GREEN_CELL for s in styles)

        assert coloured(2.0)
        assert not coloured(2.5)


# --------------------------------------------------------------------------- #
# Cross-tier divergence (Δ) — the value and the drawer's Δ row
# --------------------------------------------------------------------------- #

class TestDivergence:

    def test_short_minus_long(self):
        """z22 = 1.0, z252 = 4.0 -> Delta = -3.0. Negative means the signal is
        LESS unusual recently than historically."""
        assert DS._divergence(_z_row(), 'gap') == pytest.approx(-3.0)

    def test_falls_back_to_180d_when_252d_is_absent(self):
        """252d needs ~a year of same-block history, so it is NULL for months
        after a fresh start. Without this fallback every asset's Delta would be
        blank for that entire period — the feature would appear broken."""
        row = _z_row(raw_gap_z_252d=None)
        assert DS._divergence(row, 'gap') == pytest.approx(-2.0)

    def test_absent_when_no_long_tier_is_populated(self):
        row = _z_row(raw_gap_z_252d=None, raw_gap_z_180d=None)
        assert DS._divergence(row, 'gap') is None

    def test_absent_when_the_short_tier_is_absent(self):
        row = _z_row(raw_gap_z_22d=None)
        assert DS._divergence(row, 'gap') is None

    def test_prefers_252d_over_180d_when_both_exist(self):
        """The widest available contrast, not merely the first one found."""
        assert DS._DIV_LONG_TIERS[0] == '252d'
        assert DS._divergence(_z_row(), 'gap') == pytest.approx(-3.0)

    def test_sign_is_preserved_in_both_directions(self):
        """Delta is signed and both signs are meaningful: positive = unusual
        recently but not historically; negative = the reverse. Nothing may
        abs() this."""
        rising = _z_row()                                    # z22 < z252
        falling = _z_row(**{f'raw_gap_z_{t}': v for t, v in
                            zip(('22d', '90d', '180d', '252d'), (4.0, 3.0, 2.0, 1.0))})
        assert DS._divergence(rising, 'gap') < 0
        assert DS._divergence(falling, 'gap') > 0

    def test_a_delta_row_renders_in_every_tier_block(self):
        texts = _texts(D.create_detail_panel(_z_row(), 2.0, '22d'))
        assert texts.count('Δ 22d−long') == 7

    def test_delta_row_shows_N_A_rather_than_zero_when_absent(self):
        """A fabricated 0.0 would read as 'the tiers agree', which is a claim
        the data does not support when one of them does not exist."""
        row = _z_row(**{f'raw_{s}_z_252d': None for s in
                        ('gap', 'early_return', 'vwap', 'block_volume',
                         'session_cumulative_volume', 'pvd', 'session_return')},
                     **{f'raw_{s}_z_180d': None for s in
                        ('gap', 'early_return', 'vwap', 'block_volume',
                         'session_cumulative_volume', 'pvd', 'session_return')})
        assert DS._divergence(row, 'gap') is None
        texts = _texts(D.create_detail_panel(row, 2.0, '22d'))
        assert 'N/A' in texts


# --------------------------------------------------------------------------- #
# Index breadth RS counted per tier
# --------------------------------------------------------------------------- #

import dataclasses

import pandas as pd

from metrics import IndexAggregator

#: RS values whose SIGN differs by tier — the whole reason this went per-tier.
#: A fixture where every tier agreed would pass even if the code still read
#: session_rs_90d four times over.
_RS_BY_TIER = {
    1: {'22d':  0.004, '90d':  0.001, '180d': -0.003, '252d':  0.002},
    2: {'22d':  0.002, '90d': -0.001, '180d': -0.002, '252d': -0.001},
    3: {'22d': -0.003, '90d': -0.002, '180d':  0.004, '252d':  0.003},
    4: {'22d': None,   '90d':  0.005, '180d':  0.001, '252d': None},   # partial
}


class _Loader:
    def get_index_constituents(self, csv_key):
        return [(a, 'NSE_EQ', 10.0) for a in _RS_BY_TIER] if csv_key == 'NIFTY' else []


class _Prices:
    def is_ready(self):
        return True

    def get_current_prices(self, ids, segments):
        return pd.DataFrame()


def _index_summary():
    latest = {'momentum': [], 'relative_strength': []}
    for aid, per_tier in _RS_BY_TIER.items():
        latest['momentum'].append({
            'asset_id': aid, 'segment_code': 'NSE_EQ', 'symbol': f'S{aid}',
            'timestamp': 't', 'current_price': 101.0, 'previous_close': 100.0,
        })
        row = {'asset_id': aid, 'segment_code': 'NSE_EQ', 'timestamp': 't',
               'benchmark_name': 'Nifty'}
        for t, v in per_tier.items():
            if v is not None:
                row[f'session_rs_{t}'] = v
        latest['relative_strength'].append(row)
    return IndexAggregator(_Loader()).compute(latest, _Prices())['NIFTY']


class TestRsBreadthPerTier:

    @pytest.mark.parametrize('tier,expected', [
        ('22d',  (2, 1, 3)),   # a4 has no 22d value
        ('90d',  (2, 2, 4)),
        ('180d', (2, 2, 4)),
        ('252d', (2, 1, 3)),   # a4 has no 252d value
    ])
    def test_counts_are_computed_per_tier(self, tier, expected):
        """Hand-computed from _RS_BY_TIER, not from the code under test."""
        s = _index_summary()
        got = (getattr(s, f'positive_rs_{tier}'),
               getattr(s, f'negative_rs_{tier}'),
               getattr(s, f'rs_evaluated_{tier}'))
        assert got == expected

    def test_the_counts_actually_differ_between_tiers(self):
        """Guards the whole mechanism. If the code still read session_rs_90d
        for every tier, all four would be identical and every assertion above
        could still pass against a less careful fixture."""
        s = _index_summary()
        seen = {(getattr(s, f'positive_rs_{t}'), getattr(s, f'negative_rs_{t}'))
                for t in ('22d', '90d', '180d', '252d')}
        assert len(seen) > 1, "tier had no effect on the RS breadth counts"

    def test_a_constituent_missing_that_tier_is_not_evaluated(self):
        """Absence is not a zero. Constituent 4 has no 22d value, so it must
        not count as 'evaluated and neither positive nor negative'."""
        s = _index_summary()
        assert s.rs_evaluated_22d == 3
        assert s.rs_evaluated_90d == 4

    def test_untiered_fields_are_gone(self):
        """Reintroducing `positive_rs` would fail nothing else — build_index_card
        reads via .get(), so it would silently prefer a stale untiered value."""
        declared = {f.name for f in dataclasses.fields(_index_summary())}
        for gone in ('positive_rs', 'negative_rs', 'rs_evaluated'):
            assert gone not in declared

    def test_a_constituent_is_never_both_positive_and_negative(self):
        s = _index_summary()
        for t in ('22d', '90d', '180d', '252d'):
            pos = getattr(s, f'positive_rs_{t}')
            neg = getattr(s, f'negative_rs_{t}')
            assert pos + neg <= getattr(s, f'rs_evaluated_{t}')


# --------------------------------------------------------------------------- #
# The ↗ glyph marks a sharp shift in participation
# --------------------------------------------------------------------------- #

import re


def _signals_html(velocity=None, expansion_state=None, threshold=2.0, tier='90d'):
    row = {'asset_id': 1, 'segment_code': 'NSE_EQ', 'symbol': 'T',
           'current_price': 100.0, 'day_events': [], 'all_benchmarks': []}
    if velocity is not None:
        row[f'velocity_{tier}'] = velocity
    if expansion_state is not None:
        row[f'expansion_state_{tier}'] = expansion_state
    td = D.build_table_data([row], z_threshold=threshold, tier=tier)[0]
    return dict((g, c) for c, g in
                re.findall(r'color:(#[0-9a-fA-F]{6});[^>]*>(.)</span>', td['Signals']))


def _arrow_colour(**kw):
    return _signals_html(**kw).get('↗')


class TestVelocityGlyph:

    def test_lights_on_a_sharp_shift_in_either_direction(self):
        assert _arrow_colour(velocity=3.1) != D._GLYPH_INACTIVE
        assert _arrow_colour(velocity=-2.6) != D._GLYPH_INACTIVE

    def test_stays_dark_for_an_ordinary_shift(self):
        """The whole point. The old predicate (`bool(expansion_state)`) lit
        99.9% of the time — expansion_state returns 0 only when velocity is
        EXACTLY 0.0, and velocity is a continuous float. A permanently-lit
        glyph carries no information in a glance-scan column."""
        for v in (0.4, 0.0, -0.4, 1.9):
            assert _arrow_colour(velocity=v) == D._GLYPH_INACTIVE, (
                f"velocity={v} should not light the glyph at threshold 2.0"
            )

    def test_a_mild_expansion_state_no_longer_lights_it(self):
        """expansion_state = ±1 used to light it. It is a SIGN CLASSIFICATION,
        roughly a coin flip, so no threshold on it can ever be rare — even
        |state| == 2 still lights 48.9% of the time."""
        assert _arrow_colour(velocity=0.3, expansion_state=1) == D._GLYPH_INACTIVE
        assert _arrow_colour(velocity=0.3, expansion_state=2) == D._GLYPH_INACTIVE

    def test_colour_carries_the_direction_the_arrow_cannot(self):
        """`↗` points up for a collapse exactly as for an expansion — the shape
        cannot distinguish them, so the hue must."""
        assert _arrow_colour(velocity=3.1) == '#5fb87c'    # expanding
        assert _arrow_colour(velocity=-3.1) == '#d94560'   # contracting

    def test_follows_the_z_slider_not_a_private_threshold(self):
        """Velocity is a difference of z-scores, so it is already in sigma and
        the slider's units apply directly. A glyph with its own threshold would
        drift out of agreement with its neighbours the moment the slider moved."""
        assert _arrow_colour(velocity=1.8, threshold=2.5) == D._GLYPH_INACTIVE
        assert _arrow_colour(velocity=1.8, threshold=1.5) != D._GLYPH_INACTIVE

    def test_absent_velocity_is_not_lit(self):
        assert _arrow_colour(velocity=None, expansion_state=2) == D._GLYPH_INACTIVE

    def test_the_column_has_nine_slots(self):
        """Fixed slot positions are what make the column pattern-matchable."""
        assert len(D._SIGNAL_GLYPHS) == 9
        assert len(_signals_html(velocity=1.0)) == 9

    def test_every_slot_takes_its_glyph_and_colour_from_the_one_map(self):
        """The slots used to carry their own copies of the glyphs and colours,
        agreeing with _HEALTH_GLYPH_MAP only by hand; and the α slot alone got
        the RS aggregate, picked out by its position in the tuple."""
        keys = [key for key, _, _ in D._SIGNAL_GLYPHS]      # slot order is this tuple's
        assert len(keys) == 9 and set(keys) == set(D._HEALTH_GLYPH_MAP)
        cell = D._build_signals_cell({'day_events': []}, max_abs_intraday_rs_z=5.0,
                                     z_threshold=2.0, tier='22d')
        colours = dict((g, c) for c, g in
                       re.findall(r'color:(#[0-9a-fA-F]{6});[^>]*>(.)</span>', cell))
        glyph, colour = D._HEALTH_GLYPH_MAP['relative_strength']
        assert colours[glyph] == colour


class TestEveryLitRowShowsWhy:
    """Every rule that lights a row has a glyph slot, and the why-line lists
    only signals that light a row. VWAP z and Rolling Momentum z used to light
    a row with no slot of their own, so the row showed only dark glyphs; the
    why-line listed gap z, which lights no row, under the early-return glyph."""

    @staticmethod
    def _glyphs(tier='22d', **fields):
        row = {'asset_id': 1, 'segment_code': 'NSE_EQ', 'symbol': 'T',
               'current_price': 100.0, 'day_events': [], 'all_benchmarks': []}
        row.update({f'{k}_{tier}': v for k, v in fields.items()})
        td = D.build_table_data([row], z_threshold=2.0, tier=tier)[0]
        return td, dict((g, c) for c, g in
                        re.findall(r'color:(#[0-9a-fA-F]{6});[^>]*>(.)</span>', td['Signals']))

    def test_vwap_z_lights_its_own_glyph(self):
        _, glyphs = self._glyphs(vwap_z=2.6)
        assert glyphs['‖'] != D._GLYPH_INACTIVE

    def test_rolling_momentum_z_lights_its_own_glyph(self):
        _, glyphs = self._glyphs(session_return_z=-2.4)
        assert glyphs['↻'] != D._GLYPH_INACTIVE

    def test_both_stay_dark_for_ordinary_values(self):
        _, glyphs = self._glyphs(vwap_z=1.0, session_return_z=-1.0)
        assert glyphs['‖'] == D._GLYPH_INACTIVE
        assert glyphs['↻'] == D._GLYPH_INACTIVE

    def test_gap_z_is_not_on_the_why_line(self):
        """Gap z lights no row and is fixed at 09:15 all day — it stays on the
        Momentum card only."""
        td, _ = self._glyphs(gap_z=3.1)
        assert getattr(D._build_why_line(td, 2.0, '22d'), 'children', None) is None


# --------------------------------------------------------------------------- #
# 'window_missed' distinguishes a missed open from "not ready yet"
# --------------------------------------------------------------------------- #

from datetime import datetime, time as _dtime
from zoneinfo import ZoneInfo

from calculators.relative_strength_adapter import run_relative_strength_analysis

_IST = ZoneInfo("Asia/Kolkata")


def _rs_status(hour, minute, window_prices):
    """Run the real RS adapter against the tiered-RS stubs."""
    from tests.test_relative_strength_tiers import _Stub, _Loader
    from calculators.statistics import MAX_TIER
    stub = _Stub(MAX_TIER, window_prices=window_prices)
    now = datetime(2026, 8, 21, hour, minute, tzinfo=_IST)
    rows = run_relative_strength_analysis(stub, stub, stub,
                                          asset_loader=_Loader(), now=now)
    return rows[0].status


class TestWindowMissed:
    """Momentum and RS pin to block 1 (09:15->09:30). A late engine start leaves
    that window empty TODAY, and both go blank for the whole session with no
    recovery — observed live 2026-08-11, first tick 11:49.

    The blank was three situations wearing one appearance: not 09:30 yet, the
    engine missed the open, and insufficient history. Only the second is a
    fault, and it was invisible.
    """

    def test_before_the_window_closes_is_not_a_missed_window(self):
        """At 09:20 there is legitimately no window price yet. Flagging that as
        a fault would cry wolf every single morning."""
        assert _rs_status(9, 20, window_prices=True) != 'window_missed'

    def test_after_the_window_with_ticks_is_normal(self):
        assert _rs_status(11, 0, window_prices=True) == 'valid'

    def test_after_the_window_with_no_ticks_is_flagged(self):
        """THE case. The window has passed and nothing was captured — this
        asset cannot produce an opening measurement for the rest of the day."""
        assert _rs_status(11, 0, window_prices=False) == 'window_missed'

    def test_the_two_blank_states_are_distinguishable(self):
        """Before the fix both rendered as the same empty cell, so a trader
        seeing a blank Momentum column at 14:00 could not tell 'not ready yet'
        from 'will never be ready today'."""
        assert _rs_status(9, 20, window_prices=False) != _rs_status(11, 0, window_prices=False)

    def test_no_substitute_window_is_measured(self):
        """A late start must NOT be papered over by measuring some other
        window. That would answer a different question under the same field
        name — the class of silent lie this whole body of work removes."""
        from tests.test_relative_strength_tiers import _Stub, _Loader
        from calculators.statistics import MAX_TIER
        stub = _Stub(MAX_TIER, window_prices=False)
        rows = run_relative_strength_analysis(
            stub, stub, stub, asset_loader=_Loader(),
            now=datetime(2026, 8, 21, 11, 0, tzinfo=_IST))
        row = rows[0]
        for tier in ('22d', '90d', '180d', '252d'):
            assert getattr(row, f'intraday_rs_{tier}') is None, (
                "an intraday RS was produced despite the opening window being "
                "missed — a substitute window was measured"
            )
        # session_rs needs only the open price, so it legitimately survives.
        assert row.session_rs_90d is not None


# --------------------------------------------------------------------------- #
# The RS card explains its own numbers
# --------------------------------------------------------------------------- #

_BETA = {'22d': 0.97, '90d': 1.03, '180d': 1.11, '252d': 1.24}
_SRS = {'22d': 0.0044, '90d': 0.0051, '180d': 0.0058, '252d': 0.0066}
_IRS = {'22d': 0.0027, '90d': 0.0032, '180d': 0.0037, '252d': 0.0041}
_ZRS = {'22d': 2.80, '90d': 0.30, '180d': -2.10, '252d': 0.90}


def _rs_row(name='Nifty'):
    bm = {'benchmark_name': name}
    for t in _BETA:
        bm[f'beta_{t}'] = _BETA[t]
        bm[f'session_rs_{t}'] = _SRS[t]
        bm[f'intraday_rs_{t}'] = _IRS[t]
        bm[f'intraday_rs_z_{t}'] = _ZRS[t]
    return {'raw_all_benchmarks': [bm], 'raw_intraday_rs_90d': _IRS['90d']}


def _card_rows(tier='22d', row=None):
    card = DS.build_relative_strength_card(row or _rs_row(), 2.0, tier)
    return card.children[1:]          # skip the H5 card title


class TestRsCardPerTierTable:

    def test_beta_sits_on_the_same_row_as_the_alpha_z_it_produced(self):
        """THE point of the restructure. Before it, the alpha-z matrix showed
        four tiers while a separate context block explained only 90d — so a
        trader reading the 22d cell could not see it was measured with
        beta_22 = 0.97 rather than beta_90 = 1.03. The number that explained
        the cell was not on the card.
        """
        for r in _card_rows():
            vals = _texts(r)
            if vals and vals[0] == '22d':
                assert '+0.970' in vals, "beta_22 missing from the 22d row"
                assert '+2.80' in vals, "the 22d alpha-z is not on its own row"
                assert '+1.030' not in vals, "a different tier's beta leaked in"
                return
        pytest.fail("no 22d row rendered")

    @pytest.mark.parametrize('tier', ['22d', '90d', '180d', '252d'])
    def test_every_tier_row_is_internally_coherent(self, tier):
        """Each row is ONE measurement: this beta, this RS, this z. Mixing
        tiers within a row would imply a relationship that does not exist —
        RS measured with beta_22 has no business beside a z normalised against
        a beta_90 distribution."""
        for r in _card_rows(tier):
            vals = _texts(r)
            if vals and vals[0] == tier:
                assert f"{_BETA[tier]:+.3f}" in vals
                assert f"{_SRS[tier] * 100:+.2f}%" in vals
                assert f"{_IRS[tier] * 100:+.2f}%" in vals
                assert f"{_ZRS[tier]:+.2f}" in vals
                return
        pytest.fail(f"no {tier} row rendered")

    def test_all_four_tiers_render_regardless_of_selection(self):
        """Same rule as the other tier grids: emphasise, never collapse."""
        for sel in ('22d', '90d', '180d', '252d'):
            labels = [v for r in _card_rows(sel) for v in _texts(r)]
            for t in ('22d', '90d', '180d', '252d'):
                assert t in labels

    def test_the_selected_tier_row_is_emphasised(self):
        for sel in ('22d', '180d'):
            active = [r for r in _card_rows(sel)
                      if getattr(r, 'style', None) == DS._GRID_ACTIVE_ROW_STYLE]
            assert len(active) == 1, f"expected exactly one emphasised row at {sel}"
            assert _texts(active[0])[0] == sel

    def test_no_selection_emphasises_nothing(self):
        assert not [r for r in _card_rows(None)
                    if getattr(r, 'style', None) == DS._GRID_ACTIVE_ROW_STYLE]

    def test_the_separate_context_block_is_gone(self):
        """It explained only 90d and is what the per-tier table replaced."""
        labels = [v for r in _card_rows() for v in _texts(r)]
        assert not any('Context' in str(v) for v in labels)
        assert not any('β 90d' == v for v in labels)

    def test_a_second_benchmark_gets_its_own_table(self):
        """A stock in NIFTY and SENSEX emits one RS row per benchmark, and each
        needs its own beta column — betas differ per benchmark."""
        row = _rs_row()
        row['raw_all_benchmarks'] = [row['raw_all_benchmarks'][0], _rs_row('Sensex')['raw_all_benchmarks'][0]]
        labels = [v for r in _card_rows(row=row) for v in _texts(r)]
        assert any('Nifty' in str(v) for v in labels)
        assert any('Sensex' in str(v) for v in labels)
        assert labels.count('22d') == 2, "each benchmark needs its own tier rows"

    def test_renders_without_benchmarks(self):
        """Index rows carry no benchmarks — the card must still build."""
        card = DS.build_relative_strength_card({'raw_all_benchmarks': []}, 2.0, '22d')
        assert card is not None


# --------------------------------------------------------------------------- #
# Cross-tier divergence (Δ) — the scores-table column
# --------------------------------------------------------------------------- #

def _table_row(symbol='T', **fields):
    row = {'asset_id': 1, 'segment_code': 'NSE_EQ', 'symbol': symbol,
           'current_price': 100.0, 'day_events': [], 'all_benchmarks': []}
    row.update(fields)
    return row


def _div_of(**fields):
    td = D.build_table_data([_table_row(**fields)], z_threshold=2.0, tier='90d')[0]
    return td['Div']


class TestDivergenceColumn:

    def test_value_is_the_max_absolute_delta_across_signals(self):
        """A FINDING tool: a stock surfaces if ANY signal disagrees across
        horizons. A mean would wash one strong disagreement out against six
        quiet ones — the opposite of what a scan wants."""
        v = _div_of(gap_z_22d=1.0, gap_z_252d=0.8,          # Δ 0.2
                    pvd_z_22d=4.0, pvd_z_252d=-6.5)          # Δ 10.5  <- max
        assert v == pytest.approx(10.5)

    def test_the_value_is_a_magnitude(self):
        """Direction lives in the drawer's per-signal Δ rows; the column ranks
        by how much the horizons disagree, either way."""
        assert _div_of(gap_z_22d=-4.0, gap_z_252d=1.0) == pytest.approx(5.0)

    def test_uses_the_180d_fallback(self):
        assert _div_of(gap_z_22d=3.0, gap_z_252d=None, gap_z_180d=1.0) == pytest.approx(2.0)

    def test_absent_when_nothing_can_be_compared(self):
        assert _div_of(gap_z_22d=None, gap_z_252d=None) is None

    def test_relative_strength_is_aggregated_across_benchmarks(self):
        """A stock in NIFTY and SENSEX carries one α-z per benchmark."""
        v = _div_of(all_benchmarks=[
            {'benchmark_name': 'Nifty',  'intraday_rs_z_22d': 1.0, 'intraday_rs_z_252d': 0.5},
            {'benchmark_name': 'Sensex', 'intraday_rs_z_22d': 3.0, 'intraday_rs_z_252d': -1.5},
        ])
        assert v == pytest.approx(4.5)

    # ---- the numeric-sort requirement ------------------------------------- #

    def test_the_value_is_a_number_not_a_formatted_string(self):
        """THE correctness requirement of this column.

        `sort_action='native'` sorts TEXT lexicographically. As a string,
        "10.5" sorts before "2.1", so the column would confidently rank the
        LEAST divergent stocks first — worse than not having it.
        """
        v = _div_of(gap_z_22d=4.0, gap_z_252d=-6.5)
        assert isinstance(v, float), f"Div must be numeric, got {type(v).__name__}"

    def test_the_column_is_declared_numeric(self):
        cols = {c['id']: c for c in D.create_main_table().columns}
        assert cols['Div']['type'] == 'numeric'

    def test_lexicographic_and_numeric_orders_genuinely_differ(self):
        """Proves the previous two assertions are not merely decorative — with
        these values the wrong type produces a visibly wrong ranking."""
        vals = [10.5, 2.1, 2.0, 0.2]
        assert sorted(vals, reverse=True) != [
            float(s) for s in sorted([str(v) for v in vals], reverse=True)
        ]

    def test_absent_stays_None_rather_than_becoming_zero(self):
        """0.0 would mean 'the horizons agree perfectly', a claim the data does
        not support when one tier does not exist — and it would sort among the
        quiet stocks rather than being absent."""
        assert _div_of(gap_z_22d=None) is None

    def test_the_column_renders_in_the_table(self):
        cols = [c['id'] for c in D.create_main_table().columns]
        assert 'Div' in cols
        assert cols.index('Div') > cols.index('Symbol')

    def test_column_and_drawer_report_the_same_number(self):
        """They share `_divergence`. If a column that sorted you to a stock
        disagreed with the drawer you opened, the column would be untrustworthy."""
        raw = {f'raw_gap_z_22d': 4.0, f'raw_gap_z_252d': -6.5}
        assert DS._divergence(raw, 'gap') == pytest.approx(10.5)
        assert _div_of(gap_z_22d=4.0, gap_z_252d=-6.5) == pytest.approx(10.5)
