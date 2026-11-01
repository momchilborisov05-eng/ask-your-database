"""
Give the app's two managed identities their database rights, and nothing more.

Runs as you (Azure CLI login, which must be the server's Entra admin):

    python infra/grant_sql_access.py <server>.database.windows.net <database> \\
        --app id-askdb-app --reader id-askdb-reader --schemas shop

- The reader identity runs the SQL that Claude writes: it gets SELECT on the
  listed schemas only. It can't write, can't create anything, and can't read
  other schemas (for example the app's own audit log).
- The app identity keeps the audit log: it may create and change tables in its
  own schema askdb, and has no rights on the data schemas.

Running it again is safe: existing users and grants are left as they are.
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safe_sql import connect_mssql  # noqa: E402

NAME = re.compile(r"[A-Za-z0-9_-]{1,128}")
WRITER_ROLES = ("db_owner", "db_datawriter", "db_ddladmin")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("server")
    parser.add_argument("database")
    parser.add_argument("--app", required=True, help="the app identity's name (audit log)")
    parser.add_argument("--reader", required=True, help="the reader identity's name (runs the questions' SQL)")
    parser.add_argument("--schemas", default="shop", help="comma-separated schemas the reader may SELECT from")
    args = parser.parse_args()
    schemas = [s.strip() for s in args.schemas.split(",") if s.strip()]
    for name in [args.app, args.reader, *schemas]:
        if not NAME.fullmatch(name):
            sys.exit(f"Unexpected name: {name}")

    conn = connect_mssql(
        f"Server=tcp:{args.server},1433;Database={args.database};Encrypt=yes;"
        "TrustServerCertificate=no;Authentication=ActiveDirectoryDefault"
    )
    cur = conn.cursor()
    for identity in (args.app, args.reader):
        cur.execute("SELECT 1 FROM sys.database_principals WHERE name = ?", (identity,))
        if not cur.fetchone():
            cur.execute(f"CREATE USER [{identity}] FROM EXTERNAL PROVIDER")

    # The reader: SELECT on the data schemas only.
    for schema in schemas:
        cur.execute("SELECT 1 FROM sys.schemas WHERE name = ?", (schema,))
        if not cur.fetchone():
            sys.exit(f"Schema '{schema}' doesn't exist in {args.database}.")
        cur.execute(f"GRANT SELECT ON SCHEMA::[{schema}] TO [{args.reader}]")
    cur.execute(
        "SELECT r.name FROM sys.database_role_members m "
        "JOIN sys.database_principals r ON r.principal_id = m.role_principal_id "
        "JOIN sys.database_principals u ON u.principal_id = m.member_principal_id WHERE u.name = ?",
        (args.reader,))
    risky = [r[0] for r in cur.fetchall() if r[0] in WRITER_ROLES]
    if risky:
        print(f"Warning: '{args.reader}' is in {', '.join(risky)} and could write. Remove it from those roles; "
              "the app refuses to run queries while REQUIRE_READ_ONLY=1.")

    # The app: its own schema for the audit log.
    cur.execute("IF SCHEMA_ID('askdb') IS NULL EXEC('CREATE SCHEMA askdb')")
    cur.execute(f"GRANT CREATE TABLE TO [{args.app}]")
    cur.execute(f"GRANT ALTER, SELECT, INSERT, UPDATE, DELETE, REFERENCES ON SCHEMA::askdb TO [{args.app}]")

    conn.commit()
    conn.close()
    print(f"'{args.reader}' can read {', '.join(schemas)}; '{args.app}' can keep the audit log in askdb.")


if __name__ == "__main__":
    main()
