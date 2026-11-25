"""
SQL date/time extraction must not depend on how the timestamp is STORED.

SQLite's date functions PARSE their argument, and when it carries a UTC offset
they convert to UTC before extracting:

    strftime('%H:%M', '2026-08-31T09:15:00')          ->  09:15
    strftime('%H:%M', '2026-08-31T09:15:00+05:30')    ->  03:45   <- converted
    DATE('2026-08-31T00:30:00+05:30')                 ->  2026-08-30

Every extraction in this codebase therefore uses `substr()`, a lexical slice
that never invokes that logic. Characters 1-19 of an ISO-8601 timestamp are
`YYYY-MM-DDTHH:MM:SS` whether or not an offset follows, so the same expression
yields IST wall-clock under either storage format.

WHY THIS MATTERS ENOUGH TO TEST. `scripts/daily.py` selects the session's
opening rows by matching the literal string '09:15', and bounds each 15-minute
block with literal '%H:%M:%S' values. Under a converted timestamp those match
NOTHING — every `daily_ohlc.open_price` archives as NULL and
`time_block_volumes` archives empty, with no exception raised. `daily_ohlc`
feeds momentum gap-z, beta, ATR and the previous close, so the failure is both
silent and foundational.

Each test seeds the SAME INSTANT in both formats. A test that checked only
today's naive storage would pass identically before and after the fix and would
guard nothing.
"""
import sqlite3

import pytest

# The same instant, written both ways. 09:15:00 IST is the session open — the
# exact value daily.py's open_price CASE arm matches on.
NAIVE_OPEN = '2026-08-31T09:15:00.123456'
AWARE_OPEN = '2026-08-31T09:15:00.123456+05:30'

# 15:25:30 IST — a late-session row, matched by the closing-time arm of the
# query in TestDailyExtractionShape (`>= '15:25'`).
NAIVE_CLOSE = '2026-08-31T15:25:30.000000'
AWARE_CLOSE = '2026-08-31T15:25:30.000000+05:30'


@pytest.fixture
def conn():
    """An ohlc_data-shaped table holding one naive and one aware row per
    instant, so every assertion below compares the two formats directly."""
    c = sqlite3.connect(':memory:')
    c.execute("""
        CREATE TABLE ohlc_data (
            timestamp TEXT,
            asset_id INTEGER,
            segment_code TEXT,
            open_price REAL,
            close_price REAL,
            interval_volume INTEGER
        )
    """)
    c.executemany(
        "INSERT INTO ohlc_data VALUES (?, ?, ?, ?, ?, ?)",
        [
            (NAIVE_OPEN,  1, 'NSE_EQ', 100.0, 100.0, 500),
            (AWARE_OPEN,  2, 'NSE_EQ', 100.0, 100.0, 500),
            (NAIVE_CLOSE, 1, 'NSE_EQ', 110.0, 110.0, 700),
            (AWARE_CLOSE, 2, 'NSE_EQ', 110.0, 110.0, 700),
        ],
    )
    c.commit()
    yield c
    c.close()


class TestSubstrIsFormatIndependent:
    """The three substitutions used throughout the codebase."""

    def test_date_slice_matches_across_formats(self, conn):
        rows = conn.execute(
            "SELECT DISTINCT substr(timestamp, 1, 10) FROM ohlc_data"
        ).fetchall()
        assert rows == [('2026-08-31',)], (
            f"expected one distinct date across both formats, got {rows}. "
            f"More than one means the aware row resolved to a different day."
        )

    def test_time_slice_matches_across_formats(self, conn):
        naive = conn.execute(
            "SELECT substr(timestamp, 12, 8) FROM ohlc_data WHERE timestamp = ?",
            (NAIVE_OPEN,)).fetchone()[0]
        aware = conn.execute(
            "SELECT substr(timestamp, 12, 8) FROM ohlc_data WHERE timestamp = ?",
            (AWARE_OPEN,)).fetchone()[0]
        assert naive == aware == '09:15:00', (
            f"naive={naive} aware={aware}; both must read IST wall-clock"
        )

    def test_strftime_over_slice_matches_across_formats(self, conn):
        naive = conn.execute(
            "SELECT strftime('%H:%M', substr(timestamp, 1, 19)) FROM ohlc_data "
            "WHERE timestamp = ?", (NAIVE_OPEN,)).fetchone()[0]
        aware = conn.execute(
            "SELECT strftime('%H:%M', substr(timestamp, 1, 19)) FROM ohlc_data "
            "WHERE timestamp = ?", (AWARE_OPEN,)).fetchone()[0]
        assert naive == aware == '09:15'


class TestNegativeControls:
    """Prove the fix is load-bearing.

    These assert the BROKEN behaviour of the bare-column forms. If SQLite ever
    stopped converting offsets to UTC these would fail — which is the signal we
    want, because the whole substr convention exists only because it does.
    """

    def test_bare_strftime_shifts_the_hour_on_an_aware_timestamp(self, conn):
        got = conn.execute(
            "SELECT strftime('%H:%M', timestamp) FROM ohlc_data WHERE timestamp = ?",
            (AWARE_OPEN,)).fetchone()[0]
        assert got == '03:45', (
            f"expected the documented UTC conversion 03:45, got {got}. "
            f"If this changed, revisit the substr convention."
        )

    def test_bare_time_shifts_the_hour_on_an_aware_timestamp(self, conn):
        got = conn.execute(
            "SELECT TIME(timestamp) FROM ohlc_data WHERE timestamp = ?",
            (AWARE_OPEN,)).fetchone()[0]
        assert got == '03:45:00'

    def test_bare_date_rolls_back_a_day_before_0530_ist(self, conn):
        """The DATE() sites are safe only because session hours 09:15-15:30 IST
        land in the same UTC calendar day. Anything before 05:30 IST does not."""
        got = conn.execute(
            "SELECT DATE('2026-08-31T00:30:00+05:30')").fetchone()[0]
        assert got == '2026-08-30'


class TestDailyExtractionShape:
    """End-to-end over a query in the shape of daily.py's open-price CASE
    expression, with a closing-time arm written the same way.

    The archive itself is exercised by tests/test_daily_archive.py.
    """

    QUERY = """
        SELECT
            asset_id,
            MIN(CASE WHEN strftime('%H:%M', substr(timestamp, 1, 19)) = '09:15'
                     THEN open_price END) AS open_price,
            MAX(CASE WHEN strftime('%H:%M', substr(timestamp, 1, 19)) >= '15:25'
                     THEN close_price END) AS close_price
        FROM ohlc_data
        WHERE substr(timestamp, 1, 10) = ?
        GROUP BY asset_id
    """

    def test_open_and_close_are_non_null_under_both_formats(self, conn):
        rows = dict(
            (r[0], (r[1], r[2]))
            for r in conn.execute(self.QUERY, ('2026-08-31',)).fetchall()
        )
        assert set(rows) == {1, 2}, f"both assets must be grouped, got {set(rows)}"
        # asset 1 = naive rows, asset 2 = aware rows. Identical results required.
        assert rows[1] == (100.0, 110.0), f"naive rows gave {rows[1]}"
        assert rows[2] == (100.0, 110.0), (
            f"aware rows gave {rows[2]} — a None here is the silent NULL-archive "
            f"failure this whole change exists to prevent"
        )

    def test_block_window_bounds_select_the_right_rows(self, conn):
        """daily.py / engine.py bound each 15-min block with substr(ts,12,8)
        against IST '%H:%M:%S' literals."""
        for asset_id in (1, 2):
            total = conn.execute("""
                SELECT COALESCE(SUM(interval_volume), 0) FROM ohlc_data
                WHERE asset_id = ?
                  AND substr(timestamp, 1, 10) = ?
                  AND substr(timestamp, 12, 8) >= ?
                  AND substr(timestamp, 12, 8) < ?
            """, (asset_id, '2026-08-31', '09:15:00', '09:30:00')).fetchone()[0]
            assert total == 500, (
                f"asset {asset_id} block-1 volume was {total}, expected 500. "
                f"0 means the window matched nothing — an empty archive."
            )
