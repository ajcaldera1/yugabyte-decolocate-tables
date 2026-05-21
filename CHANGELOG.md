# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-05-21

### Added

- Batched ``ysql_dump`` for DDL capture: multiple ``-t`` patterns per invocation
  (default ``--ddl-capture-mode batched``)
- Session-level Odyssey ``DEALLOCATE`` before/after all captures instead of per dump
- ``--ddl-capture-mode`` (``batched`` | ``per-object``) and ``--ddl-capture-batch-size``

## [0.2.0] - 2026-05-21

### Added

- SSL/TLS support: ``--sslmode``, ``--sslcert``, ``--sslkey``, ``--sslrootcert``, and
  ``--sslcrl`` CLI flags with ``PGSSL*`` environment variable fallbacks for psycopg
  and ``ysql_dump``
- Clear Odyssey / YSQL connection manager pooled ``ysql_dump`` prepared statements
  (``DEALLOCATE ALL`` before and after each ``ysql_dump``; disable with
  ``--no-clear-odyssey-prepares``)
- Automatic rollback on ``--execute`` failure: restore Phase 1 renamed tables
  and recreate dropped views (disable with ``--no-rollback-on-failure``)
- Phase 3 row-count and uncollocated verification before dropping the backup table
- Release build script (``scripts/build-release.sh``) and GitHub Actions release workflow

### Fixed

- psycopg3 ``can't change autocommit now: connection in transaction status INTRANS``
  after planning queries
- CREATE TABLE DDL: place ``SPLIT INTO N TABLETS`` outside ``WITH (...)`` per YugabyteDB grammar
- PRIMARY KEY ``recordid ASC`` rewrite corrupting column names (e.g. ``recordi HASHd``)
- Parallel COPY with psycopg3 (driver detection, ``memoryview`` chunks, progress bar postfix)
- Constraint/index name collisions after rename (suffix ``_n`` on new-table DDL)
- ``ysql_dump`` DDL sanitization for cloud clusters: strip psql meta-commands, ``SET yb_*``,
  ``DO $$`` restore blocks, pg_dump comment fragments, binary-upgrade SQL, and
  ``pg_stat_statements_reset`` calls
- View recreate executes only ``CREATE VIEW`` statements from captured DDL
- Only track dropped views for rollback after Phase 1 transaction commits
- Strip ``colocation_id`` from uncollocated ``CREATE TABLE`` DDL
- Quote mixed-case identifiers in ``ysql_dump -t`` patterns (e.g. ``public."FBNK_CURRENCY"``)
- Row-count verify log format (``%,d`` invalid in Python logging)

## [0.1.0] - 2026-05-20

### Added

- Initial standalone PyPI package `yugabyte-decolocate-tables`
- CLI entry point `decolocate-tables` and `python -m decolocate_tables`
- Colocated-to-uncollocated migration with view recreation, parallel COPY, resume
- DDL adjustments: `COLOCATION = false`, `SPLIT INTO` tablets, PK `ASC` to `HASH`
