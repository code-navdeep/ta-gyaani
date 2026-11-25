"""
Segment metadata — physical facts about market segments, not tunable settings.

Deliberately dependency-free (no imports at all) so that `engine.py`, the
adapters and `metrics.py` can all import it without creating a cycle. `engine.py`
imports nothing else local; this is the one exception, and it is a leaf.

Why this exists as a shared module rather than a literal at each site: the rule
below governs five separate places — the engine's interval calculation, its
record construction, its time-block storage, the Volume Block adapter, and the
dashboard's volume subplot. Scattered `== 'IDX_I'` checks are exactly how the
previous inconsistency arose: `dashboard.py` believed the engine filtered index
volume ("engine writes 0 for IDX_I interval_volume") and the engine did no such
thing, so the two disagreed silently for as long as the comment existed.
"""

# Segments whose instruments are NOT TRADEABLE and therefore have NO VOLUME.
#
# An index is a computed number, not an instrument. Nobody can trade one, so it
# has no traded volume — ever, not "usually zero". Whatever a quote endpoint
# returns in the `volume` field for an index is not a measurement of anything.
#
# Applied consistently at every volume site:
#   - ohlc_data.volume / .interval_volume stored as NULL, never 0, so that
#     "this instrument has no volume" stays distinguishable from "zero traded"
#   - no time_block_volumes row written at all
#   - volume_block_adapter emits no VolumeBlockRow, so Volume Dynamics — which is
#     built entirely on Volume Block's z-score — emits nothing for indices either
#
# NSE_EQ is the only volume-bearing segment in the current universe. Note that
# Sensex trades on BSE but carries segment_code 'IDX_I' like every other index,
# so exchange is irrelevant here; only tradeability matters.
NON_VOLUME_SEGMENTS = frozenset({'IDX_I'})


def has_volume(segment_code: str) -> bool:
    """True if instruments in this segment can be traded, and so have volume.

    A missing segment (None, or the NaN pandas reads from a blank CSV cell) is
    refused with a ValueError rather than answered: "unknown" is not "has
    volume", and guessing either way hides a broken asset row.
    """
    if not isinstance(segment_code, str) or not segment_code:
        raise ValueError(f"not a segment code: {segment_code!r}")
    return segment_code not in NON_VOLUME_SEGMENTS
