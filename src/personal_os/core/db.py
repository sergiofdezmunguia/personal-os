"""SQLite: conexión, transacciones y migraciones por módulo.

Cada módulo es dueño de sus tablas y aporta su propio directorio de migraciones
(`<modulo>/migrations/NNNN_nombre.sql`). El runner registra lo aplicado en
`schema_migrations(namespace, version)`.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any

_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._depth = 0

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[Database]:
        """Transacción anidable: solo la más externa hace BEGIN/COMMIT."""
        if self._depth == 0:
            self._conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield self
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self._conn.execute("ROLLBACK")
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    # --- migraciones -----------------------------------------------------------------

    def migrate(self, namespaces: Sequence[tuple[str, str]]) -> list[str]:
        """Aplica migraciones pendientes. `namespaces` = [(nombre, paquete_con_migrations)]."""
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " namespace TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL,"
            " applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),"
            " PRIMARY KEY (namespace, version))"
        )
        applied: list[str] = []
        for namespace, package in namespaces:
            done = {
                r["version"]
                for r in self.all(
                    "SELECT version FROM schema_migrations WHERE namespace = ?", (namespace,)
                )
            }
            folder = resources.files(package).joinpath("migrations")
            files = sorted(
                (f.name for f in folder.iterdir() if _MIGRATION_RE.match(f.name)),
            )
            for name in files:
                version = int(name[:4])
                if version in done:
                    continue
                sql = folder.joinpath(name).read_text(encoding="utf-8")
                with self.transaction():
                    for statement in _split_sql(sql):
                        self._conn.execute(statement)
                    self._conn.execute(
                        "INSERT INTO schema_migrations(namespace, version, name) VALUES (?,?,?)",
                        (namespace, version, name),
                    )
                applied.append(f"{namespace}/{name}")
        return applied


def _split_sql(script: str) -> list[str]:
    """Divide un script en sentencias (nuestras migraciones no usan triggers con ';')."""
    lines = [ln for ln in script.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
