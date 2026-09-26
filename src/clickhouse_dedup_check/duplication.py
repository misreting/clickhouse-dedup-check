"""Measure how far a table actually is from deduplicated, right now.

This is the number a naive dashboard query sees: a plain `count()` against the
table, with no `FINAL`. Compared against the count of distinct sorting keys,
the gap is live, uncollapsed duplication -- not a hypothetical risk.
"""

from __future__ import annotations

from dataclasses import dataclass

from .client import QueryClient, execute
from .schema import ReplacingTable


@dataclass(frozen=True)
class DuplicationReport:
    table: str
    physical_rows: int
    distinct_keys: int

    @property
    def duplicate_rows(self) -> int:
        return max(self.physical_rows - self.distinct_keys, 0)

    @property
    def duplication_ratio(self) -> float:
        if self.distinct_keys == 0:
            return 0.0
        return self.physical_rows / self.distinct_keys

    @property
    def is_deduplicated(self) -> bool:
        return self.physical_rows == self.distinct_keys


def measure_duplication(client: QueryClient, table: ReplacingTable) -> DuplicationReport:
    """Count physical rows vs. distinct sorting keys with a plain SELECT (no FINAL).

    Costs one full-table `count()` and one `uniqExact()` over the sorting key --
    the same shape of query most dashboards already run, so this is a realistic
    stand-in for what they'd actually see.
    """
    key_columns = table.sorting_key_columns
    if not key_columns:
        raise ValueError(f"{table.qualified_name} has no usable sorting key")
    key_expr = ", ".join(key_columns)
    sql = (
        f"SELECT count() AS physical_rows, uniqExact({key_expr}) AS distinct_keys "
        f"FROM {table.qualified_name}"
    )
    rows = execute(client, sql)
    physical_rows, distinct_keys = rows[0]
    return DuplicationReport(
        table=table.qualified_name,
        physical_rows=int(physical_rows),
        distinct_keys=int(distinct_keys),
    )
