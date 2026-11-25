"""
The market calendar: which days are trading days.

A trading day is a weekday not listed in `data/holidays.txt`. On a listed
holiday everything is skipped — the engine does not fetch, the metrics thread
does not run, and the end-of-day jobs at 15:34 (archive, cleanup, token renewal)
do not run. A holiday collected and archived as if it were a trading day puts a
spurious sample into every asset's every block, and a spurious `daily_ohlc` row
into gap history and beta.

The app is started at the open, stopped after the close, and never run on a
holiday, so this gate sits behind the operator; it is a second safety, not the
first.

`holidays.txt`: one date per line as dd/mm/yyyy; lines starting with `#` are
comments. It lives in `data/` (bind-mounted, so an edit needs no rebuild) and
needs next year's dates added each year, once NSE publishes its holiday list
for that year (late in the year).

None of these stops the app — each is logged at ERROR and the app runs,
skipping weekends plus whatever dates did parse:
  - the file is missing;
  - a line is not a dd/mm/yyyy date (named by line number, then ignored);
  - no date in the file falls in the current year.
"""

import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import FrozenSet, Iterable, Optional

from calculators.time_blocks import IST

logger = logging.getLogger(__name__)

HOLIDAYS_FILE = Path(__file__).resolve().parent.parent / 'data' / 'holidays.txt'

_holidays: Optional[FrozenSet[date]] = None
_lock = threading.Lock()


def parse_holidays(lines: Iterable[str], source: str = 'holidays.txt') -> FrozenSet[date]:
    """The dates in `lines`. Blank lines and `#` comments are skipped; a line
    that is not a dd/mm/yyyy date is logged at ERROR and ignored."""
    found = set()
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        try:
            found.add(datetime.strptime(line, '%d/%m/%Y').date())
        except ValueError:
            logger.error(
                f"HOLIDAY CALENDAR: {source} line {number} is not a dd/mm/yyyy date "
                f"and is ignored: {line!r}. If it was meant to be a holiday, the app "
                f"will run on that day as if it were a trading day."
            )
    return frozenset(found)


def load_holidays(path: Path = HOLIDAYS_FILE, today: Optional[date] = None) -> FrozenSet[date]:
    """Read the holiday file. Never raises for a missing or incomplete file —
    see the module docstring."""
    today = today or datetime.now(IST).date()
    try:
        with open(path, encoding='utf-8') as f:
            holidays = parse_holidays(f, source=str(path))
    except FileNotFoundError:
        logger.error(
            f"HOLIDAY CALENDAR MISSING: {path} not found. Market holidays will NOT be "
            f"skipped — on a holiday the app will fetch, compute and archive as if it "
            f"were a trading day."
        )
        return frozenset()
    if not any(d.year == today.year for d in holidays):
        logger.error(
            f"HOLIDAY CALENDAR: {path} lists no holiday in {today.year}, so this year's "
            f"holidays will NOT be skipped. Add them from NSE's holiday calendar."
        )
    return holidays


def is_market_holiday(day: date) -> bool:
    """True if `day` is listed in holidays.txt. The file is read once per
    process, on first use."""
    global _holidays
    with _lock:
        if _holidays is None:
            _holidays = load_holidays()
    return day in _holidays


def is_trading_day(day: date) -> bool:
    """A weekday that is not a listed market holiday."""
    return day.weekday() < 5 and not is_market_holiday(day)


def previous_trading_day(day: date) -> date:
    """The latest trading day before `day` — never simply the previous calendar
    day. Monday 2026-09-07 → Friday 2026-09-04; Tuesday 2026-09-15 → Friday
    2026-09-11, because Monday 14/09 (Ganesh Chaturthi) is listed. It can only
    be as right as holidays.txt: an unlisted holiday counts as a trading day."""
    prev = day - timedelta(days=1)
    while not is_trading_day(prev):
        prev -= timedelta(days=1)
    return prev
