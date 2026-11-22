"""
Benchmark for the offline core: how often the NL->SQL engine gets the right answer,
how well the safety layers block attacks without blocking normal queries, and how
long each step takes. Claude mode is not measured here (it costs money); the numbers
for it come from examples/example-answers.json, measured on 2026-10-28.

    python scripts/benchmark.py            # full run, writes examples/benchmark.json and .md
    python scripts/benchmark.py --quick    # CI smoke: fewer latency rounds, writes to a temp folder

Everything runs on a temporary copy of shop.db, so the red team cannot touch the real file.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from assistant import AskError, DatabaseAssistant  # noqa: E402
from dashboard import QUERIES  # noqa: E402
from demo import PLANS, DemoClient  # noqa: E402
from safe_sql import SqliteSource  # noqa: E402
from sqlkit.catalog import Catalog  # noqa: E402
from sqlkit.evaluate import evaluate_questions, latency, red_team  # noqa: E402
from sqlkit.nl2sql import Declined, OfflineEngine  # noqa: E402
from sqlkit.policy import Policy  # noqa: E402

SEMANTIC = ROOT / "config" / "shop.semantic.json"
DENIED = ("customers.email",)


def load(name: str) -> dict | list:
    return json.loads((ROOT / "eval" / name).read_text(encoding="utf-8"))


def benign_set(questions: list[dict]) -> list[dict]:
    """Queries that must pass: red-team controls, every gold query, the dashboard and the demo plans."""
    items = [{"id": b["id"], "sql": b["sql"]} for b in load("redteam.json")["benign"]]
    items += [{"id": f"gold-{q['id']}", "sql": q["gold"]} for q in questions]
    items += [{"id": f"dashboard-{k}", "sql": v["sql"]} for k, v in QUERIES.items()]
    items += [{"id": f"demo-{i}", "sql": plan[3]} for i, plan in enumerate(PLANS.values())]
    return items


def compact(block: dict) -> dict:
    return {k: v for k, v in block.items() if k != "items"}


def bench_accuracy(engine, policy, db, questions) -> dict:
    result = evaluate_questions(engine, policy, db, questions)
    misses = [{"id": r["id"], "question": r["question"], "reason": r["reason"] or "wrong result"}
              for r in result["items"] if r["kind"] == "template" and not r["correct"]]
    return {"all": result["all"], "template": result["template"], "beyond": result["beyond"],
            "template_misses": misses}


def bench_latency(engine, policy, db, questions, rounds: int) -> dict:
    texts = [q["question"] for q in questions]
    sqls = [q["gold"] for q in questions]
    link, plan, verify, run = [], [], [], []
    source = SqliteSource(Path(db), policy=policy)
    for _ in range(rounds):
        for text in texts:
            t0 = time.perf_counter()
            engine.linker.link(text)
            link.append((time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter()
            try:
                engine.plan(text)
            except Declined:
                pass
            plan.append((time.perf_counter() - t0) * 1000)
        for sql in sqls:
            t0 = time.perf_counter()
            policy.check(sql)
            verify.append((time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter()
            source.run(sql)
            run.append((time.perf_counter() - t0) * 1000)

    # end to end through the same path as the web app: assistant + offline client + guarded source
    assistant = DatabaseAssistant(DemoClient(engine), source, linker=engine.linker, engine="offline")
    ask, answered = [], 0
    for text in texts:
        t0 = time.perf_counter()
        try:
            assistant.ask(text)
            answered += 1
        except AskError:
            pass
        ask.append((time.perf_counter() - t0) * 1000)
    return {"schema_linking": latency(link), "nl_to_sql_plan": latency(plan), "static_check": latency(verify),
            "query_execution": latency(run),
            "offline_ask_end_to_end": {**latency(ask), "answered": answered, "questions": len(texts)}}


def bench_redteam(db, questions) -> dict:
    data = load("redteam.json")
    report = red_team(db, data["attacks"], benign_set(questions), denied_columns=DENIED, timeout_s=1.0)
    return report.rates()


def claude_mode() -> dict:
    path = ROOT / "examples" / "example-answers.json"
    answers = json.loads(path.read_text(encoding="utf-8"))
    seconds = [a["seconds"] for a in answers]
    return {
        "measured_at": "2026-10-28",
        "source": "examples/example-answers.json (8 example questions, one run each)",
        "questions": len(answers),
        "answered_first_try": sum(1 for a in answers if a["attempts"] == 1),
        "seconds_mean": round(sum(seconds) / len(seconds), 1),
        "seconds_max": max(seconds),
        "total_cost_usd_approx": 0.13,
        "execution_accuracy": "not measured",
        "red_team_with_model": "not measured",
    }


def to_markdown(r: dict) -> str:
    ins, out, rt, lat = r["in_sample"], r["held_out"], r["red_team"], r["latency"]

    def pct(x):
        return f"{x * 100:.1f}%"

    def ex(b):
        return f"{b['correct']}/{b['questions']} = {b['correct'] / b['questions'] * 100:.1f}%"

    def ms(b):
        return f"p50 {b['p50_ms']:.2f} ms, p95 {b['p95_ms']:.2f} ms"

    lines = [
        "# Benchmark", "",
        f"Measured {r['measured_at']} on Python {r['machine']['python']}, {r['machine']['platform']}. "
        "Offline mode only (no AI); every number below comes from `scripts/benchmark.py`.", "",
        "## NL->SQL execution accuracy (offline engine)", "",
        "EX = the result of the generated query equals the result of the hand-written gold query "
        "(rows compared as a multiset, order-insensitive, numbers rounded to 2 decimals). "
        "A declined question counts as wrong.", "",
        "| Set | Template-shaped | Beyond templates | All | Coverage (all) | Precision when answered |",
        "|---|---|---|---|---|---|",
        f"| In-sample (`eval/questions.json`, engine tuned on it) | {ex(ins['template'])} | {ex(ins['beyond'])} "
        f"| {ex(ins['all'])} | {pct(ins['all']['coverage'])} | {pct(ins['all']['precision_when_answered'])} |",
        f"| Held-out (`eval/holdout.json`, written after the engine froze) | {ex(out['template'])} "
        f"| {ex(out['beyond'])} | {ex(out['all'])} | {pct(out['all']['coverage'])} "
        f"| {pct(out['all']['precision_when_answered'])} |",
        "",
        "| Schema linking (tables) | Recall | Precision |", "|---|---|---|",
        f"| In-sample | {pct(ins['all']['link_table_recall'])} | {pct(ins['all']['link_table_precision'])} |",
        f"| Held-out | {pct(out['all']['link_table_recall'])} | {pct(out['all']['link_table_precision'])} |",
        "",
        "## Red team", "",
        f"{rt['attacks']} attacks, {rt['benign']} benign queries (red-team controls, all gold queries, "
        "dashboard and demo SQL). Each layer is measured on its own and combined.", "",
        "| Layer | Block rate (attacks) | False-block rate (benign) |", "|---|---|---|",
    ]
    for layer, label in (("static", "Static check (sqlkit parser + policy)"),
                         ("runtime", "Runtime only (read-only connection, authorizer, timeout)"),
                         ("combined", "Both (as shipped)")):
        lines.append(f"| {label} | {pct(rt['block_rate'][layer])} | {pct(rt['false_block_rate'][layer])} |")
    lines += ["", "| Category | n | Static | Runtime | Combined |", "|---|---|---|---|---|"]
    for cat, v in rt["by_category"].items():
        lines.append(f"| {cat} | {v['n']} | {pct(v['static'])} | {pct(v['runtime'])} | {pct(v['combined'])} |")
    lines += ["", f"Database file unchanged after the run (sha256): **{rt['db_unchanged']}**.", "",
              "## Latency (offline)", "", "| Step | Result |", "|---|---|",
              f"| Schema linking | {ms(lat['schema_linking'])} |",
              f"| NL->SQL plan (link + frame + compile) | {ms(lat['nl_to_sql_plan'])} |",
              f"| Static check (tokenize + parse + verify + rewrite) | {ms(lat['static_check'])} |",
              f"| Guarded execution on shop.db (static check + read-only run) | {ms(lat['query_execution'])} |",
              f"| Offline ask, end to end (web-app path) | {ms(lat['offline_ask_end_to_end'])} |",
              "", "## Claude mode", "",
              f"Not re-measured (paid API). From `examples/example-answers.json`, measured "
              f"{r['claude_mode']['measured_at']}: {r['claude_mode']['questions']} questions, "
              f"{r['claude_mode']['answered_first_try']} answered on the first try, "
              f"mean {r['claude_mode']['seconds_mean']} s per answer, about "
              f"${r['claude_mode']['total_cost_usd_approx']} in total. Execution accuracy and red-team rates "
              "with the model: not measured.", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=20, help="latency rounds over the question set")
    ap.add_argument("--quick", action="store_true", help="CI smoke run: 2 rounds, output to a temp folder")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    rounds = 2 if args.quick else args.rounds

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "shop.db"
        shutil.copyfile(ROOT / "shop.db", db)
        semantic = json.loads(SEMANTIC.read_text(encoding="utf-8"))
        catalog = Catalog.from_sqlite(db)
        engine = OfflineEngine(catalog, semantic)
        policy = Policy.from_catalog(catalog, denied_columns=DENIED)
        questions, holdout = load("questions.json"), load("holdout.json")
        questions = questions["questions"] if isinstance(questions, dict) else questions
        holdout = holdout["questions"] if isinstance(holdout, dict) else holdout

        result = {
            "measured_at": time.strftime("%Y-%m-%d"),
            "machine": {"python": platform.python_version(), "platform": platform.platform(terse=True),
                        "processor": platform.processor() or platform.machine()},
            "in_sample": bench_accuracy(engine, policy, db, questions),
            "held_out": bench_accuracy(engine, policy, db, holdout),
            "red_team": bench_redteam(db, questions + holdout),
            "latency": bench_latency(engine, policy, db, questions, rounds),
            "claude_mode": claude_mode(),
            "rounds": rounds,
        }

    out = Path(args.out) if args.out else (Path(tempfile.gettempdir()) / "askdb-bench" if args.quick
                                           else ROOT / "examples")
    out.mkdir(parents=True, exist_ok=True)
    (out / "benchmark.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (out / "benchmark.md").write_text(to_markdown(result), encoding="utf-8")
    print(to_markdown(result))
    if args.quick:  # smoke gates for CI
        assert result["red_team"]["block_rate"]["combined"] == 1.0, "an attack got through"
        assert result["red_team"]["db_unchanged"], "the database changed"
        assert result["in_sample"]["template"]["execution_accuracy"] >= 0.9, "accuracy regressed"
    return 0


if __name__ == "__main__":
    sys.exit(main())
