"""
VWAP comes from the EXCHANGE, not from a reconstruction.

The adapter used to build its own VWAP by summing `last_price x interval_volume`
over every 5-second tick it had captured. It now reads the exchange's own
`average_price` out of the in-memory price feed and hands it straight to the
locked calculator, whose contract asks for exactly that ("production VWAP /
average price").

Why this needs a test at all: the two sources produce *plausible* numbers that
differ. Measured on 2026-08-28, the reconstruction diverged from the exchange's
figure by up to 1.108% (GODREJPROP: 2033.71 vs 2056.49). Displacement is
ln(price / vwap) and runs to a few tenths of a percent, so a wrong source does
not break anything visibly — it quietly shifts every displacement, every z-tier,
the row highlight and the VWAP glyph. Nothing would raise.

Two of these tests are structural rather than numeric:
  - `ohlc_db` is passed as None. If any DB path survived, these blow up.
  - `get_current_prices` is called exactly ONCE, not once per asset. Moving it
    back inside the loop would restore 82 lookups per 30-second cycle while
    producing identical output, so only a call count catches it.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from calculators.vwap_adapter import VwapAdapter, create_calculator

IST = ZoneInfo("Asia/Kolkata")

# Mid-session, so `current_completed_block` returns a real block and the history
# fetch is exercised rather than skipped.
NOW = datetime(2026, 8, 28, 11, 7, 0, tzinfo=IST)


class _Loader:
    """Minimal AssetLoader stand-in — the adapter reads only `.assets_df`."""

    def __init__(self, rows):
        self.assets_df = pd.DataFrame(rows)


class _Prices:
    """Stands in for PriceDataManager. Counts its own calls."""

    def __init__(self, rows, ready=True):
        self._rows = rows
        self._ready = ready
        self.call_count = 0
        self.last_args = None

    def is_ready(self):
        return self._ready

    def get_current_prices(self, asset_ids=None, segment_codes=None):
        self.call_count += 1
        self.last_args = (list(asset_ids or []), list(segment_codes or []))
        return pd.DataFrame(self._rows)


class _NoHistory:
    """historical.db stand-in with no same-block history, so the tier bundle is
    all-absent and these tests stay about the SOURCE, not the z-scoring."""

    def get_historical_intraday_metrics(self, *a, **kw):
        return None


def _adapter(asset_rows, price_rows, ready=True):
    prices = _Prices(price_rows, ready=ready)
    # ohlc_db is None ON PURPOSE — see the module docstring.
    adapter = VwapAdapter(
        None, _NoHistory(), metrics_db_manager=None,
        asset_loader=_Loader(asset_rows), price_data_manager=prices,
    )
    return adapter, prices


def _by_symbol(rows):
    return {r.symbol: r for r in rows}


# One stock, one index — both segments exercised.
ASSETS = [
    {'asset_id': 11536, 'segment_code': 'NSE_EQ', 'asset_name': 'TCS'},
    {'asset_id': 13,    'segment_code': 'IDX_I',  'asset_name': 'Nifty'},
]


class TestSourceIsTheExchange:

    def test_vwap_is_average_price_verbatim(self):
        """The reported VWAP must BE `average_price`, not something derived from
        it. An exact-equality assertion is deliberate: a reconstruction would
        land near this value, not on it."""
        adapter, _ = _adapter(ASSETS, [
            {'asset_id': 11536, 'segment_code': 'NSE_EQ',
             'last_price': 2334.0, 'average_price': 2325.35},
            {'asset_id': 13, 'segment_code': 'IDX_I',
             'last_price': 24106.8, 'average_price': None},
        ])
        tcs = _by_symbol(adapter.calculate_vwap_analysis(now=NOW))['TCS']
        assert tcs.vwap == 2325.35, (
            f"VWAP is {tcs.vwap}, expected the exchange's average_price 2325.35 "
            f"exactly. A near-miss means something is recomputing it."
        )

    def test_current_price_is_last_price(self):
        adapter, _ = _adapter(ASSETS, [
            {'asset_id': 11536, 'segment_code': 'NSE_EQ',
             'last_price': 2334.0, 'average_price': 2325.35},
            {'asset_id': 13, 'segment_code': 'IDX_I',
             'last_price': 24106.8, 'average_price': None},
        ])
        tcs = _by_symbol(adapter.calculate_vwap_analysis(now=NOW))['TCS']
        assert tcs.current_price == 2334.0
        assert tcs.status == 'valid'

    def test_displacement_is_computed_from_the_exchange_vwap(self):
        """price 2334.0 against vwap 2325.35 is a positive displacement and
        side=+1. Pins that the swapped source actually reaches the calculator."""
        adapter, _ = _adapter(ASSETS, [
            {'asset_id': 11536, 'segment_code': 'NSE_EQ',
             'last_price': 2334.0, 'average_price': 2325.35},
            {'asset_id': 13, 'segment_code': 'IDX_I',
             'last_price': 24106.8, 'average_price': None},
        ])
        tcs = _by_symbol(adapter.calculate_vwap_analysis(now=NOW))['TCS']
        assert tcs.displacement is not None and tcs.displacement > 0
        assert tcs.side == 1


class TestMissingAveragePrice:

    @pytest.mark.parametrize('avg', [None, 0, -1.0, float('nan')])
    def test_absent_or_nonpositive_average_price_is_insufficient_data(self, avg):
        """The broker does not always send it. That is 'no measurement', not a
        fabricated one — and the price we DO have is still carried, so the row
        is not emptier than the data warrants."""
        adapter, _ = _adapter(
            [{'asset_id': 11536, 'segment_code': 'NSE_EQ', 'asset_name': 'TCS'}],
            [{'asset_id': 11536, 'segment_code': 'NSE_EQ',
              'last_price': 2334.0, 'average_price': avg}],
        )
        row = adapter.calculate_vwap_analysis(now=NOW)[0]
        assert row.status == 'insufficient_data'
        assert row.vwap is None
        assert row.displacement is None
        assert row.current_price == 2334.0, "the known price must still be reported"

    def test_one_asset_missing_from_the_feed_still_emits_its_row(self):
        """A PER-ASSET gap must not remove the asset. No row reads as a stalled
        calculator on the health bar.

        Distinct from an empty feed — see
        TestStructure.test_no_live_quotes_at_all_returns_empty. Only the
        universe-wide case justifies returning nothing."""
        adapter, _ = _adapter(
            [{'asset_id': 11536, 'segment_code': 'NSE_EQ', 'asset_name': 'TCS'},
             {'asset_id': 1594,  'segment_code': 'NSE_EQ', 'asset_name': 'INFY'}],
            # INFY quoted, TCS absent.
            [{'asset_id': 1594, 'segment_code': 'NSE_EQ',
              'last_price': 1144.0, 'average_price': 1139.49}],
        )
        rows = _by_symbol(adapter.calculate_vwap_analysis(now=NOW))
        assert set(rows) == {'TCS', 'INFY'}, "the unquoted asset must still appear"
        assert rows['TCS'].status == 'insufficient_data'
        assert rows['TCS'].vwap is None
        assert rows['TCS'].current_price is None
        # The quoted one is unaffected by its neighbour's gap.
        assert rows['INFY'].status == 'valid'
        assert rows['INFY'].vwap == 1139.49


class TestIndices:

    def test_index_gets_a_row_with_no_volume_variation(self):
        """Reading A: the computation is skipped, the row is NOT. Absence of a
        MEASUREMENT is reported; absence of a ROW is not — a missing row is
        indistinguishable from a dead calculator."""
        adapter, _ = _adapter(ASSETS, [
            {'asset_id': 11536, 'segment_code': 'NSE_EQ',
             'last_price': 2334.0, 'average_price': 2325.35},
            {'asset_id': 13, 'segment_code': 'IDX_I',
             'last_price': 24106.8, 'average_price': None},
        ])
        nifty = _by_symbol(adapter.calculate_vwap_analysis(now=NOW))['Nifty']
        assert nifty.status == 'no_volume_variation'
        assert nifty.vwap is None

    def test_index_is_excluded_even_when_the_feed_offers_an_average_price(self):
        """The guard must not depend on the feed being empty for indices.

        Indices are excluded because an index HAS no volume-weighted average
        price, not because the data happens to be missing. Before this change
        they fell out incidentally, via a `interval_volume > 0` SQL filter that
        no longer exists. If the broker ever sent a value here, `has_volume()`
        must still refuse it."""
        adapter, _ = _adapter(ASSETS, [
            {'asset_id': 11536, 'segment_code': 'NSE_EQ',
             'last_price': 2334.0, 'average_price': 2325.35},
            {'asset_id': 13, 'segment_code': 'IDX_I',
             'last_price': 24106.8, 'average_price': 24100.0},   # <- non-null
        ])
        nifty = _by_symbol(adapter.calculate_vwap_analysis(now=NOW))['Nifty']
        assert nifty.status == 'no_volume_variation'
        assert nifty.vwap is None, "an index must never report a VWAP"


class TestStructure:

    def test_one_row_per_asset(self):
        adapter, _ = _adapter(ASSETS, [
            {'asset_id': 11536, 'segment_code': 'NSE_EQ',
             'last_price': 2334.0, 'average_price': 2325.35},
            {'asset_id': 13, 'segment_code': 'IDX_I',
             'last_price': 24106.8, 'average_price': None},
        ])
        rows = adapter.calculate_vwap_analysis(now=NOW)
        assert len(rows) == len(ASSETS)
        assert {(r.asset_id, r.segment_code) for r in rows} == {
            (11536, 'NSE_EQ'), (13, 'IDX_I')}

    def test_prices_are_fetched_once_not_per_asset(self):
        """The old code queried the DB inside the loop — 82 queries per cycle.
        Output is identical either way, so only a call count catches a
        regression here."""
        many = [{'asset_id': i, 'segment_code': 'NSE_EQ', 'asset_name': f'S{i}'}
                for i in range(1, 21)]
        feed = [{'asset_id': i, 'segment_code': 'NSE_EQ',
                 'last_price': 100.0 + i, 'average_price': 99.0 + i}
                for i in range(1, 21)]
        adapter, prices = _adapter(many, feed)
        rows = adapter.calculate_vwap_analysis(now=NOW)
        assert len(rows) == 20
        assert prices.call_count == 1, (
            f"get_current_prices called {prices.call_count} times for 20 assets; "
            f"it must be fetched once before the loop"
        )

    def test_no_live_quotes_at_all_returns_empty(self):
        """The one genuinely universe-wide condition. Distinct from the
        per-asset gaps above, which must still emit rows."""
        adapter, _ = _adapter(ASSETS, [], ready=False)
        assert adapter.calculate_vwap_analysis(now=NOW) == []

    def test_factory_threads_the_price_manager_through(self):
        """create_calculator is the only path metrics.py uses. If the parameter
        is dropped there, every asset hits the None guard and VWAP silently
        emits nothing."""
        prices = _Prices([])
        adapter = create_calculator(
            None, _NoHistory(), metrics_db_manager=None,
            asset_loader=_Loader(ASSETS), price_data_manager=prices,
        )
        assert adapter.price_data_manager is prices


class _StoredVwap:
    """metrics.db stand-in holding one stored TCS row."""

    def __init__(self, stamp, displacement):
        self.rows = [{'asset_id': 11536, 'segment_code': 'NSE_EQ',
                      'timestamp': stamp, 'displacement': displacement}]

    def get_latest_vwap(self):
        return self.rows, None


TCS_ONLY = [{'asset_id': 11536, 'segment_code': 'NSE_EQ', 'asset_name': 'TCS'}]
TCS_QUOTE = [{'asset_id': 11536, 'segment_code': 'NSE_EQ',
              'last_price': 2334.0, 'average_price': 2325.35}]


class TestPreviousDisplacementIsTodaysOnly:
    """acceptance compares this cycle's distance from VWAP with the previous
    cycle's. The previous value used to be seeded from the newest stored row and
    kept across midnight, so a session's first acceptance compared two
    different days' VWAPs."""

    def _seeded(self, stamp):
        return VwapAdapter(None, _NoHistory(), metrics_db_manager=_StoredVwap(stamp, 0.001),
                           asset_loader=_Loader(TCS_ONLY), price_data_manager=_Prices(TCS_QUOTE))

    def test_a_stored_value_from_yesterday_is_not_used(self):
        (row,) = self._seeded('2026-08-27T15:32:40+05:30').calculate_vwap_analysis(now=NOW)
        assert row.acceptance is None

    def test_a_stored_value_from_earlier_today_is(self):
        (row,) = self._seeded('2026-08-28T11:06:26+05:30').calculate_vwap_analysis(now=NOW)
        assert row.acceptance == pytest.approx(abs(row.displacement) - 0.001)

    def test_a_container_running_overnight_starts_the_day_fresh(self):
        adapter, _ = _adapter(TCS_ONLY, TCS_QUOTE)
        adapter.calculate_vwap_analysis(now=NOW)
        next_morning = datetime(2026, 8, 31, 9, 35, tzinfo=IST)
        (row,) = adapter.calculate_vwap_analysis(now=next_morning)
        assert row.acceptance is None
        (row,) = adapter.calculate_vwap_analysis(now=next_morning.replace(minute=36))
        assert row.acceptance is not None
