"""Servicio de calendario: única puerta para crear/modificar eventos. No conoce Apple."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from personal_os.calendar.models import CalendarError, CalendarEvent, EventNotFound
from personal_os.core import rrule as _rrule
from personal_os.core.clock import Clock, to_iso
from personal_os.core.db import Database
from personal_os.core.events import ChangeContext, EventLog
from personal_os.core.ids import new_id

_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
_D_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EDITABLE = ("title", "notes", "location", "starts_at", "ends_at", "all_day", "rrule", "alerts")


def _row(row) -> CalendarEvent:
    data = dict(row)
    data["all_day"] = bool(data["all_day"])
    data["alerts"] = tuple(json.loads(data["alerts"]))
    return CalendarEvent(**data)


def validate_times(starts_at: str, ends_at: str, all_day: bool) -> None:
    pattern, parse = (_D_RE, date.fromisoformat) if all_day else (_DT_RE, datetime.fromisoformat)
    for v in (starts_at, ends_at):
        if not pattern.match(v):
            fmt = "YYYY-MM-DD" if all_day else "YYYY-MM-DDTHH:MM"
            raise CalendarError(f"Fecha {v!r} inválida (formato {fmt})")
        try:
            parse(v)
        except ValueError as exc:
            raise CalendarError(f"Fecha {v!r} inválida: {exc}") from exc
    if parse(ends_at) <= parse(starts_at):
        raise CalendarError("El final debe ser posterior al inicio")


def _alerts(alerts) -> tuple[int, ...]:
    out = tuple(sorted({int(a) for a in alerts}))
    if any(a < 0 or a > 40320 for a in out):
        raise CalendarError("Los avisos son minutos antes del inicio (0..40320)")
    return out


class CalendarService:
    def __init__(self, db: Database, clock: Clock, events: EventLog, timezone: str) -> None:
        self.db = db
        self.clock = clock
        self.events = events
        self.timezone = timezone

    # ---------------------------------------------------------------- consultas

    def get(self, event_id: str) -> CalendarEvent:
        row = self.db.one("SELECT * FROM calendar_events WHERE id = ?", (event_id,))
        if row is None:
            raise EventNotFound(f"No existe el evento {event_id}")
        return _row(row)

    def resolve_id(self, ref: str) -> CalendarEvent:
        rows = self.db.all("SELECT id FROM calendar_events WHERE id LIKE ?", (ref + "%",))
        if len(rows) > 1:
            raise CalendarError(f"El prefijo {ref!r} es ambiguo")
        if not rows:
            raise EventNotFound(f"No encuentro {ref!r}")
        return self.get(rows[0]["id"])

    def list(
        self, *, include_cancelled: bool = False, since: str | None = None
    ) -> list[CalendarEvent]:
        sql, params = "SELECT * FROM calendar_events WHERE 1=1", []
        if not include_cancelled:
            sql += " AND status = 'confirmed'"
        if since:
            sql += " AND (ends_at >= ? OR rrule IS NOT NULL)"
            params.append(since)
        return [_row(r) for r in self.db.all(sql + " ORDER BY starts_at", params)]

    def for_sync(self, updated_since: str) -> list[CalendarEvent]:
        rows = self.db.all(
            "SELECT * FROM calendar_events WHERE status = 'confirmed' OR updated_at >= ?"
            " ORDER BY created_at",
            (updated_since,),
        )
        return [_row(r) for r in rows]

    # ---------------------------------------------------------------- mutaciones

    def create(
        self,
        ctx: ChangeContext,
        title: str,
        *,
        starts_at: str,
        ends_at: str | None = None,
        duration_minutes: int | None = None,
        all_day: bool = False,
        notes: str = "",
        location: str = "",
        rrule: str | None = None,
        alerts: tuple[int, ...] | list[int] = (),
        origin_module: str = "cli",
        origin_ref: str | None = None,
        validate_rrule: bool = True,
    ) -> CalendarEvent:
        title = title.strip()
        if not title:
            raise CalendarError("El título no puede estar vacío")
        if ends_at is None:
            if all_day:
                ends_at = (date.fromisoformat(starts_at) + timedelta(days=1)).isoformat()
            else:
                start = datetime.fromisoformat(starts_at)
                ends_at = (start + timedelta(minutes=duration_minutes or 60)).strftime(
                    "%Y-%m-%dT%H:%M"
                )
        validate_times(starts_at, ends_at, all_day)
        if rrule and validate_rrule:
            try:
                rrule = _rrule.normalize_rrule(rrule)
            except _rrule.RRuleError as exc:
                raise CalendarError(str(exc)) from exc
        now = to_iso(self.clock.now())
        event = CalendarEvent(
            id=new_id("calendar_event"),
            title=title,
            notes=notes.rstrip(),
            location=location.strip(),
            starts_at=starts_at,
            ends_at=ends_at,
            all_day=all_day,
            timezone=self.timezone,
            rrule=rrule,
            alerts=_alerts(alerts),
            status="confirmed",
            origin_module=origin_module,
            origin_ref=origin_ref,
            created_at=now,
            updated_at=now,
        )
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO calendar_events VALUES (:id,:title,:notes,:location,:starts_at,:ends_at,"
                ":all_day,:timezone,:rrule,:alerts,:status,:origin_module,:origin_ref,:created_at,:updated_at)",
                {
                    **event.__dict__,
                    "all_day": int(all_day),
                    "alerts": json.dumps(list(event.alerts)),
                },
            )
            self.events.record(
                ctx,
                "calendar_event.created",
                "calendar_event",
                event.id,
                {"title": title, "starts_at": starts_at, "ends_at": ends_at, "rrule": rrule},
            )
        return event

    def update(
        self, ctx: ChangeContext, event_id: str, *, validate_rrule: bool = True, **fields
    ) -> CalendarEvent:
        """Actualiza campos editables. Valida el estado resultante como un todo."""
        event = self.get(event_id)
        unknown = set(fields) - set(EDITABLE)
        if unknown:
            raise CalendarError(f"Campos no editables: {sorted(unknown)}")
        changes = {k: v for k, v in fields.items() if getattr(event, k) != v}
        if "alerts" in changes:
            changes["alerts"] = _alerts(changes["alerts"])
            if changes["alerts"] == event.alerts:
                del changes["alerts"]
        if not changes:
            return event
        merged = {**event.__dict__, **changes}
        validate_times(merged["starts_at"], merged["ends_at"], merged["all_day"])
        if changes.get("rrule") and validate_rrule:
            try:
                changes["rrule"] = _rrule.normalize_rrule(changes["rrule"])
            except _rrule.RRuleError as exc:
                raise CalendarError(str(exc)) from exc
        if "title" in changes and not str(changes["title"]).strip():
            raise CalendarError("El título no puede estar vacío")
        db_changes = dict(changes)
        if "alerts" in db_changes:
            db_changes["alerts"] = json.dumps(list(db_changes["alerts"]))
        if "all_day" in db_changes:
            db_changes["all_day"] = int(db_changes["all_day"])
        with self.db.transaction():
            self._write(event.id, db_changes)
            self.events.record(
                ctx,
                "calendar_event.updated",
                "calendar_event",
                event.id,
                {k: list(v) if isinstance(v, tuple) else v for k, v in changes.items()},
            )
        return self.get(event.id)

    def cancel(
        self, ctx: ChangeContext, event_id: str, *, reason: str = "cancelled"
    ) -> CalendarEvent:
        event = self.get(event_id)
        if event.status == "cancelled":
            return event
        with self.db.transaction():
            self._write(event.id, {"status": "cancelled"})
            self.events.record(
                ctx, "calendar_event.cancelled", "calendar_event", event.id, {"reason": reason}
            )
        return self.get(event.id)

    def _write(self, event_id: str, changes: dict) -> None:
        changes = {**changes, "updated_at": to_iso(self.clock.now())}
        sets = ", ".join(f"{k} = :{k}" for k in changes)
        self.db.execute(
            f"UPDATE calendar_events SET {sets} WHERE id = :id", {**changes, "id": event_id}
        )
