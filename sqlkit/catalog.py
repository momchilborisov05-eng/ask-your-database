"""The schema catalog: tables, columns, foreign keys, row counts and short text values."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Catalog:
    tables: dict[str, list[str]]                       # table -> columns, in order
    types: dict[tuple[str, str], str] = field(default_factory=dict)
    foreign_keys: list[tuple[str, str, str, str]] = field(default_factory=list)  # t.c -> rt.rc
    row_counts: dict[str, int] = field(default_factory=dict)
    values: dict[tuple[str, str], list[str]] = field(default_factory=dict)       # categorical text values

    @classmethod
    def from_sqlite(cls, path: str | Path, max_values: int = 40) -> Catalog:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            cat = cls(tables={})
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name")]
            for table in names:
                q = '"' + table.replace('"', '""') + '"'
                cols = conn.execute(f"PRAGMA table_info({q})").fetchall()
                cat.tables[table] = [c[1] for c in cols]
                for c in cols:
                    cat.types[(table, c[1])] = (c[2] or "").upper()
                for fk in conn.execute(f"PRAGMA foreign_key_list({q})").fetchall():
                    cat.foreign_keys.append((table, fk[3], fk[2], fk[4] or "id"))
                cat.row_counts[table] = conn.execute(f"SELECT COUNT(*) FROM {q}").fetchone()[0]
                for c in cols:
                    if (c[2] or "").upper() != "TEXT":
                        continue
                    cq = '"' + c[1].replace('"', '""') + '"'
                    vals = conn.execute(f"SELECT DISTINCT {cq} FROM {q} WHERE {cq} IS NOT NULL "
                                        f"LIMIT {max_values + 1}").fetchall()
                    if 1 < len(vals) <= max_values and all(len(str(v[0])) <= 40 for v in vals):
                        cat.values[(table, c[1])] = sorted(str(v[0]) for v in vals)
            return cat
        finally:
            conn.close()

    def columns_lower(self) -> dict[str, set[str]]:
        return {t.lower(): {c.lower() for c in cols} for t, cols in self.tables.items()}

    def neighbours(self) -> dict[str, list[tuple[str, str, str, str]]]:
        """Undirected foreign-key graph: table -> [(table, column, other_table, other_column)]."""
        graph: dict[str, list] = {t: [] for t in self.tables}
        for t, c, rt, rc in self.foreign_keys:
            graph.setdefault(t, []).append((t, c, rt, rc))
            graph.setdefault(rt, []).append((rt, rc, t, c))
        return graph
