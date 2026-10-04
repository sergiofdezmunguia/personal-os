"""Log de eventos de dominio (append-only) y contexto de cambio.

Cada cambio relevante del sistema deja un evento con su actor y su correlation_id.
Es la auditoría interna y, en el futuro, el mecanismo por el que otros módulos reaccionan
sin acoplarse.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from personal_os.core.clock import Clock, to_iso
from personal_os.core.db import Database
from personal_os.core.ids import new_id

ACTORS = ("cli", "apple", "system")


@dataclass(frozen=True)
class ChangeContext:
    """Quién origina un cambio y a qué operación pertenece."""

    actor: str
    correlation_id: str = field(default_factory=lambda: new_id("correlation"))

    @classmethod
    def cli(cls) -> ChangeContext:
        return cls(actor="cli")

    @classmethod
    def apple(cls, correlation_id: str) -> ChangeContext:
        return cls(actor="apple", correlation_id=correlation_id)

    @classmethod
    def system(cls, correlation_id: str | None = None) -> ChangeContext:
        return cls(actor="system", correlation_id=correlation_id or new_id("correlation"))


@dataclass(frozen=True)
class DomainEvent:
    seq: int
    id: str
    type: str
    entity_type: str
    entity_id: str
    actor: str
    correlation_id: str | None
    payload: dict[str, Any]
    occurred_at: str


class EventLog:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def record(
        self,
        ctx: ChangeContext,
        type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        event_id = new_id("event")
        self.db.execute(
            "INSERT INTO events(id, type, entity_type, entity_id, actor, correlation_id,"
            " payload, occurred_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                event_id,
                type,
                entity_type,
                entity_id,
                ctx.actor,
                ctx.correlation_id,
                json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                to_iso(self.clock.now()),
            ),
        )
        return event_id

    def query(
        self,
        *,
        entity_id: str | None = None,
        type_prefix: str | None = None,
        correlation_id: str | None = None,
        limit: int = 50,
    ) -> list[DomainEvent]:
        where, params = [], []
        if entity_id:
            where.append("entity_id = ?")
            params.append(entity_id)
        if type_prefix:
            where.append("type LIKE ?")
            params.append(type_prefix + "%")
        if correlation_id:
            where.append("correlation_id = ?")
            params.append(correlation_id)
        sql = "SELECT * FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        return [
            DomainEvent(
                seq=r["seq"],
                id=r["id"],
                type=r["type"],
                entity_type=r["entity_type"],
                entity_id=r["entity_id"],
                actor=r["actor"],
                correlation_id=r["correlation_id"],
                payload=json.loads(r["payload"]),
                occurred_at=r["occurred_at"],
            )
            for r in self.db.all(sql, params)
        ]
