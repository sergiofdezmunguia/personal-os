"""Avisos para el iPhone: el PC los deja en `<buzón>/panel/avisos.json` en cada sync y el
script «Personal OS Avisos» de Scriptable los muestra como notificación (ADR 0011).

Cada aviso tiene un id estable (no se repite) y una ventana [deliver_after, expires]: el
iPhone solo lo muestra si se ejecuta dentro de ella (las automatizaciones corren 3 veces al
día). Sin servidor ni push: si el PC no sincroniza, no hay avisos nuevos.

- **Buenos días** (07:00–12:30): lo gastado ayer y cómo va el mes; el iPhone añade la agenda.
- **Resumen semanal** (domingo 19:00 – lunes 11:00): la semana frente a la habitual.
- **Alertas** (3 días): subidas de cuotas, cobros recurrentes nuevos, posibles duplicados y
  renovaciones anuales próximas.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from personal_os.finance import analyst

FILE = "avisos.json"
MORNING = (time(7, 0), time(12, 30))
WEEKLY_AT = time(19, 0)
ALERT_DAYS = 3
MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre"]  # fmt: skip


def eur(cents: int, *, decimals: bool | None = None) -> str:
    """9,99 € · 1.234 € (sin decimales a partir de 100 €, salvo que se pidan)."""
    show = decimals if decimals is not None else abs(cents) < 10000
    value = abs(cents) / 100
    text = f"{value:,.2f}" if show else f"{value:,.0f}"
    text = text.replace(",", "·").replace(".", ",").replace("·", ".")
    return ("−" if cents < 0 else "") + text + " €"


def short_day(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d.day}/{d.month}"


def _utc(local: datetime) -> str:
    return local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def _notice(nid, title, body, start: datetime, end: datetime, *, agenda=False, kind="alerta"):
    return {
        "id": nid,
        "kind": kind,
        "title": title,
        "body": body,
        "deliver_after": _utc(start),
        "expires": _utc(end),
        "agenda": agenda,
    }


def _names(items: list[tuple[str, int]]) -> str:
    return ", ".join(f"{name} {eur(c)}" for name, c in items)


def morning(svc, panel, now: datetime) -> dict | None:
    """Solo si el PC ha sincronizado hoy de madrugada o por la mañana (datos de ayer)."""
    if now.time() >= MORNING[1]:
        return None
    today = now.date()
    yesterday = (today - timedelta(days=1)).isoformat()
    s = analyst.spend(svc, yesterday, yesterday)
    if s.expense_cents:
        first = f"Ayer: {eur(s.expense_cents)} en {s.payments} pago{'s' * (s.payments != 1)}"
        first += f" ({_names(s.top[:2])})." if s.top else "."
    else:
        first = "Ayer no hubo gastos."
    if s.uncategorized:
        first += f" {s.uncategorized} sin categoría."
    p = panel.pace
    month = MONTHS[int(p.month[5:7]) - 1]
    diff = p.variable_cents - p.variable_habit_to_day_cents
    if p.variable_habit_cents:
        pace = f"{eur(abs(diff), decimals=False)} {'más' if diff > 0 else 'menos'} que tu ritmo"
        second = (
            f"En {month} llevas {eur(p.variable_cents, decimals=False)} de gasto variable, {pace}."
        )
    else:
        second = f"En {month} llevas {eur(p.variable_cents, decimals=False)} de gasto variable."
    for lp in panel.portfolio:
        base = lp.value_cents - lp.day_change_cents
        day = f"{100 * lp.day_change_cents / base:+.1f} %".replace(".", ",") if base else ""
        second += (
            f"\nCartera: {eur(lp.value_cents, decimals=False)}"
            + (f" ({day} en la última sesión)" if lp.day_change_cents and day else "")
            + "."
        )
    start = datetime.combine(today, MORNING[0], now.tzinfo)
    end = datetime.combine(today, MORNING[1], now.tzinfo)
    return _notice(f"dia:{today}", "☀️ Buenos días", f"{first}\n{second}", start, end,
                   agenda=True, kind="dia")  # fmt: skip


def weekly(svc, panel, now: datetime) -> dict | None:
    """El domingo, a partir del mediodía (lo que haya entrado hasta la sync de la tarde)."""
    if now.weekday() != 6 or now.time() < time(12, 0):
        return None
    today = now.date()
    w = analyst.week(svc, last_day=today.isoformat())
    s = w.spend
    lines = []
    if w.habit_cents:
        diff = s.expense_cents - w.habit_cents
        word = "más" if diff > 0 else "menos"
        lines.append(
            f"Esta semana: {eur(s.expense_cents, decimals=False)} "
            f"({eur(abs(diff), decimals=False)} {word} que una semana normal)."
        )
    else:
        lines.append(f"Esta semana: {eur(s.expense_cents, decimals=False)}.")
    if s.by_category:
        lines.append("Más en: " + _names([(n, c) for n, c in s.by_category[:3]]) + ".")
    if s.top:
        lines.append(f"Mayor gasto: {s.top[0][0]} ({eur(s.top[0][1])}).")
    for r in w.rises:
        lines.append(f"⚠️ {r.merchant} ha subido a {eur(r.last_cents)}.")
    p = panel.pace
    if p.variable_habit_cents:
        diff = p.variable_cents - p.variable_habit_to_day_cents
        word = "por encima" if diff > 0 else "por debajo"
        lines.append(f"El mes va {eur(abs(diff), decimals=False)} {word} de tu ritmo.")
    if s.uncategorized:
        lines.append(f"{s.uncategorized} movimientos sin categoría.")
    start = datetime.combine(today, WEEKLY_AT, now.tzinfo)
    end = datetime.combine(today + timedelta(days=1), time(11, 0), now.tzinfo)
    return _notice(f"semana:{today}", "📊 Tu semana", "\n".join(lines), start, end,
                   kind="semana")  # fmt: skip


def alerts(svc, now: datetime) -> list[dict]:
    today = now.date()
    since = (today - timedelta(days=ALERT_DAYS)).isoformat()
    end = now + timedelta(days=ALERT_DAYS)
    out = []
    rec = analyst.recurring(svc, today=today.isoformat())
    for r in rec:
        key = analyst.series_key(r.merchant).replace(" ", "-")
        if r.previous_cents and r.last_day >= since:
            per_year = r.rise_cents * (12 if r.cadence == "mensual" else 1)
            body = (
                f"{r.merchant} te ha cobrado {eur(r.last_cents)} (antes {eur(r.previous_cents)}): "
                f"{eur(per_year)} más al año."
            )
            out.append(_notice(f"subida:{key}:{r.last_day}", "📈 Sube una cuota", body, now, end))
        if r.cadence == "mensual" and len(r.charges) == analyst.MIN_CHARGES and r.last_day >= since:
            body = f"{r.merchant}: {eur(r.last_cents)} al mes desde {short_day(r.first_seen)}."
            out.append(
                _notice(f"nueva:{key}:{r.first_seen}", "🔁 Nuevo cobro recurrente", body, now, end)
            )
        if r.cadence == "anual" and r.next_day:
            days = (date.fromisoformat(r.next_day) - today).days
            if 0 <= days <= 7:
                body = f"{r.merchant} se renueva hacia el {short_day(r.next_day)} ({eur(r.last_cents)} al año)."
                out.append(
                    _notice(f"renueva:{key}:{r.next_day}", "📅 Renovación anual", body, now, end)
                )
    for d in analyst.duplicates(svc, today=today.isoformat(), days=ALERT_DAYS):
        body = f"{d.count} cargos de {eur(d.cents)} en {d.merchant} el {short_day(d.day)}. ¿Es correcto?"
        out.append(
            _notice(
                f"duplicado:{d.day}:{d.merchant}:{d.cents}", "⚠️ ¿Cobro duplicado?", body, now, end
            )
        )
    return out


def ideas(sat, now: datetime) -> list[dict]:
    """Ideas del satélite que piden actuar (comprar o vender): una vez por semana y valor."""
    if sat is None:
        return []
    week = now.strftime("%G-W%V")
    out = []
    for i in sat.ideas:
        if i.kind == "comprar":
            body = (f"{i.name} cae un {i.reason.split(' ')[0][1:]} desde máximos. Idea: hasta "
                    f"{eur(i.amount_cents, decimals=False)} (stop {i.stop} €). Míralo en el panel.")  # fmt: skip
            title = "💡 Idea de compra"
        elif i.kind == "vender":
            body = f"{i.name}: {i.reason} Míralo en el panel."
            title = "💡 Idea de venta"
        else:
            continue
        out.append(_notice(f"idea:{i.kind}:{i.isin}:{week}", title, body, now,
                           now + timedelta(days=ALERT_DAYS)))  # fmt: skip
    return out


def build(svc, panel, now: datetime, *, satellite=None) -> list[dict]:
    items = [morning(svc, panel, now), weekly(svc, panel, now), *alerts(svc, now),
             *ideas(satellite, now)]  # fmt: skip
    return [n for n in items if n]


def write(folder: Path, notices: list[dict], *, generated_at: str) -> Path:
    data = {"version": 1, "generated_at": generated_at, "notices": notices}
    target = folder / FILE
    tmp = folder / (FILE + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, target)
    return target
