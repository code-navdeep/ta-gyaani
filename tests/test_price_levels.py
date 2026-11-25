"""
Price Levels: blank CSV levels are skipped, and a level entry carries only the
fields the calculator can actually produce.

Levels are entered by users in `assets_select.csv`, and many assets have none.
pandas reads a blank cell as NaN; `float(NaN)` does not raise and `NaN <= 0` is
False, so each blank used to become a level of value NaN — 48 assets emitted a
`valid` row whose every distance was NaN. An asset with no levels now gets no
row at all.

`rejection` and `acceptance` were deleted: in 115,498 stored rows over
2026-09-04 and 2026-09-07 neither was ever 1, and by construction neither could
be.
"""
import io
import math
from datetime import datetime

import pandas as pd

from calculators.price_levels_adapter import run_price_levels_analysis
from calculators.time_blocks import IST

NOW = datetime(2026, 9, 7, 11, 0, tzinfo=IST)

_CSV = """asset_id,asset_name,segment_code,local_high,global_high,local_low,global_low
13,NIFTY,IDX_I,,,,
11536,TCS,NSE_EQ,2300,,,
"""


class _Loader:
    assets_df = pd.read_csv(io.StringIO(_CSV))


class _Ticks:
    def get_session_prices(self, asset_id, segment_code, now):
        return pd.DataFrame({
            'timestamp': ['2026-09-07T10:59:50+05:30', '2026-09-07T10:59:55+05:30'],
            'last_price': [2290.0, 2295.0],
        })


def _rows():
    return run_price_levels_analysis(_Ticks(), asset_loader=_Loader(), now=NOW)


def test_an_asset_with_no_levels_gets_no_row():
    assert [r.symbol for r in _rows()] == ['TCS']


def test_blank_levels_are_skipped_not_read_as_nan():
    (row,) = _rows()
    assert list(row.levels) == ['local_high']
    for entry in row.levels.values():
        for key, value in entry.items():
            assert not (isinstance(value, float) and math.isnan(value)), (
                f"{key} is NaN — a blank CSV cell was treated as a level")


def test_a_level_entry_carries_only_producible_fields():
    (row,) = _rows()
    assert set(row.levels['local_high']) == {
        'level_value', 'distance_pct', 'distance_log', 'side',
        'approached', 'crossed', 'status',
    }
