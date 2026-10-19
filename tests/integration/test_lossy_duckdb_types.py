"""DuckDB's Arrow export changes TIMETZ, BIT and BIGNUM values without an error (TIMETZ loses its UTC offset,
BIT and BIGNUM arrive as their internal bytes), so the plan refuses such columns before any data moves,
unless a mask says what to send instead or the column is excluded. Each refusal has a twin that copies
the same column correctly once the config says how."""

import io
import uuid

import duckdb
import pytest
from conftest import HARDCHARGER_PASSWORD, HARDCHARGER_URI, HARDCHARGER_USERNAME, attached_catalog, detach, query

from adbc_replicate_hardcharger.config import parse_config
from adbc_replicate_hardcharger.engine import EXIT_OK, EXIT_VIEW_ERRORS, Replicator, RunOptions
from adbc_replicate_hardcharger.events import Reporter
from adbc_replicate_hardcharger.planner import PlanError

pytestmark = pytest.mark.integration

AS_TEXT = {"strategy": "expression", "sql": "CAST({column} AS VARCHAR)"}

# (column type, a value, its text in the source, the reason the plan gives)
LOSSY = {
    "tz": ("TIMETZ", "'12:34:56+05:30'::TIMETZ", "12:34:56+05:30", "without its UTC offset"),
    "bits": ("BIT", "'1010'::BIT", "1010", "BIT arrives as its internal bytes"),
    "big": ("BIGNUM", "'123456789012345678901234567890123456789012'::BIGNUM",
            "123456789012345678901234567890123456789012", "BIGNUM arrives as its internal bytes"),
    "tzs": ("TIMETZ[]", "['01:00:00-08'::TIMETZ]", "['01:00:00-08']", "without its UTC offset"),
    "bitmap": ("MAP(VARCHAR, BIT)", "MAP {'k': '101'::BIT}", "{k=101}", "BIT arrives as its internal bytes"),
}


@pytest.fixture
def paths(tmp_path, hardcharger):
    src = tmp_path / "source.duckdb"
    con = duckdb.connect(database=str(src))
    columns = ", ".join(f"{name} {spec[0]}" for name, spec in LOSSY.items())
    values = ", ".join(spec[1] for spec in LOSSY.values())
    con.execute(query=f"""
        CREATE SCHEMA app;
        CREATE SCHEMA other;
        CREATE TABLE app.t (id INTEGER, {columns});
        INSERT INTO app.t VALUES (1, {values}), (2, {', '.join('NULL' for _ in LOSSY)});
        CREATE TABLE app.safe (id INTEGER, s STRUCT("bit" INTEGER, bignum INTEGER), tm TIME, b BLOB);
        INSERT INTO app.safe VALUES (1, {{'bit': 1, 'bignum': 2}}, TIME '01:02:03', '\\xAA'::BLOB);
        CREATE TABLE other.o (id INTEGER);
        INSERT INTO other.o VALUES (1);
        CREATE VIEW app.v_tz AS SELECT id, '12:34:56+05:30'::TIMETZ AS tz FROM other.o;
    """)
    con.close()
    catalog = "c" + uuid.uuid4().hex[:12]
    target = attached_catalog(hardcharger=hardcharger, name=catalog)
    yield {"source": str(src), "catalog": catalog, "target": target}
    detach(hardcharger=hardcharger, conn=target, name=catalog)


def config(paths, **extra):
    data = {"version": 1, "source": {"type": "duckdb", "path": paths["source"]},
            "target": {"uri": HARDCHARGER_URI, "username": HARDCHARGER_USERNAME, "password": HARDCHARGER_PASSWORD,
                       "catalog": paths["catalog"], "checkpoint": False},
            "objects": [{"db_schemas": ["app"], "views": []}]}
    data.update(extra)
    return parse_config(data=data)


def run(cfg):
    out = io.StringIO()
    summary = Replicator(config=cfg, reporter=Reporter(fmt="text", stream=out), options=RunOptions()).run()
    return summary, out.getvalue()


def target(paths, sql):
    return query(conn=paths["target"], sql=sql)


def test_lossy_columns_are_refused_before_any_data_moves(paths):
    with pytest.raises(PlanError) as refused:
        run(cfg=config(paths=paths))
    message = str(refused.value)
    for name, (_, _, _, reason) in LOSSY.items():
        assert f"app.t.{name} (" in message and reason in message, (name, message)
    assert "app.safe" not in message                    # field names "bit" / bignum and TIME are fine
    assert "CAST({column} AS VARCHAR)" in message and "exclude: [COLUMN]" in message
    # nothing was copied, not even the safe table (only the run log exists)
    assert target(paths=paths, sql="SELECT count(*) FROM duckdb_tables() WHERE database_name = current_database() AND schema_name = 'app'") == [(0,)]


def test_masked_as_text_the_same_columns_arrive_exactly(paths):
    summary, out = run(cfg=config(paths=paths, columns=[{"tables": ["app.t"], "mask": dict.fromkeys(LOSSY, AS_TEXT)}]))
    assert summary.exit_code == EXIT_OK and summary.stats["tables_copied"] == 2, out
    names = ", ".join(LOSSY)
    assert target(paths=paths, sql=f"SELECT {names} FROM app.t ORDER BY id") == [
        tuple(spec[2] for spec in LOSSY.values()), tuple(None for _ in LOSSY)]
    assert target(paths=paths, sql="SELECT s.bit, s.bignum, CAST(tm AS VARCHAR) FROM app.safe") == [(1, 2, "01:02:03")]


def test_excluded_the_table_copies_without_them(paths):
    summary, out = run(cfg=config(paths=paths, columns=[{"tables": ["app.t"], "exclude": list(LOSSY)}]))
    assert summary.exit_code == EXIT_OK, out
    assert [r[0] for r in target(paths=paths, sql="DESCRIBE app.t")] == ["id"]


def test_a_view_is_not_materialized_with_a_lossy_column(paths):
    # app.v_tz reads other.o, which isn't copied, so it can't be created as a view.
    views = {"on_failure": "materialize"}
    objects = [{"db_schemas": ["app"], "tables": ["safe"]}]
    summary, out = run(cfg=config(paths=paths, objects=objects, views=views))
    assert summary.exit_code == EXIT_VIEW_ERRORS and "app.v_tz" in summary.view_failures, out
    assert "app.v_tz won't be materialized" in out and "without its UTC offset" in out, out
    assert target(paths=paths, sql="SELECT count(*) FROM duckdb_tables() WHERE database_name = current_database() AND table_name = 'v_tz'") == [(0,)]
    summary, out = run(cfg=config(paths=paths, objects=objects, views=views,
                                  columns=[{"tables": ["app.v_tz"], "mask": {"tz": AS_TEXT}}]))
    assert summary.exit_code == EXIT_OK, out
    assert target(paths=paths, sql="SELECT tz FROM app.v_tz") == [("12:34:56+05:30",)]
