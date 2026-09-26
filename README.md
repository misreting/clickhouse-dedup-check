# clickhouse-dedup-check

Catch the `ReplacingMergeTree` bug before your dashboard does.

```
$ python examples/dashboard_demo.py --host localhost --port 18124 --user default --password test

What a naive dashboard query sees (plain SELECT, no FINAL):
  rows = 10,000,000   revenue = $50,000,000.00

What is actually true (argMax(amount_cents, ver) per event_id):
  rows = 250,000   revenue = $1,250,000.00

Overcount factor: 40.0x

Now running clickhouse-dedup-check against this table:
  physical_rows=10000000 distinct_keys=250000 duplication_ratio=40.0x
  [CRITICAL] final-not-used: 2 recent SELECT(s) against
  default.dashboard_demo_revenue_events_4f513be9 in the last 1 HOUR, none used
  FINAL. ReplacingMergeTree does not deduplicate on read -- every one of these
  queries can see live duplicate rows.
```

That's `examples/dashboard_demo.py` against a real ClickHouse 26.9.3.38, run
today. The table is 250,000 revenue events, each replayed 40 times (a normal
backfill/retry pattern) and held fragmented across 40 parts to represent a
table where ingestion is ahead of merging -- which is the ordinary state of a
busy production table. A plain `count()`/`sum()` over-counts revenue by 40x.
Nothing here is contrived: this is the literal behavior of the documented,
supported, widely-recommended engine.

## The gap

`ReplacingMergeTree` is sold as "a table that deduplicates by primary key." It
doesn't. It deduplicates **eventually**, during background merges that ClickHouse
schedules on its own timetable, and a plain `SELECT` before that merge happens
returns every duplicate row as a normal row. Teams find this when a dashboard
double-counts revenue, and the two obvious workarounds don't hold up:

- **"It'll merge soon enough."** In one reproduction, three duplicate rows in
  their own tiny parts sat un-merged for 3+ minutes of continuous polling with
  no sign of consolidating, and nothing in ClickHouse promises it ever will
  without `OPTIMIZE`. In another reproduction, a larger, more fragmented table
  merged down in under two seconds once merges were allowed to run. **There is
  no SLA either way** -- the same engine gave us "not in 3 minutes" and
  "under 2 seconds" depending on part shape, and nothing about the table tells
  you in advance which you'll get.
- **"We run `OPTIMIZE TABLE ... FINAL` on a schedule."** This does force a real
  merge and does produce a genuinely deduplicated table -- for one instant.
  Verified: immediately after `OPTIMIZE ... FINAL` collapsed a table to zero
  duplicates, a single new `INSERT` of an existing key was enough to make a
  plain `SELECT` return a duplicate again. `OPTIMIZE` is a point-in-time
  operation, not a standing guarantee, and a nightly cron job doesn't protect
  the queries that run during the day.

The correct fix -- adding `FINAL` to every read, or deduplicating at query
time with `argMax(col, ver)` -- is well known. What's not well known is what it
costs, and this package measures that too, along with the `ver`/`is_deleted`
edge cases that change the semantics in ways that are easy to get wrong.

## What was actually reproduced

Everything below was run against a real `clickhouse/clickhouse-server:latest`
(26.9.3.38) container, not written from memory. Reproduction steps are in
`examples/dashboard_demo.py` and the integration tests in `tests/`.

**Duplicates appear immediately, and may never merge on their own.**
Three separate single-row `INSERT`s of the same key produced `count() = 3`
immediately, before any merge. Polling every 20s for 3 minutes with no other
activity: still 3 rows, still 3 parts, no spontaneous merge. `SELECT ... FINAL`
correctly returned 1 row the whole time.

**`FINAL` is not free, and the cost scales with fragmentation, not just row count.**
On a 10,000,000-row / 40-part table (250,000 distinct keys, merges deliberately
held off with `SYSTEM STOP MERGES` to simulate ingestion outrunning merges):

| | plain `SELECT` | `SELECT ... FINAL` |
| --- | --- | --- |
| query time | 12 ms | 224 ms (**~19x**) |
| memory | 5.3 MiB | 262 MiB (**~49x**) |
| rows read | 10,000,000 | 10,983,040 |

(Re-running the same scenario a second time gave 6 ms / 169 ms -- ~28x instead
of ~19x. Query *time* is noisy on a laptop-run container; memory was
consistent both times, ~262 MiB. Either way the multiplier is large enough
that "just add FINAL everywhere" is not a free decision.)

This is why teams avoid blanket-adding `FINAL`: it's correct, but a 30x-100x
cost multiplier on a hot path is a real production concern, not a rounding error.

**`OPTIMIZE TABLE ... FINAL` guarantees a point-in-time result, not an invariant.**
After it ran, `count()` and `count() FINAL` were equal. One `INSERT` of an
existing key later, they diverged again. Anything that relies on a periodic
`OPTIMIZE` to keep a table "clean" is relying on a snapshot that expires on the
next write.

**`ver` picks the higher version regardless of insertion order -- and ties are
undefined, not safe.** A row inserted first with `ver = 99` beat a row
inserted after it with `ver = 1`, as documented. But two rows with the *same*
`ver` for the same key have no principled winner: in our reproduction the row
inserted later won, reproducibly, across three repeated trials -- but this is
observed behavior on one ClickHouse version, not a documented guarantee, and
should not be relied on. A `ver` column that can tie is a schema bug.

**`is_deleted` rows are hidden correctly, but do not physically disappear
without two extra steps.** `SELECT ... FINAL` correctly filtered out a tombstoned
row. `OPTIMIZE TABLE ... FINAL` (no `CLEANUP`) did not remove it -- a plain
`SELECT` after the optimize still returned the tombstone. Enabling it required:

1. `ALTER TABLE ... MODIFY SETTING allow_experimental_replacing_merge_with_cleanup = 1`
   (the setting is marked **experimental**; ClickHouse refuses `CLEANUP`
   without it: `Code: 344. Experimental merges with CLEANUP are not allowed`).
2. The explicit statement `OPTIMIZE TABLE ... FINAL CLEANUP`.

Setting `clean_deleted_rows = 'Always'` on the table *plus* the experimental
flag was **not enough on its own** -- a plain `OPTIMIZE ... FINAL` still left
the tombstone on disk. Only the explicit `CLEANUP` keyword removed it. Skip
either step and tombstones accumulate on disk forever.

## Install

```sh
pip install clickhouse-dedup-check
```

Python >= 3.11. Zero runtime dependencies -- standard library only.

## Use

### As a library

`run_check` is duck-typed: pass anything with a `.query(sql)` method,
including a `clickhouse-connect` `Client`. This package also ships
`HttpClient`, a stdlib-only client over ClickHouse's HTTP interface, so it
works without installing a driver.

```python
from clickhouse_dedup_check import HttpClient, run_check

client = HttpClient(host="localhost", port=8123, user="default", password="secret")
report = run_check(client, database="analytics")

for table_report in report.tables:
    print(table_report.table.qualified_name, table_report.duplication)
    for trap in table_report.traps:
        print(f"  [{trap.severity}] {trap.id}: {trap.message}")

raise SystemExit(report.exit_code)  # 0 clean, 1 warning, 2 critical
```

### As a CLI

```sh
clickhouse-dedup-check --host localhost --port 8123 --user default --password secret
clickhouse-dedup-check --database analytics --json
```

Real captured output, against a table of 50,000 events each inserted 5 times
(held fragmented, one plain `SELECT` run against it beforehand):

```
== default.cli_demo_1790453325 (ReplacingMergeTree) ==
  sorting key: event_id
  ver: ver  is_deleted: (none)  active parts: 5
  live duplication: 250000 physical rows vs 50000 distinct keys (200000 duplicate rows, 5.00x)
  [CRITICAL] final-not-used: 1 recent SELECT(s) against default.cli_demo_1790453325 in the last 1 HOUR, none used FINAL. ReplacingMergeTree does not deduplicate on read -- every one of these queries can see live duplicate rows.
    fix:  Add FINAL to reads that need a correct answer, or dedupe at query time with argMax(col, ver) grouped by the sorting key.
    cost: FINAL forces a merge-on-read across every part the query touches. Measured on a fragmented 10M-row / 40-part table: ~19x the query time and ~50x the memory of the equivalent plain SELECT (see README).
```

Exit codes: `0` nothing flagged, `1` a `warning`-level trap, `2` a `critical`
trap -- wire it into CI to fail a build when a new `ReplacingMergeTree` table
ships without `FINAL` anywhere in its read path.

## What it checks

| Check | What it measures | Severity |
| --- | --- | --- |
| Live duplication | `count()` vs `uniqExact(sorting key)` on the actual table, right now | (data, not a trap) |
| `FINAL` not used | Scans `system.query_log` for recent SELECTs against the table that never used `FINAL` | critical |
| No `ver` column | `ReplacingMergeTree()` with no version argument -- merge winner is insertion-order, not domain logic | warning |
| Real version ties | `GROUP BY (key, ver) HAVING count() > 1` -- actual ties in the actual data | critical |
| `is_deleted` without cleanup | Tombstones can never be physically removed with current settings | warning |
| `is_deleted` cleanup needs the keyword | Cleanup setting is on, but `OPTIMIZE ... FINAL CLEANUP` must still be run explicitly | info |
| Clean count is not permanent | Table shows zero duplication right now -- a reminder that this is a snapshot | info |

The `FINAL`-usage and duplication checks only read `system.tables` and
`system.query_log` (and, for duplication, the table itself) -- no locks, no
`OPTIMIZE`, safe to run against production. `system.query_log` audit is
empirical (what actually ran), not static analysis: a database connection
can't see your application's source code.

## Options

| Option (CLI) | Library equivalent | Default | What it does |
| --- | --- | --- | --- |
| `--database` | `run_check(..., database=...)` | all non-system databases | Limit the scan to one database |
| `--lookback` | `run_check(..., query_log_lookback=...)` | `1 DAY` | `query_log` window for the FINAL-usage audit |
| `--no-measure` | `run_check(..., measure=False)` | measure on | Skip the live row-count query (schema/trap audit only, cheaper) |
| `--json` | `report.to_json()` | text | Machine-readable output |

## What it does not do

- **It does not fix anything.** It flags; you add `FINAL`, fix the `ver`
  column, or run `OPTIMIZE ... FINAL CLEANUP` yourself.
- **The `FINAL`-usage check is a query-log audit, not static analysis.** It
  can only see queries that actually ran in the lookback window, sees the
  `FINAL` keyword anywhere in the query text (not scoped to a specific table
  in a multi-table query), and depends on `system.query_log` being enabled
  and retained long enough to matter.
- **It does not detect duplication in `Replicated*MergeTree` variants
  differently from plain ones** -- they're matched by an `engine LIKE
  '%ReplacingMergeTree%'` substring, which is correct for both, but this
  package does not account for cross-replica divergence.
- **Sorting keys are parsed as a plain comma-separated column list.** A
  functional sorting key (e.g. `ORDER BY (toDate(ts), id)`) is not specially
  parsed; pass the actual key expression through if you rely on this.
- **It does not touch experimental cleanup settings for you.** Given how new
  and clearly labeled "experimental" `allow_experimental_replacing_merge_with_cleanup`
  is, this package only reports its state -- enabling it is a deliberate,
  environment-specific decision.

## Develop

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q       # needs a real ClickHouse; see below
.venv/bin/python -m mypy src --strict
```

Tests are integration tests against a real ClickHouse server -- no mocks. Point
them at one with:

```sh
export CLICKHOUSE_HOST=localhost CLICKHOUSE_PORT=8123
export CLICKHOUSE_USER=default CLICKHOUSE_PASSWORD=test
```

or start one locally the same way this package's own verification did:

```sh
docker run -d --name clickhouse-dedup-check-dev \
  -p 8123:8123 -p 9000:9000 \
  -e CLICKHOUSE_USER=default -e CLICKHOUSE_PASSWORD=test \
  clickhouse/clickhouse-server:latest
```

If ClickHouse isn't reachable, the test session is skipped rather than faked.

## License

MIT
