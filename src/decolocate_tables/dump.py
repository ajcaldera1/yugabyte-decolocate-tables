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
from pathlib import Path
from typing import List, Optional, Tuple

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


def _run_ysql_dump(
    ysql_dump: str,
    conninfo: dict,
    table_pattern: str,
    schema_only: bool = True,
) -> str:
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
        "-t", table_pattern,
        "--no-owner",
        "--include-yb-metadata",
    ]
    if schema_only:
        cmd.append("--schema-only")

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
            f"ysql_dump failed for {table_pattern}:\n{result.stderr or result.stdout}"
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
    pattern = f"{table.qualified.schema}.{backup_name}"
    raw = _run_ysql_dump(ysql_dump, conninfo, pattern, schema_only=True)
    raw = strip_psql_meta_commands(strip_view_statements(raw))
    raw = rewrite_table_name_in_sql(
        raw,
        table.qualified.schema,
        backup_name,
        table.qualified.name,
    )
    _create_sql, post_sql = split_schema_dump(raw)
    post_sql = suffix_new_table_object_names(post_sql)

    safe = f"{table.qualified.schema}.{table.qualified.name}".replace(".", "_")
    create_path = work_dir / f"table_{safe}_create.sql"
    post_path = work_dir / f"table_{safe}_post_create.sql"

    create_path.write_text(
        f"-- Phase 1 already completed for {table.qualified}; "
        "CREATE TABLE skipped on resume.\n",
        encoding="utf-8",
    )
    post_path.write_text(post_sql + "\n", encoding="utf-8")

    table.create_table_sql_path = str(create_path.resolve())
    table.post_create_sql_path = str(post_path.resolve())
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
    pattern = f"{table.qualified.schema}.{table.qualified.name}"
    raw = _run_ysql_dump(ysql_dump, conninfo, pattern, schema_only=True)
    raw = strip_psql_meta_commands(strip_view_statements(raw))
    processed = inject_colocation_false(raw, split_into_tablets=split_into_tablets)
    processed = suffix_new_table_object_names(processed)
    create_sql, post_sql = split_schema_dump(processed)

    safe = f"{table.qualified.schema}.{table.qualified.name}".replace(".", "_")
    create_path = work_dir / f"table_{safe}_create.sql"
    post_path = work_dir / f"table_{safe}_post_create.sql"

    create_path.write_text(create_sql + "\n", encoding="utf-8")
    post_path.write_text(post_sql + "\n", encoding="utf-8")

    table.create_table_sql_path = str(create_path.resolve())
    table.post_create_sql_path = str(post_path.resolve())
    logger.info("Captured table DDL for %s", table.qualified)


def capture_view_ddl(
    view: ViewInfo,
    work_dir: Path,
    ysql_dump: str,
    conninfo: dict,
) -> None:
    pattern = f"{view.qualified.schema}.{view.qualified.name}"
    raw = _run_ysql_dump(ysql_dump, conninfo, pattern, schema_only=True)
    raw = strip_psql_meta_commands(raw)
    safe = f"{view.qualified.schema}.{view.qualified.name}".replace(".", "_")
    path = work_dir / f"view_{safe}.sql"
    path.write_text(raw, encoding="utf-8")
    view.ddl_file = str(path.resolve())
    logger.info("Captured view DDL for %s", view.qualified)
