"""Persistencia del módulo sync: enlaces externos, operaciones, entradas procesadas y ejecuciones."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from personal_os.core.clock import Clock, to_iso
from personal_os.core.db import Database


def state_hash(data: Any) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


DELETED = "deleted"  # valor especial de last_pushed_hash: pedimos borrar el objeto


@dataclass
class Link:
    provider: str
    entity_type: str
    entity_id: str
    external_id: str | None = None
    etag: str | None = None
    last_pushed_hash: str | None = None
    last_pushed_state: dict | None = None
    pushed_at: str | None = None
    last_seen_hash: str | None = None
    last_seen_state: dict | None = None
    last_seen_marker: str | None = None
    sync_state: str = "pending_push"
    pending_op_id: str | None = None
    last_error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


_JSON_FIELDS = ("last_pushed_state", "last_seen_state")


class LinkStore:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def _from_row(self, row) -> Link:
        data = dict(row)
        for f in _JSON_FIELDS:
            data[f] = json.loads(data[f]) if data[f] else None
        return Link(**data)

    def get(self, provider: str, entity_type: str, entity_id: str) -> Link | None:
        row = self.db.one(
            "SELECT * FROM external_links WHERE provider=? AND entity_type=? AND entity_id=?",
            (provider, entity_type, entity_id),
        )
        return self._from_row(row) if row else None

    def by_external_id(self, provider: str, external_id: str) -> Link | None:
        row = self.db.one(
            "SELECT * FROM external_links WHERE provider=? AND external_id=?",
            (provider, external_id),
        )
        return self._from_row(row) if row else None

    def all(self, provider: str, entity_type: str | None = None) -> list[Link]:
        sql, params = "SELECT * FROM external_links WHERE provider=?", [provider]
        if entity_type:
            sql += " AND entity_type=?"
            params.append(entity_type)
        return [self._from_row(r) for r in self.db.all(sql + " ORDER BY created_at", params)]

    def save(self, link: Link) -> Link:
        now = to_iso(self.clock.now())
        link.created_at = link.created_at or now
        link.updated_at = now
        if link.external_id:
            # Un objeto externo solo puede estar enlazado a una entidad.
            self.db.execute(
                "UPDATE external_links SET external_id=NULL, sync_state='orphaned'"
                " WHERE provider=? AND external_id=? AND NOT (entity_type=? AND entity_id=?)",
                (link.provider, link.external_id, link.entity_type, link.entity_id),
            )
        data = dict(link.__dict__)
        for f in _JSON_FIELDS:
            data[f] = json.dumps(data[f], ensure_ascii=False, sort_keys=True) if data[f] else None
        cols = ", ".join(data)
        placeholders = ", ".join(f":{k}" for k in data)
        self.db.execute(
            f"INSERT OR REPLACE INTO external_links ({cols}) VALUES ({placeholders})", data
        )
        return link

    def conflicts(self) -> list[Link]:
        return [
            self._from_row(r)
            for r in self.db.all(
                "SELECT * FROM external_links WHERE sync_state IN ('conflict','error') ORDER BY updated_at"
            )
        ]


@dataclass
class Operation:
    op_id: str
    provider: str
    operation: str
    entity_type: str
    entity_id: str
    external_id: str | None
    batch_id: str | None
    request: dict
    status: str
    result: dict | None
    correlation_id: str | None
    created_at: str
    updated_at: str


class OperationLog:
    """Registro de todo lo que el Personal OS envía a un proveedor externo."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def record(
        self,
        *,
        op_id: str,
        provider: str,
        operation: str,
        entity_type: str,
        entity_id: str,
        external_id: str | None,
        request: dict,
        status: str,
        batch_id: str | None = None,
        correlation_id: str | None = None,
        result: dict | None = None,
    ) -> None:
        now = to_iso(self.clock.now())
        self.db.execute(
            "INSERT INTO external_operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                op_id,
                provider,
                operation,
                entity_type,
                entity_id,
                external_id,
                batch_id,
                json.dumps(request, ensure_ascii=False, sort_keys=True),
                status,
                json.dumps(result, ensure_ascii=False, sort_keys=True) if result else None,
                correlation_id,
                now,
                now,
            ),
        )

    def get(self, op_id: str) -> Operation | None:
        row = self.db.one("SELECT * FROM external_operations WHERE op_id=?", (op_id,))
        if row is None:
            return None
        data = dict(row)
        data["request"] = json.loads(data["request"])
        data["result"] = json.loads(data["result"]) if data["result"] else None
        return Operation(**data)

    def finish(
        self, op_id: str, status: str, result: dict | None = None, external_id: str | None = None
    ) -> None:
        self.db.execute(
            "UPDATE external_operations SET status=?, result=?, external_id=COALESCE(?, external_id),"
            " updated_at=? WHERE op_id=?",
            (
                status,
                json.dumps(result, ensure_ascii=False, sort_keys=True) if result else None,
                external_id,
                to_iso(self.clock.now()),
                op_id,
            ),
        )

    def recent(self, limit: int = 50, entity_id: str | None = None) -> list[Operation]:
        sql, params = "SELECT op_id FROM external_operations", []
        if entity_id:
            sql += " WHERE entity_id=?"
            params.append(entity_id)
        rows = self.db.all(sql + " ORDER BY created_at DESC, rowid DESC LIMIT ?", [*params, limit])
        return [self.get(r["op_id"]) for r in rows]  # type: ignore[misc]


class InboxLedger:
    """Qué acks/snapshots del buzón ya se procesaron (no se pueden borrar desde Windows)."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def seen(self, provider: str, kind: str, item_id: str) -> bool:
        return (
            self.db.one(
                "SELECT 1 FROM sync_inbox_processed WHERE provider=? AND kind=? AND item_id=?",
                (provider, kind, item_id),
            )
            is not None
        )

    def mark(self, provider: str, kind: str, item_id: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO sync_inbox_processed VALUES (?,?,?,?)",
            (provider, kind, item_id, to_iso(self.clock.now())),
        )


class Cursors:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, provider: str, key: str) -> str | None:
        row = self.db.one(
            "SELECT value FROM sync_cursors WHERE provider=? AND key=?", (provider, key)
        )
        return row["value"] if row else None

    def set(self, provider: str, key: str, value: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO sync_cursors(provider, key, value) VALUES (?,?,?)",
            (provider, key, value),
        )


@dataclass
class SyncReport:
    run_id: str
    provider: str
    stats: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def inc(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n


class SyncRuns:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def start(self, run_id: str, provider: str) -> None:
        self.db.execute(
            "INSERT INTO sync_runs(id, provider, started_at, status) VALUES (?,?,?, 'running')",
            (run_id, provider, to_iso(self.clock.now())),
        )

    def finish(self, report: SyncReport, error: str | None = None) -> None:
        status = "error" if (error or report.errors) else "ok"
        self.db.execute(
            "UPDATE sync_runs SET finished_at=?, status=?, stats=?, error=? WHERE id=?",
            (
                to_iso(self.clock.now()),
                status,
                json.dumps(
                    {"stats": report.stats, "warnings": report.warnings, "errors": report.errors},
                    ensure_ascii=False,
                ),
                error,
                report.run_id,
            ),
        )

    def recent(self, limit: int = 10) -> list[dict]:
        rows = self.db.all(
            "SELECT * FROM sync_runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,)
        )
        out = []
        for r in rows:
            d = dict(r)
            d["stats"] = json.loads(d["stats"])
            out.append(d)
        return out
