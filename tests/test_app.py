"""
Offline tests: SQL safety, the dashboard queries, and the AI flow with a fake Claude client.

Run from the project folder:
    python -m unittest discover tests
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY") or "test-key"  # never used: the client is fake

import seed_db  # noqa: E402
from app import create_app  # noqa: E402
from dashboard import QUERIES, load_dashboard  # noqa: E402
from safe_sql import UnsafeQuery, run_query  # noqa: E402

TMP = Path(tempfile.mkdtemp())
DB = TMP / "shop.db"
seed_db.build(DB)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def count(table):
    with sqlite3.connect(DB) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class FakeClaude:
    """Returns the queued answers in order and records every request."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return NS(
            stop_reason="end_turn",
            content=[NS(type="text", text=json.dumps(self.answers.pop(0)))],
            usage=NS(input_tokens=50, output_tokens=20, cache_read_input_tokens=1000, cache_creation_input_tokens=0),
        )


def plan(sql, chart=None, can_answer=True):
    return {"can_answer": can_answer, "sql": sql, "explanation": "Counts orders.",
            "chart": chart or {"type": "none", "x": "", "y": ""}}


class SafetyTest(unittest.TestCase):
    BLOCKED = [
        "DROP TABLE orders",
        "DELETE FROM orders",
        "UPDATE orders SET status = 'cancelled'",
        "INSERT INTO categories VALUES (99, 'x')",
        "SELECT 1; DROP TABLE orders",
        "WITH x AS (SELECT 1) DELETE FROM orders",
        "PRAGMA table_info(orders)",
        "ATTACH DATABASE 'other.db' AS other",
        "CREATE TABLE hack (id INTEGER)",
        "",
    ]

    def test_dangerous_queries_are_blocked(self):
        orders_before = count("orders")
        for sql in self.BLOCKED:
            with self.subTest(sql=sql), self.assertRaises(UnsafeQuery):
                run_query(DB, sql)
        self.assertEqual(count("orders"), orders_before)
        self.assertEqual(count("categories"), 7)

    def test_select_and_cte_work(self):
        result = run_query(DB, "WITH t AS (SELECT status, COUNT(*) AS n FROM orders GROUP BY status) SELECT * FROM t ORDER BY n DESC")
        self.assertEqual(result.columns, ["status", "n"])
        self.assertEqual(result.rows[0][0], "delivered")

    def test_row_limit(self):
        result = run_query(DB, "SELECT id FROM order_items", max_rows=10)
        self.assertEqual(len(result.rows), 10)
        self.assertTrue(result.truncated)

    def test_slow_query_is_stopped(self):
        slow = "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT COUNT(*) FROM n"
        with self.assertRaises(UnsafeQuery) as ctx:
            run_query(DB, slow, timeout_s=0.3)
        self.assertIn("stopped", str(ctx.exception))


class DashboardTest(unittest.TestCase):
    def test_all_queries_run(self):
        data = load_dashboard(DB)
        self.assertEqual(set(data), set(QUERIES))
        self.assertEqual(len(data["top_products"]["rows"]), 5)
        self.assertEqual(len(data["categories"]["rows"]), 7)

    def test_monthly_growth_uses_previous_month(self):
        rows = load_dashboard(DB)["monthly"]["rows"]
        self.assertIsNone(rows[0][2])  # the first month has no previous month
        expected = round(100 * (rows[1][1] - rows[0][1]) / rows[0][1], 1)
        self.assertEqual(rows[1][2], expected)


class AskTest(unittest.TestCase):
    def make_client(self, answers):
        self.fake = FakeClaude(answers)
        app = create_app(DB, ai_client_factory=lambda: self.fake)
        app.testing = True
        return app.test_client()

    def test_question_to_answer(self):
        client = self.make_client([
            plan("SELECT status, COUNT(*) AS orders FROM orders GROUP BY status ORDER BY orders DESC",
                 {"type": "bar", "x": "status", "y": "orders"}),
            {"answer": "Most orders were delivered."},
        ])
        data = client.post("/api/ask", json={"question": "Orders by status?"}).get_json()
        self.assertEqual(data["answer"], "Most orders were delivered.")
        self.assertEqual(data["columns"], ["status", "orders"])
        self.assertEqual(data["chart"]["type"], "bar")
        self.assertEqual(data["attempts"], 1)
        # The schema is in a cached system block, so follow-up questions are cheaper.
        system = self.fake.requests[0]["system"][0]
        self.assertEqual(system["cache_control"], {"type": "ephemeral"})
        self.assertIn("CREATE TABLE orders", system["text"])

    def test_ai_fixes_its_own_error(self):
        client = self.make_client([
            plan("SELECT COUNT(*) AS n FROM orderz"),       # wrong table name
            plan("SELECT COUNT(*) AS n FROM orders"),       # fixed
            {"answer": "There are 3,140 orders."},
        ])
        data = client.post("/api/ask", json={"question": "How many orders?"}).get_json()
        self.assertEqual(data["attempts"], 2)
        retry_message = self.fake.requests[1]["messages"][-1]["content"]
        self.assertIn("no such table", retry_message)

    def test_ai_cannot_write(self):
        client = self.make_client([plan("DELETE FROM orders")] * 3)
        response = client.post("/api/ask", json={"question": "Delete everything"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(count("orders"), 3140)

    def test_unanswerable_question(self):
        client = self.make_client([plan("", can_answer=False) | {"explanation": "There is no weather data."}])
        response = client.post("/api/ask", json={"question": "What was the weather?"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("weather", response.get_json()["error"])

    def test_bad_chart_columns_are_dropped(self):
        client = self.make_client([
            plan("SELECT COUNT(*) AS n FROM orders", {"type": "bar", "x": "missing", "y": "n"}),
            {"answer": "3,140."},
        ])
        self.assertEqual(client.post("/api/ask", json={"question": "Count"}).get_json()["chart"]["type"], "none")

    def test_edited_sql_endpoint_is_read_only(self):
        client = self.make_client([])
        self.assertEqual(client.post("/api/query", json={"sql": "DROP TABLE orders"}).status_code, 400)
        ok = client.post("/api/query", json={"sql": "SELECT COUNT(*) AS n FROM customers"}).get_json()
        self.assertEqual(ok["rows"], [[800]])


if __name__ == "__main__":
    unittest.main()
