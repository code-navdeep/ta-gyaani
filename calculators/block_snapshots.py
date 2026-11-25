"""
The per-block snapshot — the three values the shared store
(historical.db.intraday_time_block_metrics) records for each asset at the END
of each 15-minute block:

  - session_price_move: (the block's last price / previous close - 1) x 100
  - vwap_displacement:  ln(that price / the same reading's exchange VWAP)
  - pvd_raw:            PV Divergence's Fisher value over the block's readings

computed from the block's OWN readings in ohlc.db — [block start, block reading
end), block 25 up to 15:31 — by the functions the live calculators use, so the
stored history and the live value it is compared against are the same quantity.

Two writers use it and must agree: the metrics thread records each block live,
just after it ends (MetricsOrchestrator._maybe_write_intraday_block), and
scripts/backfill.py rebuilds whole days from ohlc.db (rebuild_block_snapshots).
One function, so the two cannot drift apart.
"""
from datetime import date, datetime, timedelta
from typing import Dict, Optional

import pandas as pd

from calculators.price_volume_divergence_adapter import continuous_price_volume_series
from calculators.price_volume_divergence_calculator import calculate_pvd_v3
from calculators.statistics import finite_positive
from calculators.time_blocks import (BLOCK_EDGE_TOLERANCE, IST, block_end_edge_ok,
                                     block_reading_end_time, block_start_time, reading_clock)
from calculators.vwap_deviation_calculator import calculate_displacement


def block_end_values(readings: Optional[pd.DataFrame],
                     previous_close: Optional[float],
                     day: date,
                     block: int,
                     day_first: Optional[str],
                     tolerance: timedelta = BLOCK_EDGE_TOLERANCE) -> Dict[str, Optional[float]]:
    """The three snapshot values for one asset at the end of one block.

    `readings`: the asset's rows from ohlc.db (timestamp, last_price,
    average_price, volume, interval_volume) covering at least [block start -
    `tolerance`, block reading end) — more is fine; only the block's own
    readings, [block start, block reading end), give values. `previous_close`:
    the close of the day's previous trading day; None leaves the price move
    empty. `day_first`: the stamp of the day's first reading with a running
    total (OHLCDatabaseReader.session_first_reading).

      - Price move and VWAP distance come from the block's last reading with a
        price, and only when it came within `tolerance` of the block's end
        (time_blocks.block_end_edge_ok): an older one is not the block's end
        value — after an outage it can be minutes stale.
      - The PV Divergence value needs every counted reading in the block to
        follow the one before it within `tolerance`, and never counts the day's
        first reading (continuous_price_volume_series); otherwise None.

    A block with no reading of its own records None for all three — never an
    earlier block's price.
    """
    values: Dict[str, Optional[float]] = {
        'session_price_move': None, 'vwap_displacement': None, 'pvd_raw': None,
    }
    if readings is None or readings.empty:
        return values
    start = datetime.combine(day, block_start_time(block), tzinfo=IST)
    end = datetime.combine(day, block_reading_end_time(block), tzinfo=IST).isoformat()
    # ISO text bounds, compared as text — as get_intraday_data_range compares them.
    context = readings[(readings['timestamp'] >= (start - tolerance).isoformat())
                       & (readings['timestamp'] < end)].sort_values('timestamp')
    own = context[context['timestamp'] >= start.isoformat()]
    if own.empty:
        return values

    last = None
    for r in own.itertuples(index=False):
        if finite_positive(r.last_price):
            last = r
    if last is not None and block_end_edge_ok(block, reading_clock(last.timestamp), tolerance):
        price = float(last.last_price)
        if previous_close is not None:
            values['session_price_move'] = (price / previous_close - 1.0) * 100.0
        if finite_positive(last.average_price):
            values['vwap_displacement'] = calculate_displacement(price, float(last.average_price))

    series = continuous_price_volume_series(context, start.isoformat(), day_first, tolerance)
    if series is not None and len(series[0]) >= 2:
        values['pvd_raw'] = calculate_pvd_v3(
            prices=series[0], volumes=series[1], previous_fisher_history=[]).fisher_value
    return values
