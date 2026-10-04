"""`pos event …`"""

from __future__ import annotations

import typer

from personal_os import bootstrap as wiring
from personal_os.calendar.models import CalendarError
from personal_os.cli.output import echo_json, fail
from personal_os.cli.parsing import parse_datetime, today
from personal_os.core import rrule as _rrule
from personal_os.core.events import ChangeContext

app = typer.Typer(
    help="Eventos de calendario (hora concreta o día completo).", no_args_is_help=True
)


def _svc():
    return wiring.calendar_service(wiring.open_app())


def _when(date: str, time: str | None) -> str:
    return f"{date}T{time}" if time else date


@app.command("add")
def add(
    title: str,
    start: str = typer.Option(
        ..., "--start", "-s", help="'2026-10-06 18:00' | 'mañana 18:00' | fecha (día completo)"
    ),
    end: str | None = typer.Option(None, "--end", "-e", help="Fin (misma sintaxis)"),
    duration: int | None = typer.Option(
        None, "--duration", "-m", help="Duración en minutos (60 por defecto)"
    ),
    alert: list[int] = typer.Option([], "--alert", "-a", help="Aviso N minutos antes (repetible)"),
    notes: str = typer.Option("", "--notes", "-n"),
    location: str = typer.Option("", "--location", "-l"),
    every: str | None = typer.Option(
        None, "--every", help="daily | weekly | monthly | yearly | weekdays"
    ),
    rrule: str | None = typer.Option(None, "--rrule"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Crea un evento. Sin hora en --start ⇒ evento de día completo."""
    svc = _svc()
    s_date, s_time = parse_datetime(start, svc.timezone)
    all_day = s_time is None
    ends_at = None
    if end:
        e_date, e_time = parse_datetime(end, svc.timezone)
        if all_day and e_time:
            fail("Un evento de día completo no lleva hora de fin")
        ends_at = _when(e_date, e_time if not all_day else None)
        if not all_day and e_time is None:
            fail("Indica la hora de fin")
    rule = rrule
    if every:
        try:
            rule = _rrule.normalize_rrule(
                {
                    "daily": "FREQ=DAILY",
                    "weekly": "FREQ=WEEKLY",
                    "monthly": "FREQ=MONTHLY",
                    "yearly": "FREQ=YEARLY",
                    "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
                }[every]
            )
        except KeyError:
            fail("--every debe ser daily | weekly | monthly | yearly | weekdays")
    try:
        ev = svc.create(
            ChangeContext.cli(),
            title,
            starts_at=_when(s_date, s_time),
            ends_at=ends_at,
            duration_minutes=duration,
            all_day=all_day,
            notes=notes,
            location=location,
            rrule=rule,
            alerts=alert,
        )
    except CalendarError as exc:
        fail(str(exc))
    if as_json:
        echo_json(ev)
        return
    typer.echo(
        f"✓ {ev.id}  {ev.title}  {ev.starts_at} → {ev.ends_at}"
        + (f"  avisos: {list(ev.alerts)} min" if ev.alerts else "")
    )
    typer.echo("  Ejecuta `pos sync` para enviarlo a Calendario.")


@app.command("list")
def list_(
    all_: bool = typer.Option(False, "--all", "-a", help="Incluye pasados y cancelados"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Próximos eventos."""
    svc = _svc()
    events = svc.list(
        include_cancelled=all_, since=None if all_ else today(svc.timezone).isoformat()
    )
    if as_json:
        echo_json(events)
        return
    if not events:
        typer.echo("No hay eventos.")
    for ev in events:
        rec = f"  ↻ {_rrule.describe(ev.rrule)}" if ev.rrule and "FREQ=" in ev.rrule else ""
        status = "" if ev.status == "confirmed" else f"  ({ev.status})"
        typer.echo(f"{ev.id}  {ev.starts_at:<16}  {ev.title}{rec}{status}")


@app.command("edit")
def edit(
    ref: str,
    title: str | None = typer.Option(None, "--title"),
    start: str | None = typer.Option(None, "--start", "-s"),
    end: str | None = typer.Option(None, "--end", "-e"),
    notes: str | None = typer.Option(None, "--notes", "-n"),
    location: str | None = typer.Option(None, "--location", "-l"),
    alert: list[int] | None = typer.Option(None, "--alert", "-a", help="Sustituye los avisos"),
    no_alerts: bool = typer.Option(False, "--no-alerts"),
) -> None:
    """Edita un evento (si cambias el inicio sin fin, se conserva la duración)."""
    from datetime import date, datetime

    svc = _svc()
    try:
        ev = svc.resolve_id(ref)
        fields: dict = {}
        if title is not None:
            fields["title"] = title
        if notes is not None:
            fields["notes"] = notes
        if location is not None:
            fields["location"] = location
        if alert:
            fields["alerts"] = tuple(alert)
        if no_alerts:
            fields["alerts"] = ()
        if start:
            d, t = parse_datetime(start, svc.timezone)
            if (t is None) != ev.all_day:
                fail("No se puede cambiar entre día completo y con hora; cancela y crea otro")
            new_start = _when(d, t)
            if end is None:
                parse = date.fromisoformat if ev.all_day else datetime.fromisoformat
                delta = parse(ev.ends_at) - parse(ev.starts_at)
                fmt = "%Y-%m-%d" if ev.all_day else "%Y-%m-%dT%H:%M"
                fields["ends_at"] = (parse(new_start) + delta).strftime(fmt)
            fields["starts_at"] = new_start
        if end:
            d, t = parse_datetime(end, svc.timezone)
            fields["ends_at"] = _when(d, t)
        svc.update(ChangeContext.cli(), ev.id, **fields)
    except CalendarError as exc:
        fail(str(exc))
    typer.echo("✓ Actualizado")


@app.command("cancel")
def cancel(ref: str) -> None:
    """Cancela un evento (se borrará del calendario en la próxima sync)."""
    svc = _svc()
    try:
        ev = svc.resolve_id(ref)
        svc.cancel(ChangeContext.cli(), ev.id)
    except CalendarError as exc:
        fail(str(exc))
    typer.echo(f"✗ Cancelado: {ev.title}")
