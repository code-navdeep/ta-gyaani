"""
scripts/daily.py — the 15:34 job that archives the day from ohlc.db into
historical.db (daily_ohlc and time_block_volumes).

Both run against a real ohlc.db built with the engine's own schema and a real
historical.db, so the SQL itself is exercised, not a stand-in.
"""
import logging
import sqlite3
from datetime import date

import engine as engine_mod
from calculators.time_blocks import BLOCK_COUNT
from scripts.daily import DailyHistoricalExtractor

DAY = date(2026, 8, 21)            # a Friday
DAY_ISO = DAY.isoformat()


def _extractor(tmp_path, ticks):
    """`ticks`: (clock 'HH:MM:SS', asset_id, open_price, interval_volume)."""
    db = engine_mod.OHLCDatabase(db_path=str(tmp_path / 'ohlc.db'))
    with sqlite3.connect(db.db_path) as conn:
        for clock, aid, open_price, iv in ticks:
            conn.execute(
                "INSERT INTO ohlc_data (timestamp, asset_id, symbol, segment_code, "
                "last_price, open_price, high_price, low_price, close_price, "
                "volume, interval_volume, average_price) "
                "VALUES (?, ?, ?, 'NSE_EQ', 101.0, ?, 105.0, 95.0, 100.0, ?, ?, 100.5)",
                (f'{DAY_ISO}T{clock}+05:30', aid, f'S{aid}', open_price, 1000 + iv, iv))
        conn.commit()
    return DailyHistoricalExtractor(db.db_path, str(tmp_path / 'historical.db'))


class TestOneIncompleteAssetDoesNotLoseTheDay:
    """A NULL in any one asset's open/high/low used to fail validation for the
    whole day. The 15:34 job does not pass --force, so nothing was
    archived for any of the 86 assets and the ohlc.db prune was skipped."""

    def test_the_others_archive_and_the_one_is_logged(self, tmp_path, caplog):
        ticks = []
        for aid in range(1, 12):           # 11 stocks; the asset-coverage check needs 10
            open_price = None if aid == 11 else 100.0
            # 09:29:55 makes block 1 complete (observed within 30 s of its end).
            ticks += [('09:15:00', aid, open_price, 500),
                      ('09:29:55', aid, open_price, 50),
                      ('15:25:00', aid, open_price, 700)]
        extractor = _extractor(tmp_path, ticks)

        with caplog.at_level(logging.WARNING):
            assert extractor.run_extraction(DAY, allow_partial_coverage=True)

        with sqlite3.connect(tmp_path / 'historical.db') as conn:
            archived = {r[0] for r in conn.execute(
                "SELECT asset_id FROM daily_ohlc WHERE date = ?", (DAY_ISO,))}
        assert archived == set(range(1, 11))
        assert any('S11' in r.getMessage() and 'open_price' in r.getMessage()
                   for r in caplog.records)


class TestArchiveUsesTheSharedGrid:
    """The block number an archived volume is filed under is what Volume Block
    z-scores against. The extractor used to build its own copy of the grid."""

    def test_ticks_are_filed_under_the_shared_block_numbers(self, tmp_path):
        """Blocks are half-open, [start, end): 09:29:55 is block 1's last tick,
        09:30:00 block 2's first, 15:15:00 block 25's first. The zero-volume
        readings close each block's edges within 30 s so it is complete and
        archived (time_blocks.block_complete); block 24, unobserved at its
        start, is not."""
        extractor = _extractor(tmp_path, [('09:29:55', 1, 100.0, 11),
                                          ('09:30:00', 1, 100.0, 22),
                                          ('09:44:55', 1, 100.0, 0),
                                          ('15:14:55', 1, 100.0, 0),
                                          ('15:15:00', 1, 100.0, 33),
                                          ('15:30:55', 1, 100.0, 0)])
        tb = extractor.extract_time_block_volumes(DAY)
        assert dict(zip(tb['time_block'], tb['cumulative_volume'])) == {
            1: 11, 2: 22, BLOCK_COUNT: 33}

    def test_the_private_copy_is_gone(self, tmp_path):
        assert not hasattr(_extractor(tmp_path, []), 'time_blocks')


class TestTodayIsNotArchivedWhileCollecting:
    """Archived before its readings stop (15:31), a day's morning is stored as
    the whole day — and the 15:34 archive only fills EMPTY fields, so it is
    never corrected. `python scripts/daily.py` with no date means today."""

    def test_the_boundary_is_1531(self):
        from datetime import datetime
        from calculators.time_blocks import IST
        from scripts.daily import session_still_collecting
        today = date(2026, 9, 7)
        assert session_still_collecting(today, datetime(2026, 9, 7, 12, 5, tzinfo=IST))
        assert session_still_collecting(today, datetime(2026, 9, 7, 15, 30, 59, tzinfo=IST))
        assert not session_still_collecting(today, datetime(2026, 9, 7, 15, 31, tzinfo=IST))
        assert not session_still_collecting(date(2026, 9, 4),
                                            datetime(2026, 9, 7, 12, 5, tzinfo=IST))

    def test_run_extraction_refuses_the_day(self, tmp_path, monkeypatch):
        import scripts.daily as daily_mod
        monkeypatch.setattr(daily_mod, 'session_still_collecting', lambda day, now=None: True)
        ticks = [(clock, aid, 100.0, 500) for aid in range(1, 12)
                 for clock in ('09:15:00', '15:25:00')]
        extractor = _extractor(tmp_path, ticks)
        assert not extractor.run_extraction(DAY, allow_partial_coverage=True)
        with sqlite3.connect(tmp_path / 'historical.db') as conn:
            assert conn.execute("SELECT COUNT(*) FROM daily_ohlc").fetchone() == (0,)
