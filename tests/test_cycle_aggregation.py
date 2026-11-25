"""
The index aggregator is fed THIS cycle's rows, not a copy of the cache.

`get_latest_metrics()` deep-copies the whole cache (up to ~750 rows) to protect callers
that might modify it. IndexAggregator only reads, so the copy it used to take
every 30 seconds bought nothing: the rows it copied were the very rows the cache
had just been given.
"""
import dataclasses

import pytest

import metrics
from calculators.schemas import MomentumRow

_OFF = ('BREAKOUT', 'VWAP', 'RELATIVE_STRENGTH', 'VOLUME_BLOCK', 'VOLUME_DYNAMICS',
        'PRICE_VOLUME_DIVERGENCE', 'PRICE_LEVELS', 'ROLLING_MOMENTUM')
_ROW = MomentumRow(asset_id=11536, segment_code='NSE_EQ', symbol='TCS', current_price=2304.0)


class _Aggregator:
    def __init__(self):
        self.inputs = []

    def compute(self, latest_metrics, price_data_manager):
        self.inputs.append(latest_metrics)
        return {}


class _Store:
    def store_momentum_metrics(self, rows):
        return len(rows)

    def store_index_metrics(self, summaries):
        return len(summaries)


@pytest.fixture
def cycle(monkeypatch):
    for name in _OFF:
        monkeypatch.setattr(metrics, f'{name}_SYSTEM_AVAILABLE', False)
    monkeypatch.setattr(metrics, 'MOMENTUM_SYSTEM_AVAILABLE', True)
    monkeypatch.setattr(metrics, 'run_momentum_analysis', lambda *a, **k: [_ROW])
    monkeypatch.setattr(metrics, 'LATEST_METRICS', {'timestamp': None, 'cycle_number': 0})
    monkeypatch.setattr(metrics, 'LATEST_INDEX_METRICS', {})
    monkeypatch.setattr(metrics, 'HYDRATION_TIMESTAMP', None)
    monkeypatch.setitem(metrics.CALCULATOR_HEALTH, 'momentum', None)

    def _no_copy():
        raise AssertionError("the cycle deep-copied the cache")
    monkeypatch.setattr(metrics, 'get_latest_metrics', _no_copy)

    orch = object.__new__(metrics.MetricsOrchestrator)
    orch.ohlc_db_manager = object()
    orch.historical_db_manager = object()
    orch.price_data_manager = None
    orch.asset_loader = None
    orch.vwap_calc = None
    orch.metrics_db_manager = _Store()
    orch.index_aggregator = _Aggregator()
    orch._last_written_time_block = {}
    orch._maybe_write_intraday_block = lambda now: None
    return orch


def test_the_aggregator_gets_the_rows_the_cache_holds_not_a_copy(cycle):
    cycle.collect_metrics()
    [given] = cycle.index_aggregator.inputs
    assert given['momentum'] == [dataclasses.asdict(_ROW)]
    assert given['momentum'] is metrics.LATEST_METRICS['momentum']


def test_no_rows_this_cycle_means_no_aggregation(cycle, monkeypatch):
    """If the cycle's rows cannot be built, the index cards keep their previous
    values — the documented outcome of any aggregation failure — rather than
    being recomputed from nothing."""
    def _broken(rows):
        raise ValueError('cannot serialise')
    monkeypatch.setattr(metrics, '_serialize_rows', _broken)
    cycle.collect_metrics()
    assert cycle.index_aggregator.inputs == []
