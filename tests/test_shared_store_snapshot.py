"""
The shared intraday store records each block's value AT ITS END, read from the
block's own stored readings.

The writer used to take its three values from the metrics cache on the first
cycle inside a bracket centred on the block end. With cycles ~34 s apart that
cycle fell in the bracket's first half: on 2026-09-07 every block was written
21-58 seconds before it ended. Momentum and Relative Strength rebuild "the price
at 09:30" from this store, and VWAP, Rolling Momentum and PV Divergence z-score
today against it — so every one of those histories held values from before the
block closed.

And a value is recorded only when the readings behind it can be trusted
(time_blocks.BLOCK_EDGE_TOLERANCE, 30 s): the price move and VWAP distance need
the block's last reading within 30 s of its end — after an outage it can be
minutes stale — and PV Divergence needs every reading within 30 s of the one
before it, never counting the day's first reading, whose interval holds the
whole running total so far.
"""
import math
import sqlite3
from datetime import datetime, timedelta

import pandas as pd
import pytest

import engine as engine_mod
import metrics
from calculators.database_manager import OHLCDatabaseReader
from calculators.price_volume_divergence_calculator import calculate_pvd_v3
from calculators.time_blocks import IST

ASSET, SEG = 11536, 'NSE_EQ'
PREV_CLOSE = 100.0


def _every_5_s(count):
    """(clock, price, exchange VWAP, interval volume): a reading every 5 s from
    09:15:05, as the engine takes them. The day's first reading holds the whole
    running total so far — 66,818 shares, TCS's pre-09:15 volume on 2026-08-28."""
    start = datetime(2026, 9, 7, 9, 15, 5)
    return [((start + timedelta(seconds=5 * i)).strftime('%H:%M:%S'),
             100.0 + 0.05 * ((i * 7) % 11),
             100.0 + 0.002 * i,
             66_818 if i == 0 else 400 + (i * 37) % 250)
            for i in range(count)]


# Block 1 is 09:15-09:30: readings 09:15:05 … 09:29:55. The 09:30:00 reading is
# stamped AT the end, so it belongs to block 2.
_BLOCK_1 = _every_5_s(179)
_AT_THE_END = ('09:30:00', 150.0, 100.90, 900)


class _Writer:
    def __init__(self):
        self.rows = []

    def store_intraday_time_block_metrics(self, rows):
        self.rows.extend(rows)
        return len(rows)


class _Loader:
    assets_df = pd.DataFrame([{'asset_id': ASSET, 'segment_code': SEG, 'asset_name': 'TCS'}])


def _at(clock):
    h, m, s = (int(x) for x in clock.split(':'))
    return datetime(2026, 9, 7, h, m, s, tzinfo=IST)


def _orch(tmp_path, monkeypatch, readings):
    """An orchestrator whose ohlc.db holds `readings`, with running totals."""
    db = engine_mod.OHLCDatabase(db_path=str(tmp_path / 'ohlc.db'))
    total = 0
    with sqlite3.connect(db.db_path) as conn:
        for clock, price, vwap, iv in readings:
            total += iv
            conn.execute(
                "INSERT INTO ohlc_data (timestamp, asset_id, symbol, segment_code, "
                "last_price, average_price, volume, interval_volume) "
                "VALUES (?, ?, 'TCS', ?, ?, ?, ?, ?)",
                (f'2026-09-07T{clock}+05:30', ASSET, SEG, price, vwap, total, iv))
        conn.commit()
    monkeypatch.setattr(metrics, 'get_latest_metrics', lambda: {'momentum': [
        {'asset_id': ASSET, 'segment_code': SEG, 'previous_close': PREV_CLOSE}]})

    o = object.__new__(metrics.MetricsOrchestrator)
    o.historical_db_writer = _Writer()
    o.asset_loader = _Loader()
    o.ohlc_db_manager = OHLCDatabaseReader(db.db_path)
    o._written_intraday_blocks = set()
    return o


@pytest.fixture
def orch(tmp_path, monkeypatch):
    return _orch(tmp_path, monkeypatch, _BLOCK_1 + [_AT_THE_END])


def _pvd(readings):
    return calculate_pvd_v3(prices=[p for _c, p, _v, _i in readings],
                            volumes=[i for _c, _p, _v, i in readings],
                            previous_fisher_history=[]).fisher_value


def test_nothing_is_written_before_the_block_ends(orch):
    orch._maybe_write_intraday_block(_at('09:29:40'))
    assert orch.historical_db_writer.rows == []


def test_the_block_is_recorded_from_its_last_reading(orch):
    orch._maybe_write_intraday_block(_at('09:30:20'))
    (row,) = orch.historical_db_writer.rows
    _clock, last_price, last_vwap, _iv = _BLOCK_1[-1]
    assert row['time_block'] == 1
    assert row['session_price_move'] == pytest.approx((last_price / PREV_CLOSE - 1) * 100)
    assert row['vwap_displacement'] == pytest.approx(math.log(last_price / last_vwap))


def test_pv_divergence_leaves_out_the_days_first_reading(orch):
    """Its 66,818-share interval is everything traded before it that day, not
    five seconds' worth."""
    orch._maybe_write_intraday_block(_at('09:30:20'))
    (row,) = orch.historical_db_writer.rows
    assert row['pvd_raw'] == pytest.approx(_pvd(_BLOCK_1[1:]))
    assert _pvd(_BLOCK_1) != pytest.approx(_pvd(_BLOCK_1[1:])), "the first reading must matter"


def test_nothing_is_written_after_the_write_window(orch):
    orch._maybe_write_intraday_block(_at('09:33:00'))
    assert orch.historical_db_writer.rows == []


def test_a_block_without_readings_records_none_not_an_earlier_price(orch):
    """Block 2 (09:30-09:45) holds only the 09:30:00 reading; block 3 holds
    none. Block 3 must not inherit block 2's price."""
    orch._maybe_write_intraday_block(_at('10:00:30'))
    (row,) = orch.historical_db_writer.rows
    assert row['time_block'] == 3
    assert (row['session_price_move'], row['vwap_displacement'], row['pvd_raw']) == (None, None, None)


def test_a_block_last_seen_over_30_s_before_its_end_records_no_price(tmp_path, monkeypatch):
    """Readings stop at 09:28:55, 65 s before block 1 ends: that price is not
    the 09:30 price. PV Divergence, over readings with no gap, still stands."""
    o = _orch(tmp_path, monkeypatch, [r for r in _BLOCK_1 if r[0] < '09:29:00'])
    o._maybe_write_intraday_block(_at('09:30:20'))
    (row,) = o.historical_db_writer.rows
    assert (row['session_price_move'], row['vwap_displacement']) == (None, None)
    assert row['pvd_raw'] is not None


def test_a_gap_over_30_s_inside_the_block_records_no_pv_divergence(tmp_path, monkeypatch):
    """No readings from 09:20:05 to 09:20:55: the 09:21:00 reading's interval
    holds a minute of trading, one spike the correlation would be built
    around. The block's end price still stands."""
    o = _orch(tmp_path, monkeypatch, [r for r in _BLOCK_1 if not '09:20:05' <= r[0] <= '09:20:55'])
    o._maybe_write_intraday_block(_at('09:30:20'))
    (row,) = o.historical_db_writer.rows
    assert row['pvd_raw'] is None
    assert row['session_price_move'] is not None
