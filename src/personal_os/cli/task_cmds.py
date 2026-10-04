"""`pos task …`"""

from __future__ import annotations

import dataclasses

import typer

from personal_os.cli import wiring
from personal_os.cli.output import echo_json, fail
from personal_os.cli.parsing import parse_date, parse_time
from personal_os.core.events import ChangeContext
from personal_os.tasks import recurrence
from personal_os.tasks.models import TaskError

app = typer.Typer(help="Tareas.", no_args_is_help=True)


def _svc():
    return wiring.task_service(wiring.open_app())


def _fmt_due(date: str | None, time: str | None) -> str:
    if not date:
        return "—"
    return f"{date} {time}" if time else date


def _row(task, occ) -> dict:
    return {
        "task": dataclasses.asdict(task),
        "open_occurrence": dataclasses.asdict(occ) if occ else None,
    }


@app.command("add")
def add(
    title: str,
    due: str | None = typer.Option(
        None, "--due", "-d", help="YYYY-MM-DD | hoy | mañana | +N | lunes…"
    ),
    time: str | None = typer.Option(None, "--time", "-t", help="HH:MM (activa aviso en el iPhone)"),
    notes: str = typer.Option("", "--notes", "-n"),
    every: str | None = typer.Option(
        None, "--every", help="daily | weekly | monthly | yearly | weekdays"
    ),
    interval: int = typer.Option(1, "--interval", help="Cada N periodos"),
    on: str | None = typer.Option(None, "--on", help="Días: MO,TU,WE,TH,FR,SA,SU"),
    rrule: str | None = typer.Option(None, "--rrule", help="Regla RRULE explícita"),
    from_completion: bool = typer.Option(
        False, "--from-completion", help="Recurrencia desde la compleción"
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Crea una tarea (opcionalmente recurrente)."""
    svc = _svc()
    try:
        rule = rrule
        if every:
            if rrule:
                raise TaskError("Usa --every o --rrule, no ambos")
            rule = recurrence.build_rrule(every, interval, on)
        task, occ = svc.create_task(
            ChangeContext.cli(),
            title,
            notes=notes,
            due_date=parse_date(due, svc.timezone) if due else None,
            due_time=parse_time(time) if time else None,
            rrule=rule,
            anchor="completion" if from_completion else "schedule",
        )
    except TaskError as exc:
        fail(str(exc))
    if as_json:
        echo_json(_row(task, occ))
        return
    rec = f" · {recurrence.describe(task.rrule)}" if task.rrule else ""
    typer.echo(f"✓ {task.id}  {task.title}  [{_fmt_due(occ.due_date, occ.due_time)}]{rec}")
    typer.echo("  Ejecuta `pos sync` para enviarla al iPhone.")


@app.command("list")
def list_(
    all_: bool = typer.Option(False, "--all", "-a", help="Incluye completadas y canceladas"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Lista tareas activas (con su ocurrencia abierta)."""
    svc = _svc()
    rows = [(t, svc.open_occurrence(t.id)) for t in svc.list_tasks(include_closed=all_)]
    if as_json:
        echo_json([_row(t, o) for t, o in rows])
        return
    if not rows:
        typer.echo("No hay tareas.")
        return
    rows.sort(key=lambda r: ((r[1].due_date or "9999") if r[1] else "9999", r[0].created_at))
    for task, occ in rows:
        due = _fmt_due(occ.due_date, occ.due_time) if occ else "—"
        rec = f"  ↻ {recurrence.describe(task.rrule)}" if task.rrule else ""
        status = "" if task.status == "active" else f"  ({task.status})"
        typer.echo(f"{task.id}  {due:<16}  {task.title}{rec}{status}")


@app.command("show")
def show(ref: str, as_json: bool = typer.Option(False, "--json")) -> None:
    """Detalle de una tarea y sus ocurrencias."""
    svc = _svc()
    try:
        task, _ = svc.resolve_id(ref)
    except TaskError as exc:
        fail(str(exc))
    occs = svc.occurrences(task.id)
    if as_json:
        echo_json({"task": task, "occurrences": occs})
        return
    typer.echo(f"{task.id}  {task.title}  ({task.status})")
    if task.notes:
        typer.echo(f"  notas: {task.notes}")
    if task.rrule:
        typer.echo(
            f"  recurrencia: {recurrence.describe(task.rrule)} [{task.rrule}] ancla={task.recurrence_anchor}"
        )
    typer.echo(
        f"  origen: {task.origin_module}{' / ' + task.origin_ref if task.origin_ref else ''}"
    )
    for o in occs:
        extra = ""
        if o.status == "completed":
            extra = f" el {o.completed_at} vía {o.completed_via}"
        elif o.closed_reason:
            extra = f" ({o.closed_reason})"
        typer.echo(f"  #{o.seq} {o.id}  {_fmt_due(o.due_date, o.due_time):<16} {o.status}{extra}")


def _occurrence_ref(svc, ref: str):
    task, occ = svc.resolve_id(ref)
    if occ is None:
        raise TaskError(f"La tarea {task.id} no tiene ocurrencia abierta")
    return task, occ


@app.command("done")
def done(ref: str) -> None:
    """Completa la ocurrencia abierta (genera la siguiente si es recurrente)."""
    svc = _svc()
    try:
        task, occ = _occurrence_ref(svc, ref)
        _, nxt = svc.complete(ChangeContext.cli(), occ.id)
    except TaskError as exc:
        fail(str(exc))
    typer.echo(f"✓ Completada: {task.title}")
    if nxt:
        typer.echo(f"  Siguiente: {_fmt_due(nxt.due_date, nxt.due_time)}")


@app.command("skip")
def skip(ref: str) -> None:
    """Salta la ocurrencia abierta sin completarla (la serie continúa)."""
    svc = _svc()
    try:
        task, occ = _occurrence_ref(svc, ref)
        nxt = svc.skip(ChangeContext.cli(), occ.id)
    except TaskError as exc:
        fail(str(exc))
    typer.echo(f"↷ Saltada: {task.title}")
    if nxt:
        typer.echo(f"  Siguiente: {_fmt_due(nxt.due_date, nxt.due_time)}")


@app.command("reopen")
def reopen(ref: str) -> None:
    """Reabre una ocurrencia completada (indica el id de ocurrencia `occ_…`)."""
    svc = _svc()
    try:
        _, occ = svc.resolve_id(ref)
        if occ is None:
            raise TaskError("Indica el id de la ocurrencia (occ_…), ver `pos task show`")
        svc.reopen(ChangeContext.cli(), occ.id)
    except TaskError as exc:
        fail(str(exc))
    typer.echo("↺ Reabierta")


@app.command("edit")
def edit(
    ref: str,
    title: str | None = typer.Option(None, "--title"),
    notes: str | None = typer.Option(None, "--notes"),
    due: str | None = typer.Option(None, "--due", "-d"),
    time: str | None = typer.Option(None, "--time", "-t"),
    no_time: bool = typer.Option(False, "--no-time", help="Quita la hora"),
    no_due: bool = typer.Option(False, "--no-due", help="Quita la fecha"),
) -> None:
    """Edita título/notas (de la tarea) o fecha/hora (de la ocurrencia abierta)."""
    svc = _svc()
    ctx = ChangeContext.cli()
    try:
        task, occ = svc.resolve_id(ref)
        if title is not None or notes is not None:
            svc.update_task(ctx, task.id, title=title, notes=notes)
        if any([due, time, no_time, no_due]):
            if occ is None:
                raise TaskError("No hay ocurrencia abierta que reprogramar")
            kwargs: dict = {}
            if no_due:
                kwargs.update(due_date=None, due_time=None)
            else:
                if due:
                    kwargs["due_date"] = parse_date(due, svc.timezone)
                if time:
                    kwargs["due_time"] = parse_time(time)
                if no_time:
                    kwargs["due_time"] = None
            svc.reschedule(ctx, occ.id, **kwargs)
    except TaskError as exc:
        fail(str(exc))
    typer.echo("✓ Actualizada")


@app.command("cancel")
def cancel(ref: str) -> None:
    """Cancela la tarea (la serie completa)."""
    svc = _svc()
    try:
        task, _ = svc.resolve_id(ref)
        svc.cancel_task(ChangeContext.cli(), task.id)
    except TaskError as exc:
        fail(str(exc))
    typer.echo(f"✗ Cancelada: {task.title}")
