"""
The OHLC reader's in-memory store of today's readings (get_session_prices).

Breakout and Price Levels need the whole day's prices every cycle. They used to
re-read them from disk for every asset, every cycle (up to ~770,000 rows a
cycle by the close). The reader now holds the day's readings and each new `now` reads
only rows newer than the newest held, all assets in one query.

That is safe only because the engine stamps a whole fetch with one timestamp,
writes it in one transaction, one fetch after another, and never edits a
reading. These tests pin what the store must do on top of that: the right
window, no re-reading, late commits picked up, one read per cycle, and a clean
start each day — the restart / late-start / new-day cases.
"""
import math
import sqlite3
from datetime import datetime

import pandas as pd
import pytest

import engine as engine_mod
from calculators.database_manager import OHLCDatabaseReader
from calculators.time_blocks import IST

TCS = (11536, 'NSE_EQ')


def _at(day, clock):
    return datetime.fromisoformat(f'2026-09-{day:02d}T{clock}+05:30')


@pytest.fixture
def db(tmp_path):
    database = engine_mod.OHLCDatabase(db_path=str(tmp_path / 'ohlc.db'))

    def write(readings):
        """readings: (iso timestamp, (asset_id, segment_code), last_price)."""
        with sqlite3.connect(database.db_path) as conn:
            conn.executemany(
                "INSERT INTO ohlc_data (timestamp, asset_id, symbol, segment_code, last_price) "
                "VALUES (?, ?, 'S', ?, ?)",
                [(ts, aid, seg, price) for ts, (aid, seg), price in readings])

    def delete(ts):
        with sqlite3.connect(database.db_path) as conn:
            conn.execute("DELETE FROM ohlc_data WHERE timestamp = ?", (ts,))

    return OHLCDatabaseReader(database.db_path), write, delete


def test_the_day_so_far_oldest_first_up_to_but_not_including_now(db):
    reader, write, _ = db
    write([('2026-09-04T15:29:55+05:30', TCS, 2304.0),        # yesterday
           ('2026-09-07T09:15:10.250000+05:30', TCS, 2291.0),
           ('2026-09-07T09:15:05+05:30', TCS, 2290.0),
           ('2026-09-07T11:00:00+05:30', TCS, 2280.0),        # exactly now
           ('2026-09-07T11:00:05+05:30', TCS, 2281.0)])       # after now
    frame = reader.get_session_prices(*TCS, _at(7, '11:00:00'))
    assert list(frame['last_price']) == [2290.0, 2291.0]
    assert list(frame['timestamp']) == list(pd.to_datetime(
        ['2026-09-07T09:15:05+05:30', '2026-09-07T09:15:10.250000+05:30'], format='ISO8601'))


def test_rows_already_held_are_not_read_again(db):
    """Delete a held reading from disk: if the second cycle re-read the day, it
    would vanish. It stays; only the new reading is added."""
    reader, write, delete = db
    write([('2026-09-07T09:15:05+05:30', TCS, 2290.0),
           ('2026-09-07T09:15:10+05:30', TCS, 2291.0)])
    reader.get_session_prices(*TCS, _at(7, '09:15:30'))
    delete('2026-09-07T09:15:05+05:30')
    write([('2026-09-07T09:15:35+05:30', TCS, 2292.0)])
    frame = reader.get_session_prices(*TCS, _at(7, '09:16:00'))
    assert list(frame['last_price']) == [2290.0, 2291.0, 2292.0]


def test_a_fetch_written_after_a_cycle_read_is_picked_up_next_cycle(db):
    """The engine stamps a fetch before writing it. A fetch stamped 10:59:58
    but written after the 11:00:00 cycle's read is newer than everything held,
    so the next cycle's read takes it."""
    reader, write, _ = db
    write([('2026-09-07T10:59:53+05:30', TCS, 2280.0)])
    reader.get_session_prices(*TCS, _at(7, '11:00:00'))
    write([('2026-09-07T10:59:58+05:30', TCS, 2281.0)])
    frame = reader.get_session_prices(*TCS, _at(7, '11:00:30'))
    assert list(frame['last_price']) == [2280.0, 2281.0]


def test_one_disk_read_per_cycle(db, monkeypatch):
    """Breakout and Price Levels pass the same `now` in one cycle; for 86
    assets each that used to be 172 reads. The same `now` reads once."""
    reader, write, _ = db
    write([('2026-09-07T09:15:05+05:30', TCS, 2290.0)])
    opened = []
    original = reader.get_read_connection

    def counting():
        opened.append(1)
        return original()
    monkeypatch.setattr(reader, 'get_read_connection', counting)
    now = _at(7, '11:00:00')
    reader.get_session_prices(*TCS, now)
    reader.get_session_prices(1333, 'NSE_EQ', now)
    reader.get_session_prices(*TCS, now)
    assert len(opened) == 1


def test_a_new_day_starts_empty(db):
    """A container left running overnight must not carry Friday's readings into
    Monday."""
    reader, write, _ = db
    write([('2026-09-04T10:00:00+05:30', TCS, 2310.0),
           ('2026-09-07T09:15:05+05:30', TCS, 2290.0)])
    assert list(reader.get_session_prices(*TCS, _at(4, '11:00:00'))['last_price']) == [2310.0]
    assert list(reader.get_session_prices(*TCS, _at(7, '11:00:00'))['last_price']) == [2290.0]


def test_the_composite_key_keeps_shared_ids_apart(db):
    """asset_id 25 is BankNifty in IDX_I and ADANIENT in NSE_EQ."""
    reader, write, _ = db
    write([('2026-09-07T09:15:05+05:30', (25, 'IDX_I'), 51000.0),
           ('2026-09-07T09:15:05+05:30', (25, 'NSE_EQ'), 2400.0)])
    now = _at(7, '11:00:00')
    assert list(reader.get_session_prices(25, 'IDX_I', now)['last_price']) == [51000.0]
    assert list(reader.get_session_prices(25, 'NSE_EQ', now)['last_price']) == [2400.0]


def test_a_missing_price_is_kept_as_not_a_number(db):
    """The engine stores a missing price as NULL. It is passed on as NaN, and the
    calculators' own filters (positive prices only) drop it, as before."""
    reader, write, _ = db
    write([('2026-09-07T09:15:05+05:30', TCS, None),
           ('2026-09-07T09:15:10+05:30', TCS, 2291.0)])
    prices = list(reader.get_session_prices(*TCS, _at(7, '11:00:00'))['last_price'])
    assert math.isnan(prices[0]) and prices[1] == 2291.0


class TestTheCalculatorsOnTheRealStore:
    """The adapter tests use stand-ins that return text timestamps; the store
    returns real times. These run both calculators end to end on it."""

    def test_breakout_on_ashokleys_readings(self, db):
        """ASHOKLEY 2026-09-04: range 168.61-170.23; 170.19 at 09:44:57, 170.25
        at 09:45:02 and above from then on."""
        from calculators.breakout_adapter import run_breakout_analysis
        reader, write, _ = db
        ashokley = (212, 'NSE_EQ')
        write([(f'2026-09-04T{clock}+05:30', ashokley, price) for clock, price in [
            ('09:15:05', 168.61), ('09:30:00', 170.23), ('09:44:57', 170.19),
            ('09:45:02', 170.25), ('09:50:00', 170.38), ('09:55:00', 170.30)]])

        class _Loader:
            assets_df = pd.DataFrame([{'asset_id': 212, 'segment_code': 'NSE_EQ',
                                       'symbol': 'ASHOKLEY'}])
        [row] = run_breakout_analysis(reader, None, asset_loader=_Loader(),
                                      now=_at(4, '10:00:00'))
        assert (row.range_low, row.range_high) == (168.61, 170.23)
        assert row.breakout_direction == 1 and row.accepted_breakout == 1

    def test_price_levels_sees_a_crossing_earlier_in_the_day(self, db):
        from calculators.price_levels_adapter import run_price_levels_analysis
        reader, write, _ = db
        write([('2026-09-07T09:20:00+05:30', TCS, 2295.0),
               ('2026-09-07T09:25:00+05:30', TCS, 2305.0),    # crosses 2,300
               ('2026-09-07T10:59:55+05:30', TCS, 2290.0)])

        class _Loader:
            assets_df = pd.DataFrame([{'asset_id': 11536, 'segment_code': 'NSE_EQ',
                                       'asset_name': 'TCS', 'local_high': 2300.0}])
        [row] = run_price_levels_analysis(reader, asset_loader=_Loader(),
                                          now=_at(7, '11:00:00'))
        assert row.day_events == ['local_high']


def test_an_asset_with_no_readings_gets_an_empty_frame(db):
    reader, write, _ = db
    write([('2026-09-07T09:15:05+05:30', TCS, 2290.0)])
    frame = reader.get_session_prices(1333, 'NSE_EQ', _at(7, '11:00:00'))
    assert frame.empty and list(frame.columns) == ['timestamp', 'last_price']
