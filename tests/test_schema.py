from __future__ import annotations

from clickhouse_dedup_check.client import HttpClient
from clickhouse_dedup_check.schema import discover_replacing_tables


def test_discovers_ver_and_is_deleted_columns(clickhouse_client: HttpClient, table_name: str) -> None:
    clickhouse_client.query(
        f"""
        CREATE TABLE {table_name} (
            id UInt64,
            payload String,
            ver UInt64,
            is_deleted UInt8
        ) ENGINE = ReplacingMergeTree(ver, is_deleted)
        ORDER BY id
        """
    )

    tables = discover_replacing_tables(clickhouse_client, database="default")
    found = [t for t in tables if t.name == table_name]
    assert len(found) == 1

    table = found[0]
    assert table.qualified_name == f"default.{table_name}"
    assert table.ver_column == "ver"
    assert table.is_deleted_column == "is_deleted"
    assert table.sorting_key_columns == ["id"]
    assert table.cleanup_enabled is False


def test_table_with_no_version_column_reports_none(
    clickhouse_client: HttpClient, table_name: str
) -> None:
    clickhouse_client.query(
        f"CREATE TABLE {table_name} (id UInt64, payload String) "
        "ENGINE = ReplacingMergeTree ORDER BY id"
    )

    tables = discover_replacing_tables(clickhouse_client, database="default")
    found = [t for t in tables if t.name == table_name]
    assert len(found) == 1
    assert found[0].ver_column is None
    assert found[0].is_deleted_column is None


def test_cleanup_enabled_is_parsed_from_engine_full(
    clickhouse_client: HttpClient, table_name: str
) -> None:
    clickhouse_client.query(
        f"""
        CREATE TABLE {table_name} (
            id UInt64,
            ver UInt64,
            is_deleted UInt8
        ) ENGINE = ReplacingMergeTree(ver, is_deleted)
        ORDER BY id
        SETTINGS allow_experimental_replacing_merge_with_cleanup = 1
        """
    )

    tables = discover_replacing_tables(clickhouse_client, database="default")
    found = [t for t in tables if t.name == table_name]
    assert len(found) == 1
    assert found[0].cleanup_enabled is True


def test_ignores_non_replacing_engines(clickhouse_client: HttpClient, table_name: str) -> None:
    clickhouse_client.query(
        f"CREATE TABLE {table_name} (id UInt64) ENGINE = MergeTree ORDER BY id"
    )
    tables = discover_replacing_tables(clickhouse_client, database="default")
    assert all(t.name != table_name for t in tables)
