"""Sandboxed query execution.

Two layers of defense:
  1. Guardrail middleware (guardrails.py) blocks destructive SQL before it gets here.
  2. The connection itself is opened with read_only=True, so even a query that
     slipped past the guardrails cannot write to the database.

Adds a wall-clock timeout via duckdb's interrupt() and captures the EXPLAIN plan.
"""
import threading
import time
from dataclasses import dataclass, field

import duckdb

from .config import settings


@dataclass
class ExecutionResult:
    ok: bool
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int = 0
    elapsed_ms: float = 0.0
    explain_plan: str = ""
    error: str = ""


class ReadOnlyExecutor:
    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or settings.db_path
        self._con = duckdb.connect(self._db_path, read_only=True)
        self._lock = threading.Lock()

    def execute(self, sql: str, timeout_s: float | None = None,
                max_rows: int | None = None) -> ExecutionResult:
        timeout_s = timeout_s or settings.guardrails.query_timeout_s
        max_rows = max_rows or settings.guardrails.default_row_limit

        with self._lock:
            # EXPLAIN first — validates the plan without touching data
            try:
                plan_rows = self._con.execute(f"EXPLAIN {sql}").fetchall()
                explain_plan = "\n".join(str(r[-1]) for r in plan_rows)
            except duckdb.Error as e:
                return ExecutionResult(ok=False, error=f"Planning failed: {e}")

            timer = threading.Timer(timeout_s, self._con.interrupt)
            timer.start()
            start = time.perf_counter()
            try:
                cursor = self._con.execute(sql)
                rows = cursor.fetchmany(max_rows)
                columns = [d[0] for d in cursor.description]
            except duckdb.InterruptException:
                return ExecutionResult(
                    ok=False, explain_plan=explain_plan,
                    error=f"Query cancelled after exceeding {timeout_s}s timeout",
                )
            except duckdb.Error as e:
                return ExecutionResult(ok=False, explain_plan=explain_plan,
                                       error=str(e))
            finally:
                timer.cancel()
            elapsed_ms = (time.perf_counter() - start) * 1000

        return ExecutionResult(
            ok=True,
            columns=columns,
            rows=[[_jsonable(v) for v in row] for row in rows],
            row_count=len(rows),
            elapsed_ms=round(elapsed_ms, 2),
            explain_plan=explain_plan,
        )

    def close(self) -> None:
        self._con.close()


def _jsonable(v):
    """Convert DuckDB values (Decimal, date, datetime) to JSON-safe types."""
    import datetime
    import decimal

    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    return v
