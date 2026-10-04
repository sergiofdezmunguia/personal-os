"""`pos doctor` y `pos backup …`"""

from __future__ import annotations

import typer

from personal_os import bootstrap
from personal_os.cli.output import echo_json, fail
from personal_os.core import backup as bk
from personal_os.core import offsite
from personal_os.core.clock import SystemClock
from personal_os.core.config import Config, load_config
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
    if cfg.backup.offsite_enabled:
        try:
            remote = offsite.push(
                info, cfg.backup.offsite_dir, offsite.parse_recipient(cfg.backup.offsite_recipient)
            )
        except (offsite.OffsiteError, OSError) as exc:
            fail(f"El backup local está hecho, pero la copia externa ha fallado: {exc}")
        typer.echo(f"✓ copia cifrada en {remote.path}")


@backup_app.command("list")
def list_(
    as_json: bool = typer.Option(False, "--json"),
    remote: bool = typer.Option(False, "--offsite", help="Lista la copia externa cifrada"),
) -> None:
    """Backups disponibles (del más antiguo al más reciente)."""
    cfg = load_config()
    where = _offsite_dir(cfg) if remote else cfg.backup_dir
    items = bk.list_backups(where)
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
        typer.echo(f"No hay backups en {where}")
    for b in items:
        typer.echo(_describe(b))


@backup_app.command("verify")
def verify(
    ref: str = typer.Argument("latest", help="latest | nombre | prefijo | all"),
    remote: bool = typer.Option(
        False, "--offsite", help="Verifica la copia externa (descifra con la clave privada)"
    ),
) -> None:
    """Verifica un backup (descompresión, sha256, integridad, filas)."""
    cfg = load_config()
    where = _offsite_dir(cfg) if remote else cfg.backup_dir
    identity = _identity(cfg) if remote else None
    try:
        items = bk.list_backups(where) if ref == "all" else [bk.resolve_backup(where, ref)]
    except bk.BackupError as exc:
        fail(str(exc))
    bad = 0
    for b in items:
        r = bk.verify_backup(b, identity)
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
    if cfg.backup.offsite_enabled:
        removed_remote, remote_warnings = bk.prune(
            cfg.backup.offsite_dir,
            keep_daily=cfg.backup.keep_daily,
            keep_weekly=cfg.backup.keep_weekly,
            keep_monthly=cfg.backup.keep_monthly,
        )
        typer.echo(f"{len(removed_remote)} copia(s) externas eliminadas")
        warnings += remote_warnings
    for w in warnings:
        typer.secho(f"  aviso: {w}", fg=typer.colors.YELLOW)
    if warnings:
        raise typer.Exit(1)


# --------------------------------------------------------------------------- copia externa


def _offsite_dir(cfg: Config):
    if not cfg.backup.offsite_enabled:
        fail("Copia externa no configurada ([backup].offsite_dir). Ver `pos backup keygen`.")
    return cfg.backup.offsite_dir


def _identity(cfg: Config):
    try:
        return offsite.load_identity(cfg.offsite_identity_path)
    except offsite.OffsiteError as exc:
        fail(str(exc))


@backup_app.command("keygen")
def keygen() -> None:
    """Genera la clave age de la copia externa (privada en config, 0600; nunca sobrescribe)."""
    cfg = load_config()
    try:
        public = offsite.generate_identity(cfg.offsite_identity_path)
    except offsite.OffsiteError as exc:
        fail(str(exc))
    typer.echo(f"✓ Clave privada creada en {cfg.offsite_identity_path} (permisos 0600)")
    typer.echo(f"  Clave pública: {public}\n")
    typer.echo("1. GUARDA UNA COPIA de la clave privada en tu gestor de contraseñas.")
    typer.echo("   Sin ella, los backups externos NO se pueden recuperar.")
    typer.echo(f"2. Añade a {cfg.config_file}, en [backup]:")
    typer.echo('   offsite_dir = "/mnt/c/Users/<usuario>/iCloudDrive/PersonalOS-backups"')
    typer.echo(f'   offsite_recipient = "{public}"')
    typer.echo("3. Ejecuta `uv run pos backup offsite` y después `uv run pos doctor`.")


@backup_app.command("offsite")
def offsite_() -> None:
    """Sube ahora el último backup local (cifrado) si falta fuera, y aplica la retención allí."""
    cfg = load_config()
    _offsite_dir(cfg)
    pushed, warnings = bootstrap.sync_offsite(cfg)
    if pushed is not None:
        typer.echo(f"✓ copia cifrada en {pushed.path}")
    elif not warnings:
        typer.echo("✓ La copia externa ya está al día")
    for w in warnings:
        typer.secho(f"  aviso: {w}", fg=typer.colors.YELLOW)
    if warnings:
        raise typer.Exit(1)


@backup_app.command("fetch")
def fetch(ref: str = typer.Argument("latest", help="latest | nombre | prefijo")) -> None:
    """Descifra un backup externo al directorio local (para `drill` o `restore`)."""
    cfg = load_config()
    identity = _identity(cfg)
    try:
        remote = bk.resolve_backup(_offsite_dir(cfg), ref)
        local = offsite.fetch(remote, cfg.backup_dir, identity)
    except (bk.BackupError, offsite.OffsiteError) as exc:
        fail(str(exc))
    typer.echo(f"✓ {_describe(local)}\n  en {cfg.backup_dir}")
    typer.echo(f"  Siguiente: uv run pos backup drill {local.path.name}")
