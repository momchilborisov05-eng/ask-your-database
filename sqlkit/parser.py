"""
A recursive-descent parser for the read-only part of SQLite's SELECT grammar.

Supported: WITH [RECURSIVE] CTEs (with column lists, [NOT] MATERIALIZED), SELECT
[DISTINCT], result columns with aliases, * and t.*, FROM with subqueries,
table-valued functions and every join form (comma, [NATURAL] [LEFT|RIGHT|FULL]
[OUTER] | INNER | CROSS JOIN with ON or USING), WHERE, GROUP BY, HAVING, WINDOW,
UNION [ALL] / INTERSECT / EXCEPT, ORDER BY with NULLS FIRST/LAST, LIMIT/OFFSET
and LIMIT a, b. Expressions follow SQLite's precedence table: OR, AND, NOT,
equality-class (= == != <> IS [NOT] [DISTINCT FROM], [NOT] IN, LIKE, GLOB, REGEXP,
MATCH, BETWEEN, ISNULL, NOTNULL), comparison, bitwise, + -, * / %, || -> ->>,
unary, COLLATE. Primaries include CASE, CAST, [NOT] EXISTS, scalar subqueries,
row values, function calls with DISTINCT, FILTER (WHERE ...) and OVER windows
with ROWS/RANGE/GROUPS frames.

Anything else (INSERT, PRAGMA, VALUES, a second statement, ...) is a syntax error,
so the parser itself is a default-deny allow-list of statement shapes.
"""

from __future__ import annotations

from sqlkit.nodes import (
    CTE,
    Between,
    Binary,
    Case,
    Cast,
    Collate,
    Column,
    Exists,
    ExprList,
    FrameBound,
    Func,
    InList,
    InQuery,
    IsNull,
    Join,
    Like,
    Literal,
    OrderItem,
    Param,
    Query,
    ResultColumn,
    SelectCore,
    Star,
    Subquery,
    SubqueryRef,
    TableFunctionRef,
    TableRef,
    Unary,
    Window,
)
from sqlkit.tokenizer import NON_RESERVED, SqlSyntaxError, Token, tokenize

__all__ = ["MultipleStatements", "SqlSyntaxError", "parse"]

MAX_NESTING = 64


class MultipleStatements(SqlSyntaxError):
    """More than one statement, e.g. `SELECT 1; DROP TABLE orders`."""


def parse(sql: str) -> Query:
    """Parse exactly one SELECT statement (an optional trailing `;` is allowed)."""
    return Parser(tokenize(sql)).statement()


class Parser:
    def __init__(self, tokens: list[Token]):
        self.toks = tokens
        self.i = 0
        self.nesting = 0

    # ---------- token helpers ----------

    @property
    def tok(self) -> Token:
        return self.toks[self.i]

    def peek(self, k: int = 1) -> Token:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def advance(self) -> Token:
        t = self.toks[self.i]
        if t.kind != "eof":
            self.i += 1
        return t

    @property
    def prev(self) -> Token:
        return self.toks[self.i - 1]

    def error(self, message: str, tok: Token | None = None) -> SqlSyntaxError:
        tok = tok or self.tok
        found = "end of query" if tok.kind == "eof" else repr(tok.value)
        return SqlSyntaxError(f"{message}, found {found}", tok.pos)

    def at_kw(self, *words: str) -> bool:
        return self.tok.kind == "kw" and self.tok.value in words

    def accept_kw(self, *words: str) -> bool:
        if self.at_kw(*words):
            self.advance()
            return True
        return False

    def expect_kw(self, word: str) -> Token:
        if not self.at_kw(word):
            raise self.error(f"Expected {word}")
        return self.advance()

    def at_p(self, ch: str) -> bool:
        return self.tok.kind == "punct" and self.tok.value == ch

    def accept_p(self, ch: str) -> bool:
        if self.at_p(ch):
            self.advance()
            return True
        return False

    def expect_p(self, ch: str) -> Token:
        if not self.at_p(ch):
            raise self.error(f"Expected {ch!r}")
        return self.advance()

    def at_op(self, *ops: str) -> bool:
        return self.tok.kind == "op" and self.tok.value in ops

    def is_name(self, tok: Token) -> bool:
        return tok.kind in ("ident", "qident") or (tok.kind == "kw" and tok.value in NON_RESERVED)

    def name(self, what: str = "a name") -> str:
        if not self.is_name(self.tok):
            raise self.error(f"Expected {what}")
        return self.advance().value

    def starts_query(self, tok: Token | None = None) -> bool:
        tok = tok or self.tok
        return tok.kind == "kw" and tok.value in ("SELECT", "WITH")

    def enter(self):
        self.nesting += 1
        if self.nesting > MAX_NESTING:
            raise self.error("The query is nested too deeply")

    def leave(self):
        self.nesting -= 1

    # ---------- statements ----------

    def statement(self) -> Query:
        if not self.starts_query():
            raise self.error("Only SELECT queries are allowed")
        query = self.query()
        while self.accept_p(";"):
            if self.tok.kind != "eof":
                raise MultipleStatements("Only one statement can run at a time", self.prev.pos)
        if self.tok.kind != "eof":
            raise self.error("Unexpected text after the query")
        return query

    def query(self) -> Query:
        self.enter()
        ctes, recursive = [], False
        if self.accept_kw("WITH"):
            recursive = self.accept_kw("RECURSIVE")
            while True:
                ctes.append(self.cte())
                if not self.accept_p(","):
                    break
        cores, ops = [self.select_core()], []
        while self.at_kw("UNION", "INTERSECT", "EXCEPT"):
            op = self.advance().value
            if op == "UNION" and self.accept_kw("ALL"):
                op = "UNION ALL"
            ops.append(op)
            cores.append(self.select_core())
        q = Query(cores=cores, ops=ops, ctes=ctes, recursive=recursive)
        if self.accept_kw("ORDER"):
            self.expect_kw("BY")
            q.order_by = self.order_items()
        if self.accept_kw("LIMIT"):
            first_tok = self.tok
            first = self.expr()
            first_span = (first_tok.pos, self.prev.end)
            if self.accept_kw("OFFSET"):
                q.limit, q.offset = first, self.expr()
                q.limit_span = first_span
            elif self.accept_p(","):  # LIMIT offset, count
                count_tok = self.tok
                q.offset, q.limit = first, self.expr()
                q.limit_span = (count_tok.pos, self.prev.end)
            else:
                q.limit, q.limit_span = first, first_span
        q.end_pos = self.prev.end
        self.leave()
        return q

    def cte(self) -> CTE:
        name = self.name("a CTE name")
        columns = []
        if self.accept_p("("):
            columns = self.name_list()
            self.expect_p(")")
        self.expect_kw("AS")
        materialized = None
        if self.accept_kw("NOT"):
            self.expect_kw("MATERIALIZED")
            materialized = "NOT MATERIALIZED"
        elif self.accept_kw("MATERIALIZED"):
            materialized = "MATERIALIZED"
        self.expect_p("(")
        if not self.starts_query():
            raise self.error("A CTE must contain a SELECT")
        query = self.query()
        self.expect_p(")")
        return CTE(name=name, query=query, columns=columns, materialized=materialized)

    def name_list(self) -> list[str]:
        names = [self.name()]
        while self.accept_p(","):
            names.append(self.name())
        return names

    def select_core(self) -> SelectCore:
        if self.at_kw("VALUES"):
            raise self.error("VALUES lists are not supported; use SELECT")
        self.expect_kw("SELECT")
        distinct = self.accept_kw("DISTINCT")
        if not distinct:
            self.accept_kw("ALL")
        core = SelectCore(columns=self.result_columns(), distinct=distinct)
        if self.accept_kw("FROM"):
            core.from_ = self.from_clause()
        if self.accept_kw("WHERE"):
            core.where = self.expr()
        if self.accept_kw("GROUP"):
            self.expect_kw("BY")
            core.group_by = self.expr_list()
        if self.accept_kw("HAVING"):
            core.having = self.expr()
        if self.accept_kw("WINDOW"):
            while True:
                wname = self.name("a window name")
                self.expect_kw("AS")
                self.expect_p("(")
                core.windows.append((wname, self.window_def()))
                self.expect_p(")")
                if not self.accept_p(","):
                    break
        return core

    def result_columns(self) -> list[ResultColumn]:
        cols = [self.result_column()]
        while self.accept_p(","):
            cols.append(self.result_column())
        return cols

    def result_column(self) -> ResultColumn:
        if self.at_op("*"):
            return ResultColumn(Star(pos=self.advance().pos))
        if (self.is_name(self.tok) and self.peek().kind == "punct" and self.peek().value == "."
                and self.peek(2).kind == "op" and self.peek(2).value == "*"):
            table_tok = self.advance()
            self.advance()
            self.advance()
            return ResultColumn(Star(table=table_tok.value, pos=table_tok.pos))
        expr = self.expr()
        return ResultColumn(expr, self.alias(allow_string=True))

    def alias(self, allow_string: bool = False) -> str | None:
        if self.accept_kw("AS"):
            if allow_string and self.tok.kind == "str":
                return self.advance().value
            return self.name("an alias")
        if self.tok.kind in ("ident", "qident"):
            return self.advance().value
        if allow_string and self.tok.kind == "str":
            return self.advance().value
        return None

    # ---------- FROM ----------

    JOIN_START = ("JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL")

    def from_clause(self):
        left = self.table_or_subquery()
        while True:
            if self.accept_p(","):
                kind = ","
            elif self.at_kw(*self.JOIN_START):
                kind = self.join_operator()
            else:
                return left
            right = self.table_or_subquery()
            join = Join(left=left, right=right, kind=kind)
            if self.accept_kw("ON"):
                join.on = self.expr()
            elif self.accept_kw("USING"):
                self.expect_p("(")
                join.using = self.name_list()
                self.expect_p(")")
            left = join

    def join_operator(self) -> str:
        words = []
        if self.accept_kw("NATURAL"):
            words.append("NATURAL")
        if self.at_kw("LEFT", "RIGHT", "FULL"):
            words.append(self.advance().value)
            if self.accept_kw("OUTER"):
                words.append("OUTER")
        elif self.at_kw("INNER", "CROSS"):
            words.append(self.advance().value)
        self.expect_kw("JOIN")
        words.append("JOIN")
        return " ".join(words)

    def table_or_subquery(self):
        if self.accept_p("("):
            self.enter()
            if self.starts_query():
                node = SubqueryRef(self.query())
                self.expect_p(")")
                node.alias = self.alias()
            else:
                node = self.from_clause()
                self.expect_p(")")
            self.leave()
            return node
        tok = self.tok
        name = self.name("a table name")
        schema = None
        if self.accept_p("."):
            schema, name = name, self.name("a table name")
        if self.at_p("("):
            func = self.function_call(name, tok)
            return TableFunctionRef(func, self.alias())
        ref = TableRef(name=name, schema=schema, pos=tok.pos)
        ref.alias = self.alias()
        if self.at_kw("INDEXED") or (self.at_kw("NOT") and self.peek().is_kw("INDEXED")):
            raise self.error("INDEXED BY hints are not supported")
        return ref

    # ---------- ORDER BY / lists ----------

    def order_items(self) -> list[OrderItem]:
        items = []
        while True:
            item = OrderItem(self.expr())
            if self.accept_kw("DESC"):
                item.desc = True
            else:
                self.accept_kw("ASC")
            if self.accept_kw("NULLS"):
                if not self.at_kw("FIRST", "LAST"):
                    raise self.error("Expected FIRST or LAST")
                item.nulls = self.advance().value
            items.append(item)
            if not self.accept_p(","):
                return items

    def expr_list(self) -> list:
        items = [self.expr()]
        while self.accept_p(","):
            items.append(self.expr())
        return items

    # ---------- expressions, lowest precedence first ----------

    def expr(self):
        self.enter()
        node = self.or_expr()
        self.leave()
        return node

    def or_expr(self):
        node = self.and_expr()
        while self.accept_kw("OR"):
            node = Binary("OR", node, self.and_expr())
        return node

    def and_expr(self):
        node = self.not_expr()
        while self.accept_kw("AND"):
            node = Binary("AND", node, self.not_expr())
        return node

    def not_expr(self):
        if self.at_kw("NOT") and not self.peek().is_kw("EXISTS"):
            self.advance()
            return Unary("NOT", self.not_expr())
        return self.equality()

    def equality(self):
        node = self.comparison()
        while True:
            t = self.tok
            if t.kind == "op" and t.value in ("=", "==", "!=", "<>"):
                self.advance()
                node = Binary(t.value, node, self.comparison())
            elif t.is_kw("IS"):
                self.advance()
                op = "IS NOT" if self.accept_kw("NOT") else "IS"
                if self.at_kw("DISTINCT"):
                    self.advance()
                    self.expect_kw("FROM")
                    op = "IS NOT" if op == "IS" else "IS"  # IS DISTINCT FROM == IS NOT
                node = Binary(op, node, self.comparison())
            elif t.kind == "ident" and t.value.upper() in ("ISNULL", "NOTNULL"):
                self.advance()
                node = IsNull(node, negated=t.value.upper() == "NOTNULL")
            elif t.is_kw("NOT") and self.peek().is_kw("NULL"):
                self.advance()
                self.advance()
                node = IsNull(node, negated=True)
            elif t.is_kw("IN", "LIKE", "GLOB", "REGEXP", "MATCH", "BETWEEN") or (
                    t.is_kw("NOT") and self.peek().is_kw("IN", "LIKE", "GLOB", "REGEXP", "MATCH", "BETWEEN")):
                negated = self.accept_kw("NOT")
                node = self.predicate(node, negated)
            else:
                return node

    def predicate(self, left, negated: bool):
        word = self.advance().value
        if word == "IN":
            if not self.at_p("("):
                raise self.error("IN needs a list or a subquery in parentheses")
            self.advance()
            if self.starts_query():
                q = self.query()
                self.expect_p(")")
                return InQuery(left, q, negated)
            items = [] if self.at_p(")") else self.expr_list()
            self.expect_p(")")
            return InList(left, items, negated)
        if word == "BETWEEN":
            low = self.comparison()
            self.expect_kw("AND")
            return Between(left, low, self.comparison(), negated)
        pattern = self.comparison()
        escape = self.comparison() if self.accept_kw("ESCAPE") else None
        return Like(word, left, pattern, negated, escape)

    def comparison(self):
        node = self.bitwise()
        while self.at_op("<", "<=", ">", ">="):
            op = self.advance().value
            node = Binary(op, node, self.bitwise())
        return node

    def bitwise(self):
        node = self.additive()
        while self.at_op("&", "|", "<<", ">>"):
            op = self.advance().value
            node = Binary(op, node, self.additive())
        return node

    def additive(self):
        node = self.multiplicative()
        while self.at_op("+", "-"):
            op = self.advance().value
            node = Binary(op, node, self.multiplicative())
        return node

    def multiplicative(self):
        node = self.concat()
        while self.at_op("*", "/", "%"):
            op = self.advance().value
            node = Binary(op, node, self.concat())
        return node

    def concat(self):
        node = self.unary()
        while self.at_op("||", "->", "->>"):
            op = self.advance().value
            node = Binary(op, node, self.unary())
        return node

    def unary(self):
        if self.at_op("-", "+", "~"):
            op = self.advance().value
            self.enter()
            node = Unary(op, self.unary())
            self.leave()
            return node
        node = self.primary()
        while self.accept_kw("COLLATE"):
            node = Collate(node, self.name("a collation"))
        return node

    def primary(self):
        t = self.tok
        if t.kind in ("num", "str", "blob"):
            self.advance()
            return Literal(t.kind, t.value)
        if t.kind == "param":
            self.advance()
            return Param(t.value)
        if t.is_kw("NULL"):
            self.advance()
            return Literal("null", "NULL")
        if t.is_kw("TRUE", "FALSE"):
            self.advance()
            return Literal("bool", t.value)
        if t.is_kw("CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP"):
            self.advance()
            return Literal("keyword", t.value)
        if t.kind == "punct" and t.value == "(":
            self.advance()
            self.enter()
            if self.starts_query():
                node = Subquery(self.query())
            else:
                first = self.expr()
                node = ExprList([first, *self._more_items()]) if self.at_p(",") else first
            self.expect_p(")")
            self.leave()
            return node
        if t.is_kw("CASE"):
            return self.case()
        if t.is_kw("CAST"):
            self.advance()
            self.expect_p("(")
            inner = self.expr()
            self.expect_kw("AS")
            type_name = self.type_name()
            self.expect_p(")")
            return Cast(inner, type_name)
        if t.is_kw("EXISTS") or (t.is_kw("NOT") and self.peek().is_kw("EXISTS")):
            negated = self.accept_kw("NOT")
            self.advance()
            self.expect_p("(")
            q = self.query()
            self.expect_p(")")
            return Exists(q, negated)
        if t.kind == "kw" and t.value in ("LIKE", "GLOB", "REPLACE", "MATCH", "REGEXP") and self.peek().value == "(":
            self.advance()
            return self.function_call(t.value.lower(), t)
        if self.is_name(t):
            self.advance()
            if self.at_p("(") and t.kind != "qident":
                return self.function_call(t.value, t)
            if self.at_p("."):
                self.advance()
                second = self.name("a column name")
                if self.at_p("."):
                    self.advance()
                    return Column(self.name("a column name"), table=second, schema=t.value, pos=t.pos)
                return Column(second, table=t.value, pos=t.pos)
            return Column(t.value, pos=t.pos)
        raise self.error("Expected an expression")

    def _more_items(self) -> list:
        items = []
        while self.accept_p(","):
            items.append(self.expr())
        return items

    def type_name(self) -> str:
        words = [self.name("a type name")]
        while self.is_name(self.tok):
            words.append(self.advance().value)
        text = " ".join(words)
        if self.accept_p("("):
            sizes = [self.signed_number()]
            if self.accept_p(","):
                sizes.append(self.signed_number())
            self.expect_p(")")
            text += "(" + ", ".join(sizes) + ")"
        return text

    def signed_number(self) -> str:
        sign = self.advance().value if self.at_op("+", "-") else ""
        if self.tok.kind != "num":
            raise self.error("Expected a number")
        return sign + self.advance().value

    def case(self) -> Case:
        self.expect_kw("CASE")
        operand = None if self.at_kw("WHEN") else self.expr()
        whens = []
        while self.accept_kw("WHEN"):
            cond = self.expr()
            self.expect_kw("THEN")
            whens.append((cond, self.expr()))
        if not whens:
            raise self.error("CASE needs at least one WHEN")
        else_ = self.expr() if self.accept_kw("ELSE") else None
        self.expect_kw("END")
        return Case(operand, whens, else_)

    def function_call(self, name: str, tok: Token) -> Func:
        self.expect_p("(")
        func = Func(name=name, pos=tok.pos)
        if self.at_op("*"):
            self.advance()
            func.star = True
        elif not self.at_p(")"):
            func.distinct = self.accept_kw("DISTINCT")
            func.args = self.expr_list()
            if self.at_kw("ORDER"):
                raise self.error("ORDER BY inside an aggregate is not supported")
        self.expect_p(")")
        if self.accept_kw("FILTER"):
            self.expect_p("(")
            self.expect_kw("WHERE")
            func.filter_where = self.expr()
            self.expect_p(")")
        if self.accept_kw("OVER"):
            if self.accept_p("("):
                func.over = self.window_def()
                self.expect_p(")")
            else:
                func.over = Window(name=self.name("a window name"))
        return func

    def window_def(self) -> Window:
        w = Window()
        if self.tok.kind in ("ident", "qident") and not self.at_kw("PARTITION", "ORDER"):
            w.name = self.advance().value
        if self.accept_kw("PARTITION"):
            self.expect_kw("BY")
            w.partition_by = self.expr_list()
        if self.accept_kw("ORDER"):
            self.expect_kw("BY")
            w.order_by = self.order_items()
        if self.at_kw("ROWS", "RANGE", "GROUPS"):
            w.frame_units = self.advance().value
            if self.accept_kw("BETWEEN"):
                w.frame_start = self.frame_bound()
                self.expect_kw("AND")
                w.frame_end = self.frame_bound()
            else:
                w.frame_start = self.frame_bound()
            if self.at_kw("EXCLUDE"):
                raise self.error("EXCLUDE in window frames is not supported")
        return w

    def frame_bound(self) -> FrameBound:
        if self.accept_kw("UNBOUNDED"):
            if self.at_kw("PRECEDING", "FOLLOWING"):
                return FrameBound("UNBOUNDED " + self.advance().value)
            raise self.error("Expected PRECEDING or FOLLOWING")
        if self.accept_kw("CURRENT"):
            self.expect_kw("ROW")
            return FrameBound("CURRENT ROW")
        offset = self.bitwise()
        if self.at_kw("PRECEDING", "FOLLOWING"):
            return FrameBound(self.advance().value, offset)
        raise self.error("Expected PRECEDING or FOLLOWING")
