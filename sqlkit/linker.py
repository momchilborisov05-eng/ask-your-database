"""
Schema linking: which tables, columns and stored values a question talks about.

The linker matches question words against four sources, strongest first:

1. stored values (category, city, channel, product names ...) as exact word n-grams,
   then single words within edit distance 1 (for words of 5+ letters, so "Plovidv"
   still finds Plovdiv), plus aliases from the semantic layer ("mobile" -> mobile_app);
2. the words of semantic-layer metrics and dimensions ("revenue", "per month");
3. table and column names, singular or plural, with underscores read as spaces;
4. nothing else: no embeddings, no model, so the result is deterministic and explainable.

The linked tables are then connected through the foreign-key graph: a greedy
Steiner-tree approximation adds the shortest join path (BFS) from the tree to each
remaining table. The result goes into Claude's prompt as hints and drives the
offline NL->SQL engine.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from sqlkit.catalog import Catalog

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)*")
# Short words that are never a value or a column on their own.
STOP = {"a", "an", "the", "of", "in", "on", "for", "by", "to", "and", "or", "is", "are", "was", "were", "what",
        "which", "who", "how", "many", "much", "me", "show", "list", "give", "per", "each", "with", "from", "at",
        "did", "do", "does", "we", "our", "us", "it", "its", "this", "that", "last", "top", "most", "best", "all",
        "id", "name", "be", "has", "have", "had", "than", "more", "less", "over", "out", "so", "as", "into"}


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("_", " "))


def singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ches", "shes", "xes", "sses")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def within_one_edit(a: str, b: str) -> bool:
    """True when a and b differ by at most one insertion, deletion, substitution or swap."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = [i for i in range(la) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
    if la > lb:
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


def find_phrase(tokens: list[str], phrase: list[str], used: set[int] | None = None) -> int:
    """Start index of phrase in tokens (whole words, singular-insensitive), or -1."""
    n = len(phrase)
    if not n:
        return -1
    target = [singular(w) for w in phrase]
    for i in range(len(tokens) - n + 1):
        if used and any(j in used for j in range(i, i + n)):
            continue
        if [singular(w) for w in tokens[i:i + n]] == target:
            return i
    return -1


@dataclass
class ValueMatch:
    table: str
    column: str
    value: str
    text: str           # the question words that matched
    kind: str           # exact | fuzzy | alias
    start: int = 0
    end: int = 0


@dataclass
class Linking:
    tables: list[str] = field(default_factory=list)
    columns: list[tuple[str, str]] = field(default_factory=list)
    values: list[ValueMatch] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    joins: list[tuple[str, str, str, str]] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def to_prompt(self) -> str:
        """Short hints for Claude's prompt; empty when nothing was linked."""
        lines = []
        if self.tables:
            lines.append("Tables that look relevant: " + ", ".join(self.tables))
        if self.values:
            lines.append("Values mentioned in the question: " + "; ".join(
                f"'{v.text}' -> {v.table}.{v.column} = '{v.value}'" for v in self.values))
        if self.joins:
            lines.append("Join path: " + "; ".join(f"{t}.{c} = {rt}.{rc}" for t, c, rt, rc in self.joins))
        return "\n".join(lines)

    def summary(self) -> dict:
        return {
            "tables": self.tables,
            "columns": [f"{t}.{c}" for t, c in self.columns],
            "values": [{"text": v.text, "column": f"{v.table}.{v.column}", "value": v.value, "kind": v.kind}
                       for v in self.values],
            "metrics": self.metrics,
            "dimensions": self.dimensions,
            "joins": [f"{t}.{c} = {rt}.{rc}" for t, c, rt, rc in self.joins],
            "elapsed_ms": round(self.elapsed_ms, 3),
        }


class SchemaLinker:
    def __init__(self, catalog: Catalog, semantic: dict | None = None):
        self.catalog = catalog
        self.semantic = semantic or {}
        self.graph = catalog.neighbours()
        # value index: lowercase value words -> (table, column, value), longest first
        self.value_index: list[tuple[list[str], str, str, str]] = []
        for (table, column), values in catalog.values.items():
            for value in values:
                toks = words(value)
                if toks and not (len(toks) == 1 and (toks[0] in STOP or len(toks[0]) < 3)):
                    self.value_index.append((toks, table, column, value))
        for key, aliases in self.semantic.get("value_aliases", {}).items():
            table, column = key.split(".")
            for alias, value in aliases.items():
                self.value_index.append((words(alias), table, column, "\0" + value))  # \0 marks an alias
        self.value_index.sort(key=lambda item: -len(item[0]))
        self.fuzzy_words = {}
        for toks, table, column, value in self.value_index:
            if len(toks) == 1 and len(toks[0]) >= 5 and not value.startswith("\0"):
                self.fuzzy_words.setdefault(toks[0], (table, column, value))

    @classmethod
    def from_files(cls, db_path: str | Path, semantic_path: str | Path | None) -> SchemaLinker:
        semantic = json.loads(Path(semantic_path).read_text(encoding="utf-8")) if semantic_path else None
        return cls(Catalog.from_sqlite(db_path), semantic)

    # ---------- values ----------

    def link_values(self, tokens: list[str]) -> list[ValueMatch]:
        used: set[int] = set()
        found: list[ValueMatch] = []
        for toks, table, column, value in self.value_index:
            start = find_phrase(tokens, toks, used) if len(toks) > 1 else self._exact(tokens, toks[0], used)
            if start < 0:
                continue
            end = start + len(toks)
            used.update(range(start, end))
            alias = value.startswith("\0")
            found.append(ValueMatch(table, column, value.lstrip("\0"), " ".join(tokens[start:end]),
                                    "alias" if alias else "exact", start, end))
        for i, tok in enumerate(tokens):
            if i in used or len(tok) < 5 or tok in STOP:
                continue
            for word, (table, column, value) in self.fuzzy_words.items():
                if within_one_edit(tok, word):
                    used.add(i)
                    found.append(ValueMatch(table, column, value, tok, "fuzzy", i, i + 1))
                    break
        # one value per column and text: drop duplicates that came through aliases
        unique, seen = [], set()
        for v in sorted(found, key=lambda v: v.start):
            key = (v.table, v.column, v.value)
            if key not in seen:
                seen.add(key)
                unique.append(v)
        return unique

    @staticmethod
    def _exact(tokens: list[str], word: str, used: set[int]) -> int:
        for i, tok in enumerate(tokens):
            if i not in used and (tok == word or singular(tok) == singular(word)):
                return i
        return -1

    # ---------- semantic words, tables, columns ----------

    def _semantic(self, tokens: list[str], kind: str) -> list[tuple[str, int, int, str]]:
        """(name, start, end, phrase) for every metric/dimension whose words appear; longest phrase wins."""
        hits = []
        for name, spec in self.semantic.get(kind, {}).items():
            best = None
            for phrase in spec.get("words", []):
                toks = words(phrase)
                start = find_phrase(tokens, toks)
                if start >= 0 and (best is None or len(toks) > best[1] - best[0]):
                    best = (start, start + len(toks), phrase)
            if best:
                hits.append((name, *best))
        return hits

    def link(self, question: str) -> Linking:
        import time

        t0 = time.perf_counter()
        tokens = words(question)
        result = Linking()
        result.values = self.link_values(tokens)
        value_spans = {i for v in result.values for i in range(v.start, v.end)}

        metrics = self._semantic(tokens, "metrics")
        dims = self._semantic(tokens, "dimensions")
        result.metrics = [m[0] for m in sorted(metrics, key=lambda h: (h[1], h[1] - h[2]))]
        result.dimensions = [d[0] for d in sorted(dims, key=lambda h: (h[1], h[1] - h[2]))]

        tables: list[str] = []

        def add(table: str):
            if table in self.catalog.tables and table not in tables:
                tables.append(table)

        for v in result.values:
            add(v.table)
        for name in result.metrics:
            for t in self.semantic["metrics"][name].get("tables", []):
                add(t)
        for name in result.dimensions:
            for t in self.semantic["dimensions"][name].get("tables", []):
                add(t)
        singular_tokens = [singular(t) for i, t in enumerate(tokens) if i not in value_spans]
        for table, cols in self.catalog.tables.items():
            if find_phrase(singular_tokens, words(table)) >= 0:
                add(table)
            for col in cols:
                parts = [w for w in words(col) if w not in ("id", "eur")]
                if parts and not (len(parts) == 1 and parts[0] in STOP) and find_phrase(singular_tokens, parts) >= 0:
                    result.columns.append((table, col))
        for t, _c in result.columns:
            add(t)
        for v in result.values:
            if (v.table, v.column) not in result.columns:
                result.columns.append((v.table, v.column))
        result.tables = tables
        result.joins = self.join_path(tables)
        result.elapsed_ms = (time.perf_counter() - t0) * 1000
        return result

    # ---------- joins ----------

    def join_path(self, tables: list[str]) -> list[tuple[str, str, str, str]]:
        """Greedy Steiner tree over the FK graph: connect every table to the growing tree by BFS."""
        if len(tables) < 2:
            return []
        tree = {tables[0]}
        edges: list[tuple[str, str, str, str]] = []
        for target in tables[1:]:
            if target in tree:
                continue
            path = self._bfs(tree, target)
            if path is None:
                continue  # not connected: leave it to the caller
            for edge in path:
                tree.add(edge[0])
                tree.add(edge[2])
                edges.append(edge)
        return edges

    def _bfs(self, sources: set[str], target: str):
        queue = deque((s, []) for s in sorted(sources))
        seen = set(sources)
        while queue:
            node, path = queue.popleft()
            if node == target:
                return path
            for t, c, other, oc in self.graph.get(node, []):
                if other not in seen:
                    seen.add(other)
                    queue.append((other, path + [(t, c, other, oc)]))
        return None
