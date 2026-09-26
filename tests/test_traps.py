from __future__ import annotations

from collections.abc import Callable

from clickhouse_dedup_check.client import HttpClient
from clickhouse_dedup_check.duplication import measure_duplication
from clickhouse_dedup_check.schema import discover_replacing_tables
from clickhouse_dedup_check.traps import (
    check_final_usage,
    check_is_deleted_cleanup,
    check_optimize_final_illusion,
    check_version_ties,
)


def _table(clickhouse_client: HttpClient, table_name: str):
    tables = discover_replacing_tables(clickhouse_client, database="default")
    return next(t for t in tables if t.name == table_name)


def test_final_not_used_is_flagged(
    clickhouse_client: HttpClient, table_name: str, flush_logs: Callable[[], None]
) -> None:
    clickhouse_client.query(
        f"CREATE TABLE {table_name} (id UInt64, ver UInt64) "
        f"ENGINE = ReplacingMergeTree(ver) ORDER BY id"
    )
    clickhouse_client.query(f"SELECT count() FROM {table_name}")
    flush_logs()

    table = _table(clickhouse_client, table_name)
    trap = check_final_usage(clickhouse_client, table, lookback="1 HOUR")

    assert trap is not None
    assert trap.id == "final-not-used"
    assert trap.severity == "critical"


def test_final_usage_is_not_flagged_when_final_was_used(
    clickhouse_client: HttpClient, table_name: str, flush_logs: Callable[[], None]
) -> None:
    clickhouse_client.query(
        f"CREATE TABLE {table_name} (id UInt64, ver UInt64) "
        f"ENGINE = ReplacingMergeTree(ver) ORDER BY id"
    )
    clickhouse_client.query(f"SELECT count() FROM {table_name} FINAL")
    flush_logs()

    table = _table(clickhouse_client, table_name)
    trap = check_final_usage(clickhouse_client, table, lookback="1 HOUR")

    assert trap is None


def test_no_query_history_returns_no_trap(
    clickhouse_client: HttpClient, table_name: str
) -> None:
    clickhouse_client.query(
        f"CREATE TABLE {table_name} (id UInt64, ver UInt64) "
        f"ENGINE = ReplacingMergeTree(ver) ORDER BY id"
    )
    table = _table(clickhouse_client, table_name)
    # No SELECTs have run against this brand-new table yet.
    trap = check_final_usage(clickhouse_client, table, lookback="1 HOUR")
    assert trap is None


def test_missing_version_column_is_flagged(
    clickhouse_client: HttpClient, table_name: str
) -> None:
    clickhouse_client.query(
        f"CREATE TABLE {table_name} (id UInt64) ENGINE = ReplacingMergeTree ORDER BY id"
    )
    table = _table(clickhouse_client, table_name)
    trap = check_version_ties(clickhouse_client, table)
    assert trap is not None
    assert trap.id == "no-version-column"
    assert trap.severity == "warning"


def test_real_version_ties_are_detected(
    clickhouse_client: HttpClient, table_name: str
) -> None:
    clickhouse_client.query(
        f"CREATE TABLE {table_name} (id UInt64, payload String, ver UInt64) "
        f"ENGINE = ReplacingMergeTree(ver) ORDER BY id"
    )
    clickhouse_client.query(f"INSERT INTO {table_name} VALUES (1, 'alpha', 5)")
    clickhouse_client.query(f"INSERT INTO {table_name} VALUES (1, 'beta', 5)")

    table = _table(clickhouse_client, table_name)
    trap = check_version_ties(clickhouse_client, table)

    assert trap is not None
    assert trap.id == "version-ties"
    assert trap.severity == "critical"


def test_no_ties_when_versions_differ(clickhouse_client: HttpClient, table_name: str) -> None:
    clickhouse_client.query(
        f"CREATE TABLE {table_name} (id UInt64, payload String, ver UInt64) "
        f"ENGINE = ReplacingMergeTree(ver) ORDER BY id"
    )
    clickhouse_client.query(f"INSERT INTO {table_name} VALUES (1, 'alpha', 5)")
    clickhouse_client.query(f"INSERT INTO {table_name} VALUES (1, 'beta', 6)")

    table = _table(clickhouse_client, table_name)
    trap = check_version_ties(clickhouse_client, table)
    assert trap is None


def test_is_deleted_without_cleanup_setting_is_flagged(
    clickhouse_client: HttpClient, table_name: str
) -> None:
    clickhouse_client.query(
        f"""
        CREATE TABLE {table_name} (id UInt64, ver UInt64, is_deleted UInt8)
        ENGINE = ReplacingMergeTree(ver, is_deleted)
        ORDER BY id
        """
    )
    table = _table(clickhouse_client, table_name)
    trap = check_is_deleted_cleanup(table)
    assert trap is not None
    assert trap.id == "is-deleted-not-cleaned-up"
    assert trap.severity == "warning"


def test_is_deleted_with_cleanup_setting_still_needs_the_keyword(
    clickhouse_client: HttpClient, table_name: str
) -> None:
    clickhouse_client.query(
        f"""
        CREATE TABLE {table_name} (id UInt64, ver UInt64, is_deleted UInt8)
        ENGINE = ReplacingMergeTree(ver, is_deleted)
        ORDER BY id
        SETTINGS allow_experimental_replacing_merge_with_cleanup = 1
        """
    )
    table = _table(clickhouse_client, table_name)
    trap = check_is_deleted_cleanup(table)
    assert trap is not None
    assert trap.id == "is-deleted-cleanup-keyword-required"
    assert trap.severity == "info"


def test_table_without_is_deleted_has_no_cleanup_trap(
    clickhouse_client: HttpClient, table_name: str
) -> None:
    clickhouse_client.query(
        f"CREATE TABLE {table_name} (id UInt64, ver UInt64) "
        f"ENGINE = ReplacingMergeTree(ver) ORDER BY id"
    )
    table = _table(clickhouse_client, table_name)
    assert check_is_deleted_cleanup(table) is None


def test_optimize_final_illusion_flagged_only_when_currently_deduplicated(
    clickhouse_client: HttpClient, table_name: str
) -> None:
    clickhouse_client.query(
        f"CREATE TABLE {table_name} (id UInt64, ver UInt64) "
        f"ENGINE = ReplacingMergeTree(ver) ORDER BY id"
    )
    clickhouse_client.query(f"INSERT INTO {table_name} VALUES (1, 1)")
    table = _table(clickhouse_client, table_name)
    clean_report = measure_duplication(clickhouse_client, table)
    trap = check_optimize_final_illusion(clean_report)
    assert trap is not None
    assert trap.id == "optimize-final-is-not-a-standing-guarantee"
    assert trap.severity == "info"

    clickhouse_client.query(f"INSERT INTO {table_name} VALUES (1, 2)")
    table = _table(clickhouse_client, table_name)
    dirty_report = measure_duplication(clickhouse_client, table)
    assert check_optimize_final_illusion(dirty_report) is None
