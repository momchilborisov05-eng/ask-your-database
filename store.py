"""
The app's own database: an audit log of every question and query.

Who asked what, the SQL that ran, whether it was blocked or failed, how many
rows came back, and what the model call cost. It is the basis for each user's
history, the daily spending limits and the admin audit view.

It is kept apart from the database being asked about:
    sqlite:///askdb.db                                  local file (default)
    mssql://<server>.database.windows.net/<database>    Azure SQL, schema "askdb", signed in
                                                        as the app's own identity, which can
                                                        write here but not in the data schemas
"""

import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

from safe_sql import connect_mssql

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner         TEXT    NOT NULL,
    owner_name    TEXT    NOT NULL,
    kind          TEXT    NOT NULL CHECK (kind IN ('ask', 'sql')),
    question      TEXT    NOT NULL,
    sql_text      TEXT,
    status        TEXT    NOT NULL CHECK (status IN ('ok', 'error', 'blocked')),
    error         TEXT,
    row_count     INTEGER,
    attempts      INTEGER,
    model         TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    cost_usd      REAL,
    elapsed_ms    INTEGER,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_questions_owner ON questions(owner, created_at);
CREATE INDEX IF NOT EXISTS ix_questions_created ON questions(created_at);
"""

MSSQL_SCHEMA = [
    "IF SCHEMA_ID('askdb') IS NULL EXEC('CREATE SCHEMA askdb')",
    """IF OBJECT_ID('askdb.questions', 'U') IS NULL CREATE TABLE askdb.questions (
        id            INT IDENTITY(1,1) PRIMARY KEY,
        owner         NVARCHAR(200)  NOT NULL,
        owner_name    NVARCHAR(200)  NOT NULL,
        kind          VARCHAR(10)    NOT NULL CHECK (kind IN ('ask', 'sql')),
        question      NVARCHAR(MAX)  NOT NULL,
        sql_text      NVARCHAR(MAX),
        status        VARCHAR(10)    NOT NULL CHECK (status IN ('ok', 'error', 'blocked')),
        error         NVARCHAR(2000),
        row_count     INT,
        attempts      INT,
        model         VARCHAR(100),
        input_tokens  INT,
        output_tokens INT,
        cached_tokens INT,
        cost_usd      DECIMAL(12, 6),
        elapsed_ms    INT,
        created_at    DATETIME2(0)   NOT NULL
    )""",
    """IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_questions_owner')
        CREATE INDEX ix_questions_owner ON askdb.questions(owner, created_at)""",
    """IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_questions_created')
        CREATE INDEX ix_questions_created ON askdb.questions(created_at)""",
]

COLUMNS = ("owner", "owner_name", "kind", "question", "sql_text", "status", "error", "row_count", "attempts",
           "model", "input_tokens", "output_tokens", "cached_tokens", "cost_usd", "elapsed_ms")


class Store:
    def __init__(self, url: str):
        parsed = urlparse(url)
        if parsed.scheme == "sqlite":
            self.dialect, self.table = "sqlite", "questions"
            self.path = parsed.path[1:] or ":memory:"
        elif parsed.scheme == "mssql":
            database = parsed.path.lstrip("/")
            if not parsed.hostname or not database:
                raise ValueError("Use mssql://<server>.database.windows.net/<database>")
            self.dialect, self.table = "mssql", "askdb.questions"
            self.connection_string = (
                f"Server=tcp:{parsed.hostname},{parsed.port or 1433};Database={database};"
                "Encrypt=yes;TrustServerCertificate=no;"
                # The app's identity (AZURE_CLIENT_ID) in Azure, your Azure CLI login on a laptop.
                "Authentication=ActiveDirectoryDefault"
            )
        else:
            raise ValueError(f"Unsupported database URL: {url}")
        self._create_schema()

    def _connect(self):
        if self.dialect == "sqlite":
            conn = sqlite3.connect(self.path, timeout=15)
            conn.execute("PRAGMA journal_mode = WAL")
            return conn
        return connect_mssql(self.connection_string)

    def _create_schema(self):
        conn = self._connect()
        try:
            if self.dialect == "sqlite":
                conn.executescript(SQLITE_SCHEMA)
            else:
                cur = conn.cursor()
                for statement in MSSQL_SCHEMA:
                    cur.execute(statement)
            conn.commit()
        finally:
            conn.close()

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]
        finally:
            conn.close()

    def ping(self) -> bool:
        try:
            self._query("SELECT 1 AS ok")
            return True
        except Exception:
            return False

    def log(self, **values) -> int:
        """Record one question or query; returns its id."""
        row = {c: values.get(c) for c in COLUMNS}
        now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
        row["created_at"] = now.isoformat(sep=" ") if self.dialect == "sqlite" else now
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        returning = ("", " RETURNING id") if self.dialect == "sqlite" else (" OUTPUT INSERTED.id", "")
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"INSERT INTO {self.table} ({columns}){returning[0]} VALUES ({marks}){returning[1]}",
                        tuple(row.values()))
            new_id = int(cur.fetchone()[0])
            conn.commit()
            return new_id
        finally:
            conn.close()

    def get(self, question_id: int, owner: str | None = None) -> dict | None:
        sql, params = f"SELECT * FROM {self.table} WHERE id = ?", (question_id,)
        if owner is not None:
            sql, params = sql + " AND owner = ?", params + (owner,)
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def history(self, owner: str | None = None, limit: int = 50) -> list[dict]:
        """The latest questions of one user, or of everyone (the audit view)."""
        top, suffix = (f"TOP ({int(limit)}) ", "") if self.dialect == "mssql" else ("", f" LIMIT {int(limit)}")
        where, params = ("WHERE owner = ? ", (owner,)) if owner is not None else ("", ())
        return self._query(
            f"SELECT {top}id, owner_name, kind, question, sql_text, status, error, row_count, cost_usd, "
            f"elapsed_ms, created_at FROM {self.table} {where}ORDER BY id DESC{suffix}", params)

    def spent_since(self, since: datetime, owner: str | None = None) -> float:
        since_value = since.replace(tzinfo=None)
        if self.dialect == "sqlite":
            since_value = since_value.isoformat(sep=" ")
        sql, params = f"SELECT COALESCE(SUM(cost_usd), 0) AS spent FROM {self.table} WHERE created_at >= ?", (
            since_value,)
        if owner is not None:
            sql, params = sql + " AND owner = ?", params + (owner,)
        return float(self._query(sql, params)[0]["spent"])
