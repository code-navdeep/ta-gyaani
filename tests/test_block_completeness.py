"""
A 15-minute block's volume is trusted only when both of its edges were
observed: a reading within 30 s (time_blocks.BLOCK_EDGE_TOLERANCE) before its
start and within 30 s of its end.

The intervals inside a block add up to (the running total at its last reading)
minus (the total at the last reading before it), so a gap wholly inside a block
changes nothing; only time left unobserved at one of its edges does. The engine
stores an incomplete block with an EMPTY volume (Volume Block then reports
`incomplete_block`), and the archive files no row for it.

The outage pattern is TCS on 2026-09-02: the first reading at 10:30:50, readings
until ~10:37, nothing until ~11:13, then one interval holding 144,695 shares.
Block 5 was stored as 0 and block 8 took the whole gap's volume.
"""
import sqlite3
from datetime import date, datetime, time as dt_time, timedelta

import pandas as pd

import engine as engine_mod
from calculators.database_manager import OHLCDatabaseReader
from calculators.time_blocks import IST, block_complete, block_end_edge_ok, block_start_edge_ok
from scripts.daily import DailyHistoricalExtractor

TCS, SEG = 11536, 'NSE_EQ'
DAY = '2026-09-02'


def _db(tmp_path, readings):
    """readings: (clock 'HH:MM:SS', interval volume, running total) for TCS on DAY."""
    db = engine_mod.OHLCDatabase(db_path=str(tmp_path / 'ohlc.db'))
    _insert(db.db_path, [(f'{DAY}T{c}+05:30', TCS, SEG, iv, total) for c, iv, total in readings])
    return db


def _insert(path, rows):
    """rows: (stamp, asset_id, segment_code, interval volume, running total)."""
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO ohlc_data (timestamp, asset_id, symbol, segment_code, last_price, "
            "volume, interval_volume) VALUES (?, ?, 'TCS', ?, 3100.0, ?, ?)",
            [(stamp, aid, seg, total, iv) for stamp, aid, seg, iv, total in rows])
        conn.commit()


def _every_5_s(start, end, first_interval=100):
    """Clocks from `start` up to (not including) `end`, 5 s apart."""
    t = datetime.combine(date(2026, 9, 2), start)
    stop = datetime.combine(date(2026, 9, 2), end)
    out = []
    while t < stop:
        out.append((t.strftime('%H:%M:%S'), first_interval if not out else 100))
        t += timedelta(seconds=5)
    return out


def _outage_day():
    """The 2026-09-02 pattern: a late start at 10:30:50 (its interval is 0 —
    a first reading at or after 09:30), readings to 10:37:20, nothing until
    11:13:00, whose interval holds the gap's 144,695 shares, then to 11:29:55."""
    clocks = (_every_5_s(dt_time(10, 30, 50), dt_time(10, 37, 25), first_interval=0)
              + _every_5_s(dt_time(11, 13), dt_time(11, 30), first_interval=144_695))
    total, readings = 2_000_000, []
    for clock, iv in clocks:
        total += iv
        readings.append((clock, iv, total))
    return readings


class _Loader:
    assets_df = pd.DataFrame([{'asset_id': TCS, 'segment_code': SEG, 'asset_name': 'TCS'}])


class _StubEngine:
    def __init__(self, database):
        self.database = database
        self.asset_loader = _Loader()


def _engine_block(eng, block):
    engine_mod.OHLCEngine._store_time_block_volumes(
        eng, DAY, block, datetime(2026, 9, 2, 12, 0, tzinfo=IST))
    with sqlite3.connect(eng.database.db_path) as conn:
        return conn.execute("SELECT cumulative_volume FROM time_block_volumes "
                            "WHERE date = ? AND time_block = ?", (DAY, block)).fetchone()


class TestTheRule:

    def test_a_short_gap_across_a_boundary_leaves_both_blocks_complete(self):
        """10:44:58 then 10:45:30: block 6 misses 2 s at its end, block 7's
        first reading carries 32 s of which 30 are its own — 2 s misfiled."""
        assert block_end_edge_ok(6, dt_time(10, 44, 58))
        assert block_start_edge_ok(7, dt_time(10, 44, 58), dt_time(10, 45, 30))

    def test_block_1_needs_nothing_before_it(self):
        """The day's first reading, before 09:30, carries everything since
        before 09:15."""
        assert block_start_edge_ok(1, None, dt_time(9, 29, 50))

    def test_a_late_start_is_judged_from_the_block_start(self):
        assert not block_start_edge_ok(6, None, dt_time(10, 30, 50))    # 50 s missed
        assert block_start_edge_ok(6, None, dt_time(10, 30, 20))        # 20 s

    def test_block_25_ends_at_1531(self):
        assert block_end_edge_ok(25, dt_time(15, 30, 35))
        assert not block_end_edge_ok(25, dt_time(15, 30, 0))

    def test_a_block_never_seen_is_not_complete(self):
        assert not block_complete(7, dt_time(10, 37, 20), None, None)
        assert not block_complete(5, None, None, None)


class TestEngineOnTheOutageDay:

    def test_the_blocks_around_the_outage_are_stored_empty(self, tmp_path):
        eng = _StubEngine(_db(tmp_path, _outage_day()))
        # 5: no readings (was 0). 6: seen from 10:30:50 to 10:37:20. 7: never
        # seen. 8: would hold the whole gap's 144,695 shares.
        for block in (5, 6, 7, 8):
            assert _engine_block(eng, block) == (None,), f"block {block}"

    def test_the_block_after_it_is_whole(self, tmp_path):
        eng = _StubEngine(_db(tmp_path, _outage_day()))
        assert _engine_block(eng, 9) == (180 * 100,)      # 11:15:00 … 11:29:55

    def test_every_write_is_judged_and_the_last_one_stands(self, tmp_path):
        """Written at 09:29:05, block 1's last reading is 55 s before its end:
        stored empty. Written again once readings reach 09:29:55: stored."""
        early = [(c, iv, 1_000 + 100 * i)
                 for i, (c, iv) in enumerate(_every_5_s(dt_time(9, 15, 5), dt_time(9, 29, 10)))]
        eng = _StubEngine(_db(tmp_path, early))
        assert _engine_block(eng, 1) == (None,)
        late = [(f'{DAY}T{c}+05:30', TCS, SEG, 100, 100_000 + 100 * i)
                for i, (c, _iv) in enumerate(_every_5_s(dt_time(9, 29, 10), dt_time(9, 30)))]
        _insert(eng.database.db_path, late)
        assert _engine_block(eng, 1) == ((len(early) + len(late)) * 100,)


def test_the_archive_files_only_the_whole_block(tmp_path):
    db = _db(tmp_path, _outage_day())
    tb = DailyHistoricalExtractor(db.db_path, str(tmp_path / 'historical.db')) \
        .extract_time_block_volumes(date(2026, 9, 2))
    assert dict(zip(tb['time_block'], tb['cumulative_volume'])) == {9: 180 * 100}


class TestSessionFirstReading:
    """The day's first reading with a running total. Its interval is not a
    5-second amount, so PV Divergence leaves it out."""

    def _reader(self, tmp_path, rows):
        db = engine_mod.OHLCDatabase(db_path=str(tmp_path / 'ohlc.db'))
        _insert(db.db_path, rows)
        return OHLCDatabaseReader(db.db_path)

    def test_a_first_reading_without_a_running_total_is_skipped(self, tmp_path):
        reader = self._reader(tmp_path, [
            ('2026-09-01T15:29:55+05:30', TCS, SEG, 100, 5_000),      # the day before
            (f'{DAY}T09:15:02+05:30', TCS, SEG, None, None),           # no running total
            (f'{DAY}T09:15:07+05:30', TCS, SEG, 66_818, 66_818),
            (f'{DAY}T09:15:00+05:30', 25, SEG, 10, 10),                # ADANIENT, same id as BankNifty
        ])
        assert reader.session_first_reading(TCS, SEG, date(2026, 9, 2)) == f'{DAY}T09:15:07+05:30'
        assert reader.session_first_reading(25, 'IDX_I', date(2026, 9, 2)) is None

    def test_none_yet_is_asked_again(self, tmp_path):
        reader = self._reader(tmp_path, [])
        assert reader.session_first_reading(TCS, SEG, date(2026, 9, 2)) is None
        _insert(reader.db_path, [(f'{DAY}T10:30:50+05:30', TCS, SEG, 0, 2_000_000)])
        assert reader.session_first_reading(TCS, SEG, date(2026, 9, 2)) == f'{DAY}T10:30:50+05:30'
