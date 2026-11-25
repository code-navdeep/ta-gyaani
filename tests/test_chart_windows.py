"""
The chart-window pills (1h / 2h / 4h / 8h / 1d, in the settings drawer) must
give every chart the window they name, and the index mini-charts must show the
last session outside market hours rather than nothing.

- Asset charts: for 1d the builders received None ("the whole day") and
  replaced it with 2 hours, so 1d was narrower than 4h and 8h.
- Index mini-charts: all six drew `None if hours > 8 else 2`, so 1h, 4h and 8h
  all showed 2 hours.
- Index mini-charts read index_metrics from 00:00 today with no fallback, so
  pre-market, evenings and weekends they were blank beneath a card full of the
  last session's numbers.
"""
import dataclasses
import sqlite3
from datetime import datetime, timedelta

import pandas as pd
import pytest

import dashboard
from calculators.metrics_database_manager import MetricsDatabaseManager
from calculators.schemas import IndexSummary
from calculators.time_blocks import IST
from dashboard_signals import build_momentum_chart

# 09:15 to 14:00 at 5-minute steps — longer than every pill except 1d.
_TS = pd.date_range('2026-08-21 09:15', '2026-08-21 14:00', freq='5min')


def _x_start(fig):
    return pd.Timestamp(fig.layout.xaxis.range[0])


class TestAssetChartsFollowThePill:

    @pytest.fixture
    def history(self):
        return pd.DataFrame({'timestamp': _TS, 'early_return_z_22d': 1.0})

    def test_1d_shows_the_whole_day(self, history):
        assert _x_start(build_momentum_chart(history, window_hours=None)) == _TS[0]

    def test_4h_shows_four_hours(self, history):
        assert _x_start(build_momentum_chart(history, window_hours=4)) == \
            _TS[-1] - pd.Timedelta(hours=4)

    def test_no_argument_still_shows_two_hours(self, history):
        assert _x_start(build_momentum_chart(history)) == _TS[-1] - pd.Timedelta(hours=2)


class TestIndexChartsFollowThePill:

    @pytest.fixture
    def history(self):
        return pd.DataFrame({'timestamp': _TS.astype(str), 'cross_sectional_dispersion': 0.5})

    @pytest.mark.parametrize('hours', [1, 4, 8])
    def test_each_pill_gives_its_own_hours(self, history, hours):
        fig = dashboard.build_index_dispersion_chart(history, hours=hours)
        assert _x_start(fig) == _TS[-1] - pd.Timedelta(hours=hours)

    def test_1d_shows_the_whole_day(self, history):
        assert _x_start(dashboard.build_index_dispersion_chart(history, hours=24)) == _TS[0]


def _graphs(component):
    """Every dcc.Graph figure in a Dash component tree."""
    if getattr(component, 'figure', None) is not None:
        yield component.figure
    children = getattr(component, 'children', None)
    if children is None or isinstance(children, str):
        return
    for child in children if isinstance(children, list) else [children]:
        yield from _graphs(child)


def _texts(component):
    if isinstance(component, str):
        yield component
        return
    children = getattr(component, 'children', None)
    if children is None:
        return
    for child in children if isinstance(children, list) else [children]:
        yield from _texts(child)


class TestIndexChartsFallBackToTheLastSession:

    @pytest.fixture
    def yesterday(self, tmp_path, monkeypatch):
        """A temporary metrics.db whose only index_metrics row is from
        yesterday at 14:00 — the state of a weekend or a pre-market morning."""
        db = MetricsDatabaseManager(db_path=str(tmp_path / 'metrics.db'))
        stamp = (datetime.now(IST) - timedelta(days=1)).replace(
            hour=14, minute=0, second=0, microsecond=0)
        db.store_index_metrics({'NIFTY': dataclasses.asdict(IndexSummary(
            index_name='NIFTY', advancers=30, decliners=20,
            cross_sectional_dispersion=0.8, weighted_intraday_return=0.3))})
        with sqlite3.connect(db.db_path) as conn:
            conn.execute("UPDATE index_metrics SET created_at = ?, timestamp = ?",
                         (stamp.isoformat(), stamp.isoformat()))
        monkeypatch.setattr(dashboard, 'get_metrics_db', lambda: db)
        return stamp

    def test_the_query_falls_back_and_says_when(self, yesterday):
        df, anchor = dashboard.get_index_breadth_history('NIFTY', hours=2)
        assert not df.empty
        assert anchor == yesterday

    def test_the_card_labels_the_session_and_its_charts_have_data(self, yesterday):
        card = dashboard.build_index_card({'index_name': 'NIFTY'}, hours=2)
        label = f"Last session: {yesterday:%Y-%m-%d} · "
        assert any(t.startswith(label) for t in _texts(card))
        assert any(len(fig.data) > 0 for fig in _graphs(card))


def test_a_stamp_without_fractional_seconds_does_not_break_a_chart():
    """isoformat() leaves out the fraction when microseconds are exactly 0, so a
    stored column can mix forms. Without format='ISO8601' pandas guesses ONE
    format from the first stamp and fails on the other kind."""
    from dashboard_signals import _make_signed_line_traces, _padded_xrange
    stamps = pd.Series(['2026-09-07T10:30:00.123456+05:30',
                        '2026-09-07T10:30:05+05:30',
                        '2026-09-07T10:30:10.5+05:30'])
    start, _end = _padded_xrange(stamps, window_hours=None)
    assert start == pd.Timestamp('2026-09-07T10:30:00.123456+05:30')
    assert len(_make_signed_line_traces(stamps, [1.0, -1.0, 2.0], 'X')) == 2
