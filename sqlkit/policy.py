"""
The static verifier. Before a query reaches the database it must:

1. tokenize and parse as exactly one SELECT (parser = an allow-list of statement shapes);
2. contain no write / DDL / PRAGMA / ATTACH / transaction keyword outside strings;
3. read only allow-listed tables (CTEs and subqueries are resolved by scope, so a CTE
   named like a real table shadows it correctly) and never sqlite_* system tables or
   other attached databases;
4. reference only columns that exist in the table or derived table they resolve to,
   and never a denied (PII) column, directly or through `*` / `t.*`;
5. call only allow-listed pure functions (no load_extension, randomblob, zeroblob ...);
6. use no bind parameters, stay within a subquery depth and join budget, and not build
   an unfiltered cross product larger than max_cross_rows (estimated from row counts).

Then it rewrites the query: a LIMIT of max_rows + 1 is added when missing, a larger
literal LIMIT is clamped, and a computed LIMIT is wrapped, so the database itself
stops early. The +1 row lets the caller detect truncation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from sqlkit import nodes as n
from sqlkit.catalog import Catalog
from sqlkit.parser import MultipleStatements, Parser
from sqlkit.tokenizer import WRITE_KEYWORDS, SqlSyntaxError, tokenize

SAFE_FUNCTIONS = frozenset("""
abs avg ceil ceiling char coalesce concat concat_ws count cume_dist date datetime degrees dense_rank exp
first_value floor format glob group_concat hex ifnull iif instr json json_array json_array_length
json_each json_extract json_group_array json_group_object json_object json_tree json_type json_valid
julianday lag last_value lead length like likely ln log log10 log2 lower ltrim max min mod nth_value
ntile nullif octet_length percent_rank pi pow power printf quote radians random rank replace round
row_number rtrim sign sin cos tan sqrt string_agg strftime substr substring sum time timediff total
trim trunc typeof unicode unixepoch unlikely upper
""".split())

ROWID = {"rowid", "oid", "_rowid_"}

CATEGORY_BY_KEYWORD = {
    "INSERT": "write", "UPDATE": "write", "DELETE": "write", "UPSERT": "write", "INTO": "write",
    "SET": "write", "RETURNING": "write",
    "DROP": "ddl", "CREATE": "ddl", "ALTER": "ddl", "RENAME": "ddl", "TRIGGER": "ddl", "VACUUM": "ddl",
    "REINDEX": "ddl", "ANALYZE": "ddl",
    "PRAGMA": "pragma", "ATTACH": "attach", "DETACH": "attach",
    "BEGIN": "transaction", "COMMIT": "transaction", "ROLLBACK": "transaction", "SAVEPOINT": "transaction",
    "RELEASE": "transaction", "TRANSACTION": "transaction",
}


class PolicyViolation(ValueError):
    """The query was refused before it reached the database. category says why."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


@dataclass
class Verdict:
    sql: str                                 # the query to run (possibly rewritten)
    original: str
    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    rewrites: list[str] = field(default_factory=list)
    joins: int = 0
    depth: int = 0
    elapsed_ms: float = 0.0

    def summary(self) -> dict:
        return {"tables": self.tables, "columns": len(self.columns), "functions": self.functions,
                "rewrites": self.rewrites, "joins": self.joins, "depth": self.depth,
                "elapsed_ms": round(self.elapsed_ms, 3)}


@dataclass
class Policy:
    tables: dict[str, set[str]]                          # allowed table -> its columns (lower case)
    denied_columns: set[tuple[str, str]] = field(default_factory=set)
    functions: frozenset[str] = SAFE_FUNCTIONS
    row_counts: dict[str, int] = field(default_factory=dict)
    max_rows: int = 500
    max_depth: int = 8
    max_joins: int = 12
    max_cross_rows: int = 2_000_000
    max_length: int = 20_000
    allow_recursive: bool = True                         # the runtime time limit stops runaway recursion

    @classmethod
    def from_catalog(cls, catalog: Catalog, denied_columns=(), **options) -> Policy:
        tables = catalog.columns_lower()
        denied = set()
        for item in denied_columns:
            table, _, column = item.strip().lower().partition(".")
            if table in tables and column in tables[table]:
                denied.add((table, column))
        counts = {t.lower(): c for t, c in catalog.row_counts.items()}
        return cls(tables=tables, denied_columns=denied, row_counts=counts, **options)

    def check(self, sql: str, max_rows: int | None = None) -> Verdict:
        """Verify and rewrite one query; raise PolicyViolation or SqlSyntaxError."""
        started = time.perf_counter()
        if len(sql) > self.max_length:
            raise PolicyViolation("resource", f"The query is longer than {self.max_length:,} characters.")
        tokens = tokenize(sql)
        for tok in tokens:
            if tok.kind == "kw" and tok.value in WRITE_KEYWORDS:
                raise PolicyViolation(CATEGORY_BY_KEYWORD.get(tok.value, "write"),
                                      f"This query is not allowed: only reading data is permitted ({tok.value}).")
            if tok.kind == "param":
                raise PolicyViolation("parameter", "This query is not allowed: bind parameters are not supported.")
        try:
            query = Parser(tokens).statement()
        except MultipleStatements as e:
            raise PolicyViolation("multi-statement", "This query is not allowed: only one statement can run "
                                                     "at a time.") from e
        walker = _Walker(self)
        walker.query(query, None, {}, 1)
        verdict = Verdict(sql=sql, original=sql, tables=sorted(walker.tables), columns=sorted(walker.columns),
                          functions=sorted(walker.functions), joins=walker.joins, depth=walker.max_seen_depth)
        self._limit(query, sql, tokens, (max_rows or self.max_rows) + 1, verdict)
        verdict.elapsed_ms = (time.perf_counter() - started) * 1000
        return verdict

    @staticmethod
    def _limit(query: n.Query, sql: str, tokens, cap: int, verdict: Verdict):
        last = [t for t in tokens if t.kind != "eof"]
        while last and last[-1].kind == "punct" and last[-1].value == ";":
            last.pop()
        text = sql[:last[-1].end] if last else sql
        if query.limit is None:
            verdict.sql = f"{text}\nLIMIT {cap}"
            verdict.rewrites.append(f"LIMIT {cap} added")
            return
        lit = query.limit
        if isinstance(lit, n.Literal) and lit.kind == "num" and _as_int(lit.value) is not None:
            value = _as_int(lit.value)
            if value < 0 or value > cap:   # a negative LIMIT means "no limit" in SQLite
                start, end = query.limit_span
                verdict.sql = sql[:start] + str(cap) + text[end:]
                verdict.rewrites.append(f"LIMIT {lit.value} clamped to {cap}")
            else:
                verdict.sql = text
            return
        verdict.sql = f"SELECT * FROM (\n{text}\n) LIMIT {cap}"
        verdict.rewrites.append(f"computed LIMIT wrapped, LIMIT {cap}")


def _reads_column(node) -> bool:
    if isinstance(node, n.Column):
        return True
    return any(_reads_column(child) for child in node.children())


def _references(item, name: str) -> bool:
    if isinstance(item, n.TableRef):
        return item.name.lower() == name and not item.schema
    if isinstance(item, n.Join):
        return _references(item.left, name) or _references(item.right, name)
    return False


def _check_recursion_stops(cte: n.CTE) -> None:
    """A recursive step with no WHERE and no LIMIT never ends; the time limit would stop it,
    but there is no reason to start it."""
    q = cte.query
    for core in q.cores[1:]:
        if core.from_ is not None and _references(core.from_, cte.name.lower()) and core.where is None \
                and q.limit is None:
            raise PolicyViolation("resource", f"This query is not allowed: the recursive CTE {cte.name} has no "
                                              "stop condition (WHERE or LIMIT).")


def _as_int(text: str) -> int | None:
    try:
        return int(text.replace("_", ""), 0)
    except ValueError:
        return None


@dataclass
class _Source:
    table: str | None             # the base table, if this is one
    columns: set[str] | None      # None: unknown (e.g. a derived table with an unnamed expression)
    rows: int = 1000


class _Scope:
    def __init__(self, parent: _Scope | None):
        self.parent = parent
        self.sources: dict[str, _Source] = {}
        self.aliases: set[str] = set()


class _Walker:
    def __init__(self, policy: Policy):
        self.p = policy
        self.tables: set[str] = set()
        self.columns: set[str] = set()
        self.functions: set[str] = set()
        self.joins = 0
        self.max_seen_depth = 0

    # ---------- queries ----------

    def query(self, q: n.Query, outer: _Scope | None, ctes: dict, depth: int) -> list[str] | None:
        if depth > self.p.max_depth:
            raise PolicyViolation("resource", f"This query is not allowed: subqueries are nested deeper than "
                                              f"{self.p.max_depth} levels.")
        self.max_seen_depth = max(self.max_seen_depth, depth)
        if q.recursive and not self.p.allow_recursive:
            raise PolicyViolation("resource", "This query is not allowed: recursive CTEs are switched off.")
        local = dict(ctes)
        for cte in q.ctes:
            name = cte.name.lower()
            if q.recursive:
                local[name] = {c.lower() for c in cte.columns} or None
                _check_recursion_stops(cte)
            out = self.query(cte.query, outer, local, depth + 1)
            if cte.columns:
                local[name] = {c.lower() for c in cte.columns}
            else:
                local[name] = set(out) if out is not None else None
        outputs, first_scope = None, None
        for i, core in enumerate(q.cores):
            cols, scope = self.core(core, outer, local, depth)
            if i == 0:
                outputs, first_scope = cols, scope
        if q.order_by:
            if len(q.cores) == 1:
                order_scope = first_scope
            else:  # a compound query orders by its output columns
                order_scope = _Scope(outer)
                order_scope.aliases = set(outputs or [])
                order_scope.sources["_compound"] = _Source(None, set(outputs) if outputs is not None else None)
            for item in q.order_by:
                self.expr(item.expr, order_scope, local, depth, allow_aliases=True)
        for e in (q.limit, q.offset):
            if e is not None:
                self.expr(e, _Scope(outer), local, depth)
        return outputs

    def core(self, core: n.SelectCore, outer, ctes, depth):
        scope = _Scope(outer)
        unconstrained: list[int] = []
        if core.from_ is not None:
            self.from_item(core.from_, scope, ctes, depth, unconstrained)
        if unconstrained and core.where is None:
            estimate = 1
            for rows in unconstrained:
                estimate *= max(rows, 1)
            if estimate > self.p.max_cross_rows:
                raise PolicyViolation("resource", f"This query is not allowed: it builds an unfiltered cross product "
                                                  f"of about {estimate:,} rows.")
        scope.aliases = {rc.alias.lower() for rc in core.columns if rc.alias}
        outputs: list[str] | None = []
        for rc in core.columns:
            if isinstance(rc.expr, n.Star):
                expanded = self.star(rc.expr, scope)
                if expanded is None or outputs is None:
                    outputs = None
                else:
                    outputs.extend(expanded)
                continue
            self.expr(rc.expr, scope, ctes, depth)
            if outputs is not None:
                if rc.alias:
                    outputs.append(rc.alias.lower())
                elif isinstance(rc.expr, n.Column):
                    outputs.append(rc.expr.name.lower())
        for part in (core.where, core.having, *core.group_by):
            if part is not None:
                self.expr(part, scope, ctes, depth, allow_aliases=True)
        for _, window in core.windows:
            self.expr(window, scope, ctes, depth)
        return outputs, scope

    def from_item(self, item, scope: _Scope, ctes, depth, unconstrained: list):
        if isinstance(item, n.Join):
            self.joins += 1
            if self.joins > self.p.max_joins:
                raise PolicyViolation("resource", f"This query is not allowed: more than {self.p.max_joins} joins.")
            before = set(scope.sources)
            self.from_item(item.left, scope, ctes, depth, unconstrained)
            left_new = [scope.sources[k] for k in scope.sources if k not in before]
            mid = set(scope.sources)
            self.from_item(item.right, scope, ctes, depth, unconstrained)
            right_new = [scope.sources[k] for k in scope.sources if k not in mid]
            # ON 1 = 1 is a cross join in disguise: only a condition that reads a column constrains it.
            constrained = (item.on is not None and _reads_column(item.on)) or item.using or "NATURAL" in item.kind
            if not constrained:  # estimate each side by its largest table (joined sides are constrained)
                if not unconstrained:
                    unconstrained.append(max((s.rows for s in left_new), default=1))
                unconstrained.append(max((s.rows for s in right_new), default=1))
            if item.on is not None:
                self.expr(item.on, scope, ctes, depth)
            for name in item.using:
                self.using_column(name.lower(), left_new + right_new)
            return
        if isinstance(item, n.TableRef):
            key = (item.alias or item.name).lower()
            scope.sources[key] = self.table(item, ctes)
            return
        if isinstance(item, n.SubqueryRef):
            out = self.query(item.query, scope.parent, ctes, depth + 1)
            key = (item.alias or f"(subquery {len(scope.sources)})").lower()
            scope.sources[key] = _Source(None, set(out) if out is not None else None)
            return
        if isinstance(item, n.TableFunctionRef):
            self.expr(item.func, scope, ctes, depth)
            key = (item.alias or item.func.name).lower()
            scope.sources[key] = _Source(None, None)
            return
        raise PolicyViolation("syntax", "This query is not allowed: unsupported FROM clause.")

    def table(self, ref: n.TableRef, ctes) -> _Source:
        name = ref.name.lower()
        if ref.schema and ref.schema.lower() not in ("main",):
            raise PolicyViolation("attach", f"This query is not allowed: it reads another database "
                                            f"({ref.schema}.{ref.name}).")
        if name in ctes and not ref.schema:
            return _Source(None, ctes[name])
        if name.startswith("sqlite_") or name.startswith("pragma_"):
            raise PolicyViolation("system-table", f"This query is not allowed: {ref.name} is a system table.")
        if name not in self.p.tables:
            raise PolicyViolation("table", f"This query is not allowed: unknown table {ref.name}.")
        self.tables.add(name)
        return _Source(name, self.p.tables[name], self.p.row_counts.get(name, 1000))

    def using_column(self, name: str, sources: list[_Source]):
        for src in sources:
            if src.table and (src.table, name) in self.p.denied_columns:
                raise PolicyViolation("pii", f"This query is not allowed: {src.table}.{name} is a protected column.")

    def star(self, star: n.Star, scope: _Scope) -> list[str] | None:
        if star.table:
            src = self.lookup(star.table.lower(), scope)
            if src is None:
                raise PolicyViolation("column", f"This query is not allowed: unknown table {star.table} in "
                                                f"{star.table}.*.")
            sources = [src]
        else:
            sources = list(scope.sources.values())
        out: list[str] | None = []
        for src in sources:
            if src.table:
                hidden = sorted(c for t, c in self.p.denied_columns if t == src.table)
                if hidden:
                    raise PolicyViolation("pii", f"This query is not allowed: * would include the protected column "
                                                 f"{src.table}.{hidden[0]}; list the columns you need.")
                self.columns.update(f"{src.table}.{c}" for c in src.columns or ())
            if src.columns is None or out is None:
                out = None
            else:
                out.extend(sorted(src.columns))
        return out

    @staticmethod
    def lookup(name: str, scope: _Scope | None) -> _Source | None:
        while scope is not None:
            if name in scope.sources:
                return scope.sources[name]
            scope = scope.parent
        return None

    # ---------- expressions ----------

    def expr(self, node, scope: _Scope, ctes, depth, allow_aliases: bool = False):
        stack = [node]
        while stack:
            e = stack.pop()
            if isinstance(e, n.Column):
                self.column(e, scope, allow_aliases)
                continue
            if isinstance(e, n.Param):
                raise PolicyViolation("parameter", "This query is not allowed: bind parameters are not supported.")
            if isinstance(e, n.Func):
                name = e.name.lower()
                if name not in self.p.functions:
                    raise PolicyViolation("function", f"This query is not allowed: the function {e.name}() is not "
                                                      "on the allow-list.")
                self.functions.add(name)
            if isinstance(e, (n.Subquery, n.Exists)):
                self.query(e.query, scope, ctes, depth + 1)
                continue
            if isinstance(e, n.InQuery):
                stack.append(e.expr)
                self.query(e.query, scope, ctes, depth + 1)
                continue
            stack.extend(e.children())

    def column(self, col: n.Column, scope: _Scope, allow_aliases: bool):
        name = col.name.lower()
        if col.schema and col.schema.lower() != "main":
            raise PolicyViolation("attach", f"This query is not allowed: it reads another database ({col.schema}).")
        if col.table:
            src = self.lookup(col.table.lower(), scope)
            if src is None:
                raise PolicyViolation("column", f"This query is not allowed: unknown table or alias {col.table} "
                                                f"in {col.table}.{col.name}.")
            self._accept(src, name, f"{col.table}.{col.name}")
            return
        level = scope
        while level is not None:
            matches = [s for s in level.sources.values() if s.columns is None or name in s.columns]
            known = [s for s in matches if s.columns is not None]
            if known:
                for src in known:
                    self._accept(src, name, col.name)
                return
            if matches or name in ROWID and level.sources:
                return
            if allow_aliases and level is scope and name in level.aliases:
                return
            level = level.parent
        raise PolicyViolation("column", f"This query is not allowed: unknown column {col.name}.")

    def _accept(self, src: _Source, name: str, shown: str):
        if src.columns is not None and name not in src.columns and name not in ROWID:
            raise PolicyViolation("column", f"This query is not allowed: unknown column {shown}.")
        if src.table:
            if (src.table, name) in self.p.denied_columns:
                raise PolicyViolation("pii", f"This query is not allowed: {src.table}.{name} is a protected column.")
            self.columns.add(f"{src.table}.{name}")


__all__ = ["Policy", "PolicyViolation", "SAFE_FUNCTIONS", "SqlSyntaxError", "Verdict"]
