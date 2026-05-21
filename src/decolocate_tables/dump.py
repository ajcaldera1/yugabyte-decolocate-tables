# Copyright (c) YugabyteDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
# in compliance with the License.  You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under the License
# is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied.  See the License for the specific language governing permissions and limitations
# under the License.

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from decolocate_tables.connection import clear_odyssey_pooled_prepares, libpq_ssl_env
from decolocate_tables.models import QualifiedName, TableInfo, ViewInfo

logger = logging.getLogger(__name__)

CREATE_TABLE_RE = re.compile(
    r"^CREATE\s+TABLE\s+",
    re.IGNORECASE | re.MULTILINE,
)

COLOCATION_TRUE_RE = re.compile(
    r"\b(COLOCATION|colocation)\s*=\s*true\b",
    re.IGNORECASE,
)

# ysql_dump --include-yb-metadata may emit colocation_id; invalid with COLOCATION=false.
COLOCATION_ID_PROP_RE = re.compile(
    r",?\s*\b(?:COLOCATION_ID|colocation_id)\s*=\s*[^,)]+",
    re.IGNORECASE,
)

WITH_CLAUSE_RE = re.compile(r"\bWITH\s*\(", re.IGNORECASE)

SPLIT_INTO_TABLETS_RE = re.compile(
    r"\bSPLIT\s+INTO\s+\d+\s+TABLETS\b",
    re.IGNORECASE,
)

# Default for uncollocated CREATE TABLE shells after RENAME (one tablet per table).
DEFAULT_SPLIT_INTO_TABLETS = 1

# Colocated tables use ASC on the primary-key sharding column; uncollocated use HASH.
PK_COMPOSITE_ASC_RE = re.compile(
    r"(\bPRIMARY\s+KEY\s*\(\s*\([^)]+\))\s+ASC\b",
    re.IGNORECASE,
)
PK_FIRST_COLUMN_ASC_RE = re.compile(
    r"(\bPRIMARY\s+KEY\s*\(\s*(?!\()(\"[^\"]+\"|[\w.]+))\s+ASC\b",
    re.IGNORECASE,
)
PK_FIRST_COLUMN_PLAIN_RE = re.compile(
    r"""
    (\bPRIMARY\s+KEY\s*\(\s*)       # PRIMARY KEY (
    (?!\()                          # not composite (col1, col2) form
    (\"[^\"]+\"|[\w.]+)             # leading column
    (?=\s*[,)])                     # column ends before , or )
    """,
    re.IGNORECASE | re.VERBOSE,
)

VIEW_STMT_RE = re.compile(
    r"^\s*(CREATE|ALTER)\s+(OR\s+REPLACE\s+)?VIEW\b",
    re.IGNORECASE | re.MULTILINE,
)


class DumpError(Exception):
    pass


DDL_CAPTURE_MODE_BATCHED = "batched"
DDL_CAPTURE_MODE_PER_OBJECT = "per-object"
DEFAULT_DDL_CAPTURE_BATCH_SIZE = 32

# schema.table with optional double-quoted identifiers
_QUAL_NAME_IN_STMT_RE = re.compile(
    r'("([^"]+)"|([a-zA-Z_][\w$]*))\s*\.\s*("([^"]+)"|([a-zA-Z_][\w$]*))'
)

_CREATE_TABLE_HEAD_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:ONLY\s+)?",
    re.IGNORECASE,
)
_ALTER_TABLE_HEAD_RE = re.compile(
    r"ALTER\s+TABLE\s+(?:ONLY\s+)?",
    re.IGNORECASE,
)
_CREATE_VIEW_HEAD_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+",
    re.IGNORECASE,
)


def _qualified_from_stmt_match(m: re.Match[str]) -> QualifiedName:
    # Groups: (schema token, schema quoted, schema bare, name token, name quoted, name bare)
    schema = m.group(2) if m.group(2) is not None else m.group(3)
    name = m.group(5) if m.group(5) is not None else m.group(6)
    return QualifiedName(schema=schema, name=name)


def _qualified_after_prefix(stmt: str, prefix_re: re.Pattern[str]) -> Optional[QualifiedName]:
    m = prefix_re.search(stmt)
    if not m:
        return None
    tail = stmt[m.end() :]
    qm = _QUAL_NAME_IN_STMT_RE.match(tail.lstrip())
    if not qm:
        return None
    return _qualified_from_stmt_match(qm)


def _qualified_on_clause(stmt: str) -> Optional[QualifiedName]:
    m = re.search(r"\bON\s+(?:ONLY\s+)?", stmt, re.IGNORECASE)
    if not m:
        return None
    qm = _QUAL_NAME_IN_STMT_RE.match(stmt[m.end() :].lstrip())
    if not qm:
        return None
    return _qualified_from_stmt_match(qm)


def _statement_owner_table(
    stmt: str,
    tables_by_key: Dict[str, QualifiedName],
) -> Optional[str]:
    """Return ``str(qualified)`` key for the table that owns *stmt*, if known."""
    for prefix_re in (_CREATE_TABLE_HEAD_RE, _ALTER_TABLE_HEAD_RE):
        qn = _qualified_after_prefix(stmt, prefix_re)
        if qn is not None:
            key = str(qn)
            if key in tables_by_key:
                return key

    qn = _qualified_on_clause(stmt)
    if qn is not None:
        key = str(qn)
        if key in tables_by_key:
            return key

    mentioned = []
    for key, qn in tables_by_key.items():
        if qn.regclass() in stmt:
            mentioned.append(key)
            continue
        bare = f"{qn.schema}.{qn.name}"
        if bare in stmt or bare.lower() in stmt.lower():
            mentioned.append(key)
    if len(mentioned) == 1:
        return mentioned[0]
    return None


def partition_table_dump(
    sql: str,
    qualified_names: Sequence[QualifiedName],
) -> Dict[str, str]:
    """
    Split a multi-table ``ysql_dump`` into per-table SQL blobs keyed by ``str(qualified)``.
    """
    tables_by_key = {str(qn): qn for qn in qualified_names}
    sanitized = strip_psql_meta_commands(strip_view_statements(sql))
    buckets: Dict[str, List[str]] = {key: [] for key in tables_by_key}

    for stmt in _split_sql_statements(sanitized):
        if not is_executable_sql_statement(stmt):
            continue
        owner = _statement_owner_table(stmt, tables_by_key)
        if owner is None:
            logger.debug("Skipping unattributed table DDL: %.80s", stmt)
            continue
        buckets[owner].append(stmt)

    return {key: "\n\n".join(parts) for key, parts in buckets.items()}


def _statement_owner_view(
    stmt: str,
    views_by_key: Dict[str, QualifiedName],
) -> Optional[str]:
    qn = _qualified_after_prefix(stmt, _CREATE_VIEW_HEAD_RE)
    if qn is not None:
        key = str(qn)
        if key in views_by_key:
            return key
    return None


def partition_view_dump(
    sql: str,
    qualified_names: Sequence[QualifiedName],
) -> Dict[str, str]:
    """Split a multi-view ``ysql_dump`` into per-view SQL keyed by ``str(qualified)``."""
    views_by_key = {str(qn): qn for qn in qualified_names}
    sanitized = strip_psql_meta_commands(sql)
    buckets: Dict[str, List[str]] = {key: [] for key in views_by_key}

    for stmt in _split_sql_statements(sanitized):
        if not is_executable_sql_statement(stmt):
            continue
        owner = _statement_owner_view(stmt, views_by_key)
        if owner is None:
            logger.debug("Skipping unattributed view DDL: %.80s", stmt)
            continue
        buckets[owner].append(stmt)

    return {key: "\n\n".join(parts) for key, parts in buckets.items()}


def _batched(items: Sequence, batch_size: int):
    for i in range(0, len(items), batch_size):
        yield items[i : i + batch_size]


def _run_ysql_dump(
    ysql_dump: str,
    conninfo: dict,
    table_patterns: Union[str, Sequence[str]],
    schema_only: bool = True,
) -> str:
    if isinstance(table_patterns, str):
        patterns = [table_patterns]
    else:
        patterns = list(table_patterns)

    if conninfo.get("clear_odyssey_prepares", True):
        clear_odyssey_pooled_prepares(conninfo)

    env = libpq_ssl_env(conninfo, os.environ.copy())
    if conninfo.get("password"):
        env["PGPASSWORD"] = conninfo["password"]

    cmd = [
        ysql_dump,
        "-h", conninfo["host"],
        "-p", str(conninfo["port"]),
        "-U", conninfo["user"],
        "-d", conninfo["dbname"],
        "--no-owner",
        "--include-yb-metadata",
    ]
    for pattern in patterns:
        cmd.extend(["-t", pattern])
    if schema_only:
        cmd.append("--schema-only")

    label = ", ".join(patterns) if len(patterns) <= 3 else f"{len(patterns)} objects"
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise DumpError(
            f"ysql_dump failed for {label}:\n{result.stderr or result.stdout}"
        )

    if conninfo.get("clear_odyssey_prepares", True):
        clear_odyssey_pooled_prepares(conninfo)

    return result.stdout


def _effective_split_tablets(split_into_tablets: Optional[int]) -> int:
    if split_into_tablets is None:
        return DEFAULT_SPLIT_INTO_TABLETS
    return split_into_tablets


def _colocation_false_with_option() -> str:
    """YSQL table property; belongs inside ``WITH (...)`` only."""
    return "COLOCATION = false"


def convert_primary_key_to_hash_sharding(stmt: str) -> str:
    """
    Rewrite primary-key definitions for uncollocated tables.

    Colocated table DDL from ysql_dump typically marks the sharding column with
    ASC (range-style). Uncollocated tables should use HASH on that column instead.
    """
    stmt = PK_COMPOSITE_ASC_RE.sub(r"\1 HASH", stmt)
    stmt = PK_FIRST_COLUMN_ASC_RE.sub(r"\1 HASH", stmt)
    stmt = PK_FIRST_COLUMN_PLAIN_RE.sub(r"\1\2 HASH", stmt, count=1)
    return stmt


def strip_colocation_id_properties(stmt: str) -> str:
    """Remove ``colocation_id`` / ``COLOCATION_ID`` from table storage options."""
    stmt = COLOCATION_ID_PROP_RE.sub("", stmt)
    return _normalize_with_list_commas(stmt)


def _normalize_with_list_commas(stmt: str) -> str:
    """Tidy ``WITH (...)`` lists after removing misplaced clauses."""
    stmt = re.sub(r"\(\s*,", "(", stmt)
    stmt = re.sub(r",\s*,", ", ", stmt)
    stmt = re.sub(r",\s*\)", ")", stmt)
    return stmt


def _ensure_split_clause(stmt: str, num_tablets: int) -> str:
    """
    Ensure ``SPLIT INTO n TABLETS`` is a top-level clause, not inside ``WITH``.

    YugabyteDB grammar: ``WITH (COLOCATION = ...)`` then ``SPLIT INTO ... TABLETS``.
    """
    stmt = SPLIT_INTO_TABLETS_RE.sub("", stmt)
    stmt = _normalize_with_list_commas(stmt)
    stmt = stmt.rstrip().rstrip(";").rstrip()
    return f"{stmt} SPLIT INTO {num_tablets} TABLETS;"


def _inject_colocation_into_statement(
    stmt: str, split_into_tablets: Optional[int] = None
) -> str:
    num_tablets = _effective_split_tablets(split_into_tablets)
    colocation_opt = _colocation_false_with_option()
    stmt = COLOCATION_TRUE_RE.sub(lambda m: f"{m.group(1)} = false", stmt)

    if re.search(r"\bPARTITION\s+OF\b", stmt, re.IGNORECASE):
        # Partition DDL may use: FOR VALUES WITH (...) WITH (COLOCATION = ...).
        stmt = stmt.rstrip().rstrip(";")
        if not re.search(r"\b(COLOCATION|colocation)\s*=\s*false", stmt, re.IGNORECASE):
            stmt = f"{stmt} WITH ({colocation_opt});"
        else:
            stmt = f"{stmt};"
        return convert_primary_key_to_hash_sharding(stmt)

    if not re.search(r"\b(COLOCATION|colocation)\s*=\s*false", stmt, re.IGNORECASE):
        if WITH_CLAUSE_RE.search(stmt):
            stmt = re.sub(
                r"\bWITH\s*\(",
                f"WITH ({colocation_opt}, ",
                stmt,
                count=1,
                flags=re.IGNORECASE,
            )
        else:
            stmt = stmt.rstrip().rstrip(";")
            stmt = f"{stmt} WITH ({colocation_opt})"

    stmt = strip_colocation_id_properties(_ensure_split_clause(stmt, num_tablets))
    return convert_primary_key_to_hash_sharding(stmt)


def inject_colocation_false(sql: str, split_into_tablets: Optional[int] = None) -> str:
    """Ensure CREATE TABLE statements use COLOCATION = false."""
    lines = sql.splitlines(keepends=True)
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not CREATE_TABLE_RE.match(line.strip()):
            out.append(line)
            i += 1
            continue

        stmt_lines = [line]
        i += 1
        while i < len(lines) and not stmt_lines[-1].strip().endswith(";"):
            stmt_lines.append(lines[i])
            i += 1
        stmt = "".join(stmt_lines)
        out.append(_inject_colocation_into_statement(stmt, split_into_tablets))

    return "".join(out)


def _is_ysql_dump_session_line(line: str) -> bool:
    """Lines that require superuser or are only meaningful in ysqlsh/psql."""
    stripped = line.strip().rstrip(";").strip()
    if not stripped:
        return False
    if re.match(r"SET\s+(?:(?:SESSION|LOCAL)\s+)?yb_", stripped, re.IGNORECASE):
        return True
    if re.match(
        r"SELECT\s+.*set_config\s*\(\s*'yb_",
        stripped,
        re.IGNORECASE,
    ):
        return True
    return False


_YB_DO_BLOCK_RE = re.compile(r"DO\s+\$\$.*?\$\$;", re.IGNORECASE | re.DOTALL)


def strip_pg_dump_line_comments(sql: str) -> str:
    """Remove ``--`` comment lines so semicolons inside them are not split as SQL."""
    lines: List[str] = []
    for line in sql.splitlines(keepends=True):
        if line.lstrip().startswith("--"):
            continue
        lines.append(line)
    return "".join(lines)


_PGDUMP_METADATA_FRAGMENT_RE = re.compile(
    r"^(?:Type|Schema|Owner|Name|Dependencies|Relates)\s*:",
    re.IGNORECASE,
)
_EXECUTABLE_SQL_START_RE = re.compile(
    r"^(?:CREATE|ALTER|DROP|COMMENT|GRANT|REVOKE|SET|SELECT|INSERT|UPDATE|"
    r"DELETE|DO|TRUNCATE|ANALYZE|VACUUM|REFRESH)\b",
    re.IGNORECASE,
)
_BINARY_UPGRADE_RESTORE_RE = re.compile(
    r"(?:binary_upgrade_|pg_nextoid|pg_restore_relation|"
    r"pg_restore_constraint|yb_read_|yb_restore_)",
    re.IGNORECASE,
)
# ysql_dump may emit extension maintenance calls the table owner cannot run on cloud.
_PRIVILEGED_MAINTENANCE_RE = re.compile(
    r"(?:pg_stat_statements_(?:reset|save)|pg_reload_conf|"
    r"pg_rotate_logfile|pg_cancel_backend|pg_terminate_backend)\s*\(",
    re.IGNORECASE,
)


def is_executable_sql_statement(stmt: str) -> bool:
    """
    True if *stmt* is real DDL/DML from ysql_dump, not a metadata fragment left
    after splitting on semicolons inside ``-- Name: ...; Type: TABLE;`` comments.
    """
    s = stmt.strip()
    if not s or s == ";":
        return False
    if s.lstrip().startswith("--"):
        return False
    if _PGDUMP_METADATA_FRAGMENT_RE.match(s):
        return False
    if _BINARY_UPGRADE_RESTORE_RE.search(s):
        return False
    if _PRIVILEGED_MAINTENANCE_RE.search(s):
        return False
    return bool(_EXECUTABLE_SQL_START_RE.match(s))


def iter_executable_statements(sql: str):
    """Yield statements from captured SQL that are safe to execute via psycopg."""
    for stmt in _split_sql_statements(sql):
        if is_executable_sql_statement(stmt):
            yield stmt


def strip_yugabyte_restore_do_blocks(sql: str) -> str:
    """
    Remove ``DO $$ ... $$`` blocks that only configure Yugabyte restore ``yb_*`` GUCs.

    Cloud roles often cannot ``SET yb_*`` inside these blocks during view recreate.
    """
    parts: List[str] = []
    last = 0
    for match in _YB_DO_BLOCK_RE.finditer(sql):
        parts.append(sql[last : match.start()])
        block = match.group(0)
        if not re.search(r"\byb_\w+", block, re.IGNORECASE):
            parts.append(block)
        last = match.end()
    parts.append(sql[last:])
    return "".join(parts)


def strip_psql_meta_commands(sql: str) -> str:
    """
    Remove psql meta-commands (``\\if``, ``\\connect``, etc.) from ysql_dump output.

    Also drops Yugabyte session ``SET yb_*`` / ``set_config('yb_*')`` lines and
    restore ``DO`` blocks that ``ysql_dump`` emits but non-superuser roles cannot apply.
    """
    sql = strip_yugabyte_restore_do_blocks(sql)
    sql = strip_pg_dump_line_comments(sql)
    lines: List[str] = []
    for line in sql.splitlines(keepends=True):
        if line.lstrip().startswith("\\"):
            continue
        if _is_ysql_dump_session_line(line):
            continue
        if _BINARY_UPGRADE_RESTORE_RE.search(line):
            continue
        if _PRIVILEGED_MAINTENANCE_RE.search(line):
            continue
        lines.append(line)
    return "".join(lines)


def strip_view_statements(sql: str) -> str:
    """Remove CREATE/ALTER VIEW statements from a table dump."""
    parts = re.split(r"(?m)^(?=CREATE\s|ALTER\s)", sql)
    kept = []
    for part in parts:
        if not part.strip():
            continue
        if VIEW_STMT_RE.match(part):
            continue
        kept.append(part)
    return "".join(kept)


def split_schema_dump(sql: str) -> Tuple[str, str]:
    """
    Split schema dump into CREATE TABLE block(s) and everything else.
    """
    statements = _split_sql_statements(sql)
    create_parts: List[str] = []
    other_parts: List[str] = []

    for stmt in statements:
        stripped = stmt.lstrip()
        if CREATE_TABLE_RE.match(stripped):
            create_parts.append(stmt)
        elif is_executable_sql_statement(stmt):
            other_parts.append(stmt)

    return "\n\n".join(create_parts), "\n\n".join(other_parts)


def _split_sql_statements(sql: str) -> List[str]:
    """Split SQL on semicolons outside of single-quoted strings."""
    statements: List[str] = []
    current: List[str] = []
    in_single = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_single:
            in_single = True
            current.append(ch)
        elif ch == "'" and in_single:
            if i + 1 < len(sql) and sql[i + 1] == "'":
                current.append("''")
                i += 1
            else:
                in_single = False
                current.append(ch)
        elif ch == ";" and not in_single:
            current.append(ch)
            stmt = "".join(current).strip()
            if stmt:
                statements.append(stmt if stmt.endswith(";") else stmt + ";")
            current = []
        else:
            current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail if tail.endswith(";") else tail + ";")
    return statements


# Constraint and index names are unique per schema; the renamed backup keeps the
# original names while the new table's DDL must use distinct names.
DEFAULT_NEW_TABLE_CONSTRAINT_SUFFIX = "_n"

_SQL_IDENT = r'("(?:[^"]|"")*"|\w+)'

_CONSTRAINT_NAME_RE = re.compile(
    rf"\bCONSTRAINT\s+({_SQL_IDENT})",
    re.IGNORECASE,
)
_ADD_CONSTRAINT_NAME_RE = re.compile(
    rf"\bADD\s+CONSTRAINT\s+({_SQL_IDENT})",
    re.IGNORECASE,
)
_CREATE_INDEX_NAME_RE = re.compile(
    rf"(\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+)"
    rf"(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?({_SQL_IDENT})",
    re.IGNORECASE,
)


def _suffix_sql_identifier(ident: str, suffix: str) -> str:
    if ident.startswith('"'):
        inner = ident[1:-1]
        if inner.endswith(suffix):
            return ident
        return f'"{inner}{suffix}"'
    if ident.endswith(suffix):
        return ident
    return ident + suffix


def suffix_new_table_object_names(
    sql: str,
    suffix: str = DEFAULT_NEW_TABLE_CONSTRAINT_SUFFIX,
) -> str:
    """
    Append *suffix* to constraint and index names in captured DDL for the new table.

    After Phase 1 renames the colocated table to a backup, those object names remain
    in the schema; reusing them on the uncollocated table fails with duplicate-name errors.
    """

    def _constraint_repl(m: re.Match[str]) -> str:
        return f"CONSTRAINT {_suffix_sql_identifier(m.group(1), suffix)}"

    def _add_constraint_repl(m: re.Match[str]) -> str:
        return f"ADD CONSTRAINT {_suffix_sql_identifier(m.group(1), suffix)}"

    def _index_repl(m: re.Match[str]) -> str:
        return f"{m.group(1)}{_suffix_sql_identifier(m.group(2), suffix)}"

    sql = _CONSTRAINT_NAME_RE.sub(_constraint_repl, sql)
    sql = _ADD_CONSTRAINT_NAME_RE.sub(_add_constraint_repl, sql)
    sql = _CREATE_INDEX_NAME_RE.sub(_index_repl, sql)
    return sql


def rewrite_table_name_in_sql(
    sql: str,
    schema: str,
    old_name: str,
    new_name: str,
) -> str:
    """
    Rewrite qualified references from schema.old_name to schema.new_name in DDL.
    Used when capturing post-create DDL from a backup table during resume.
    """
    if old_name == new_name:
        return sql
    # "schema"."old" -> "schema"."new" (ysql_dump typically quotes identifiers)
    sql = re.sub(
        re.escape(f'"{schema}"."{old_name}"'),
        f'"{schema}"."{new_name}"',
        sql,
        flags=re.IGNORECASE,
    )
    # schema.old (unquoted identifiers in some dump output)
    sql = re.sub(
        rf"\b{re.escape(schema)}\.{re.escape(old_name)}\b",
        f"{schema}.{new_name}",
        sql,
        flags=re.IGNORECASE,
    )
    # Standalone quoted table name (e.g. ON ONLY "old")
    sql = re.sub(
        re.escape(f'"{old_name}"'),
        f'"{new_name}"',
        sql,
        flags=re.IGNORECASE,
    )
    return sql


def _table_work_paths(work_dir: Path, qn: QualifiedName) -> Tuple[Path, Path]:
    safe = f"{qn.schema}.{qn.name}".replace(".", "_")
    return (
        work_dir / f"table_{safe}_create.sql",
        work_dir / f"table_{safe}_post_create.sql",
    )


def _view_work_path(work_dir: Path, qn: QualifiedName) -> Path:
    safe = f"{qn.schema}.{qn.name}".replace(".", "_")
    return work_dir / f"view_{safe}.sql"


def _apply_table_ddl_from_raw(
    table: TableInfo,
    work_dir: Path,
    raw_sql: str,
    split_into_tablets: Optional[int],
) -> None:
    """Process dumped SQL and write create/post_create files for a new migration."""
    raw = strip_psql_meta_commands(strip_view_statements(raw_sql))
    processed = inject_colocation_false(raw, split_into_tablets=split_into_tablets)
    processed = suffix_new_table_object_names(processed)
    create_sql, post_sql = split_schema_dump(processed)

    create_path, post_path = _table_work_paths(work_dir, table.qualified)
    create_path.write_text(create_sql + "\n", encoding="utf-8")
    post_path.write_text(post_sql + "\n", encoding="utf-8")

    table.create_table_sql_path = str(create_path.resolve())
    table.post_create_sql_path = str(post_path.resolve())


def _apply_resuming_table_post_create(
    table: TableInfo,
    work_dir: Path,
    raw_sql: str,
    backup_name: str,
) -> None:
    raw = strip_psql_meta_commands(strip_view_statements(raw_sql))
    raw = rewrite_table_name_in_sql(
        raw,
        table.qualified.schema,
        backup_name,
        table.qualified.name,
    )
    _create_sql, post_sql = split_schema_dump(raw)
    post_sql = suffix_new_table_object_names(post_sql)

    create_path, post_path = _table_work_paths(work_dir, table.qualified)
    create_path.write_text(
        f"-- Phase 1 already completed for {table.qualified}; "
        "CREATE TABLE skipped on resume.\n",
        encoding="utf-8",
    )
    post_path.write_text(post_sql + "\n", encoding="utf-8")

    table.create_table_sql_path = str(create_path.resolve())
    table.post_create_sql_path = str(post_path.resolve())


def _capture_tables_batched(
    tables: Sequence[TableInfo],
    work_dir: Path,
    ysql_dump: str,
    conninfo: dict,
    split_into_tablets: Optional[int],
    batch_size: int,
) -> int:
    """Return number of ``ysql_dump`` invocations performed."""
    dumps = 0
    to_capture = [t for t in tables if not t.resuming]
    resuming = [t for t in tables if t.resuming]

    for batch in _batched(to_capture, batch_size):
        patterns = [t.qualified.ysql_dump_table_pattern() for t in batch]
        started = time.monotonic()
        raw = _run_ysql_dump(ysql_dump, conninfo, patterns, schema_only=True)
        dumps += 1
        parts = partition_table_dump(raw, [t.qualified for t in batch])
        for table in batch:
            key = str(table.qualified)
            chunk = parts.get(key, "")
            if not chunk.strip():
                raise DumpError(
                    f"No DDL partitioned for {table.qualified} in batched ysql_dump"
                )
            _apply_table_ddl_from_raw(table, work_dir, chunk, split_into_tablets)
            logger.info("Captured table DDL for %s", table.qualified)
        logger.info(
            "Captured DDL for %d table(s) in 1 ysql_dump (%.1fs)",
            len(batch),
            time.monotonic() - started,
        )

    for batch in _batched(resuming, batch_size):
        partition_names = [
            QualifiedName(t.qualified.schema, t.backup_name) for t in batch
        ]
        patterns = [qn.ysql_dump_table_pattern() for qn in partition_names]
        started = time.monotonic()
        raw = _run_ysql_dump(ysql_dump, conninfo, patterns, schema_only=True)
        dumps += 1
        parts = partition_table_dump(raw, partition_names)
        for table in batch:
            if not table.backup_name:
                raise DumpError(
                    f"Resuming table {table.qualified} is missing backup_name"
                )
            key = str(
                QualifiedName(table.qualified.schema, table.backup_name)
            )
            chunk = parts.get(key, "")
            _apply_resuming_table_post_create(
                table, work_dir, chunk, table.backup_name
            )
            logger.info(
                "Captured post-create DDL for resumed migration %s (from backup %s)",
                table.qualified,
                table.backup_name,
            )
        logger.info(
            "Captured post-create DDL for %d resumed table(s) in 1 ysql_dump (%.1fs)",
            len(batch),
            time.monotonic() - started,
        )

    return dumps


def _capture_views_batched(
    views: Sequence[ViewInfo],
    work_dir: Path,
    ysql_dump: str,
    conninfo: dict,
    batch_size: int,
) -> int:
    dumps = 0
    for batch in _batched(views, batch_size):
        patterns = [v.qualified.ysql_dump_table_pattern() for v in batch]
        started = time.monotonic()
        raw = _run_ysql_dump(ysql_dump, conninfo, patterns, schema_only=True)
        dumps += 1
        parts = partition_view_dump(raw, [v.qualified for v in batch])
        for view in batch:
            key = str(view.qualified)
            chunk = parts.get(key, "")
            path = _view_work_path(work_dir, view.qualified)
            path.write_text(chunk + ("\n" if chunk else ""), encoding="utf-8")
            view.ddl_file = str(path.resolve())
            logger.info("Captured view DDL for %s", view.qualified)
        logger.info(
            "Captured DDL for %d view(s) in 1 ysql_dump (%.1fs)",
            len(batch),
            time.monotonic() - started,
        )
    return dumps


def _capture_tables_per_object(
    tables: Sequence[TableInfo],
    work_dir: Path,
    ysql_dump: str,
    conninfo: dict,
    split_into_tablets: Optional[int],
) -> int:
    dumps = 0
    for table in tables:
        if table.resuming:
            if not table.backup_name:
                raise DumpError(
                    f"Resuming table {table.qualified} is missing backup_name"
                )
            capture_table_ddl_resume(
                table,
                work_dir,
                ysql_dump,
                conninfo,
                table.backup_name,
            )
        else:
            capture_table_ddl(
                table,
                work_dir,
                ysql_dump,
                conninfo,
                split_into_tablets=split_into_tablets,
            )
        dumps += 1
    return dumps


def _capture_views_per_object(
    views: Sequence[ViewInfo],
    work_dir: Path,
    ysql_dump: str,
    conninfo: dict,
) -> int:
    dumps = 0
    for view in views:
        capture_view_ddl(view, work_dir, ysql_dump, conninfo)
        dumps += 1
    return dumps


def capture_migration_ddl(
    tables: Sequence[TableInfo],
    views: Sequence[ViewInfo],
    work_dir: Path,
    ysql_dump: str,
    conninfo: dict,
    *,
    split_into_tablets: Optional[int] = None,
    capture_mode: str = DDL_CAPTURE_MODE_BATCHED,
    batch_size: int = DEFAULT_DDL_CAPTURE_BATCH_SIZE,
) -> None:
    """
    Capture table and view DDL for a migration plan.

    Uses one Odyssey ``DEALLOCATE`` pass before and after all dumps (Phase 1), and
    batches multiple ``-t`` patterns per ``ysql_dump`` invocation by default (Phase 2).
    """
    if capture_mode not in (DDL_CAPTURE_MODE_BATCHED, DDL_CAPTURE_MODE_PER_OBJECT):
        raise DumpError(
            f"Invalid ddl capture mode {capture_mode!r}; "
            f"use {DDL_CAPTURE_MODE_BATCHED!r} or {DDL_CAPTURE_MODE_PER_OBJECT!r}"
        )

    session_clear = conninfo.get("clear_odyssey_prepares", True)
    inner_conninfo = {**conninfo, "clear_odyssey_prepares": False}

    if session_clear:
        clear_odyssey_pooled_prepares(conninfo)

    started = time.monotonic()
    try:
        if capture_mode == DDL_CAPTURE_MODE_PER_OBJECT:
            table_dumps = _capture_tables_per_object(
                tables, work_dir, ysql_dump, inner_conninfo, split_into_tablets
            )
            view_dumps = _capture_views_per_object(
                views, work_dir, ysql_dump, inner_conninfo
            )
        else:
            table_dumps = _capture_tables_batched(
                tables,
                work_dir,
                ysql_dump,
                inner_conninfo,
                split_into_tablets,
                batch_size,
            )
            view_dumps = _capture_views_batched(
                views, work_dir, ysql_dump, inner_conninfo, batch_size
            )
    finally:
        if session_clear:
            clear_odyssey_pooled_prepares(conninfo)

    logger.info(
        "DDL capture complete: %d table(s), %d view(s), %d ysql_dump call(s) in %.1fs "
        "(mode=%s)",
        len(tables),
        len(views),
        table_dumps + view_dumps,
        time.monotonic() - started,
        capture_mode,
    )


def capture_table_ddl_resume(
    table: TableInfo,
    work_dir: Path,
    ysql_dump: str,
    conninfo: dict,
    backup_name: str,
) -> None:
    """
    Capture post-create DDL for a table whose Phase 1 already ran.

    The backup table still holds indexes, constraints, and triggers under the
    backup name; dump it and rewrite object names to the live target table.
    """
    pattern = QualifiedName(table.qualified.schema, backup_name).ysql_dump_table_pattern()
    raw = _run_ysql_dump(ysql_dump, conninfo, pattern, schema_only=True)
    _apply_resuming_table_post_create(table, work_dir, raw, backup_name)
    logger.info(
        "Captured post-create DDL for resumed migration %s (from backup %s)",
        table.qualified,
        backup_name,
    )


def capture_table_ddl(
    table: TableInfo,
    work_dir: Path,
    ysql_dump: str,
    conninfo: dict,
    split_into_tablets: Optional[int] = None,
) -> None:
    pattern = table.qualified.ysql_dump_table_pattern()
    raw = _run_ysql_dump(ysql_dump, conninfo, pattern, schema_only=True)
    _apply_table_ddl_from_raw(table, work_dir, raw, split_into_tablets)
    logger.info("Captured table DDL for %s", table.qualified)


def capture_view_ddl(
    view: ViewInfo,
    work_dir: Path,
    ysql_dump: str,
    conninfo: dict,
) -> None:
    pattern = view.qualified.ysql_dump_table_pattern()
    raw = _run_ysql_dump(ysql_dump, conninfo, pattern, schema_only=True)
    raw = strip_psql_meta_commands(raw)
    path = _view_work_path(work_dir, view.qualified)
    path.write_text(raw + "\n", encoding="utf-8")
    view.ddl_file = str(path.resolve())
    logger.info("Captured view DDL for %s", view.qualified)
