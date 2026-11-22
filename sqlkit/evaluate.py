"""
Measurement code shared by the benchmark and the tests.

* Execution accuracy (EX): run the predicted and the gold query and compare the result
  sets as multisets of rows. Row order is ignored (ORDER BY is presentation), column
  order and names are ignored (each row is compared as a sorted tuple of values), and
  numbers are rounded to 2 decimals so 12.0 == 12 and float noise does not count.
* Schema-linking recall/precision: the tables the linker found against the tables the
  gold query reads (taken from the static check's verdict, not hand-labelled).
* Red team: every attack runs against each safety layer on its own (static check only,
  runtime only) and against both together; a benign control set measures false blocks.
  The database file is hashed before and after, so a write that got through would show.
"""

from __future__ import annotations

import hashlib
import sqlite3
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sqlkit.policy import Policy, PolicyViolation
from sqlkit.tokenizer import SqlSyntaxError


def normalize(value):
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    if value is None:
        return "\0null"
    return str(value)


def row_key(row) -> tuple:
    return tuple(sorted((normalize(v) for v in row), key=lambda v: (isinstance(v, str), v)))


def result_set(rows) -> Counter:
    return Counter(row_key(r) for r in rows)


def same_result(predicted_rows, gold_rows) -> bool:
    return result_set(predicted_rows) == result_set(gold_rows)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def latency(samples_ms: list[float]) -> dict:
    return {"n": len(samples_ms), "p50_ms": round(percentile(samples_ms, 50), 3),
            "p95_ms": round(percentile(samples_ms, 95), 3), "mean_ms": round(statistics.fmean(samples_ms), 3)
            if samples_ms else 0.0}


def file_hash(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------- execution accuracy ----------

@dataclass
class QuestionResult:
    id: str
    kind: str
    question: str
    answered: bool
    correct: bool
    sql: str = ""
    reason: str = ""
    link_recall: float = 0.0
    link_precision: float = 0.0
    ms: float = 0.0


def evaluate_questions(engine, policy: Policy, db_path: str | Path, questions: list[dict]) -> dict:
    """Run the offline engine on every question and compare with the gold result."""
    from sqlkit.nl2sql import Declined

    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    results: list[QuestionResult] = []
    try:
        for q in questions:
            gold_rows = conn.execute(q["gold"]).fetchall()
            gold_tables = set(policy.check(q["gold"]).tables)
            started = time.perf_counter()
            linking = engine.linker.link(q["question"])
            found = set(linking.tables)
            recall = len(found & gold_tables) / len(gold_tables) if gold_tables else 1.0
            precision = len(found & gold_tables) / len(found) if found else 0.0
            item = QuestionResult(q["id"], q.get("kind", "template"), q["question"], False, False,
                                  link_recall=recall, link_precision=precision)
            try:
                plan = engine.plan(q["question"])
                verdict = policy.check(plan.sql)
                rows = conn.execute(verdict.sql).fetchall()
                item.answered, item.sql = True, plan.sql
                item.correct = same_result(rows, gold_rows)
            except Declined as e:
                item.reason = str(e)
            except (PolicyViolation, SqlSyntaxError, sqlite3.Error) as e:  # counts as answered and wrong
                item.answered, item.reason = True, f"{type(e).__name__}: {e}"
            item.ms = (time.perf_counter() - started) * 1000
            results.append(item)
    finally:
        conn.close()
    return summarize_questions(results)


def summarize_questions(results: list[QuestionResult]) -> dict:
    def block(items):
        answered = [r for r in items if r.answered]
        correct = [r for r in items if r.correct]
        return {
            "questions": len(items),
            "answered": len(answered),
            "correct": len(correct),
            "coverage": round(len(answered) / len(items), 4) if items else 0.0,
            "execution_accuracy": round(len(correct) / len(items), 4) if items else 0.0,
            "precision_when_answered": round(len(correct) / len(answered), 4) if answered else 0.0,
            "link_table_recall": round(statistics.fmean(r.link_recall for r in items), 4) if items else 0.0,
            "link_table_precision": round(statistics.fmean(r.link_precision for r in items), 4) if items else 0.0,
        }

    return {
        "all": block(results),
        "template": block([r for r in results if r.kind == "template"]),
        "beyond": block([r for r in results if r.kind == "beyond"]),
        "items": [r.__dict__ for r in results],
    }


# ---------- red team ----------

@dataclass
class Layered:
    static: bool = False
    runtime: bool = False

    @property
    def combined(self) -> bool:
        return self.static or self.runtime


@dataclass
class RedTeamReport:
    attacks: list[dict] = field(default_factory=list)
    benign: list[dict] = field(default_factory=list)
    db_unchanged: bool = True

    def rates(self) -> dict:
        def rate(items, layer):
            return round(sum(1 for a in items if a[layer]) / len(items), 4) if items else 0.0

        categories = sorted({a["category"] for a in self.attacks})
        return {
            "attacks": len(self.attacks),
            "benign": len(self.benign),
            "block_rate": {layer: rate(self.attacks, layer) for layer in ("static", "runtime", "combined")},
            "false_block_rate": {layer: rate(self.benign, layer) for layer in ("static", "runtime", "combined")},
            "by_category": {
                c: {"n": len(items), **{layer: rate(items, layer) for layer in ("static", "runtime", "combined")}}
                for c in categories for items in [[a for a in self.attacks if a["category"] == c]]
            },
            "db_unchanged": self.db_unchanged,
            "missed_combined": [a["id"] for a in self.attacks if not a["combined"]],
            "false_blocks_static": [b["id"] for b in self.benign if b["static"]],
        }


def _layers(sql: str, policy: Policy, runtime_source, timeout_s: float) -> tuple[Layered, str]:
    from safe_sql import UnsafeQuery

    out = Layered()
    reason = ""
    try:
        policy.check(sql)
    except (PolicyViolation, SqlSyntaxError) as e:
        out.static = True
        reason = getattr(e, "category", "syntax")
    try:
        runtime_source.run(sql, timeout_s=timeout_s)
    except UnsafeQuery:
        out.runtime = True
    return out, reason


def red_team(db_path: str | Path, attacks: list[dict], benign: list[dict], denied_columns=("customers.email",),
             timeout_s: float = 1.0) -> RedTeamReport:
    """attacks: [{id, category, sql}], benign: [{id, sql}]. Runs on a copy is the caller's job."""
    from safe_sql import SqliteSource
    from sqlkit.catalog import Catalog

    policy = Policy.from_catalog(Catalog.from_sqlite(db_path), denied_columns=denied_columns)
    runtime = SqliteSource(Path(db_path), policy=None)
    before = file_hash(db_path)
    report = RedTeamReport()
    for a in attacks:
        layers, reason = _layers(a["sql"], policy, runtime, timeout_s)
        report.attacks.append({"id": a["id"], "category": a["category"], "static": layers.static,
                               "runtime": layers.runtime, "combined": layers.combined, "static_reason": reason})
    for b in benign:
        layers, reason = _layers(b["sql"], policy, runtime, timeout_s)
        report.benign.append({"id": b["id"], "static": layers.static, "runtime": layers.runtime,
                              "combined": layers.combined, "static_reason": reason})
    report.db_unchanged = file_hash(db_path) == before
    return report
