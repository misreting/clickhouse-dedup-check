"""Reproduce the dashboard-double-counting bug this package exists to catch.

Builds a ReplacingMergeTree table shaped like a revenue-events table: 250,000
distinct event ids, each inserted 40 times (a realistic replay/backfill
pattern), held fragmented across 40 parts with `SYSTEM STOP MERGES` so the
measurement reflects a table where ingestion is ahead of background merging --
exactly the state a busy production table can be in. Then it runs the report
and prints what a naive dashboard query would see next to the truth.

Usage:
    python examples/dashboard_demo.py [--host H] [--port P] [--user U] [--password W]

Needs a real ClickHouse reachable at the given address (see README for how
this package's own tests start one via Docker).
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid

sys.path.insert(0, "src")

from clickhouse_dedup_check.client import HttpClient  # noqa: E402
from clickhouse_dedup_check.report import run_check  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--user", default="default")
    parser.add_argument("--password", default="")
    args = parser.parse_args()

    client = HttpClient(
        host=args.host, port=args.port, user=args.user, password=args.password
    )

    # Unique per run so a previous run's query_log history (which may include
    # a FINAL-using query from this very script) can't mask the finding below.
    table = f"dashboard_demo_revenue_events_{uuid.uuid4().hex[:8]}"
    client.query(f"DROP TABLE IF EXISTS {table}")
    client.query(
        f"""
        CREATE TABLE {table} (
            event_id UInt64,
            amount_cents Int64,
            ver UInt64
        ) ENGINE = ReplacingMergeTree(ver)
        ORDER BY event_id
        """
    )
    client.query(f"SYSTEM STOP MERGES {table}")

    print("Inserting 40 batches of 250,000 rows each (10,000,000 rows total, "
          "250,000 distinct event ids)...")
    start = time.perf_counter()
    for ver in range(1, 41):
        client.query(
            f"INSERT INTO {table} SELECT number, 500, {ver} FROM numbers(250000)"
        )
    elapsed = time.perf_counter() - start
    print(f"Insert done in {elapsed:.1f}s.\n")

    # The "naive dashboard" query -- what most application code actually runs.
    plain = client.query(f"SELECT count(), sum(amount_cents) FROM {table}")
    # The true total, computed with argMax(..., ver) instead of the FINAL
    # keyword -- this is the query-time-dedup fix this package recommends,
    # and it deliberately does not use FINAL so the demo's own instrumentation
    # doesn't mask the "nobody used FINAL" finding below.
    correct = client.query(
        f"""
        SELECT count(), sum(amt) FROM (
            SELECT event_id, argMax(amount_cents, ver) AS amt
            FROM {table}
            GROUP BY event_id
        )
        """
    )

    plain_rows, plain_sum = plain.result_rows[0]
    correct_rows, correct_sum = correct.result_rows[0]

    print("What a naive dashboard query sees (plain SELECT, no FINAL):")
    print(f"  rows = {plain_rows:,}   revenue = ${plain_sum / 100:,.2f}")
    print()
    print("What is actually true (argMax(amount_cents, ver) per event_id):")
    print(f"  rows = {correct_rows:,}   revenue = ${correct_sum / 100:,.2f}")
    print()
    print(f"Overcount factor: {plain_sum / correct_sum:.1f}x\n")

    client.query("SYSTEM FLUSH LOGS")
    print("Now running clickhouse-dedup-check against this table:")
    report = run_check(client, database="default", query_log_lookback="1 HOUR")
    demo = next(tr for tr in report.tables if tr.table.name == table)
    print(f"  physical_rows={demo.duplication.physical_rows} "
          f"distinct_keys={demo.duplication.distinct_keys} "
          f"duplication_ratio={demo.duplication.duplication_ratio:.1f}x")
    for trap in demo.traps:
        print(f"  [{trap.severity.upper()}] {trap.id}: {trap.message}")

    client.query(f"SYSTEM START MERGES {table}")
    client.query(f"DROP TABLE IF EXISTS {table}")


if __name__ == "__main__":
    main()
