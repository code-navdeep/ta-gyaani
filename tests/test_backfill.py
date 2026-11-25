"""
scripts/backfill.py — rebuilding historical.db from ohlc.db.

Two things it must do now that the archive writes no close:
  1. After storing every day, write each day's official close from the NEXT
     trading day's first reading (the morning step's rule, for every day in
     ohlc.db), then refit β. Without it a backfill leaves every close empty, so
     β, gap history and ATR get nothing from the backfilled days.
  2. Drop any asset whose day lacks its open, high or low before storing — the
     per-asset check daily.py's run_extraction applies, which the backfill did
     not get when that check moved out of validate_data_quality.

And these:
  - the --start-date / --end-date range is a real choice (it used to error out,
    or be silently ignored under --all);
  - a day already archived is processed again, harmlessly — only missing
    fields are filled, so a day holding only its close is completed — and the
    counts are rows actually written (it used to skip only the newest archived
    date, and report every row sent as added);
  - --allow-partial-coverage stores a day the app ran only part of, with the
    other quality checks still applied;
  - today is refused until its session stops collecting (15:31);
  - each archived day's block snapshots are rebuilt, equal to what the live
    writer stores for the same readings.
"""
import importlib
import logging
import sqlite3
from datetime import date, datetime

import pandas as pd
import pytest

import engine as engine_mod
import metrics
from calculators import market_calendar
from calculators.database_manager import OHLCDatabaseReader
from calculators.time_blocks import IST

TCS, THIN = (11536, 'NSE_EQ'), (2885, 'NSE_EQ')


@pytest.fixture
def backfill(monkeypatch):
    """Import the script without it opening a log file under data/logs."""
    monkeypatch.syspath_prepend('scripts')
    monkeypatch.setattr('logging.handlers.TimedRotatingFileHandler',
                        lambda *a, **k: logging.NullHandler())
    module = importlib.import_module('backfill')
    beta_runs = []
    monkeypatch.setattr(module.subprocess, 'run',
                        lambda cmd, **kw: beta_runs.append(cmd) or type('R', (), {'returncode': 0})())
    return module, beta_runs


def _ohlc(tmp_path, readings):
    """readings: (iso timestamp, asset, price, close_price); price None = no price."""
    db = engine_mod.OHLCDatabase(db_path=str(tmp_path / 'ohlc.db'))
    with sqlite3.connect(db.db_path) as conn:
        for ts, (aid, seg), price, close in readings:
            conn.execute(
                "INSERT INTO ohlc_data (timestamp, asset_id, symbol, segment_code, last_price, "
                "open_price, high_price, low_price, close_price, volume, interval_volume) "
                "VALUES (?, ?, 'S', ?, ?, ?, ?, ?, ?, 100, 100)",
                (ts, aid, seg, price, price, price, price, close))
        conn.commit()
    return db.db_path


def _close(hist_path, day, asset):
    with sqlite3.connect(hist_path) as conn:
        row = conn.execute(
            "SELECT open_price, close_price FROM daily_ohlc WHERE date = ? AND asset_id = ? "
            "AND segment_code = ?", (day, *asset)).fetchone()
    return row


@pytest.fixture
def run(tmp_path, backfill):
    module, beta_runs = backfill
    # Each 09:29:55 reading makes block 1 complete (seen within 30 s of its
    # end), so the day has a block to archive; it changes no open or close.
    ohlc = _ohlc(tmp_path, [
        ('2026-09-04T09:15:05+05:30', TCS, 2300.0, 2320.1),   # Friday: Thursday's close
        ('2026-09-04T09:29:55+05:30', TCS, 2300.0, 2320.1),
        ('2026-09-04T15:29:55+05:30', TCS, 2304.0, 2320.1),
        ('2026-09-04T09:15:05+05:30', THIN, None, 1300.0),    # no price all day
        ('2026-09-07T09:15:05+05:30', TCS, 2290.0, 2304.0),   # Monday: Friday's close
        ('2026-09-07T09:29:55+05:30', TCS, 2290.0, 2304.0),
        ('2026-09-07T15:29:55+05:30', TCS, 2270.0, 2304.0),
    ])
    hist = str(tmp_path / 'historical.db')
    backfiller = module.HistoricalBackfiller(ohlc, hist)

    def _go(preview=False):
        backfiller.backfill_date_range([date(2026, 9, 4), date(2026, 9, 7)],
                                       preview=preview, force=True)
        return hist, beta_runs
    return _go


def test_each_day_gets_its_close_from_the_next_trading_days_first_reading(run):
    hist, _ = run()
    assert _close(hist, '2026-09-04', TCS) == (2300.0, 2304.0)
    assert _close(hist, '2026-09-03', TCS) == (None, 2320.1)     # close-only row
    assert _close(hist, '2026-09-07', TCS) == (2290.0, None)     # arrives next morning


def test_beta_is_refitted_once_after_the_closes(run):
    _, beta_runs = run()
    assert len(beta_runs) == 1 and beta_runs[0][-1].endswith('precompute_beta.py')


def test_a_day_with_no_open_high_or_low_is_not_archived(run):
    hist, _ = run()
    with sqlite3.connect(hist) as conn:
        rows = conn.execute("SELECT date, open_price FROM daily_ohlc WHERE asset_id = ?",
                            (THIN[0],)).fetchall()
    assert rows == [('2026-09-03', None)]    # only its close-only row, from Friday's reading


def test_preview_writes_nothing(run):
    hist, beta_runs = run(preview=True)
    with sqlite3.connect(hist) as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_ohlc").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM intraday_time_block_metrics").fetchone() == (0,)
    assert beta_runs == []


# ---------------------------------------------------------------------------
# Command line, re-runs, partial days, today, block snapshots
# ---------------------------------------------------------------------------

LATER = datetime(2026, 9, 29, 20, 0, tzinfo=IST)     # a "now" on none of the test days


def _eleven(day_iso, clocks=('09:15:05', '09:29:55')):
    """Eleven stocks — the quality check wants at least ten — each with readings
    at `clocks` (by default both inside block 1; 09:29:55 is within 30 s of its
    end, so block 1 is complete — time_blocks.block_complete)."""
    return [(f'{day_iso}T{clock}+05:30', (1000 + i, 'NSE_EQ'), 100.0 + i, None)
            for i in range(11) for clock in clocks]


@pytest.fixture
def no_holidays(monkeypatch):
    monkeypatch.setattr(market_calendar, '_holidays', frozenset())


class TestCommandLine:
    AVAILABLE = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 4), date(2026, 9, 7)]

    def test_a_date_range_selects_the_days_inside_it(self, backfill):
        module, _ = backfill
        args = module.parse_args(['--start-date', '2026-09-02', '--end-date', '2026-09-04'])
        assert module.select_dates(args, self.AVAILABLE) == [date(2026, 9, 2), date(2026, 9, 4)]

    @pytest.mark.parametrize('argv', [
        ['--start-date', '2026-09-02'],                                       # no end
        ['--days', '2', '--end-date', '2026-09-04'],                          # end, no start
        ['--all', '--start-date', '2026-09-02', '--end-date', '2026-09-04'],  # was silently ignored
        ['--days', '0'],                                                      # crashed
        ['--days', '-2'],                                                     # took an odd slice
    ])
    def test_a_half_or_conflicting_choice_is_refused(self, backfill, argv):
        module, _ = backfill
        with pytest.raises(SystemExit):
            module.parse_args(argv)


class TestRerunningOverArchivedDays:

    def test_a_second_run_writes_nothing_and_says_so(self, tmp_path, backfill):
        module, _ = backfill
        b = module.HistoricalBackfiller(_ohlc(tmp_path, _eleven('2026-09-04')),
                                        str(tmp_path / 'historical.db'))
        first = b.backfill_date_range([date(2026, 9, 4)], allow_partial_coverage=True, now=LATER)
        second = b.backfill_date_range([date(2026, 9, 4)], allow_partial_coverage=True, now=LATER)
        assert (first['ohlc_records'], first['tb_records']) == (11, 11)
        assert (second['successful'], second['ohlc_records'], second['tb_records']) == (1, 0, 0)

    def test_a_day_holding_only_its_close_is_completed(self, tmp_path, backfill):
        """The morning step inserts a close-only row when a day was never
        archived. Skipping "the newest archived date" left exactly that day
        alone: a close with no open, high or low."""
        module, _ = backfill
        hist = str(tmp_path / 'historical.db')
        b = module.HistoricalBackfiller(_ohlc(tmp_path, _eleven('2026-09-04')), hist)
        b.extractor.historical_db.fill_closes(date(2026, 9, 4), pd.DataFrame(
            [{'asset_id': 1000 + i, 'segment_code': 'NSE_EQ', 'symbol': 'S', 'close_price': 50.0}
             for i in range(11)]))
        stats = b.backfill_date_range([date(2026, 9, 4)], allow_partial_coverage=True, now=LATER)
        assert stats['ohlc_records'] == 11
        with sqlite3.connect(hist) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM daily_ohlc WHERE date = '2026-09-04' "
                "AND open_price IS NOT NULL AND close_price = 50.0").fetchone() == (11,)


class TestPartialDays:

    def test_a_short_day_needs_the_flag(self, tmp_path, backfill):
        """Readings in block 1 only: 1 of 25 blocks, under the 70% the check wants."""
        module, _ = backfill
        b = module.HistoricalBackfiller(_ohlc(tmp_path, _eleven('2026-09-04')),
                                        str(tmp_path / 'historical.db'))
        refused = b.backfill_date_range([date(2026, 9, 4)], now=LATER)
        stored = b.backfill_date_range([date(2026, 9, 4)], allow_partial_coverage=True, now=LATER)
        assert (refused['successful'], refused['failed']) == (0, 1)
        assert stored['successful'] == 1

    def test_the_other_checks_still_apply(self, tmp_path, backfill):
        """Three stocks — under the ten the asset-coverage check wants."""
        module, _ = backfill
        readings = [r for r in _eleven('2026-09-04') if r[1][0] < 1003]
        b = module.HistoricalBackfiller(_ohlc(tmp_path, readings), str(tmp_path / 'historical.db'))
        stats = b.backfill_date_range([date(2026, 9, 4)], allow_partial_coverage=True, now=LATER)
        assert (stats['successful'], stats['failed']) == (0, 1)


class TestTodayWhileCollecting:

    def test_today_is_skipped_before_1531(self, tmp_path, backfill):
        module, _ = backfill
        hist = str(tmp_path / 'historical.db')
        b = module.HistoricalBackfiller(_ohlc(tmp_path, _eleven('2026-09-07')), hist)
        stats = b.backfill_date_range([date(2026, 9, 7)], allow_partial_coverage=True,
                                      now=datetime(2026, 9, 7, 12, 5, tzinfo=IST))
        assert (stats['skipped'], stats['successful']) == (1, 0)
        with sqlite3.connect(hist) as conn:
            assert conn.execute("SELECT COUNT(*) FROM daily_ohlc").fetchone() == (0,)

    def test_from_1531_it_is_archived(self, tmp_path, backfill):
        module, _ = backfill
        b = module.HistoricalBackfiller(_ohlc(tmp_path, _eleven('2026-09-07')),
                                        str(tmp_path / 'historical.db'))
        stats = b.backfill_date_range([date(2026, 9, 7)], allow_partial_coverage=True,
                                      now=datetime(2026, 9, 7, 15, 31, tzinfo=IST))
        assert (stats['skipped'], stats['successful']) == (0, 1)


class TestBlockSnapshotsAreRebuilt:
    """The per-block snapshots rebuilt from a day's readings must equal what the
    live writer stores from the same readings: VWAP, Rolling Momentum and PV
    Divergence compare today's live value against this history."""

    TCS = (11536, 'NSE_EQ')
    # (clock, price, exchange VWAP, interval volume). Block 1 is 09:15-09:30;
    # the 09:30:00 reading is block 2's.
    READINGS = [('09:15:05', 2290.0, 2291.0, 5000), ('09:20:00', 2295.0, 2292.0, 800),
                ('09:25:00', 2293.0, 2292.5, 650), ('09:29:55', 2300.0, 2294.0, 700),
                ('09:30:00', 2301.0, 2295.0, 600)]

    @pytest.fixture
    def rebuilt(self, tmp_path, backfill, no_holidays):
        """Every reading carries Friday's close, ₹2,304.00, as a Monday reading
        does; the backfill files it as 09-04's close, then rebuilds 09-07."""
        module, _ = backfill
        db = engine_mod.OHLCDatabase(db_path=str(tmp_path / 'ohlc.db'))
        with sqlite3.connect(db.db_path) as conn:
            for clock, price, vwap, iv in self.READINGS:
                conn.execute(
                    "INSERT INTO ohlc_data (timestamp, asset_id, symbol, segment_code, "
                    "last_price, open_price, high_price, low_price, close_price, volume, "
                    "interval_volume, average_price) "
                    "VALUES (?, ?, 'TCS', ?, ?, ?, ?, ?, 2304.0, 100, ?, ?)",
                    (f'2026-09-07T{clock}+05:30', *self.TCS, price, price, price, price, iv, vwap))
        hist = str(tmp_path / 'historical.db')
        module.HistoricalBackfiller(db.db_path, hist).backfill_date_range(
            [date(2026, 9, 7)], force=True, now=LATER)
        with sqlite3.connect(hist) as conn:
            rows = conn.execute(
                "SELECT time_block, session_price_move, vwap_displacement, pvd_raw "
                "FROM intraday_time_block_metrics WHERE date = '2026-09-07'").fetchall()
        return {r[0]: r[1:] for r in rows}, db.db_path

    def test_only_blocks_with_readings_get_rows(self, rebuilt):
        rows, _ = rebuilt
        assert set(rows) == {1, 2}

    def test_block_1_equals_the_live_writer(self, rebuilt):
        rows, ohlc_path = rebuilt
        live = object.__new__(metrics.MetricsOrchestrator)
        live.ohlc_db_manager = OHLCDatabaseReader(ohlc_path)
        expected = live._block_end_values(*self.TCS, date(2026, 9, 7), 1, 2304.0)
        wanted = (expected['session_price_move'], expected['vwap_displacement'],
                  expected['pvd_raw'])
        for got, want in zip(rows[1], wanted):
            assert got is None if want is None else got == pytest.approx(want)
        assert rows[1][0] == pytest.approx((2300.0 / 2304.0 - 1) * 100)
