from __future__ import annotations

import json
import os

from clickhouse_dedup_check.cli import main
from clickhouse_dedup_check.client import HttpClient
from clickhouse_dedup_check.report import EXIT_CRITICAL, EXIT_OK, run_check


def test_run_check_flags_a_deliberately_bad_table(
    clickhouse_client: HttpClient, table_name: str
) -> None:
    clickhouse_client.query(
        f"CREATE TABLE {table_name} (id UInt64, payload String) "
        f"ENGINE = ReplacingMergeTree ORDER BY id"
    )
    clickhouse_client.query(f"INSERT INTO {table_name} VALUES (1, 'a')")
    clickhouse_client.query(f"INSERT INTO {table_name} VALUES (1, 'b')")

    report = run_check(clickhouse_client, database="default")
    table_reports = [tr for tr in report.tables if tr.table.name == table_name]
    assert len(table_reports) == 1

    tr = table_reports[0]
    assert tr.duplication is not None
    assert tr.duplication.physical_rows == 2
    assert tr.duplication.distinct_keys == 1

    trap_ids = {t.id for t in tr.traps}
    assert "no-version-column" in trap_ids
    assert report.exit_code >= EXIT_OK


def test_report_to_dict_is_json_serializable(
    clickhouse_client: HttpClient, table_name: str
) -> None:
    clickhouse_client.query(
        f"CREATE TABLE {table_name} (id UInt64, ver UInt64) "
        f"ENGINE = ReplacingMergeTree(ver) ORDER BY id"
    )
    clickhouse_client.query(f"INSERT INTO {table_name} SELECT number, 1 FROM numbers(10)")

    report = run_check(clickhouse_client, database="default")
    payload = json.loads(report.to_json())
    assert "tables" in payload
    assert "exit_code" in payload


def test_cli_json_mode_exits_nonzero_on_critical_trap(
    clickhouse_client: HttpClient, table_name: str, flush_logs
) -> None:
    clickhouse_client.query(
        f"CREATE TABLE {table_name} (id UInt64, payload String, ver UInt64) "
        f"ENGINE = ReplacingMergeTree(ver) ORDER BY id"
    )
    clickhouse_client.query(f"INSERT INTO {table_name} VALUES (1, 'a', 1)")
    clickhouse_client.query(f"INSERT INTO {table_name} VALUES (1, 'a', 1)")  # real version tie
    clickhouse_client.query(f"SELECT * FROM {table_name}")  # no FINAL used
    flush_logs()

    argv = [
        "--host", os.environ.get("CLICKHOUSE_HOST", "localhost"),
        "--port", os.environ.get("CLICKHOUSE_PORT", "18124"),
        "--user", os.environ.get("CLICKHOUSE_USER", "default"),
        "--password", os.environ.get("CLICKHOUSE_PASSWORD", "test"),
        "--database", "default",
        "--lookback", "1 HOUR",
        "--json",
    ]
    exit_code = main(argv)
    assert exit_code == EXIT_CRITICAL


def test_cli_text_mode_runs_without_error(clickhouse_client: HttpClient, table_name: str) -> None:
    clickhouse_client.query(
        f"CREATE TABLE {table_name} (id UInt64, ver UInt64) "
        f"ENGINE = ReplacingMergeTree(ver) ORDER BY id"
    )
    argv = [
        "--host", os.environ.get("CLICKHOUSE_HOST", "localhost"),
        "--port", os.environ.get("CLICKHOUSE_PORT", "18124"),
        "--user", os.environ.get("CLICKHOUSE_USER", "default"),
        "--password", os.environ.get("CLICKHOUSE_PASSWORD", "test"),
        "--database", "default",
    ]
    exit_code = main(argv)
    assert exit_code in (EXIT_OK, 1, EXIT_CRITICAL)
