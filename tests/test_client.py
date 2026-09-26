from __future__ import annotations

import pytest

from clickhouse_dedup_check.client import ClickHouseError, HttpClient, execute


def test_query_returns_rows(clickhouse_client: HttpClient) -> None:
    rows = execute(clickhouse_client, "SELECT 1 AS a, 2 AS b")
    assert rows == [(1, 2)]


def test_uint64_columns_are_coerced_to_int(clickhouse_client: HttpClient) -> None:
    # ClickHouse's JSON formats quote 64-bit integers as strings to avoid
    # JS precision loss; the client must turn them back into real ints.
    rows = execute(clickhouse_client, "SELECT toUInt64(9223372036854775807) AS big")
    assert rows == [(9223372036854775807,)]
    assert isinstance(rows[0][0], int)


def test_bad_query_raises_clickhouse_error(clickhouse_client: HttpClient) -> None:
    with pytest.raises(ClickHouseError):
        clickhouse_client.query("SELECT * FROM this_table_does_not_exist_at_all")


def test_column_names_are_reported(clickhouse_client: HttpClient) -> None:
    result = clickhouse_client.query("SELECT 1 AS foo, 'x' AS bar")
    assert result.column_names == ["foo", "bar"]
