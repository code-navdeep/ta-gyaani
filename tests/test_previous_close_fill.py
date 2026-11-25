"""
The previous close: filled each morning from the day's first reading.

A reading's close_price is the exchange's official close of the PREVIOUS trading
day, constant all session (verified for all 86 assets: 2026-09-07's first value
equals 2026-09-04's final traded price; Sensex alone starts moving from ~15:15,
under the closing auction). So a day's own close is not known when the day is
archived at 15:34: the archive writes each row without a close, and on the next
trading morning's first metrics cycle with prices — before any calculator runs —
the metrics thread writes the close into the previous trading day's row (a
close-only row if the day has none), refits beta, and drops the historical
reader's remembered answers. The archive used to take close_price after 15:25,
which filed each day under the day before's close.
"""
import sqlite3
from datetime import date, datetime

import pandas as pd
import pytest

import engine as engine_mod
import metrics
from calculators import market_calendar
from calculators.database_manager import OHLCDatabaseReader
from calculators.historical_database_manager import DatabaseCache, HistoricalDatabaseWriter
from calculators.market_calendar import previous_trading_day
from calculators.time_blocks import IST
from scripts.daily import DailyHistoricalExtractor

TCS, NIFTY = (11536, 'NSE_EQ'), (13, 'IDX_I')


def _ohlc(tmp_path, readings):
    """readings: (iso timestamp, asset, close_price, last_price)."""
    db = engine_mod.OHLCDatabase(db_path=str(tmp_path / 'ohlc.db'))
    with sqlite3.connect(db.db_path) as conn:
        for ts, (aid, seg), close, last in readings:
            conn.execute(
                "INSERT INTO ohlc_data (timestamp, asset_id, symbol, segment_code, last_price, "
                "open_price, high_price, low_price, close_price, volume, interval_volume) "
                "VALUES (?, ?, 'S', ?, ?, ?, ?, ?, ?, 100, 100)",
                (ts, aid, seg, last, last, last, last, close))
        conn.commit()
    return db.db_path


def _row(hist_path, day, asset):
    with sqlite3.connect(hist_path) as conn:
        return conn.execute(
            "SELECT open_price, high_price, low_price, close_price FROM daily_ohlc "
            "WHERE date = ? AND asset_id = ? AND segment_code = ?",
            (day, *asset)).fetchone()


class TestPreviousTradingDay:

    def test_monday_looks_back_to_friday(self):
        assert previous_trading_day(date(2026, 9, 7)) == date(2026, 9, 4)

    def test_a_listed_holiday_is_skipped(self, monkeypatch):
        monkeypatch.setattr(market_calendar, '_holidays', frozenset({date(2026, 9, 14)}))
        assert previous_trading_day(date(2026, 9, 15)) == date(2026, 9, 11)


def test_the_first_reading_supplies_the_close(tmp_path):
    """Sensex's close_price moves from ~15:15 (the closing auction); only the
    first reading of the day carries the previous day's close."""
    path = _ohlc(tmp_path, [
        ('2026-09-07T09:15:05+05:30', TCS, None, 2300.0),     # no close: skipped
        ('2026-09-07T09:15:10+05:30', TCS, 2304.0, 2301.0),
        ('2026-09-07T15:20:03+05:30', TCS, 2281.5, 2280.0),   # later, different
    ])
    got = OHLCDatabaseReader(path).get_first_reading_closes(date(2026, 9, 7))
    assert list(zip(got['asset_id'], got['close_price'])) == [(TCS[0], 2304.0)]


class TestFillCloses:

    def test_an_archived_row_gets_only_its_close(self, tmp_path):
        writer = HistoricalDatabaseWriter(str(tmp_path / 'historical.db'))
        writer.bulk_insert_daily_ohlc(pd.DataFrame([{
            'date': '2026-09-04', 'asset_id': TCS[0], 'segment_code': TCS[1], 'symbol': 'TCS',
            'open_price': 2310.0, 'high_price': 2330.0, 'low_price': 2290.0,
            'close_price': None, 'volume': 5, 'vwap': 2305.0}]))
        writer.fill_closes(date(2026, 9, 4), pd.DataFrame([
            {'asset_id': TCS[0], 'segment_code': TCS[1], 'symbol': 'TCS', 'close_price': 2304.0}]))
        assert _row(writer.db_path, '2026-09-04', TCS) == (2310.0, 2330.0, 2290.0, 2304.0)

    def test_a_missing_day_gets_a_close_only_row(self, tmp_path):
        writer = HistoricalDatabaseWriter(str(tmp_path / 'historical.db'))
        writer.fill_closes(date(2026, 9, 3), pd.DataFrame([
            {'asset_id': TCS[0], 'segment_code': TCS[1], 'symbol': 'TCS', 'close_price': 2320.1}]))
        assert _row(writer.db_path, '2026-09-03', TCS) == (None, None, None, 2320.1)


class _Prices:
    def is_ready(self):
        return True


class _Reader:
    def __init__(self):
        self.cache = DatabaseCache()
        self.cache.set('get_previous_close_data', 'stale')


@pytest.fixture
def morning(tmp_path, monkeypatch):
    """Tuesday 2026-09-08, 09:15:30: Monday's row is archived without a close;
    Tuesday's first reading carries Monday's close."""
    ohlc_path = _ohlc(tmp_path, [('2026-09-08T09:15:05+05:30', TCS, 2270.0, 2275.0)])
    writer = HistoricalDatabaseWriter(str(tmp_path / 'historical.db'))
    writer.bulk_insert_daily_ohlc(pd.DataFrame([{
        'date': '2026-09-07', 'asset_id': TCS[0], 'segment_code': TCS[1], 'symbol': 'TCS',
        'open_price': 2300.0, 'high_price': 2310.0, 'low_price': 2265.0,
        'close_price': None, 'volume': 5, 'vwap': 2290.0}]))

    beta_runs = []
    monkeypatch.setattr(metrics.subprocess, 'run',
                        lambda cmd, **kw: beta_runs.append(cmd) or type('R', (), {'returncode': 0})())

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 8, 9, 15, 30, tzinfo=IST)

    monkeypatch.setattr(metrics, 'datetime', _Clock)
    for name in ('BREAKOUT', 'VWAP', 'RELATIVE_STRENGTH', 'VOLUME_BLOCK', 'VOLUME_DYNAMICS',
                 'PRICE_VOLUME_DIVERGENCE', 'PRICE_LEVELS', 'ROLLING_MOMENTUM'):
        monkeypatch.setattr(metrics, f'{name}_SYSTEM_AVAILABLE', False)
    monkeypatch.setattr(metrics, 'MOMENTUM_SYSTEM_AVAILABLE', True)
    seen_by_momentum = []
    monkeypatch.setattr(metrics, 'run_momentum_analysis', lambda *a, **k: seen_by_momentum.append(
        writer.get_daily_closes(date(2026, 9, 7))) or [])
    monkeypatch.setattr(metrics, 'update_metrics_cache', lambda *_: None)

    o = object.__new__(metrics.MetricsOrchestrator)
    o.ohlc_db_manager = OHLCDatabaseReader(ohlc_path)
    o.historical_db_writer = writer
    o.historical_db_manager = _Reader()
    o.price_data_manager = _Prices()
    o.asset_loader = None
    o.vwap_calc = None
    o.index_aggregator = None
    o.metrics_db_manager = None
    o._last_written_time_block = {}
    o._maybe_write_intraday_block = lambda now: None
    o._close_filled_date = None
    o._closes_missing = set()
    return o, writer, beta_runs, seen_by_momentum


class TestMorningStep:

    def test_the_close_is_in_before_momentum_runs(self, morning):
        o, writer, _, seen = morning
        o.collect_metrics()
        assert seen == [{TCS: 2270.0}]
        assert _row(writer.db_path, '2026-09-07', TCS) == (2300.0, 2310.0, 2265.0, 2270.0)

    def test_beta_is_refitted_right_after_and_the_reader_forgets(self, morning):
        o, _, beta_runs, _ = morning
        o.collect_metrics()
        assert len(beta_runs) == 1 and beta_runs[0][-1] == 'scripts/precompute_beta.py'
        assert o.historical_db_manager.cache.get('get_previous_close_data') is None

    def test_it_runs_once_a_day(self, morning):
        o, _, beta_runs, _ = morning
        o.collect_metrics()
        o.collect_metrics()
        assert len(beta_runs) == 1

    def test_a_stock_missing_from_the_first_fetch_is_filled_when_it_arrives(self, morning):
        """NIFTY has no reading when the step first runs. Its close is written on
        the first later cycle that has one, and beta is refitted once more —
        not never, which left it without a previous close all day."""
        o, writer, beta_runs, _ = morning
        o.asset_loader = type('Loader', (), {'assets_df': pd.DataFrame(
            {'asset_id': [TCS[0], NIFTY[0]], 'segment_code': [TCS[1], NIFTY[1]]})})()
        o.collect_metrics()
        assert o._closes_missing == {NIFTY}
        assert _row(writer.db_path, '2026-09-07', NIFTY) is None

        with sqlite3.connect(o.ohlc_db_manager.db_path) as conn:
            conn.execute(
                "INSERT INTO ohlc_data (timestamp, asset_id, symbol, segment_code, last_price, "
                "open_price, high_price, low_price, close_price) "
                "VALUES ('2026-09-08T09:15:10+05:30', ?, 'NIFTY', ?, 23800, 23800, 23800, 23800, 23779.15)",
                NIFTY)
            conn.commit()
        o.collect_metrics()
        assert _row(writer.db_path, '2026-09-07', NIFTY) == (None, None, None, 23779.15)
        assert o._closes_missing == set()
        assert len(beta_runs) == 2

        o.collect_metrics()
        assert len(beta_runs) == 2          # nothing missing: no further refits


class TestArchive:

    def _extractor(self, tmp_path):
        # 09:29:55 makes block 1 complete (observed within 30 s of its end),
        # so the day has a block to archive; it changes no open, high or low.
        path = _ohlc(tmp_path, [
            ('2026-09-07T09:15:05+05:30', TCS, 2304.0, 2300.0),
            ('2026-09-07T09:29:55+05:30', TCS, 2304.0, 2285.0),
            ('2026-09-07T15:29:55+05:30', TCS, 2304.0, 2270.0),
        ])
        return DailyHistoricalExtractor(path, str(tmp_path / 'historical.db'))

    def test_the_archive_writes_no_close_and_keeps_the_row(self, tmp_path):
        ex = self._extractor(tmp_path)
        df = ex.extract_daily_ohlc(date(2026, 9, 7))
        assert df['close_price'].isna().all()
        assert len(ex.drop_incomplete_ohlc_rows(df, date(2026, 9, 7))) == 1

    def test_a_forced_re_archive_keeps_a_filled_close(self, tmp_path):
        ex = self._extractor(tmp_path)
        ex.historical_db.fill_closes(date(2026, 9, 7), pd.DataFrame([
            {'asset_id': TCS[0], 'segment_code': TCS[1], 'symbol': 'S', 'close_price': 2270.0}]))
        assert ex.run_extraction(date(2026, 9, 7), force=True)
        assert _row(ex.historical_db.db_path, '2026-09-07', TCS)[3] == 2270.0

    def test_a_re_archive_fills_a_close_only_row(self, tmp_path):
        ex = self._extractor(tmp_path)
        ex.historical_db.fill_closes(date(2026, 9, 7), pd.DataFrame([
            {'asset_id': TCS[0], 'segment_code': TCS[1], 'symbol': 'S', 'close_price': 2270.0}]))
        ex.historical_db.bulk_insert_daily_ohlc(ex.extract_daily_ohlc(date(2026, 9, 7)))
        assert _row(ex.historical_db.db_path, '2026-09-07', TCS) == (2300.0, 2300.0, 2270.0, 2270.0)
