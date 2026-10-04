"""CLI `pos`: interfaz de desarrollo y pruebas. Sin lógica de negocio: solo llama a servicios."""

from __future__ import annotations

import getpass
import json
import shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import typer

from personal_os import bootstrap as wiring
from personal_os.cli import event_cmds, ops_cmds, task_cmds
from personal_os.cli.output import echo_json as _echo_json
from personal_os.cli.output import fail
from personal_os.core import secrets as secrets_mod
from personal_os.core.config import ConfigError, load_config, write_config_template
from personal_os.core.ids import new_id, ulid
from personal_os.sync.ports import Due, ReminderDelete, ReminderState, ReminderUpsert

app = typer.Typer(help="Personal OS — CLI de desarrollo.", no_args_is_help=True)
secrets_app = typer.Typer(help="Secretos locales (nunca en git).", no_args_is_help=True)
bridge_app = typer.Typer(
    help="Bridge iPhone (Scriptable) y buzón de iCloud Drive.", no_args_is_help=True
)
sync_app = typer.Typer(help="Sincronización con Apple.", invoke_without_command=True)
app.add_typer(task_cmds.app, name="task")
app.add_typer(event_cmds.app, name="event")
app.add_typer(ops_cmds.backup_app, name="backup")
app.command("doctor")(ops_cmds.doctor)
app.add_typer(sync_app, name="sync")
app.add_typer(secrets_app, name="secrets")
app.add_typer(bridge_app, name="bridge")


# --------------------------------------------------------------------------------------
# init / secrets


@app.command()
def init() -> None:
    """Crea la configuración local (si no existe) y aplica migraciones."""
    path = write_config_template()
    application = wiring.open_app()
    typer.echo(f"Config:   {path}")
    typer.echo(f"Base de datos: {application.config.db_path}")
    typer.echo(f"Secretos: {secrets_mod.secrets_path()}")


@secrets_app.command("set")
def secrets_set(name: str) -> None:
    """Guarda un secreto pidiéndolo por teclado (no se muestra ni queda en el historial)."""
    value = getpass.getpass(f"{name}: ").strip()
    path = secrets_mod.set_secret(name, value)
    typer.echo(f"Guardado en {path} (permisos 0600).")


@secrets_app.command("status")
def secrets_status() -> None:
    """Muestra qué secretos están configurados, sin revelar valores."""
    _echo_json(secrets_mod.secret_status())


# --------------------------------------------------------------------------------------
# bridge


@bridge_app.command("install")
def bridge_install() -> None:
    """Copia el script del bridge a la carpeta de Scriptable en iCloud Drive."""
    cfg = load_config()
    mailbox = wiring.mailbox_dir(cfg)
    scriptable_dir = mailbox.parent
    if not scriptable_dir.exists():
        raise typer.BadParameter(f"No existe la carpeta de Scriptable: {scriptable_dir}")
    source = wiring.BRIDGE_SOURCE.read_text(encoding="utf-8")
    source = source.replace(
        'const LIST_NAME = "Personal OS";',
        f"const LIST_NAME = {json.dumps(cfg.apple.reminders_list)};",
    ).replace('const MAILBOX = "personal-os";', f"const MAILBOX = {json.dumps(mailbox.name)};")
    target = scriptable_dir / wiring.BRIDGE_SCRIPT_NAME
    tmp = target.with_suffix(".tmp")
    tmp.write_text(source, encoding="utf-8")
    shutil.move(tmp, target)
    wiring.reminders_gateway(cfg).ensure_dirs()
    typer.echo(f"Instalado: {target}")


@bridge_app.command("status")
def bridge_status() -> None:
    """Estado del buzón (lotes pendientes, acks y snapshots sin consumir)."""
    status = wiring.reminders_gateway(load_config()).status()
    _echo_json(status)


@bridge_app.command("inbox")
def bridge_inbox() -> None:
    """Muestra acks y snapshots recibidos del iPhone, sin consumirlos."""
    inbox = wiring.reminders_gateway(load_config()).collect()
    _echo_json(
        {
            "acks": list(inbox.acks),
            "snapshots": list(inbox.snapshots),
            "invalid_files": list(inbox.invalid_files),
        }
    )


def _probe_file():
    return load_config().data_dir / "bridge-probes.json"


@bridge_app.command("probe")
def bridge_probe(
    minutes: int = typer.Option(3, help="Vencimiento dentro de N minutos (para probar el aviso)."),
) -> None:
    """Envía un recordatorio de prueba al iPhone (sin pasar por el dominio de tareas)."""
    cfg = load_config()
    gateway = wiring.reminders_gateway(cfg)
    due_local = datetime.now(ZoneInfo(cfg.timezone)) + timedelta(minutes=minutes)
    marker = f"prb_{ulid()}"
    batch_id = new_id("batch")
    gateway.submit(
        batch_id,
        [
            ReminderUpsert(
                op_id=new_id("operation"),
                marker=marker,
                external_id=None,
                expected=None,
                fields=ReminderState(
                    title=f"Prueba Personal OS {due_local:%H:%M}",
                    notes="Recordatorio de prueba del bridge. Puedes completarlo.",
                    due=Due(date=due_local.date().isoformat(), time=f"{due_local:%H:%M}"),
                    is_completed=False,
                ),
            )
        ],
    )
    path = _probe_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    probes = json.loads(path.read_text()) if path.exists() else []
    probes.append(marker)
    path.write_text(json.dumps(probes))
    typer.echo(f"Lote {batch_id} escrito. Marcador: {marker}. Vence a las {due_local:%H:%M}.")


@bridge_app.command("probe-cleanup")
def bridge_probe_cleanup() -> None:
    """Envía al iPhone el borrado de todos los recordatorios de prueba."""
    gateway = wiring.reminders_gateway(load_config())
    path = _probe_file()
    probes = json.loads(path.read_text()) if path.exists() else []
    if not probes:
        typer.echo("No hay pruebas registradas.")
        return
    gateway.submit(
        new_id("batch"),
        [
            ReminderDelete(op_id=new_id("operation"), marker=m, external_id=None, expected=None)
            for m in probes
        ],
    )
    path.write_text("[]")
    typer.echo(f"Enviado borrado de {len(probes)} recordatorios de prueba.")


# --------------------------------------------------------------------------------------
# sync


def _print_report(report) -> None:
    stats = ", ".join(f"{k}={v}" for k, v in sorted(report.stats.items())) or "sin cambios"
    typer.echo(f"[{report.provider}] {stats}")
    for w in report.warnings:
        typer.secho(f"  aviso: {w}", fg=typer.colors.YELLOW)
    for e in report.errors:
        typer.secho(f"  error: {e}", fg=typer.colors.RED)


@sync_app.callback()
def sync_run(
    ctx: typer.Context,
    only: str | None = typer.Option(None, "--only", help="reminders | calendar"),
    no_push: bool = typer.Option(False, "--no-push", help="Solo recibe cambios"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Sincroniza con Recordatorios y Calendario (`pos sync status` para el estado)."""
    if ctx.invoked_subcommand is not None:
        return
    application = wiring.open_app()
    try:
        outcome = wiring.run_sync(application, only=only, push=not no_push)
    except (ValueError, ConfigError, RuntimeError) as exc:
        fail(str(exc))
    reports = outcome.reports
    if outcome.backup is not None and not as_json:
        typer.echo(f"[backup] {outcome.backup.path.name}")
    for w in outcome.backup_warnings:
        typer.secho(f"  aviso: {w}", fg=typer.colors.YELLOW)
    if as_json:
        _echo_json(reports)
        return
    for r in reports:
        _print_report(r)
    if any(r.stats.get("commands_sent") for r in reports):
        typer.echo(
            "  Los cambios llegarán a Recordatorios en la próxima ejecución del bridge en el iPhone."
        )


@sync_app.command("status")
def sync_status(as_json: bool = typer.Option(False, "--json")) -> None:
    """Últimas ejecuciones, buzón y enlaces con problemas."""
    from personal_os.sync.store import LinkStore, SyncRuns

    application = wiring.open_app()
    runs = SyncRuns(application.db, application.clock).recent(5)
    problems = LinkStore(application.db, application.clock).conflicts()
    data: dict = {"runs": runs, "problem_links": problems}
    try:
        data["mailbox"] = wiring.reminders_gateway(application.config).status()
    except Exception as exc:  # buzón no configurado
        data["mailbox"] = f"no disponible: {exc}"
    if as_json:
        _echo_json(data)
        return
    typer.echo("Últimas sincronizaciones:")
    for r in runs:
        typer.echo(
            f"  {r['started_at']}  {r['provider']:<16} {r['status']:<6} {r['stats'].get('stats', {})}"
        )
    mailbox = data["mailbox"]
    if isinstance(mailbox, str):
        typer.echo(f"Buzón: {mailbox}")
    else:
        typer.echo(
            f"Buzón: {len(mailbox.pending_batches)} lotes sin procesar por el iPhone, "
            f"{mailbox.acks} acks, {mailbox.snapshots} snapshots"
        )
    typer.echo(f"Enlaces con error/conflicto: {len(problems)}")
    for link in problems:
        typer.echo(
            f"  {link.entity_id} [{link.provider}] {link.sync_state}: {link.last_error or ''}"
        )


@app.command("log")
def log(
    entity: str | None = typer.Option(
        None, "--entity", "-e", help="id (tarea, ocurrencia, evento)"
    ),
    type_prefix: str | None = typer.Option(None, "--type", help="Prefijo de tipo, p. ej. sync."),
    limit: int = typer.Option(30, "--limit", "-n"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Log de eventos de dominio (auditoría)."""
    from personal_os.core.events import EventLog

    application = wiring.open_app()
    events = EventLog(application.db, application.clock).query(
        entity_id=entity, type_prefix=type_prefix, limit=limit
    )
    if as_json:
        _echo_json(events)
        return
    for e in reversed(events):
        payload = json.dumps(e.payload, ensure_ascii=False)
        typer.echo(f"{e.occurred_at}  {e.actor:<6} {e.type:<32} {e.entity_id}  {payload}")


@app.command("ops")
def ops(
    entity: str | None = typer.Option(None, "--entity", "-e"),
    limit: int = typer.Option(30, "--limit", "-n"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Operaciones enviadas a Apple (qué ha creado/modificado el Personal OS)."""
    from personal_os.sync.store import OperationLog

    application = wiring.open_app()
    items = OperationLog(application.db, application.clock).recent(limit, entity)
    if as_json:
        _echo_json(items)
        return
    for op in reversed(items):
        typer.echo(
            f"{op.created_at}  {op.provider:<16} {op.operation:<7} {op.status:<10} "
            f"{op.entity_id}  {op.external_id or ''}"
        )


if __name__ == "__main__":
    app()
