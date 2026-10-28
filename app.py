"""
Ask Your Database - ask questions about a SQL database in plain language.

Run:
    python seed_db.py      # once: creates the sample shop.db
    python app.py          # http://localhost:5000
"""

import argparse
import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

from assistant import AskError, DatabaseAssistant  # noqa: E402  (reads CLAUDE_MODEL after .env is loaded)
from dashboard import EXAMPLE_QUESTIONS, load_dashboard  # noqa: E402
from safe_sql import UnsafeQuery, describe_schema, run_query  # noqa: E402

DEFAULT_DB = Path(__file__).with_name("shop.db")


def create_app(db_path: Path = DEFAULT_DB, ai_client_factory=anthropic.Anthropic) -> Flask:
    if not Path(db_path).exists():
        raise SystemExit(f"{db_path} not found. Run: python seed_db.py")
    app = Flask(__name__)
    assistant = None

    def get_assistant() -> DatabaseAssistant:
        nonlocal assistant
        if assistant is None:
            assistant = DatabaseAssistant(ai_client_factory(), db_path)
        return assistant

    def ai_ready() -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    def error(message: str, status: int = 400):
        return jsonify({"error": message}), status

    @app.get("/")
    def index():
        return render_template("index.html", examples=EXAMPLE_QUESTIONS, ai_ready=ai_ready())

    @app.get("/api/dashboard")
    def dashboard():
        return jsonify(load_dashboard(db_path))

    @app.get("/api/schema")
    def schema():
        return jsonify({"schema": describe_schema(db_path)})

    @app.post("/api/ask")
    def ask():
        if not ai_ready():
            return error("AI needs an API key. Add ANTHROPIC_API_KEY to your .env file.")
        question = str((request.get_json(silent=True) or {}).get("question", "")).strip()
        if not question:
            return error("Type a question first.")
        if len(question) > 500:
            return error("The question is too long (max 500 characters).")
        try:
            return jsonify(get_assistant().ask(question).to_dict())
        except AskError as e:
            return error(str(e), 422)
        except anthropic.AuthenticationError:
            return error("Invalid API key. Check ANTHROPIC_API_KEY in your .env file.", 502)
        except anthropic.RateLimitError:
            return error("Rate limit reached. Wait a minute and try again.", 429)
        except anthropic.APIConnectionError:
            return error("Network error. Check your internet connection.", 502)
        except anthropic.APIStatusError as e:
            return error(f"AI service error ({e.status_code}). Try again in a moment.", 502)

    @app.post("/api/query")
    def query():
        """Run your own SQL (read-only), e.g. after editing a generated query."""
        sql = str((request.get_json(silent=True) or {}).get("sql", ""))
        try:
            result = run_query(db_path, sql)
        except UnsafeQuery as e:
            return error(str(e))
        return jsonify({"columns": result.columns, "rows": result.rows,
                        "truncated": result.truncated, "elapsed_ms": result.elapsed_ms})

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    print(f"Ask Your Database is running: open http://localhost:{args.port}")
    create_app().run(port=args.port, threaded=True)
