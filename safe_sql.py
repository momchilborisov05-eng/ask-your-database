"""
Runs SQL safely on a SQLite database: read-only, one SELECT at a time, with a
row limit and a time limit. Three independent layers protect the data:

1. The database file is opened in read-only mode, so SQLite itself refuses writes.
2. An authorizer allows only reading tables and calling functions. INSERT, UPDATE,
   DELETE, DROP, ALTER, PRAGMA, ATTACH and everything else are denied.
3. Only one statement can run per call (sqlite3 refuses "SELECT 1; DROP TABLE x").

A progress handler stops queries that run longer than the time limit.
"""

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),  # WITH RECURSIVE ...
}


class UnsafeQuery(ValueError):
    """The query tried to do something other than read data, or it failed."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    truncated: bool
    elapsed_ms: int


def _authorizer(action, *_args):
    return sqlite3.SQLITE_OK if action in ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.set_authorizer(_authorizer)
    return conn


def run_query(db_path: Path, sql: str, max_rows: int = 500, timeout_s: float = 5.0) -> QueryResult:
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise UnsafeQuery("The query is empty.")
    if not sql.lower().startswith(("select", "with")):
        raise UnsafeQuery("Only SELECT queries are allowed.")

    started = time.monotonic()
    conn = connect_readonly(db_path)
    # Called every 10,000 SQLite steps; returning True aborts the query.
    conn.set_progress_handler(lambda: time.monotonic() - started > timeout_s, 10_000)
    try:
        cursor = conn.execute(sql)
        rows = cursor.fetchmany(max_rows + 1)
        columns = [d[0] for d in cursor.description or []]
    except sqlite3.DatabaseError as e:
        message = str(e)
        if "not authorized" in message or "readonly" in message:
            raise UnsafeQuery("This query is not allowed: only reading data is permitted.") from e
        if "interrupted" in message:
            raise UnsafeQuery(f"The query took longer than {timeout_s:.0f} seconds and was stopped.") from e
        raise UnsafeQuery(f"SQL error: {message}") from e
    except sqlite3.ProgrammingError as e:  # e.g. more than one statement
        raise UnsafeQuery(f"SQL error: {e}") from e
    finally:
        conn.close()

    truncated = len(rows) > max_rows
    return QueryResult(
        columns=columns,
        rows=[list(r) for r in rows[:max_rows]],
        truncated=truncated,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def describe_schema(db_path: Path) -> str:
    """
    A text description of the database for the AI: every CREATE TABLE statement,
    row counts, and the possible values of short text columns (statuses, cities...),
    so the model writes queries with the right column names and values.
    """
    conn = connect_readonly(db_path)
    try:
        conn.set_authorizer(None)  # this function reads sqlite_master and runs PRAGMA table_info
        parts = []
        tables = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for name, create_sql in tables:
            count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            parts.append(f"{create_sql};\n-- {count} rows")
            for col in conn.execute(f'PRAGMA table_info("{name}")').fetchall():
                col_name, col_type = col[1], (col[2] or "").upper()
                if col_type != "TEXT":
                    continue
                values = conn.execute(
                    f'SELECT DISTINCT "{col_name}" FROM "{name}" ORDER BY 1 LIMIT 21'
                ).fetchall()
                if 1 < len(values) <= 20:
                    parts.append(f"-- {name}.{col_name} values: " + ", ".join(repr(v[0]) for v in values))
                elif col_name.endswith(("_at", "signed_up")):
                    low, high = conn.execute(f'SELECT MIN("{col_name}"), MAX("{col_name}") FROM "{name}"').fetchone()
                    parts.append(f"-- {name}.{col_name} ranges from {low} to {high}")
            parts.append("")
        return "\n".join(parts)
    finally:
        conn.close()
