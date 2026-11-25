"""
The session's final readings count into block 25, up to 15:31.

Under the closing auction the day's full total can arrive stamped after 15:30:00:
on 2026-09-07 TCS's auction count (132,757 shares) and closing price (₹2,270.00)
arrived in the reading stamped 15:30:02, after block 25 ([15:15, 15:30)) had
ended, so block 25 held only 1,913 shares. All three writers now count readings
up to 15:31 into block 25 — the engine's live total, the archive's recount, and
the shared-store snapshot — and nothing stamped at or after 15:31 counts
anywhere, so a stray late fetch cannot change block 25.
"""
import math
import sqlite3
from datetime import date, datetime, time as dt_time

import pandas as pd
import pytest

import engine as engine_mod
import metrics
from calculators.database_manager import OHLCDatabaseReader
from calculators.time_blocks import (BLOCK_COUNT, IST, all_blocks, block_end_time,
                                     block_reading_end_time)
from scripts.daily import DailyHistoricalExtractor

ASSET, SEG = 11536, 'NSE_EQ'
DAY = '2026-09-07'
PREV_CLOSE = 2304.0

# (clock, last_price, exchange VWAP, interval volume). The engine now fetches
# until 15:31, so a day's last readings run to ~15:30:57 — block 25 is only
# complete when observed that close to its 15:31 end (time_blocks.block_complete).
_READINGS = [
    ('15:15:03', 2280.0, 2290.00, 1_913),       # inside block 25
    ('15:30:02', 2270.0, 2289.50, 132_757),     # the auction's result, after 15:30
    ('15:30:57', 2270.0, 2289.50, 0),           # the last fetch before 15:31
    ('15:31:05', 9999.0, 2289.50, 999),         # stray, after the cut-off
]


def _ohlc_db(tmp_path):
    db = engine_mod.OHLCDatabase(db_path=str(tmp_path / 'ohlc.db'))
    with sqlite3.connect(db.db_path) as conn:
        for clock, price, vwap, iv in _READINGS:
            conn.execute(
                "INSERT INTO ohlc_data (timestamp, asset_id, symbol, segment_code, last_price, "
                "average_price, volume, interval_volume) VALUES (?, ?, 'TCS', ?, ?, ?, 1, ?)",
                (f'{DAY}T{clock}+05:30', ASSET, SEG, price, vwap, iv))
        conn.commit()
    return db


def test_block_25_reads_up_to_1531_and_no_other_block_moves():
    assert block_reading_end_time(BLOCK_COUNT) == dt_time(15, 31)
    for block in all_blocks()[:-1]:
        assert block_reading_end_time(block) == block_end_time(block)


class _Loader:
    assets_df = pd.DataFrame([{'asset_id': ASSET, 'segment_code': SEG, 'asset_name': 'TCS'}])


class _StubEngine:
    def __init__(self, database):
        self.database = database
        self.asset_loader = _Loader()


def test_the_engine_counts_the_final_reading_into_block_25(tmp_path):
    eng = _StubEngine(_ohlc_db(tmp_path))
    engine_mod.OHLCEngine._store_time_block_volumes(
        eng, DAY, BLOCK_COUNT, datetime(2026, 9, 7, 15, 30, 0, tzinfo=IST))
    with sqlite3.connect(eng.database.db_path) as conn:
        (volume,) = conn.execute(
            "SELECT cumulative_volume FROM time_block_volumes WHERE time_block = 25").fetchone()
    assert volume == 1_913 + 132_757


def test_the_archive_counts_it_the_same_way(tmp_path):
    db = _ohlc_db(tmp_path)
    extractor = DailyHistoricalExtractor(db.db_path, str(tmp_path / 'historical.db'))
    tb = extractor.extract_time_block_volumes(date(2026, 9, 7))
    assert dict(zip(tb['time_block'], tb['cumulative_volume'])) == {25: 1_913 + 132_757}


class _Writer:
    def __init__(self):
        self.rows = []

    def store_intraday_time_block_metrics(self, rows):
        self.rows.extend(rows)
        return len(rows)


@pytest.fixture
def orch(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, 'get_latest_metrics', lambda: {'momentum': [
        {'asset_id': ASSET, 'segment_code': SEG, 'previous_close': PREV_CLOSE}]})
    o = object.__new__(metrics.MetricsOrchestrator)
    o.historical_db_writer = _Writer()
    o.asset_loader = _Loader()
    o.ohlc_db_manager = OHLCDatabaseReader(_ohlc_db(tmp_path).db_path)
    o._written_intraday_blocks = set()
    return o


def test_the_shared_store_waits_for_the_final_readings(orch):
    orch._maybe_write_intraday_block(datetime(2026, 9, 7, 15, 30, 20, tzinfo=IST))
    assert orch.historical_db_writer.rows == []


class _VolumeBlockStore:
    def __init__(self):
        self.blocks = []

    def store_volume_block_metrics(self, rows):
        self.blocks += [r['time_block'] for r in rows]
        return len(rows)


def _volume_block_cycle(monkeypatch, clock):
    """One metrics cycle with only Volume Block on, reporting block 25, at `clock`."""
    from calculators.schemas import VolumeBlockRow

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 7, *clock, tzinfo=IST)

    monkeypatch.setattr(metrics, 'datetime', _Clock)
    for name in ('MOMENTUM', 'BREAKOUT', 'VWAP', 'RELATIVE_STRENGTH', 'VOLUME_DYNAMICS',
                 'PRICE_VOLUME_DIVERGENCE', 'PRICE_LEVELS', 'ROLLING_MOMENTUM'):
        monkeypatch.setattr(metrics, f'{name}_SYSTEM_AVAILABLE', False)
    monkeypatch.setattr(metrics, 'VOLUME_BLOCK_SYSTEM_AVAILABLE', True)
    monkeypatch.setattr(metrics, 'run_volume_block_analysis', lambda *a, **k: (
        [VolumeBlockRow(asset_id=ASSET, segment_code=SEG, time_block=25)], {}))
    monkeypatch.setattr(metrics, 'update_metrics_cache', lambda *_: None)
    monkeypatch.setitem(metrics.CALCULATOR_HEALTH, 'volume_block', None)
    o = object.__new__(metrics.MetricsOrchestrator)
    o.ohlc_db_manager = object()
    o.historical_db_manager = object()
    o.price_data_manager = None
    o.asset_loader = None
    o.vwap_calc = None
    o.index_aggregator = None
    o._last_written_time_block = {}
    o._maybe_write_intraday_block = lambda now: None
    o.metrics_db_manager = _VolumeBlockStore()
    o.collect_metrics()
    return o.metrics_db_manager.blocks


def test_volume_blocks_stored_block_25_row_waits_for_the_final_readings(monkeypatch):
    """Volume Block stores each block's row once. Stored at 15:30:xx, block 25's
    row would never see an auction result arriving before 15:31."""
    assert _volume_block_cycle(monkeypatch, (15, 30, 20)) == []


def test_it_is_stored_once_they_are_in(monkeypatch):
    assert _volume_block_cycle(monkeypatch, (15, 31, 10)) == [25]


def test_block_25s_stored_price_is_the_closing_price(orch):
    orch._maybe_write_intraday_block(datetime(2026, 9, 7, 15, 31, 10, tzinfo=IST))
    (row,) = orch.historical_db_writer.rows
    assert row['time_block'] == 25
    assert row['session_price_move'] == pytest.approx((2270.0 / PREV_CLOSE - 1) * 100)
    assert row['vwap_displacement'] == pytest.approx(math.log(2270.0 / 2289.50))
