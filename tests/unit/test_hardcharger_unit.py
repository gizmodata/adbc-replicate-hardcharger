"""Hardcharger source and target without a server: config, and how the driver is called (a stand-in
adbc_driver_hardcharger module records the connect() arguments). The live suite is
tests/integration/test_hardcharger.py."""

import sys
import types

import pytest

from adbc_replicate_hardcharger.config import ConfigError, json_schema, parse_config
from adbc_replicate_hardcharger.sources import make_source
from adbc_replicate_hardcharger.targets import make_target
from adbc_replicate_hardcharger.targets.hardcharger import _failed_before_running, is_transient


def config(source=None, target=None, **extra):
    return parse_config(data={
        "version": 1,
        "source": source or {"type": "duckdb", "path": "/tmp/x.duckdb"},
        "target": target or {"type": "hardcharger", "uri": "hardcharger://db.example.com"},
        "objects": [{"db_schemas": ["main"]}], **extra})


@pytest.fixture
def driver(monkeypatch):
    """Replace adbc_driver_hardcharger with a recorder of dbapi.connect() calls."""
    calls = []

    class Connection:
        def __init__(self, kwargs):
            self.kwargs = kwargs

    def connect(uri=None, **kwargs):
        calls.append({"uri": uri, **kwargs})
        return Connection(kwargs=kwargs)

    package = types.ModuleType("adbc_driver_hardcharger")
    dbapi = types.ModuleType("adbc_driver_hardcharger.dbapi")
    dbapi.connect = connect
    package.dbapi = dbapi
    monkeypatch.setitem(sys.modules, "adbc_driver_hardcharger", package)
    monkeypatch.setitem(sys.modules, "adbc_driver_hardcharger.dbapi", dbapi)
    return calls


def test_target_defaults():
    target = config().target
    assert target.type == "hardcharger" and target.ingest_parts == 8
    assert not target.tls_skip_verify and target.tls_root_certs is None and target.checkpoint


@pytest.mark.parametrize("parts", [0, 257])
def test_ingest_parts_range(parts):
    with pytest.raises(ConfigError, match="ingest_parts"):
        config(target={"type": "hardcharger", "uri": "hardcharger://h", "ingest_parts": parts})


def test_snapshot_consistency_refused_for_a_hardcharger_source():
    with pytest.raises(ConfigError, match="snapshot' isn't supported for a hardcharger source"):
        config(source={"type": "hardcharger", "uri": "hardcharger://h", "consistency": "snapshot"})


def test_json_schema_has_hardcharger():
    text = str(json_schema())
    assert "HardchargerSource" in text and "HardchargerTarget" in text and "ingest_parts" in text


def test_target_connects_without_autocommit_and_with_ingest_parts(driver):
    target = make_target(config=config(target={
        "type": "hardcharger", "uri": "hardcharger://db.example.com:4433", "username": "loader", "password": "pw",
        "tls_root_certs": "/etc/ca.pem", "ingest_parts": 3, "options": {"adbc.warp.statement.result_partitions": "1"}}
    ).target)
    target._open()
    assert driver == [{"uri": "hardcharger://db.example.com:4433", "username": "loader", "password": "pw",
                       "tls_skip_verify": False, "tls_root_certs": "/etc/ca.pem", "autocommit": False,
                       "db_kwargs": {"adbc.hardcharger.ingest.parts": "3",
                                     "adbc.warp.statement.result_partitions": "1"}}]


def test_target_options_override_ingest_parts(driver):
    make_target(config=config(target={"type": "hardcharger", "uri": "hardcharger://h",
                                      "options": {"adbc.hardcharger.ingest.parts": "2"}}).target)._open()
    assert driver[0]["db_kwargs"] == {"adbc.hardcharger.ingest.parts": "2"}


@pytest.mark.parametrize(("consistency", "autocommit"), [("none", True), ("serializable", False)])
def test_source_autocommit_follows_consistency(driver, consistency, autocommit):
    source = make_source(config=config(source={"type": "hardcharger", "uri": "hardcharger://h",
                                               "consistency": consistency}).source)
    conn = source.connect()
    worker = source.worker_connection(conn=conn)
    assert worker is not conn and [c["autocommit"] for c in driver] == [autocommit, autocommit]
    assert source.dialect == "duckdb" and source.cross_catalog


def test_source_hint_for_a_hugeint_too_large_for_decimal128():
    source = make_source(config=config(source={"type": "hardcharger", "uri": "hardcharger://h"}).source)
    hint = source.error_hint(error="InternalError: INTERNAL: [warpsql] ExecuteQuery: Conversion Error: Hardcharger "
                                   "cannot send the HUGEINT value 1701... as Arrow decimal128(38, 0)")
    assert "CAST({column} AS VARCHAR)" in hint
    assert source.error_hint(error="some other failure") is None


def test_server_draining_is_transient_and_never_ran():
    error = Exception("OperationalError: [warpsql] ExecuteQuery: the server is shutting down "
                      "(server_draining, HTTP 503, request r_1)")
    assert is_transient(error=error) and _failed_before_running(error=error)
    assert not is_transient(error=Exception("Catalog Error: ... (sql_error, HTTP 400, request r_2)"))
