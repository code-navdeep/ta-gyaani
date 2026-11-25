"""
A failed metrics.db write must not stamp the calculator healthy.

`CALCULATOR_HEALTH` drives the health bar in the header's left cell. Its stamp sits
directly after each calculator's `store_*_metrics` call, so it is only honest if
a failed store actually fails. The store methods used to catch, log and return
normally — and the log holds 46 `disk I/O error` store failures during which the
health bar kept showing those calculators fresh.
"""
import sqlite3
from datetime import datetime

import pytest

import metrics
from calculators.time_blocks import IST
from calculators.metrics_database_manager import MetricsDatabaseManager
from calculators.schemas import MomentumRow, VolumeBlockRow


class TestStoreMethodsRaise:
    def test_a_failed_write_propagates(self, tmp_path):
        db = MetricsDatabaseManager(db_path=str(tmp_path / 'metrics.db'))
        with sqlite3.connect(db.db_path) as conn:
            conn.execute("DROP TABLE momentum_metrics")
        row = {'asset_id': 1, 'segment_code': 'NSE_EQ', 'timestamp': 't'}
        with pytest.raises(sqlite3.OperationalError):
            db.store_momentum_metrics([row])


# Every calculator except Momentum and Volume Block is switched off, so one
# cycle exercises exactly the two store paths under test.
_OFF = ('BREAKOUT', 'VWAP', 'RELATIVE_STRENGTH', 'VOLUME_DYNAMICS',
        'PRICE_VOLUME_DIVERGENCE', 'PRICE_LEVELS', 'ROLLING_MOMENTUM')


class _Store:
    def __init__(self):
        self.fail = False
        self.calls = []

    def _store(self, name, rows):
        self.calls.append(name)
        if self.fail:
            raise sqlite3.OperationalError('disk I/O error')
        return len(rows)

    def store_momentum_metrics(self, rows):
        return self._store('momentum', rows)

    def store_volume_block_metrics(self, rows):
        return self._store('volume_block', rows)


class _Clock(datetime):
    """11:00 IST: block 3 (09:30-09:45) is complete, so Volume Block stores its
    row — it stores a block only once the block's readings are complete, which
    on the real clock would make this test fail if run before 09:45."""
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 7, 11, 0, tzinfo=IST)


@pytest.fixture
def cycle(monkeypatch):
    monkeypatch.setattr(metrics, 'datetime', _Clock)
    for name in _OFF:
        monkeypatch.setattr(metrics, f'{name}_SYSTEM_AVAILABLE', False)
    monkeypatch.setattr(metrics, 'MOMENTUM_SYSTEM_AVAILABLE', True)
    monkeypatch.setattr(metrics, 'VOLUME_BLOCK_SYSTEM_AVAILABLE', True)
    monkeypatch.setattr(metrics, 'run_momentum_analysis', lambda *a, **k: [
        MomentumRow(asset_id=1, segment_code='NSE_EQ')])
    monkeypatch.setattr(metrics, 'run_volume_block_analysis', lambda *a, **k: (
        [VolumeBlockRow(asset_id=1, segment_code='NSE_EQ', time_block=3)], {}))
    monkeypatch.setattr(metrics, 'update_metrics_cache', lambda *_: None)
    for key in ('momentum', 'volume_block'):
        monkeypatch.setitem(metrics.CALCULATOR_HEALTH, key, None)

    orch = object.__new__(metrics.MetricsOrchestrator)
    orch.ohlc_db_manager = object()
    orch.historical_db_manager = object()
    orch.price_data_manager = None
    orch.asset_loader = None
    orch.vwap_calc = None
    orch.index_aggregator = None
    orch._last_written_time_block = {}
    orch._maybe_write_intraday_block = lambda now: None
    orch.metrics_db_manager = _Store()
    return orch


class TestHealthFollowsTheStore:
    def test_failed_store_does_not_stamp(self, cycle):
        cycle.metrics_db_manager.fail = True
        cycle.collect_metrics()
        assert cycle.metrics_db_manager.calls == ['momentum', 'volume_block']
        assert metrics.CALCULATOR_HEALTH['momentum'] is None
        assert metrics.CALCULATOR_HEALTH['volume_block'] is None

    def test_successful_store_stamps(self, cycle):
        """The control: without it, the test above would also pass if health
        were never stamped at all."""
        cycle.collect_metrics()
        assert metrics.CALCULATOR_HEALTH['momentum'] is not None
        assert metrics.CALCULATOR_HEALTH['volume_block'] is not None


class TestVolumeBlockRetriesAFailedBlock:
    """Volume Block writes a block once, then skips it on later cycles. The
    record of what was written must follow the store, or a failed block is
    marked done, the next cycle finds nothing to write and stamps health, and
    the row is lost with the bar green."""

    def test_failed_block_is_not_recorded_as_written(self, cycle):
        cycle.metrics_db_manager.fail = True
        cycle.collect_metrics()
        assert cycle._last_written_time_block == {}

    def test_failed_block_is_written_on_the_next_cycle(self, cycle):
        cycle.metrics_db_manager.fail = True
        cycle.collect_metrics()
        cycle.metrics_db_manager.fail = False
        cycle.collect_metrics()
        assert cycle.metrics_db_manager.calls.count('volume_block') == 2
        assert cycle._last_written_time_block == {(1, 'NSE_EQ'): 3}
        assert metrics.CALCULATOR_HEALTH['volume_block'] is not None
