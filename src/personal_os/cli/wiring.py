"""Composition root: el único sitio que conoce todas las piezas y las conecta."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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
    Path(__file__).resolve().parents[3] / "bridge" / "scriptable" / "personal-os-sync.js"
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
