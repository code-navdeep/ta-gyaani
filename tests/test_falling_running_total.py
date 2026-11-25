"""
A running total lower than the last good one is a bad reading.

A day's running total of shares traded cannot fall. During the closing-auction
period the quote feed reports one that does: on 2026-09-07 TCS's total read
2,163,939 shares, then 0 from 15:20:05, then 132,757 (the auction's own count)
at 15:29:57, then 2,296,696 at 15:30:02. The engine subtracted each reading from
the one before, so the drop became an interval of -2,163,939 shares, block 25
was archived negative for 81 of 82 stocks, and Volume Block — log(1 + volume),
undefined below -1 — failed for every stock at the close.

Now a bad reading stores both its total and its interval as NULL, the next good
reading is measured from the last good one, and both block writers and the
archive check refuse a negative block.
"""
import sqlite3
from datetime import date, datetime

import pandas as pd

import engine as engine_mod
from calculators.time_blocks import IST
from scripts.daily import DailyHistoricalExtractor

ASSET, SEG = 1333, 'NSE_EQ'


class _Loader:
    def __init__(self, rows):
        self.assets_df = pd.DataFrame(rows)


class _StubEngine:
    def __init__(self, database, asset_loader=None):
        self.database = database
        self.asset_loader = asset_loader


def _engine(tmp_path, loader=None):
    return _StubEngine(engine_mod.OHLCDatabase(db_path=str(tmp_path / 'ohlc.db')), loader)


def _feed(eng, readings):
    """Process each (timestamp, running_total) as the engine does — compute,
    then store — and return the (volume, interval) stored for each."""
    stored = []
    with eng.database.get_connection() as conn:
        for ts, total in readings:
            volume, interval = engine_mod.OHLCEngine._calculate_interval_volume(
                eng, conn, ASSET, SEG, total, ts)
            conn.execute(
                "INSERT INTO ohlc_data (timestamp, asset_id, symbol, segment_code, "
                "volume, interval_volume) VALUES (?, ?, 'TEST', ?, ?, ?)",
                (ts, ASSET, SEG, volume, interval))
            conn.commit()
            stored.append((volume, interval))
    return stored


class TestBadReadings:

    def test_the_agreed_example(self, tmp_path):
        """Totals 1000, 1200, 0, 0, 1500 -> intervals 1000, 200, empty, empty,
        300. The first 1000 is the day's first reading, inside block 1."""
        stored = _feed(_engine(tmp_path), [
            ('2026-08-21T09:15:05+05:30', 1000),
            ('2026-08-21T09:15:10+05:30', 1200),
            ('2026-08-21T09:15:15+05:30', 0),
            ('2026-08-21T09:15:20+05:30', 0),
            ('2026-08-21T09:15:25+05:30', 1500),
        ])
        assert stored == [(1000, 1000), (1200, 200), (None, None), (None, None), (1500, 300)]

    def test_the_real_close_of_2026_09_07(self, tmp_path):
        stored = _feed(_engine(tmp_path), [
            ('2026-09-07T15:14:57+05:30', 2_162_026),   # first reading here: late start, discarded
            ('2026-09-07T15:15:03+05:30', 2_163_939),
            ('2026-09-07T15:15:08+05:30', 2_163_939),   # frozen: equal is not lower
            ('2026-09-07T15:20:05+05:30', 0),
            ('2026-09-07T15:29:57+05:30', 132_757),     # the auction's own count, alone
            ('2026-09-07T15:30:02+05:30', 2_296_696),
        ])
        intervals = [i for _v, i in stored]
        assert intervals == [0, 1_913, 0, None, None, 132_757]
        assert all(i is None or i >= 0 for i in intervals)

    def test_a_failed_lookup_loses_no_volume(self, tmp_path):
        """Totals 1,000,000, then 1,000,900 whose previous-reading lookup fails,
        then 1,001,500. The failed reading is stored like a bad one — nothing —
        so the third is measured from the first: 1,500 shares, all of them. It
        used to keep the failed reading's total with an empty interval, the
        third was measured from it (600), and 900 shares left the block."""
        class _Broken:
            def execute(self, *args, **kwargs):
                raise sqlite3.OperationalError('disk I/O error')

        eng = _engine(tmp_path)
        stored = _feed(eng, [('2026-08-21T09:15:05+05:30', 1_000_000)])
        failed = engine_mod.OHLCEngine._calculate_interval_volume(
            eng, _Broken(), ASSET, SEG, 1_000_900, '2026-08-21T09:15:10+05:30')
        with eng.database.get_connection() as conn:
            conn.execute(
                "INSERT INTO ohlc_data (timestamp, asset_id, symbol, segment_code, "
                "volume, interval_volume) VALUES ('2026-08-21T09:15:10+05:30', ?, 'TEST', ?, ?, ?)",
                (ASSET, SEG, *failed))
            conn.commit()
        stored += [failed] + _feed(eng, [('2026-08-21T09:15:15+05:30', 1_001_500)])
        assert stored == [(1_000_000, 1_000_000), (None, None), (1_001_500, 1_500)]


class TestEngineBlockWriterRefusesNegative:

    def test_a_negative_block_is_not_written(self, tmp_path):
        loader = _Loader([{'asset_id': ASSET, 'segment_code': SEG, 'asset_name': 'TEST'},
                          {'asset_id': 1594, 'segment_code': SEG, 'asset_name': 'OTHER'}])
        eng = _engine(tmp_path, loader)
        with eng.database.get_connection() as conn:
            # 09:29:55 — within 30 s of block 1's end, so the block is complete
            # and only the negative sum decides.
            for aid, iv in [(ASSET, -500), (1594, 700)]:
                conn.execute(
                    "INSERT INTO ohlc_data (timestamp, asset_id, symbol, segment_code, "
                    "volume, interval_volume) VALUES ('2026-08-21T09:29:55', ?, 'S', ?, 1, ?)",
                    (aid, SEG, iv))
            conn.commit()
        engine_mod.OHLCEngine._store_time_block_volumes(
            eng, '2026-08-21', 1, datetime(2026, 8, 21, 9, 30, tzinfo=IST))
        with sqlite3.connect(eng.database.db_path) as conn:
            written = dict(conn.execute(
                "SELECT asset_id, cumulative_volume FROM time_block_volumes"))
        assert written == {1594: 700}


class TestArchiveRefusesNegative:

    def test_the_extractor_drops_a_negative_block(self, tmp_path):
        db = engine_mod.OHLCDatabase(db_path=str(tmp_path / 'ohlc.db'))
        with sqlite3.connect(db.db_path) as conn:
            for aid, iv in [(ASSET, -500), (1594, 700)]:
                conn.execute(
                    "INSERT INTO ohlc_data (timestamp, asset_id, symbol, segment_code, "
                    "last_price, volume, interval_volume) "
                    "VALUES ('2026-08-21T09:29:55+05:30', ?, 'S', ?, 100.0, 1, ?)",
                    (aid, SEG, iv))
            conn.commit()
        extractor = DailyHistoricalExtractor(db.db_path, str(tmp_path / 'historical.db'))
        tb = extractor.extract_time_block_volumes(date(2026, 8, 21))
        assert dict(zip(tb['asset_id'], tb['cumulative_volume'])) == {1594: 700}

    def test_the_quality_check_fails_a_negative_volume(self, tmp_path):
        db = engine_mod.OHLCDatabase(db_path=str(tmp_path / 'ohlc.db'))
        extractor = DailyHistoricalExtractor(db.db_path, str(tmp_path / 'historical.db'))
        ohlc = pd.DataFrame({'volume': [1000] * 10})
        good = pd.DataFrame({'cumulative_volume': [500] * 250})
        bad = pd.DataFrame({'cumulative_volume': [500] * 249 + [-1]})
        day = date(2026, 8, 21)
        assert extractor.validate_data_quality(ohlc, good, day)
        assert not extractor.validate_data_quality(ohlc, bad, day)
