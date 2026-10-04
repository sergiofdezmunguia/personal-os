"""Servidor MCP del Personal OS: otra interfaz (como la CLI) sobre los mismos servicios.

Herramientas mínimas: crear/listar/completar tareas, crear/listar eventos y sincronizar.
Todo cambio queda auditado con actor `mcp`. Sin lógica de negocio aquí.

Ejecutar: `uv run pos-mcp` (stdio). Registrado para Claude Code en `.mcp.json`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from personal_os import bootstrap
from personal_os.calendar.models import CalendarError
from personal_os.core import rrule as _rrule
from personal_os.core.config import ConfigError
from personal_os.core.events import ChangeContext
from personal_os.tasks import recurrence
from personal_os.tasks.models import TaskError

Repeat = Literal["daily", "weekly", "monthly", "yearly", "weekdays"]
Weekday = Literal["MO", "TU", "WE", "TH", "FR", "SA", "SU"]

INSTRUCTIONS = """Personal OS: tareas y eventos del usuario, sincronizados con Apple Recordatorios
y Apple Calendario. Los datos viven en el Personal OS; Apple es la interfaz.

- Tarea (create_task): algo que hacer y marcar como hecho. Va a Recordatorios.
- Evento (create_calendar_event): cita con hora concreta o día completo. Va a Calendario.
- Fechas y horas son "de pared" en la zona horaria del usuario (ver list_tasks → timezone/today).
  Formatos: fecha YYYY-MM-DD, hora HH:MM, inicio de evento YYYY-MM-DDTHH:MM.
- Tras crear o modificar, llama a sync_apple. Calendario se actualiza al momento;
  Recordatorios cuando el iPhone ejecute su bridge (al cerrar la app o a horas fijas).
"""


def _ctx() -> ChangeContext:
    return ChangeContext(actor="mcp")


def _today(tz: str) -> str:
    return datetime.now(ZoneInfo(tz)).date().isoformat()


def _task_view(svc, task, occ=None) -> dict[str, Any]:
    occ = occ if occ is not None else svc.open_occurrence(task.id)
    return {
        "id": task.id,
        "title": task.title,
        "notes": task.notes,
        "status": task.status,
        "repeat": recurrence.describe(task.rrule) if task.rrule else None,
        "rrule": task.rrule,
        "due_date": occ.due_date if occ else None,
        "due_time": occ.due_time if occ else None,
        "open_occurrence_id": occ.id if occ and occ.is_open else None,
        "origin": task.origin_module,
    }


def _event_view(ev) -> dict[str, Any]:
    data = dataclasses.asdict(ev)
    data["alerts"] = list(ev.alerts)
    for k in ("timezone", "created_at", "updated_at", "origin_ref"):
        data.pop(k)
    return data


def build_server(open_app: Callable[[], bootstrap.App] = bootstrap.open_app) -> MCPServer:
    server = MCPServer(name="personal-os", instructions=INSTRUCTIONS, version="0.1.0")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False))
    def create_task(
        title: str,
        due_date: str | None = None,
        due_time: str | None = None,
        notes: str = "",
        repeat: Repeat | None = None,
        interval: int = 1,
        on_days: list[Weekday] | None = None,
        repeat_from_completion: bool = False,
    ) -> dict[str, Any]:
        """Crea una tarea (va a Apple Recordatorios). due_time solo con due_date; con hora el
        iPhone avisa. repeat crea una serie: al completar una ocurrencia se genera la siguiente.
        repeat_from_completion cuenta el intervalo desde la compleción ("cada 3 meses desde
        la última vez")."""
        svc = bootstrap.task_service(open_app())
        try:
            rule = (
                recurrence.build_rrule(repeat, interval, ",".join(on_days) if on_days else None)
                if repeat
                else None
            )
            task, occ = svc.create_task(
                _ctx(),
                title,
                notes=notes,
                due_date=due_date,
                due_time=due_time,
                rrule=rule,
                anchor="completion" if repeat_from_completion else "schedule",
                origin_module="mcp",
            )
        except TaskError as exc:
            raise ToolError(str(exc)) from exc
        return _task_view(svc, task, occ)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_tasks(include_closed: bool = False) -> dict[str, Any]:
        """Lista las tareas (activas por defecto) con su próxima fecha. Incluye today y
        timezone para interpretar fechas relativas."""
        application = open_app()
        svc = bootstrap.task_service(application)
        tasks = [_task_view(svc, t) for t in svc.list_tasks(include_closed=include_closed)]
        tasks.sort(key=lambda t: (t["due_date"] or "9999", t["due_time"] or ""))
        tz = application.config.timezone
        return {"today": _today(tz), "timezone": tz, "tasks": tasks}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True))
    def complete_task(task_id: str) -> dict[str, Any]:
        """Completa la ocurrencia abierta de una tarea (id `tsk_…`, `occ_…` o prefijo único).
        Si es recurrente, devuelve la siguiente ocurrencia generada."""
        svc = bootstrap.task_service(open_app())
        try:
            task, occ = svc.resolve_id(task_id)
            if occ is None:
                raise TaskError(f"La tarea {task.id} no tiene ocurrencia abierta")
            done, nxt = svc.complete(_ctx(), occ.id)
        except TaskError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "completed": {
                "task_id": task.id,
                "title": task.title,
                "completed_at": done.completed_at,
            },
            "next": {"due_date": nxt.due_date, "due_time": nxt.due_time} if nxt else None,
        }

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False))
    def create_calendar_event(
        title: str,
        start: str,
        end: str | None = None,
        duration_minutes: int | None = None,
        alerts_minutes_before: list[int] | None = None,
        notes: str = "",
        location: str = "",
        repeat: Repeat | None = None,
    ) -> dict[str, Any]:
        """Crea un evento (va a Apple Calendario). start = YYYY-MM-DDTHH:MM (con hora) o
        YYYY-MM-DD (día completo; end exclusivo). Sin end: duración (60 min por defecto) o un
        día. alerts_minutes_before: avisos, p. ej. [60, 10]."""
        svc = bootstrap.calendar_service(open_app())
        try:
            rule = _rrule.normalize_rrule(recurrence.SHORTCUTS[repeat]) if repeat else None
            ev = svc.create(
                _ctx(),
                title,
                starts_at=start,
                ends_at=end,
                duration_minutes=duration_minutes,
                all_day="T" not in start,
                notes=notes,
                location=location,
                rrule=rule,
                alerts=alerts_minutes_before or [],
                origin_module="mcp",
            )
        except (CalendarError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return _event_view(ev)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_calendar_events(from_date: str | None = None) -> dict[str, Any]:
        """Eventos confirmados que terminan a partir de from_date (YYYY-MM-DD; hoy por
        defecto). Los recurrentes se incluyen siempre."""
        application = open_app()
        tz = application.config.timezone
        events = bootstrap.calendar_service(application).list(since=from_date or _today(tz))
        return {"today": _today(tz), "timezone": tz, "events": [_event_view(e) for e in events]}

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=True)
    )
    def sync_apple(only: Literal["reminders", "calendar"] | None = None) -> dict[str, Any]:
        """Sincroniza con Apple: envía cambios y recibe lo hecho en el iPhone (tareas
        completadas, ediciones, borrados, recordatorios/eventos creados a mano)."""
        try:
            outcome = bootstrap.run_sync(open_app(), only=only)
        except (ConfigError, RuntimeError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return {
            "reports": [
                {
                    "provider": r.provider,
                    "stats": r.stats,
                    "warnings": r.warnings,
                    "errors": r.errors,
                }
                for r in outcome.reports
            ],
            "backup": outcome.backup.path.name if outcome.backup else None,
            "backup_warnings": outcome.backup_warnings,
            "note": "Los cambios en Recordatorios llegan al iPhone cuando se ejecute su bridge.",
        }

    return server


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
