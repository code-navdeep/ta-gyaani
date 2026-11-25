"""
Frozen prices must not look live.

Every calculator reads prices from PriceDataManager, the in-memory store the
engine's ~5-second quotes feed. When the engine stops fetching (expired token,
network down) the calculators keep succeeding on the last quotes and the
metrics cycle's timestamp keeps advancing, so the header said LIVE over frozen
prices. And the store never expired anything, so a container left running
overnight served yesterday's closes to the first cycles of the next session.
"""
from datetime import datetime, timedelta

import pytest

import dashboard
import metrics
from calculators.price_data_manager import PriceDataManager
from calculators.time_blocks import IST


def _quote(asset_id, ts):
    return {'asset_id': asset_id, 'segment_code': 'NSE_EQ', 'symbol': f'S{asset_id}',
            'last_price': 100.0, 'timestamp': ts.isoformat()}


class TestOtherDaysQuotesAreDropped:
    """No socket is opened: PriceDataManager connects only in start()."""

    def test_yesterdays_quote_is_not_served(self):
        pdm = PriceDataManager()
        pdm._update_price_data(_quote(1333, datetime.now(IST) - timedelta(days=1)))
        assert not pdm.is_ready()
        assert pdm.get_current_prices().empty

    def test_todays_quote_is_served(self):
        pdm = PriceDataManager()
        pdm._update_price_data(_quote(1333, datetime.now(IST)))
        assert pdm.is_ready()
        assert list(pdm.get_current_prices()['asset_id']) == [1333]

    def test_only_the_stale_quote_goes(self):
        """Checked per quote: a stock with no quote yet today must not keep
        yesterday's just because other stocks have updated."""
        pdm = PriceDataManager()
        pdm._update_price_data(_quote(1333, datetime.now(IST) - timedelta(days=1)))
        pdm._update_price_data(_quote(1594, datetime.now(IST)))
        assert list(pdm.get_current_prices()['asset_id']) == [1594]


class _Feed:
    """Stands in for the registered PriceDataManager."""

    def __init__(self, last):
        self.last = last

    def get_status(self):
        return {'last_update_time': self.last.isoformat() if self.last else None}


@pytest.fixture
def live_cycle(monkeypatch):
    """A metrics cycle that has just run — the header's old notion of LIVE."""
    monkeypatch.setattr(metrics, 'get_latest_metrics',
                        lambda: {'timestamp': datetime.now(IST).isoformat()})
    monkeypatch.setattr(metrics, 'get_hydration_timestamp', lambda: None)


def _header(monkeypatch, last_quote):
    monkeypatch.setattr(metrics, '_PRICE_FEED', _Feed(last_quote))
    return dashboard.update_staleness_indicator(0).children


class TestHeaderShowsFrozenPrices:

    def test_an_old_quote_reads_prices_stale(self, monkeypatch, live_cycle):
        text = _header(monkeypatch, datetime.now(IST) - timedelta(seconds=120))
        assert text.startswith('⚠ PRICES STALE')

    def test_a_fresh_quote_reads_live(self, monkeypatch, live_cycle):
        assert _header(monkeypatch, datetime.now(IST) - timedelta(seconds=3)) == '● LIVE'

    def test_no_quote_at_all_reads_no_prices(self, monkeypatch, live_cycle):
        assert _header(monkeypatch, None) == '⚠ NO PRICES'
