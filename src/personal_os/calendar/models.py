"""Eventos de calendario: cosas con hora concreta (o días completos) que viven en el calendario.

Fechas "de pared" en `timezone`:
- con hora:    starts_at/ends_at = "YYYY-MM-DDTHH:MM"
- día completo: starts_at/ends_at = "YYYY-MM-DD" (ends_at exclusivo, como en iCalendar)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EventStatus = Literal["confirmed", "cancelled"]


@dataclass(frozen=True)
class CalendarEvent:
    id: str
    title: str
    notes: str
    location: str
    starts_at: str
    ends_at: str
    all_day: bool
    timezone: str
    rrule: str | None
    alerts: tuple[int, ...]  # minutos antes del inicio
    status: EventStatus
    origin_module: str
    origin_ref: str | None
    created_at: str
    updated_at: str


class CalendarError(Exception):
    pass


class EventNotFound(CalendarError):
    pass
