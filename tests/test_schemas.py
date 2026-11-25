"""
Schema shape tests — locked-calculator generation.

Guards the typed-schema layer:
  - Every Row class can be instantiated with all-default (None) fields.
  - `dataclasses.asdict()` round-trips faithfully.
  - Expected field counts remain stable — an accidental field add/removal in
    a Row class is caught before it silently reshapes downstream storage.
  - List/dict fields default to actual empty collections, never None.
"""
import dataclasses

import pytest

from calculators.schemas import (
    MomentumRow, BreakoutRow, VwapRow, RelativeStrengthRow,
    VolumeBlockRow, VolumeDynamicsRow, PriceVolumeDivergenceRow,
    PriceLevelsRow, RollingMomentumRow, IndexSummary,
    ALL_ROW_CLASSES, all_emitted_fields,
)


# Expected field counts per Row class. If a legitimate change adds/removes a
# field (e.g. building out the 22/180/252d tiers), update this table in the
# same commit as the Row-class edit. A drift without the table update is
# caught here.
_EXPECTED_FIELD_COUNTS = {
    # 49 -> 48: momentum_score removed — the mean of the gap z and early-return
    # z the card already shows, pinned to 90d while every surface follows the tier.
    MomentumRow: 48,
    # 15 -> 16: breakout_now added — outside the range right now, which the
    # scanning surfaces use; breakout_direction stays the day's first breakout.
    BreakoutRow: 16,
    VwapRow: 26,
    # 48 -> 57: dropped untiered session_rs/intraday_rs/acceptance (-3), added
    # all three at four tiers each (+12). Each is measured with its tier's beta,
    # so there is no single untiered value to keep.
    RelativeStrengthRow: 57,
    # 40 -> 41: session_cumulative_blocks — the session cumulative sums only
    # today's complete blocks, so the card says how many.
    VolumeBlockRow: 41,
    # 6 identity/status + 5 signals x 4 tiers = 26. Volume Dynamics is a pure
    # derivative of Volume Block's z, so it tiers exactly as Volume Block does.
    VolumeDynamicsRow: 26,
    # 30 -> 31: estimators_available added alongside estimator_agreement, which
    # is now None below 2 surviving estimators (one alone cannot "agree").
    PriceVolumeDivergenceRow: 31,
    PriceLevelsRow: 8,
    RollingMomentumRow: 24,
    # 32 -> 30: removed six stored spike counters (vol_spike_high/low +
    # zscore_90d_evaluated, session_return_z_spike_high/low +
    # session_return_z_evaluated) because they baked a threshold the dashboard
    # controls at display time (-6); weighted_volume_zscore gained its other
    # three tiers (+3); added constituent_z, the raw per-constituent z-values
    # the dashboard now counts from (+1).
    # 30 -> 39: RS breadth went per tier — dropped positive_rs / negative_rs /
    # rs_evaluated (-3), added all three at four tiers each (+12). They count a
    # SIGN, not a threshold crossing, so unlike the spike counters above they can
    # be stored once per tier rather than derived at display time.
    # 39 -> 42: weighted_volume_velocity went per tier (-1, +4). The single
    # field read VolumeDynamicsRow's non-existent `velocity` and was always NULL.
    IndexSummary: 42,
}


class TestFieldCounts:
    @pytest.mark.parametrize('cls,expected_count', list(_EXPECTED_FIELD_COUNTS.items()))
    def test_field_count_stable(self, cls, expected_count):
        actual = len(dataclasses.fields(cls))
        assert actual == expected_count, (
            f"{cls.__name__} field count drifted: {expected_count} → {actual}. "
            f"If intentional, update _EXPECTED_FIELD_COUNTS in this test file."
        )


class TestInstantiation:
    @pytest.mark.parametrize('cls', list(ALL_ROW_CLASSES))
    def test_all_defaults_ok(self, cls):
        """Every Row must be constructible with no args. Fields default to
        None (or empty list/dict via field(default_factory=...))."""
        instance = cls()
        assert instance is not None

    @pytest.mark.parametrize('cls', list(ALL_ROW_CLASSES))
    def test_asdict_returns_dict_with_all_fields(self, cls):
        """`dataclasses.asdict()` returns a dict keyed by every declared field name."""
        d = dataclasses.asdict(cls())
        expected_keys = {f.name for f in dataclasses.fields(cls)}
        assert set(d.keys()) == expected_keys


class TestListDictDefaults:
    """PriceLevelsRow and IndexSummary use `field(default_factory=list/dict)`
    for collection fields — verify defaults are actual empty collections,
    not None (would break `for x in row.day_events` at consumer sites)."""

    def test_price_levels_row_collection_defaults(self):
        pl = PriceLevelsRow()
        assert pl.levels == {}
        assert pl.day_events == []

    def test_index_summary_leadership_defaults(self):
        idx = IndexSummary()
        assert idx.top_contributors == []
        assert idx.bottom_detractors == []


class TestAllEmittedFieldsHelper:
    def test_returns_union_across_all_row_classes(self):
        emitted = all_emitted_fields()
        expected = set()
        for cls in ALL_ROW_CLASSES:
            expected.update(f.name for f in dataclasses.fields(cls))
        assert emitted == expected

    def test_contains_representative_fields(self):
        emitted = all_emitted_fields()
        # Sample: one field per Row class, chosen to catch class-omission regressions.
        for f in ('gap_return', 'breakout_direction', 'displacement',
                  'session_rs_z_90d', 'block_volume_z_90d', 'expansion_state_90d',
                  'pearson_r', 'day_events', 'ex_top5_intraday_return'):
            assert f in emitted, f"{f!r} missing from all_emitted_fields()"

    def test_no_bare_status_field_needed_by_dashboard(self):
        """Every Row class now declares a `status` field (unlike the old
        schema, where only 3 did) — dashboard.py must alias every one of
        them (via Signal.field_renames) rather than consuming a bare
        'status' key, or later-merged calculators silently clobber earlier
        ones' status in the per-asset merged row."""
        status_bearing = [cls for cls in ALL_ROW_CLASSES
                           if 'status' in {f.name for f in dataclasses.fields(cls)}]
        # Exactly the nine per-asset Rows; IndexSummary carries no status. A
        # floor like ">= 7" stayed green with two calculators' status gone.
        from calculators.schemas import IndexSummary
        assert set(status_bearing) == set(ALL_ROW_CLASSES) - {IndexSummary}, (
            "every per-asset Row class must carry a status field — if one "
            "dropped it, the field_renames aliasing requirement may be stale"
        )
