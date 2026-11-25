"""
The price chart's three session overlays — the opening-range box, the
PrevClose line and the ATR band — belong to the session the chart SHOWS, which
is not always today: with no readings today, the chart shows the last recorded
session (get_today_ohlc's fallback).

They used to take today's date and the archive's newest row. So a previous
session's chart had no box (drawn on today, off its axis), a PrevClose line at
the shown day's own close, and — after 15:34, when the newest row is the day
just archived, without its close — no PrevClose line at all and an ATR that
already counted the shown day's own range.
"""
import sqlite3
from datetime import date, timedelta

import pandas as pd
import pytest

import dashboard
from calculators import market_calendar
from calculators.historical_database_manager import (HistoricalDatabaseReader,
                                                     HistoricalDatabaseWriter)

TCS = (11536, 'NSE_EQ')
FRIDAY, THURSDAY = date(2026, 9, 4), date(2026, 9, 3)


def _reader(tmp_path, rows):
    """rows: (date, open, high, low, close) for TCS."""
    path = str(tmp_path / 'historical.db')
    HistoricalDatabaseWriter(path)                     # creates the schema
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO daily_ohlc (date, asset_id, segment_code, open_price, high_price, "
            "low_price, close_price) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(d.isoformat(), *TCS, o, h, l, c) for d, o, h, l, c in rows])
    return HistoricalDatabaseReader(path, enable_cache=False)


class TestCloseOnADay:

    def test_the_close_stored_for_exactly_that_day(self, tmp_path):
        reader = _reader(tmp_path, [(THURSDAY, 2300, 2330, 2290, 2320.10),
                                    (FRIDAY, 2300, 2310, 2295, 2304.00)])
        assert reader.get_close_on(*TCS, THURSDAY) == 2320.10

    def test_no_row_and_no_close_yet_are_both_none(self, tmp_path):
        """Friday archived at 15:34 without its close: until Monday morning's
        fill, there is no close for it — not zero, not Thursday's."""
        reader = _reader(tmp_path, [(FRIDAY, 2300, 2310, 2295, None)])
        assert reader.get_close_on(*TCS, FRIDAY) is None
        assert reader.get_close_on(*TCS, THURSDAY) is None


class TestAtrBeforeTheShownDay:

    def test_the_shown_days_own_range_does_not_count(self, tmp_path):
        """Fifteen days with a true range of 10 each, then the shown day with a
        range of 1,000. The ATR in force during the shown day is 10."""
        first = FRIDAY - timedelta(days=15)
        rows = [(first + timedelta(days=i), 100, 105, 95, 100) for i in range(15)]
        rows.append((FRIDAY, 100, 1100, 100, 100))
        reader = _reader(tmp_path, rows)
        assert reader.get_atr(*TCS, before=FRIDAY, period=14) == pytest.approx(10.0)


class _FakeHistorical:
    def __init__(self):
        self.close_days, self.atr_before = [], []

    def get_close_on(self, asset_id, segment_code, day):
        self.close_days.append(day)
        return {THURSDAY: 2320.10}.get(day)

    def get_atr(self, asset_id, segment_code, before, period=14):
        self.atr_before.append(before)
        return 25.0


class _FakeMetrics:
    def get_asset_metrics_history(self, asset_id, segment_code, hours):
        return {}, False, None


@pytest.fixture
def previous_session_chart(monkeypatch):
    """The price chart on a day with no readings of its own, showing Friday."""
    readings = pd.DataFrame({
        'timestamp': ['2026-09-04T09:15:05+05:30', '2026-09-04T09:40:00+05:30',
                      '2026-09-04T11:00:00+05:30', '2026-09-04T15:29:55+05:30'],
        'last_price': [2300.0, 2310.0, 2295.0, 2304.0],
        'open_price': [2300.0] * 4, 'high_price': [2310.0] * 4,
        'low_price': [2295.0] * 4, 'close_price': [2320.10] * 4,
        'interval_volume': [5000, 800, 700, 900],
    })
    historical = _FakeHistorical()
    monkeypatch.setattr(market_calendar, '_holidays', frozenset())
    monkeypatch.setattr(dashboard, 'get_today_ohlc', lambda *a, **k: (readings, True, FRIDAY))
    monkeypatch.setattr(dashboard, 'get_historical_db', lambda: historical)
    monkeypatch.setattr(dashboard, 'get_metrics_db', lambda: _FakeMetrics())
    fig = dashboard.build_charts(99999, 'NSE_EQ', show_atr=True)[0]
    return fig, historical


class TestPriceChartFollowsTheShownSession:

    def test_the_opening_range_box_is_on_the_shown_day(self, previous_session_chart):
        fig, _ = previous_session_chart
        boxes = [s for s in fig.layout.shapes if s.type == 'rect']
        assert len(boxes) == 1 and str(boxes[0].x0).startswith('2026-09-04')

    def test_prevclose_is_the_close_of_the_trading_day_before_it(self, previous_session_chart):
        fig, historical = previous_session_chart
        assert historical.close_days == [THURSDAY]
        assert any(s.type == 'line' and s.y0 == pytest.approx(2320.10) for s in fig.layout.shapes)
        assert 'PrevClose' in [a.text for a in fig.layout.annotations]

    def test_the_atr_band_is_the_one_in_force_that_session(self, previous_session_chart):
        fig, historical = previous_session_chart
        assert historical.atr_before == [FRIDAY]
        band = {t.name: t.y[0] for t in fig.data if 'ATR' in (t.name or '')}
        assert band == {'PrevClose+ATR(14d)': pytest.approx(2345.10),
                        'PrevClose-ATR(14d)': pytest.approx(2295.10)}
