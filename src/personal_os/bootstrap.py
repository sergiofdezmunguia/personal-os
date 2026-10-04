"""Composition root: el único sitio que conoce todas las piezas y las conecta.

Lo usan las interfaces (CLI, MCP). Ninguna otra capa lo importa.
"""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from personal_os.core.clock import Clock, SystemClock
from personal_os.core.config import Config, ConfigError, load_config
from personal_os.core.db import Database

# (namespace, paquete que contiene migrations/). El orden importa por las FKs.
MIGRATIONS = [
    ("core", "personal_os.core"),
    ("tasks", "personal_os.tasks"),
    ("calendar", "personal_os.calendar"),
    ("sync", "personal_os.sync"),
]

BRIDGE_SOURCE = (
    Path(__file__).resolve().parents[2] / "bridge" / "scriptable" / "personal-os-sync.js"
)
BRIDGE_SCRIPT_NAME = "Personal OS Sync.js"


@dataclass
class App:
    config: Config
    db: Database
    clock: Clock


def open_app(config: Config | None = None, clock: Clock | None = None) -> App:
    cfg = config or load_config()
    db = Database(cfg.db_path)
    db.migrate(MIGRATIONS)
    return App(config=cfg, db=db, clock=clock or SystemClock())


def mailbox_dir(cfg: Config) -> Path:
    if cfg.apple.mailbox_dir is None:
        raise ConfigError(
            f"Falta [apple].mailbox_dir en {cfg.config_file}. Ver docs/setup/iphone.md."
        )
    return cfg.apple.mailbox_dir


def reminders_gateway(cfg: Config):
    from personal_os.adapters.apple.reminders_mailbox import MailboxRemindersGateway

    return MailboxRemindersGateway(mailbox_dir(cfg), cfg.apple.reminders_list)


# --------------------------------------------------------------------------- servicios


def task_service(application: App):
    from personal_os.core.events import EventLog
    from personal_os.tasks.service import TaskService

    return TaskService(
        application.db,
        application.clock,
        EventLog(application.db, application.clock),
        application.config.timezone,
    )


def reminders_sync(application: App):
    from personal_os.core.events import EventLog
    from personal_os.sync.reminders_sync import RemindersSync

    return RemindersSync(
        application.db,
        application.clock,
        EventLog(application.db, application.clock),
        task_service(application),
        reminders_gateway(application.config),
        application.config.timezone,
    )


@contextmanager
def sync_lock(cfg: Config) -> Iterator[None]:
    """Impide dos sincronizaciones simultáneas."""
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.data_dir / "sync.lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Ya hay una sincronización en curso") from exc
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def calendar_configured(cfg: Config) -> bool:
    from personal_os.core.secrets import get_secret

    return bool(cfg.apple.apple_id) and get_secret("icloud_app_password") is not None


def calendar_service(application: App):
    from personal_os.calendar.service import CalendarService
    from personal_os.core.events import EventLog

    return CalendarService(
        application.db,
        application.clock,
        EventLog(application.db, application.clock),
        application.config.timezone,
    )


def calendar_gateway(cfg: Config):
    from personal_os.adapters.apple.calendar_caldav import CalDavCalendarGateway
    from personal_os.core.secrets import require_secret

    if not cfg.apple.apple_id:
        raise ConfigError(f"Falta [apple].apple_id en {cfg.config_file}")
    return CalDavCalendarGateway(
        cfg.apple.caldav_url,
        cfg.apple.apple_id,
        require_secret("icloud_app_password"),
        cfg.apple.calendar_name,
        cfg.timezone,
    )


def calendar_sync(application: App):
    from personal_os.core.events import EventLog
    from personal_os.sync.calendar_sync import CalendarSync

    return CalendarSync(
        application.db,
        application.clock,
        EventLog(application.db, application.clock),
        calendar_service(application),
        calendar_gateway(application.config),
        application.config.timezone,
    )


@dataclass
class SyncOutcome:
    reports: list
    backup: object | None = None  # BackupInfo si se hizo backup automático
    offsite: object | None = None  # BackupInfo si se subió una copia cifrada
    backup_warnings: list[str] = field(default_factory=list)


def auto_backup(application: App) -> tuple[object | None, object | None, list[str]]:
    """Backup diario automático (si está activado y el último tiene > 24 h) + poda, y copia
    cifrada fuera de este disco si está configurada. Nunca interrumpe la sync: avisa."""
    from personal_os.core import backup as bk

    cfg = application.config
    if not cfg.backup.auto_daily or not cfg.db_path.exists():
        return None, None, []
    info, warnings = None, []
    if bk.needs_auto_backup(cfg.backup_dir, application.clock):
        try:
            info = bk.create_backup(cfg.db_path, cfg.backup_dir, application.clock)
            _, pruned = bk.prune(
                cfg.backup_dir,
                keep_daily=cfg.backup.keep_daily,
                keep_weekly=cfg.backup.keep_weekly,
                keep_monthly=cfg.backup.keep_monthly,
            )
            warnings += pruned
        except (bk.BackupError, OSError) as exc:
            warnings.append(f"Backup automático fallido: {exc}")
    pushed, offsite_warnings = sync_offsite(cfg)
    return info, pushed, warnings + offsite_warnings


def sync_offsite(cfg: Config) -> tuple[object | None, list[str]]:
    """Sube el último backup local cifrado si falta fuera y poda allí. (None, []) si no está
    configurado."""
    from personal_os.core import offsite

    if not cfg.backup.offsite_enabled:
        return None, []
    try:
        result = offsite.sync(
            cfg.backup_dir,
            cfg.backup.offsite_dir,
            offsite.parse_recipient(cfg.backup.offsite_recipient),
            keep_daily=cfg.backup.keep_daily,
            keep_weekly=cfg.backup.keep_weekly,
            keep_monthly=cfg.backup.keep_monthly,
        )
    except (offsite.OffsiteError, OSError) as exc:
        return None, [f"Copia externa fallida: {exc}"]
    return result.pushed, result.warnings


def run_sync(application: App, *, only: str | None = None, push: bool = True) -> SyncOutcome:
    """Backup diario si toca, y sincroniza Recordatorios y (si está configurado) Calendario."""
    if only not in (None, "reminders", "calendar"):
        raise ValueError("only debe ser 'reminders', 'calendar' o None")
    outcome = SyncOutcome(reports=[])
    with sync_lock(application.config):
        outcome.backup, outcome.offsite, outcome.backup_warnings = auto_backup(application)
        if only in (None, "reminders"):
            outcome.reports.append(reminders_sync(application).run(push=push))
        if only in (None, "calendar"):
            if calendar_configured(application.config):
                outcome.reports.append(calendar_sync(application).run(push=push))
            elif only == "calendar":
                raise ConfigError(
                    "Calendario no configurado (apple_id + `pos secrets set icloud_app_password`)"
                )
    return outcome
