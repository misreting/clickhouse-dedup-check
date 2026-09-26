"""`clickhouse-dedup-check` command-line entry point."""

from __future__ import annotations

import argparse
import sys

from .client import ClickHouseError, HttpClient
from .report import Report, run_check


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clickhouse-dedup-check",
        description=(
            "Audit ReplacingMergeTree tables for the eventual-deduplication trap: "
            "find tables, measure live duplicate rows, and flag FINAL/ver/is_deleted "
            "mismatches."
        ),
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--user", default="default")
    parser.add_argument("--password", default="")
    parser.add_argument(
        "--database",
        default=None,
        help="Limit the scan to one database. Default: every non-system database.",
    )
    parser.add_argument("--secure", action="store_true", help="Use HTTPS.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    parser.add_argument(
        "--lookback",
        default="1 DAY",
        help="query_log lookback window for the FINAL-usage audit, e.g. '7 DAY'.",
    )
    parser.add_argument(
        "--no-measure",
        action="store_true",
        help="Skip the live row-count vs. distinct-key measurement (schema/trap audit only).",
    )
    return parser


def render_text(report: Report) -> str:
    if not report.tables:
        return "No ReplacingMergeTree tables found."

    lines: list[str] = []
    for table_report in report.tables:
        table = table_report.table
        lines.append(f"== {table.qualified_name} ({table.engine}) ==")
        lines.append(f"  sorting key: {table.sorting_key or '(none)'}")
        lines.append(
            f"  ver: {table.ver_column or '(none)'}  "
            f"is_deleted: {table.is_deleted_column or '(none)'}  "
            f"active parts: {table.active_parts}"
        )
        duplication = table_report.duplication
        if duplication is not None:
            lines.append(
                f"  live duplication: {duplication.physical_rows} physical rows vs "
                f"{duplication.distinct_keys} distinct keys "
                f"({duplication.duplicate_rows} duplicate rows, "
                f"{duplication.duplication_ratio:.2f}x)"
            )
        if not table_report.traps:
            lines.append("  no traps flagged")
        for trap in table_report.traps:
            lines.append(f"  [{trap.severity.upper()}] {trap.id}: {trap.message}")
            lines.append(f"    fix:  {trap.fix}")
            lines.append(f"    cost: {trap.cost}")
        lines.append("")
    lines.append(f"exit code: {report.exit_code}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    client = HttpClient(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database or "default",
        secure=args.secure,
    )

    try:
        report = run_check(
            client,
            database=args.database,
            measure=not args.no_measure,
            query_log_lookback=args.lookback,
        )
    except ClickHouseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(report.to_json())
    else:
        print(render_text(report))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
