from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

from clickhouse_dedup_check.client import HttpClient


def _make_client() -> HttpClient:
    return HttpClient(
        host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        port=int(os.environ.get("CLICKHOUSE_PORT", "18124")),
        user=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", "test"),
        database=os.environ.get("CLICKHOUSE_DATABASE", "default"),
    )


@pytest.fixture(scope="session")
def clickhouse_client() -> HttpClient:
    client = _make_client()
    try:
        client.query("SELECT 1")
    except Exception as error:  # noqa: BLE001 - environment probe, any failure means skip
        pytest.skip(f"ClickHouse is not reachable for integration tests: {error}")
    return client


@pytest.fixture
def table_name(clickhouse_client: HttpClient) -> Iterator[str]:
    """A unique table name per test, dropped afterward."""
    name = f"dedup_check_test_{uuid.uuid4().hex[:12]}"
    yield name
    clickhouse_client.query(f"DROP TABLE IF EXISTS {name}")


@pytest.fixture
def flush_logs(clickhouse_client: HttpClient):
    """Call after running queries that need to show up in system.query_log."""

    def _flush() -> None:
        clickhouse_client.query("SYSTEM FLUSH LOGS")

    return _flush
