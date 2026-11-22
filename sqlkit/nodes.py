"""AST node types for the read-only SELECT subset. Every node is a plain dataclass."""

from __future__ import annotations

from dataclasses import dataclass, field


class Node:
    """Base class; children() lets the policy walk any tree without knowing every type."""

    def children(self) -> list[Node]:
        out: list[Node] = []
        for value in vars(self).values():
            if isinstance(value, Node):
                out.append(value)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, Node):
                        out.append(item)
                    elif isinstance(item, tuple):
                        out.extend(x for x in item if isinstance(x, Node))
        return out


# ---------- expressions ----------

@dataclass
class Literal(Node):
    kind: str            # num, str, blob, null, bool, keyword (CURRENT_DATE ...)
    value: str


@dataclass
class Param(Node):
    name: str


@dataclass
class Column(Node):
    name: str
    table: str | None = None
    schema: str | None = None
    pos: int = 0


@dataclass
class Star(Node):
    """`*` or `t.*` in a result column list."""
    table: str | None = None
    pos: int = 0


@dataclass
class FrameBound(Node):
    kind: str                       # UNBOUNDED PRECEDING, CURRENT ROW, PRECEDING, FOLLOWING, UNBOUNDED FOLLOWING
    offset: Node | None = None


@dataclass
class Window(Node):
    name: str | None = None         # OVER w (named window) or a base window name
    partition_by: list[Node] = field(default_factory=list)
    order_by: list[OrderItem] = field(default_factory=list)
    frame_units: str | None = None  # ROWS, RANGE, GROUPS
    frame_start: FrameBound | None = None
    frame_end: FrameBound | None = None


@dataclass
class Func(Node):
    name: str
    args: list[Node] = field(default_factory=list)
    distinct: bool = False
    star: bool = False              # count(*)
    filter_where: Node | None = None
    over: Window | None = None
    pos: int = 0


@dataclass
class Unary(Node):
    op: str                         # - + ~ NOT
    operand: Node


@dataclass
class Binary(Node):
    op: str                         # OR AND = == != <> < <= > >= + - * / % || << >> & | IS, IS NOT, -> ->>
    left: Node
    right: Node


@dataclass
class Like(Node):
    op: str                         # LIKE GLOB REGEXP MATCH
    expr: Node
    pattern: Node
    negated: bool = False
    escape: Node | None = None


@dataclass
class Between(Node):
    expr: Node
    low: Node
    high: Node
    negated: bool = False


@dataclass
class InList(Node):
    expr: Node
    items: list[Node]
    negated: bool = False


@dataclass
class InQuery(Node):
    expr: Node
    query: Query
    negated: bool = False


@dataclass
class Exists(Node):
    query: Query
    negated: bool = False


@dataclass
class Subquery(Node):
    query: Query


@dataclass
class Case(Node):
    operand: Node | None
    whens: list[tuple[Node, Node]]
    else_: Node | None = None


@dataclass
class Cast(Node):
    expr: Node
    type_name: str


@dataclass
class Collate(Node):
    expr: Node
    collation: str


@dataclass
class IsNull(Node):
    """x ISNULL / x NOTNULL / x NOT NULL (postfix forms)."""
    expr: Node
    negated: bool = False


@dataclass
class ExprList(Node):
    """A row value: (a, b) as used in (a, b) IN (...) or comparisons."""
    items: list[Node]


# ---------- query structure ----------

@dataclass
class OrderItem(Node):
    expr: Node
    desc: bool = False
    nulls: str | None = None        # FIRST or LAST


@dataclass
class ResultColumn(Node):
    expr: Node                      # an expression or a Star
    alias: str | None = None


@dataclass
class TableRef(Node):
    name: str
    schema: str | None = None
    alias: str | None = None
    pos: int = 0


@dataclass
class SubqueryRef(Node):
    query: Query
    alias: str | None = None


@dataclass
class TableFunctionRef(Node):
    """FROM json_each(...) and other table-valued functions."""
    func: Func
    alias: str | None = None


@dataclass
class Join(Node):
    left: Node
    right: Node
    kind: str                       # ",", JOIN, INNER JOIN, LEFT JOIN, CROSS JOIN, NATURAL JOIN, ...
    on: Node | None = None
    using: list[str] = field(default_factory=list)


@dataclass
class SelectCore(Node):
    columns: list[ResultColumn]
    distinct: bool = False
    from_: Node | None = None
    where: Node | None = None
    group_by: list[Node] = field(default_factory=list)
    having: Node | None = None
    windows: list[tuple[str, Window]] = field(default_factory=list)


@dataclass
class CTE(Node):
    name: str
    query: Query
    columns: list[str] = field(default_factory=list)
    materialized: str | None = None  # MATERIALIZED / NOT MATERIALIZED


@dataclass
class Query(Node):
    """[WITH ...] core [UNION|INTERSECT|EXCEPT core]... [ORDER BY ...] [LIMIT n [OFFSET m]]"""
    cores: list[SelectCore]
    ops: list[str] = field(default_factory=list)   # UNION, UNION ALL, INTERSECT, EXCEPT between cores
    ctes: list[CTE] = field(default_factory=list)
    recursive: bool = False
    order_by: list[OrderItem] = field(default_factory=list)
    limit: Node | None = None
    offset: Node | None = None
    # where the LIMIT value sits in the source text, for in-place rewrites
    limit_span: tuple[int, int] | None = None
    end_pos: int = 0
