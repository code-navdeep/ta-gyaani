"""
Test harness — shared fixtures for the smoke-test suite.

Boot discipline:
  - Dashboard.py's auth check raises RuntimeError at import time if
    DASHBOARD_USER / DASHBOARD_PASSWORD are missing. Setting them here (before
    any dashboard import) lets pytest collect + import the module in a bare
    test environment without a real .env file.
  - Contract check in dashboard.py runs at import too — it exercises schemas
    reflection, so any Row-side rename that skips CONSUMED_FIELDS surfaces
    as an ImportError during test collection. That's the entire point.
"""
import os
os.environ.setdefault('DASHBOARD_USER', 'test-user')
os.environ.setdefault('DASHBOARD_PASSWORD', 'test-pass')

# No test may write the app's real logs. main.py sets logging up when it is
# imported (three test modules import it), and so does scripts/backfill.py;
# each opened a file under data/logs — the files the container writes, shared
# with it through the bind mount. Replaced for the whole session, before any
# test module is imported, by a handler that discards every record. Every log
# setup looks the class up by name when it runs, so each gets this one.
import logging
import logging.handlers
logging.handlers.TimedRotatingFileHandler = lambda *args, **kwargs: logging.NullHandler()

import dataclasses
import pytest

from calculators.schemas import (
    MomentumRow, BreakoutRow, VwapRow, RelativeStrengthRow,
    VolumeBlockRow, VolumeDynamicsRow, PriceVolumeDivergenceRow,
    PriceLevelsRow, RollingMomentumRow,
)
from calculators.statistics import TIERS


# Two test assets — one NSE_EQ stock, one IDX_I index, shaped as production
# emits them. The index gets no Volume Block / Volume Dynamics / RS / Price
# Levels row (no volume, no benchmark, no CSV levels), and its VWAP and PV
# Divergence rows carry status 'no_volume_variation' with no values.
_STOCK = {'asset_id': 2885, 'segment_code': 'NSE_EQ', 'symbol': 'RELIANCE'}
_INDEX = {'asset_id': 13, 'segment_code': 'IDX_I', 'symbol': 'NIFTY'}
_TS = '2026-07-15T10:30:00+05:30'      # IST-aware, as every persisted stamp is


def _tiers(prefix, z_22, avg, std):
    """All four tiers of one z-scored signal, each DISTINCT: z rises by 0.1 per
    tier from `z_22`, so a surface reading the wrong tier shows a wrong number.
    data_points is the tier's own N — the exact-fill rule."""
    fields = {}
    for i, t in enumerate(TIERS):
        fields[f'{prefix}_z_{t}d'] = round(z_22 + 0.1 * i, 3)
        fields[f'{prefix}_avg_{t}d'] = avg
        fields[f'{prefix}_std_{t}d'] = std
        fields[f'{prefix}_data_points_{t}d'] = t
    return fields


def _momentum_row(**overrides):
    """Fully populated MomentumRow — all four tiers of gap and early-return z."""
    base = dict(
        current_price=2500.0, open_price=2490.0, previous_close=2480.0,
        price_after_window=2495.0, timestamp=_TS,
        gap_return=0.004, gap_direction=1, early_return=0.002,
        continuation=0.002, acceleration=0.001,
        status='valid',
        **_tiers('gap', 1.1, 0.15, 0.5),
        **_tiers('early_return', 0.9, 0.1, 0.4),
    )
    base.update(overrides)
    return MomentumRow(**base)


def _breakout_row(**overrides):
    base = dict(
        current_price=2500.0, range_high=2495.0, range_low=2485.0,
        range_width=0.004, breakout_position=0.5,
        fresh_breakout=1, breakout_direction=1, breakout_now=1, accepted_breakout=1,
        failed_breakout=0, distance_from_range=0.002,
        status='valid', timestamp=_TS,
    )
    base.update(overrides)
    return BreakoutRow(**base)


def _vwap_row(**overrides):
    base = dict(
        current_price=2500.0, vwap=2495.5, displacement=0.0018,
        side=1, acceptance=0.0002, status='valid', timestamp=_TS,
        **_tiers('vwap', 1.0, 0.05, 0.3),
    )
    base.update(overrides)
    return VwapRow(**base)


def _rs_row(**overrides):
    base = dict(
        benchmark_name='Nifty', benchmark_id=13,
        current_price=2500.0, open_price=2490.0,
        status='valid', timestamp=_TS,
        # Every tier distinct, same reasoning as _vd_row: RS is measured once
        # per beta tier, so a surface that reads the wrong tier must
        # produce a visibly wrong number rather than the expected one.
        beta_22d=0.97, beta_90d=1.03, beta_180d=1.11, beta_252d=1.24,
        session_rs_22d=0.0044, session_rs_90d=0.0051,
        session_rs_180d=0.0058, session_rs_252d=0.0066,
        intraday_rs_22d=0.0027, intraday_rs_90d=0.0032,
        intraday_rs_180d=0.0037, intraday_rs_252d=0.0041,
        acceptance_22d=0.00008, acceptance_90d=0.0001,
        acceptance_180d=-0.00012, acceptance_252d=0.00014,
        session_rs_z_22d=2.4, session_rs_avg_22d=0.05, session_rs_std_22d=0.24,
        session_rs_data_points_22d=22,
        session_rs_z_90d=2.1, session_rs_avg_90d=0.04, session_rs_std_90d=0.22,
        session_rs_data_points_90d=90,
        session_rs_z_180d=1.7, session_rs_avg_180d=0.035, session_rs_std_180d=0.21,
        session_rs_data_points_180d=180,
        session_rs_z_252d=1.3, session_rs_avg_252d=0.03, session_rs_std_252d=0.20,
        session_rs_data_points_252d=252,
        intraday_rs_z_22d=2.0, intraday_rs_avg_22d=0.04, intraday_rs_std_22d=0.20,
        intraday_rs_data_points_22d=22,
        intraday_rs_z_90d=1.8, intraday_rs_avg_90d=0.03, intraday_rs_std_90d=0.18,
        intraday_rs_data_points_90d=90,
        intraday_rs_z_180d=1.5, intraday_rs_avg_180d=0.025, intraday_rs_std_180d=0.17,
        intraday_rs_data_points_180d=180,
        intraday_rs_z_252d=1.1, intraday_rs_avg_252d=0.02, intraday_rs_std_252d=0.16,
        intraday_rs_data_points_252d=252,
    )
    base.update(overrides)
    return RelativeStrengthRow(**base)


def _volume_block_row(**overrides):
    base = dict(
        time_block=5, block_volume=23000.0, session_cumulative_volume=95000.0,
        session_cumulative_blocks=5, status='valid', timestamp=_TS,
        **_tiers('block_volume', 1.4, 0.04, 0.16),
        **_tiers('session_cumulative_volume', 1.1, 0.03, 0.14),
    )
    base.update(overrides)
    return VolumeBlockRow(**base)


def _vd_row(**overrides):
    # Volume Dynamics tiers exactly as Volume Block does — it is a pure
    # derivative of Volume Block's z. Distinct values per tier so a
    # cross-tier mix-up shows up as a wrong number rather than passing.
    base = dict(time_block=5, status='valid', timestamp=_TS)
    for i, tier in enumerate(TIERS):
        base[f'volume_z_{tier}d'] = 1.5 + i
        base[f'velocity_{tier}d'] = 0.28 + i
        base[f'acceleration_{tier}d'] = 0.11 + i
        base[f'persistence_{tier}d'] = 0.9 + i
        base[f'expansion_state_{tier}d'] = 2
    base.update(overrides)
    return VolumeDynamicsRow(**base)


def _pvd_row(**overrides):
    base = dict(
        pearson_r=0.42, spearman_r=0.38, winsorized_r=0.40, fisher_value=0.448,
        sample_size=179, critical_r=0.147, reliable=1,
        # Agreement +1.0 means all three estimators agreed, so all three exist.
        estimator_agreement=1.0, estimators_available=3,
        price_volatility=0.85, status='valid', timestamp=_TS,
        **_tiers('pvd', 1.5, 0.06, 0.36),
    )
    base.update(overrides)
    return PriceVolumeDivergenceRow(**base)


def _price_levels_row(**overrides):
    base = dict(
        current_price=2500.0, status='valid', timestamp=_TS,
        levels={
            'local_high': {'level_value': 2495.0, 'distance_pct': 0.2, 'distance_log': 0.002,
                            'side': 1, 'approached': 0, 'crossed': 1, 'status': 'valid'},
            'global_high': {'level_value': 2600.0, 'distance_pct': -3.85, 'distance_log': -0.039,
                             'side': -1, 'approached': 0, 'crossed': 0, 'status': 'valid'},
        },
        day_events=['local_high'],
    )
    base.update(overrides)
    return PriceLevelsRow(**base)


def _rolling_momentum_row(**overrides):
    base = dict(
        current_price=2500.0, previous_close=2480.0,
        session_return=0.81, status='valid', timestamp=_TS,
        **_tiers('session_return', 1.3, 0.05, 0.5),
    )
    base.update(overrides)
    return RollingMomentumRow(**base)


def _asd(rows):
    """dataclasses.asdict shortcut for a list of Row instances."""
    return [dataclasses.asdict(r) for r in rows]


@pytest.fixture
def fixture_metrics():
    """A fake LATEST_METRICS dict with populated rows for both a stock and an index.

    Matches the exact shape that metrics.py `_serialize_rows()` produces at the
    LATEST_METRICS storage boundary — plain dicts, one list per calculator key.
    """
    momentum = _asd([
        _momentum_row(**_STOCK),
        _momentum_row(**_INDEX, current_price=25000.0, open_price=24900.0, previous_close=24800.0),
    ])
    breakout = _asd([
        _breakout_row(**_STOCK),
        _breakout_row(**_INDEX, current_price=25000.0, range_high=24950.0, range_low=24800.0),
    ])
    # An index has no volume, so no volume-weighted average price: a row saying
    # so, with no values.
    vwap = _asd([
        _vwap_row(**_STOCK),
        VwapRow(**_INDEX, status='no_volume_variation', timestamp=_TS),
    ])
    # RS emits for stocks only, one row per (stock, benchmark). Index rows have no RS.
    rs = _asd([_rs_row(**_STOCK)])
    # Volume Block and Volume Dynamics emit nothing for an index.
    volume_block = _asd([_volume_block_row(**_STOCK)])
    vd = _asd([_vd_row(**_STOCK)])
    pvd = _asd([
        _pvd_row(**_STOCK),
        PriceVolumeDivergenceRow(**_INDEX, status='no_volume_variation', timestamp=_TS),
    ])
    # Price Levels emits for stocks with CSV levels only; indices have none.
    price_levels = _asd([_price_levels_row(**_STOCK)])
    rolling_momentum = _asd([
        _rolling_momentum_row(**_STOCK),
        _rolling_momentum_row(**_INDEX, current_price=25000.0, previous_close=24800.0),
    ])

    return {
        'timestamp': _TS,
        'cycle_number': 100,
        'momentum': momentum,
        'breakout': breakout,
        'vwap': vwap,
        'relative_strength': rs,
        'volume_block': volume_block,
        'volume_dynamics': vd,
        'price_volume_divergence': pvd,
        'price_levels': price_levels,
        'rolling_momentum': rolling_momentum,
    }
