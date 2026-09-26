"""A duck-typed ClickHouse client contract, plus a stdlib-only HTTP client.

Nothing in this package imports a ClickHouse driver. `Checker` (see `checker.py`)
accepts *any* object with a `.query(sql) -> result` method -- clickhouse-connect's
`Client`, a `clickhouse_driver.Client` wrapped as `client.query = client.execute`,
or the `HttpClient` below, which this package ships so the CLI works with zero
installed dependencies.

The adapter in `execute()` accepts two result shapes:

- an object exposing `.result_rows` (and optionally `.column_names`), matching
  clickhouse-connect's `QueryResult`, or
- a plain sequence of row tuples/lists, for clients whose `.query()` returns
  rows directly.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class QueryClient(Protocol):
    """The only shape this package requires: something that can run SQL."""

    def query(self, sql: str) -> Any: ...


def rows_of(result: Any) -> list[tuple[Any, ...]]:
    """Normalize a query result from any duck-typed client into row tuples."""
    rows = getattr(result, "result_rows", None)
    if rows is None:
        rows = result
    return [tuple(row) for row in rows]


def column_names_of(result: Any) -> list[str]:
    """Best-effort column names; empty if the client's result doesn't carry them."""
    names = getattr(result, "column_names", None)
    return [str(n) for n in names] if names is not None else []


def execute(client: QueryClient, sql: str) -> list[tuple[Any, ...]]:
    """Run `sql` through `client.query` and return normalized row tuples."""
    return rows_of(client.query(sql))


class ClickHouseError(RuntimeError):
    """Raised when the ClickHouse HTTP interface returns a non-2xx response."""


_NULLABLE_RE = re.compile(r"^Nullable\((.+)\)$")


def _coerce(value: Any, ch_type: str) -> Any:
    if value is None:
        return None
    match = _NULLABLE_RE.match(ch_type)
    if match:
        ch_type = match.group(1)
    if ch_type.startswith(("UInt", "Int")):
        return int(value)
    if ch_type.startswith("Float"):
        return float(value)
    return value


@dataclass
class HttpResult:
    """Query result shape returned by `HttpClient.query`."""

    result_rows: list[tuple[Any, ...]]
    column_names: list[str] = field(default_factory=list)


class HttpClient:
    """A minimal ClickHouse client over its native HTTP interface.

    Standard library only (`urllib`), so the CLI never requires a driver to be
    installed. This is one example of a duck-typed client -- `Checker` only
    needs `.query(sql)`; a clickhouse-connect `Client` works identically.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8123,
        user: str = "default",
        password: str = "",
        database: str = "default",
        secure: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._database = database
        self._scheme = "https" if secure else "http"
        self._timeout = timeout

    def query(self, sql: str) -> HttpResult:
        url = (
            f"{self._scheme}://{self._host}:{self._port}/"
            f"?database={self._database}&default_format=JSONCompact"
        )
        request = urllib.request.Request(url, data=sql.encode("utf-8"), method="POST")
        token = base64.b64encode(f"{self._user}:{self._password}".encode()).decode("ascii")
        request.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise ClickHouseError(f"ClickHouse query failed: {detail.strip()}") from error
        except urllib.error.URLError as error:
            raise ClickHouseError(f"Could not reach ClickHouse at {url}: {error}") from error

        stripped = body.strip()
        if not stripped:
            # DDL/DML statements (CREATE, INSERT, OPTIMIZE, SYSTEM ...) return an
            # empty body -- there is no result set to parse.
            return HttpResult(result_rows=[], column_names=[])

        payload = json.loads(stripped)
        meta = payload.get("meta", [])
        column_names = [m["name"] for m in meta]
        column_types = [m.get("type", "") for m in meta]
        result_rows = [
            tuple(_coerce(value, column_types[i]) for i, value in enumerate(row))
            for row in payload.get("data", [])
        ]
        return HttpResult(result_rows=result_rows, column_names=column_names)
