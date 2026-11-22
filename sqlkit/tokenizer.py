"""
A hand-written SQLite tokenizer. Every token keeps its start and end offset in the
source, so the policy layer can rewrite the original text (for example replace a
LIMIT value) without reprinting the user's formatting.

Token kinds:
    kw      a keyword, value upper-cased (SELECT, FROM, DELETE, ...)
    ident   a bare identifier, value as written
    qident  a quoted identifier ("x", [x] or `x`), value unquoted
    num     a numeric literal
    str     a string literal, value unescaped
    blob    a blob literal X'..'
    param   a bind parameter (?, ?1, :name, @name, $name)
    op      an operator (+ - * / % || = == != <> < <= > >= << >> & | ~ -> ->>)
    punct   ( ) , . ;
    eof     end of input
"""

from __future__ import annotations

from dataclasses import dataclass

KEYWORDS = {
    # the read-only subset
    "SELECT", "FROM", "WHERE", "GROUP", "BY", "HAVING", "ORDER", "LIMIT", "OFFSET", "AS", "ON", "USING",
    "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER", "CROSS", "NATURAL", "AND", "OR", "NOT", "IN", "IS",
    "NULL", "LIKE", "GLOB", "REGEXP", "MATCH", "BETWEEN", "CASE", "WHEN", "THEN", "ELSE", "END", "CAST",
    "DISTINCT", "ALL", "UNION", "INTERSECT", "EXCEPT", "WITH", "RECURSIVE", "EXISTS", "ASC", "DESC",
    "NULLS", "FIRST", "LAST", "OVER", "PARTITION", "FILTER", "WINDOW", "ROWS", "RANGE", "GROUPS",
    "UNBOUNDED", "PRECEDING", "FOLLOWING", "CURRENT", "ROW", "ESCAPE", "COLLATE", "CURRENT_DATE",
    "CURRENT_TIME", "CURRENT_TIMESTAMP", "TRUE", "FALSE", "MATERIALIZED", "EXCLUDE", "TIES", "OTHERS",
    "NO", "INDEXED",
    # everything that changes data, the schema, the connection or the transaction
    "INSERT", "UPDATE", "DELETE", "REPLACE", "UPSERT", "DROP", "CREATE", "ALTER", "PRAGMA", "ATTACH",
    "DETACH", "VACUUM", "REINDEX", "ANALYZE", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE",
    "TRANSACTION", "TRIGGER", "INTO", "VALUES", "SET", "TABLE", "INDEX", "VIEW", "EXPLAIN", "RETURNING",
    "CONFLICT", "ABORT", "FAIL", "IGNORE", "TEMP", "TEMPORARY", "VIRTUAL", "DATABASE", "RENAME",
}

# Statements and clauses that write or reach outside a read-only SELECT. Seeing one of
# these anywhere in the code (not inside a string or quoted name) is enough to refuse.
WRITE_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "UPSERT", "DROP", "CREATE", "ALTER", "PRAGMA", "ATTACH", "DETACH",
    "VACUUM", "REINDEX", "ANALYZE", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "TRANSACTION",
    "TRIGGER", "INTO", "SET", "RETURNING", "RENAME",
}

# Keywords SQLite also accepts as plain names (columns called "first", "row", "range", ...).
NON_RESERVED = {
    "FIRST", "LAST", "ROW", "ROWS", "RANGE", "GROUPS", "CURRENT", "FILTER", "PARTITION", "PRECEDING",
    "FOLLOWING", "UNBOUNDED", "NULLS", "ASC", "DESC", "OVER", "WINDOW", "RECURSIVE", "ESCAPE", "MATCH",
    "REGEXP", "GLOB", "REPLACE", "MATERIALIZED", "EXCLUDE", "TIES", "OTHERS", "NO", "INDEXED", "TEMP",
    "VIEW", "DATABASE", "ABORT", "FAIL", "IGNORE", "CONFLICT", "VIRTUAL", "TABLE", "INDEX", "VALUES",
    "ANALYZE", "RENAME", "TRUE", "FALSE",
}

OPERATORS = ["->>", "||", "<=", ">=", "<>", "!=", "==", "<<", ">>", "->",
             "+", "-", "*", "/", "%", "=", "<", ">", "&", "|", "~"]
PUNCT = set("(),.;")


class SqlSyntaxError(ValueError):
    """The text is not valid SQL for this subset. pos is the offset where it went wrong."""

    def __init__(self, message: str, pos: int = 0):
        super().__init__(f"{message} (at character {pos})")
        self.pos = pos


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    value: str
    pos: int
    end: int

    def is_kw(self, *words: str) -> bool:
        return self.kind == "kw" and self.value in words

    def __repr__(self) -> str:  # compact in test failure messages
        return f"{self.kind}:{self.value}"


def _is_ident_start(ch: str) -> bool:
    return ch == "_" or ch.isalpha()


def _is_ident_part(ch: str) -> bool:
    return ch == "_" or ch == "$" or ch.isalnum()


def tokenize(sql: str) -> list[Token]:
    """Split SQL into tokens. Comments and whitespace are dropped; an eof token ends the list."""
    tokens: list[Token] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue
        if sql.startswith("--", i):
            nl = sql.find("\n", i)
            i = n if nl < 0 else nl + 1
            continue
        if sql.startswith("/*", i):
            close = sql.find("*/", i + 2)
            if close < 0:
                raise SqlSyntaxError("Unterminated /* comment", i)
            i = close + 2
            continue
        start = i
        if ch in "xX" and i + 1 < n and sql[i + 1] == "'":
            value, i = _quoted(sql, i + 1, "'", "'")
            if len(value) % 2 or any(c not in "0123456789abcdefABCDEF" for c in value):
                raise SqlSyntaxError("Malformed blob literal", start)
            tokens.append(Token("blob", value, start, i))
            continue
        if ch == "'":
            value, i = _quoted(sql, i, "'", "'")
            tokens.append(Token("str", value, start, i))
            continue
        if ch == '"':
            value, i = _quoted(sql, i, '"', '"')
            tokens.append(Token("qident", value, start, i))
            continue
        if ch == "`":
            value, i = _quoted(sql, i, "`", "`")
            tokens.append(Token("qident", value, start, i))
            continue
        if ch == "[":
            close = sql.find("]", i + 1)
            if close < 0:
                raise SqlSyntaxError("Unterminated [name]", i)
            tokens.append(Token("qident", sql[i + 1:close], start, close + 1))
            i = close + 1
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and sql[i + 1].isdigit()):
            i = _number_end(sql, i)
            if i < n and _is_ident_start(sql[i]):
                raise SqlSyntaxError("Malformed number", start)
            tokens.append(Token("num", sql[start:i], start, i))
            continue
        if _is_ident_start(ch):
            while i < n and _is_ident_part(sql[i]):
                i += 1
            word = sql[start:i]
            upper = word.upper()
            tokens.append(Token("kw", upper, start, i) if upper in KEYWORDS else Token("ident", word, start, i))
            continue
        if ch == "?":
            i += 1
            while i < n and sql[i].isdigit():
                i += 1
            tokens.append(Token("param", sql[start:i], start, i))
            continue
        if ch in ":@$" and i + 1 < n and _is_ident_start(sql[i + 1]):
            i += 1
            while i < n and _is_ident_part(sql[i]):
                i += 1
            tokens.append(Token("param", sql[start:i], start, i))
            continue
        if ch in PUNCT:
            tokens.append(Token("punct", ch, start, i + 1))
            i += 1
            continue
        for op in OPERATORS:
            if sql.startswith(op, i):
                tokens.append(Token("op", op, start, i + len(op)))
                i += len(op)
                break
        else:
            raise SqlSyntaxError(f"Unexpected character {ch!r}", i)
    tokens.append(Token("eof", "", n, n))
    return tokens


def _quoted(sql: str, i: int, open_: str, close: str) -> tuple[str, int]:
    """Read a quoted run starting at sql[i] == open_; a doubled close char is an escaped one."""
    out = []
    j = i + 1
    while True:
        k = sql.find(close, j)
        if k < 0:
            raise SqlSyntaxError(f"Unterminated {open_}...{close}", i)
        out.append(sql[j:k])
        if sql.startswith(close * 2, k):
            out.append(close)
            j = k + 2
            continue
        return "".join(out), k + 1


def _number_end(sql: str, i: int) -> int:
    n = len(sql)
    if sql.startswith(("0x", "0X"), i):
        j = i + 2
        while j < n and sql[j] in "0123456789abcdefABCDEF":
            j += 1
        if j == i + 2:
            raise SqlSyntaxError("Malformed hex number", i)
        return j
    j = i
    while j < n and (sql[j].isdigit() or sql[j] == "_"):
        j += 1
    if j < n and sql[j] == ".":
        j += 1
        while j < n and sql[j].isdigit():
            j += 1
    if j < n and sql[j] in "eE":
        k = j + 1
        if k < n and sql[k] in "+-":
            k += 1
        if k < n and sql[k].isdigit():
            while k < n and sql[k].isdigit():
                k += 1
            j = k
    return j
