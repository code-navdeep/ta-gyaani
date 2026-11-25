"""
Breakout adapter: prices stay on their own timestamps, and one degenerate asset
cannot blank Breakout for the rest.

- Non-positive prices are masked out of the FRAME, and prices and timestamps are
  extracted from the same rows. Filtering the prices alone and zipping them
  against the unfiltered timestamps put every later price on the wrong time, so
  the post-range series — which decides direction, acceptance and failure — was
  wrong.
- A flat opening range (one price all through 09:15-09:45) has zero width, so
  the position is undefined. `abs(None)` used to raise, and because the whole
  run sits in one try in metrics.py, that one stock blanked Breakout for every
  asset. Each asset now has its own try, and the flat asset still gets a row.
"""
from datetime import datetime

import pandas as pd

from calculators.breakout_adapter import run_breakout_analysis
from calculators.time_blocks import IST

NOW = datetime(2026, 8, 21, 11, 0, tzinfo=IST)

TRENDING = [('09:16', 100.0), ('09:30', 102.0), ('09:44', 101.0),
            ('09:50', 101.5), ('10:00', 103.0), ('10:05', 104.0)]
WITH_ZERO = TRENDING[:1] + [('09:20', 0.0)] + TRENDING[1:]
FLAT = [('09:16', 100.0), ('09:30', 100.0), ('09:44', 100.0),
        ('09:50', 101.0), ('10:00', 102.0)]


class _Ohlc:
    def __init__(self, series_by_asset):
        self.series = series_by_asset

    def get_session_prices(self, asset_id, segment_code, now):
        return pd.DataFrame([
            {'timestamp': f'2026-08-21T{hhmm}:00+05:30', 'last_price': price}
            for hhmm, price in self.series[asset_id]
        ])


class _Loader:
    def __init__(self, ids):
        self.assets_df = pd.DataFrame(
            [{'asset_id': a, 'segment_code': 'NSE_EQ', 'symbol': f'S{a}'} for a in ids])


def _rows(series_by_asset):
    rows = run_breakout_analysis(_Ohlc(series_by_asset), None,
                                 asset_loader=_Loader(list(series_by_asset)), now=NOW)
    return {r.asset_id: r for r in rows}


_FIELDS = ('current_price', 'range_high', 'range_low', 'range_width',
           'breakout_position', 'fresh_breakout', 'breakout_direction',
           'accepted_breakout', 'failed_breakout', 'distance_from_range', 'status')


def test_a_zero_price_does_not_shift_the_rest_onto_wrong_timestamps():
    rows = _rows({1: TRENDING, 2: WITH_ZERO})
    for field in _FIELDS:
        assert getattr(rows[2], field) == getattr(rows[1], field), field
    assert rows[1].breakout_direction == 1


def test_a_flat_opening_range_emits_a_row_and_spares_the_others():
    rows = _rows({1: FLAT, 2: TRENDING})
    assert set(rows) == {1, 2}
    assert rows[1].range_width == 0.0
    assert rows[1].breakout_position is None
    assert rows[1].distance_from_range is None
    assert rows[2].breakout_direction == 1


def test_a_stock_with_no_priced_reading_yet_gets_a_row_saying_so():
    """No reading today, or none with a price: a row with status
    'insufficient_data' and every value empty — the flags too, since 0 would
    claim "not in breakout" — rather than no row, which read as a dead
    calculator on the health bar. The others are unaffected."""
    rows = _rows({1: [], 2: [('09:16', 0.0)], 3: TRENDING})
    assert set(rows) == {1, 2, 3}
    for aid in (1, 2):
        assert rows[aid].status == 'insufficient_data'
        assert (rows[aid].breakout_now, rows[aid].breakout_direction,
                rows[aid].accepted_breakout, rows[aid].range_high) == (None, None, None, None)
    assert rows[3].status == 'valid'


# Opening range 09:15-09:45 is 100-102 in all three. Each breaks UP at 10:00.
_BROKE_UP = [('09:16', 100.0), ('09:30', 102.0), ('09:44', 101.0),
             ('09:50', 101.5), ('10:00', 103.0)]
FELL_BELOW = _BROKE_UP + [('10:15', 101.0), ('10:30', 99.0), ('10:45', 98.5)]
BACK_INSIDE = _BROKE_UP + [('10:30', 99.0), ('10:45', 101.0)]
ONE_BELOW = _BROKE_UP + [('10:30', 101.0), ('10:45', 99.0)]


class TestBreakoutNow:
    """`breakout_direction` is the day's first breakout and never clears — at
    15:00 on 2026-09-07 it was nonzero on 84 of 86 assets. `breakout_now` is
    "outside the range right now, on two readings in a row", and it is what
    the ▲ glyph, the row highlight, the why-line and the index counts use."""

    def test_the_first_breakout_is_kept_while_now_follows_price(self):
        row = _rows({1: FELL_BELOW})[1]
        assert row.breakout_direction == 1
        assert row.breakout_now == -1

    def test_back_inside_the_range_is_not_in_breakout(self):
        assert _rows({1: BACK_INSIDE})[1].breakout_now == 0

    def test_one_reading_outside_is_not_confirmed(self):
        assert _rows({1: ONE_BELOW})[1].breakout_now == 0

    def test_two_readings_above_is_in_breakout_up(self):
        assert _rows({1: TRENDING})[1].breakout_now == 1


def test_a_breakout_on_the_first_reading_after_0945_is_seen():
    """ASHOKLEY, 2026-09-04: opening range 168.61-170.23; 170.19 at 09:44:57,
    170.25 at 09:45:02, and above the range from then on. The first-breakout
    scan used to start at the first reading after 09:45, which has no earlier
    neighbour to cross from, so it recorded nothing until price dipped inside
    at 09:54:40 and crossed out again — ~10 minutes late. It now starts from
    the last opening-window reading."""
    ashokley = [('09:16', 168.61), ('09:30', 170.23), ('09:44', 170.19),
                ('09:45', 170.25), ('09:50', 170.38), ('09:55', 170.30)]
    row = _rows({1: ashokley})[1]
    assert row.breakout_direction == 1
    assert row.accepted_breakout == 1
