"""Every README config example, run for real against the test servers.

Each example keeps its own objects, columns, refresh, views and target settings; only how to reach the
servers changes (URI, user, password; a private CA's file and Oracle network encryption are left out, a
DuckDB file's path points at a seeded file). The source objects each example names are seeded under those
names, and its target catalog is a fresh database attached on the Hardcharger server, so the example's
plan has to match real objects (its column rules fail the run when they match nothing).

The Snowflake example needs a Snowflake account and objects of its own; it is checked by
tests/unit/test_readme.py only, and Snowflake replication by test_snowflake_to_hardcharger.py.
"""

from __future__ import annotations

import http.server
import json
import re
import threading
import uuid
from pathlib import Path

import duckdb
import pytest
import yaml
from conftest import (
    HARDCHARGER_PASSWORD,
    HARDCHARGER_URI,
    HARDCHARGER_USERNAME,
    ORACLE_PASSWORD,
    ORACLE_SYSTEM_PASSWORD,
    ORACLE_URI,
    ORACLE_USERNAME,
    POSTGRES_PASSWORD,
    POSTGRES_URI,
    POSTGRES_USERNAME,
    attached_catalog,
    detach,
    execute,
    oracle_connect,
    query,
    run,
)

from adbc_replicate_hardcharger.config import parse_config
from adbc_replicate_hardcharger.engine import EXIT_OK

pytestmark = pytest.mark.integration

README = Path(__file__).resolve().parents[2] / "README.md"
BLOCKS = re.findall(r"```yaml\n(.*?)```", README.read_text(encoding="utf-8"), flags=re.DOTALL)


def block(marker: str) -> dict:
    """The README YAML block containing `marker` (exactly one), parsed."""
    found = [b for b in BLOCKS if marker in b]
    assert len(found) == 1, (marker, len(found))
    return yaml.safe_load(found[0])


# ---- seeded sources, named as the examples name them -------------------------------------------

ORACLE_SCHEMAS = {
    "HR": ["CREATE TABLE {s}.employees (id NUMBER(10) PRIMARY KEY, name VARCHAR2(50), ssn VARCHAR2(11), "
           "salary NUMBER(12,2), phone VARCHAR2(20), email VARCHAR2(50), db_password VARCHAR2(20), audit_blob BLOB, "
           "hired DATE, dbt_invocation_id VARCHAR2(36), dbt_run_started_at TIMESTAMP)",
           "INSERT INTO {s}.employees SELECT LEVEL, 'Employee ' || LEVEL, '123-45-' || LPAD(LEVEL, 4, '0'), "
           "LEVEL * 100.5, '555-0100', 'e' || LEVEL || '@example.com', 'secret', HEXTORAW('00'), "
           "DATE '2020-01-01' + LEVEL, 'inv', TIMESTAMP '2026-01-01 00:00:00' FROM dual CONNECT BY LEVEL <= 50",
           "CREATE TABLE {s}.badge_scans (id NUMBER(10), employee_id NUMBER(10), scanned_at TIMESTAMP, "
           "door VARCHAR2(10))",
           "INSERT INTO {s}.badge_scans VALUES (1, 1, TIMESTAMP '2026-01-01 08:00:00', 'A')",
           "CREATE TABLE {s}.employees_bak (id NUMBER(10))",
           "CREATE TABLE {s}.tmp_load (id NUMBER(10))",
           "CREATE VIEW {s}.v_staff AS SELECT id, name FROM {s}.employees",
           "CREATE VIEW {s}.v_heavy_payroll AS SELECT id, salary FROM {s}.employees WHERE salary > 1000"],
    "SALES_EU": ["CREATE TABLE {s}.orders (id NUMBER(10) PRIMARY KEY, amount NUMBER(12,2))",
                 "INSERT INTO {s}.orders VALUES (1, 9.5)"],
    "STAGING": ["CREATE TABLE {s}.gl_entries (id NUMBER(10), amount NUMBER(12,2))",
                "INSERT INTO {s}.gl_entries VALUES (1, 2.5)",
                "CREATE TABLE {s}.orders (id NUMBER(10))",
                "INSERT INTO {s}.orders VALUES (1)",
                "CREATE TABLE {s}.scratch (id NUMBER(10))",
                "CREATE VIEW {s}.v_orders AS SELECT id FROM {s}.orders"],
    "ERP": ["CREATE TABLE {s}.quote_lines (row_id NUMBER(10) PRIMARY KEY, quote_id NUMBER(10), item VARCHAR2(20), "
            "bom VARCHAR2(20), due DATE)",
            "INSERT INTO {s}.quote_lines VALUES (1, 7, 'bolt', '/2/3', DATE '2026-10-16')"],
    # a view calling the user's own PL/SQL functions (views.functions)
    "QUOTES": ["CREATE TABLE {s}.lines (id NUMBER(10) PRIMARY KEY, bom VARCHAR2(20), due DATE)",
               "INSERT INTO {s}.lines VALUES (1, '/2/3', DATE '2026-10-16')",
               "CREATE OR REPLACE FUNCTION {s}.bom_factor(p VARCHAR2) RETURN NUMBER IS BEGIN RETURN 1; END;",
               "CREATE OR REPLACE FUNCTION {s}.next_weekday(p_date DATE) RETURN DATE IS BEGIN RETURN p_date + 1; END;",
               "CREATE VIEW {s}.v_quote_factors AS SELECT id, bom_factor(bom) AS factor, "
               "next_weekday(due) + 1 AS ship_after FROM {s}.lines"],
}
POSTGRES_SCHEMAS = {
    "public": ["CREATE TABLE public.accounts (id int PRIMARY KEY, name text)",
               "INSERT INTO public.accounts VALUES (1, 'Acme'), (2, 'Globex')"],
    "billing": ["CREATE TABLE billing.invoices (id int PRIMARY KEY, account_id int, total numeric, issued date)",
                "INSERT INTO billing.invoices VALUES (1, 1, 10.5, DATE '2026-10-01'), (2, 2, 7, DATE '2026-10-02')",
                "CREATE VIEW billing.v_open AS SELECT id, total FROM billing.invoices WHERE total > 8"],
    "analytics": ["CREATE TABLE analytics.fact_orders (order_id int, amount numeric(12,2), "
                  "dbt_run_started_at timestamp)",
                  "INSERT INTO analytics.fact_orders VALUES (1, 5, TIMESTAMP '2026-10-01'), (2, 6, TIMESTAMP '2026-10-01')",
                  "CREATE TABLE analytics.order_lines (order_id int, line_number int, qty int, dbt_run_started_at timestamp)",
                  "INSERT INTO analytics.order_lines VALUES (1, 1, 3, TIMESTAMP '2026-10-01')",
                  "CREATE TABLE analytics.web_events (id int, url text, dbt_run_started_at timestamp)",
                  "INSERT INTO analytics.web_events VALUES (1, '/', TIMESTAMP '2026-10-01')"],
    "shop": ["CREATE TABLE shop.customers (id int PRIMARY KEY, name text, email text, ssn text)",
             "INSERT INTO shop.customers SELECT i, 'Customer ' || i, 'c' || i || '@example.com', "
             "'123-45-' || lpad(i::text, 4, '0') FROM generate_series(1, 1000) i",
             "CREATE TABLE shop.orders (id bigint PRIMARY KEY, customer_id int, amount numeric(12,2), ordered_at timestamptz)",
             "INSERT INTO shop.orders SELECT i, i % 1000 + 1, round((random() * 500)::numeric, 2), "
             "now() - i * interval '1 hour' FROM generate_series(1, 100000) i",
             "CREATE VIEW shop.customer_totals AS SELECT c.id, c.name, sum(o.amount) AS total FROM shop.customers c "
             "JOIN shop.orders o ON o.customer_id = c.id GROUP BY c.id, c.name"],
}


@pytest.fixture(scope="module")
def oracle_examples(oracle):
    system = oracle_connect(username="system", password=ORACLE_SYSTEM_PASSWORD)
    for schema, statements in ORACLE_SCHEMAS.items():
        execute(conn=system, statements=[
            f"BEGIN EXECUTE IMMEDIATE 'DROP USER {schema} CASCADE'; EXCEPTION WHEN OTHERS THEN NULL; END;",
            f"CREATE USER {schema} IDENTIFIED BY readme QUOTA UNLIMITED ON users",
            *[s.format(s=schema) for s in statements], "COMMIT"])
        objects = query(conn=system, sql=f"SELECT object_name, object_type FROM all_objects WHERE owner = '{schema}' "
                                         "AND object_type IN ('TABLE', 'VIEW', 'FUNCTION')")
        execute(conn=system, statements=[
            f"GRANT {'EXECUTE' if kind == 'FUNCTION' else 'SELECT'} ON {schema}.{name} TO {ORACLE_USERNAME}"
            for name, kind in objects])
    yield
    system.close()


@pytest.fixture(scope="module")
def postgres_examples(postgres):
    for schema, statements in POSTGRES_SCHEMAS.items():
        prepare = [f"DROP SCHEMA IF EXISTS {schema} CASCADE", f"CREATE SCHEMA {schema}"]
        if schema == "public":
            prepare = ["DROP TABLE IF EXISTS public.accounts"]
        execute(conn=postgres, statements=[*prepare, *statements])
    yield


@pytest.fixture
def webhook():
    """A local webhook receiver standing in for Slack and an ops endpoint."""
    bodies: list = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            bodies.append((self.path, json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", bodies
    server.shutdown()


# ---- running an example -------------------------------------------------------------------------

def local(source: dict) -> dict:
    """The example's source, reaching the test servers instead of the example's hosts."""
    source = dict(source)
    if source["type"] == "oracle":
        source.update(uri=ORACLE_URI, username=ORACLE_USERNAME, password=ORACLE_PASSWORD)
        source.pop("options", None)      # adbc.oracle.nne: the test server doesn't offer encryption
    elif source["type"] == "postgresql":
        source.update(uri=POSTGRES_URI, username=POSTGRES_USERNAME, password=POSTGRES_PASSWORD)
    elif source["type"] == "hardcharger":
        source.update(uri=HARDCHARGER_URI, username=HARDCHARGER_USERNAME, password=HARDCHARGER_PASSWORD)
        source.pop("tls_root_certs", None)
    return source


def local_target(target: dict, catalog: str) -> dict:
    target = {**target, "uri": HARDCHARGER_URI, "username": HARDCHARGER_USERNAME,
              "password": HARDCHARGER_PASSWORD, "catalog": catalog}
    target.pop("tls_root_certs", None)   # the test server is plain HTTP (TLS: tests/unit and the README run)
    return target


def run_example(data: dict, hardcharger, monkeypatch, env: dict | None = None):
    """Run the example config `data` for real, into a fresh catalog named like the example's (if any)."""
    for name, value in {"ORACLE_USERNAME": ORACLE_USERNAME, "ORACLE_PASSWORD": ORACLE_PASSWORD,
                        "PGUSER": POSTGRES_USERNAME, "PGPASSWORD": POSTGRES_PASSWORD,
                        "HARDCHARGER_USERNAME": HARDCHARGER_USERNAME, "HARDCHARGER_PASSWORD": HARDCHARGER_PASSWORD,
                        "STAGING_USERNAME": HARDCHARGER_USERNAME, "STAGING_PASSWORD": HARDCHARGER_PASSWORD,
                        "MASK_SALT": "pepper", **(env or {})}.items():
        monkeypatch.setenv(name, value)
    catalog = data.get("target", {}).get("catalog") or "readme_" + uuid.uuid4().hex[:8]
    conn = attached_catalog(hardcharger=hardcharger, name=catalog)
    try:
        data = {**data, "source": local(source=data["source"]),
                "target": local_target(target=data.get("target", {}), catalog=catalog)}
        summary, out = run(config=parse_config(data=data))
        tables = {(s, t) for s, t in query(conn=conn, sql="SELECT schema_name, table_name FROM duckdb_tables() "
                                                          "WHERE database_name = current_database()")}
        views = {(s, v) for s, v in query(conn=conn, sql="SELECT schema_name, view_name FROM duckdb_views() "
                                                         "WHERE database_name = current_database() AND NOT internal")}
        return summary, out, tables, views
    finally:
        detach(hardcharger=hardcharger, conn=conn, name=catalog)


ORACLE_BASE = {"version": 1, "source": {"type": "oracle"}, "objects": [{"db_schemas": ["HR", "ERP"]}]}


def test_choosing_what_to_replicate(oracle_examples, hardcharger, monkeypatch):
    summary, out, tables, views = run_example(data=block(marker="materialize_views: [\"V_HEAVY_*\"]"),
                                                 hardcharger=hardcharger, monkeypatch=monkeypatch)
    assert summary.exit_code == EXIT_OK, out
    assert {("hr", "employees"), ("hr", "badge_scans"), ("hr", "v_heavy_payroll"), ("sales_eu", "orders"),
            ("staging_raw", "gl_entries"), ("staging_raw", "orders")} <= tables, tables
    assert not {t for t in tables if t[1] in ("employees_bak", "tmp_load", "scratch")}, tables
    assert ("hr", "v_staff") in views and not {v for v in views if v[0] == "staging_raw"}, views


def test_columns_example_values(oracle_examples, hardcharger, monkeypatch):
    data = {**ORACLE_BASE, **block(marker="include: [ID, EMPLOYEE_ID, SCANNED_AT]")}
    catalog = "readme_cols"
    data["target"] = {"catalog": catalog}
    for name, value in {"ORACLE_USERNAME": ORACLE_USERNAME, "ORACLE_PASSWORD": ORACLE_PASSWORD,
                        "MASK_SALT": "pepper"}.items():
        monkeypatch.setenv(name, value)
    conn = attached_catalog(hardcharger=hardcharger, name=catalog)
    try:
        cfg = parse_config(data={**data, "source": local(source=data["source"]),
                                 "target": local_target(target={}, catalog=catalog)})
        summary, out = run(config=cfg)
        assert summary.exit_code == EXIT_OK, out
        columns = [r[0] for r in query(conn=conn, sql="DESCRIBE hr.employees")]
        assert "db_password" not in columns and "audit_blob" not in columns, columns
        row = query(conn=conn, sql="SELECT ssn, salary, phone, email FROM hr.employees WHERE id = 1")[0]
        assert len(row[0]) == 64 and row[1] is None and row[2] == "XXX-XXX-XXXX" and row[3] == "***@example.com", row
        assert [r[0] for r in query(conn=conn, sql="DESCRIBE hr.badge_scans")] == ["id", "employee_id", "scanned_at"]
        assert "source_row_id" in [r[0] for r in query(conn=conn, sql="DESCRIBE erp.quote_lines")]
    finally:
        detach(hardcharger=hardcharger, conn=conn, name=catalog)


def test_incremental_by_primary_key(oracle_examples, hardcharger, monkeypatch):
    data = {**ORACLE_BASE, **block(marker="mode: incremental          # changed rows only")}
    summary, out, tables, _ = run_example(data=data, hardcharger=hardcharger, monkeypatch=monkeypatch)
    assert summary.exit_code == EXIT_OK and ("hr", "employees") in tables, out


def test_incremental_by_watermark(postgres_examples, hardcharger, monkeypatch):
    data = {"version": 1, "source": {"type": "postgresql"}, "objects": [{"db_schemas": ["analytics"]}],
            **block(marker="watermark_column: DBT_RUN_STARTED_AT")}
    summary, out, tables, _ = run_example(data=data, hardcharger=hardcharger, monkeypatch=monkeypatch)
    assert summary.exit_code == EXIT_OK, out
    assert {("analytics", "fact_orders"), ("analytics", "order_lines"), ("analytics", "web_events")} <= tables


def test_views_settings(oracle_examples, hardcharger, monkeypatch, tmp_path):
    data = {**ORACLE_BASE, "objects": [{"db_schemas": ["HR", "QUOTES"]}], **block(marker="overrides_dir: ./view-overrides")}
    overrides = tmp_path / "view-overrides"
    overrides.mkdir()
    (overrides / "HR.V_STAFF.sql").write_text("SELECT id, upper(name) AS name FROM hr.employees")
    data["views"] = {**data["views"], "overrides_dir": str(overrides)}
    summary, out, _, views = run_example(data=data, hardcharger=hardcharger, monkeypatch=monkeypatch)
    assert summary.exit_code == EXIT_OK, out
    assert {("hr", "v_staff"), ("quotes", "v_quote_factors")} <= views, (views, out)


def test_notifications(oracle_examples, hardcharger, monkeypatch, webhook):
    url, bodies = webhook
    data = {**ORACLE_BASE, **block(marker="url: ${SLACK_WEBHOOK_URL}")}
    summary, out, _, _ = run_example(data=data, hardcharger=hardcharger, monkeypatch=monkeypatch,
                                        env={"SLACK_WEBHOOK_URL": f"{url}/slack", "OPS_WEBHOOK_URL": f"{url}/ops"})
    assert summary.exit_code == EXIT_OK, out
    assert [path for path, _ in bodies] == ["/ops"]           # success goes to the ops hook only
    assert bodies[0][1]["outcome"] == "success"


def test_everything_else(oracle_examples, hardcharger, monkeypatch):
    data = {**ORACLE_BASE, **block(marker="on_table_error: abort    # abort (default")}
    summary, out, tables, _ = run_example(data=data, hardcharger=hardcharger, monkeypatch=monkeypatch)
    assert summary.exit_code == EXIT_OK and ("_adbc_replicate_hardcharger", "run_log") in tables, out


def test_oracle_source(oracle_examples, hardcharger, monkeypatch):
    summary, out, tables, _views = run_example(data=block(marker="adbc.oracle.nne: required"),
                                                 hardcharger=hardcharger, monkeypatch=monkeypatch)
    assert summary.exit_code == EXIT_OK and ("erp", "quote_lines") in tables, out


def test_postgresql_source(postgres_examples, hardcharger, monkeypatch):
    summary, out, tables, views = run_example(data=block(marker="unconstrained_numeric: decimal"),
                                                 hardcharger=hardcharger, monkeypatch=monkeypatch)
    assert summary.exit_code == EXIT_OK, out
    assert {("public", "accounts"), ("billing", "invoices")} <= tables and ("billing", "v_open") in views, out


def test_duckdb_source_and_lossy_columns(hardcharger, monkeypatch, tmp_path):
    path = tmp_path / "analytics.duckdb"
    con = duckdb.connect(database=str(path))
    con.execute(query="CREATE TABLE main.events (id INTEGER, local_time TIMETZ, flags BIT, name VARCHAR); "
                      "INSERT INTO main.events VALUES (1, '12:00:00+05:30', '101', 'a')")
    con.close()
    data = block(marker="path: ./analytics.duckdb")
    data["source"] = {**data["source"], "path": str(path)}
    from adbc_replicate_hardcharger.planner import PlanError
    with pytest.raises(PlanError, match="TIME WITH TIME ZONE"):            # as the README says
        run_example(data=data, hardcharger=hardcharger, monkeypatch=monkeypatch)
    data.update(block(marker="local_time: {strategy: expression"))
    summary, out, tables, _ = run_example(data=data, hardcharger=hardcharger, monkeypatch=monkeypatch)
    assert summary.exit_code == EXIT_OK and ("main", "events") in tables, out


def test_hardcharger_source(hardcharger, monkeypatch):
    execute(conn=hardcharger, statements=[
        "CREATE SCHEMA IF NOT EXISTS public", "CREATE OR REPLACE TABLE public.accounts AS SELECT 1 AS id, 'Acme' AS name",
        "CREATE SCHEMA IF NOT EXISTS billing",
        "CREATE OR REPLACE TABLE billing.invoices AS SELECT 1 AS id, 1 AS account_id, 10.5 AS total"])
    summary, out, tables, _ = run_example(data=block(marker="name: production-to-staging"),
                                             hardcharger=hardcharger, monkeypatch=monkeypatch)
    assert summary.exit_code == EXIT_OK, out
    assert {("public", "accounts"), ("billing", "invoices")} <= tables, tables


def test_target_settings(postgres_examples, hardcharger, monkeypatch):
    data = {"version": 1, "source": {"type": "postgresql"}, "objects": [{"db_schemas": ["billing"]}],
            **block(marker="ingest_parts: 8                                 # the default")}
    summary, out, tables, _ = run_example(data=data, hardcharger=hardcharger, monkeypatch=monkeypatch)
    assert summary.exit_code == EXIT_OK and ("billing", "invoices") in tables, out


def test_quick_start(postgres_examples, hardcharger, monkeypatch):
    summary, out, _tables, views = run_example(data=block(marker="name: shop-to-hardcharger"),
                                                 hardcharger=hardcharger, monkeypatch=monkeypatch)
    assert summary.exit_code == EXIT_OK and summary.stats["rows_copied"] == 101_000, out
    assert ("shop", "customer_totals") in views


def test_helm_values_config(oracle_examples, hardcharger, monkeypatch):
    summary, out, tables, _ = run_example(data=block(marker="# helm values.yaml")["config"],
                                             hardcharger=hardcharger, monkeypatch=monkeypatch)
    assert summary.exit_code == EXIT_OK and ("erp", "quote_lines") in tables, out
