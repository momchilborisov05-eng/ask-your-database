"""
Offline NL -> SQL: a template engine for the common question shapes, used when there is
no API key (and as the measured baseline in the benchmark).

    question --> frame (metrics, dimensions, filters, period, top-N) --> SQL

1. The schema linker finds metric and dimension words (from the semantic layer, e.g.
   config/shop.semantic.json) and stored values ("Sofia", "Laptops", "mobile").
2. The frame extractor adds a period ("March 2026", "Q1 2026", "2025", "last 30 days"),
   a top-N and a sort direction ("top 5", "which ... most", "bottom 3").
3. The compiler joins only the tables it needs, along the foreign-key path the linker
   found, applies the business rules from the semantic layer (revenue excludes cancelled
   and returned orders) and writes one SELECT, which then goes through the same static
   policy and read-only runner as Claude's SQL.

Anything outside these shapes (comparisons, "never", growth, medians ...) is declined
with a reason instead of guessed: a wrong number is worse than no number.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlkit.catalog import Catalog
from sqlkit.linker import Linking, SchemaLinker, singular, words

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
           "ten": 10, "twenty": 20}
_NUM = r"(\d+|" + "|".join(NUMBERS) + ")"
_MONTH = r"\b(" + "|".join(m[:3] + "(?:" + m[3:] + ")?" for m in MONTHS) + r")\.?\s+(20\d\d)\b"
_QUARTER = r"\b(?:q([1-4])|(first|second|third|fourth) quarter)\s*(?:of\s+)?(20\d\d)\b"
_LAST = r"\b(?:last|past|previous)\s+" + _NUM + r"\s+(days?|weeks?|months?)\b"
_DESC = r"\b(most|highest|biggest|largest|best|top|max(?:imum)?|greatest)\b"
_ASC = r"\b(least|lowest|smallest|worst|fewest|min(?:imum)?|bottom)\b"


class Declined(ValueError):
    """The question is outside the shapes the offline engine answers; the message says why."""


@dataclass
class Frame:
    metrics: list[str]
    dimensions: list[str] = field(default_factory=list)         # at most one categorical + one time
    filters: dict[str, list[str]] = field(default_factory=dict)  # "table.column" -> values
    period: tuple[str, str, str] | None = None                  # (kind, a, b): range start/end or relative
    period_label: str = ""
    top_n: int | None = None
    ascending: bool = False

    def describe(self) -> dict:
        return {"metrics": self.metrics, "dimensions": self.dimensions, "filters": self.filters,
                "period": self.period_label or None, "top_n": self.top_n,
                "order": "ascending" if self.ascending else "descending"}


@dataclass
class Plan:
    sql: str
    chart: dict
    explanation: str
    frame: Frame
    linking: Linking


def _number(text: str) -> int:
    return int(text) if text.isdigit() else NUMBERS[text]


class OfflineEngine:
    def __init__(self, catalog: Catalog, semantic: dict):
        self.catalog = catalog
        self.semantic = semantic
        self.linker = SchemaLinker(catalog, semantic)
        self.aliases = semantic["aliases"]
        self.filter_columns = set(semantic.get("filter_columns", []))

    @classmethod
    def from_files(cls, db_path: str | Path, semantic_path: str | Path) -> OfflineEngine:
        semantic = json.loads(Path(semantic_path).read_text(encoding="utf-8"))
        return cls(Catalog.from_sqlite(db_path), semantic)

    # ---------- question -> frame ----------

    def frame(self, question: str) -> tuple[Frame, Linking]:
        text = " ".join(words(question))
        for phrase in self.semantic.get("unsupported", []):
            if re.search(r"\b" + re.escape(phrase.strip()) + r"\b", text):
                raise Declined(f"Questions with '{phrase.strip()}' need the AI mode (set ANTHROPIC_API_KEY); "
                               "the offline engine answers metric-by-dimension questions only.")
        tokens = words(question)
        linking = self.linker.link(question)
        metric_hits = self.linker._semantic(tokens, "metrics")
        dim_hits = self.linker._semantic(tokens, "dimensions")

        # Overlapping words: the longer phrase wins ("payment method" beats "payments").
        def overlaps(a, b):
            return a[1] < b[2] and b[1] < a[2]

        def longer(a, b):
            return b[2] - b[1] > a[2] - a[1]

        metric_hits = [m for m in metric_hits if not any(overlaps(m, x) and longer(m, x)
                                                         for x in dim_hits + metric_hits)]
        dim_hits = [d for d in dim_hits if not any(overlaps(d, m) and m[2] - m[1] > d[2] - d[1]
                                                   for m in metric_hits)]
        top_n, ascending = self._top(text)
        superlative = top_n is not None or bool(re.search(r"\b(which|who)\b", text))
        # Same span, both a metric and a dimension ("customers"): decide from the rest of the question.
        for m in list(metric_hits):
            for d in [d for d in dim_hits if overlaps(m, d)]:
                other_metric = any(not overlaps(x, m) for x in metric_hits)
                other_dim = any(not overlaps(x, d) and x[0] not in ("month", "year") for x in dim_hits)
                if other_metric or (superlative and not other_dim):
                    metric_hits.remove(m)
                else:
                    dim_hits.remove(d)
                break
        # A bare noun ("orders", "customers") next to a real metric is context, not a second metric:
        # "revenue from website orders". It stays when joined with "and": "orders and revenue".
        strong = [m for m in metric_hits if m[3] not in self.semantic["metrics"][m[0]].get("weak", [])]
        if strong:
            def joined(h):
                return (h[2] < len(tokens) and tokens[h[2]] == "and") or (h[1] > 0 and tokens[h[1] - 1] == "and")

            metric_hits = [m for m in metric_hits if m in strong or joined(m)]
        metric_hits.sort(key=lambda h: h[1])
        dim_hits.sort(key=lambda h: h[1])

        metrics = self._compatible([m[0] for m in metric_hits])
        time_dims = [d for d in dim_hits if d[0] in ("month", "year")]
        cat_dims = [d for d in dim_hits if d[0] not in ("month", "year")]
        if len(cat_dims) > 1:
            raise Declined("Grouping by two categories at once needs the AI mode; ask for one breakdown.")
        dims = [d[0] for d in cat_dims] + [d[0] for d in time_dims[:1]]
        if not metrics:
            if not cat_dims:
                raise Declined("The offline engine could not find a metric (revenue, orders, units, customers, "
                               "return rate, profit ...) in this question.")
            metrics = ["revenue"]

        # Superlative over a singular noun ("which city has the most ...") means the single winner.
        if top_n is None and cat_dims:
            word = " ".join(tokens[cat_dims[0][1]:cat_dims[0][2]])
            if re.search(_DESC, text) or re.search(_ASC, text):
                ascending = bool(re.search(_ASC, text)) and not re.search(_DESC, text)
                if word == singular(word):
                    top_n = 1

        used = {i for h in metric_hits + dim_hits for i in range(h[1], h[2])}
        filters: dict[str, list[str]] = {}
        for v in linking.values:
            key = f"{v.table}.{v.column}"
            if key in self.filter_columns and not any(i in used for i in range(v.start, v.end)):
                filters.setdefault(key, [])
                if v.value not in filters[key]:
                    filters[key].append(v.value)
        frame = Frame(metrics=metrics, dimensions=dims, filters=filters, top_n=top_n, ascending=ascending)
        self._period(text, frame)
        return frame, linking

    def _compatible(self, names: list[str]) -> list[str]:
        """Several metrics share one query when they need the same extra filter and time column;
        if one of them excludes cancelled/returned orders, all of them are computed on those orders."""
        out: list[str] = []
        for name in names:
            spec = self.semantic["metrics"][name]
            if not out:
                out.append(name)
                continue
            first = self.semantic["metrics"][out[0]]
            same = all(spec.get(k) == first.get(k) for k in ("where", "time", "only_with"))
            if same and name not in out:
                out.append(name)
        return out

    @staticmethod
    def _top(text: str) -> tuple[int | None, bool]:
        m = re.search(r"\b(top|best|bottom|worst|first)\s+" + _NUM + r"\b", text)
        if m:
            return _number(m[2]), m[1] in ("bottom", "worst")
        m = re.search(r"\b" + _NUM + r"\s+(best|top|most|biggest|largest|highest|least|lowest|worst|smallest)\b",
                      text)
        if m:
            return _number(m[1]), m[2] in ("least", "lowest", "worst", "smallest")
        return None, False

    def _period(self, text: str, frame: Frame) -> None:
        m = re.search(_MONTH, text)
        if m:
            month = next(i for i, name in enumerate(MONTHS, 1) if name.startswith(m[1][:3]))
            year = int(m[2])
            end = (year + 1, 1) if month == 12 else (year, month + 1)
            frame.period = ("range", f"{year}-{month:02d}-01", f"{end[0]}-{end[1]:02d}-01")
            frame.period_label = f"{MONTHS[month - 1].title()} {year}"
            return
        m = re.search(_QUARTER, text)
        if m:
            q = int(m[1]) if m[1] else ["first", "second", "third", "fourth"].index(m[2]) + 1
            year = int(m[3])
            start = 3 * q - 2
            end = (year + 1, 1) if q == 4 else (year, start + 3)
            frame.period = ("range", f"{year}-{start:02d}-01", f"{end[0]}-{end[1]:02d}-01")
            frame.period_label = f"Q{q} {year}"
            return
        m = re.search(_LAST, text)
        if m:
            n, unit = _number(m[1]), singular(m[2])
            days = n * {"day": 1, "week": 7, "month": 30}[unit]
            frame.period = ("relative", f"-{days} days", "")
            frame.period_label = f"the last {n} {unit}{'s' if n != 1 else ''} of data"
            return
        m = re.search(r"\b(20\d\d)\b", text)
        if m:
            year = int(m[1])
            frame.period = ("range", f"{year}-01-01", f"{year + 1}-01-01")
            frame.period_label = str(year)

    # ---------- frame -> SQL ----------

    def compile(self, frame: Frame, linking: Linking | None = None) -> Plan:
        metrics = [self.semantic["metrics"][m] for m in frame.metrics]
        dims = [self.semantic["dimensions"][d] for d in frame.dimensions]
        main = metrics[0]
        time_col = main.get("time", self.semantic["default_time"])
        if frame.period and main.get("period_tables"):
            time_col = self.semantic["default_time"]

        tables: list[str] = []
        for spec in metrics + [d for d in dims if "tables" in d]:
            for t in spec.get("tables", []):
                if t not in tables:
                    tables.append(t)
        needs_time = frame.period or any("time" in d for d in dims)
        for key in frame.filters:
            if key.split(".")[0] not in tables:
                tables.append(key.split(".")[0])
        if needs_time:
            time_table = self._table_of(time_col)
            if time_table not in tables:
                tables.append(time_table)
        status_filter = "orders.status" in frame.filters
        valid = (any(m.get("valid_only") for m in metrics) and not status_filter
                 and "status" not in frame.dimensions)
        if valid and "orders" not in tables:
            tables.append("orders")
        only = main.get("only_with")
        if only and any(t not in only for t in tables):
            raise Declined(f"Splitting {main['label']} by that would count rows twice through the joins; "
                           "this needs the AI mode.")

        joins = self.linker.join_path(tables)
        joined = {tables[0]}
        from_sql = f"{tables[0]} {self.aliases[tables[0]]}"
        for t, c, other, oc in joins:
            new, known, new_col, known_col = (other, t, oc, c) if t in joined else (t, other, c, oc)
            from_sql += (f"\nJOIN {new} {self.aliases[new]} ON {self.aliases[new]}.{new_col} = "
                         f"{self.aliases[known]}.{known_col}")
            joined.add(new)
        if set(tables) - joined:
            raise Declined("The tables in this question are not connected by foreign keys.")

        select, group, order = [], [], []
        cat = next((d for d in dims if "time" not in d), None)
        timed = next((d for d in dims if "time" in d), None)
        if timed:
            expr = f"strftime('{timed['time']}', {time_col})"
            select.append(f"{expr} AS {timed['alias']}")
            group.append(expr)
        if cat:
            select.append(f"{cat['sql']} AS {cat['alias']}")
            group.append(cat.get("group", cat["sql"]))
        for m in metrics:
            select.append(f"{m['sql']} AS {m['alias']}")

        where = []
        if valid:
            where.append(self.semantic["valid_orders"])
        for m in metrics:
            if m.get("where") and m["where"] not in where:
                where.append(m["where"])
        for key, values in frame.filters.items():
            if key == "orders.status" and main.get("keeps_status"):
                continue
            table, column = key.split(".")
            col = f"{self.aliases[table]}.{column}"
            quoted = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
            where.append(f"{col} = {quoted}" if len(values) == 1 else f"{col} IN ({quoted})")
        if frame.period:
            kind, a, b = frame.period
            if kind == "range":
                where.append(f"{time_col} >= '{a}' AND {time_col} < '{b}'")
            else:
                table = self._table_of(time_col)
                bare = time_col.split(".")[1]
                where.append(f"{time_col} >= date((SELECT MAX({bare}) FROM {table}), '{a}')")

        direction = "" if frame.ascending else " DESC"
        if timed:
            order.append(timed["alias"])
        if cat:
            order.append(f"{metrics[0]['alias']}{direction}")
            order.append(cat["alias"])

        sql = "SELECT " + ", ".join(select) + "\nFROM " + from_sql
        if where:
            sql += "\nWHERE " + "\n  AND ".join(where)
        if group:
            sql += "\nGROUP BY " + ", ".join(group)
        if order:
            sql += "\nORDER BY " + ", ".join(order)
        if cat and frame.top_n and not timed:
            sql += f"\nLIMIT {frame.top_n}"

        if timed and not cat:
            chart = {"type": "line", "x": timed["alias"], "y": metrics[0]["alias"]}
        elif cat and not timed:
            chart = {"type": "bar", "x": cat["alias"], "y": metrics[0]["alias"]}
        else:
            chart = {"type": "none", "x": "", "y": ""}
        return Plan(sql=sql, chart=chart, explanation=self._explain(frame, valid), frame=frame,
                    linking=linking or Linking())

    def _table_of(self, column: str) -> str:
        alias = column.split(".")[0]
        return next(t for t, a in self.aliases.items() if a == alias)

    def _explain(self, frame: Frame, valid: bool) -> str:
        labels = [self.semantic["metrics"][m]["label"] for m in frame.metrics]
        text = " and ".join(labels).capitalize()
        if frame.dimensions:
            text += " by " + " and ".join(d.replace("_", " ") for d in frame.dimensions)
        if frame.top_n:
            text += f", {'bottom' if frame.ascending else 'top'} {frame.top_n}"
        for key, values in frame.filters.items():
            text += f", {key.split('.')[1]} = {' or '.join(values)}"
        if frame.period_label:
            text += f", {frame.period_label}"
        text += "."
        if valid:
            text += " Cancelled and returned orders are left out, as the business rules say."
        return "Offline engine (no AI): " + text

    def plan(self, question: str) -> Plan:
        frame, linking = self.frame(question)
        return self.compile(frame, linking)


def describe_rows(columns: list[str], rows: list[list]) -> str:
    """A deterministic one-sentence answer from a result, for the offline mode."""
    if not rows:
        return "The query returned no rows."

    def fmt(value):
        if isinstance(value, float):
            return f"{value:,.2f}"
        if isinstance(value, int):
            return f"{value:,}"
        return str(value)

    if len(rows) == 1:
        return "Result: " + ", ".join(f"{c.replace('_', ' ')} {fmt(v)}" for c, v in zip(columns, rows[0])) + "."
    label, value = columns[0], columns[-1]
    numeric = [r for r in rows if isinstance(r[-1], (int, float))]
    if label in ("month", "year") and numeric:
        peak = max(numeric, key=lambda r: r[-1])
        return (f"{len(rows)} {label}s from {rows[0][0]} to {rows[-1][0]}; the highest {value.replace('_', ' ')} "
                f"was {fmt(peak[-1])} in {peak[0]}.")
    first = rows[0]
    return (f"{len(rows)} rows; first is {fmt(first[0])} with {value.replace('_', ' ')} {fmt(first[-1])}, "
            f"last is {fmt(rows[-1][0])} with {fmt(rows[-1][-1])}.")
