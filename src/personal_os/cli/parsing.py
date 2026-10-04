"""Parseo de fechas/horas cómodo para la CLI (determinista, sin LLM)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import typer

_WEEKDAYS = {
    "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2, "jueves": 3, "viernes": 4,
    "sabado": 5, "sábado": 5, "domingo": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}  # fmt: skip


def today(tz: str) -> date:
    return datetime.now(ZoneInfo(tz)).date()


def parse_date(value: str, tz: str) -> str:
    """YYYY-MM-DD | hoy | mañana | pasado | +N | lunes..domingo (el próximo)."""
    v = value.strip().lower()
    base = today(tz)
    if v in ("hoy", "today"):
        return base.isoformat()
    if v in ("mañana", "manana", "tomorrow"):
        return (base + timedelta(days=1)).isoformat()
    if v == "pasado":
        return (base + timedelta(days=2)).isoformat()
    if re.fullmatch(r"\+\d+", v):
        return (base + timedelta(days=int(v[1:]))).isoformat()
    if v in _WEEKDAYS:
        delta = (_WEEKDAYS[v] - base.weekday()) % 7 or 7
        return (base + timedelta(days=delta)).isoformat()
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError as exc:
        raise typer.BadParameter(
            f"Fecha {value!r} no válida (YYYY-MM-DD, hoy, mañana, +N, lunes…)"
        ) from exc


def parse_time(value: str) -> str:
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", value.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
        raise typer.BadParameter(f"Hora {value!r} no válida (HH:MM)")
    return f"{int(m.group(1)):02d}:{int(m.group(2) or 0):02d}"


def parse_datetime(value: str, tz: str) -> tuple[str, str | None]:
    """'2026-10-06 18:00' | 'mañana 18:00' | '2026-10-06' → (fecha, hora|None)."""
    parts = value.strip().replace("T", " ").split()
    if len(parts) == 1:
        return parse_date(parts[0], tz), None
    if len(parts) == 2:
        return parse_date(parts[0], tz), parse_time(parts[1])
    raise typer.BadParameter(f"Fecha/hora {value!r} no válida")
