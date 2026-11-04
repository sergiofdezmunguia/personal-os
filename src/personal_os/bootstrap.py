"""Composition root: el único sitio que conoce todas las piezas y las conecta.

Lo usan las interfaces (CLI, MCP). Ninguna otra capa lo importa.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from personal_os.core.clock import Clock, SystemClock
from personal_os.core.config import Config, ConfigError, load_config
from personal_os.core.db import Database

# (namespace, paquete que contiene migrations/). El orden importa por las FKs.
MIGRATIONS = [
    ("core", "personal_os.core"),
    ("tasks", "personal_os.tasks"),
    ("calendar", "personal_os.calendar"),
    ("finance", "personal_os.finance"),
    ("sync", "personal_os.sync"),
]

BRIDGE_SOURCE = (
    Path(__file__).resolve().parents[2] / "bridge" / "scriptable" / "personal-os-sync.js"
)
BRIDGE_SCRIPT_NAME = "Personal OS Sync.js"
PANEL_VIEWER_SOURCE = BRIDGE_SOURCE.with_name("personal-os-panel.js")
PANEL_VIEWER_NAME = "Personal OS Panel.js"  # abre el panel en el iPhone (visor con JavaScript)
WIDGET_SOURCE = BRIDGE_SOURCE.with_name("personal-os-widget.js")
WIDGET_NAME = "Personal OS Widget.js"  # widget de pantalla de inicio (dinero + citas + tareas)
APUNTAR_SOURCE = BRIDGE_SOURCE.with_name("apuntar.js")
AVISOS_SOURCE = BRIDGE_SOURCE.with_name("avisos.js")
AVISOS_NAME = "Personal OS Avisos.js"  # avisos del PC como notificaciones (lo llama el sync)
APUNTAR_NAME = "Apuntar.js"  # frase en español → evento del Calendario del iPhone (Siri/Atajos)


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


def finance_service(application: App):
    from personal_os.core.events import EventLog
    from personal_os.finance.service import FinanceService

    return FinanceService(
        application.db, application.clock, EventLog(application.db, application.clock)
    )


def cash_yields(cfg: Config) -> tuple:
    from personal_os.finance.close import CashYield

    return tuple(
        CashYield(y.institution, y.interest_rate, y.saveback_rate, y.saveback_cap_cents)
        for y in cfg.finance.cash_yield
    )


def finance_month_close(application: App, month: str):
    from personal_os.finance.close import month_close

    return month_close(finance_service(application), month, yields=cash_yields(application.config))


def finance_inbox(application: App):
    """Importa la bandeja de extractos. None si no está configurada."""
    from personal_os.core.events import ChangeContext
    from personal_os.finance.inbox import process_inbox

    inbox = application.config.finance.inbox_dir
    if inbox is None:
        return None
    if not inbox.is_dir():
        raise ConfigError(f"[finance].inbox_dir no existe o no es accesible: {inbox}")
    today = application.clock.now().astimezone(ZoneInfo(application.config.timezone)).date()
    return process_inbox(
        finance_service(application),
        ChangeContext.system(),
        inbox,
        today=today.isoformat(),
        now=application.clock.now().timestamp(),
    )


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
    """Backup diario si toca, sincroniza Recordatorios y (si está configurado) Calendario, e
    importa la bandeja de extractos (si está configurada)."""
    if only not in (None, "reminders", "calendar", "finance"):
        raise ValueError("only debe ser 'reminders', 'calendar', 'finance' o None")
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
        if only in (None, "finance"):
            report = _finance_inbox_report(application)
            if report is not None:
                outcome.reports.append(report)
            report = bank_sync(application)
            if report is not None:
                outcome.reports.append(report)
            report = applepay_enrich(application)
            if report is not None:
                outcome.reports.append(report)
            report = update_prices(application)
            if report is not None:
                outcome.reports.append(report)
    return outcome


PRICES_MIN_HOURS = 6  # como mucho una descarga de cotizaciones cada 6 h


def update_prices(application: App, *, force: bool = False, client=None):
    """Último cierre de cada ISIN en cartera (ADR 0012). None si no toca o no hay cartera."""
    from datetime import datetime, timedelta

    from personal_os.adapters.quotes import QuoteError, QuotesClient
    from personal_os.core.ids import new_id
    from personal_os.finance import investments
    from personal_os.sync.store import SyncReport

    svc = finance_service(application)
    isins = investments.open_isins(svc)
    if not isins:
        return None
    now = application.clock.now()
    last = investments.last_price_fetch(svc)
    if (
        not force
        and last
        and datetime.fromisoformat(last.replace("Z", "+00:00"))
        > now - timedelta(hours=PRICES_MIN_HOURS)
    ):
        return None
    client = client or QuotesClient()
    report = SyncReport(run_id=new_id("correlation"), provider="prices")
    rows = []
    for isin in isins:
        try:
            rows += [(q.isin, q.day, q.price, q.currency, q.source) for q in client.quotes(isin)]
        except QuoteError as exc:
            report.errors.append(str(exc))
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    report.stats["isins"] = len(isins)
    report.stats["prices_new"] = investments.store_prices(svc, rows, fetched_at=stamp)
    return report


def applepay_enrich(application: App):
    """Comercio de Apple Pay → movimientos sin concepto. None si no hay registro de pagos."""
    from personal_os.core.events import ChangeContext
    from personal_os.core.ids import new_id
    from personal_os.finance.importers import applepay
    from personal_os.sync.store import SyncReport

    inbox = application.config.finance.inbox_dir
    path = inbox / "applepay" / applepay.FILE_NAME if inbox else None
    if path is None or not path.is_file():
        return None
    report = SyncReport(run_id=new_id("correlation"), provider="applepay")
    try:
        payments, skipped = applepay.parse(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        report.errors.append(f"No se pudo leer {path.name}: {exc}")
        return report
    report.stats["payments"] = len(payments)
    report.stats["lines_ignored"] = skipped
    report.stats["transactions_enriched"] = finance_service(application).enrich_from_card_payments(
        ChangeContext.system(),
        payments,
        placeholders=[fallback for _, _, fallback in BANKS.values()],
        tz=ZoneInfo(application.config.timezone),
    )
    return report


# Open banking (ADR 0010). alias de la sesión → (entidad, tipo de cuenta, concepto si falta).
BANKS = {
    "santander": ("santander", "bank", "Movimiento sin concepto"),
    "traderepublic": ("trade_republic", "broker", "Trade Republic · sin concepto"),
}
BANK_MIN_HOURS = 5  # PSD2 limita las consultas desatendidas (~4 al día por cuenta)
BANK_DAYS = 30  # ventana de movimientos que se piden en cada consulta
BANK_RENEW_DAYS = 14  # avisa de renovar la autorización con esta antelación


def bank_sync(application: App, *, days: int = BANK_DAYS, force: bool = False, client=None):
    """Trae saldo y movimientos de los bancos autorizados. None si no está configurado.
    Nunca interrumpe la sync: los fallos van como errores del informe."""
    from datetime import datetime, timedelta

    from personal_os.adapters.enablebanking import BankingError, EnableBankingClient
    from personal_os.core.clock import to_iso
    from personal_os.core.events import ChangeContext
    from personal_os.core.ids import new_id
    from personal_os.finance.importers import StatementError
    from personal_os.finance.importers import enablebanking as eb
    from personal_os.finance.models import FinanceError
    from personal_os.sync.store import SyncReport

    cred_dir = application.config.config_dir / "enablebanking"
    if client is None:
        if not cred_dir.is_dir():
            return None
        try:
            client = EnableBankingClient(cred_dir)
        except BankingError as exc:
            report = SyncReport(run_id=new_id("correlation"), provider="bank")
            report.errors.append(str(exc))
            return report
    report = SyncReport(run_id=new_id("correlation"), provider="bank")
    now = application.clock.now()
    svc = finance_service(application)
    last = application.db.one(
        "SELECT MAX(imported_at) AS t FROM fin_imports WHERE source = ?", (eb.SOURCE,)
    )
    if not force and last and last["t"]:
        since = now - datetime.fromisoformat(last["t"].replace("Z", "+00:00"))
        if since < timedelta(hours=BANK_MIN_HOURS):
            report.stats["skipped_recent"] = 1
            return report
    today = now.astimezone(ZoneInfo(application.config.timezone)).date()
    date_from = (today - timedelta(days=days)).isoformat()
    for session in client.sessions():
        if session.alias not in BANKS:
            report.warnings.append(f"{session.alias}: banco desconocido, se ignora")
            continue
        institution, kind, fallback = BANKS[session.alias]
        if session.valid_until and session.valid_until < to_iso(now):
            report.errors.append(
                f"{session.alias}: la autorización caducó el {session.valid_until[:10]};"
                " renuévala (docs/finance.md, «Open banking»)"
            )
            continue
        if session.valid_until and session.valid_until < to_iso(
            now + timedelta(days=BANK_RENEW_DAYS)
        ):
            report.warnings.append(
                f"{session.alias}: la autorización caduca el {session.valid_until[:10]};"
                " renuévala (docs/finance.md, «Open banking»)"
            )
        accounts = [a for a in svc.list_accounts() if a.institution == institution]
        if len(accounts) != 1:
            report.errors.append(
                f"{session.alias}: necesito exactamente una cuenta de {institution} ya"
                f" importada (hay {len(accounts)}); importa primero un extracto"
            )
            continue
        for uid in session.accounts:
            try:
                balances = client.balances(uid)
                txs = client.transactions(uid, date_from=date_from)
                statement = eb.parse(
                    institution=institution,
                    account_name=accounts[0].name,
                    account_kind=kind,
                    balances=balances,
                    transactions=txs,
                    fallback_description=fallback,
                )
                payload = json.dumps([balances, txs], sort_keys=True).encode()
                result = svc.import_statement(
                    ChangeContext.system(),
                    statement,
                    file_name=f"open banking · {session.alias} · {today.isoformat()}",
                    file_sha256=hashlib.sha256(payload).hexdigest(),
                    account_id=accounts[0].id,
                )
            except (BankingError, StatementError, FinanceError) as exc:
                report.errors.append(f"{session.alias}: {exc}")
                continue
            report.inc("accounts")
            report.inc("transactions_new", result.rows_new)
            report.inc("transactions_linked", result.rows_linked)
            report.warnings.extend(f"{session.alias}: {w}" for w in result.warnings
                                   if not result.already_imported)  # fmt: skip
    return report


def _finance_inbox_report(application: App):
    """La bandeja de extractos como un paso más de la sync. Nunca la interrumpe: avisa."""
    from personal_os.core.ids import new_id
    from personal_os.sync.store import SyncReport

    report = SyncReport(run_id=new_id("correlation"), provider="finance_inbox")
    try:
        inbox = finance_inbox(application)
    except ConfigError as exc:
        report.errors.append(str(exc))
        return report
    if inbox is None:
        return None
    report.stats["files_imported"] = len(inbox.imported)
    report.stats["transactions_new"] = inbox.rows_new
    for item in inbox.items:
        if item.result and item.result.already_imported and not item.error:
            report.inc("files_already_imported")
        for w in item.result.warnings if item.result and not item.result.already_imported else []:
            report.warnings.append(f"{item.file_name}: {w}")
    for item in inbox.failed:
        report.warnings.append(f"{item.file_name}: {item.error}")
    return report
