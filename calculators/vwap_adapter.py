"""
VWAP adapter — the "shell" around VWAP_LOCKED_V1's pure `calculate_vwap_state`.

**VWAP comes from the EXCHANGE, not from us.** The quote payload carries
`average_price` — the exchange's own volume-weighted average price for the day —
and that is what this adapter feeds the calculator, read from
`PriceDataManager.get_current_prices()` in memory.

This adapter used to RECONSTRUCT the VWAP itself, summing
`last_price x interval_volume` over every 5-second tick we had captured
(via `OHLCDatabaseReader.get_daily_vwap_data`, since deleted). Four reasons that
was wrong:

  1. The locked calculator's own contract asks for the exchange's figure:
     `vwap_deviation_calculator` declares its input as "production VWAP /
     average price" and states "VWAP source must match production feed".
  2. A reconstruction is only as good as our tick coverage, and a gap is
     silent. Any late start, restart or missed poll skews it with nothing
     raised. Measured 2026-08-28 (engine up from 12:07, so partial coverage):
     divergence up to 1.108% against the exchange's number.
  3. `displacement = ln(price / vwap)` is typically a few TENTHS of a percent,
     so a 1% error in the denominator does not perturb the signal — it swamps
     it, and every z-tier, the row highlight and the VWAP glyph derive from it.
  4. `scripts/daily.py` already archived `daily_ohlc.vwap` from `average_price`,
     so the live path and the EOD path disagreed about the same stock on the
     same day. They now agree.

`previous_vwap_displacement` is Kind-B state: VWAP updates every cycle, not
at block boundaries, so "previous" genuinely means "what this adapter itself
computed last cycle" — held in memory on a persistent instance, and seeded on
startup from the most recent stored metrics.db row. SAME TRADING DAY ONLY: a
value from an earlier day (a stored row from yesterday, or a container left
running overnight) is not used, so a session's first `acceptance` is None
rather than a comparison across two days' VWAPs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from calculators.vwap_deviation_calculator import calculate_vwap_state
from calculators.schemas import VwapRow
from calculators.segments import has_volume
from calculators.statistics import FETCH_DAYS, finite_positive, tier_fields
from calculators.time_blocks import IST, current_completed_block

logger = logging.getLogger(__name__)

# LOOKBACK = 90 is retired. Tier depths live in calculators/statistics.py
# (TIERS / MAX_TIER / FETCH_DAYS); a local constant named "lookback" holding 90
# would misdescribe an adapter whose top tier is 252.

# The 25-block 15-min grid lives in calculators/time_blocks.py. It used to be
# redeclared here, and in six other places in three different encodings — so a
# change to session hours or block width needed seven correct edits, and a
# missed copy would desynchronise this calculator's reads from the engine's
# writes with nothing raised.


# `_mean_std` is gone deliberately. It computed the DISPLAYED mu/sigma
# separately from the z — the same split that made Volume Block's drawer show a
# z which could not be reconstructed from the mu/sigma printed beside it. Both
# now come from one zscore_tiers call inside the calculator. Its own [:LOOKBACK]
# slice and floor-of-20 would also have capped the displayed stats at 90 samples
# and reported stats for tiers that have no z.


def _blank_vwap_row(asset_id, segment, symbol, timestamp: str, status: str,
                    current_price: Optional[float] = None) -> VwapRow:
    """A VwapRow carrying identity and a REASON, but no VWAP measurement.

    Emitted instead of skipping the asset. Skipping made two different states
    look identical on the health bar: "this calculator ran and had nothing to
    compute for this asset" and "this calculator did not run at all". Only the
    second is a fault, and the dashboard could not tell them apart.

    The 4 indices reach this path every single cycle: an index is not tradeable
    and the engine stores NULL volume for it, so a VOLUME-weighted average price
    does not exist. That is a permanent property of an index, not a data gap —
    so it is reported (status='no_volume_variation', matching what PV Divergence
    already emits for the same 4 assets in the same situation) rather than
    silently dropping them from an 86-asset universe down to 82.
    """
    return VwapRow(
        asset_id=int(asset_id),
        segment_code=segment,
        symbol=symbol,
        current_price=current_price,
        timestamp=timestamp,
        status=status,
    )


class VwapAdapter:
    def __init__(self, ohlc_db_manager, historical_db_manager, metrics_db_manager=None,
                 asset_loader=None, price_data_manager=None):
        self.ohlc_db = ohlc_db_manager
        self.historical_db = historical_db_manager
        self.metrics_db = metrics_db_manager
        self.asset_loader = asset_loader
        # Source of the exchange's own `average_price` — the VWAP this adapter
        # reports. In memory, O(1), refreshed over ZMQ every 5 seconds.
        self.price_data_manager = price_data_manager
        # Kind-B state: {(asset_id, segment_code): (its date, last displacement)}.
        # The date is kept so only a value from the SAME trading day is used.
        self._previous_displacement: Dict[Tuple[int, str], Tuple[date, float]] = {}
        self._hydrate()

    def _hydrate(self) -> None:
        """Seed previous-displacement state from the most recent stored row
        per asset, so a container restart costs one cycle of None, not a
        cold start every time."""
        if self.metrics_db is None:
            return
        try:
            rows, _ = self.metrics_db.get_latest_vwap()
        except Exception as exc:
            logger.warning("vwap_adapter: hydration read failed: %s", exc)
            return
        for row in rows or []:
            aid, seg = row.get('asset_id'), row.get('segment_code')
            disp, stamp = row.get('displacement'), row.get('timestamp')
            if aid is None or disp is None or not stamp:
                continue
            try:
                day = datetime.fromisoformat(stamp).astimezone(IST).date()
            except (TypeError, ValueError):
                continue
            self._previous_displacement[(aid, seg)] = (day, float(disp))
        logger.info("vwap_adapter: hydrated %d previous-displacement values", len(self._previous_displacement))

    def calculate_vwap_analysis(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> List[VwapRow]:
        now = (now or datetime.now(IST)).astimezone(IST)
        if self.asset_loader is None or getattr(self.asset_loader, 'assets_df', None) is None:
            return []

        frame = self.asset_loader.assets_df
        ids = frame['asset_id'].tolist()
        segments = frame['segment_code'].tolist()
        if not ids:
            return []

        symbol_col = frame['symbol'] if 'symbol' in frame.columns else frame.get('asset_name')
        symbol_map = {}
        if symbol_col is not None:
            symbol_map = {(aid, seg): sym for aid, seg, sym in
                           zip(frame['asset_id'], frame['segment_code'], symbol_col)}

        # Fetched ONCE, not per asset. The old code called get_daily_vwap_data
        # inside the loop — 82 SQLite queries every 30-second cycle — where every
        # other price-consuming adapter (momentum, rolling_momentum,
        # relative_strength) makes a single in-memory call before looping.
        #
        # `return []` here is the genuinely universe-wide case: no live quotes at
        # all means nothing can be said about ANY asset. It is NOT the
        # per-asset case (no quote or no history for one asset), which still
        # emits a row for that asset carrying a status: a skipped asset reads
        # as a stopped calculator on the health bar.
        if self.price_data_manager is None or not self.price_data_manager.is_ready():
            return []
        prices = self.price_data_manager.get_current_prices(ids, segments)
        if prices is None or prices.empty:
            return []
        # Keyed on the COMPOSITE (asset_id, segment_code): asset_id 25 is
        # BankNifty in IDX_I and ADANIENT in NSE_EQ, so keying on asset_id alone
        # would hand one entity's price to the other.
        price_map = {(r.asset_id, r.segment_code): r
                     for r in prices.itertuples(index=False)}

        block = current_completed_block(now)

        history_by_asset: Dict[Tuple[int, str], List[float]] = {}
        if block is not None:
            # FETCH_DAYS = MAX_TIER * 2 = 504. The old LOOKBACK*2 = 180 could not
            # supply the 252 samples the top tier needs.
            #
            # exclude_date drops TODAY from the comparison window. This table is
            # written live at each block boundary, so today's own displacement is
            # already in it — and a point included in its own window pulls mu
            # toward itself and inflates sigma, compressing the reading exactly
            # when it is most extreme. VWAP is one of the three calculators that
            # read this store live (with Rolling Momentum and PV Divergence).
            hist_frame = self.historical_db.get_historical_intraday_metrics(
                ids, segments, time_blocks=[block], days_back=FETCH_DAYS,
                columns=['vwap_displacement'],
                exclude_date=now.date().isoformat(),
            )
            if hist_frame is not None and not hist_frame.empty:
                hist_frame = hist_frame.dropna(subset=['vwap_displacement'])
                for key, grp in hist_frame.groupby(['asset_id', 'segment_code'], dropna=False):
                    # Most-recent-first, passed WHOLE — no [:LOOKBACK] truncation.
                    # zscore_tiers owns the per-tier slicing.
                    grp = grp.sort_values('date', ascending=False)
                    history_by_asset[key] = grp['vwap_displacement'].astype(float).tolist()

        rows: List[VwapRow] = []

        for asset_id, segment in zip(ids, segments):
            symbol = symbol_map.get((asset_id, segment))

            # THIS GUARD IS LOAD-BEARING — it is now the ONLY thing excluding
            # indices, and it must stay ABOVE the lookup below.
            #
            # It used to be a tidy early exit: the old get_daily_vwap_data
            # filtered `interval_volume > 0`, an index's interval_volume is
            # NULL, and NULL > 0 is never true — so indices fell out
            # incidentally. That query is gone, and the accidental exclusion
            # went with it.
            #
            # Verified 2026-08-28: all 5,264 stored IDX_I rows carry
            # `average_price` NULL, so without this guard every index would drop
            # to the 'insufficient_data' branch below. That status means "no
            # quote yet, wait" — a promise this asset can never keep. An index is
            # not tradeable, has no volume, and therefore has no volume-weighted
            # average price, today or ever. Report the PERMANENT reason instead,
            # using the same word PV Divergence uses for these same 4 assets.
            if not has_volume(segment):
                rows.append(_blank_vwap_row(
                    asset_id, segment, symbol, now.isoformat(), 'no_volume_variation'))
                continue

            quote = price_map.get((asset_id, segment))
            avg = getattr(quote, 'average_price', None) if quote is not None else None
            last = getattr(quote, 'last_price', None) if quote is not None else None

            # `average_price` IS the VWAP — the exchange's own, for the whole
            # session. Nothing is recomputed from it.
            #
            # It arrives rounded to 2dp (engine.py, at the quote-parse site).
            # Relative precision is ~4e-6 at Rs 2,325 and ~7e-4 at Rs 14.70, and
            # displacement is ln(price/vwap), so the rounding is well below the
            # signal at every price in this universe.
            if not finite_positive(avg) or not finite_positive(last):
                # No quote yet, or the broker sent no average_price for it.
                # Carry the price if we have it — only the VWAP is missing.
                rows.append(_blank_vwap_row(
                    asset_id, segment, symbol, now.isoformat(), 'insufficient_data',
                    current_price=float(last) if finite_positive(last) else None))
                continue

            vwap = float(avg)
            current_price = float(last)

            key = (asset_id, segment)
            held = self._previous_displacement.get(key)
            # Only a value from TODAY: acceptance across two days would compare
            # two different VWAPs.
            prev_disp = held[1] if held is not None and held[0] == now.date() else None
            same_block_hist = history_by_asset.get(key, [])

            result = calculate_vwap_state(
                price=current_price,
                vwap=vwap,
                previous_vwap_displacement=prev_disp,
                previous_same_time_vwap_history=same_block_hist,
            )

            if result.displacement is not None:
                self._previous_displacement[key] = (now.date(), result.displacement)

            rows.append(VwapRow(
                asset_id=int(asset_id),
                segment_code=segment,
                symbol=symbol_map.get((asset_id, segment)),
                current_price=current_price,
                timestamp=now.isoformat(),
                vwap=vwap,
                displacement=result.displacement,
                side=result.side,
                acceptance=result.acceptance,
                status=result.status,
                # z, mu and sigma all come from the calculator's single
                # zscore_tiers call, so mu/sigma cannot end up in a different
                # unit space from the z they sit beside — the defect that made
                # Volume Block's drawer irreconcilable.
                **tier_fields('vwap', result.displacement_tiers),
            ))

        return rows


def create_calculator(ohlc_db_manager, historical_db_manager, metrics_db_manager=None,
                      asset_loader=None, price_data_manager=None) -> VwapAdapter:
    return VwapAdapter(ohlc_db_manager, historical_db_manager, metrics_db_manager,
                       asset_loader, price_data_manager)
