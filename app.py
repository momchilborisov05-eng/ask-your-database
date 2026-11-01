"""
Ask Your Database - ask questions about a SQL database in plain language.

Run locally (SQLite sample shop):
    python seed_db.py      # once: creates the sample shop.db
    python app.py          # http://localhost:5000

In production it runs under gunicorn in a container on Azure Container Apps
(see Dockerfile and infra/), configured with environment variables:

    ASKDB_DATABASE     database to ask about: sqlite:///shop.db or
                       mssql://<server>.database.windows.net/<db>?schemas=sales,dbo
    READER_CLIENT_ID   managed identity that may only SELECT from those schemas
    ASKDB_STORE        the app's own audit log: sqlite:///askdb.db or mssql://...
    ASKDB_CONTEXT      business rules for the model (Markdown), default config/shop.md
    AUTH_MODE          none (one local user) or easyauth (Microsoft sign-in by the platform)
    ALLOWED_USERS      optional comma-separated allowlist of sign-in names
    ADMIN_USERS        sign-in names that may read everyone's audit log
    DAILY_BUDGET_USD / DAILY_BUDGET_TOTAL_USD   spending limits per user / for the app
    REQUIRE_READ_ONLY  1: refuse to run queries if the database login could write
    ALLOW_CUSTOM_SQL   0: hide "Run edited SQL" (only AI-written queries run)
    DEMO_MODE          1: no API key needed (example questions only)
"""

import argparse
import base64
import csv
import io
import json
import logging
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from flask import Flask, Response, g, jsonify, render_template, request

load_dotenv()

from assistant import MODEL, AskError, DatabaseAssistant, load_context  # noqa: E402
from dashboard import EXAMPLE_QUESTIONS, load_dashboard  # noqa: E402
from safe_sql import UnsafeQuery, open_source  # noqa: E402
from store import Store  # noqa: E402

HERE = Path(__file__).parent
log = logging.getLogger("askdb")


@dataclass
class Settings:
    database_url: str = f"sqlite:///{HERE / 'shop.db'}"
    schemas: list[str] = field(default_factory=list)
    reader_client_id: str | None = None
    store_url: str = f"sqlite:///{HERE / 'askdb.db'}"
    context_path: str | None = str(HERE / "config" / "shop.md")
    auth_mode: str = "none"
    allowed_users: set[str] = field(default_factory=set)
    admin_users: set[str] = field(default_factory=set)
    daily_budget_per_user: float = 1.00
    daily_budget_total: float = 5.00
    require_read_only: bool = False
    allow_custom_sql: bool = True
    demo: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        def names(key):
            return {n.strip().lower() for n in os.environ.get(key, "").split(",") if n.strip()}

        defaults = cls()
        return cls(
            database_url=os.environ.get("ASKDB_DATABASE", defaults.database_url),
            schemas=[s.strip() for s in os.environ.get("ASKDB_SCHEMAS", "").split(",") if s.strip()],
            reader_client_id=os.environ.get("READER_CLIENT_ID") or None,
            store_url=os.environ.get("ASKDB_STORE", defaults.store_url),
            context_path=os.environ.get("ASKDB_CONTEXT", defaults.context_path) or None,
            auth_mode=os.environ.get("AUTH_MODE", "none").lower(),
            allowed_users=names("ALLOWED_USERS"),
            admin_users=names("ADMIN_USERS"),
            daily_budget_per_user=float(os.environ.get("DAILY_BUDGET_USD", "1.00")),
            daily_budget_total=float(os.environ.get("DAILY_BUDGET_TOTAL_USD", "5.00")),
            require_read_only=os.environ.get("REQUIRE_READ_ONLY") == "1",
            allow_custom_sql=os.environ.get("ALLOW_CUSTOM_SQL", "1") != "0",
            demo=os.environ.get("DEMO_MODE") == "1",
        )


@dataclass
class User:
    id: str
    name: str
    admin: bool


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def create_app(settings: Settings | None = None, ai_client_factory=None) -> Flask:
    settings = settings or Settings.from_env()
    if settings.auth_mode not in {"none", "easyauth"}:
        raise ValueError("AUTH_MODE must be 'none' or 'easyauth'")
    if ai_client_factory is None:
        if settings.demo:
            from demo import DemoClient
            ai_client_factory = DemoClient
        else:
            ai_client_factory = lambda: anthropic.Anthropic(max_retries=3)  # noqa: E731

    source = open_source(settings.database_url, settings.schemas, settings.reader_client_id)
    store = Store(settings.store_url)
    context = load_context(settings.context_path)
    app = Flask(__name__)
    app.config["settings"] = settings
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Created on first use: describing an Azure SQL schema takes a few seconds.
    lock = threading.Lock()
    lazy: dict = {}

    def get_assistant() -> DatabaseAssistant:
        with lock:
            if "assistant" not in lazy:
                lazy["assistant"] = DatabaseAssistant(ai_client_factory(), source, context)
            return lazy["assistant"]

    def read_only() -> bool:
        """Whether the database login can't write; asked of the server once."""
        with lock:
            if "read_only" not in lazy:
                lazy["read_only"] = source.check_read_only()
                if not lazy["read_only"]:
                    log.warning("The database login can change data. Queries are still checked and rolled "
                                "back, but use a read-only login in production (infra/grant_sql_access.py).")
            return lazy["read_only"]

    def ai_ready() -> bool:
        return settings.demo or bool(os.environ.get("ANTHROPIC_API_KEY"))

    # ---------- request handling ----------

    @app.before_request
    def start():
        g.started = time.monotonic()
        g.request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        g.nonce = secrets.token_urlsafe(16)

    @app.after_request
    def finish(response):
        response.headers["X-Request-ID"] = g.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            f"default-src 'self'; script-src 'self' 'nonce-{g.nonce}' https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        if request.path not in {"/healthz", "/readyz"} and not request.path.startswith("/static"):
            log.info(json.dumps({"request_id": g.request_id, "method": request.method, "path": request.path,
                                 "status": response.status_code,
                                 "ms": int((time.monotonic() - g.started) * 1000)}))
        return response

    @app.errorhandler(ApiError)
    def api_error(e: ApiError):
        return jsonify({"error": str(e)}), e.status

    def current_user() -> User:
        if settings.auth_mode == "none":
            return User("local", "local", True)
        # Set by the platform after sign-in; it strips these headers from client requests.
        user_id = request.headers.get("x-ms-client-principal-id")
        name = request.headers.get("x-ms-client-principal-name", "")
        if not user_id:
            raise ApiError("Sign in required.", 401)
        if settings.allowed_users and name.lower() not in settings.allowed_users:
            raise ApiError("Your account is not on this app's access list.", 403)
        return User(user_id, name or _name_from_principal() or user_id, name.lower() in settings.admin_users)

    def check_budget(user: User):
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        if store.spent_since(today, user.id) >= settings.daily_budget_per_user:
            raise ApiError(f"You've reached today's limit of ${settings.daily_budget_per_user:.2f}. "
                           "It resets at 00:00 UTC.", 429)
        if store.spent_since(today) >= settings.daily_budget_total:
            raise ApiError("The app has reached today's overall spending limit. Try again tomorrow.", 429)

    def check_database():
        if settings.require_read_only and not read_only():
            raise ApiError("The database login can change data, so queries are switched off. "
                           "Connect with a read-only login.", 503)

    # ---------- pages and health ----------

    @app.get("/")
    def index():
        return render_template("index.html", examples=EXAMPLE_QUESTIONS, ai_ready=ai_ready(),
                               demo=settings.demo, custom_sql=settings.allow_custom_sql, nonce=g.nonce)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        checks = {"store": store.ping()}
        try:
            source.run("SELECT 1 AS ok")
            checks["database"] = True
            checks["read_only"] = read_only()
        except Exception as e:  # report, don't crash: this is what the probe is for
            log.warning("readiness: database check failed: %s", e)
            checks["database"] = False
        ready = checks["store"] and checks["database"] and (checks.get("read_only") or not settings.require_read_only)
        return jsonify({"status": "ready" if ready else "not ready", **checks,
                        "dialect": source.dialect, "model": MODEL}), 200 if ready else 503

    # ---------- API ----------

    @app.get("/api/me")
    def me():
        user = current_user()
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return jsonify({"name": user.name, "admin": user.admin, "auth": settings.auth_mode,
                        "demo": settings.demo, "database": f"{source.label}: {source.name}",
                        "custom_sql": settings.allow_custom_sql,
                        "spent_today_usd": round(store.spent_since(today, user.id), 4),
                        "daily_budget_usd": settings.daily_budget_per_user})

    @app.get("/api/dashboard")
    def dashboard():
        current_user()
        check_database()
        data = load_dashboard(source)
        return jsonify(data if data else {"available": False})

    @app.get("/api/schema")
    def schema():
        current_user()
        check_database()
        return jsonify({"schema": get_assistant().schema})

    @app.post("/api/ask")
    def ask():
        user = current_user()
        if not ai_ready():
            raise ApiError("AI needs an API key. Set ANTHROPIC_API_KEY, or DEMO_MODE=1 to try it.", 503)
        question = str((request.get_json(silent=True) or {}).get("question", "")).strip()
        if not question:
            raise ApiError("Type a question first.")
        if len(question) > 500:
            raise ApiError("The question is too long (max 500 characters).")
        check_database()
        check_budget(user)
        entry = {"owner": user.id, "owner_name": user.name, "kind": "ask", "question": question}
        try:
            answer = get_assistant().ask(question)
        except AskError as e:
            store.log(**entry, **_usage(e.usage), status="error", error=str(e)[:2000])
            raise ApiError(str(e), 422) from None
        except anthropic.APIError as e:
            store.log(**entry, status="error", error=f"{type(e).__name__}: {e}"[:2000])
            raise ApiError(_api_error_message(e), 429 if isinstance(e, anthropic.RateLimitError) else 502) from None
        question_id = store.log(**entry, **_usage(answer.usage), status="ok", sql_text=answer.sql,
                                row_count=len(answer.rows), attempts=answer.attempts,
                                elapsed_ms=answer.elapsed_ms)
        return jsonify({"id": question_id, **answer.to_dict()})

    @app.post("/api/query")
    def query():
        """Run your own SQL (read-only), e.g. after editing a generated query."""
        user = current_user()
        if not settings.allow_custom_sql:
            raise ApiError("Running your own SQL is switched off on this server.", 403)
        check_database()
        sql = str((request.get_json(silent=True) or {}).get("sql", ""))[:20000]
        entry = {"owner": user.id, "owner_name": user.name, "kind": "sql", "question": "(edited SQL)",
                 "sql_text": sql}
        try:
            result = source.run(sql)
        except UnsafeQuery as e:
            blocked = "not allowed" in str(e) or "Only" in str(e)
            store.log(**entry, status="blocked" if blocked else "error", error=str(e)[:2000])
            raise ApiError(str(e)) from None
        question_id = store.log(**entry, status="ok", row_count=len(result.rows), elapsed_ms=result.elapsed_ms)
        return jsonify({"id": question_id, "columns": result.columns, "rows": result.rows,
                        "truncated": result.truncated, "elapsed_ms": result.elapsed_ms})

    @app.get("/api/history")
    def history():
        """Your latest questions; ?all=1 is the audit log of everyone's, for admins."""
        user = current_user()
        everyone = request.args.get("all") == "1"
        if everyone and not user.admin:
            raise ApiError("Only admins can see the audit log.", 403)
        return jsonify([_jsonable(r) for r in store.history(None if everyone else user.id)])

    @app.get("/api/questions/<int:question_id>/export.csv")
    def export(question_id: int):
        """The full result of a past query as CSV (re-run now, up to 10,000 rows)."""
        user = current_user()
        entry = store.get(question_id, None if user.admin else user.id)
        if not entry or entry["status"] != "ok" or not entry["sql_text"]:
            raise ApiError("Query not found.", 404)
        check_database()
        try:
            result = source.run(entry["sql_text"], max_rows=10_000, timeout_s=30)
        except UnsafeQuery as e:
            raise ApiError(str(e)) from None
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(result.columns)
        for row in result.rows:
            # A leading = + - @ would make Excel run the cell as a formula.
            writer.writerow(["'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v
                             for v in row])
        return Response("﻿" + out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="query-{question_id}.csv"'})

    return app


def _usage(usage: dict | None) -> dict:
    if not usage:
        return {}
    return {"model": usage.get("model"), "input_tokens": usage["input_tokens"] + usage["cache_write_tokens"],
            "output_tokens": usage["output_tokens"], "cached_tokens": usage["cached_tokens"],
            "cost_usd": usage.get("cost_usd")}


def _api_error_message(e: anthropic.APIError) -> str:
    if isinstance(e, anthropic.AuthenticationError):
        return "Invalid API key. Check ANTHROPIC_API_KEY."
    if isinstance(e, anthropic.RateLimitError):
        return "Rate limit reached. Wait a minute and try again."
    if isinstance(e, anthropic.APIConnectionError):
        return "Network error: could not reach the AI service."
    if isinstance(e, anthropic.APIStatusError):
        return f"AI service error ({e.status_code}). Try again in a moment."
    return "AI service error. Try again in a moment."


def _jsonable(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
        elif hasattr(value, "is_finite"):  # decimal.Decimal from SQL Server
            out[key] = float(value)
        else:
            out[key] = value
    return out


def _name_from_principal() -> str | None:
    """The display name from X-MS-CLIENT-PRINCIPAL (base64 JSON), when the name header is empty."""
    raw = request.headers.get("x-ms-client-principal")
    if not raw:
        return None
    try:
        claims = json.loads(base64.b64decode(raw))["claims"]
    except (ValueError, KeyError, TypeError):
        return None
    return next((c["val"] for c in claims if c.get("typ") == "name"), None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    print(f"Ask Your Database is running: open http://localhost:{args.port}")
    create_app().run(port=args.port, threaded=True)
