"""
sqlkit: the engineered core of Ask Your Database, written by hand with no SQL library.

- tokenizer.py  SQLite tokens with positions (strings, comments, quoted names, numbers)
- parser.py     recursive-descent parser for the read-only SELECT subset -> AST
- printer.py    AST -> canonical SQL (used for rewrites and round-trip tests)
- policy.py     static verifier: one read-only SELECT, table/column/function allow-lists,
                PII deny-list, complexity limits, then LIMIT injection / clamping
- catalog.py    schema catalog read from SQLite (tables, columns, foreign keys, values)
- linker.py     schema linking: question words -> tables, columns, values, join path
- nl2sql.py     offline NL -> SQL: question -> semantic frame -> SQL via a semantic layer
- evaluate.py   execution accuracy (result-set comparison) and the red-team harness
"""

from sqlkit.parser import SqlSyntaxError, parse
from sqlkit.policy import Policy, PolicyViolation, Verdict
from sqlkit.tokenizer import tokenize

__all__ = ["Policy", "PolicyViolation", "SqlSyntaxError", "Verdict", "parse", "tokenize"]
