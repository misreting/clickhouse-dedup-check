from __future__ import annotations

from clickhouse_dedup_check.client import HttpClient
from clickhouse_dedup_check.duplication import measure_duplication
from clickhouse_dedup_check.schema import discover_replacing_tables


def _table(clickhouse_client: HttpClient, table_name: str):
    tables = discover_replacing_tables(clickhouse_client, database="default")
    return next(t for t in tables if t.name == table_name)


def test_duplicate_inserts_are_visible_before_any_merge(
    clickhouse_client: HttpClient, table_name: str
) -> None:
    clickhouse_client.query(
        f"""
        CREATE TABLE {table_name} (id UInt64, payload String, ver UInt64)
        ENGINE = ReplacingMergeTree(ver)
        ORDER BY id
        """
    )
    # Three separate inserts of the same key -> three separate parts, no merge yet.
    clickhouse_client.query(f"INSERT INTO {table_name} VALUES (1, 'v1', 1)")
    clickhouse_client.query(f"INSERT INTO {table_name} VALUES (1, 'v2', 2)")
    clickhouse_client.query(f"INSERT INTO {table_name} VALUES (1, 'v3', 3)")

    table = _table(clickhouse_client, table_name)
    report = measure_duplication(clickhouse_client, table)

    assert report.physical_rows == 3
    assert report.distinct_keys == 1
    assert report.duplicate_rows == 2
    assert report.duplication_ratio == 3.0
    assert report.is_deduplicated is False


def test_distinct_keys_report_as_deduplicated(
    clickhouse_client: HttpClient, table_name: str
) -> None:
    clickhouse_client.query(
        f"""
        CREATE TABLE {table_name} (id UInt64, payload String, ver UInt64)
        ENGINE = ReplacingMergeTree(ver)
        ORDER BY id
        """
    )
    clickhouse_client.query(
        f"INSERT INTO {table_name} SELECT number, 'x', 1 FROM numbers(1000)"
    )

    table = _table(clickhouse_client, table_name)
    report = measure_duplication(clickhouse_client, table)

    assert report.physical_rows == 1000
    assert report.distinct_keys == 1000
    assert report.duplicate_rows == 0
    assert report.is_deduplicated is True


def test_optimize_final_does_not_survive_a_later_insert(
    clickhouse_client: HttpClient, table_name: str
) -> None:
    clickhouse_client.query(
        f"""
        CREATE TABLE {table_name} (id UInt64, payload String, ver UInt64)
        ENGINE = ReplacingMergeTree(ver)
        ORDER BY id
        """
    )
    clickhouse_client.query(f"INSERT INTO {table_name} VALUES (1, 'v1', 1)")
    clickhouse_client.query(f"INSERT INTO {table_name} VALUES (1, 'v2', 2)")
    clickhouse_client.query(f"OPTIMIZE TABLE {table_name} FINAL")

    table = _table(clickhouse_client, table_name)
    after_optimize = measure_duplication(clickhouse_client, table)
    assert after_optimize.is_deduplicated is True

    # A single new insert of the same key reintroduces a live duplicate.
    clickhouse_client.query(f"INSERT INTO {table_name} VALUES (1, 'v3', 3)")
    table = _table(clickhouse_client, table_name)
    after_new_insert = measure_duplication(clickhouse_client, table)
    assert after_new_insert.is_deduplicated is False
    assert after_new_insert.physical_rows == 2
    assert after_new_insert.distinct_keys == 1
