# CLAUDE.md

Project notes for Claude and other contributors.

## What this is

`adbc-replicate-hardcharger`: config-driven ADBC → ADBC replication into Hardcharger (GizmoData's
DuckDB 2.0 server, over Chargewire), the only target. Python; the data path is Arrow C streams between
drivers (`fetch_record_batch` → `adbc_ingest`), so Python never touches rows. Derived from the public
gizmodata/adbc-replicate (0.6.0), whose lessons the defaults encode (12 MiB batch coalescing, one catalog
snapshot instead of per-object queries, `abandoned` runs). Hardcharger work stays here, not in
adbc-replicate. Keep the config schema,
`plan --format json` and the event stream stable and UI-friendly (a wizard may write the config).

## Layout

```
src/adbc_replicate_hardcharger/config.py      pydantic models = the config contract (+ JSON Schema)
src/adbc_replicate_hardcharger/planner.py     rules + source catalog -> Plan (tables, views, name_map)
src/adbc_replicate_hardcharger/engine.py      run: consistency, parallel copies, change detection, views
src/adbc_replicate_hardcharger/sources/*.py   one plugin per database (base.py is the interface)
src/adbc_replicate_hardcharger/targets/       the Hardcharger target (DuckDB SQL, partitioned ingest)
src/adbc_replicate_hardcharger/views.py       sqlglot translation (Oracle rewrites, name re-pointing)
src/adbc_replicate_hardcharger/state.py       sync state / run log tables in the target
tests/unit                        no servers: config, views, matching, README YAML check
tests/integration                 real Hardcharger + Oracle + PostgreSQL (docker-compose.yml): engine
                                  tests from a DuckDB file (each in a catalog attached for it), every
                                  source, types, and every README config run for real;
                                  Snowflake tests are marker `snowflake` (need credentials)
charts/adbc-replicate-hardcharger             Helm CronJob
```

## Conventions

- ADBC vocabulary everywhere (catalog, db_schema, table) — never Oracle-specific terms.
- Keyword arguments at call sites (also for pyarrow/ADBC calls that accept them).
- Python floor 3.11; CI matrix 3.11–3.14 + a 3.15 job (DuckDB has no 3.15 wheels yet, so the
  `all` extra skips it there). Dependency floors are the latest releases (the tool gets its own
  venv/container): raise them when bumping rather than adding compatibility code. The
  lowest-dependencies CI job installs exactly the floors.
- Tests read only `ADBC_REPLICATE_HARDCHARGER_TEST_*` variables: developer shells export real
  credentials under generic names. Docker addresses are `127.0.0.1`, never `localhost`.
- CI's `driver` job builds adbc-driver-hardcharger from gizmodata/hardcharger-adbc at a pinned commit (the
  release tag matching the pyproject floor; public, fetched anonymously; zig for a glibc-2.17 library) and
  every job installs that artifact; CI's Docker build finds it in `wheels/`, the release image uses PyPI. The test
  server is `ghcr.io/gizmodata/hardcharger:main`, pulled with GITHUB_TOKEN (the package grants this repo).
- Integration tests FAIL (never skip) when a server is unreachable.
- Every YAML block in README.md is validated by tests/unit/test_readme.py, and every config example runs
  for real in tests/integration/test_readme_examples.py (sources seeded under the example's names);
  quick-start output in the README was captured from a real run — re-capture it if output changes.
- Releases: bump `src/adbc_replicate_hardcharger/__init__.py`, `charts/adbc-replicate-hardcharger/Chart.yaml`
  (version + appVersion) and CHANGELOG together; tag `vX.Y.Z`. release.yml re-runs ci.yml on
  the tag and checks all three versions match, then waits for an approval of the `release` environment
  before publishing to PyPI, GHCR (image + chart) and creating the GitHub Release; it also refuses to
  publish while adbc-driver-hardcharger isn't on PyPI. Tag only on Philip's go.

## Gotchas found the hard way

- Oracle `AS OF SCN` fails with ORA-01466 for ~2 s after a table is created/altered/truncated.
  `TIMESTAMP_TO_SCN` is ~3 s coarse; an exact SCN needs `EXECUTE ON SYS.DBMS_FLASHBACK`
  (only SYS can grant it in 23ai). The integration fixture waits until flashback reads work.
- Oracle unconstrained `NUMBER` reads as float64 by default; we CAST to NUMBER(38,10).
  Oracle computed columns (CASE, functions) lose precision metadata — but
  `CASE WHEN 1 = 0 THEN col END` keeps it, which is how the `null` mask stays typed.
- PostgreSQL's driver returns `numeric` as text (arrow.opaque); we round in SQL then strictly
  cast to decimal128 client-side. `pg_get_viewdef` expands `SELECT *` into explicit columns.
- sqlglot silently drops Oracle `(+)` when generating; always run `eliminate_join_marks` first.
  sqlglot renamed `Select.args["from"]` to `"from_"` — use the `_FROM` constant in views.py.
- DuckDB can't resolve `"x"."t"` when a catalog and a db_schema are both named `x`; target
  DDL and translated views qualify with the catalog exactly in that case. DuckDB 2.0 (Hardcharger 0.3.0)
  still refuses such a view stored in catalog `x` even fully qualified (it works from another catalog), so
  those views are reported (`test_catalog_named_like_a_db_schema`).
- DuckDB flags every database's `main` schema as `internal` in `duckdb_schemas()`; the DuckDB source lists
  it anyway (it used to drop it, so `db_schemas: [main]` copied nothing).
- DuckDB's ADBC driver supports ingest mode `replace` only from 1.5.x (floor: 1.5.6).
- Snowflake shared (imported) databases have no time travel: `AT(TIMESTAMP => …)` fails with
  000707 on every table (found on SNOWFLAKE_SAMPLE_DATA), so the plan refuses snapshot mode
  there (`INFORMATION_SCHEMA.DATABASES.TYPE = 'IMPORTED DATABASE'`).
- sqlglot (Oracle → DuckDB) silently: drops unsupported arguments unless generated with
  `unsupported_level=RAISE`; maps `CAST(x AS DATE)` to DuckDB `DATE` (Oracle DATE has a time:
  use `TIMESTAMP_S`); maps `SYSDATE` to UTC (it's the server's wall clock — often not UTC, e.g. US
  Eastern); passes Oracle `TRUNC` units ('DD') and unknown functions through unchanged (those
  fail loudly in DuckDB). views.py rewrites or refuses each; rewrites of Oracle functions are
  compared with real Oracle in `tests/integration/test_oracle_view_functions.py` — extend it
  with every new rewrite, with NULL, decimal and date inputs.
- sqlglot also passes Oracle `||` through, and DuckDB's `||` is NULL if either side is (Oracle: '');
  Oracle's '' is NULL, so string functions' empty results must become NULL; numbers and dates
  turn into text Oracle's way (`_oracle_text`). DuckDB's multi-column UNPIVOT drops a row when any
  value is NULL (Oracle: all), so it's rewritten as UNION ALL.
- Oracle `GREATEST`/`LEAST` are NULL if any argument is; DuckDB's skip NULLs (sqlglot even marks Oracle's
  `ignore_nulls=True`), so they're wrapped in an all-arguments-present CASE. `TO_DATE(TO_CHAR(date), fmt)`
  reads the DD-MON-RR text leniently by field position (`'YYYY-MM-DD'` gives year 16): `_to_date_of_nls_text`.
- Oracle's `''` is NULL: source `''` literals become `CAST(NULL AS VARCHAR)` as values, comparison operands and
  function arguments, except where Oracle reads NULL as `''` (REPLACE, REGEXP_REPLACE, `||`) — done on the
  untouched tree, so the translator's own `''` (nullif templates) is never touched.
- Names: Oracle/Snowflake fold unquoted names to upper case and quoted ones (`"PartNo"`) keep theirs; DuckDB
  ignores case. A reference that matches a column only case-insensitively is resolved by the source in an outer
  query, so `_case_sensitive_references` refuses it (planner passes exact names in `Plan.column_names`).
- DuckDB 1.5.6 `strftime()` on a `TIMESTAMP_S` before 1677 fails ("Could not convert Timestamp(US) to
  Timestamp(NS)"); cast to `TIMESTAMP` first when formatting dates that may be that old.
- An audit that runs heavy views on a small server (1 GB `memory_limit`) can OOM-kill it: DuckDB's RSS goes
  well past `memory_limit`. Reproduce heavy views locally (copy the tables to a DuckDB file, `threads=1`,
  same `memory_limit`, `/usr/bin/time -l`) instead of on a customer's server.
- The Snowflake CI user (`ADBC_REPLICATE_CI`) has only PUBLIC: it can read the sample share and
  use SNOWFLAKE_LEARNING_WH, nothing else. Snowflake tests must stay read-only.
- DuckDB's Arrow export silently changes TIMETZ (drops the offset), BIT and BIGNUM (internal bytes), also
  nested. `Source.lossy_type()` names such types and the planner refuses them unmasked (`lossy_duckdb_type`
  matches type names only, never STRUCT field names like `"bit"`).
- Hardcharger (DuckDB 2.0, Chargewire): bulk ingest, partitioned or not, joins the session's transaction
  (rollback undoes it); `test_ingest_parts_roll_back_with_the_transaction` proves it through a recording
  proxy. DuckDB 2.0 `MERGE` refuses several source rows per key (1.5.6 picked one); `merge()` checks first
  either way. Hardcharger refuses a HUGEINT >= 10^38 instead of sending it (DuckDB 1.5 sends an
  out-of-range decimal128). An Arrow `null` column becomes VARCHAR (DuckDB: its NULL type).
- Hardcharger 0.3.0's sensitive-path guard refuses any SQL string literal containing `{` or `*`
  (`'{"a": 1}'`, `'^[0-9]{3}$'`, `'*'`) as a "user credential store" path; bound parameters pass. Tests build
  JSON with `json_object()`, and the Oracle view-function tests pin exactly which translations it refuses.
- A global search-and-replace of `ADBC_REPLICATE_` also rewrites Oracle object names in tests
  (`ADBC_REPLICATE_EXT` is a directory object); rename identifiers by what they are, not by prefix.
- `typeof(x)` in a probe: a column named like a keyword (`null`, `binary`) parses as the keyword, not the
  column. Probe types with a neutral column name (`v`).
