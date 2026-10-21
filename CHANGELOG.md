# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.0.0] - 2026-10-20

### Changed
- Require the released Hardcharger ADBC 2.0.0 driver and test the final driver implementation.

<!-- Set the date above at tag time. -->

The first release. Its version follows the Hardcharger server's (2.0.0); 0.1.0 below was built but
never published.

### Changed

- The container ships the installed dependencies' license texts in
  `/opt/adbc-replicate-hardcharger/THIRD_PARTY_NOTICES.txt`, with its own LICENSE and NOTICE.
- The repository's public files: NOTICE (naming adbc-replicate as the origin), CONTRIBUTING.md
  with the Developer Certificate of Origin sign-off, SECURITY.md and CODE_OF_CONDUCT.md.

- CI builds the driver from hardcharger-adbc `13f4eb4` (Warp SQL protocol 1.0.0, on warp-sql-adbc
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
