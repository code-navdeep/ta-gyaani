"""
Trading-session 15-minute block grid — single source of truth.

The NSE/BSE equity session runs 09:15–15:30 IST: 375 minutes, divided into
exactly **25 blocks of 15 minutes**. Block `b` (1-indexed) covers
`[09:15 + 15(b-1), 09:15 + 15b)` and *ends* at `09:15 + 15b`:

    block  1  →  09:15–09:30   (ends 09:30)
    block  2  →  09:30–09:45   (ends 09:45)
    ...
    block 25  →  15:15–15:30   (ends 15:30)

Two distinct questions are asked about this grid, and they are NOT the same:

  * **"Which block has most recently finished?"** — `current_completed_block()`.
    Used by the rolling calculators (VWAP, Volume Block, PV Divergence,
    Rolling Momentum) to pick the block whose history they z-score against.
    Returns 25 only at/after 15:30, which is why Volume Block's block-25 row
    lands during the metrics post-close grace rather than during the session.

  * **"Is the clock inside a block's write bracket right now?"** —
    `block_at_detection_window()`. Used by the engine's 5-second loop, which
    rewrites the block's live total on every fetch inside the bracket, judging
    each write for completeness (`block_complete`), so the last write stands.
    Each bracket is ±1 minute around the block end (a 2-minute window), wide
    enough that a 5-second poll cannot step over it. The shared-store writer
    does NOT use it: it waits for the block's reading end
    (`block_reading_end_time`).

Before this module the grid was redeclared in several places, in three
different encodings — four adapters (reversed `dt_time` completion marks; PV
Divergence used raw `(hh, mm)` tuples instead), `engine.py`'s hardcoded
25-entry bracket dict, `metrics.py`'s `_intraday_block_boundaries`, and
`scripts/daily.py`'s archive. Any change to session hours or block width needed
a correct edit in every one, and a missed copy would desynchronise the engine's
writes from the calculators' reads silently.
"""
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# The one canonical IST.
# ---------------------------------------------------------------------------
# Every timestamp this system writes is IST-aware, and this is the single
# definition of that zone. It lives here because this module already owns the
# session's time semantics (SESSION_START/SESSION_END and the block grid), and
# because it is a near-leaf: importing it cannot create a cycle.
#
# There used to be TEN declarations of this one fact — main.py held a fixed
# offset and each of the nine adapters declared its own ZoneInfo.
#
# A FIXED OFFSET, not ZoneInfo("Asia/Kolkata"), and the choice is deliberate:
# India has observed no daylight saving since 1945, so the two are
# semantically identical for every timestamp this system will ever produce —
# but ZoneInfo needs the IANA database present at runtime, and a slim base
# image without `tzdata` raises ZoneInfoNotFoundError. A fixed offset cannot
# fail that way. Both render the same '+05:30' suffix; tests/test_timestamp_
# awareness.py pins that equivalence so this cannot drift.
IST = timezone(timedelta(hours=5, minutes=30), 'IST')

# ---------------------------------------------------------------------------
# Grid definition — everything below is derived from these four constants.
# ---------------------------------------------------------------------------

SESSION_START = dt_time(9, 15)
SESSION_END = dt_time(15, 30)
BLOCK_MINUTES = 15
BLOCK_COUNT = 25          # (15*60 + 30 - (9*60 + 15)) // 15 == 375 // 15 == 25

_SESSION_START_MINUTES = SESSION_START.hour * 60 + SESSION_START.minute

# Half-width of the write-detection bracket around each block end, in minutes.
# 1 → a 2-minute window (e.g. block 1 detected across 09:29–09:31).
DETECTION_HALF_WIDTH_MINUTES = 1


def _minutes_to_time(total_minutes: int) -> dt_time:
    return dt_time(total_minutes // 60, total_minutes % 60)


def block_end_minutes(block: int) -> int:
    """Minutes-since-midnight at which `block` ends. Block 1 → 570 (09:30)."""
    if not 1 <= block <= BLOCK_COUNT:
        raise ValueError(f"block must be 1..{BLOCK_COUNT}, got {block}")
    return _SESSION_START_MINUTES + BLOCK_MINUTES * block


def block_end_time(block: int) -> dt_time:
    """Wall-clock time at which `block` ends. Block 25 → 15:30."""
    return _minutes_to_time(block_end_minutes(block))


def block_start_time(block: int) -> dt_time:
    """Wall-clock time at which `block` begins. Block 1 → 09:15."""
    return _minutes_to_time(block_end_minutes(block) - BLOCK_MINUTES)


# The session's final readings count into block 25, up to this cut-off.
#
# Under the closing auction held in the session's last fifteen minutes (from
# ~15:15 the quotes freeze until the auction's result arrives), the day's
# full total can arrive stamped AFTER 15:30:00: on 2026-09-07 TCS's auction
# count (132,757 shares) and closing price arrived in the reading stamped
# 15:30:02, after block 25 ([15:15, 15:30)) had ended — so it fell outside
# every block and block 25 held only 1,913 shares. On 2026-09-04 the same
# result arrived at 15:28:39, inside the block. Counting the final readings
# into block 25 makes it hold the auction either way, with no special case if
# the trial ends.
#
# The cut-off, not "whatever comes last": the engine totals block 25 live only
# until 15:31 (its detection window, below), and the archive recounts at 15:34.
# A reading stamped after 15:31 can only come from something abnormal, such as
# a stray late fetch; counting it would make the archived block 25 — the
# yardstick for every future block-25 z-score — differ from what was shown
# live. Nothing stamped at or after the cut-off counts in any block.
FINAL_READINGS_CUTOFF = dt_time(15, 31)


def block_reading_end_time(block: int) -> dt_time:
    """End (exclusive) of the readings that count into `block`.

    The block's own end, except block 25, which also takes the session's final
    readings up to FINAL_READINGS_CUTOFF. Used by every writer that turns
    readings into a block value: the engine's live block total, the archive's
    recount, and the shared-store snapshot.
    """
    end = block_end_time(block)  # validates `block`
    return FINAL_READINGS_CUTOFF if block == BLOCK_COUNT else end


# ---------------------------------------------------------------------------
# Block completeness — when a block's volume (or its end-of-block value) can
# be trusted.
# ---------------------------------------------------------------------------
# A reading's interval is its running total minus the last GOOD reading's (one
# whose running total was stored), so the intervals inside a block add up to:
#
#     total at the block's LAST reading - total at the last reading BEFORE it.
#
# The block is therefore exact except at its two edges. It wrongly GAINS the
# volume traded between the last reading before its start and the start, and
# LOSES the volume between its last reading and its end. At the engine's ~5 s
# spacing each edge is ~5 s of trading (~0.6% of a block). A gap wholly INSIDE
# a block does not change its total at all; only time left unobserved at one of
# its two boundaries does.
#
# Worked example, T = 30 s: readings at 10:44:58, then 10:45:30. Block 6
# (10:30-10:45) misses 2 s at its end. Block 7's first reading carries 32 s of
# trading, but 30 s of it (10:45:00-10:45:30) is block 7's own, so block 7 is
# off by 2 s at its start. Both complete. Readings stopping at 10:37:22 and
# resuming at 11:13:02 (TCS, 2026-09-02): block 6 misses 7 min 38 s at its end,
# block 8 gains 23 min at its start — both incomplete.
#
# 30 s is six times the engine's normal ~5 s spacing, so ordinary jitter
# between readings never marks a block incomplete; only a real interruption
# (a late start, a dropped connection, a stalled fetch) does.
BLOCK_EDGE_TOLERANCE = timedelta(seconds=30)


def _clock_seconds(t: dt_time) -> float:
    return t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6


def reading_clock(stamp: Optional[str]) -> Optional[dt_time]:
    """The IST clock time of a stored reading's timestamp, as written — with
    or without a +05:30 offset, exactly as substr() reads it. None when there
    is no stamp (None, or the float NaN pandas uses for an empty text cell)."""
    return datetime.fromisoformat(stamp).time() if isinstance(stamp, str) and stamp else None


def block_start_edge_ok(block: int, prev_reading: Optional[dt_time],
                        first_reading: Optional[dt_time],
                        tolerance: timedelta = BLOCK_EDGE_TOLERANCE) -> bool:
    """True when the block's start is observed closely enough.

    `prev_reading`: clock time of the last GOOD reading before the block's
    start that day, or None if there is none. `first_reading`: clock time of
    the first good reading inside the block.

    With no earlier reading, the day's first reading is inside this block. In
    block 1 that is fine: a first reading before 09:30 carries the whole
    running total, everything since before 09:15. In any later block it is a
    late start: that reading counts nothing, so the block misses from its
    start to that reading.
    """
    start = _clock_seconds(block_start_time(block))
    limit = tolerance.total_seconds()
    if prev_reading is not None:
        return start - _clock_seconds(prev_reading) <= limit
    if first_reading is None:
        return False
    if block == 1:
        return True
    return _clock_seconds(first_reading) - start <= limit


def block_end_edge_ok(block: int, last_reading: Optional[dt_time],
                      tolerance: timedelta = BLOCK_EDGE_TOLERANCE) -> bool:
    """True when the block's last good reading came within `tolerance` of the
    block's reading end (block 25: 15:31). Also decides whether a value read at
    that reading — a price, a VWAP — can stand for the block's end."""
    if last_reading is None:
        return False
    return (_clock_seconds(block_reading_end_time(block))
            - _clock_seconds(last_reading)) <= tolerance.total_seconds()


def block_complete(block: int, prev_reading: Optional[dt_time],
                   first_reading: Optional[dt_time], last_reading: Optional[dt_time],
                   tolerance: timedelta = BLOCK_EDGE_TOLERANCE) -> bool:
    """A block's volume is complete when both of its edges were observed
    within `tolerance`. Clock times are IST wall-clock, read from the stored
    stamps exactly as the SQL's substr() reads them."""
    return (block_start_edge_ok(block, prev_reading, first_reading, tolerance)
            and block_end_edge_ok(block, last_reading, tolerance))


# Descending by end time so `current_completed_block` returns the LATEST
# finished block on its first match.
COMPLETION_MARKS: Tuple[Tuple[dt_time, int], ...] = tuple(
    (block_end_time(b), b) for b in range(BLOCK_COUNT, 0, -1)
)


def current_completed_block(now: datetime) -> Optional[int]:
    """The most recently *finished* block, or None before 09:30.

    Note this is deliberately open-ended past 15:30 — it keeps returning 25
    for the rest of the day, which is what lets Volume Block's final row land
    during the metrics post-close grace.
    """
    t = now.time()
    for end_t, block in COMPLETION_MARKS:
        if t >= end_t:
            return block
    return None


def detection_window(block: int) -> Tuple[dt_time, dt_time]:
    """The `[start, end]` bracket in which a once-per-block write should fire.

    Centred on the block end, ±DETECTION_HALF_WIDTH_MINUTES.
    Block 1 → (09:29, 09:31).
    """
    end_m = block_end_minutes(block)
    return (
        _minutes_to_time(end_m - DETECTION_HALF_WIDTH_MINUTES),
        _minutes_to_time(end_m + DETECTION_HALF_WIDTH_MINUTES),
    )


def block_at_detection_window(now: datetime) -> Optional[int]:
    """The block whose write bracket contains `now`, else None.

    Brackets do not overlap (2-minute windows spaced 15 minutes apart), so at
    most one block can match.
    """
    t = now.time()
    for block in range(1, BLOCK_COUNT + 1):
        start_t, end_t = detection_window(block)
        if start_t <= t <= end_t:
            return block
    return None


def clamp_to_day_start(cutoff_dt: datetime, anchor_dt: datetime) -> datetime:
    """Never let a rolling lookback cross into the calendar day before
    `anchor_dt` — so every chart shows exactly one trading day however large
    the requested hour window is.

    One definition, shared by the dashboard's price and index-history queries
    and metrics.db's history query: this rule used to exist as two copies.
    """
    day_start = anchor_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return max(cutoff_dt, day_start)


def seconds_until_next_block_end(now: datetime) -> Optional[float]:
    """Seconds from `now` until the next block end strictly in the future.

    Returns None once the final block (25, ending 15:30) has passed — there is
    no next boundary today, so a caller sizing a cache TTL should fall back to
    its flat default rather than caching until tomorrow.
    """
    t = now.time()
    for block in range(1, BLOCK_COUNT + 1):
        end_t = block_end_time(block)
        if t < end_t:
            end_dt = datetime.combine(now.date(), end_t)
            if now.tzinfo is not None:
                end_dt = end_dt.replace(tzinfo=now.tzinfo)
            return (end_dt - now).total_seconds()
    return None


def all_blocks() -> Tuple[int, ...]:
    """`(1, 2, ..., 25)` — for callers that need to enumerate the grid."""
    return tuple(range(1, BLOCK_COUNT + 1))


def recent_completed_blocks(now: datetime, count: int) -> Tuple[int, ...]:
    """The `count` most recently finished blocks, newest first.

    Volume Block needs the last three (current plus two prior) to supply
    Volume Dynamics with enough ZQ history for velocity, acceleration and
    3-block persistence. Truncates near the open rather than padding: at
    09:45 only blocks (2, 1) exist, so that is what comes back.
    """
    if count <= 0:
        return ()
    latest = current_completed_block(now)
    if latest is None:
        return ()
    lowest = max(1, latest - count + 1)
    return tuple(range(latest, lowest - 1, -1))
