"""Aggregate schema discovery, live duplication measurement and trap detection
into one report, with a CI-friendly exit code.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any

from .client import QueryClient
from .duplication import DuplicationReport, measure_duplication
from .schema import ReplacingTable, discover_replacing_tables
from .traps import (
    Trap,
    check_final_usage,
    check_is_deleted_cleanup,
    check_optimize_final_illusion,
    check_version_ties,
)

EXIT_OK = 0
EXIT_WARNING = 1
EXIT_CRITICAL = 2

_SEVERITY_EXIT = {"critical": EXIT_CRITICAL, "warning": EXIT_WARNING, "info": EXIT_OK}


@dataclass
class TableReport:
    table: ReplacingTable
    duplication: DuplicationReport | None
    traps: list[Trap] = field(default_factory=list)


@dataclass
class Report:
    tables: list[TableReport]

    @property
    def exit_code(self) -> int:
        code = EXIT_OK
        for table_report in self.tables:
            for trap in table_report.traps:
                code = max(code, _SEVERITY_EXIT[trap.severity])
        return code

    def to_dict(self) -> dict[str, Any]:
        return {
            "tables": [
                {
                    "table": tr.table.qualified_name,
                    "engine": tr.table.engine,
                    "sorting_key": tr.table.sorting_key,
                    "ver_column": tr.table.ver_column,
                    "is_deleted_column": tr.table.is_deleted_column,
                    "cleanup_enabled": tr.table.cleanup_enabled,
                    "active_parts": tr.table.active_parts,
                    "duplication": (
                        None
                        if tr.duplication is None
                        else {
                            "physical_rows": tr.duplication.physical_rows,
                            "distinct_keys": tr.duplication.distinct_keys,
                            "duplicate_rows": tr.duplication.duplicate_rows,
                            "duplication_ratio": tr.duplication.duplication_ratio,
                        }
                    ),
                    "traps": [
                        {
                            "id": t.id,
                            "severity": t.severity,
                            "message": t.message,
                            "fix": t.fix,
                            "cost": t.cost,
                        }
                        for t in tr.traps
                    ],
                }
                for tr in self.tables
            ],
            "exit_code": self.exit_code,
        }

    def to_json(self, indent: int | None = 2) -> str:
        return _json.dumps(self.to_dict(), indent=indent)


def run_check(
    client: QueryClient,
    database: str | None = None,
    measure: bool = True,
    query_log_lookback: str = "1 DAY",
) -> Report:
    """Run the full audit: discover tables, measure live duplication, flag traps."""
    tables = discover_replacing_tables(client, database=database)
    table_reports: list[TableReport] = []
    for table in tables:
        duplication = measure_duplication(client, table) if measure else None

        traps: list[Trap] = []
        final_trap = check_final_usage(client, table, lookback=query_log_lookback)
        if final_trap is not None:
            traps.append(final_trap)
        version_trap = check_version_ties(client, table)
        if version_trap is not None:
            traps.append(version_trap)
        cleanup_trap = check_is_deleted_cleanup(table)
        if cleanup_trap is not None:
            traps.append(cleanup_trap)
        if duplication is not None:
            illusion_trap = check_optimize_final_illusion(duplication)
            if illusion_trap is not None:
                traps.append(illusion_trap)

        table_reports.append(TableReport(table=table, duplication=duplication, traps=traps))
    return Report(tables=table_reports)
