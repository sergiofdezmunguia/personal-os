"""Ports: interfaces que el Core espera de los proveedores externos, y sus DTOs.

Los adaptadores (p. ej. Apple) implementan estos protocolos. Ni el dominio ni el motor de
sincronización conocen detalles de Apple; los adaptadores no conocen el dominio.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

# --------------------------------------------------------------------------------------
# Recordatorios


@dataclass(frozen=True)
class Due:
    date: str  # YYYY-MM-DD (hora local)
    time: str | None = None  # HH:MM (hora local) o None si es todo el día

    def to_json(self) -> dict:
        return {"date": self.date, "time": self.time}

    @classmethod
    def from_json(cls, data: dict | None) -> Due | None:
        if not data:
            return None
        return cls(date=data["date"], time=data.get("time"))


@dataclass(frozen=True)
class ReminderState:
    title: str
    notes: str
    due: Due | None
    is_completed: bool

    def to_json(self) -> dict:
        return {
            "title": self.title,
            "notes": self.notes,
            "due": self.due.to_json() if self.due else None,
            "is_completed": self.is_completed,
        }

    @classmethod
    def from_json(cls, data: dict) -> ReminderState:
        return cls(
            title=data.get("title", ""),
            notes=(data.get("notes") or "").rstrip(),
            due=Due.from_json(data.get("due")),
            is_completed=bool(data.get("is_completed")),
        )


@dataclass(frozen=True)
class ReminderUpsert:
    op_id: str
    marker: str
    external_id: str | None
    expected: ReminderState | None
    fields: ReminderState
    type: Literal["upsert"] = "upsert"


@dataclass(frozen=True)
class ReminderDelete:
    op_id: str
    marker: str
    external_id: str | None
    expected: ReminderState | None
    type: Literal["delete"] = "delete"


ReminderCommand = ReminderUpsert | ReminderDelete

ResultStatus = Literal["created", "applied", "deleted", "conflict", "not_found", "error"]


@dataclass(frozen=True)
class ReminderResult:
    op_id: str
    status: ResultStatus
    marker: str | None
    external_id: str | None
    state: ReminderState | None
    error: str | None = None


@dataclass(frozen=True)
class ReminderAck:
    batch_id: str
    run_id: str
    processed_at: str
    results: tuple[ReminderResult, ...]


@dataclass(frozen=True)
class ObservedReminder:
    external_id: str
    marker: str | None
    state: ReminderState
    completion_date: str | None
    creation_date: str | None


@dataclass(frozen=True)
class RemindersSnapshot:
    run_id: str
    taken_at: str
    list_name: str
    list_found: bool
    list_error: str | None
    device_timezone: str | None
    window_days: int
    applied_batches: tuple[str, ...]
    reminders: tuple[ObservedReminder, ...]


@dataclass(frozen=True)
class RemindersInbox:
    acks: tuple[ReminderAck, ...] = ()
    snapshots: tuple[RemindersSnapshot, ...] = ()  # ordenados por taken_at ascendente
    invalid_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class MailboxStatus:
    location: str
    pending_batches: tuple[str, ...] = field(default_factory=tuple)
    acks: int = 0
    snapshots: int = 0


class RemindersGateway(Protocol):
    """Canal asíncrono con Apple Recordatorios."""

    def submit(self, batch_id: str, commands: Sequence[ReminderCommand]) -> None:
        """Publica un lote de comandos para que el dispositivo lo aplique."""
        ...

    def collect(self) -> RemindersInbox:
        """Lee los acks y snapshots disponibles. Nunca borra: el Core registra lo procesado
        y la limpieza del buzón la hace el dispositivo (iCloud para Windows no permite borrar)."""
        ...

    def status(self) -> MailboxStatus: ...


# --------------------------------------------------------------------------------------
# Calendario


@dataclass(frozen=True)
class EventPayload:
    """Representación de un evento independiente de Apple/iCalendar."""

    uid: str
    title: str
    notes: str
    location: str
    start: str  # ISO local "YYYY-MM-DDTHH:MM" o "YYYY-MM-DD" si all_day
    end: str
    all_day: bool
    timezone: str
    rrule: str | None
    alerts: tuple[int, ...]  # minutos antes del inicio


@dataclass(frozen=True)
class RemoteEvent:
    href: str
    etag: str
    payload: EventPayload
    has_overrides: bool = False  # instancias modificadas individualmente (RECURRENCE-ID)


class PreconditionFailed(Exception):
    """El recurso remoto cambió (ETag distinto) o ya existía."""


class RemoteNotFound(Exception):
    pass


class CalendarGateway(Protocol):
    def put(self, payload: EventPayload, href: str | None, etag: str | None) -> tuple[str, str]:
        """Crea (href None) o actualiza con If-Match. Devuelve (href, etag)."""
        ...

    def delete(self, href: str, etag: str | None) -> None: ...

    def get(self, href: str) -> RemoteEvent | None: ...

    def list_all(self) -> list[RemoteEvent]:
        """Todos los eventos del calendario gestionado (href, etag y contenido)."""
        ...

    def list_etags(self) -> dict[str, str]:
        """href -> etag, para detectar cambios de forma barata."""
        ...
