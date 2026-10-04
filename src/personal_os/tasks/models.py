"""Modelo de tareas.

- `Task` es la definición (o serie, si es recurrente): título, notas y regla.
- `Occurrence` es cada instancia concreta que se hace/completa. Una tarea no recurrente
  tiene exactamente una ocurrencia. Como mucho hay una ocurrencia abierta por tarea.

Las fechas son "de pared" (YYYY-MM-DD / HH:MM) en `Task.timezone`: lo que el usuario ve.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TaskStatus = Literal["active", "completed", "cancelled"]
OccurrenceStatus = Literal["open", "completed", "skipped", "cancelled"]
RecurrenceAnchor = Literal["schedule", "completion"]


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    notes: str
    due_date: str | None
    due_time: str | None
    timezone: str
    rrule: str | None
    recurrence_anchor: RecurrenceAnchor
    status: TaskStatus
    origin_module: str
    origin_ref: str | None
    created_at: str
    updated_at: str

    @property
    def is_recurring(self) -> bool:
        return self.rrule is not None


@dataclass(frozen=True)
class Occurrence:
    id: str
    task_id: str
    seq: int
    scheduled_date: str | None
    due_date: str | None
    due_time: str | None
    status: OccurrenceStatus
    completed_at: str | None
    completed_via: str | None
    closed_reason: str | None
    created_at: str
    updated_at: str

    @property
    def is_open(self) -> bool:
        return self.status == "open"


class TaskError(Exception):
    pass


class NotFound(TaskError):
    pass
