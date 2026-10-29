"""What is particular to Hardcharger™ (DuckDB 2.0 over Chargewire, through adbc-driver-hardcharger), against
a real server (ghcr.io/gizmodata/hardcharger:main; see docker-compose.yml): types, partitioned ingest and
transactions, MERGE, and Hardcharger as a source.

Types are checked by value, not just by name: every Arrow type is ingested into Hardcharger and into
DuckDB's own ADBC ingest (the reference), and every DuckDB type is replicated through the engine, from a
DuckDB file into Hardcharger and from Hardcharger into Hardcharger.
"""

from __future__ import annotations

import datetime as dt
import decimal
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pyarrow as pa
import pytest
from conftest import (
    HARDCHARGER_PASSWORD,
    HARDCHARGER_URI,
    HARDCHARGER_USERNAME,
    execute,
    hardcharger_connect,
    hardcharger_source,
    hardcharger_target,
    make_config,
    postgres_source,
    query,
    run,
)

from adbc_replicate_hardcharger.config import parse_config
from adbc_replicate_hardcharger.engine import EXIT_FAILED, EXIT_OK
from adbc_replicate_hardcharger.planner import PlanError
from adbc_replicate_hardcharger.sources import make_source
from adbc_replicate_hardcharger.sources.duckdb import duckdb_connect
from adbc_replicate_hardcharger.targets import make_target
from adbc_replicate_hardcharger.targets.hardcharger import DuplicateKeyError

pytestmark = pytest.mark.integration

D = decimal.Decimal


def duckdb_file(path) -> object:
    conn = duckdb_connect(path=str(path))
    execute(conn=conn, statements=["SET TimeZone = 'UTC'"])   # the servers' zone, so TIMESTAMPTZ text compares
    return conn


def typed_values(conn, table: str, column: str) -> tuple[str, list]:
    """(the column's DuckDB type, its values as DuckDB text, ordered by id)."""
    rows = query(conn=conn, sql=f"SELECT typeof({column}), CAST({column} AS VARCHAR) FROM {table} ORDER BY id")
    return rows[0][0], [r[1] for r in rows]


# ---- every DuckDB type, through the engine ------------------------------------------------------

EVERY_DDL = """CREATE TABLE {table} (id INTEGER PRIMARY KEY, b BOOLEAN, ti TINYINT, si SMALLINT, i INTEGER,
 bi BIGINT, hi HUGEINT, uti UTINYINT, usi USMALLINT, ui UINTEGER, ubi UBIGINT, uhi UHUGEINT, f FLOAT, d DOUBLE,
 dec DECIMAL(18,3), dec38 DECIMAL(38,10), s VARCHAR, bl BLOB, bt BIT, dt DATE, tm TIME, tmns TIME_NS, tmtz TIMETZ,
 ts TIMESTAMP, tss TIMESTAMP_S, tsms TIMESTAMP_MS, tsns TIMESTAMP_NS, tstz TIMESTAMPTZ, iv INTERVAL, u UUID, j JSON,
 e ENUM('sad', 'happy'), l INTEGER[], arr DOUBLE[3], st STRUCT(a INTEGER, b VARCHAR), m MAP(VARCHAR, INTEGER),
 un UNION(n INTEGER, s VARCHAR), bn BIGNUM)"""
# No '{...}' string literals: Hardcharger 0.3.0 refuses any as a "user credential store" path (reported).
EVERY_ROWS = """INSERT INTO {table} VALUES
 (1, true, -128, -32768, -2147483648, -9223372036854775808, -12345678901234567890123, 255, 65535, 4294967295,
  18446744073709551615, 12345678901234567890123, 1.5, 1.0000000001, 123.456,
  1234567890123456789012345678.0123456789, 'héllo', '\\xDE\\xAD'::BLOB, '1010', DATE '0001-01-01',
  TIME '23:59:59.999999', '12:34:56.123456789'::TIME_NS, '12:34:56+05:30'::TIMETZ,
  TIMESTAMP '2026-01-01 12:34:56.123456', TIMESTAMP_S '1500-01-01 00:00:00', TIMESTAMP_MS '2026-01-01 00:00:00.123',
  '2026-01-01 00:00:00.123456789'::TIMESTAMP_NS, TIMESTAMPTZ '2026-07-01 12:00:00+00',
  INTERVAL '1 year 2 months 3 days 4 hours 5 minutes 6.000007 seconds', '12345678-1234-5678-1234-567812345678',
  json_object('a', [1, 2]), 'happy', [1, NULL, 3], [1.0, 2.0, 3.0], {{'a': 1, 'b': 'x'}}, MAP {{'k': 1}},
  union_value(n := 3), '123456789012345678901234567890123456789012'::BIGNUM),
 (2, false, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -0.25, -1e300, -0.001, -0.0000000001, '', ''::BLOB, '0', DATE '9999-12-31',
  TIME '00:00:00', '00:00:00'::TIME_NS, '00:00:00-08'::TIMETZ, TIMESTAMP '1970-01-01 00:00:00',
  TIMESTAMP_S '2262-01-01 00:00:00', TIMESTAMP_MS '1970-01-01 00:00:00', '1970-01-01 00:00:00'::TIMESTAMP_NS,
  TIMESTAMPTZ '1970-01-01 00:00:00+00', INTERVAL '-1 day', '00000000-0000-0000-0000-000000000000', json_array(),
  'sad', [], [0.0, -0.0, 1e-300], {{'a': NULL, 'b': NULL}}, MAP {{}}, union_value(s := 'x'), '-1'::BIGNUM),
 (3, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
  NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)"""

# The column types a DuckDB-family source's rows arrive with (DuckDB's Arrow export, then the target's ingest).
# Hardcharger and a DuckDB file get exactly these.
EXPECTED_TYPES = {
    "hi": "DECIMAL(38,0)", "uhi": "DECIMAL(38,0)", "bt": "VARCHAR", "tmtz": "VARCHAR", "tss": "TIMESTAMP",
    "tsms": "TIMESTAMP", "u": "VARCHAR", "j": "VARCHAR", "e": "VARCHAR", "bn": "VARCHAR",
}
# DuckDB's Arrow export would change these values (TIMETZ loses its offset, BIT and BIGNUM arrive as their
# internal bytes), so the plan refuses them unless masked; copied as text, they arrive exactly.
AS_TEXT = {"strategy": "expression", "sql": "CAST({column} AS VARCHAR)"}
LOSSY = ("bt", "tmtz", "bn")
AS_TEXT_RULE = [{"tables": ["every"], "mask": dict.fromkeys(LOSSY, AS_TEXT)}]

COLUMNS = ["id", "b", "ti", "si", "i", "bi", "hi", "uti", "usi", "ui", "ubi", "uhi", "f", "d", "dec", "dec38", "s",
           "bl", "bt", "dt", "tm", "tmns", "tmtz", "ts", "tss", "tsms", "tsns", "tstz", "iv", "u", "j", "e", "l", "arr",
           "st", "m", "un", "bn"]


def assert_copied_exactly(source_conn, source_table: str, target_conn, target_table: str) -> None:
    """Each column's type is the source's (or EXPECTED_TYPES') and its values, as DuckDB text, the source's."""
    for column in COLUMNS:
        source = typed_values(conn=source_conn, table=source_table, column=column)
        copied = typed_values(conn=target_conn, table=target_table, column=column)
        assert copied[0] == EXPECTED_TYPES.get(column, source[0]), (column, copied, source)
        assert copied[1] == source[1], (column, copied, source)


def test_every_duckdb_type_replicates_into_hardcharger_by_value(hardcharger, tmp_path, prefix):
    path = tmp_path / "every.duckdb"
    src = duckdb_file(path=path)
    execute(conn=src, statements=["CREATE SCHEMA app", EVERY_DDL.format(table="app.every"),
                                  EVERY_ROWS.format(table="app.every")])
    src.commit()
    src.close()
    summary, out = run(config=make_config(prefix=prefix, source={"type": "duckdb", "path": str(path)},
                                          objects=[{"db_schemas": ["app"], "target_db_schema": f"{prefix}_app"}],
                                          columns=AS_TEXT_RULE))
    assert summary.exit_code == EXIT_OK and summary.stats["tables_copied"] == 1, out
    src = duckdb_file(path=path)
    try:
        assert_copied_exactly(source_conn=src, source_table="app.every", target_conn=hardcharger,
                              target_table=f"{prefix}_app.every")
    finally:
        src.close()


def test_every_duckdb_type_replicates_out_of_hardcharger_by_value(hardcharger, prefix):
    schema = f"{prefix}_src"
    execute(conn=hardcharger, statements=[f"CREATE SCHEMA {schema}", EVERY_DDL.format(table=f"{schema}.every"),
                                          EVERY_ROWS.format(table=f"{schema}.every")])
    objects = [{"db_schemas": [schema], "target_db_schema": f"{prefix}_out"}]
    with pytest.raises(PlanError) as refused:      # unmasked, the lossy columns are refused before any copy
        run(config=make_config(prefix=prefix, source=hardcharger_source(), objects=objects))
    for column in LOSSY:
        assert f"{schema}.every.{column} (" in str(refused.value), str(refused.value)
    summary, out = run(config=make_config(prefix=prefix, source=hardcharger_source(), objects=objects,
                                          columns=AS_TEXT_RULE))
    assert summary.exit_code == EXIT_OK and summary.stats["tables_copied"] == 1, out
    assert_copied_exactly(source_conn=hardcharger, source_table=f"{schema}.every", target_conn=hardcharger,
                          target_table=f"{prefix}_out.every")


def test_hugeint_beyond_38_digits_fails_with_a_hint(hardcharger, prefix):
    # DuckDB 1.5 sends such a value as an out-of-range decimal128(38, 0); Hardcharger refuses to.
    schema = f"{prefix}_big"
    execute(conn=hardcharger, statements=[
        f"CREATE SCHEMA {schema}", f"CREATE TABLE {schema}.t (id INTEGER, h HUGEINT)",
        f"INSERT INTO {schema}.t VALUES (1, 170141183460469231731687303715884105727)"])
    objects = [{"db_schemas": [schema], "target_db_schema": f"{prefix}_bigout"}]
    summary, out = run(config=make_config(prefix=prefix, source=hardcharger_source(), objects=objects))
    assert summary.exit_code == EXIT_FAILED, out
    assert "cannot send the HUGEINT value" in out and "CAST({column} AS VARCHAR)" in out, out
    # ... and the hint works: the value arrives whole, as text.
    summary, out = run(config=make_config(prefix=prefix, source=hardcharger_source(), objects=objects, columns=[{
        "tables": ["t"], "mask": {"h": AS_TEXT}}]))
    assert summary.exit_code == EXIT_OK, out
    assert query(conn=hardcharger, sql=f"SELECT h FROM {prefix}_bigout.t") == \
        [("170141183460469231731687303715884105727",)]


# ---- every Arrow type, through ADBC ingest ------------------------------------------------------

ARROW_TYPES = {
    "bool": (pa.bool_(), [True, None, False]),
    "int8": (pa.int8(), [-128, None, 127]),
    "int16": (pa.int16(), [-32768, None, 32767]),
    "int32": (pa.int32(), [-2**31, None, 2**31 - 1]),
    "int64": (pa.int64(), [-2**63, None, 2**63 - 1]),
    "uint8": (pa.uint8(), [0, None, 255]),
    "uint16": (pa.uint16(), [0, None, 65535]),
    "uint32": (pa.uint32(), [0, None, 2**32 - 1]),
    "uint64": (pa.uint64(), [0, None, 2**64 - 1]),
    "float32": (pa.float32(), [1.5, None, -2.25]),
    "float64": (pa.float64(), [1.0000000001, None, float("inf")]),
    "dec32": (pa.decimal32(7, 2), [D("12345.67"), None, D("-0.01")]),
    "dec64": (pa.decimal64(15, 3), [D("123456789012.345"), None, D("-0.001")]),
    "dec128": (pa.decimal128(38, 10), [D("1234567890123456789012345678.0123456789"), None, D("-0.0000000001")]),
    "str": (pa.string(), ["héllo", None, ""]),
    "large_str": (pa.large_string(), ["a", None, ""]),
    "str_view": (pa.string_view(), ["a string longer than twelve bytes", None, ""]),
    "bin": (pa.binary(), [b"\x00\xff", None, b""]),
    "large_bin": (pa.large_binary(), [b"\xde\xad", None, b""]),
    "bin_view": (pa.binary_view(), [b"\xbe\xef", None, b""]),
    "fixed_bin": (pa.binary(4), [b"abcd", None, b"\x00\x00\x00\x01"]),
    "date32": (pa.date32(), [dt.date(1, 1, 1), None, dt.date(9999, 12, 31)]),
    "date64": (pa.date64(), [dt.date(2026, 10, 19), None, dt.date(1970, 1, 1)]),
    "time32_s": (pa.time32("s"), [dt.time(23, 59, 59), None, dt.time(0)]),
    "time32_ms": (pa.time32("ms"), [dt.time(12, 0, 0, 123000), None, dt.time(0)]),
    "time64_us": (pa.time64("us"), [dt.time(12, 0, 0, 123456), None, dt.time(0)]),
    "time64_ns": (pa.time64("ns"), [dt.time(12, 0, 0, 123456), None, dt.time(0)]),
    "ts_s": (pa.timestamp("s"), [dt.datetime(1900, 1, 1), None, dt.datetime(2262, 1, 1)]),
    "ts_ms": (pa.timestamp("ms"), [dt.datetime(2026, 1, 1, 0, 0, 0, 123000), None, dt.datetime(1970, 1, 1)]),
    "ts_us": (pa.timestamp("us"), [dt.datetime(2026, 1, 1, 0, 0, 0, 123456), None, dt.datetime(1, 1, 1)]),
    "ts_ns": (pa.timestamp("ns"), [dt.datetime(2026, 1, 1, 0, 0, 0, 123456), None, dt.datetime(1970, 1, 1)]),
    "ts_utc": (pa.timestamp("us", tz="UTC"), [dt.datetime(2026, 1, 1, 12, tzinfo=dt.UTC), None,
                                               dt.datetime(1970, 1, 1, tzinfo=dt.UTC)]),
    "ts_ny": (pa.timestamp("us", tz="America/New_York"), [dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC), None,
                                                          dt.datetime(1970, 1, 1, tzinfo=dt.UTC)]),
    "dur_us": (pa.duration("us"), [dt.timedelta(days=1, microseconds=5), None, dt.timedelta(0)]),
    "dur_s": (pa.duration("s"), [dt.timedelta(seconds=90), None, dt.timedelta(0)]),
    "interval": (pa.month_day_nano_interval(), [(14, 3, 1_000_000_000), None, (0, 0, 0)]),
    "list": (pa.list_(pa.int32()), [[1, None, 3], None, []]),
    "large_list": (pa.large_list(pa.string()), [["a"], None, []]),
    "fixed_list": (pa.list_(pa.float64(), 3), [[1.0, 2.0, 3.0], None, [0.0, 0.0, 0.0]]),
    "list_view": (pa.list_view(pa.int64()), [[1, 2], None, []]),
    "struct": (pa.struct([("a", pa.int32()), ("b", pa.string())]), [{"a": 1, "b": "x"}, None, {"a": None, "b": None}]),
    "map": (pa.map_(pa.string(), pa.int64()), [[("k", 1), ("j", 2)], None, []]),
    "dict": (None, ["red", None, "blue"]),
    "uuid": (None, [uuid.UUID("12345678-1234-5678-1234-567812345678").bytes, None, uuid.UUID(int=0).bytes]),
    "json": (None, ['{"a": 1}', None, "[]"]),
    "null": (pa.null(), [None, None, None]),
}
# Hardcharger's ingest matches DuckDB's own on every Arrow type but one: an Arrow null column (no values at
# all) becomes VARCHAR, where DuckDB makes a column of its "NULL" type, which can never hold a value.
HARDCHARGER_ARROW_DIFFERS = {"null": "VARCHAR"}

def arrow_table(name: str) -> pa.Table:
    arrow_type, values = ARROW_TYPES[name]
    if name == "dict":
        array = pa.array(values).dictionary_encode()
    elif name == "uuid":
        array = pa.ExtensionArray.from_storage(typ=pa.uuid(), storage=pa.array(values, type=pa.binary(16)))
    elif name == "json":
        array = pa.ExtensionArray.from_storage(typ=pa.json_(), storage=pa.array(values, type=pa.string()))
    else:
        array = pa.array(values, type=arrow_type)
    return pa.table({"id": pa.array([1, 2, 3], type=pa.int32()), "v": array})


def ingested(conn, table: str, data: pa.Table) -> tuple[str, list]:
    with conn.cursor() as cur:
        cur.adbc_ingest(table_name=table, data=data, mode="replace")
    conn.commit()
    return typed_values(conn=conn, table=table, column="v")


def test_every_arrow_type_ingests_as_duckdb_does(hardcharger, prefix):
    reference = duckdb_connect(path=":memory:")
    execute(conn=reference, statements=["SET TimeZone = 'UTC'"])
    target = hardcharger_connect(autocommit=False)
    differs = {}
    try:
        for name in ARROW_TYPES:
            data = arrow_table(name=name)
            expected = ingested(conn=reference, table=f"a_{name}", data=data)
            got = ingested(conn=target, table=f"{prefix}_a_{name}", data=data)
            if got != expected:
                differs[name] = got[0]
                assert got[1] == expected[1], (name, got, expected)   # same values, other type
    finally:
        reference.close()
        target.close()
    assert differs == HARDCHARGER_ARROW_DIFFERS, differs


@pytest.mark.parametrize(("arrow_type", "values", "message"), [
    (pa.float16(), [1.5, None], "unsupported_arrow_type"),
    (pa.decimal256(40, 5), [D("12345678901234567890123456789012345.12345"), None], "decimal"),
])
def test_arrow_types_without_a_duckdb_column_are_refused(hardcharger, prefix, arrow_type, values, message):
    target = hardcharger_connect(autocommit=False)
    table = f"{prefix}_refused"
    try:
        with pytest.raises(Exception, match=f"(?i){message}"), target.cursor() as cur:
            cur.adbc_ingest(table_name=table, data=pa.table({"v": pa.array(values, type=arrow_type)}), mode="replace")
        target.rollback()
        assert not query(conn=hardcharger, sql=f"SELECT 1 FROM duckdb_tables() WHERE table_name = '{table}'")
    finally:
        target.close()


# ---- partitioned ingest and transactions --------------------------------------------------------

@pytest.fixture
def proxied(tmp_path):
    """A Hardcharger connection through a proxy that records every request, to count ingest parts."""
    log = tmp_path / "requests.log"
    log.touch()
    address = urlsplit(HARDCHARGER_URI.replace("hardcharger://", "http://"))
    process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("recording_proxy.py")),
                                address.hostname, str(address.port), str(log)], stdout=subprocess.PIPE, text=True)
    port = int(process.stdout.readline())
    target = {"type": "hardcharger", "uri": f"hardcharger://127.0.0.1:{port}?tls=false",
              "username": HARDCHARGER_USERNAME, "password": HARDCHARGER_PASSWORD}

    def requests() -> tuple[tuple[int, int, int], list[str]]:
        """(single ingests, partitioned ingests opened, distinct parts), and the raw log. Distinct paths: the
        driver re-sends a request whose answer was lost (with its Idempotency-Key), and each try is logged."""
        lines = log.read_text().splitlines()
        posts = {r for r in lines if r.startswith("POST ")}
        return (sum(1 for r in posts if r.endswith("/ingest")), sum(1 for r in posts if r.endswith("/ingests")),
                sum(1 for r in posts if "/parts/" in r)), lines

    yield target, requests
    process.kill()
    process.wait()


def batches(rows: int, batch_rows: int) -> pa.RecordBatchReader:
    table = pa.table({"id": pa.array(range(rows), type=pa.int64()),
                      "s": pa.array([f"row {i}" for i in range(rows)])})
    return pa.RecordBatchReader.from_batches(schema=table.schema, batches=table.to_batches(max_chunksize=batch_rows))


@pytest.mark.parametrize("parts", [8, 1])
def test_ingest_parts_roll_back_with_the_transaction(proxied, hardcharger, prefix, parts):
    """The engine relies on a load being part of the target transaction: readers keep the old copy until
    commit, and a failed load (or a merge mismatch) rolls back. Partitioned uploads go over other
    connections, so check that this still holds for them."""
    target_config, requests = proxied
    target = make_target(config=parse_config(data={
        "version": 1, "source": {"type": "duckdb"}, "target": {**target_config, "ingest_parts": parts},
        "objects": [{"db_schemas": ["x"]}]}).target)
    conn = target.connect()
    table = f"{prefix}_parts"
    try:
        execute(conn=conn, statements=[f"CREATE TABLE {table} AS SELECT 1 AS id, 'old' AS s"])
        conn.commit()
        assert target.ingest(conn=conn, db_schema="main", table=table, reader=batches(rows=200_000,
                                                                                      batch_rows=20_000)) == 200_000
        counts, lines = requests()
        if parts == 1:
            assert counts == (1, 0, 0), lines
        else:
            # One partitioned ingest. The driver hands 16,384-row slices to whichever part is ready
            # for its next one, so a part may end up carrying none: at most `parts` carry rows.
            single, partitioned, with_rows = counts
            assert (single, partitioned) == (0, 1) and 1 <= with_rows <= parts, lines
        assert query(conn=hardcharger, sql=f"SELECT count(*) FROM {table}") == [(1,)]     # not committed yet
        assert query(conn=conn, sql=f"SELECT count(*), count(DISTINCT id) FROM {table}") == [(200_000, 200_000)]
        conn.rollback()
        assert query(conn=conn, sql=f"SELECT * FROM {table}") == [(1, "old")]
        conn.commit()
        target.ingest(conn=conn, db_schema="main", table=table, reader=batches(rows=50_000, batch_rows=5_000))
        conn.commit()
        assert query(conn=hardcharger, sql=f"SELECT count(*), sum(id) FROM {table}") == [(50_000, 1_249_975_000)]
    finally:
        conn.close()


def test_merge_refuses_a_repeated_key(hardcharger, prefix):
    """DuckDB 1.5 MERGE applied an arbitrary row of several with one key; DuckDB 2.0 (Hardcharger) refuses
    them. The target checks the staged keys itself first, so both behave alike: DuplicateKeyError, and the
    engine copies the table whole."""
    s = f"{prefix}_merge"
    execute(conn=hardcharger, statements=[
        f"CREATE SCHEMA {s}", f"CREATE TABLE {s}.t (k INTEGER PRIMARY KEY, v VARCHAR)",
        f"INSERT INTO {s}.t VALUES (1, 'a'), (2, 'b')",
        f"CREATE TABLE {s}.changes AS SELECT * FROM (VALUES (1, 'x'), (1, 'y'), (3, 'c')) v(k, v)"])
    with pytest.raises(Exception, match="cannot affect the same target row more than once"):
        execute(conn=hardcharger, statements=[
            f"MERGE INTO {s}.t AS t USING {s}.changes AS s ON (t.k = s.k) WHEN MATCHED THEN UPDATE BY NAME "
            "WHEN NOT MATCHED THEN INSERT BY NAME"])
    target = make_target(config=parse_config(data={
        "version": 1, "source": {"type": "duckdb"}, "target": hardcharger_target(), "objects": [{"db_schemas": ["x"]}]
    }).target)
    conn = target.connect()
    try:
        with pytest.raises(DuplicateKeyError, match="occur more than once"):
            target.merge(conn=conn, db_schema=s, table="t", key=["k"], staging_db_schema=s, changes="changes",
                         keys=None)
    finally:
        conn.close()
    assert query(conn=hardcharger, sql=f"SELECT * FROM {s}.t ORDER BY k") == [(1, "a"), (2, "b")]


# ---- Hardcharger as a target -------------------------------------------------------------------

def pg_objects(prefix):
    return [{"db_schemas": ["hr", "sales"], "target_db_schema": f"{prefix}_{{db_schema}}"}]


def test_postgres_to_hardcharger_matches_source(postgres, hardcharger, prefix):
    config = make_config(prefix=prefix, source=postgres_source(consistency="serializable"), objects=pg_objects(prefix),
                         target=hardcharger_target(), columns=[{"tables": ["hr.emp"], "mask": {"ssn": "redact"}}])
    summary, out = run(config=config)
    assert summary.exit_code == EXIT_OK, out
    assert summary.stats["tables_copied"] == 4 and summary.stats["views_created"] == 3, out
    h, s = f"{prefix}_hr", f"{prefix}_sales"
    for table, source_table in ((f"{h}.emp", "hr.emp"), (f"{h}.dept", "hr.dept"), (f"{s}.orders", "sales.orders"),
                                (f"{h}.dept_counts", "hr.dept_counts")):
        assert query(conn=hardcharger, sql=f"SELECT count(*) FROM {table}")[0][0] == \
            query(conn=postgres, sql=f"SELECT count(*) FROM {source_table}")[0][0]
    types = {r[0]: r[1] for r in query(conn=hardcharger, sql=f"DESCRIBE {h}.emp")}
    assert types["salary"] == "DECIMAL(12,2)" and types["bonus"] == "DECIMAL(38,10)"
    assert types["tags"] == "VARCHAR[]" and types["updated"] == "TIMESTAMP WITH TIME ZONE" and types["photo"] == "BLOB"
    expected = query(conn=postgres, sql="SELECT id, name, salary::text, hired::text, badge::text, profile::text, "
                                        "active, tags FROM hr.emp ORDER BY id")
    actual = query(conn=hardcharger, sql=f"SELECT id, name, CAST(salary AS VARCHAR), CAST(hired AS VARCHAR), badge, "
                                         f"profile, active, tags FROM {h}.emp ORDER BY id")
    assert actual == expected
    assert query(conn=hardcharger, sql=f"SELECT DISTINCT ssn FROM {h}.emp") == [("****",)]
    for view in ("v_emp", "v_rich", "v_orders"):
        assert query(conn=hardcharger, sql=f"SELECT count(*) FROM {h}.{view}") == \
            query(conn=postgres, sql=f"SELECT count(*) FROM hr.{view}")

    summary, out = run(config=config)       # unchanged: fingerprints from the state kept in Hardcharger
    assert summary.stats["tables_copied"] == 0 and summary.stats["tables_skipped"] == 4, out
    with postgres.cursor() as cur:
        cur.execute(operation="UPDATE sales.orders SET amount = amount + 1 WHERE id = 7")
    try:
        summary, out = run(config=config)
        assert summary.stats["tables_copied"] == 1 and "sales.orders" in out, out
        assert query(conn=hardcharger, sql=f"SELECT CAST(amount AS VARCHAR) FROM {s}.orders WHERE id = 7") == \
            query(conn=postgres, sql="SELECT amount::text FROM sales.orders WHERE id = 7")
    finally:
        with postgres.cursor() as cur:
            cur.execute(operation="UPDATE sales.orders SET amount = amount - 1 WHERE id = 7")


# ---- Hardcharger as a source -------------------------------------------------------------------

def test_hardcharger_source_with_views_and_watermark_merges(hardcharger, prefix):
    s = f"{prefix}_shop"
    execute(conn=hardcharger, statements=[
        f"CREATE SCHEMA {s}",
        f"CREATE TABLE {s}.customers (id INTEGER PRIMARY KEY, name VARCHAR, updated_at TIMESTAMP)",
        f"INSERT INTO {s}.customers SELECT i, 'customer ' || i, TIMESTAMP '2026-01-01' + to_minutes(i) "
        "FROM range(1, 3001) t(i)",
        f"CREATE TABLE {s}.orders (id BIGINT PRIMARY KEY, customer_id INTEGER, amount DECIMAL(12,2), "
        "updated_at TIMESTAMP)",
        f"INSERT INTO {s}.orders SELECT i, i % 3000 + 1, i * 1.25, TIMESTAMP '2026-01-01' + to_minutes(i) "
        "FROM range(1, 200001) t(i)",
        f"CREATE VIEW {s}.big_spenders AS SELECT c.name, sum(o.amount) AS total FROM {s}.orders o "
        f"JOIN {s}.customers c ON c.id = o.customer_id GROUP BY c.name HAVING sum(o.amount) > 10000"])
    config = make_config(prefix=prefix, source=hardcharger_source(),
                         objects=[{"db_schemas": [s], "target_db_schema": f"{prefix}_copy"}], parallelism=2,
                         refresh={"mode": "incremental", "watermark_column": "updated_at",
                                  "watermark_lookback_minutes": 0})

    def assert_identical():
        for table in ("customers", "orders"):
            assert query(conn=hardcharger, sql=f"SELECT * FROM {prefix}_copy.{table} ORDER BY id") == \
                query(conn=hardcharger, sql=f"SELECT * FROM {s}.{table} ORDER BY id"), table
        assert query(conn=hardcharger, sql=f"SELECT * FROM {prefix}_copy.big_spenders ORDER BY name") == \
            query(conn=hardcharger, sql=f"SELECT * FROM {s}.big_spenders ORDER BY name")

    summary, out = run(config=config)
    assert summary.exit_code == EXIT_OK and summary.stats["tables_copied"] == 2, out
    assert summary.stats["views_created"] == 1, out
    assert_identical()
    execute(conn=hardcharger, statements=[
        f"UPDATE {s}.orders SET amount = amount + 100, updated_at = TIMESTAMP '2027-01-01' WHERE id % 1000 = 0",
        f"INSERT INTO {s}.orders SELECT i, 1, 1, TIMESTAMP '2027-01-01' FROM range(200001, 200101) t(i)",
        f"DELETE FROM {s}.orders WHERE id BETWEEN 10 AND 19"])
    summary, out = run(config=config)
    assert summary.exit_code == EXIT_OK, out
    assert summary.stats["tables_merged"] == 2 and summary.stats["tables_copied"] == 0, out
    assert "orders: 300 changed rows merged, 10 deleted" in out and "customers: 0 changed rows merged" in out, out
    assert_identical()


def test_serializable_hardcharger_source_hides_later_commits(hardcharger, prefix):
    s = f"{prefix}_ser"
    execute(conn=hardcharger, statements=[f"CREATE SCHEMA {s}", f"CREATE TABLE {s}.t AS SELECT * FROM range(10) r(i)"])
    source = make_source(config=parse_config(data={
        "version": 1, "source": hardcharger_source(consistency="serializable"), "target": hardcharger_target(),
        "objects": [{"db_schemas": [s]}]}).source)
    conn = source.connect()
    try:
        source.begin_serializable(conn=conn)
        assert source.fetch_all(conn=conn, sql=f"SELECT count(*) FROM {s}.t") == [(10,)]
        execute(conn=hardcharger, statements=[f"INSERT INTO {s}.t VALUES (10)"])
        assert source.fetch_all(conn=conn, sql=f"SELECT count(*) FROM {s}.t") == [(10,)]
        unrelated = source.worker_connection(conn=conn)   # another connection, another transaction
        try:
            assert source.fetch_all(conn=unrelated, sql=f"SELECT count(*) FROM {s}.t") == [(11,)]
        finally:
            unrelated.close()
    finally:
        conn.close()


def test_hardcharger_to_hardcharger(hardcharger, prefix):
    s = f"{prefix}_a"
    execute(conn=hardcharger, statements=[
        f"CREATE SCHEMA {s}", f"CREATE TABLE {s}.t (id INTEGER PRIMARY KEY, v VARCHAR, n DECIMAL(10,2))",
        f"INSERT INTO {s}.t SELECT i, 'v' || i, i / 4 FROM range(50000) r(i)",
        f"CREATE VIEW {s}.big AS SELECT * FROM {s}.t WHERE n > 100"])
    config = make_config(prefix=prefix, source=hardcharger_source(), target=hardcharger_target(), parallelism=2,
                         objects=[{"db_schemas": [s], "target_db_schema": f"{prefix}_b"}])
    summary, out = run(config=config)
    assert summary.exit_code == EXIT_OK and summary.stats["views_created"] == 1, out
    for name in ("t", "big"):
        assert query(conn=hardcharger, sql=f"SELECT * FROM {prefix}_b.{name} ORDER BY id") == \
            query(conn=hardcharger, sql=f"SELECT * FROM {s}.{name} ORDER BY id"), name
    summary, out = run(config=config)
    assert summary.stats["tables_skipped"] == 1 and summary.stats["tables_copied"] == 0, out
