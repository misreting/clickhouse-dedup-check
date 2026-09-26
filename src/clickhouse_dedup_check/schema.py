"""Discover ReplacingMergeTree tables and how each one is configured.

Everything here reads only `system.tables` -- no locks, no cost proportional to
table size, safe to run against production.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .client import QueryClient, execute

_ENGINE_PARAMS_RE = re.compile(r"ReplacingMergeTree\(([^)]*)\)")
_CLEANUP_SETTING_RE = re.compile(
    r"allow_experimental_replacing_merge_with_cleanup\s*=\s*'?1'?", re.IGNORECASE
)


@dataclass(frozen=True)
class ReplacingTable:
    """One ReplacingMergeTree (or Replicated variant) table and its configuration."""

    database: str
    name: str
    engine: str
    engine_full: str
    sorting_key: str
    ver_column: str | None
    is_deleted_column: str | None
    cleanup_enabled: bool
    total_rows: int
    active_parts: int

    @property
    def qualified_name(self) -> str:
        return f"{self.database}.{self.name}"

    @property
    def sorting_key_columns(self) -> list[str]:
        key = self.sorting_key.strip()
        if not key:
            return []
        return [column.strip() for column in key.split(",") if column.strip()]


def _parse_version_params(engine_full: str) -> tuple[str | None, str | None]:
    """Pull `ver` and `is_deleted` out of `ReplacingMergeTree(ver, is_deleted)`."""
    match = _ENGINE_PARAMS_RE.search(engine_full)
    if not match:
        return None, None
    params = [p.strip() for p in match.group(1).split(",") if p.strip()]
    ver = params[0] if len(params) >= 1 else None
    is_deleted = params[1] if len(params) >= 2 else None
    return ver, is_deleted


def discover_replacing_tables(
    client: QueryClient, database: str | None = None
) -> list[ReplacingTable]:
    """Find every ReplacingMergeTree table the connected user can see.

    Pass `database` to limit the scan to one database; otherwise every
    non-system database is scanned.
    """
    where = "engine LIKE '%ReplacingMergeTree%'"
    if database is not None:
        safe_database = database.replace("'", "''")
        where += f" AND database = '{safe_database}'"
    else:
        where += " AND database NOT IN ('system', 'information_schema', 'INFORMATION_SCHEMA')"

    sql = (
        "SELECT database, name, engine, engine_full, sorting_key, "
        "total_rows, active_parts "
        f"FROM system.tables WHERE {where}"
    )
    rows = execute(client, sql)
    tables: list[ReplacingTable] = []
    for database_, name, engine, engine_full, sorting_key, total_rows, active_parts in rows:
        engine_full_str = str(engine_full or "")
        ver, is_deleted = _parse_version_params(engine_full_str)
        tables.append(
            ReplacingTable(
                database=str(database_),
                name=str(name),
                engine=str(engine),
                engine_full=engine_full_str,
                sorting_key=str(sorting_key or ""),
                ver_column=ver,
                is_deleted_column=is_deleted,
                cleanup_enabled=bool(_CLEANUP_SETTING_RE.search(engine_full_str)),
                total_rows=int(total_rows or 0),
                active_parts=int(active_parts or 0),
            )
        )
    return tables
