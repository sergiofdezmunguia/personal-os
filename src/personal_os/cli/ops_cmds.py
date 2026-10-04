"""`pos doctor` y `pos backup …`"""

from __future__ import annotations

import typer

from personal_os import bootstrap
from personal_os.cli.output import echo_json, fail
from personal_os.core import backup as bk
from personal_os.core.clock import SystemClock
from personal_os.core.config import load_config
from personal_os.ops.doctor import FAIL, OK, SKIP, WARN, run_doctor
from personal_os.ops.drill import run_drill

backup_app = typer.Typer(
    help="Backups de la base de datos (crear, verificar, restaurar).", no_args_is_help=True
)

_ICON = {
    OK: ("✓", typer.colors.GREEN),
    WARN: ("!", typer.colors.YELLOW),
    FAIL: ("✗", typer.colors.RED),
    SKIP: ("·", None),
}


def doctor(
    offline: bool = typer.Option(False, "--offline", help="No contacta con iCloud (CalDAV)"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Diagnóstico: config, secretos, SQLite, backups, buzón, bridge, CalDAV, zona horaria y sync."""
    checks = run_doctor(offline=offline)
    if as_json:
        echo_json(checks)
    else:
        for c in checks:
            icon, color = _ICON[c.status]
            typer.secho(f"{icon} {c.area:<14} {c.message}", fg=color)
            if c.hint and c.status in (WARN, FAIL):
                typer.echo(f"  → {c.hint}")
        counts = {s: sum(c.status == s for c in checks) for s in (OK, WARN, FAIL)}
        typer.echo(f"\n{counts[OK]} ok · {counts[WARN]} avisos · {counts[FAIL]} fallos")
    if any(c.status == FAIL for c in checks):
        raise typer.Exit(1)


def _describe(b: bk.BackupInfo) -> str:
    m = b.manifest or {}
    rows = sum((m.get("tables") or {}).values())
    size = (m.get("size_gz") or b.path.stat().st_size) / 1024
    label = f" [{b.label}]" if b.label else ""
    return f"{b.path.name}{label}  {size:.0f} KiB  {rows} filas"


@backup_app.command("create")
def create(
    label: str | None = typer.Option(None, "--label", help="Etiqueta (no se poda nunca)"),
) -> None:
    """Crea y verifica un backup ahora."""
    cfg = load_config()
    try:
        info = bk.create_backup(cfg.db_path, cfg.backup_dir, SystemClock(), label=label)
    except bk.BackupError as exc:
        fail(str(exc))
    typer.echo(f"✓ {_describe(info)}\n  en {cfg.backup_dir}")


@backup_app.command("list")
def list_(as_json: bool = typer.Option(False, "--json")) -> None:
    """Backups disponibles (del más antiguo al más reciente)."""
    cfg = load_config()
    items = bk.list_backups(cfg.backup_dir)
    if as_json:
        echo_json(
            [
                {
                    "file": b.path.name,
                    "created_at": b.created_at,
                    "label": b.label,
                    "manifest": b.manifest,
                }
                for b in items
            ]
        )
        return
    if not items:
        typer.echo(f"No hay backups en {cfg.backup_dir}")
    for b in items:
        typer.echo(_describe(b))


@backup_app.command("verify")
def verify(ref: str = typer.Argument("latest", help="latest | nombre | prefijo | --all")) -> None:
    """Verifica un backup (descompresión, sha256, integridad, filas)."""
    cfg = load_config()
    items = (
        bk.list_backups(cfg.backup_dir)
        if ref == "all"
        else [bk.resolve_backup(cfg.backup_dir, ref)]
    )
    bad = 0
    for b in items:
        r = bk.verify_backup(b)
        if r.ok:
            typer.secho(f"✓ {b.path.name}", fg=typer.colors.GREEN)
        else:
            bad += 1
            typer.secho(f"✗ {b.path.name}: {'; '.join(r.problems)}", fg=typer.colors.RED)
    if bad:
        raise typer.Exit(1)


@backup_app.command("drill")
def drill(
    ref: str | None = typer.Argument(None, help="Backup a probar (por defecto: crea uno nuevo)"),
) -> None:
    """Simulacro: restaura en un directorio temporal y comprueba que los datos se leen. No toca la base real."""
    application = bootstrap.open_app()
    try:
        report = run_drill(application, ref)
    finally:
        application.db.close()
    for name, ok, detail in report.steps:
        typer.secho(
            f"{'✓' if ok else '✗'} {name:<22} {detail}",
            fg=typer.colors.GREEN if ok else typer.colors.RED,
        )
    if not report.ok:
        raise typer.Exit(1)
    typer.echo("Simulacro superado: el backup es restaurable y utilizable.")


@backup_app.command("restore")
def restore(
    ref: str = typer.Argument(..., help="latest | nombre | prefijo"),
    yes: bool = typer.Option(
        False, "--yes", help="Confirmo que quiero sustituir la base de datos actual"
    ),
) -> None:
    """Restaura un backup sobre la base de datos real (antes guarda una copia pre-restore)."""
    cfg = load_config()
    try:
        info = bk.resolve_backup(cfg.backup_dir, ref)
    except bk.BackupError as exc:
        fail(str(exc))
    if not yes:
        typer.echo(
            f"Se sustituirá {cfg.db_path} por {info.path.name} ({info.created_at:%Y-%m-%d %H:%M} UTC)."
        )
        typer.echo("Antes se guardará una copia 'pre-restore' del estado actual.")
        typer.echo("Repite con --yes para continuar.")
        raise typer.Exit(2)
    try:
        with bootstrap.sync_lock(cfg):
            result = bk.restore_backup(
                info, cfg.db_path, cfg.backup_dir, SystemClock(), bootstrap.MIGRATIONS
            )
    except (bk.BackupError, RuntimeError) as exc:
        fail(str(exc))
    typer.echo(f"✓ Restaurado {info.path.name}")
    if result.pre_restore:
        typer.echo(f"  Estado anterior guardado en {result.pre_restore.path.name}")
    if result.migrations_applied:
        typer.echo(f"  Migraciones aplicadas: {', '.join(result.migrations_applied)}")
    typer.echo(
        "  Ejecuta `pos sync` para reconciliar con Apple (los cambios posteriores al backup se detectarán)."
    )


@backup_app.command("prune")
def prune_() -> None:
    """Aplica la política de retención ([backup].keep_*)."""
    cfg = load_config()
    removed, warnings = bk.prune(
        cfg.backup_dir,
        keep_daily=cfg.backup.keep_daily,
        keep_weekly=cfg.backup.keep_weekly,
        keep_monthly=cfg.backup.keep_monthly,
    )
    typer.echo(f"{len(removed)} backup(s) eliminados")
    for w in warnings:
        typer.secho(f"  aviso: {w}", fg=typer.colors.YELLOW)
