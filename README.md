# yugabyte-decolocate-tables

Migrate colocated YugabyteDB YSQL tables to uncollocated (`WITH (COLOCATION = false)`) and
recreate dependent regular views.

PyPI package: **`yugabyte-decolocate-tables`** · CLI: **`decolocate-tables`**

Repository: [github.com/ajcaldera1/yugabyte-decolocate-tables](https://github.com/ajcaldera1/yugabyte-decolocate-tables)

## Install

```bash
pip install yugabyte-decolocate-tables
```

From a clone:

```bash
git clone https://github.com/ajcaldera1/yugabyte-decolocate-tables.git
cd yugabyte-decolocate-tables
pip install .
```

Development:

```bash
pip install -e ".[dev]"
```

See [PUBLISHING.md](PUBLISHING.md) for release builds and PyPI upload.

## Prerequisites

- YugabyteDB cluster with a **colocated database**
- `ysql_dump` on `PATH` (from a YugabyteDB build/install, or `--ysql-dump`)
- Python 3.8+

## Usage

```bash
# Dry run (default): discover dependencies, capture DDL, write manifest
decolocate-tables \
  --host 127.0.0.1 --port 5433 --dbname mydb --user yugabyte \
  --table public.orders,public.order_items

# Apply migration
decolocate-tables \
  --host 127.0.0.1 --port 5433 --dbname mydb --user yugabyte \
  --table public.orders \
  --execute
```

Equivalent:

```bash
python -m decolocate_tables --dbname mydb --table public.t1
```

### SSL connections

TLS settings follow [libpq](https://www.postgresql.org/docs/current/libpq-ssl.html) conventions.
Each `--ssl*` flag falls back to the matching `PGSSL*` environment variable when omitted.
Settings apply to all psycopg connections (including parallel `COPY` workers on `yb_servers()`
nodes) and to `ysql_dump`.

```bash
decolocate-tables \
  --host mycluster.example.com --port 5433 \
  --dbname mydb --user admin --password "$PGPASSWORD" \
  --sslmode verify-full --sslrootcert /path/to/ca.crt \
  --table public.orders --execute
```

Each `--ssl*` flag falls back to the matching `PGSSL*` environment variable when omitted
(for example `export PGSSLMODE=require`).

With `verify-full`, hostnames from `yb_servers()` used for parallel `COPY` must match the
certificate SAN; otherwise use `verify-ca` or `require`.

### Odyssey / YSQL connection manager

On YugabyteDB managed and other deployments that use the Odyssey-based [YSQL Connection Manager](https://docs.yugabyte.com/stable/additional-features/connection-manager-ysql/), repeated `ysql_dump` runs can fail with `prepared statement "dumpfunc" already exists` when a pooled backend still holds `ysql_dump` prepared statements.

By default, the tool runs `DEALLOCATE ALL` on several short-lived connections before and after each `ysql_dump` to reset pooled backends. Use `--no-clear-odyssey-prepares` only when connecting directly to YSQL without a pooler.

### Options

| Flag | Description |
|------|-------------|
| `--table SCHEMA.TABLE` | Table(s) to decolocate; comma/semicolon-separated lists and repeatable flags are supported |
| `--execute` | Apply changes (default is dry-run) |
| `--work-dir DIR` | Keep captured SQL and `manifest.json` |
| `--backup-suffix SUFFIX` | Suffix for renamed backup tables (default `_colocated_bak`) |
| `--split-into-tablets N` | `SPLIT INTO N TABLETS` on new uncollocated tables (default: `1`) |
| `--ysql-dump PATH` | Path to `ysql_dump` binary |
| `--no-clear-odyssey-prepares` | Skip DEALLOCATE before/after each `ysql_dump` (direct YSQL, no pooler) |
| `--no-rollback-on-failure` | Do not restore tables/views if `--execute` fails after Phase 1 |
| `--sslmode MODE` | libpq SSL mode (`disable`, `allow`, `prefer`, `require`, `verify-ca`, `verify-full`); default: `$PGSSLMODE` |
| `--sslcert PATH` | Client certificate; default: `$PGSSLCERT` |
| `--sslkey PATH` | Client private key; default: `$PGSSLKEY` |
| `--sslrootcert PATH` | Trusted CA bundle; default: `$PGSSLROOTCERT` |
| `--sslcrl PATH` | Certificate revocation list; default: `$PGSSLCRL` |
| `--lock-timeout` | `SET lock_timeout` during migration (default `30s`) |
| `--copy-threads N` | Parallel piped `COPY` workers per table (default `4`) |
| `--no-analyze-if-had-stats` | Skip post-migrate `ANALYZE` (for clusters with auto-analyze) |
| `--no-progress` | Disable per-thread COPY progress bars (phase summaries still print) |

## What it does

1. Validates tables are colocated and the database is colocated
2. Aborts on inbound FKs from tables outside the set or dependent materialized views
3. Discovers all dependent **regular views** (including view-on-view chains)
4. Captures full table DDL via `ysql_dump --schema-only --include-yb-metadata`
5. Injects `COLOCATION = false` into `CREATE TABLE` statements
6. On `--execute`: drops views, recreates each table uncollocated, copies data in parallel via piped `COPY` (partitioned by `mod(yb_hash_code(<pk>), N)`), recreates views

If `--execute` fails after Phase 1 (views dropped and table renamed to a backup), the tool automatically drops the empty shell, renames the backup back to the original table name, and recreates dependent views from captured DDL. Use `--no-rollback-on-failure` to leave the database as-is for manual recovery.

## Data copy

| Row count | Method |
|-----------|--------|
| 0 | No data copy (empty table) |
| 1 – 99,999 | `INSERT INTO ... SELECT * FROM ...` |
| 100,000+ | Parallel piped `COPY` via `--copy-threads` workers |

Captured `CREATE TABLE` DDL is adjusted for uncollocated tables: `WITH (COLOCATION = false)`, a top-level `SPLIT INTO 1 TABLETS` clause (by default), and the primary-key sharding column is changed from `ASC` to `HASH`.

Migration runs per table (FK order): DDL → data copy → finalize (indexes/constraints) → optional `ANALYZE`, then recreates all dependent views.

## Resuming after interruption

Re-run with the same `--work-dir`, `--backup-suffix`, and `--table` values. The tool detects Phase 1 completion, truncates partial data, and continues from the backup table.

## Downtime

Table recreation requires quiescing writes. See [YugabyteDB colocation documentation](https://docs.yugabyte.com/stable/additional-features/colocation/).

## Tests

```bash
pip install -e ".[dev]"
./tests/run_tests.sh
```

Integration tests: `DECOLOCATE_INTEGRATION=1 ./tests/run_tests.sh` with a reachable cluster.

## License

Apache License 2.0. See [LICENSE](LICENSE).
