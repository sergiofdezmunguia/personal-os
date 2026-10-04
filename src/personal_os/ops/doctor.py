"""`pos doctor`: diagnóstico del sistema. Solo lee; nunca modifica nada.

Cada comprobación es independiente y devuelve ok | warn | fail | skip, con una pista de
cómo arreglarlo. Si una comprobación revienta, se informa como fallo y se sigue con el resto.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from personal_os import bootstrap
from personal_os.core import backup as bk
from personal_os.core import secrets as secrets_mod
from personal_os.core.clock import Clock, SystemClock, from_iso
from personal_os.core.config import Config, ConfigError, load_config
from personal_os.core.db import Database

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
STALE_MAILBOX = timedelta(hours=24)
STALE_SNAPSHOT = timedelta(hours=48)
STALE_BACKUP = timedelta(hours=48)


@dataclass(frozen=True)
class Check:
    area: str
    status: str
    message: str
    hint: str = ""


def repo_bridge_version() -> str | None:
    return _js_version(bootstrap.BRIDGE_SOURCE)


def _js_version(path: Path) -> str | None:
    try:
        m = re.search(r'const BRIDGE_VERSION = "([^"]+)"', path.read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(1) if m else None


def _age(clock: Clock, iso: str) -> timedelta:
    return clock.now() - from_iso(iso)


def _fmt_age(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.1f} días"


# ------------------------------------------------------------------ comprobaciones


def check_config(cfg: Config, clock: Clock) -> list[Check]:
    out = []
    if cfg.config_file.exists():
        out.append(Check("config", OK, f"{cfg.config_file} (zona {cfg.timezone})"))
    else:
        out.append(
            Check(
                "config", WARN, "No hay config.toml: se usan valores por defecto", "uv run pos init"
            )
        )
    return out


def check_secrets(cfg: Config, clock: Clock) -> list[Check]:
    status = secrets_mod.secret_status().get("icloud_app_password", "missing")
    if status.startswith("error"):
        return [Check("secretos", FAIL, status, f"chmod 600 {secrets_mod.secrets_path()}")]
    if status == "missing":
        return [
            Check(
                "secretos",
                WARN,
                "Sin contraseña de app de iCloud: Calendario desactivado",
                "uv run pos secrets set icloud_app_password",
            )
        ]
    return [Check("secretos", OK, f"icloud_app_password configurada ({status}, valor no mostrado)")]


def check_database(cfg: Config, clock: Clock) -> list[Check]:
    if not cfg.db_path.exists():
        return [Check("base de datos", FAIL, f"No existe {cfg.db_path}", "uv run pos init")]
    out = []
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    try:
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    size = cfg.db_path.stat().st_size / 1024
    if quick == "ok":
        out.append(
            Check("base de datos", OK, f"{cfg.db_path} ({size:.0f} KiB, {mode}, quick_check ok)")
        )
    else:
        out.append(
            Check(
                "base de datos",
                FAIL,
                f"quick_check: {quick}",
                "uv run pos backup restore latest --yes",
            )
        )
    db = Database(cfg.db_path)
    try:
        pending = db.pending_migrations(bootstrap.MIGRATIONS)
    finally:
        db.close()
    if pending:
        out.append(
            Check("migraciones", WARN, f"Pendientes: {', '.join(pending)}", "uv run pos init")
        )
    else:
        out.append(Check("migraciones", OK, "Esquema al día"))
    return out


def check_backups(cfg: Config, clock: Clock) -> list[Check]:
    out = []
    backups = [b for b in bk.list_backups(cfg.backup_dir) if b.label is None]
    if not backups:
        return [
            Check(
                "backups", WARN, f"No hay backups en {cfg.backup_dir}", "uv run pos backup create"
            )
        ]
    last = backups[-1]
    age = clock.now() - last.created_at
    status = WARN if age > STALE_BACKUP else OK
    out.append(
        Check(
            "backups",
            status,
            f"{len(backups)} backups; el último hace {_fmt_age(age)} ({last.path.name})",
            "uv run pos backup create" if status == WARN else "",
        )
    )
    check = bk.verify_backup(last)
    if check.ok:
        out.append(Check("backups", OK, "El último backup se verifica (sha256, integridad, filas)"))
    else:
        out.append(
            Check(
                "backups",
                FAIL,
                f"El último backup NO se verifica: {'; '.join(check.problems)}",
                "uv run pos backup create",
            )
        )
    try:
        same_place = cfg.backup_dir.resolve().is_relative_to(cfg.data_dir.resolve())
    except OSError:
        same_place = False
    if same_place:
        out.append(
            Check(
                "backups",
                WARN,
                "Los backups están en el mismo disco que la base de datos",
                "Configura [backup].dir fuera de este disco (p. ej. OneDrive). Ver docs/runbooks/backup-restore.md",
            )
        )
    if not cfg.backup.auto_daily:
        out.append(
            Check("backups", WARN, "Backup automático desactivado ([backup].auto_daily = false)")
        )
    return out


def check_mailbox(cfg: Config, clock: Clock) -> list[Check]:
    if cfg.apple.mailbox_dir is None:
        return [Check("buzón", FAIL, "Falta [apple].mailbox_dir", "Ver docs/setup/iphone.md")]
    root = cfg.apple.mailbox_dir
    if not root.parent.exists():
        return [
            Check(
                "buzón",
                FAIL,
                f"No existe {root.parent}",
                "¿iCloud para Windows abierto y carpeta Scriptable 'Mantener siempre en este dispositivo'?",
            )
        ]
    gateway = bootstrap.reminders_gateway(cfg)
    out = [Check("buzón", OK, f"{root} accesible")]
    batches = sorted((root / "outbox").glob("batch-*.json"))
    if batches:
        oldest = min(p.stat().st_mtime for p in batches)
        age = clock.now().timestamp() - oldest
        if age > STALE_MAILBOX.total_seconds():
            out.append(
                Check(
                    "buzón",
                    WARN,
                    f"{len(batches)} lote(s) sin procesar; el más antiguo hace {_fmt_age(timedelta(seconds=age))}",
                    "El iPhone no está ejecutando el bridge: revisa las automatizaciones de Atajos",
                )
            )
        else:
            out.append(Check("buzón", OK, f"{len(batches)} lote(s) esperando al iPhone (reciente)"))
    inbox = gateway.collect()
    if inbox.invalid_files:
        out.append(Check("buzón", WARN, f"Ficheros inválidos: {', '.join(inbox.invalid_files)}"))
    return out


def check_bridge(cfg: Config, clock: Clock) -> list[Check]:
    out = []
    expected = repo_bridge_version()
    if cfg.apple.mailbox_dir is not None:
        installed_path = cfg.apple.mailbox_dir.parent / bootstrap.BRIDGE_SCRIPT_NAME
        installed = _js_version(installed_path)
        if installed is None:
            out.append(
                Check(
                    "bridge",
                    FAIL,
                    f"No está instalado en {installed_path.parent}",
                    "uv run pos bridge install",
                )
            )
        elif installed != expected:
            out.append(
                Check(
                    "bridge",
                    WARN,
                    f"Instalada v{installed}, repo v{expected}",
                    "uv run pos bridge install",
                )
            )
        else:
            out.append(Check("bridge", OK, f"Instalado v{installed}"))

    if not cfg.db_path.exists():
        return out
    db = Database(cfg.db_path)
    try:
        cur = {
            r["key"]: r["value"]
            for r in db.all(
                "SELECT key, value FROM sync_cursors WHERE provider = 'apple_reminders'"
            )
        }
    except sqlite3.OperationalError:
        cur = {}
    finally:
        db.close()
    taken = cur.get("snapshot_taken_at")
    if not taken:
        out.append(
            Check(
                "bridge",
                WARN,
                "Aún no se ha recibido ningún snapshot del iPhone",
                "Ejecuta el bridge en el iPhone y luego `pos sync`",
            )
        )
        return out
    age = _age(clock, taken)
    out.append(
        Check(
            "bridge",
            WARN if age > STALE_SNAPSHOT else OK,
            f"Último snapshot del iPhone procesado hace {_fmt_age(age)}",
            "Revisa las automatizaciones de Atajos y ejecuta `pos sync`"
            if age > STALE_SNAPSHOT
            else "",
        )
    )
    device_version = cur.get("bridge_version")
    if device_version and expected and device_version != expected:
        out.append(
            Check(
                "bridge",
                WARN,
                f"El iPhone ejecutó v{device_version} (repo v{expected})",
                "Espera a que iCloud sincronice el script o ábrelo una vez en Scriptable",
            )
        )
    if cur.get("list_found") == "0":
        out.append(
            Check(
                "bridge",
                FAIL,
                f"El iPhone no encuentra la lista '{cfg.apple.reminders_list}'",
                "Créala en Recordatorios (iCloud)",
            )
        )
    tz = cur.get("device_timezone")
    if tz and tz != cfg.timezone:
        out.append(
            Check(
                "zona horaria",
                FAIL,
                f"iPhone en {tz}, Personal OS en {cfg.timezone}",
                f'Pon timezone = "{tz}" en {cfg.config_file}',
            )
        )
    elif tz:
        out.append(Check("zona horaria", OK, f"iPhone y Personal OS en {tz}"))
    return out


def check_caldav(cfg: Config, clock: Clock, *, offline: bool) -> list[Check]:
    if not bootstrap.calendar_configured(cfg):
        return [Check("caldav", SKIP, "Calendario no configurado (apple_id + contraseña de app)")]
    if offline:
        return [Check("caldav", SKIP, "Omitido (--offline)")]
    gateway = bootstrap.calendar_gateway(cfg)
    try:
        url = gateway.calendar_url
        n = len(gateway.list_etags())
    except Exception as exc:
        return [
            Check(
                "caldav",
                FAIL,
                str(exc),
                "Revisa apple_id, la contraseña de app y que exista el calendario",
            )
        ]
    finally:
        gateway.close()
    return [
        Check(
            "caldav", OK, f"Calendario '{cfg.apple.calendar_name}' accesible ({n} eventos) — {url}"
        )
    ]


def check_sync_health(cfg: Config, clock: Clock) -> list[Check]:
    if not cfg.db_path.exists():
        return []
    db = Database(cfg.db_path)
    try:
        last = db.all(
            "SELECT provider, status, started_at, error FROM sync_runs r WHERE started_at ="
            " (SELECT MAX(started_at) FROM sync_runs WHERE provider = r.provider) ORDER BY provider"
        )
        problems = db.one(
            "SELECT COUNT(*) AS n FROM external_links WHERE sync_state IN ('error','conflict')"
        )["n"]
    except sqlite3.OperationalError:
        return []
    finally:
        db.close()
    out = []
    if not last:
        out.append(Check("sync", WARN, "Nunca se ha sincronizado", "uv run pos sync"))
    for r in last:
        age = _fmt_age(_age(clock, r["started_at"]))
        if r["status"] == "ok":
            out.append(Check("sync", OK, f"{r['provider']}: última sync correcta hace {age}"))
        else:
            out.append(
                Check(
                    "sync",
                    FAIL if r["status"] == "error" else WARN,
                    f"{r['provider']}: {r['status']} hace {age}: {r['error'] or ''}",
                    "uv run pos sync status",
                )
            )
    if problems:
        out.append(
            Check(
                "sync",
                WARN,
                f"{problems} enlace(s) con error o conflicto",
                "uv run pos sync status",
            )
        )
    return out


# ------------------------------------------------------------------ ejecución


def run_doctor(*, offline: bool = False, clock: Clock | None = None) -> list[Check]:
    clock = clock or SystemClock()
    try:
        cfg = load_config()
    except ConfigError as exc:
        return [Check("config", FAIL, str(exc), "Corrige ~/.config/personal-os/config.toml")]

    checks: list[tuple[str, Callable[[], list[Check]]]] = [
        ("config", lambda: check_config(cfg, clock)),
        ("secretos", lambda: check_secrets(cfg, clock)),
        ("base de datos", lambda: check_database(cfg, clock)),
        ("backups", lambda: check_backups(cfg, clock)),
        ("buzón", lambda: check_mailbox(cfg, clock)),
        ("bridge", lambda: check_bridge(cfg, clock)),
        ("caldav", lambda: check_caldav(cfg, clock, offline=offline)),
        ("sync", lambda: check_sync_health(cfg, clock)),
    ]
    results: list[Check] = []
    for area, fn in checks:
        try:
            results.extend(fn())
        except Exception as exc:  # una comprobación rota no tumba el diagnóstico
            results.append(Check(area, FAIL, f"Error inesperado: {type(exc).__name__}: {exc}"))
    return results
