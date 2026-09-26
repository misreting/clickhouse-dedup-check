"""clickhouse-dedup-check: catch the ReplacingMergeTree eventual-deduplication trap.

Public API:

- `discover_replacing_tables(client, database=None)` -- find ReplacingMergeTree tables.
- `measure_duplication(client, table)` -- live physical rows vs. distinct sorting keys.
- `run_check(client, database=None)` -- full audit: schema + duplication + traps.

`client` is duck-typed: anything with a `.query(sql) -> result` method works.
`HttpClient` is a stdlib-only convenience client for ClickHouse's HTTP interface.
"""

from __future__ import annotations

from .client import ClickHouseError, HttpClient, QueryClient
from .duplication import DuplicationReport, measure_duplication
from .report import EXIT_CRITICAL, EXIT_OK, EXIT_WARNING, Report, TableReport, run_check
from .schema import ReplacingTable, discover_replacing_tables
from .traps import (
    Trap,
    check_final_usage,
    check_is_deleted_cleanup,
    check_optimize_final_illusion,
    check_version_ties,
)

__version__ = "0.1.0"

__all__ = [
    "ClickHouseError",
    "HttpClient",
    "QueryClient",
    "DuplicationReport",
    "measure_duplication",
    "EXIT_CRITICAL",
    "EXIT_OK",
    "EXIT_WARNING",
    "Report",
    "TableReport",
    "run_check",
    "ReplacingTable",
    "discover_replacing_tables",
    "Trap",
    "check_final_usage",
    "check_is_deleted_cleanup",
    "check_optimize_final_illusion",
    "check_version_ties",
]
