"""
Momentum's "price at 09:30" is the last traded price in the 09:15->09:30
window — never `close_price`.

`ohlc_data.close_price` is Dhan's PREVIOUS-session close, constant all day. The
window-price helper used to prefer it, so early_return came out as
ln(prev_close / open) — the overnight gap with its sign flipped. The stand-in
reader here returns both columns, as the real one does, with `close_price` set to
a plausible, different value, so reading the wrong column gives a visibly wrong
early return.
"""
from datetime import datetime
from math import log

import pandas as pd
import pytest

from calculators.momentum_adapter import run_momentum_analysis
from calculators.time_blocks import IST

ASSET, SEG = 1333, 'NSE_EQ'
PREV_CLOSE, OPEN, WINDOW_LAST = 100.0, 102.0, 103.0
NOW = datetime(2026, 8, 21, 11, 0, tzinfo=IST)


class _Loader:
    assets_df = pd.DataFrame([{'asset_id': ASSET, 'segment_code': SEG, 'symbol': 'TEST'}])


class _Stub:
    # price_data_manager
    def is_ready(self):
        return True

    def get_current_prices(self, ids, segments):
        return pd.DataFrame([{'asset_id': ASSET, 'segment_code': SEG,
                              'last_price': 104.0, 'open_price': OPEN,
                              'high_price': 105.0, 'low_price': 101.0}])

    # historical_db_manager
    def get_previous_close_data(self, ids, segments):
        return pd.DataFrame([{'asset_id': ASSET, 'segment_code': SEG,
                              'previous_close': PREV_CLOSE}])

    def get_historical_gap_data(self, ids, segments, days_back=None):
        return pd.DataFrame()

    def get_historical_intraday_metrics(self, ids, segments, **kwargs):
        return pd.DataFrame()

    # ohlc_db_manager
    def get_intraday_data_range(self, asset_id, start, end, segment_code=None):
        return pd.DataFrame([{'last_price': WINDOW_LAST, 'close_price': PREV_CLOSE}])


class _NoWindowTicks(_Stub):
    def get_intraday_data_range(self, asset_id, start, end, segment_code=None):
        return pd.DataFrame()


def _early_fields(row):
    return (row.early_return, row.continuation, row.acceleration,
            row.early_return_z_22d, row.early_return_z_90d)


def test_before_0930_nothing_is_measured_from_a_stand_in():
    """The open used to stand in for the 09:30 price, so early_return was
    ln(open/open) = 0 and its z was scored against real history."""
    at_0920 = datetime(2026, 8, 21, 9, 20, tzinfo=IST)
    stub = _NoWindowTicks()
    row = run_momentum_analysis(stub, stub, stub, asset_loader=_Loader(), now=at_0920)[0]
    assert _early_fields(row) == (None,) * 5
    assert row.status == 'window_not_closed'


def test_a_missed_window_says_so():
    stub = _NoWindowTicks()
    row = run_momentum_analysis(stub, stub, stub, asset_loader=_Loader(), now=NOW)[0]
    assert _early_fields(row) == (None,) * 5
    assert row.status == 'window_missed'


def test_early_return_is_measured_from_the_last_traded_price():
    stub = _Stub()
    row = run_momentum_analysis(stub, stub, stub, asset_loader=_Loader(), now=NOW)[0]
    assert row.price_after_window == WINDOW_LAST
    assert row.early_return == pytest.approx(log(WINDOW_LAST / OPEN))
    assert row.early_return != pytest.approx(log(PREV_CLOSE / OPEN)), (
        "early_return is the sign-flipped gap — the previous close was read as "
        "the price at 09:30"
    )
