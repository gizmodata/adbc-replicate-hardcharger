# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.2.0] - 2026-11-04

### Added

- `refresh.merge_max_fraction` (default 0.25): an incremental table's change set is counted in the source
  before it is staged, and above this share of the table's rows the table is copied whole instead of
  merged. A customer (2026-11-03): a dbt `--full-refresh` restamped every row of three models, the watermark
  filter selected 100% of them, and the resulting `MERGE ... UPDATE` of 6.25M rows ran for hours on a
  2-vCPU server where a whole copy takes minutes.
- `target.statement_timeout_seconds` (default 3600): every target session sets `hardcharger.query_timeout`,
  so a SQL statement of the loader's cannot run longer than this on the server. The same incident's
  pods were killed at the Job's deadline while their merges kept running on the server for up to 9 hours,
  holding uncommitted staging tables that made every later run fail with "Catalog write-write conflict".
  Uploads are not statements and are unaffected; 0 turns the limit off.

## [2.1.1] - 2026-11-02

### Fixed

- A run checks that an unchanged view still binds with `DESCRIBE SELECT * FROM <view>` instead of
  `SELECT * FROM <view> LIMIT 0`. The `LIMIT 0` query was planned in full, and DuckDB 2.0's optimizer took
  up to ~30 s on views with correlated subqueries: an hourly run of 291 Oracle views spent over 40 minutes
  in these checks (TCM, 2026-11-02). For a non-administrator, Hardcharger's plan check also refused such
  plans ("CUSTOM_OP" for dependent join), so every one of those views counted as broken and was recreated
  on every run. DESCRIBE only binds, so a missing table or column still fails it.

## [2.1.0] - 2026-10-29

### Changed

- adbc-replicate-hardcharger now uses the Chargewire protocol and drivers, the new name of the protocol
  `adbc-driver-hardcharger` speaks; the driver options a config can pass are `adbc.hardcharger.*` and
  `adbc.chargewire.*`. Existing configs keep working, and so do Hardcharger servers released before the rename.
- Requires `adbc-driver-hardcharger` 2.1.0, the first driver that reads the `adbc.chargewire.*` options. CI
  builds that release (hardcharger-adbc `460fcc1`, was 2.0.0's `5c5d999`).

## [2.0.1] - 2026-10-22

### Documentation

- Fix the README banner on PyPI with absolute image URLs, and make repository file links absolute.
- Retain the total PyPI downloads badge in both the README and package description.

## [2.0.0] - 2026-10-21

### Changed
- Require the released Hardcharger ADBC 2.0.0 driver and test the final driver implementation.

The first release. Its version follows the Hardcharger server's (2.0.0); 0.1.0 below was built but
never published.

### Changed

- The container ships the installed dependencies' license texts in
  `/opt/adbc-replicate-hardcharger/THIRD_PARTY_NOTICES.txt`, with its own LICENSE and NOTICE.
- The repository's public files: NOTICE (naming adbc-replicate as the origin), CONTRIBUTING.md
  with the Developer Certificate of Origin sign-off, SECURITY.md and CODE_OF_CONDUCT.md.

- CI builds the driver from hardcharger-adbc `13f4eb4` (Chargewire protocol 1.0.0, on chargewire-adbc
  `a81c326`; was `9a087be`), tested against `hardcharger-server` 2.0.0-rc.1. The 1.0.0 driver commits
  a partitioned ingest with its `part_count`, which the server now requires: with the old driver every
  partitioned load into the `:main` server failed at commit ("'part_count' must be the number of
  parts"), the Snowflake CI job among them.
- Tests: Hardcharger 2.0.0 guards paths where the engine opens them and refuses no string literal,
  so the Oracle view-function tests expect no refusal (and compare every case with Oracle); the
  ingest-parts test accepts fewer parts than asked for, since the driver hands 16,384-row slices to
  whichever part is ready.

### Documentation

- The README banner is the new Hardcharger™ logo, the bull mascot lockup in a light and a dark
  version that follows the reader's color scheme, in place of the GizmoData banner card.

## [0.1.0] - 2026-10-19

The first release of adbc-replicate-hardcharger: config-driven ADBC-to-ADBC replication into
Hardcharger™. It is derived from [adbc-replicate](https://github.com/gizmodata/adbc-replicate) 0.6.0 and
carries its history (view translation from Oracle, PostgreSQL, Snowflake and DuckDB, column masking,
change detection, incremental and watermark loads, notifications, the Helm chart); see adbc-replicate's
CHANGELOG for what came before.

### Added

- Hardcharger as the target, and the only target type (`target.type: hardcharger`, the default), through
  adbc-driver-hardcharger, a regular dependency. `hardcharger://` URIs are HTTPS on port 31337 unless
  `?tls=false`; `tls_root_certs` trusts a private CA. Loads are sent as `ingest_parts` (default 8) parts at
  once and stay in the target transaction, so readers see the old copy until commit and a failed load
  rolls back whole. A `server_draining` refusal is retried.
- Hardcharger as a source: `consistency` none or serializable (not snapshot), split results, views and
  fingerprints as for DuckDB, and a hint for `HUGEINT` values of 39 digits or more, which Hardcharger
  refuses to send as `DECIMAL(38,0)`.
- Sources: Oracle, PostgreSQL, Snowflake, DuckDB and Hardcharger, as in adbc-replicate.
- Tests against a real Hardcharger server (`ghcr.io/gizmodata/hardcharger:main`), which fail, never skip,
  when it's missing: every Arrow type and every DuckDB type compared value by value with DuckDB's own
  ingest, partitioned ingest and its transaction, MERGE, and every README config example run for real.

### Changed (from adbc-replicate)

- The package, import package, command, image and chart are `adbc-replicate-hardcharger`
  (`adbc_replicate_hardcharger`); environment variables start `ADBC_REPLICATE_HARDCHARGER_`
  (`..._CONFIG`, tests `..._TEST_*`); the sync state lives in `_adbc_replicate_hardcharger`.
- Hardcharger is the only target: the other target types, their extra and their options (`target.max_msg_size`) are gone.

### Fixed

- DuckDB source: tables in a DuckDB file's `main` schema were never listed (DuckDB flags `main` as
  internal), so `db_schemas: [main]` copied nothing, with only a warning.
