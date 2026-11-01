"""
Runs SQL safely, read-only, on the database being asked about: a SQLite file
or Azure SQL / SQL Server. Every query has a row limit and a time limit.

SQLite (a local file), three independent layers:

1. The database file is opened in read-only mode, so SQLite itself refuses writes.
2. An authorizer allows only reading tables and calling functions. INSERT, UPDATE,
   DELETE, DROP, ALTER, PRAGMA, ATTACH and everything else are denied.
3. Only one statement can run per call (sqlite3 refuses "SELECT 1; DROP TABLE x").

Azure SQL / SQL Server, four independent layers:

1. The app signs in as a database user that can only SELECT from the allowed
   schemas (infra/grant_sql_access.py creates it). check_read_only() asks the
   server for the login's effective permissions at startup; in production the
   app refuses to run queries if the login could write.
2. A guard reads the query before it is sent: one statement, SELECT or WITH
   only, and none of the keywords that change data or reach outside the
   database (INSERT, EXEC, INTO, OPENROWSET, ...), ignoring strings and comments.
3. Every query runs in a transaction that is always rolled back.
4. A query timeout set on the connection, and only the first rows are fetched.
"""

import datetime as dt
import decimal
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),  # WITH RECURSIVE ...
}

# T-SQL keywords that write, change settings, run code or read outside the database.
FORBIDDEN = {
    "insert", "update", "delete", "merge", "drop", "alter", "create", "truncate", "exec", "execute",
    "grant", "revoke", "deny", "into", "openrowset", "openquery", "opendatasource", "openxml", "bulk",
    "backup", "restore", "shutdown", "kill", "dbcc", "waitfor", "use", "declare", "set", "reconfigure",
    "checkpoint", "readtext", "writetext", "updatetext", "external",
}
WRITE_PERMISSIONS = {"INSERT", "UPDATE", "DELETE", "ALTER", "CONTROL", "EXECUTE", "TAKE OWNERSHIP"}


class UnsafeQuery(ValueError):
    """The query tried to do something other than read data, or it failed."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    truncated: bool
    elapsed_ms: int


def open_source(url: str, schemas: list[str] | None = None, reader_client_id: str | None = None):
    """sqlite:///shop.db, or mssql://<server>.database.windows.net/<database>."""
    parsed = urlparse(url)
    if parsed.scheme == "sqlite":
        return SqliteSource(Path(parsed.path[1:]))
    if parsed.scheme == "mssql":
        options = parse_qs(parsed.query)
        return MssqlSource(parsed.hostname, parsed.port or 1433, parsed.path.lstrip("/"),
                           schemas or options.get("schemas", ["dbo"])[0].split(","), reader_client_id)
    raise ValueError(f"Unsupported database URL: {url}")


# ---------- SQLite ----------

def _authorizer(action, *_args):
    return sqlite3.SQLITE_OK if action in ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.set_authorizer(_authorizer)
    return conn


class SqliteSource:
    dialect = "sqlite"
    label = "SQLite"

    def __init__(self, path: Path):
        if not Path(path).exists():
            raise FileNotFoundError(f"{path} not found. Run: python seed_db.py")
        self.path = Path(path)
        self.name = self.path.name

    def check_read_only(self) -> bool:
        return True  # opened with mode=ro and an authorizer: it can't write

    def run(self, sql: str, max_rows: int = 500, timeout_s: float = 5.0) -> QueryResult:
        sql = _strip(sql)
        started = time.monotonic()
        conn = connect_readonly(self.path)
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
        return _result(columns, rows, max_rows, started)

    def describe(self) -> str:
        """
        A text description of the database for the AI: every CREATE TABLE statement,
        row counts, and the possible values of short text columns (statuses, cities...),
        so the model writes queries with the right column names and values.
        """
        conn = connect_readonly(self.path)
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
                        low, high = conn.execute(
                            f'SELECT MIN("{col_name}"), MAX("{col_name}") FROM "{name}"').fetchone()
                        parts.append(f"-- {name}.{col_name} ranges from {low} to {high}")
                parts.append("")
            return "\n".join(parts)
        finally:
            conn.close()


# ---------- Azure SQL / SQL Server ----------

def check_select(sql: str) -> str:
    """The guard for T-SQL: raise UnsafeQuery unless this is one read-only SELECT."""
    sql = _strip(sql)
    code = _code_only(sql)
    if ";" in code:
        raise UnsafeQuery("Only one statement can run at a time.")
    words = re.findall(r"[a-z_@#][a-z0-9_@#$]*", code.lower())
    if not words or words[0] not in {"select", "with"}:
        raise UnsafeQuery("Only SELECT queries are allowed.")
    bad = sorted({w for w in words if w in FORBIDDEN or w.startswith(("xp_", "sp_", "@@"))})
    if bad:
        raise UnsafeQuery(f"This query is not allowed: only reading data is permitted ({', '.join(bad)}).")
    return sql


def _code_only(sql: str) -> str:
    """The query with comments, string literals and quoted names replaced by spaces."""
    return re.sub(r"--[^\n]*|/\*.*?\*/|N?'(?:[^']|'')*'|\[(?:[^\]]|\]\])*\]|\"(?:[^\"]|\"\")*\"",
                  " ", sql, flags=re.S)


class MssqlSource:
    dialect = "mssql"
    label = "Azure SQL"

    def __init__(self, host: str, port: int, database: str, schemas: list[str],
                 reader_client_id: str | None = None):
        if not host or not database:
            raise ValueError("Use mssql://<server>.database.windows.net/<database>?schemas=sales,dbo")
        for schema in schemas:
            if not re.fullmatch(r"[A-Za-z0-9_]{1,128}", schema):
                raise ValueError(f"Unexpected schema name: {schema}")
        self.host, self.port, self.database, self.schemas = host, port, database, schemas
        self.name = f"{database} ({', '.join(schemas)})"
        self.reader_client_id = reader_client_id

    def _credential(self):
        # The read-only identity in Azure; your own Azure CLI login on a laptop.
        from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

        if self.reader_client_id:
            return ManagedIdentityCredential(client_id=self.reader_client_id)
        return DefaultAzureCredential(exclude_interactive_browser_credential=True)

    def connect(self):
        return connect_mssql(
            f"Server=tcp:{self.host},{self.port};Database={self.database};"
            "Encrypt=yes;TrustServerCertificate=no;ApplicationIntent=ReadOnly",
            token_provider=self._credential(),
        )

    def check_read_only(self) -> bool:
        """True if the login has no permission that changes data or objects."""
        conn = self.connect()
        try:
            cur = conn.cursor()
            granted = set()
            cur.execute("SELECT permission_name FROM fn_my_permissions(NULL, 'DATABASE')")
            granted |= {r[0] for r in cur.fetchall()}
            for schema in self.schemas:
                cur.execute("SELECT permission_name FROM fn_my_permissions(?, 'SCHEMA')", (schema,))
                granted |= {r[0] for r in cur.fetchall()}
            return not any(p in WRITE_PERMISSIONS or p.startswith(("CREATE ", "ALTER ")) for p in granted)
        finally:
            conn.close()

    def run(self, sql: str, max_rows: int = 500, timeout_s: float = 5.0) -> QueryResult:
        sql = check_select(sql)
        started = time.monotonic()
        conn = self.connect()
        conn.timeout = max(1, int(timeout_s))  # query timeout, in seconds
        try:
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchmany(max_rows + 1)
            columns = [d[0] for d in cur.description or []]
        except Exception as e:  # the driver's errors carry the server's message
            message = str(e)
            if "timeout" in message.lower() or "HYT00" in message:
                raise UnsafeQuery(f"The query took longer than {timeout_s:.0f} seconds and was stopped.") from e
            if "permission was denied" in message.lower():
                raise UnsafeQuery("This query is not allowed: it reads data outside the allowed tables.") from e
            raise UnsafeQuery(f"SQL error: {_server_message(message)}") from e
        finally:
            try:
                conn.rollback()  # nothing a query did is ever kept
            finally:
                conn.close()
        return _result(columns, rows, max_rows, started)

    def describe(self) -> str:
        """Tables and views of the allowed schemas, keys, row counts and short text values."""
        conn = self.connect()
        conn.timeout = 30
        try:
            cur = conn.cursor()
            marks = ", ".join("?" for _ in self.schemas)
            cur.execute(
                "SELECT c.TABLE_SCHEMA, c.TABLE_NAME, t.TABLE_TYPE, c.COLUMN_NAME, c.DATA_TYPE, "
                "c.CHARACTER_MAXIMUM_LENGTH, c.IS_NULLABLE "
                "FROM INFORMATION_SCHEMA.COLUMNS c JOIN INFORMATION_SCHEMA.TABLES t "
                "ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME "
                f"WHERE c.TABLE_SCHEMA IN ({marks}) ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.ORDINAL_POSITION",
                tuple(self.schemas))
            tables: dict[tuple, list] = {}
            for schema, table, kind, column, dtype, length, nullable in cur.fetchall():
                tables.setdefault((schema, table, kind), []).append((column, dtype, length, nullable))
            cur.execute(
                "SELECT OBJECT_SCHEMA_NAME(f.parent_object_id), OBJECT_NAME(f.parent_object_id), pc.name, "
                "OBJECT_SCHEMA_NAME(f.referenced_object_id), OBJECT_NAME(f.referenced_object_id), rc.name "
                "FROM sys.foreign_key_columns f "
                "JOIN sys.columns pc ON pc.object_id = f.parent_object_id AND pc.column_id = f.parent_column_id "
                "JOIN sys.columns rc "
                "ON rc.object_id = f.referenced_object_id AND rc.column_id = f.referenced_column_id")
            keys: dict[tuple, list[str]] = {}
            for schema, table, column, ref_schema, ref_table, ref_column in cur.fetchall():
                keys.setdefault((schema, table), []).append(
                    f"{column} -> {ref_schema}.{ref_table}.{ref_column}")

            parts = []
            for (schema, table, kind), columns in tables.items():
                name = f"{_quote(schema)}.{_quote(table)}"
                lines = [f"    {_quote(c)} {_type(d, n)}{'' if null == 'YES' else ' NOT NULL'}"
                         for c, d, n, null in columns]
                parts.append(f"{'VIEW' if kind == 'VIEW' else 'TABLE'} {schema}.{table} (\n"
                             + ",\n".join(lines) + "\n)")
                cur.execute(f"SELECT COUNT_BIG(*) FROM {name}")
                parts.append(f"-- {cur.fetchone()[0]} rows")
                for key in keys.get((schema, table), []):
                    parts.append(f"-- foreign key: {key}")
                for column, dtype, length, _ in columns:
                    if dtype in {"varchar", "nvarchar", "char", "nchar"} and length and 0 < length <= 60:
                        cur.execute(f"SELECT DISTINCT TOP 21 {_quote(column)} FROM {name} ORDER BY 1")
                        values = [r[0] for r in cur.fetchall()]
                        if 1 < len(values) <= 20:
                            parts.append(f"-- {schema}.{table}.{column} values: "
                                         + ", ".join(repr(v) for v in values))
                    elif dtype in {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset"}:
                        cur.execute(f"SELECT MIN({_quote(column)}), MAX({_quote(column)}) FROM {name}")
                        low, high = cur.fetchone()
                        parts.append(f"-- {schema}.{table}.{column} ranges from {_plain(low)} to {_plain(high)}")
                parts.append("")
            return "\n".join(parts)
        finally:
            conn.rollback()
            conn.close()


def connect_mssql(connection_string: str, attempts: int = 8, **kwargs):
    """Connect to Azure SQL, waiting while a paused serverless database resumes.

    An auto-paused database rejects the first logins with "is not currently
    available" for up to about a minute while it wakes up.
    """
    import mssql_python  # only needed for Azure SQL

    for attempt in range(1, attempts + 1):
        try:
            return mssql_python.connect(connection_string, timeout=30, **kwargs)
        except mssql_python.OperationalError as e:
            if "not currently available" not in str(e) or attempt == attempts:
                raise
            if attempt == 1:
                print("Database is resuming from auto-pause, waiting...", file=sys.stderr)
            time.sleep(min(5 * attempt, 20))


def _quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _type(dtype: str, length) -> str:
    if length is None:
        return dtype
    return f"{dtype}({'max' if length == -1 else length})"


def _server_message(message: str) -> str:
    """The SQL Server error text without the driver's prefixes."""
    found = re.findall(r"\[SQL Server\]([^\[]+)", message)
    return found[-1].strip() if found else message.strip()[:500]


# ---------- shared ----------

def _strip(sql: str) -> str:
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise UnsafeQuery("The query is empty.")
    if not sql.lower().startswith(("select", "with")):
        raise UnsafeQuery("Only SELECT queries are allowed.")
    return sql


def _plain(value):
    """Database values as JSON-friendly Python values."""
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ", timespec="minutes" if not value.second else "seconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return value


def _result(columns: list[str], rows: list, max_rows: int, started: float) -> QueryResult:
    return QueryResult(
        columns=columns,
        rows=[[_plain(v) for v in r] for r in rows[:max_rows]],
        truncated=len(rows) > max_rows,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
