"""
The engine's quote processing — what is kept from each quote, and how one bad
quote, one failed lookup or one dead token is contained.

- The order book (`depth`) in every quote is ignored: never parsed, never
  stored, and ohlc.db has no table for it.
- A missing or zero price is stored as NULL, never 0. The archive takes the
  day's open and low with SQL MIN, which skips NULL but would pick a stored 0.
- One asset whose fields raise drops that asset only, not every asset after it
  in the same fetch.
- A failed previous-volume lookup yields None (stored NULL), never a fabricated
  zero-volume interval that would be summed into its block.
- A failed quote request re-reads .env and retries once, but only when the
  token there has changed.

The methods under test use only what is stubbed here, so a full OHLCEngine
(which binds a ZMQ socket and builds an API client) is not constructed.
"""
import sqlite3

import pytest

import engine as engine_mod

TS = '2026-08-21T11:00:00+05:30'


class _Loader:
    def get_asset_info(self, asset_id, segment_code):
        return {'asset_name': f'S{asset_id}'}


class _StubEngine:
    _calculate_interval_volume = engine_mod.OHLCEngine._calculate_interval_volume

    def __init__(self, database):
        self.database = database
        self.asset_loader = _Loader()


@pytest.fixture
def eng(tmp_path):
    return _StubEngine(engine_mod.OHLCDatabase(db_path=str(tmp_path / 'ohlc.db')))


def _quote(**overrides):
    quote = {
        'last_price': 101.5, 'volume': 1000, 'net_change': 1.5,
        'average_price': 100.8,
        'ohlc': {'open': 100.0, 'high': 102.0, 'low': 99.5, 'close': 100.0},
        'depth': {'buy': [{'price': 101.4, 'quantity': 10, 'orders': 2}],
                  'sell': [{'price': 101.6, 'quantity': 12, 'orders': 3}]},
    }
    quote.update(overrides)
    return quote


def _process(eng, api_data):
    return engine_mod.OHLCEngine._process_api_data(eng, api_data)


class TestOrderBookIsIgnored:

    def test_no_order_book_field_reaches_a_record(self, eng):
        records = _process(eng, {'NSE_EQ': {'1333': _quote()}})
        assert len(records) == 1
        for key in records[0]:
            assert 'depth' not in key and key not in ('side', 'level', 'orders')

    def test_a_fresh_database_has_no_order_book_table(self, eng):
        with sqlite3.connect(eng.database.db_path) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        assert 'market_depth' not in tables

    def test_there_is_no_order_book_writer(self):
        assert not hasattr(engine_mod.OHLCDatabase, 'store_market_depth')


class TestMissingPricesAreNull:

    def test_a_missing_low_is_none(self, eng):
        quote = _quote(ohlc={'open': 100.0, 'high': 102.0, 'close': 100.0})
        record = _process(eng, {'NSE_EQ': {'1333': quote}})[0]
        assert record['low_price'] is None
        assert record['open_price'] == 100.0

    def test_a_zero_last_price_is_none_and_so_is_the_change(self, eng):
        record = _process(eng, {'NSE_EQ': {'1333': _quote(last_price=0)}})[0]
        assert record['last_price'] is None
        assert record['change_amount'] is None
        assert record['change_percent'] is None

    def test_the_change_is_computed_when_both_prices_exist(self, eng):
        record = _process(eng, {'NSE_EQ': {'1333': _quote()}})[0]
        assert record['change_amount'] == pytest.approx(1.5)
        assert record['change_percent'] == pytest.approx(1.5)

    def test_a_missing_low_is_stored_as_null(self, eng):
        quote = _quote(ohlc={'open': 100.0, 'high': 102.0, 'close': 100.0})
        records = _process(eng, {'NSE_EQ': {'1333': quote}})
        assert eng.database.store_ohlc_data(records)
        with sqlite3.connect(eng.database.db_path) as conn:
            low = conn.execute("SELECT low_price FROM ohlc_data").fetchone()[0]
        assert low is None


class TestOneBadQuoteIsContained:

    def test_the_rest_of_the_fetch_survives(self, eng):
        """`net_change` present but null makes round() raise for that asset."""
        api_data = {'NSE_EQ': {'1': _quote(net_change=None), '2': _quote()}}
        records = _process(eng, api_data)
        assert [r['asset_id'] for r in records] == [2]


class TestFailedLookupIsNull:

    class _RaisingConn:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError('disk I/O error')

    def test_a_raising_connection_yields_none_not_zero(self, eng):
        """Handled like a bad reading: nothing is stored, so the next reading
        is measured from the last good one and no volume leaves its block
        (tests/test_falling_running_total.py walks the three readings)."""
        result = engine_mod.OHLCEngine._calculate_interval_volume(
            eng, self._RaisingConn(), 1333, 'NSE_EQ', 5000, TS)
        assert result == (None, None)


class TestTokenReload:

    @pytest.fixture
    def fetcher(self, monkeypatch):
        f = object.__new__(engine_mod.DhanQuoteFetcher)
        f.access_token, f.client_id, f.last_api_call = 'OLD', 'client', 0
        f.dotenv_reads = 0

        def fake_load_dotenv(*args, **kwargs):
            f.dotenv_reads += 1
        monkeypatch.setattr(engine_mod, 'load_dotenv', fake_load_dotenv)
        return f

    @staticmethod
    def _responses(fetcher, *responses):
        sent = []

        def request(grouped):
            sent.append(fetcher.access_token)
            return responses[len(sent) - 1]
        fetcher._request_quotes = request
        return sent

    def test_a_changed_token_is_adopted_and_the_request_retried_once(self, fetcher, monkeypatch):
        monkeypatch.setenv('DHAN_ACCESS_TOKEN', 'NEW')
        sent = self._responses(fetcher, None, {'NSE_EQ': {}})
        assert fetcher.fetch_quotes({}) == {'NSE_EQ': {}}
        assert sent == ['OLD', 'NEW']
        assert fetcher.access_token == 'NEW'

    def test_an_unchanged_token_is_not_retried(self, fetcher, monkeypatch):
        monkeypatch.setenv('DHAN_ACCESS_TOKEN', 'OLD')
        sent = self._responses(fetcher, None, {'NSE_EQ': {}})
        assert fetcher.fetch_quotes({}) is None
        assert sent == ['OLD']

    def test_a_successful_request_does_not_reread_env(self, fetcher):
        self._responses(fetcher, {'NSE_EQ': {}})
        fetcher.fetch_quotes({})
        assert fetcher.dotenv_reads == 0
