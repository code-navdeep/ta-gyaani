"""
The schedule's end time, block 25's cut-off and the end-of-day start agree.

config.json's schedule end_time is when the engine stops asking Dhan for
prices. Block 25 counts readings up to 15:31 (time_blocks.FINAL_READINGS_CUTOFF),
so the two are the same moment: fetching stops exactly when readings stop
counting. The end-of-day jobs (archive, cleanup, token renewal) start at
end_time + the metrics thread's 2-minute run-on + a 1-minute gap — 15:34 —
derived from end_time, not written in: when it was a fixed 15:33, moving
end_time to 15:31 would have started the jobs while metrics was still running.
"""
from datetime import time as dt_time

import pytest

import main
from calculators.time_blocks import FINAL_READINGS_CUTOFF
from engine import OHLCScheduleManager


class _Engine:
    def __init__(self, schedule_manager):
        self.schedule_manager = schedule_manager


def test_fetching_stops_when_block_25_stops_counting():
    sched = OHLCScheduleManager('config.json')
    assert sched._parse_time(sched.schedule_config['end_time']) == FINAL_READINGS_CUTOFF


def test_the_eod_jobs_start_at_1534(monkeypatch):
    monkeypatch.setattr(main, 'engine', _Engine(OHLCScheduleManager('config.json')))
    assert main._eod_start_time() == dt_time(15, 34)


def test_the_eod_start_follows_the_schedule(monkeypatch):
    sched = OHLCScheduleManager('config.json')
    sched.schedule_config = {**sched.schedule_config, 'end_time': '15:40'}
    monkeypatch.setattr(main, 'engine', _Engine(sched))
    assert main._eod_start_time() == dt_time(15, 43)
