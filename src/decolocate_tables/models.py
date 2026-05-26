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

import hashlib
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

# Internal marker appended when renaming the colocated table during Phase 1.
BACKUP_NAME_SUFFIX = "_colocated_bak"
BACKUP_NAME_HASH_HEX_LEN = 8
POSTGRES_MAX_IDENTIFIER_BYTES = 63


def _strip_identifier_quotes(ident: str) -> str:
    ident = ident.strip()
    if len(ident) >= 2 and ident[0] == '"' and ident[-1] == '"':
        return ident[1:-1].replace('""', '"')
    return ident


def identifier_byte_length(name: str) -> int:
    """Byte length of *name* as stored in PostgreSQL identifiers (UTF-8)."""
    return len(name.encode("utf-8"))


def _utf8_truncate(name: str, max_bytes: int) -> str:
    """Truncate *name* to at most *max_bytes* UTF-8 bytes without splitting code points."""
    if max_bytes <= 0:
        return ""
    encoded = name.encode("utf-8")
    if len(encoded) <= max_bytes:
        return name
    truncated = encoded[:max_bytes]
    while truncated and (truncated[-1] & 0xC0) == 0x80:
        truncated = truncated[:-1]
    return truncated.decode("utf-8", errors="ignore")


def derive_backup_name(table_name: str) -> str:
    """
    Deterministic backup relation name for Phase 1 rename.

    Uses ``{table_name}{BACKUP_NAME_SUFFIX}`` when it fits within
    ``POSTGRES_MAX_IDENTIFIER_BYTES``; otherwise
    ``{prefix}_{hash}{BACKUP_NAME_SUFFIX}`` with an 8-hex SHA-256 digest of the
    canonical name so resume and state detection stay stable.
    """
    canonical = f"{table_name}{BACKUP_NAME_SUFFIX}"
    if identifier_byte_length(canonical) <= POSTGRES_MAX_IDENTIFIER_BYTES:
        return canonical

    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    hash_part = digest[:BACKUP_NAME_HASH_HEX_LEN]
    suffix_bytes = identifier_byte_length(BACKUP_NAME_SUFFIX)
    overhead = 1 + len(hash_part) + suffix_bytes
    max_prefix_bytes = POSTGRES_MAX_IDENTIFIER_BYTES - overhead
    if max_prefix_bytes < 0:
        raise ValueError(
            f"backup name suffix {BACKUP_NAME_SUFFIX!r} is too long for "
            f"PostgreSQL identifiers (max {POSTGRES_MAX_IDENTIFIER_BYTES} bytes)"
        )

    prefix = _utf8_truncate(table_name, max_prefix_bytes)
    return f"{prefix}_{hash_part}{BACKUP_NAME_SUFFIX}"


def _identifier_needs_quoting(ident: str) -> bool:
    """True when the identifier must be double-quoted in SQL / ysql_dump patterns."""
    if ident != ident.lower():
        return True
    return not re.match(r"^[a-z_][a-z0-9_$]*$", ident, re.IGNORECASE)


@dataclass(frozen=True)
class QualifiedName:
    schema: str
    name: str

    @classmethod
    def parse(cls, text: str) -> "QualifiedName":
        text = text.strip()
        if "." in text:
            schema, name = text.split(".", 1)
            return cls(
                schema=_strip_identifier_quotes(schema),
                name=_strip_identifier_quotes(name),
            )
        return cls(schema="public", name=_strip_identifier_quotes(text))

    def __str__(self) -> str:
        return f"{self.schema}.{self.name}"

    def regclass(self) -> str:
        return f'"{self.schema}"."{self.name}"'

    def ysql_dump_table_pattern(self) -> str:
        """
        Pattern for ``ysql_dump -t`` / ``pg_dump -t``.

        Mixed-case and special identifiers must be quoted or dump finds no tables.
        """

        def _part(ident: str) -> str:
            return f'"{ident}"' if _identifier_needs_quoting(ident) else ident

        return f"{_part(self.schema)}.{_part(self.name)}"


def parse_table_list(table_args: Sequence[str]) -> List[QualifiedName]:
    """
    Parse --table CLI values. Each argument may list multiple tables separated
    by commas or semicolons. Repeatable --table flags are merged (deduplicated).
    """
    result: List[QualifiedName] = []
    seen: set[str] = set()
    for arg in table_args:
        for piece in re.split(r"[,;]+", arg):
            piece = piece.strip()
            if not piece:
                continue
            qn = QualifiedName.parse(piece)
            key = str(qn)
            if key in seen:
                continue
            seen.add(key)
            result.append(qn)
    return result


@dataclass
class TableInfo:
    qualified: QualifiedName
    oid: int
    relkind: str
    is_colocated: bool
    # OID used for dependency discovery (pg_depend).  After Phase 1 rename this
    # is the backup table's OID, which still matches the pre-migration table OID.
    source_oid: Optional[int] = None
    # True when Phase 1 already committed (target uncollocated, backup present).
    resuming: bool = False
    create_table_sql_path: Optional[str] = None
    post_create_sql_path: Optional[str] = None
    backup_name: Optional[str] = None
    row_count: Optional[int] = None
    data_copy_method: Optional[str] = None
    had_statistics: Optional[bool] = None
    will_analyze: Optional[bool] = None


@dataclass
class ViewInfo:
    qualified: QualifiedName
    oid: int
    depends_on: List[str] = field(default_factory=list)
    ddl_file: Optional[str] = None


@dataclass
class MigrationPlan:
    tables: List[TableInfo]
    views_create_order: List[ViewInfo]
    views_drop_order: List[ViewInfo]
    work_dir: str
    dry_run: bool = True
    copy_threads: int = 4
    analyze_if_had_stats: bool = True
