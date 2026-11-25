"""
Row-class <-> metrics.db column parity, verified by real inserts.

The failure this guards is silent by construction. A `Row` dataclass can
declare a field that `metrics.db` has no column for: `dataclasses.asdict()`
happily emits the key, `store_*_metrics` does `metric.get(col)` over a
hand-maintained `cols` list that simply omits it, and no exception is raised
anywhere. The value is dropped at the storage boundary and nothing says so.

That is exactly how `MomentumRow.high_price` / `.low_price` came to be
declared, populated by the adapter and consumed by the dashboard's Range
column while `momentum_metrics` had no columns for them. Live sessions looked
fine because the dashboard reads the in-memory cache; the loss only surfaced
after a restart, when start-up hydration rebuilt that cache from the table.

Placeholder counts are checked by INSERTING and READING BACK, never by
counting '?' characters in the SQL — a miscount there produces a plausible
row with values shifted into the wrong columns.
"""
import dataclasses
import sqlite3

import pytest

from calculators import schemas as SC
from calculators.metrics_database_manager import MetricsDatabaseManager


# (Row class, table name, store method). One entry per persisted emission.
TABLE_MAP = [
    (SC.MomentumRow,               'momentum_metrics',                'store_momentum_metrics'),
    (SC.BreakoutRow,               'breakout_metrics',                'store_breakout_metrics'),
    (SC.VwapRow,                   'vwap_metrics',                    'store_vwap_metrics'),
    (SC.RelativeStrengthRow,       'relative_strength_metrics',       'store_relative_strength_metrics'),
    (SC.VolumeBlockRow,            'volume_block_metrics',            'store_volume_block_metrics'),
    (SC.VolumeDynamicsRow,         'volume_dynamics_metrics',         'store_volume_dynamics_metrics'),
    (SC.PriceVolumeDivergenceRow,  'price_volume_divergence_metrics', 'store_price_volume_divergence_metrics'),
    (SC.PriceLevelsRow,            'price_levels_metrics',            'store_price_levels_metrics'),
    (SC.RollingMomentumRow,        'rolling_momentum_metrics',        'store_rolling_momentum_metrics'),
    (SC.IndexSummary,              'index_metrics',                   'store_index_metrics'),
]

# Columns the DB owns rather than the Row: surrogate key and write timestamp.
_DB_OWNED = {'id', 'created_at'}

# Fields whose value is transformed on the way in (JSON-encoded), so a
# read-back comparison is against the encoded form, not the original object.
_JSON_FIELDS = {'levels', 'day_events', 'top_contributors', 'bottom_detractors',
                'constituent_z'}

# JSON fields that are MAPPINGS rather than lists. Shape matters: json.dumps
# succeeds on either, so a wrong-shaped sentinel would round-trip cleanly and
# hide a real type error at the storage boundary.
_JSON_MAPPING_FIELDS = {'levels', 'constituent_z'}


@pytest.fixture
def db(tmp_path):
    """A fresh metrics.db built purely from the CREATE TABLE statements."""
    return MetricsDatabaseManager(db_path=str(tmp_path / 'metrics.db'))


def _columns(db, table):
    with sqlite3.connect(db.db_path) as conn:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _populate(cls, **overrides):
    """A Row with every field set to a distinct, type-plausible sentinel.

    Distinct values matter: uniform values would let a column-order bug
    round-trip cleanly and pass.
    """
    row = cls()
    for i, f in enumerate(dataclasses.fields(cls)):
        if f.name in _JSON_FIELDS:
            value = {} if f.name in _JSON_MAPPING_FIELDS else []
        elif f.name in ('asset_id', 'benchmark_id'):
            value = 1333
        elif f.name in ('segment_code',):
            value = 'NSE_EQ'
        elif f.name in ('symbol', 'status', 'benchmark_name', 'index_name'):
            value = f'{f.name}_v'
        elif f.name == 'timestamp':
            value = '2026-08-21T11:00:00'
        else:
            value = float(i) + 0.5        # unique per field
        setattr(row, f.name, value)
    for k, v in overrides.items():
        setattr(row, k, v)
    return row


class TestColumnParity:
    """Every declared Row field must have somewhere to land."""

    @pytest.mark.parametrize('cls,table,_store', TABLE_MAP,
                             ids=[t for _c, t, _s in TABLE_MAP])
    def test_every_row_field_has_a_column(self, db, cls, table, _store):
        declared = {f.name for f in dataclasses.fields(cls)}
        missing = sorted(declared - _columns(db, table))
        assert not missing, (
            f"{cls.__name__} declares {missing} but {table} has no such "
            f"column(s). These values are silently dropped at the storage "
            f"boundary: asdict() emits them, store_* omits them from `cols`, "
            f"and nothing raises. Add the column to the CREATE TABLE and the "
            f"name to the store method's `cols` list."
        )

    @pytest.mark.parametrize('cls,table,_store', TABLE_MAP,
                             ids=[t for _c, t, _s in TABLE_MAP])
    def test_no_orphan_columns(self, db, cls, table, _store):
        """Every column is either Row-backed or explicitly DB-owned.

        An orphan column is a field that was renamed or removed from the Row
        without the table following — it stays NULL forever and misleads
        anyone reading the schema.
        """
        declared = {f.name for f in dataclasses.fields(cls)}
        orphans = sorted(_columns(db, table) - declared - _DB_OWNED)
        assert not orphans, (
            f"{table} has column(s) {orphans} that no {cls.__name__} field "
            f"populates. They will stay NULL. Remove them, or add the "
            f"matching Row field."
        )


class TestRoundTrip:
    """Insert a fully-populated Row, read it back, compare field by field.

    This is what verifies the store method's placeholder count and column
    ordering. Counting '?' characters in the SQL does not: an off-by-one
    there yields a successful INSERT with every value shifted one column
    left, which looks entirely plausible in the table.
    """

    @pytest.mark.parametrize('cls,table,store_name', TABLE_MAP,
                             ids=[t for _c, t, _s in TABLE_MAP])
    def test_every_field_survives_the_write(self, db, cls, table, store_name):
        row = _populate(cls)
        payload = dataclasses.asdict(row)

        store = getattr(db, store_name)
        # store_index_metrics takes {index_name: dict}; the rest take [dict].
        stored = (store({payload['index_name']: payload})
                  if cls is SC.IndexSummary else store([payload]))
        assert stored == 1, f"{store_name} reported {stored} rows written, expected 1"

        with sqlite3.connect(db.db_path) as conn:
            conn.row_factory = sqlite3.Row
            fetched = conn.execute(f"SELECT * FROM {table}").fetchall()
        assert len(fetched) == 1, f"{table} holds {len(fetched)} rows, expected 1"
        got = dict(fetched[0])

        mismatched = {
            f.name: (payload[f.name], got.get(f.name))
            for f in dataclasses.fields(cls)
            if f.name not in _JSON_FIELDS and got.get(f.name) != payload[f.name]
        }
        assert not mismatched, (
            f"{table}: {len(mismatched)} field(s) did not round-trip — "
            f"{mismatched}. A uniform shift across many fields means the "
            f"store method's column list and params tuple have drifted apart."
        )

    @pytest.mark.parametrize('cls,table,store_name', [
        e for e in TABLE_MAP if any(f.name in _JSON_FIELDS
                                    for f in dataclasses.fields(e[0]))
    ], ids=lambda e: e if isinstance(e, str) else None)
    def test_json_fields_are_encoded_not_dropped(self, db, cls, table, store_name):
        row = _populate(cls)
        payload = dataclasses.asdict(row)
        store = getattr(db, store_name)
        (store({payload['index_name']: payload})
         if cls is SC.IndexSummary else store([payload]))

        with sqlite3.connect(db.db_path) as conn:
            conn.row_factory = sqlite3.Row
            got = dict(conn.execute(f"SELECT * FROM {table}").fetchone())

        for name in (_JSON_FIELDS & {f.name for f in dataclasses.fields(cls)}):
            assert got[name] is not None, (
                f"{table}.{name} stored as NULL; it should hold a JSON string"
            )


class TestHistoricalDbRecreation:
    """`historical.db` builds cleanly from its CREATE TABLE statements.

    Standing check rather than a one-off: the schema has already changed once
    (the shared store's `block_volume_z_90` was dropped) and was verified by
    hand. `metrics.db` gets this for free — every test above builds a fresh one
    — but nothing covered `historical.db` until now.

    The composite primary keys are pinned deliberately. asset_id 25 is BankNifty
    in IDX_I and ADANIENT in NSE_EQ, so a key missing `segment_code` would let
    one entity silently overwrite the other's rows.
    """

    EXPECTED = {
        'daily_ohlc': {
            'cols': {'date', 'asset_id', 'segment_code', 'symbol', 'open_price',
                     'high_price', 'low_price', 'close_price', 'volume', 'vwap',
                     'created_at'},
            'pk': ['date', 'asset_id', 'segment_code'],
        },
        'time_block_volumes': {
            'cols': {'date', 'asset_id', 'segment_code', 'symbol', 'time_block',
                     'cumulative_volume', 'created_at'},
            'pk': ['date', 'asset_id', 'segment_code', 'time_block'],
        },
        'intraday_time_block_metrics': {
            # RAW OBSERVATIONS ONLY — see the invariant on the CREATE TABLE.
            # block_volume_z_90 was removed: it was the sole DERIVED value here,
            # frozen against whatever history existed when it was written.
            'cols': {'date', 'asset_id', 'segment_code', 'symbol', 'time_block',
                     'session_price_move', 'vwap_displacement', 'pvd_raw',
                     'created_at'},
            'pk': ['date', 'asset_id', 'segment_code', 'time_block'],
        },
        'beta_cache': {
            'cols': {'asset_id', 'segment_code', 'benchmark_name', 'beta_22d',
                     'beta_90d', 'beta_180d', 'beta_252d', 'computed_at'},
            'pk': ['asset_id', 'segment_code', 'benchmark_name'],
        },
    }

    @pytest.fixture
    def hist(self, tmp_path):
        from calculators.historical_database_manager import HistoricalDatabaseManager
        return HistoricalDatabaseManager(str(tmp_path / 'historical.db'))

    def test_all_tables_created(self, hist):
        with sqlite3.connect(hist.db_path) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(self.EXPECTED) <= tables, f"missing: {set(self.EXPECTED) - tables}"

    @pytest.mark.parametrize('table', sorted(EXPECTED))
    def test_columns_match(self, hist, table):
        with sqlite3.connect(hist.db_path) as conn:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        expected = self.EXPECTED[table]['cols']
        assert cols == expected, (
            f"{table} columns drifted. missing={sorted(expected - cols)} "
            f"unexpected={sorted(cols - expected)}"
        )

    @pytest.mark.parametrize('table', sorted(EXPECTED))
    def test_primary_key_is_composite_on_segment(self, hist, table):
        with sqlite3.connect(hist.db_path) as conn:
            info = list(conn.execute(f"PRAGMA table_info({table})"))
        pk = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
        assert pk == self.EXPECTED[table]['pk'], f"{table} PK is {pk}"
        assert 'segment_code' in pk, (
            f"{table} PK omits segment_code — asset_id 25 is BankNifty in IDX_I "
            f"and ADANIENT in NSE_EQ, so one would overwrite the other"
        )

    def test_shared_store_holds_no_derived_values(self, hist):
        """The invariant: raw observations only. A stored z goes stale the
        moment more history is backfilled, and can only represent one tier."""
        with sqlite3.connect(hist.db_path) as conn:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(intraday_time_block_metrics)")}
        derived = {c for c in cols if '_z_' in c or c.endswith('_z') or 'zscore' in c}
        assert not derived, (
            f"intraday_time_block_metrics has derived column(s) {derived}. This "
            f"table stores raw observations only — Volume Dynamics receives ZQ "
            f"in memory from Volume Block instead."
        )


class TestHistoryReaders:
    """The charts drawer reads each calculator's history through nine
    hand-written SELECT lists. A column renamed in a table but not in its
    SELECT makes that query fail; the error is logged and caught, and the
    charts go blank — with every other test green."""

    _HISTORY_KEYS = {
        'momentum_metrics': 'momentum', 'breakout_metrics': 'breakout',
        'vwap_metrics': 'vwap', 'relative_strength_metrics': 'relative_strength',
        'volume_block_metrics': 'volume_block', 'volume_dynamics_metrics': 'volume_dynamics',
        'price_volume_divergence_metrics': 'price_volume_divergence',
        'price_levels_metrics': 'price_levels', 'rolling_momentum_metrics': 'rolling_momentum',
    }

    def test_every_table_comes_back_in_the_history(self, db):
        for cls, table, store_name in TABLE_MAP:
            if cls is SC.IndexSummary:
                continue
            getattr(db, store_name)([dataclasses.asdict(_populate(cls))])
        result, is_fallback, _ = db.get_asset_metrics_history(1333, 'NSE_EQ', hours=6)
        assert set(result) == set(self._HISTORY_KEYS.values()), (
            f"missing from the history: {set(self._HISTORY_KEYS.values()) - set(result)}")
        assert not is_fallback
        assert all(len(frame) == 1 for frame in result.values())


class TestRangeColumnRegression:
    """Pins the Range column's restart blindness specifically."""

    def test_high_low_price_persist(self, db):
        row = _populate(SC.MomentumRow, high_price=2612.4, low_price=2570.1)
        db.store_momentum_metrics([dataclasses.asdict(row)])

        hydrated, _max_ts = db.get_latest_momentum()
        assert hydrated, "hydration returned nothing"
        assert hydrated[0]['high_price'] == 2612.4
        assert hydrated[0]['low_price'] == 2570.1, (
            "Start-up cache hydration must recover high/low from metrics.db, or the "
            "Range column renders blank after every restart and stays blank "
            "outside market hours"
        )
