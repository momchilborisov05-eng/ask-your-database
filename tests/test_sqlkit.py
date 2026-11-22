"""
Tests for the engineered core (sqlkit): tokenizer, parser, printer, static policy,
schema linker, offline NL->SQL engine and the evaluation harness. All offline.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlkit import SqlSyntaxError, parse, tokenize  # noqa: E402
from sqlkit.catalog import Catalog  # noqa: E402
from sqlkit.evaluate import percentile, red_team, row_key, same_result  # noqa: E402
from sqlkit.linker import SchemaLinker, singular, within_one_edit, words  # noqa: E402
from sqlkit.nl2sql import Declined, OfflineEngine, describe_rows  # noqa: E402
from sqlkit.parser import MultipleStatements  # noqa: E402
from sqlkit.policy import Policy, PolicyViolation  # noqa: E402
from sqlkit.printer import to_sql  # noqa: E402

SEMANTIC = json.loads((ROOT / "config" / "shop.semantic.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("sqlkit") / "shop.db"
    shutil.copyfile(ROOT / "shop.db", path)
    return path


@pytest.fixture(scope="module")
def catalog(db):
    return Catalog.from_sqlite(db)


@pytest.fixture(scope="module")
def policy(catalog):
    return Policy.from_catalog(catalog, denied_columns=["customers.email"])


@pytest.fixture(scope="module")
def engine(catalog):
    return OfflineEngine(catalog, SEMANTIC)


def rows(db, sql):
    conn = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# ---------- tokenizer ----------

def test_tokenizer_handles_strings_comments_quoted_names_and_numbers():
    toks = tokenize("SELECT 'a''b' -- DROP TABLE x\n, [x y], \"z\", 1.5e3 /* DELETE */ FROM t")
    assert [(t.kind, t.value) for t in toks] == [
        ("kw", "SELECT"), ("str", "a'b"), ("punct", ","), ("qident", "x y"), ("punct", ","),
        ("qident", "z"), ("punct", ","), ("num", "1.5e3"), ("kw", "FROM"), ("ident", "t"), ("eof", "")]


def test_tokenizer_reports_unterminated_string_with_position():
    with pytest.raises(SqlSyntaxError) as e:
        tokenize("SELECT 'oops")
    assert e.value.pos == 7


def test_tokenizer_keeps_positions():
    toks = tokenize("SELECT  id")
    assert (toks[1].pos, toks[1].end) == (8, 10)


# ---------- parser and printer ----------

@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "SELECT p.name, SUM(i.quantity) AS units FROM order_items i JOIN products p ON p.id = i.product_id "
    "GROUP BY p.id ORDER BY units DESC LIMIT 5",
    "WITH t AS (SELECT id FROM orders) SELECT COUNT(*) FROM t",
    "SELECT CASE WHEN stock < 5 THEN 'low' ELSE 'ok' END FROM products",
    "SELECT name, RANK() OVER (PARTITION BY category_id ORDER BY price_eur DESC) FROM products",
    "SELECT id FROM orders WHERE status IN ('paid', 'shipped') AND ordered_at BETWEEN '2026-01-01' AND '2026-02-01'",
    "SELECT id FROM orders UNION ALL SELECT id FROM customers",
    "SELECT CAST(price_eur AS INTEGER) FROM products WHERE name LIKE '%Pro%' AND id NOT IN (SELECT product_id FROM order_items)",
    "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 10) SELECT x FROM n",
])
def test_printer_round_trip_is_stable(sql):
    once = to_sql(parse(sql))
    assert to_sql(parse(once)) == once


def test_printer_keeps_operator_precedence():
    printed = to_sql(parse("SELECT (1 + 2) * 3, 1 + 2 * 3"))
    assert rows(ROOT / "shop.db", printed) == [(9, 7)]


@pytest.mark.parametrize("sql", ["SELECT", "SELECT FROM orders", "SELECT * FROM", "SELECT (1", "SELECT 1 +"])
def test_parser_rejects_broken_sql(sql):
    with pytest.raises(SqlSyntaxError):
        parse(sql)


def test_parser_rejects_two_statements():
    with pytest.raises(MultipleStatements):
        parse("SELECT 1; SELECT 2")


# ---------- policy ----------

@pytest.mark.parametrize("sql, category", [
    ("DELETE FROM orders", "write"),
    ("UPDATE products SET price_eur = 0", "write"),
    ("SELECT 1; SELECT 2", "multi-statement"),
    ("SELECT 1; DROP TABLE orders", "ddl"),
    ("PRAGMA table_info(orders)", "pragma"),
    ("ATTACH DATABASE 'x.db' AS x", "attach"),
    ("SELECT * FROM sqlite_master", "system-table"),
    ("SELECT email FROM customers", "pii"),
    ("SELECT * FROM customers", "pii"),
    ("SELECT c.* FROM customers c", "pii"),
    ("SELECT load_extension('x')", "function"),
    ("SELECT * FROM nope", "table"),
    ("SELECT secret FROM orders", "column"),
    ("SELECT 1 FROM orders o JOIN order_items i ON 1 = 1 JOIN order_items j ON 1 = 1", "resource"),
    ("SELECT ?", "parameter"),
])
def test_policy_blocks_by_category(policy, sql, category):
    with pytest.raises(PolicyViolation) as e:
        policy.check(sql)
    assert e.value.category == category


def test_policy_reads_pii_through_subquery_alias(policy):
    with pytest.raises(PolicyViolation) as e:
        policy.check("SELECT x.e FROM (SELECT email AS e FROM customers) x")
    assert e.value.category == "pii"


def test_policy_ignores_keywords_inside_strings_and_comments(policy):
    verdict = policy.check("SELECT 'DELETE FROM orders' AS note, id FROM orders -- DROP TABLE orders\nLIMIT 3")
    assert verdict.tables == ["orders"]


def test_policy_injects_and_clamps_limit(policy, db):
    added = policy.check("SELECT id FROM orders")
    assert added.rewrites == ["LIMIT 501 added"]
    assert len(rows(db, added.sql)) == 501
    clamped = policy.check("SELECT id FROM orders LIMIT 100000")
    assert clamped.rewrites == ["LIMIT 100000 clamped to 501"]
    small = policy.check("SELECT id FROM orders LIMIT 3")
    assert small.rewrites == [] and len(rows(db, small.sql)) == 3


def test_policy_allows_bounded_recursion_and_blocks_unbounded(policy):
    policy.check("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 10) SELECT x FROM n")
    with pytest.raises(PolicyViolation) as e:
        policy.check("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT x FROM n")
    assert e.value.category == "resource"


def test_policy_verdict_lists_tables_and_joins(policy):
    v = policy.check("SELECT c.name, COUNT(*) FROM orders o JOIN order_items i ON i.order_id = o.id "
                     "JOIN products p ON p.id = i.product_id JOIN categories c ON c.id = p.category_id GROUP BY c.name")
    assert set(v.tables) == {"orders", "order_items", "products", "categories"}
    assert v.joins == 3
    assert v.summary()["tables"] == v.tables


# ---------- schema linker ----------

def test_word_helpers():
    assert words("Best-selling items_sold?") == ["best", "selling", "items", "sold"]
    assert singular("categories") == "category" and singular("status") == "status"
    assert within_one_edit("plovdiv", "plovidv") and not within_one_edit("sofia", "varna")


def test_linker_finds_values_aliases_and_typos(catalog):
    linker = SchemaLinker(catalog, SEMANTIC)
    link = linker.link("Revenue from the website in Plovidv paid by cash")
    found = {(v.column, v.value, v.kind) for v in link.values}
    assert ("channel", "web", "alias") in found
    assert ("city", "Plovdiv", "fuzzy") in found
    assert ("method", "cash_on_delivery", "alias") in found
    assert {"orders", "customers", "payments"} <= set(link.tables)


def test_linker_join_path_connects_categories_to_customers(catalog):
    linker = SchemaLinker(catalog, SEMANTIC)
    joins = linker.join_path(["categories", "customers"])
    touched = {t for j in joins for t in (j[0], j[2])}
    assert {"categories", "products", "order_items", "orders", "customers"} <= touched
    assert len(joins) == 4


def test_linker_prompt_hints_are_short_and_explicit(catalog):
    hints = SchemaLinker(catalog, SEMANTIC).link("Revenue by category in Sofia").to_prompt()
    assert "Tables that look relevant" in hints and "'sofia' -> customers.city = 'Sofia'" in hints


# ---------- offline NL -> SQL ----------

def test_offline_engine_answers_metric_by_dimension(engine, policy, db):
    plan = engine.plan("Revenue by category")
    assert plan.chart["type"] == "bar"
    gold = ("SELECT c.name, ROUND(SUM(i.quantity * i.unit_price_eur), 2) FROM orders o "
            "JOIN order_items i ON i.order_id = o.id JOIN products p ON p.id = i.product_id "
            "JOIN categories c ON c.id = p.category_id WHERE o.status NOT IN ('cancelled', 'returned') GROUP BY c.name")
    assert same_result(rows(db, policy.check(plan.sql).sql), rows(db, gold))


def test_offline_engine_frames_filters_period_and_top_n(engine):
    frame, _ = engine.frame("Top 3 products by revenue in Sofia in October 2026")
    assert frame.metrics == ["revenue"] and frame.dimensions == ["product"]
    assert frame.top_n == 3
    assert any(f[2] == "Sofia" for f in frame.filters) or "Sofia" in str(frame.filters)
    assert "2026-10" in str(frame.period)


def test_offline_engine_monthly_trend_is_a_line_chart(engine):
    plan = engine.plan("Monthly revenue")
    assert plan.chart["type"] == "line" and "%Y-%m" in plan.sql


@pytest.mark.parametrize("question", [
    "Which customers never ordered?",
    "Compare revenue this year vs last year",
    "What is the median order value?",
    "What is the weather in Sofia?",
])
def test_offline_engine_declines_what_it_cannot_do(engine, question):
    with pytest.raises(Declined):
        engine.plan(question)


def test_describe_rows_is_deterministic():
    assert describe_rows([], []) == "The query returned no rows."
    text = describe_rows(["category", "revenue_eur"], [["Laptops", 1234.5], ["Phones", 99.0]])
    assert "Laptops" in text and text == describe_rows(["category", "revenue_eur"], [["Laptops", 1234.5], ["Phones", 99.0]])


# ---------- evaluation harness ----------

def test_result_comparison_ignores_order_and_float_noise():
    assert same_result([("a", 1.0), ("b", 2)], [("b", 2.0000001), ("a", 1)])
    assert not same_result([("a", 1)], [("a", 1), ("a", 1)])  # multiset, not set
    assert row_key(["x", None, 3]) == row_key([3, "x", None])
    assert percentile([1, 2, 3, 4], 50) == 2.5


def test_red_team_blocks_attacks_without_blocking_benign(db):
    data = json.loads((ROOT / "eval" / "redteam.json").read_text(encoding="utf-8"))
    report = red_team(db, data["attacks"][::4], data["benign"], timeout_s=0.5).rates()
    assert report["block_rate"]["combined"] == 1.0
    assert report["false_block_rate"]["combined"] == 0.0
    assert report["db_unchanged"] is True


def test_gold_queries_pass_the_static_check(policy):
    for name in ("questions.json", "holdout.json"):
        data = json.loads((ROOT / "eval" / name).read_text(encoding="utf-8"))
        for q in data["questions"] if isinstance(data, dict) else data:
            policy.check(q["gold"])


def test_benchmark_quick_run_writes_reports():
    out = Path(tempfile.mkdtemp())
    try:
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "benchmark.py"), "--quick", "--out", str(out)],
                              capture_output=True, text=True, timeout=300, cwd=ROOT,
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        assert proc.returncode == 0, proc.stderr
        result = json.loads((out / "benchmark.json").read_text(encoding="utf-8"))
        assert result["red_team"]["block_rate"]["combined"] == 1.0
        assert result["claude_mode"]["execution_accuracy"] == "not measured"
        assert (out / "benchmark.md").read_text(encoding="utf-8").startswith("# Benchmark")
    finally:
        shutil.rmtree(out, ignore_errors=True)
