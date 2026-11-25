"""
Market holidays: on a day listed in data/holidays.txt everything is skipped —
fetching, metrics, and the end-of-day jobs (15:34) including token renewal. A missing file,
a malformed line, or no date for the current year is logged loudly and the app
runs.

A holiday collected and archived as if it were a trading day puts a spurious
sample into every asset's every block and a spurious daily_ohlc row into gap
history and beta — with no error anywhere.

These tests use their own holiday set, never the real data/holidays.txt.
"""
import logging
from datetime import date, datetime

import pytest

import engine as engine_mod
from calculators import market_calendar
from calculators.market_calendar import is_trading_day, load_holidays, parse_holidays
from calculators.time_blocks import IST
from scripts.daily import DailyHistoricalExtractor

GANESH_CHATURTHI = date(2026, 9, 14)   # a Monday


@pytest.fixture
def listed(monkeypatch):
    monkeypatch.setattr(market_calendar, '_holidays', frozenset({GANESH_CHATURTHI}))


class TestParsing:

    def test_the_files_own_format(self):
        lines = ['# Market Holidays - one date per line in dd/mm/yyyy format', '',
                 '14/09/2026', '# Monday, Ganesh Chaturthi', '',
                 '28/05/2026', '#Thursday, Bakr-Eid']
        assert parse_holidays(lines) == {GANESH_CHATURTHI, date(2026, 5, 28)}

    def test_a_malformed_line_is_named_and_the_rest_still_load(self, caplog):
        with caplog.at_level(logging.ERROR):
            got = parse_holidays(['14/09/2026', '2026-10-02'])
        assert got == {GANESH_CHATURTHI}
        assert 'line 2' in caplog.text and '2026-10-02' in caplog.text

    def test_a_missing_file_is_loud_and_skips_nothing(self, tmp_path, caplog):
        with caplog.at_level(logging.ERROR):
            got = load_holidays(tmp_path / 'holidays.txt', today=date(2026, 9, 29))
        assert got == frozenset()
        assert 'HOLIDAY CALENDAR MISSING' in caplog.text

    def test_no_date_this_year_is_loud(self, tmp_path, caplog):
        path = tmp_path / 'holidays.txt'
        path.write_text('25/12/2026\n')
        with caplog.at_level(logging.ERROR):
            load_holidays(path, today=date(2027, 1, 4))
        assert 'no holiday in 2027' in caplog.text


class TestTradingDay:

    def test_a_listed_holiday_is_not_a_trading_day(self, listed):
        assert not is_trading_day(GANESH_CHATURTHI)

    def test_the_next_weekday_is(self, listed):
        assert is_trading_day(date(2026, 9, 15))

    def test_a_weekend_is_not(self, listed):
        assert not is_trading_day(date(2026, 9, 12))


class TestGates:

    def test_engine_and_metrics_do_not_run_on_a_listed_holiday(self, listed):
        """_is_scheduled_day gates the engine's fetching AND the metrics loop."""
        mgr = object.__new__(engine_mod.OHLCScheduleManager)
        mgr.schedule_config = {'schedule_type': 'weekdays'}
        assert not mgr._is_scheduled_day(datetime(2026, 9, 14, 10, 0, tzinfo=IST))
        assert mgr._is_scheduled_day(datetime(2026, 9, 15, 10, 0, tzinfo=IST))

    def test_the_archive_refuses_a_listed_holiday(self, listed):
        extractor = object.__new__(DailyHistoricalExtractor)
        assert not extractor.validate_market_day(GANESH_CHATURTHI)
        assert extractor.validate_market_day(date(2026, 9, 15))
