"""Servicio de tareas: única puerta de entrada para modificar tareas y ocurrencias.

Toda mutación ocurre en una transacción y deja eventos de dominio con su actor.
No sabe nada de Apple: la sincronización llama a este servicio como cualquier otro cliente.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from zoneinfo import ZoneInfo

from personal_os.core.clock import Clock, from_iso, to_iso
from personal_os.core.db import Database
from personal_os.core.events import ChangeContext, EventLog
from personal_os.core.ids import new_id
from personal_os.tasks import recurrence
from personal_os.tasks.models import NotFound, Occurrence, Task, TaskError

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_UNSET = object()


def _check_date(value: str | None) -> None:
    if value is not None:
        if not _DATE_RE.match(value):
            raise TaskError(f"Fecha inválida {value!r} (formato YYYY-MM-DD)")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise TaskError(f"Fecha inválida {value!r}: {exc}") from exc


def _check_time(value: str | None) -> None:
    if value is not None and not _TIME_RE.match(value):
        raise TaskError(f"Hora inválida {value!r} (formato HH:MM)")


def _task(row) -> Task:
    return Task(**dict(row))


def _occ(row) -> Occurrence:
    return Occurrence(**dict(row))


class TaskService:
    def __init__(self, db: Database, clock: Clock, events: EventLog, timezone: str) -> None:
        self.db = db
        self.clock = clock
        self.events = events
        self.timezone = timezone

    # ---------------------------------------------------------------- consultas

    def get_task(self, task_id: str) -> Task:
        row = self.db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise NotFound(f"No existe la tarea {task_id}")
        return _task(row)

    def get_occurrence(self, occ_id: str) -> Occurrence:
        row = self.db.one("SELECT * FROM task_occurrences WHERE id = ?", (occ_id,))
        if row is None:
            raise NotFound(f"No existe la ocurrencia {occ_id}")
        return _occ(row)

    def list_tasks(self, *, include_closed: bool = False) -> list[Task]:
        sql = "SELECT * FROM tasks"
        if not include_closed:
            sql += " WHERE status = 'active'"
        sql += " ORDER BY created_at"
        return [_task(r) for r in self.db.all(sql)]

    def occurrences(self, task_id: str) -> list[Occurrence]:
        rows = self.db.all(
            "SELECT * FROM task_occurrences WHERE task_id = ? ORDER BY seq", (task_id,)
        )
        return [_occ(r) for r in rows]

    def open_occurrence(self, task_id: str) -> Occurrence | None:
        row = self.db.one(
            "SELECT * FROM task_occurrences WHERE task_id = ? AND status = 'open'", (task_id,)
        )
        return _occ(row) if row else None

    def occurrences_for_sync(self, since: str) -> list[tuple[Task, Occurrence]]:
        """Ocurrencias abiertas + las cerradas/modificadas desde `since` (para la sync)."""
        rows = self.db.all(
            "SELECT o.id FROM task_occurrences o WHERE o.status = 'open' OR o.updated_at >= ?"
            " ORDER BY o.created_at",
            (since,),
        )
        out = []
        for r in rows:
            occ = self.get_occurrence(r["id"])
            out.append((self.get_task(occ.task_id), occ))
        return out

    def resolve_id(self, ref: str) -> tuple[Task, Occurrence | None]:
        """Acepta id completo de tarea/ocurrencia o un prefijo único (p. ej. 'tsk_01M43')."""
        for table, kind in (("tasks", "task"), ("task_occurrences", "occ")):
            rows = self.db.all(f"SELECT id FROM {table} WHERE id LIKE ?", (ref + "%",))
            if len(rows) > 1:
                raise TaskError(f"El prefijo {ref!r} es ambiguo")
            if len(rows) == 1:
                if kind == "task":
                    task = self.get_task(rows[0]["id"])
                    return task, self.open_occurrence(task.id)
                occ = self.get_occurrence(rows[0]["id"])
                return self.get_task(occ.task_id), occ
        raise NotFound(f"No encuentro {ref!r}")

    # ---------------------------------------------------------------- creación

    def create_task(
        self,
        ctx: ChangeContext,
        title: str,
        *,
        notes: str = "",
        due_date: str | None = None,
        due_time: str | None = None,
        rrule: str | None = None,
        anchor: str = "schedule",
        origin_module: str = "cli",
        origin_ref: str | None = None,
    ) -> tuple[Task, Occurrence]:
        title = title.strip()
        if not title:
            raise TaskError("El título no puede estar vacío")
        _check_date(due_date)
        _check_time(due_time)
        if due_time and not due_date:
            raise TaskError("Una hora requiere fecha")
        if anchor not in ("schedule", "completion"):
            raise TaskError("anchor debe ser 'schedule' o 'completion'")
        if rrule:
            rrule = recurrence.normalize_rrule(rrule)
            start = date.fromisoformat(due_date) if due_date else self._today()
            first = recurrence.first_date(rrule, start)
            if first is None:
                raise TaskError("La regla no produce ninguna fecha")
            due_date = first.isoformat()

        now = to_iso(self.clock.now())
        task = Task(
            id=new_id("task"),
            title=title,
            notes=notes.rstrip(),
            due_date=due_date,
            due_time=due_time,
            timezone=self.timezone,
            rrule=rrule,
            recurrence_anchor=anchor,  # type: ignore[arg-type]
            status="active",
            origin_module=origin_module,
            origin_ref=origin_ref,
            created_at=now,
            updated_at=now,
        )
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO tasks VALUES (:id,:title,:notes,:due_date,:due_time,:timezone,:rrule,"
                ":recurrence_anchor,:status,:origin_module,:origin_ref,:created_at,:updated_at)",
                task.__dict__,
            )
            self.events.record(
                ctx,
                "task.created",
                "task",
                task.id,
                {
                    "title": title,
                    "due_date": due_date,
                    "due_time": due_time,
                    "rrule": rrule,
                    "origin_module": origin_module,
                },
            )
            occ = self._new_occurrence(ctx, task, seq=1, scheduled=due_date, due_time=due_time)
        return task, occ

    def _new_occurrence(
        self,
        ctx: ChangeContext,
        task: Task,
        *,
        seq: int,
        scheduled: str | None,
        due_time: str | None,
    ) -> Occurrence:
        now = to_iso(self.clock.now())
        occ = Occurrence(
            id=new_id("occurrence"),
            task_id=task.id,
            seq=seq,
            scheduled_date=scheduled,
            due_date=scheduled,
            due_time=due_time,
            status="open",
            completed_at=None,
            completed_via=None,
            closed_reason=None,
            created_at=now,
            updated_at=now,
        )
        self.db.execute(
            "INSERT INTO task_occurrences VALUES (:id,:task_id,:seq,:scheduled_date,:due_date,"
            ":due_time,:status,:completed_at,:completed_via,:closed_reason,:created_at,:updated_at)",
            occ.__dict__,
        )
        self.events.record(
            ctx,
            "task.occurrence.created",
            "occurrence",
            occ.id,
            {"task_id": task.id, "seq": seq, "due_date": scheduled, "due_time": due_time},
        )
        return occ

    # ---------------------------------------------------------------- edición

    def update_task(
        self,
        ctx: ChangeContext,
        task_id: str,
        *,
        title: str | None = None,
        notes: str | None = None,
    ) -> Task:
        task = self.get_task(task_id)
        changes: dict[str, object] = {}
        if title is not None and title.strip() and title.strip() != task.title:
            changes["title"] = title.strip()
        if notes is not None and notes.rstrip() != task.notes:
            changes["notes"] = notes.rstrip()
        if not changes:
            return task
        with self.db.transaction():
            self._touch_task(task.id, changes)
            # La ocurrencia abierta cambia su representación externa: la marcamos como tocada.
            self._touch_open_occurrence(task.id)
            self.events.record(ctx, "task.updated", "task", task.id, changes)
        return self.get_task(task.id)

    def reschedule(
        self,
        ctx: ChangeContext,
        occ_id: str,
        *,
        due_date: str | object | None = _UNSET,
        due_time: str | object | None = _UNSET,
    ) -> Occurrence:
        """Cambia la fecha de UNA ocurrencia (no altera la serie)."""
        occ = self.get_occurrence(occ_id)
        if not occ.is_open:
            raise TaskError("Solo se puede reprogramar una ocurrencia abierta")
        new_date = occ.due_date if due_date is _UNSET else due_date
        new_time = occ.due_time if due_time is _UNSET else due_time
        _check_date(new_date)  # type: ignore[arg-type]
        _check_time(new_time)  # type: ignore[arg-type]
        if new_time and not new_date:
            raise TaskError("Una hora requiere fecha")
        if (new_date, new_time) == (occ.due_date, occ.due_time):
            return occ
        with self.db.transaction():
            self._update_occ(occ.id, {"due_date": new_date, "due_time": new_time})
            self.events.record(
                ctx,
                "task.occurrence.rescheduled",
                "occurrence",
                occ.id,
                {"from": [occ.due_date, occ.due_time], "to": [new_date, new_time]},
            )
        return self.get_occurrence(occ.id)

    # ---------------------------------------------------------------- ciclo de vida

    def complete(
        self,
        ctx: ChangeContext,
        occ_id: str,
        *,
        completed_at: str | None = None,
    ) -> tuple[Occurrence, Occurrence | None]:
        """Completa una ocurrencia. Si la tarea es recurrente, genera la siguiente."""
        occ = self.get_occurrence(occ_id)
        if occ.status == "completed":
            return occ, self.open_occurrence(occ.task_id)
        if not occ.is_open:
            raise TaskError(f"La ocurrencia está {occ.status}")
        when = completed_at or to_iso(self.clock.now())
        with self.db.transaction():
            self._update_occ(
                occ.id, {"status": "completed", "completed_at": when, "completed_via": ctx.actor}
            )
            self.events.record(
                ctx, "task.occurrence.completed", "occurrence", occ.id, {"completed_at": when}
            )
            nxt = self._advance(ctx, occ, completed_at=when)
        return self.get_occurrence(occ.id), nxt

    def skip(
        self, ctx: ChangeContext, occ_id: str, *, reason: str = "skipped"
    ) -> Occurrence | None:
        """Salta una ocurrencia abierta (no hecha). La serie continúa."""
        return self._close(ctx, occ_id, status="skipped", reason=reason)

    def close_deleted_externally(self, ctx: ChangeContext, occ_id: str) -> Occurrence | None:
        """La ocurrencia desapareció en el proveedor: se cancela esa ocurrencia; la serie sigue."""
        return self._close(ctx, occ_id, status="cancelled", reason="deleted_externally")

    def _close(
        self, ctx: ChangeContext, occ_id: str, *, status: str, reason: str
    ) -> Occurrence | None:
        occ = self.get_occurrence(occ_id)
        if not occ.is_open:
            return None
        with self.db.transaction():
            self._update_occ(occ.id, {"status": status, "closed_reason": reason})
            self.events.record(
                ctx, f"task.occurrence.{status}", "occurrence", occ.id, {"reason": reason}
            )
            return self._advance(ctx, occ, completed_at=None)

    def reopen(self, ctx: ChangeContext, occ_id: str) -> Occurrence:
        """Deshace una compleción (p. ej. desmarcada en el iPhone).

        Si al completarla se generó la siguiente ocurrencia y sigue abierta, se cancela para
        mantener una única ocurrencia abierta por tarea.
        """
        occ = self.get_occurrence(occ_id)
        if occ.is_open:
            return occ
        task = self.get_task(occ.task_id)
        with self.db.transaction():
            current_open = self.open_occurrence(task.id)
            if current_open is not None and current_open.seq > occ.seq:
                self._update_occ(
                    current_open.id, {"status": "cancelled", "closed_reason": "previous_reopened"}
                )
                self.events.record(
                    ctx,
                    "task.occurrence.cancelled",
                    "occurrence",
                    current_open.id,
                    {"reason": "previous_reopened"},
                )
            elif current_open is not None:
                raise TaskError("Ya hay otra ocurrencia abierta más reciente")
            self._update_occ(
                occ.id,
                {
                    "status": "open",
                    "completed_at": None,
                    "completed_via": None,
                    "closed_reason": None,
                },
            )
            if task.status == "completed":
                self._touch_task(task.id, {"status": "active"})
            self.events.record(ctx, "task.occurrence.reopened", "occurrence", occ.id, {})
        return self.get_occurrence(occ.id)

    def cancel_task(self, ctx: ChangeContext, task_id: str) -> Task:
        """Cancela la serie completa y su ocurrencia abierta."""
        task = self.get_task(task_id)
        if task.status == "cancelled":
            return task
        with self.db.transaction():
            occ = self.open_occurrence(task.id)
            if occ is not None:
                self._update_occ(occ.id, {"status": "cancelled", "closed_reason": "task_cancelled"})
                self.events.record(
                    ctx,
                    "task.occurrence.cancelled",
                    "occurrence",
                    occ.id,
                    {"reason": "task_cancelled"},
                )
            self._touch_task(task.id, {"status": "cancelled"})
            self.events.record(ctx, "task.cancelled", "task", task.id, {})
        return self.get_task(task.id)

    # ---------------------------------------------------------------- internos

    def _advance(
        self, ctx: ChangeContext, occ: Occurrence, *, completed_at: str | None
    ) -> Occurrence | None:
        """Tras cerrar `occ`: genera la siguiente ocurrencia o cierra la tarea."""
        task = self.get_task(occ.task_id)
        if task.status != "active":
            return None
        if not task.rrule:
            final = self.get_occurrence(occ.id)
            if final.is_open:
                return None
            new_status = "completed" if final.status == "completed" else "cancelled"
            self._touch_task(task.id, {"status": new_status})
            self.events.record(ctx, f"task.{new_status}", "task", task.id, {})
            return None

        tz = ZoneInfo(task.timezone)
        completed_on = from_iso(completed_at).astimezone(tz).date() if completed_at else None
        nxt = recurrence.next_date(
            task.rrule,
            anchor=task.recurrence_anchor,
            series_start=date.fromisoformat(task.due_date) if task.due_date else self._today(),
            previous_scheduled=date.fromisoformat(
                occ.scheduled_date or occ.due_date or task.due_date
            ),  # type: ignore[arg-type]
            completed_on=completed_on,
            today=self._today(tz),
            occurrences_so_far=occ.seq,
        )
        if nxt is None:
            self._touch_task(task.id, {"status": "completed"})
            self.events.record(ctx, "task.completed", "task", task.id, {"reason": "series_ended"})
            return None
        return self._new_occurrence(
            ctx, task, seq=occ.seq + 1, scheduled=nxt.isoformat(), due_time=task.due_time
        )

    def _today(self, tz: ZoneInfo | None = None) -> date:
        return self.clock.now().astimezone(tz or ZoneInfo(self.timezone)).date()

    def _touch_task(self, task_id: str, changes: dict[str, object]) -> None:
        changes = {**changes, "updated_at": to_iso(self.clock.now())}
        sets = ", ".join(f"{k} = :{k}" for k in changes)
        self.db.execute(f"UPDATE tasks SET {sets} WHERE id = :id", {**changes, "id": task_id})

    def _update_occ(self, occ_id: str, changes: dict[str, object]) -> None:
        changes = {**changes, "updated_at": to_iso(self.clock.now())}
        sets = ", ".join(f"{k} = :{k}" for k in changes)
        self.db.execute(
            f"UPDATE task_occurrences SET {sets} WHERE id = :id", {**changes, "id": occ_id}
        )

    def _touch_open_occurrence(self, task_id: str) -> None:
        occ = self.open_occurrence(task_id)
        if occ is not None:
            self._update_occ(occ.id, {})


def tasks_with_open(
    service: TaskService, tasks: Iterable[Task]
) -> list[tuple[Task, Occurrence | None]]:
    return [(t, service.open_occurrence(t.id)) for t in tasks]
