"""
The second sample database: a B2B marketing and sales pipeline (seed_pipeline.py).

Checks that the generated data is deterministic and consistent, that the offline engine
answers the pipeline questions exactly like the hand-written gold SQL, that it declines
what it cannot build, and that the app runs on it end to end without an API key.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY") or "test-key"  # never used

import seed_pipeline  # noqa: E402
from app import SAMPLES, Settings, create_app, example_plans  # noqa: E402
from sqlkit.catalog import Catalog  # noqa: E402
from sqlkit.evaluate import same_result  # noqa: E402
from sqlkit.nl2sql import Declined, OfflineEngine  # noqa: E402
from sqlkit.policy import Policy, PolicyViolation  # noqa: E402

SEMANTIC_PATH = ROOT / "config" / "pipeline.semantic.json"
SEMANTIC = json.loads(SEMANTIC_PATH.read_text(encoding="utf-8"))
QUESTIONS = json.loads((ROOT / "eval" / "pipeline.json").read_text(encoding="utf-8"))["questions"]
TEMPLATE = [q for q in QUESTIONS if q["kind"] == "template"]
BEYOND = [q for q in QUESTIONS if q["kind"] == "beyond"]


@pytest.fixture(scope="module")
def tmp():
    path = Path(tempfile.mkdtemp())
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="module")
def db(tmp):
    path = tmp / "pipeline.db"
    seed_pipeline.build(path)
    return path


@pytest.fixture(scope="module")
def conn(db):
    connection = sqlite3.connect(db)
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def engine(db):
    return OfflineEngine.from_files(db, SEMANTIC_PATH)


@pytest.fixture(scope="module")
def policy(db):
    return Policy.from_catalog(Catalog.from_sqlite(db), denied_columns=["contacts.email"])


def scalar(conn, sql):
    return conn.execute(sql).fetchone()[0]


# ---------- the data ----------

def test_seed_is_deterministic(tmp, conn):
    again = tmp / "again.db"
    seed_pipeline.build(again)
    other = sqlite3.connect(again)
    try:
        for table in ("channels", "campaigns", "ad_spend", "ga4_sessions", "accounts", "contacts", "deals"):
            sql = f"SELECT * FROM {table} ORDER BY id"
            assert conn.execute(sql).fetchall() == other.execute(sql).fetchall(), table
    finally:
        other.close()


def test_data_is_consistent(conn):
    # Every closed deal has a close date after it was created; open deals have none.
    assert scalar(conn, "SELECT COUNT(*) FROM deals WHERE stage LIKE 'closed_%' "
                        "AND (closed_at IS NULL OR closed_at < created_at)") == 0
    assert scalar(conn, "SELECT COUNT(*) FROM deals WHERE stage NOT LIKE 'closed_%' AND closed_at IS NOT NULL") == 0
    # A lead becomes an MQL on or after the day it was created (created_at also holds the time).
    assert scalar(conn, "SELECT COUNT(*) FROM contacts WHERE mql_at < substr(created_at, 1, 10)") == 0
    # Only paid channels spend money, and GA4 never has more engaged sessions than sessions.
    assert scalar(conn, "SELECT COUNT(*) FROM ad_spend sp JOIN campaigns ca ON ca.id = sp.campaign_id "
                        "JOIN channels ch ON ch.id = ca.channel_id WHERE ch.kind != 'paid'") == 0
    assert scalar(conn, "SELECT COUNT(*) FROM ga4_sessions WHERE engaged_sessions > sessions "
                        "OR conversions > sessions") == 0
    assert scalar(conn, "SELECT COUNT(*) FROM ad_spend WHERE clicks > impressions OR cost_eur < 0") == 0
    # Original-source attribution: a deal keeps the channel of an existing contact of its account.
    assert scalar(conn, "SELECT COUNT(*) FROM deals d WHERE d.channel_id NOT IN "
                        "(SELECT c.channel_id FROM contacts c WHERE c.account_id = d.account_id)") == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# ---------- the offline engine on the pipeline semantic layer ----------

@pytest.mark.parametrize("q", TEMPLATE, ids=[q["id"] for q in TEMPLATE])
def test_engine_matches_gold(q, engine, policy, conn):
    plan = engine.plan(q["question"])
    rows = conn.execute(policy.check(plan.sql).sql).fetchall()
    gold = conn.execute(policy.check(q["gold"]).sql).fetchall()
    assert gold, "a gold query that returns nothing proves nothing"
    assert same_result(rows, gold), plan.sql


@pytest.mark.parametrize("q", BEYOND, ids=[q["id"] for q in BEYOND])
def test_engine_declines_what_needs_the_ai(q, engine, policy, conn):
    with pytest.raises(Declined):
        engine.plan(q["question"])
    assert conn.execute(policy.check(q["gold"]).sql).fetchall()  # the gold itself is valid and safe


def test_won_metrics_are_dated_by_close_date(engine):
    sql = engine.plan("How many deals did we win in Q2 2026?").sql
    assert "d.stage = 'closed_won'" in sql and "d.closed_at >= '2026-04-01'" in sql and "created_at" not in sql


def test_pipeline_is_dated_by_creation(engine):
    sql = engine.plan("Pipeline by channel in Q3 2026").sql
    assert "d.created_at >= '2026-07-01'" in sql and "closed_at" not in sql


def test_fact_tables_are_never_joined(engine):
    # Ad spend by account segment would multiply spend rows through deals: decline, don't guess.
    with pytest.raises(Declined):
        engine.plan("Spend by segment")
    for question in ("Ad spend by campaign", "Sessions by channel", "Leads by channel", "Pipeline by channel"):
        sql = engine.plan(question).sql
        facts = [t for t in ("ad_spend", "ga4_sessions", "contacts", "deals") if f" {t} " in sql]
        assert len(facts) == 1, sql


def test_metrics_with_different_definitions_are_not_mixed(engine):
    # Pipeline is dated by created_at, won revenue by closed_at: one query can't hold both.
    with pytest.raises(Declined, match="one at a time"):
        engine.plan("Pipeline and won revenue by channel in 2026")
    assert "deals" in engine.plan("Pipeline and deals by owner").sql


def test_value_inside_a_phrase_is_a_filter_not_a_breakdown(engine):
    sql = engine.plan("Pipeline from paid channels by month").sql
    assert "ch.kind = 'paid'" in sql and "ch.name" not in sql
    assert "ch.kind AS channel_type" in engine.plan("Pipeline by channel type").sql


def test_bare_breakdown_uses_the_default_metric(engine):
    assert "pipeline_eur" in engine.plan("by channel").sql


def test_email_is_blocked(policy):
    with pytest.raises(PolicyViolation):
        policy.check("SELECT email FROM contacts")
    with pytest.raises(PolicyViolation):
        policy.check("SELECT * FROM contacts")


def test_examples_are_answerable(engine, policy, conn):
    plans = example_plans(SEMANTIC)
    assert len(plans) == 2
    for example in SEMANTIC["examples"]:
        if isinstance(example, str):
            sql = engine.plan(example).sql
        else:
            sql = example["sql"]
            with pytest.raises(Declined):  # written in advance because the engine can't build it
                engine.plan(example["question"])
        result = conn.execute(policy.check(sql).sql)
        columns = [c[0] for c in result.description]
        assert result.fetchall(), example
        if not isinstance(example, str):
            assert {example["chart"]["x"], example["chart"]["y"]} <= set(columns)


# ---------- the app on the pipeline sample ----------

def test_sample_switch_selects_database_rules_and_denied_column():
    with mock.patch.dict(os.environ, {"ASKDB_SAMPLE": "pipeline"}):
        for key in ("ASKDB_DATABASE", "ASKDB_CONTEXT", "ASKDB_SEMANTIC", "ASKDB_DENY_COLUMNS"):
            os.environ.pop(key, None)
        settings = Settings.from_env()
    assert settings.database_url.endswith("pipeline.db")
    assert Path(settings.context_path or "").name == "pipeline.md"
    assert Path(settings.semantic_path or "").name == "pipeline.semantic.json"
    assert settings.deny_columns == ["contacts.email"]
    with mock.patch.dict(os.environ, {"ASKDB_SAMPLE": "warehouse"}), pytest.raises(ValueError):
        Settings.from_env()
    for sample in SAMPLES.values():
        assert (ROOT / "config" / sample["context"]).exists() and (ROOT / "config" / sample["semantic"]).exists()


@pytest.fixture(scope="module")
def client(db, tmp):
    settings = Settings(database_url=f"sqlite:///{db}", store_url=f"sqlite:///{tmp / 'askdb.db'}", demo=True,
                        context_path=str(ROOT / "config" / "pipeline.md"), semantic_path=str(SEMANTIC_PATH),
                        deny_columns=["contacts.email"])
    return create_app(settings).test_client()


def ask(client, question):
    return client.post("/api/ask", json={"question": question})


def test_app_shows_pipeline_examples(client):
    page = client.get("/").get_data(as_text=True)
    assert "Which channel brought the most pipeline in Q3 2026?" in page
    assert "pipeline created" in page  # the offline banner lists this sample's metrics
    assert "Which products sold the most" not in page


def test_app_answers_a_pipeline_question_offline(client, conn):
    response = ask(client, "Which channel brought the most pipeline in Q3 2026?")
    data = response.get_json()
    assert response.status_code == 200, data
    assert data["engine"] == "offline" and data["columns"] == ["channel", "pipeline_eur"]
    expected = conn.execute("SELECT ch.name FROM deals d JOIN channels ch ON ch.id = d.channel_id "
                            "WHERE d.created_at >= '2026-07-01' AND d.created_at < '2026-10-01' "
                            "GROUP BY ch.id ORDER BY SUM(d.amount_eur) DESC LIMIT 1").fetchone()[0]
    assert data["rows"][0][0] == expected


def test_app_answers_the_prewritten_cross_source_example(client):
    data = ask(client, "Which paid channel returns the most pipeline per euro of ad spend?").get_json()
    assert data["columns"][0] == "channel" and "pipeline_per_eur" in data["columns"]
    assert len(data["rows"]) == 4  # the four paid channels
    assert data["chart"]["type"] == "bar"


def test_app_declines_instead_of_guessing(client):
    response = ask(client, "What is the cost per opportunity by channel?")
    assert response.status_code == 422 and "AI mode" in response.get_json()["error"]


def test_shop_dashboard_is_hidden_on_another_database(client):
    assert client.get("/api/dashboard").get_json() == {"available": False}
