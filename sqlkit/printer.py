"""
AST -> canonical SQLite text. Every binary operation is parenthesised, so the output
never depends on operator precedence; names are quoted only when they need it.

The round trip parse -> print -> run gives the same result set as the original
query; the tests check this on every gold, dashboard and demo query.
"""

from __future__ import annotations

import re

from sqlkit import nodes as n
from sqlkit.tokenizer import KEYWORDS

_PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def quote_name(name: str) -> str:
    if _PLAIN.match(name) and name.upper() not in KEYWORDS:
        return name
    return '"' + name.replace('"', '""') + '"'


def to_sql(node: n.Node) -> str:
    return _Printer().print(node)


class _Printer:
    def print(self, node) -> str:
        method = getattr(self, "p_" + type(node).__name__)
        return method(node)

    # ---------- query ----------

    def p_Query(self, q: n.Query) -> str:
        parts = []
        if q.ctes:
            parts.append("WITH " + ("RECURSIVE " if q.recursive else "")
                         + ", ".join(self.print(c) for c in q.ctes))
        body = self.print(q.cores[0])
        for op, core in zip(q.ops, q.cores[1:]):
            body += f" {op} " + self.print(core)
        parts.append(body)
        if q.order_by:
            parts.append("ORDER BY " + ", ".join(self.print(o) for o in q.order_by))
        if q.limit is not None:
            parts.append("LIMIT " + self.print(q.limit))
            if q.offset is not None:
                parts.append("OFFSET " + self.print(q.offset))
        return " ".join(parts)

    def p_CTE(self, c: n.CTE) -> str:
        cols = "(" + ", ".join(quote_name(x) for x in c.columns) + ")" if c.columns else ""
        mat = f" {c.materialized}" if c.materialized else ""
        return f"{quote_name(c.name)}{cols} AS{mat} ({self.print(c.query)})"

    def p_SelectCore(self, s: n.SelectCore) -> str:
        out = "SELECT " + ("DISTINCT " if s.distinct else "") + ", ".join(self.print(c) for c in s.columns)
        if s.from_ is not None:
            out += " FROM " + self.print(s.from_)
        if s.where is not None:
            out += " WHERE " + self.print(s.where)
        if s.group_by:
            out += " GROUP BY " + ", ".join(self.print(e) for e in s.group_by)
        if s.having is not None:
            out += " HAVING " + self.print(s.having)
        if s.windows:
            out += " WINDOW " + ", ".join(f"{quote_name(name)} AS ({self._window(w)})" for name, w in s.windows)
        return out

    def p_ResultColumn(self, c: n.ResultColumn) -> str:
        text = self.print(c.expr)
        return f"{text} AS {quote_name(c.alias)}" if c.alias else text

    def p_Star(self, s: n.Star) -> str:
        return f"{quote_name(s.table)}.*" if s.table else "*"

    def p_TableRef(self, t: n.TableRef) -> str:
        name = (quote_name(t.schema) + "." if t.schema else "") + quote_name(t.name)
        return f"{name} AS {quote_name(t.alias)}" if t.alias else name

    def p_SubqueryRef(self, s: n.SubqueryRef) -> str:
        text = f"({self.print(s.query)})"
        return f"{text} AS {quote_name(s.alias)}" if s.alias else text

    def p_TableFunctionRef(self, t: n.TableFunctionRef) -> str:
        text = self.print(t.func)
        return f"{text} AS {quote_name(t.alias)}" if t.alias else text

    def p_Join(self, j: n.Join) -> str:
        left, right = self.print(j.left), self.print(j.right)
        if isinstance(j.right, n.Join):
            right = f"({right})"
        text = f"{left}, {right}" if j.kind == "," else f"{left} {j.kind} {right}"
        if j.on is not None:
            text += " ON " + self.print(j.on)
        if j.using:
            text += " USING (" + ", ".join(quote_name(u) for u in j.using) + ")"
        return text

    def p_OrderItem(self, o: n.OrderItem) -> str:
        text = self.print(o.expr) + (" DESC" if o.desc else "")
        return text + (f" NULLS {o.nulls}" if o.nulls else "")

    # ---------- expressions ----------

    def p_Literal(self, lit: n.Literal) -> str:
        if lit.kind == "str":
            return "'" + lit.value.replace("'", "''") + "'"
        if lit.kind == "blob":
            return f"X'{lit.value}'"
        return lit.value

    def p_Param(self, p: n.Param) -> str:
        return p.name

    def p_Column(self, c: n.Column) -> str:
        parts = [x for x in (c.schema, c.table, c.name) if x]
        return ".".join(quote_name(x) for x in parts)

    def p_Func(self, f: n.Func) -> str:
        if f.star:
            args = "*"
        else:
            args = ("DISTINCT " if f.distinct else "") + ", ".join(self.print(a) for a in f.args)
        text = f"{f.name}({args})"
        if f.filter_where is not None:
            text += f" FILTER (WHERE {self.print(f.filter_where)})"
        if f.over is not None:
            if f.over.name and not (f.over.partition_by or f.over.order_by or f.over.frame_units):
                text += " OVER " + quote_name(f.over.name)
            else:
                text += f" OVER ({self._window(f.over)})"
        return text

    def _window(self, w: n.Window) -> str:
        parts = []
        if w.name:
            parts.append(quote_name(w.name))
        if w.partition_by:
            parts.append("PARTITION BY " + ", ".join(self.print(e) for e in w.partition_by))
        if w.order_by:
            parts.append("ORDER BY " + ", ".join(self.print(o) for o in w.order_by))
        if w.frame_units:
            if w.frame_end is not None:
                parts.append(f"{w.frame_units} BETWEEN {self._bound(w.frame_start)} AND {self._bound(w.frame_end)}")
            else:
                parts.append(f"{w.frame_units} {self._bound(w.frame_start)}")
        return " ".join(parts)

    def _bound(self, b: n.FrameBound) -> str:
        return f"{self.print(b.offset)} {b.kind}" if b.offset is not None else b.kind

    def p_Unary(self, u: n.Unary) -> str:
        if u.op == "NOT":
            return f"(NOT {self.print(u.operand)})"
        return f"({u.op}{self.print(u.operand)})"

    def p_Binary(self, b: n.Binary) -> str:
        return f"({self.print(b.left)} {b.op} {self.print(b.right)})"

    def p_Like(self, x: n.Like) -> str:
        text = f"({self.print(x.expr)} {'NOT ' if x.negated else ''}{x.op} {self.print(x.pattern)}"
        if x.escape is not None:
            text += f" ESCAPE {self.print(x.escape)}"
        return text + ")"

    def p_Between(self, b: n.Between) -> str:
        return (f"({self.print(b.expr)} {'NOT ' if b.negated else ''}BETWEEN "
                f"{self.print(b.low)} AND {self.print(b.high)})")

    def p_InList(self, x: n.InList) -> str:
        items = ", ".join(self.print(i) for i in x.items)
        return f"({self.print(x.expr)} {'NOT ' if x.negated else ''}IN ({items}))"

    def p_InQuery(self, x: n.InQuery) -> str:
        return f"({self.print(x.expr)} {'NOT ' if x.negated else ''}IN ({self.print(x.query)}))"

    def p_Exists(self, x: n.Exists) -> str:
        return f"({'NOT ' if x.negated else ''}EXISTS ({self.print(x.query)}))"

    def p_Subquery(self, s: n.Subquery) -> str:
        return f"({self.print(s.query)})"

    def p_Case(self, c: n.Case) -> str:
        text = "CASE" + (f" {self.print(c.operand)}" if c.operand is not None else "")
        for cond, result in c.whens:
            text += f" WHEN {self.print(cond)} THEN {self.print(result)}"
        if c.else_ is not None:
            text += f" ELSE {self.print(c.else_)}"
        return text + " END"

    def p_Cast(self, c: n.Cast) -> str:
        return f"CAST({self.print(c.expr)} AS {c.type_name})"

    def p_Collate(self, c: n.Collate) -> str:
        return f"({self.print(c.expr)} COLLATE {quote_name(c.collation)})"

    def p_IsNull(self, x: n.IsNull) -> str:
        return f"({self.print(x.expr)} {'NOTNULL' if x.negated else 'ISNULL'})"

    def p_ExprList(self, x: n.ExprList) -> str:
        return "(" + ", ".join(self.print(i) for i in x.items) + ")"
