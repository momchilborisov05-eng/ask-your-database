"""
Tests for running in production: the T-SQL guard and how Azure SQL queries are
run, sign-in, spending limits, the audit log, read-only enforcement, CSV export,
demo mode and the security headers. No API calls and no Azure needed, except
the last test, which runs only when ASKDB_TEST_MSSQL points at a database
loaded with `python seed_db.py --target ...`.
"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import seed_db  # noqa: E402
from app import Settings, create_app  # noqa: E402
from assistant import DatabaseAssistant  # noqa: E402
from dashboard import EXAMPLE_QUESTIONS, load_dashboard  # noqa: E402
from safe_sql import MssqlSource, SqliteSource, UnsafeQuery, check_select, open_source  # noqa: E402
from test_app import FakeClaude, plan  # noqa: E402

DB = Path(tempfile.mkdtemp()) / "shop.db"
seed_db.build(DB)

ALICE = {"x-ms-client-principal-id": "alice-id", "x-ms-client-principal-name": "alice@example.com"}
BOB = {"x-ms-client-principal-id": "bob-id", "x-ms-client-principal-name": "bob@example.com"}


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")  # never used: the client is fake

    def build(answers=(), **settings):
        fake = FakeClaude(answers)
        app = create_app(Settings(database_url=f"sqlite:///{DB}", store_url=f"sqlite:///{tmp_path / 'audit.db'}",
                                  **settings), ai_client_factory=lambda: fake)
        app.testing = True
        return app.test_client(), fake
    return build


def good_answer():
    return [plan("SELECT status, COUNT(*) AS orders FROM orders GROUP BY status"), {"answer": "Mostly delivered."}]


# ---------- the T-SQL guard ----------

@pytest.mark.parametrize("sql", [
    "INSERT INTO shop.orders VALUES (1)",
    "SELECT * INTO shop.copy FROM shop.orders",
    "SELECT 1; DROP TABLE shop.orders",
    "WITH x AS (SELECT 1 AS a) DELETE FROM shop.orders",
    "SELECT * FROM OPENROWSET('SQLNCLI', 'Server=evil;', 'SELECT 1')",
    "EXEC xp_cmdshell 'dir'",
    "SELECT 1 WAITFOR DELAY '00:01:00'",
    "SELECT @@VERSION",
    "SELECT * FROM shop.orders o WHERE 1 = 1 UPDATE shop.orders SET status = 'x'",
    "DECLARE @x int",
    "",
])
def test_tsql_guard_blocks_writes_and_side_effects(sql):
    with pytest.raises(UnsafeQuery):
        check_select(sql)


@pytest.mark.parametrize("sql", [
    "SELECT 'delete; drop table' AS note",                     # keywords inside a string
    "SELECT [update], [set] FROM shop.audit",                  # quoted names
    "SELECT TOP (5) id FROM shop.orders -- then drop everything",  # a comment
    "WITH t AS (SELECT status, COUNT(*) AS n FROM shop.orders GROUP BY status) SELECT * FROM t;",
])
def test_tsql_guard_allows_plain_reads(sql):
    assert check_select(sql).lower().startswith(("select", "with"))


class FakeConnection:
    """Records what MssqlSource does with a connection."""

    def __init__(self, rows=(), error=None, permissions=()):
        self.rows, self.error, self.permissions = list(rows), error, list(permissions)
        self.executed, self.rolled_back, self.closed, self.timeout = [], False, False, None

    def cursor(self):
        conn = self

        class Cursor:
            description = [("n",)]

            def execute(self, sql, params=()):
                conn.executed.append((sql, params))
                if conn.error:
                    raise conn.error

            def fetchmany(self, n):
                return conn.rows[:n]

            def fetchall(self):
                return [(p,) for p in conn.permissions.pop(0)] if conn.permissions else []
        return Cursor()

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_azure_sql_queries_are_guarded_time_limited_and_rolled_back(monkeypatch):
    source = open_source("mssql://example.database.windows.net/appdb?schemas=shop")
    conn = FakeConnection(rows=[(1,), (2,), (3,)])
    monkeypatch.setattr(source, "connect", lambda: conn)

    result = source.run("SELECT n FROM shop.numbers", max_rows=2, timeout_s=7)

    assert result.rows == [[1], [2]] and result.truncated
    assert conn.timeout == 7 and conn.rolled_back and conn.closed

    with pytest.raises(UnsafeQuery):
        source.run("DELETE FROM shop.numbers")
    assert len(conn.executed) == 1  # the write never reached the server


def test_azure_sql_errors_are_readable(monkeypatch):
    source = MssqlSource("example.database.windows.net", 1433, "appdb", ["shop"])
    error = RuntimeError("[Microsoft][ODBC Driver 18][SQL Server]Invalid object name 'shop.orderz'.")
    monkeypatch.setattr(source, "connect", lambda: FakeConnection(error=error))
    with pytest.raises(UnsafeQuery, match="Invalid object name 'shop.orderz'"):
        source.run("SELECT * FROM shop.orderz")


@pytest.mark.parametrize("permissions,read_only", [
    ([["CONNECT", "VIEW DEFINITION"], ["SELECT"]], True),
    ([["CONNECT"], ["SELECT", "INSERT"]], False),
    ([["CONNECT", "CREATE TABLE"], ["SELECT"]], False),
    ([["CONTROL"], ["SELECT"]], False),
])
def test_read_only_login_is_checked_on_the_server(monkeypatch, permissions, read_only):
    source = MssqlSource("example.database.windows.net", 1433, "appdb", ["shop"])
    monkeypatch.setattr(source, "connect", lambda: FakeConnection(permissions=permissions))
    assert source.check_read_only() is read_only


def test_unsafe_schema_names_are_rejected():
    with pytest.raises(ValueError):
        open_source("mssql://example.database.windows.net/appdb?schemas=shop];DROP")


def test_prompt_uses_the_database_dialect():
    class TsqlSource:
        dialect, label = "mssql", "Azure SQL"

        def describe(self):
            return "TABLE shop.orders (\n    [id] int NOT NULL\n)"

    system = DatabaseAssistant(None, TsqlSource(), "Revenue excludes returns.").system[0]["text"]
    assert "T-SQL" in system and "TOP (50)" in system and "LIMIT 50" not in system
    assert "Revenue excludes returns." in system and "TABLE shop.orders" in system


# ---------- sign-in, limits and the audit log ----------

def test_sign_in_is_required_and_the_allowlist_applies(make_app):
    http, _ = make_app(auth_mode="easyauth", allowed_users={"alice@example.com"})
    assert http.get("/api/me").status_code == 401
    assert http.get("/api/me", headers=BOB).status_code == 403
    assert http.get("/api/me", headers=ALICE).get_json()["name"] == "alice@example.com"


def test_every_question_is_logged_with_its_sql_and_cost(make_app):
    http, _ = make_app(good_answer() + [plan("", can_answer=False) | {"explanation": "No weather data."}])

    ok = http.post("/api/ask", json={"question": "Orders by status?"}).get_json()
    assert http.post("/api/ask", json={"question": "Weather?"}).status_code == 422
    assert http.post("/api/query", json={"sql": "DROP TABLE orders"}).status_code == 400

    log = http.get("/api/history").get_json()
    assert [(e["kind"], e["status"]) for e in log] == [("sql", "blocked"), ("ask", "error"), ("ask", "ok")]
    assert log[2]["id"] == ok["id"] and log[2]["sql_text"].startswith("SELECT status")
    assert log[2]["row_count"] == 4 and log[2]["cost_usd"] > 0
    assert log[1]["cost_usd"] > 0  # a failed question still cost tokens, and counts towards the limit
    assert http.get("/api/me").get_json()["spent_today_usd"] > 0


def test_users_see_only_their_own_history_and_admins_see_the_audit_log(make_app):
    http, _ = make_app(good_answer() + good_answer(), auth_mode="easyauth", admin_users={"alice@example.com"})
    alice = http.post("/api/ask", json={"question": "Orders?"}, headers=ALICE).get_json()
    http.post("/api/ask", json={"question": "Orders, Bob?"}, headers=BOB)

    assert [e["question"] for e in http.get("/api/history", headers=BOB).get_json()] == ["Orders, Bob?"]
    assert http.get("/api/history?all=1", headers=BOB).status_code == 403
    everyone = http.get("/api/history?all=1", headers=ALICE).get_json()
    assert {e["owner_name"] for e in everyone} == {"alice@example.com", "bob@example.com"}
    assert http.get(f"/api/questions/{alice['id']}/export.csv", headers=BOB).status_code == 404


def test_daily_limit_stops_questions(make_app):
    http, fake = make_app(good_answer() + good_answer(), daily_budget_per_user=0.000001)
    assert http.post("/api/ask", json={"question": "Orders?"}).status_code == 200
    response = http.post("/api/ask", json={"question": "Again?"})
    assert response.status_code == 429 and "limit" in response.get_json()["error"]
    assert len(fake.requests) == 2  # the second question never reached the model


def test_queries_stop_when_the_login_could_write(make_app, monkeypatch):
    monkeypatch.setattr(SqliteSource, "check_read_only", lambda self: False)
    http, fake = make_app(good_answer(), require_read_only=True)
    response = http.post("/api/ask", json={"question": "Orders?"})
    assert response.status_code == 503 and "read-only" in response.get_json()["error"]
    assert http.get("/readyz").status_code == 503
    assert fake.requests == []


def test_running_your_own_sql_can_be_switched_off(make_app):
    http, _ = make_app(allow_custom_sql=False)
    assert http.post("/api/query", json={"sql": "SELECT 1"}).status_code == 403
    assert b'id="run-sql" class="btn btn-secondary" hidden' in http.get("/").data


def test_csv_export_reruns_the_query_and_is_excel_safe(make_app):
    http, _ = make_app()
    sql = """SELECT '=HYPERLINK("http://evil")' AS note, COUNT(*) AS orders FROM orders"""
    run = http.post("/api/query", json={"sql": sql}).get_json()
    text = http.get(f"/api/questions/{run['id']}/export.csv").get_data(as_text=True)
    assert text.startswith("﻿note,orders")
    assert "'=HYPERLINK" in text and "3140" in text


# ---------- demo mode, dashboard, health and headers ----------

def test_demo_mode_answers_the_example_questions(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = create_app(Settings(database_url=f"sqlite:///{DB}", store_url=f"sqlite:///{tmp_path / 'a.db'}",
                              demo=True))
    http = app.test_client()
    for question in EXAMPLE_QUESTIONS:
        data = http.post("/api/ask", json={"question": question}).get_json()
        assert data["rows"], question
        assert data["chart"]["type"] != "none", question  # the suggested columns exist in the result
    assert http.post("/api/ask", json={"question": "Anything else?"}).status_code == 422


def test_dashboard_is_hidden_for_other_databases(tmp_path, make_app):
    other = tmp_path / "other.db"
    import sqlite3
    with sqlite3.connect(other) as conn:
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT)")
    assert load_dashboard(SqliteSource(other)) is None


def test_health_and_security_headers(make_app):
    http, _ = make_app()
    assert http.get("/healthz").get_json() == {"status": "ok"}
    ready = http.get("/readyz").get_json()
    assert ready["status"] == "ready" and ready["read_only"] is True

    page = http.get("/")
    nonce = re.search(r"'nonce-([^']+)'", page.headers["Content-Security-Policy"])[1]
    assert f'<script nonce="{nonce}">'.encode() in page.data  # the inline theme script may run
    assert page.headers["X-Frame-Options"] == "DENY"
    assert page.headers["X-Request-ID"]


# ---------- a real Azure SQL database (optional) ----------

@pytest.mark.skipif(not os.environ.get("ASKDB_TEST_MSSQL"), reason="set ASKDB_TEST_MSSQL to run against Azure SQL")
def test_azure_sql_gives_the_same_dashboard_as_sqlite():
    source = open_source(os.environ["ASKDB_TEST_MSSQL"])
    assert "TABLE shop.orders" in source.describe()
    azure, local = load_dashboard(source), load_dashboard(SqliteSource(DB))
    assert azure is not None
    for card in ("kpis", "categories", "cities"):
        assert json.dumps(azure[card]["rows"]) == json.dumps(local[card]["rows"]), card
    assert [r[0] for r in azure["monthly"]["rows"]] == [r[0] for r in local["monthly"]["rows"]]
    with pytest.raises(UnsafeQuery):
        source.run("SELECT * INTO shop.copy FROM shop.orders")
    assert isinstance(source.check_read_only(), bool)
