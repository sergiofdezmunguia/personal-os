"""Recurrencias de tareas: un subconjunto pequeño y explícito de RRULE (RFC 5545).

El Personal OS es el dueño de la recurrencia: a Apple solo llega la ocurrencia abierta.
Soportado: FREQ (DAILY|WEEKLY|MONTHLY|YEARLY), INTERVAL, BYDAY (sin ordinales),
BYMONTHDAY, COUNT, UNTIL (fecha).

Anclas:
- schedule:   la serie sigue el calendario original ("cada lunes" sigue siendo lunes). Si se
              completa tarde, la siguiente es la primera fecha de la regla >= hoy.
- completion: la siguiente se calcula desde el día en que se completó ("cada 3 meses
              desde la última vez").
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from dateutil.rrule import rrulestr

from personal_os.core import rrule as _rrule
from personal_os.tasks.models import TaskError

SHORTCUTS = {
    "daily": "FREQ=DAILY",
    "weekly": "FREQ=WEEKLY",
    "monthly": "FREQ=MONTHLY",
    "yearly": "FREQ=YEARLY",
    "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
}


def normalize_rrule(rule: str) -> str:
    try:
        return _rrule.normalize_rrule(rule)
    except _rrule.RRuleError as exc:
        raise TaskError(str(exc)) from exc


describe = _rrule.describe


def build_rrule(every: str, interval: int = 1, on: str | None = None) -> str:
    base = SHORTCUTS.get(every.lower())
    if base is None:
        raise TaskError(f"--every debe ser uno de {sorted(SHORTCUTS)}")
    rule = base
    if interval > 1:
        rule += f";INTERVAL={interval}"
    if on:
        if "BYDAY" in rule:
            raise TaskError("'weekdays' ya fija los días")
        rule += f";BYDAY={on.upper()}"
    return normalize_rrule(rule)


def _rule(rule: str, dtstart: date):
    return rrulestr(rule, dtstart=datetime.combine(dtstart, datetime.min.time()))


def first_date(rule: str, start: date) -> date | None:
    """Primera fecha de la regla en o después de `start` (p. ej. weekly BYDAY=MO desde un jueves)."""
    nxt = _rule(rule, start).after(datetime.combine(start, datetime.min.time()), inc=True)
    return nxt.date() if nxt else None


def next_date(
    rule: str,
    *,
    anchor: str,
    series_start: date,
    previous_scheduled: date,
    completed_on: date | None,
    today: date,
    occurrences_so_far: int,
) -> date | None:
    """Fecha programada de la siguiente ocurrencia, o None si la serie terminó."""
    if anchor == "completion":
        base = completed_on or today
        # La regla se aplica desde el día de compleción; COUNT cuenta ocurrencias ya generadas.
        nxt = _rule(rule, base).after(datetime.combine(base, datetime.min.time()), inc=False)
        candidate = nxt.date() if nxt else None
    else:
        r = _rule(rule, series_start)
        floor = max(previous_scheduled, today - timedelta(days=1))
        nxt = r.after(datetime.combine(floor, datetime.min.time()), inc=False)
        candidate = nxt.date() if nxt else None

    if candidate is None:
        return None
    count = _count(rule)
    if count is not None and occurrences_so_far >= count:
        return None
    until = _until(rule)
    if until is not None and candidate > until:
        return None
    return candidate


def _count(rule: str) -> int | None:
    m = re.search(r"COUNT=(\d+)", rule)
    return int(m.group(1)) if m else None


def _until(rule: str) -> date | None:
    m = re.search(r"UNTIL=(\d{8})", rule)
    return datetime.strptime(m.group(1), "%Y%m%d").date() if m else None
