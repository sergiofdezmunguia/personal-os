"""Configuración local. Vive FUERA del repositorio:

- configuración: $POS_CONFIG_DIR o ~/.config/personal-os/config.toml
- datos (SQLite):  $POS_DATA_DIR  o ~/.local/share/personal-os/

Nunca contiene secretos (ver core/secrets.py).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Europe/Madrid"


def config_dir() -> Path:
    env = os.environ.get("POS_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".config" / "personal-os"


def data_dir() -> Path:
    env = os.environ.get("POS_DATA_DIR")
    return Path(env) if env else Path.home() / ".local" / "share" / "personal-os"


@dataclass(frozen=True)
class AppleConfig:
    apple_id: str | None = None
    reminders_list: str = "Personal OS"
    calendar_name: str = "Personal OS"
    caldav_url: str = "https://caldav.icloud.com"
    mailbox_dir: Path | None = None


@dataclass(frozen=True)
class BackupConfig:
    dir: Path | None = None  # None ⇒ <data_dir>/backups
    auto_daily: bool = True  # backup automático al sincronizar si el último tiene > 24 h
    keep_daily: int = 14
    keep_weekly: int = 8
    keep_monthly: int = 12


@dataclass(frozen=True)
class Config:
    timezone: str = DEFAULT_TIMEZONE
    apple: AppleConfig = field(default_factory=AppleConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    config_dir: Path = field(default_factory=config_dir)
    data_dir: Path = field(default_factory=data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pos.db"

    @property
    def backup_dir(self) -> Path:
        return self.backup.dir or (self.data_dir / "backups")

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"


class ConfigError(Exception):
    pass


def load_config() -> Config:
    cdir, ddir = config_dir(), data_dir()
    path = cdir / "config.toml"
    raw: dict = {}
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)

    tz = raw.get("timezone", DEFAULT_TIMEZONE)
    try:
        ZoneInfo(tz)
    except Exception as exc:
        raise ConfigError(f"Zona horaria inválida en {path}: {tz!r}") from exc

    apple_raw = raw.get("apple", {})
    if "password" in apple_raw or "app_password" in apple_raw:
        raise ConfigError(
            f"{path} contiene una contraseña. Los secretos van en secrets.toml "
            "(usa `pos secrets set icloud_app_password`)."
        )
    mailbox = apple_raw.get("mailbox_dir")
    apple = AppleConfig(
        apple_id=apple_raw.get("apple_id"),
        reminders_list=apple_raw.get("reminders_list", AppleConfig.reminders_list),
        calendar_name=apple_raw.get("calendar_name", AppleConfig.calendar_name),
        caldav_url=apple_raw.get("caldav_url", AppleConfig.caldav_url),
        mailbox_dir=Path(mailbox).expanduser() if mailbox else None,
    )
    b = raw.get("backup", {})
    try:
        backup = BackupConfig(
            dir=Path(b["dir"]).expanduser() if b.get("dir") else None,
            auto_daily=bool(b.get("auto_daily", True)),
            keep_daily=int(b.get("keep_daily", 14)),
            keep_weekly=int(b.get("keep_weekly", 8)),
            keep_monthly=int(b.get("keep_monthly", 12)),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Sección [backup] inválida en {path}: {exc}") from exc
    if min(backup.keep_daily, backup.keep_weekly, backup.keep_monthly) < 0 or backup.keep_daily < 1:
        raise ConfigError("[backup] keep_daily debe ser >= 1 y el resto >= 0")
    return Config(timezone=tz, apple=apple, backup=backup, config_dir=cdir, data_dir=ddir)


CONFIG_TEMPLATE = """\
# Personal OS — configuración local (NO va en git, NO contiene secretos).
timezone = "Europe/Madrid"

[apple]
# Tu Apple ID (email). La contraseña específica de app va en secrets.toml.
# apple_id = "tu@icloud.com"
reminders_list = "Personal OS"
calendar_name = "Personal OS"
# Carpeta del buzón dentro de iCloud Drive (carpeta de Scriptable), vista desde WSL.
# mailbox_dir = "/mnt/c/Users/<usuario>/iCloudDrive/iCloud~dk~simonbs~Scriptable/personal-os"

[backup]
# Por defecto en <data_dir>/backups. Mejor fuera de este disco (p. ej. OneDrive).
# dir = "/mnt/c/Users/<usuario>/OneDrive/personal-os-backups"
auto_daily = true      # backup automático en `pos sync` si el último tiene más de 24 h
keep_daily = 14
keep_weekly = 8
keep_monthly = 12
"""


def write_config_template() -> Path:
    path = config_dir() / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return path
