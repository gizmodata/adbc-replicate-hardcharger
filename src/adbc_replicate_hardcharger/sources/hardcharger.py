"""Hardcharger source: a DuckDB 2.0 server, read through adbc-driver-hardcharger (Chargewire).

The SQL is DuckDB's, so discovery, view definitions and fingerprints are the DuckDB source's;
only connecting differs. Large results arrive split into partitions read at once (the driver's
default); rows are written to the target as they come, in no particular order.
"""

from __future__ import annotations

from ..config import HardchargerSource as HardchargerConfig
from .duckdb import DuckDBSource


def hardcharger_connect(config, autocommit: bool, db_kwargs: dict[str, str] | None = None):
    """A DB-API connection to Hardcharger for a source or target config."""
    from adbc_driver_hardcharger import dbapi as hardcharger

    return hardcharger.connect(uri=config.uri, username=config.username, password=config.password,
                               tls_skip_verify=config.tls_skip_verify, tls_root_certs=config.tls_root_certs,
                               db_kwargs={**(db_kwargs or {}), **config.options}, autocommit=autocommit)


class HardchargerSource(DuckDBSource):
    config: HardchargerConfig

    def connect(self):
        # serializable: autocommit off, so every read runs in the connection's one DuckDB transaction
        # (MVCC: one snapshot). Otherwise each statement reads the data as of when it starts.
        return hardcharger_connect(config=self.config, autocommit=self.config.consistency != "serializable")

    def worker_connection(self, conn):
        # adbc_clone() would turn autocommit off, leaving a worker's first snapshot open for the whole
        # run. (Parallel workers only exist without serializable, and snapshot isn't offered.)
        return self.connect()

    def error_hint(self, error: str) -> str | None:
        if "cannot send the HUGEINT value" in error or "cannot send the UHUGEINT value" in error:
            return ("Hardcharger sends HUGEINT/UHUGEINT as decimal128(38, 0) and refuses a value of 39 digits or "
                    "more; copy the column as text with a mask expression (strategy: expression, "
                    "sql: \"CAST({column} AS VARCHAR)\")")
        return None
