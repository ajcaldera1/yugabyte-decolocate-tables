# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- SSL/TLS support: `--sslmode`, `--sslcert`, `--sslkey`, `--sslrootcert`, and
  `--sslcrl` CLI flags with `PGSSL*` environment variable fallbacks for psycopg
  and `ysql_dump`
- Clear Odyssey / YSQL connection manager pooled `ysql_dump` prepared statements
  (`DEALLOCATE ALL` before and after each `ysql_dump`; disable with
  `--no-clear-odyssey-prepares`)
- Fix psycopg3 ``can't change autocommit now: connection in transaction status INTRANS``
  after planning queries by rolling back before transactional DDL
- Fix CREATE TABLE DDL: place ``SPLIT INTO N TABLETS`` outside ``WITH (...)`` per
  YugabyteDB grammar (fixes ``syntax error at or near "INTO"``)
- Automatic rollback on ``--execute`` failure: restore Phase 1 renamed tables
  and recreate dropped views (disable with ``--no-rollback-on-failure``)
- Fix PRIMARY KEY ``recordid ASC`` rewrite corrupting column names (e.g. ``recordi HASHd``)
- Strip psql meta-commands (``\\if``, etc.) from ``ysql_dump`` view/table DDL
- Only track dropped views for rollback after Phase 1 transaction commits
- Strip ``colocation_id`` from uncollocated ``CREATE TABLE`` DDL (YSQL rejects it
  with ``COLOCATION = false``)
- Suffix constraint and index names with ``_n`` on new-table DDL so they do not
  collide with the renamed backup table's schema objects

## [0.1.0] - 2026-05-20

### Added

- Initial standalone PyPI package `yugabyte-decolocate-tables`
- CLI entry point `decolocate-tables` and `python -m decolocate_tables`
- Colocated-to-uncollocated migration with view recreation, parallel COPY, resume
- DDL adjustments: `COLOCATION = false`, `SPLIT INTO` tablets, PK `ASC` to `HASH`
