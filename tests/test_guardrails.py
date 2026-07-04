"""Offline unit tests for the guardrail middleware and executor sandbox."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import guardrails
from app.config import settings


DANGEROUS = [
    "DROP TABLE customers",
    "DELETE FROM orders",
    "UPDATE products SET price = 0",
    "INSERT INTO customers VALUES (1,'a','b','c','d','2025-01-01')",
    "CREATE TABLE t AS SELECT 1",
    "ALTER TABLE customers ADD COLUMN hacked INT",
    "TRUNCATE customers",
    "SELECT 1; DROP TABLE customers",
    "SELECT * FROM read_csv('secrets.csv')",
    "SELECT * FROM read_parquet('x.parquet')",
]

# Adversarial patterns: smuggling, obfuscation, hidden writes, admin/exfil, info leak
ADVERSARIAL = [
    # multi-statement smuggling (incl. trailing-comment variants)
    "SELECT * FROM customers WHERE name = 'x'; DELETE FROM orders; --",
    "SELECT a FROM t UNION ALL SELECT b FROM u; DROP TABLE customers",
    # comment-inside-keyword and case obfuscation
    "DROP/*x*/TABLE customers",
    "DrOp TaBlE customers",
    # write hidden inside a CTE
    "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x",
    # admin / config / extension loading
    "ATTACH 'evil.db' AS evil",
    "PRAGMA database_list",
    "SET memory_limit='1TB'",
    "INSTALL httpfs",
    "LOAD httpfs",
    # exfiltration
    "COPY customers TO 'exfil.csv'",
    # information disclosure (config, and — critically — stored secrets)
    "SELECT * FROM duckdb_secrets()",
    "SELECT * FROM duckdb_settings()",
    "SELECT getenv('PATH')",
]


@pytest.mark.parametrize("sql", DANGEROUS)
def test_dangerous_sql_blocked(sql):
    result = guardrails.check(sql)
    assert not result.allowed, f"should have blocked: {sql}"
    assert result.violations


@pytest.mark.parametrize("sql", ADVERSARIAL)
def test_adversarial_sql_blocked(sql):
    result = guardrails.check(sql)
    assert not result.allowed, f"adversarial input slipped through: {sql}"
    assert result.violations


def test_comment_smuggled_drop_is_inert_not_executed():
    """A DROP after a comment marker is parsed as an inert comment, not a second
    statement — allowed, but the DROP is a comment node and never a statement."""
    import sqlglot
    from sqlglot import exp

    result = guardrails.check("SELECT * FROM customers -- ; DROP TABLE customers")
    assert result.allowed
    # Re-parse the emitted SQL: exactly one statement, a SELECT, with no DROP node.
    statements = sqlglot.parse(result.sql, read="duckdb")
    assert len(statements) == 1
    assert isinstance(statements[0], exp.Select)
    assert statements[0].find(exp.Drop) is None  # DROP survives only as a comment


def test_plain_select_allowed():
    result = guardrails.check("SELECT * FROM customers WHERE country = 'India'")
    assert result.allowed


def test_limit_injected():
    result = guardrails.check("SELECT * FROM orders")
    assert result.allowed
    assert f"LIMIT {settings.guardrails.default_row_limit}" in result.sql
    assert any("injected" in n for n in result.notes)


def test_existing_small_limit_kept():
    result = guardrails.check("SELECT * FROM orders LIMIT 5")
    assert result.allowed
    assert "LIMIT 5" in result.sql
    assert not result.notes


def test_oversized_limit_clamped():
    result = guardrails.check("SELECT * FROM orders LIMIT 999999")
    assert result.allowed
    assert f"LIMIT {settings.guardrails.default_row_limit}" in result.sql


def test_cte_allowed():
    sql = ("WITH r AS (SELECT order_id, SUM(quantity) q FROM order_items GROUP BY order_id) "
           "SELECT AVG(q) FROM r")
    result = guardrails.check(sql)
    assert result.allowed, result.violations


def test_union_allowed():
    result = guardrails.check("SELECT name FROM customers UNION SELECT name FROM products")
    assert result.allowed, result.violations


def test_depth_limit():
    sql = "SELECT * FROM (SELECT * FROM (SELECT * FROM (SELECT * FROM customers) a) b) c"
    result = guardrails.check(sql)
    assert not result.allowed
    assert any("depth" in v.lower() for v in result.violations)


def test_depth_three_allowed():
    sql = "SELECT * FROM (SELECT * FROM (SELECT * FROM customers) a) b"
    result = guardrails.check(sql)
    assert result.allowed, result.violations


def test_garbage_rejected():
    result = guardrails.check("this is not sql at all ;;;")
    assert not result.allowed


@pytest.fixture(scope="module")
def executor():
    if not os.path.exists(settings.db_path):
        pytest.skip("warehouse.duckdb not seeded")
    from app.executor import ReadOnlyExecutor
    ex = ReadOnlyExecutor()
    yield ex
    ex.close()


def test_executor_runs_select(executor):
    r = executor.execute("SELECT COUNT(*) AS n FROM customers")
    assert r.ok
    assert r.columns == ["n"]
    assert r.rows[0][0] == 500
    assert r.explain_plan


def test_executor_readonly_blocks_writes(executor):
    """Layer-2 defense: even bypassing guardrails, the connection is read-only."""
    r = executor.execute("DELETE FROM orders")
    assert not r.ok
    assert "read-only" in r.error.lower() or "planning failed" in r.error.lower()
