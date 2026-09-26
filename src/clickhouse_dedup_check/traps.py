"""Detect the specific ReplacingMergeTree traps this package verified against a
real ClickHouse server (see the README for the reproduction). Every message
below states something that was measured, not assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .client import QueryClient, execute
from .duplication import DuplicationReport
from .schema import ReplacingTable

Severity = Literal["info", "warning", "critical"]


@dataclass(frozen=True)
class Trap:
    id: str
    table: str
    severity: Severity
    message: str
    fix: str
    cost: str


def check_final_usage(
    client: QueryClient, table: ReplacingTable, lookback: str = "1 DAY"
) -> Trap | None:
    """Look at `system.query_log` for recent SELECTs against this table and see
    whether any used FINAL.

    This is empirical, not static analysis: a database connection cannot see
    application source code, only what has actually been run against it. If
    query_log has no matching entries in the window, this returns None rather
    than guessing -- silence here means "no evidence either way," not "safe."
    """
    safe_table = table.qualified_name.replace("'", "''")
    sql = (
        "SELECT countIf(query ILIKE '%FINAL%') AS with_final, count() AS total "
        "FROM system.query_log "
        "WHERE type = 'QueryFinish' AND query_kind = 'Select' "
        f"AND has(tables, '{safe_table}') "
        f"AND event_time > now() - INTERVAL {lookback}"
    )
    rows = execute(client, sql)
    if not rows:
        return None
    with_final, total = rows[0]
    total = int(total)
    with_final = int(with_final)
    if total == 0:
        return None
    if with_final == 0:
        return Trap(
            id="final-not-used",
            table=table.qualified_name,
            severity="critical",
            message=(
                f"{total} recent SELECT(s) against {table.qualified_name} in the last "
                f"{lookback}, none used FINAL. ReplacingMergeTree does not deduplicate "
                "on read -- every one of these queries can see live duplicate rows."
            ),
            fix=(
                "Add FINAL to reads that need a correct answer, or dedupe at query time "
                "with argMax(col, ver) grouped by the sorting key."
            ),
            cost=(
                "FINAL forces a merge-on-read across every part the query touches. "
                "Measured on a fragmented 10M-row / 40-part table: ~19x the query "
                "time and ~50x the memory of the equivalent plain SELECT (see README)."
            ),
        )
    return None


def check_version_ties(client: QueryClient, table: ReplacingTable) -> Trap | None:
    """Flag missing `ver` columns, and count real version ties in the data.

    A tie means ClickHouse's merge has no principled answer to pick from. In
    our reproduction, ties were resolved reproducibly in favor of the row
    inserted later -- but this is observed behavior on one version, not a
    documented guarantee, so it should not be relied on.
    """
    if table.ver_column is None:
        return Trap(
            id="no-version-column",
            table=table.qualified_name,
            severity="warning",
            message=(
                f"{table.qualified_name} is ReplacingMergeTree() with no ver column. "
                "Which duplicate wins on merge is decided by insertion order inside "
                "the merge, not by any column you control."
            ),
            fix="Add an explicit, monotonic ver column (e.g. an event or ingest timestamp).",
            cost="Schema-only change; no runtime cost.",
        )

    key_columns = table.sorting_key_columns
    if not key_columns:
        return None
    key_expr = ", ".join(key_columns)
    sql = (
        "SELECT count() FROM ("
        f"SELECT {key_expr}, {table.ver_column}, count() AS c "
        f"FROM {table.qualified_name} "
        f"GROUP BY {key_expr}, {table.ver_column} HAVING c > 1"
        ")"
    )
    rows = execute(client, sql)
    tied_groups = int(rows[0][0]) if rows else 0
    if tied_groups > 0:
        return Trap(
            id="version-ties",
            table=table.qualified_name,
            severity="critical",
            message=(
                f"{tied_groups} (key, {table.ver_column}) group(s) in "
                f"{table.qualified_name} have more than one row: real version ties, "
                "measured in this table's actual data, not a hypothetical."
            ),
            fix=(
                f"Make {table.ver_column} collision-free -- append a monotonic "
                "tiebreaker (insert sequence, high-resolution timestamp) so two "
                "writes to the same key can never tie."
            ),
            cost="Detecting this is one GROUP BY; fixing it is a schema change.",
        )
    return None


def check_is_deleted_cleanup(table: ReplacingTable) -> Trap | None:
    """Check whether tombstone rows (is_deleted = 1) can ever be physically removed.

    Verified against a real server: `is_deleted` rows are correctly hidden by
    FINAL and by a plain `OPTIMIZE ... FINAL`, but neither physically deletes
    them. Physical removal needs the table setting
    `allow_experimental_replacing_merge_with_cleanup = 1` *and* the explicit
    `OPTIMIZE ... FINAL CLEANUP` statement -- in our test, the setting plus
    `clean_deleted_rows = 'Always'` was not enough without the CLEANUP keyword.
    """
    if table.is_deleted_column is None:
        return None
    if not table.cleanup_enabled:
        return Trap(
            id="is-deleted-not-cleaned-up",
            table=table.qualified_name,
            severity="warning",
            message=(
                f"{table.qualified_name} uses is_deleted ({table.is_deleted_column}) but "
                "allow_experimental_replacing_merge_with_cleanup is not set. Tombstoned "
                "rows are hidden from FINAL reads but stay on disk forever -- storage "
                "grows without bound, and OPTIMIZE ... FINAL alone will not reclaim it."
            ),
            fix=(
                "ALTER TABLE ... MODIFY SETTING "
                "allow_experimental_replacing_merge_with_cleanup = 1, then run "
                "OPTIMIZE TABLE ... FINAL CLEANUP."
            ),
            cost=(
                "Marked experimental in ClickHouse as of this writing; test it before "
                "relying on it in production."
            ),
        )
    return Trap(
        id="is-deleted-cleanup-keyword-required",
        table=table.qualified_name,
        severity="info",
        message=(
            f"{table.qualified_name} has allow_experimental_replacing_merge_with_cleanup "
            "enabled, but that setting alone does not remove tombstones -- confirmed "
            "even with clean_deleted_rows = 'Always'. Only OPTIMIZE ... FINAL CLEANUP, "
            "run explicitly, physically removes is_deleted rows."
        ),
        fix="Run OPTIMIZE TABLE ... FINAL CLEANUP; a plain OPTIMIZE ... FINAL is not enough.",
        cost="Same cost profile as any OPTIMIZE ... FINAL: a full merge of the table.",
    )


def check_optimize_final_illusion(duplication: DuplicationReport) -> Trap | None:
    """Flag the false sense of security a clean count gives right now.

    Verified against a real server: after `OPTIMIZE TABLE ... FINAL` collapses
    a table to zero duplicates, a single subsequent INSERT of an existing key
    immediately reintroduces a duplicate visible to a plain SELECT. A clean
    count is a snapshot, not an invariant.
    """
    if not duplication.is_deduplicated:
        return None
    return Trap(
        id="optimize-final-is-not-a-standing-guarantee",
        table=duplication.table,
        severity="info",
        message=(
            f"{duplication.table} shows zero live duplication right now "
            f"({duplication.physical_rows} rows == {duplication.distinct_keys} distinct "
            "keys), likely because of a recent merge or OPTIMIZE. This is a snapshot: "
            "the next INSERT of an existing key immediately reintroduces a duplicate "
            "visible to a plain SELECT (verified)."
        ),
        fix=(
            "Don't treat a clean count as permanent. Use FINAL or query-time dedup on "
            "any code path that reads this table, or re-run this checker on a schedule."
        ),
        cost="None to check; the discipline is not trusting a point-in-time count.",
    )
