# Benchmark

Measured 2026-11-20 on Python 3.12.3, Windows-11. Offline mode only (no AI); every number below comes from `scripts/benchmark.py`.

## NL->SQL execution accuracy (offline engine)

EX = the result of the generated query equals the result of the hand-written gold query (rows compared as a multiset, order-insensitive, numbers rounded to 2 decimals). A declined question counts as wrong.

| Set | Template-shaped | Beyond templates | All | Coverage (all) | Precision when answered |
|---|---|---|---|---|---|
| In-sample (`eval/questions.json`, engine tuned on it) | 35/36 = 97.2% | 0/6 = 0.0% | 35/42 = 83.3% | 85.7% | 97.2% |
| Held-out (`eval/holdout.json`, written after the engine froze) | 14/15 = 93.3% | 0/2 = 0.0% | 14/17 = 82.4% | 88.2% | 93.3% |

| Schema linking (tables) | Recall | Precision |
|---|---|---|
| In-sample | 97.7% | 77.8% |
| Held-out | 98.5% | 84.8% |

## Red team

67 attacks, 92 benign queries (red-team controls, all gold queries, dashboard and demo SQL). Each layer is measured on its own and combined.

| Layer | Block rate (attacks) | False-block rate (benign) |
|---|---|---|
| Static check (sqlkit parser + policy) | 100.0% | 0.0% |
| Runtime only (read-only connection, authorizer, timeout) | 61.2% | 0.0% |
| Both (as shipped) | 100.0% | 0.0% |

| Category | n | Static | Runtime | Combined |
|---|---|---|---|---|
| function | 8 | 100.0% | 50.0% | 100.0% |
| multi-statement | 6 | 100.0% | 83.3% | 100.0% |
| pii | 12 | 100.0% | 0.0% | 100.0% |
| pragma-attach | 8 | 100.0% | 87.5% | 100.0% |
| resource | 5 | 100.0% | 80.0% | 100.0% |
| system-table | 8 | 100.0% | 12.5% | 100.0% |
| unknown-object | 5 | 100.0% | 100.0% | 100.0% |
| write | 15 | 100.0% | 100.0% | 100.0% |

Database file unchanged after the run (sha256): **True**.

## Latency (offline)

| Step | Result |
|---|---|
| Schema linking | p50 3.04 ms, p95 7.52 ms |
| NL->SQL plan (link + frame + compile) | p50 4.35 ms, p95 10.76 ms |
| Static check (tokenize + parse + verify + rewrite) | p50 0.66 ms, p95 1.24 ms |
| Guarded execution on shop.db (static check + read-only run) | p50 5.05 ms, p95 10.78 ms |
| Offline ask, end to end (web-app path) | p50 14.40 ms, p95 23.38 ms |

## Claude mode

Not re-measured (paid API). From `examples/example-answers.json`, measured 2026-10-28: 8 questions, 8 answered on the first try, mean 7.8 s per answer, about $0.13 in total. Execution accuracy and red-team rates with the model: not measured.
