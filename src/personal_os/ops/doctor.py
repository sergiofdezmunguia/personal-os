"""`pos doctor`: diagnóstico del sistema. Solo lee; nunca modifica nada.

Cada comprobación es independiente y devuelve ok | warn | fail | skip, con una pista de
cómo arreglarlo. Si una comprobación revienta, se informa como fallo y se sigue con el resto.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from personal_os import bootstrap
from personal_os.core import backup as bk
from personal_os.core import offsite
from personal_os.core import secrets as secrets_mod
from personal_os.core.clock import Clock, SystemClock, from_iso, to_iso
from personal_os.core.config import Config, ConfigError, load_config
from personal_os.core.db import Database

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
STALE_MAILBOX = timedelta(hours=24)
STALE_SNAPSHOT = timedelta(hours=48)
STALE_BACKUP = timedelta(hours=48)
STALE_FINANCE_IMPORT = timedelta(days=35)
CLOSE_GRACE_DAYS = 7  # días del mes siguiente para subir extractos antes de avisar
STALE_VALUATION_DAYS = 40  # el PDF del patrimonio neto se sube a fin de mes
STALE_PRICE_WEEKDAYS = 3  # sesiones perdidas: con 2 no avisan ni Semana Santa ni Navidad
UNSEEN_CHARGE_GRACE_DAYS = 7  # un cargo probable sin CSV pasa a aviso al cumplir estos días
STALE_TRADES_DAYS = 40  # el CSV de operaciones tiene la misma cadencia mensual que el PDF
STALE_BANK_DATA_DAYS = 7  # días sin ningún dato nuevo del banco
MAX_LISTED_POSITIONS = 3
PRICES_HINT = (
    "uv run pos finance prices --force. Si persiste, las fuentes públicas pueden no cubrir "
    "ese activo (justETF; Yahoo solo en EUR)"
)
TRADES_HINT = (
    "Exporta el CSV completo de Trade Republic a la bandeja y ejecuta uv run pos finance inbox. "
    "Hasta entonces, cartera, panel e Ideas no cuentan esas compras"
)


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
    if same_place and not cfg.backup.offsite_enabled:
        out.append(
            Check(
                "backups",
                WARN,
                "Los backups están en el mismo disco que la base de datos",
                "Configura la copia externa cifrada: `uv run pos backup keygen`. Ver docs/runbooks/backup-restore.md",
            )
        )
    if not cfg.backup.auto_daily:
        out.append(
            Check("backups", WARN, "Backup automático desactivado ([backup].auto_daily = false)")
        )
    return out


def check_offsite(cfg: Config, clock: Clock) -> list[Check]:
    if not cfg.backup.offsite_enabled:
        return [Check("offsite", SKIP, "Copia externa cifrada no configurada")]
    out: list[Check] = []
    dest = cfg.backup.offsite_dir
    try:
        recipient = offsite.parse_recipient(cfg.backup.offsite_recipient)
    except offsite.OffsiteError as exc:
        return [Check("offsite", FAIL, str(exc), "Revisa [backup].offsite_recipient")]
    if not dest.is_dir():
        return [
            Check(
                "offsite",
                FAIL,
                f"Destino externo no accesible: {dest}",
                "¿Está iCloud para Windows abierto y la carpeta creada? (mkdir la primera vez)",
            )
        ]
    if dest.resolve() != dest.absolute():
        out.append(
            Check(
                "offsite",
                WARN,
                f"La ruta pasa por un enlace simbólico ({dest} → {dest.resolve()})",
                "Usa la ruta real: iCloud/OneDrive rechazan los borrados por la ruta del enlace "
                "(ver docs/investigations/2026-10-04-icloud-drive-backups.md)",
            )
        )
    identity = None
    try:
        identity = offsite.load_identity(cfg.offsite_identity_path)
    except offsite.OffsiteError as exc:
        missing = not cfg.offsite_identity_path.exists()
        out.append(
            Check(
                "offsite",
                WARN if missing else FAIL,
                str(exc),
                "Sin la clave privada no se puede verificar ni recuperar. Restáurala desde tu "
                "gestor de contraseñas"
                if missing
                else "",
            )
        )
    if identity is not None and not offsite.matches(identity, recipient):
        return [
            *out,
            Check(
                "offsite",
                FAIL,
                "La clave privada no corresponde a offsite_recipient",
                "Los backups nuevos no se podrían descifrar con tu clave. Revisa la config",
            ),
        ]
    remote = [b for b in bk.list_backups(dest) if b.label is None and b.encrypted]
    if not remote:
        return [
            *out,
            Check("offsite", WARN, f"No hay copias en {dest}", "uv run pos backup offsite"),
        ]
    last = remote[-1]
    age = clock.now() - last.created_at
    status = WARN if age > STALE_BACKUP else OK
    out.append(
        Check(
            "offsite",
            status,
            f"{len(remote)} copias cifradas; la última hace {_fmt_age(age)} ({last.path.name})",
            "uv run pos backup offsite" if status == WARN else "",
        )
    )
    if identity is not None:
        check = bk.verify_backup(last, identity)
        if check.ok:
            out.append(Check("offsite", OK, "La última copia externa se descifra y verifica"))
        else:
            out.append(
                Check(
                    "offsite",
                    FAIL,
                    f"La última copia externa NO se verifica: {'; '.join(check.problems)}",
                    "uv run pos backup create",
                )
            )
    return out


def check_finance(cfg: Config, clock: Clock) -> list[Check]:
    if not cfg.db_path.exists():
        return [Check("finanzas", SKIP, "Sin base de datos")]
    db = Database(cfg.db_path)
    try:
        if db.one("SELECT 1 FROM sqlite_master WHERE name = 'fin_imports'") is None:
            return [Check("finanzas", SKIP, "Módulo sin migrar", "uv run pos init")]
        row = db.one("SELECT MAX(imported_at) AS last, COUNT(*) AS n FROM fin_imports")
        pending = db.one("SELECT COUNT(*) AS n FROM fin_transactions WHERE category IS NULL")["n"]
        proposed = db.one("SELECT COUNT(*) AS n FROM fin_rules WHERE status = 'proposed'")["n"]
        today = clock.now().astimezone(ZoneInfo(cfg.timezone)).date()
        prev_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        prev_pending = db.one(
            "SELECT COUNT(*) AS n FROM fin_transactions"
            " WHERE category IS NULL AND booking_date LIKE ?",
            (prev_month + "-%",),
        )["n"]
        investments = _check_investments(db, today)
    finally:
        db.close()
    inbox = _check_finance_inbox(cfg)
    if not row["n"]:
        return [Check("finanzas", SKIP, "Sin extractos importados"), *inbox]
    age = _age(clock, row["last"])
    out = [
        Check(
            "finanzas",
            WARN if age > STALE_FINANCE_IMPORT else OK,
            f"Última importación hace {_fmt_age(age)}; {pending} movimientos sin categoría",
            "Descarga el extracto y `uv run pos finance import <fichero>`"
            if age > STALE_FINANCE_IMPORT
            else "",
        )
    ]
    if proposed:
        out.append(
            Check(
                "finanzas",
                WARN,
                f"{proposed} reglas propuestas por Claude esperan revisión",
                "uv run pos finance rule list --status proposed",
            )
        )
    if prev_pending and today.day > CLOSE_GRACE_DAYS:
        out.append(
            Check(
                "finanzas",
                WARN,
                f"{prev_month} sin cerrar: {prev_pending} movimientos sin categoría",
                f"Pide a Claude el cierre mensual (/monthly-close) o `uv run pos finance close {prev_month}`",
            )
        )
    return out + investments + inbox


def _check_investments(db: Database, today) -> list[Check]:
    """Valor de la cartera: depende de que el usuario suba el PDF del patrimonio neto."""
    if db.one("SELECT 1 FROM sqlite_master WHERE name = 'fin_trades'") is None:
        return []
    if db.one("SELECT 1 FROM fin_trades LIMIT 1") is None:
        return []
    fix = "Sube a la bandeja el PDF «Extracto del patrimonio neto» de Trade Republic"
    last = db.one("SELECT MAX(as_of) AS d FROM fin_valuations")["d"]
    if last is None:
        return [Check("inversiones", WARN, "Hay operaciones pero ninguna valoración", fix)]
    days = (today - date.fromisoformat(last)).days
    message = f"Última valoración del {last} (hace {days} {'día' if days == 1 else 'días'})"
    if days > STALE_VALUATION_DAYS:
        return [Check("inversiones", WARN, message, fix)]
    return [Check("inversiones", OK, message)]


def _check_finance_inbox(cfg: Config) -> list[Check]:
    from personal_os.finance.inbox import pending_files

    inbox = cfg.finance.inbox_dir
    if inbox is None:
        return [
            Check(
                "extractos",
                WARN,
                "Bandeja de extractos sin configurar",
                f"Añade [finance].inbox_dir en {cfg.config_file} (ver docs/finance.md)",
            )
        ]
    if not inbox.is_dir():
        return [
            Check("extractos", FAIL, f"Bandeja no accesible: {inbox}", "¿iCloud Drive arrancado?")
        ]
    files = pending_files(inbox)
    if files:
        return [
            Check(
                "extractos",
                WARN,
                f"{len(files)} ficheros en la bandeja sin importar",
                "uv run pos finance inbox",
            )
        ]
    return [Check("extractos", OK, f"Bandeja vacía ({inbox})")]


def _today(cfg: Config, clock: Clock) -> date:
    return clock.now().astimezone(ZoneInfo(cfg.timezone)).date()


def _has_table(db: Database, name: str) -> bool:
    return db.one("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)) is not None


@contextmanager
def _finance_service(cfg: Config, clock: Clock, table: str) -> Iterator[object | None]:
    """Servicio de finanzas sin migrar nada. None si no hay base de datos o falta `table`."""
    if not cfg.db_path.exists():
        yield None
        return
    db = Database(cfg.db_path)
    try:
        yield (
            bootstrap.finance_service(bootstrap.App(config=cfg, db=db, clock=clock))
            if _has_table(db, table)
            else None
        )
    finally:
        db.close()


def _count(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _doubt(n: int, singular: str, plural: str) -> str:
    """Duda sobre `n` indicios; `singular` y `plural` son el pronombre que los retoma."""
    if n == 1:
        return f"Podría no ser una compra; la cartera no {singular} incluye"
    return f"Podrían no ser compras; la cartera no {plural} incluye"


def _listed(items: list) -> str:
    shown = ", ".join(f"{i.name} ({i.key})" for i in items[:MAX_LISTED_POSITIONS])
    extra = len(items) - MAX_LISTED_POSITIONS
    return f"{shown} y {extra} más" if extra > 0 else shown


def check_prices(cfg: Config, clock: Clock) -> list[Check]:
    """Cierres de las posiciones abiertas y del satélite. Solo sabe la antigüedad del último
    cierre guardado: si la última descarga falló, lo dice su informe, no la base de datos."""
    from personal_os.finance import investments, satellite

    today = _today(cfg, clock).isoformat()
    with _finance_service(cfg, clock, "fin_prices") as svc:
        if svc is None:
            return []
        items = investments.position_price_freshness(svc, today=today)
        sat = cfg.finance.satellite
        market = (
            satellite.market_price_freshness(svc, sat, today=today)
            if sat.enabled and _has_table(svc.db, "fin_market_prices")
            else []
        )
        has_valuation = svc.db.one("SELECT 1 FROM fin_valuations LIMIT 1") is not None
    max_age = investments.LIVE_PRICE_MAX_AGE_DAYS
    missing = [i for i in items if i.last_close is None]
    unused = [i for i in items if i.last_close and i.age_days > max_age]
    stale = [
        i
        for i in items
        if i.last_close and i.age_days <= max_age and i.missed_weekdays >= STALE_PRICE_WEEKDAYS
    ]
    fresh = [i for i in items if i not in missing + unused + stale]
    out = []
    if fresh:
        oldest = min(i.last_close for i in fresh)
        positions = _count(len(fresh), "posición", "posiciones")
        out.append(Check("precios", OK, f"{positions} con cierre reciente (desde {oldest})"))
    if missing:
        out.append(
            Check(
                "precios",
                WARN,
                f"{_count(len(missing), 'posición abierta', 'posiciones abiertas')} sin "
                f"cotización: {_listed(missing)}",
                PRICES_HINT,
            )
        )
    if stale:
        out.append(
            Check(
                "precios",
                WARN,
                f"{_count(len(stale), 'posición', 'posiciones')} sin cierre nuevo desde hace "
                f"{STALE_PRICE_WEEKDAYS} o más sesiones (pueden ser festivos): {_listed(stale)}",
                PRICES_HINT,
            )
        )
    if unused:
        effects = (
            "el panel vuelve a la valoración del PDF"
            if has_valuation
            else "el panel pasa a mostrar la cartera a coste"
        )
        if sat.enabled:
            effects += " y el tope del satélite puede bajar"
        out.append(
            Check(
                "precios",
                WARN,
                f"{_count(len(unused), 'posición', 'posiciones')} con el último cierre de hace "
                f"más de {max_age} días: ya no se usa; {effects}: {_listed(unused)}",
                PRICES_HINT,
            )
        )
    late = [m for m in market if m.last_close is None or m.missed_weekdays >= STALE_PRICE_WEEKDAYS]
    if late:
        out.append(
            Check(
                "precios",
                WARN,
                f"Satélite: {_count(len(late), 'símbolo', 'símbolos')} sin cierre reciente "
                "(pueden ser festivos); las Ideas pueden calcularse con cierres viejos: "
                f"{_listed(late)}",
                PRICES_HINT,
            )
        )
    elif market:
        symbols = _count(len(market), "símbolo", "símbolos")
        out.append(Check("precios", OK, f"Satélite: {symbols} con cierre reciente"))
    return out


def _trade_visibility_checks(v, today: date, last_data: str | None) -> list[Check]:
    """Hasta dónde se conocen las operaciones de una cuenta e indicios de que falte algo. Decisión
    del usuario: solo los cargos probables pasan a aviso; los sin identificar, nunca."""
    area, who, until = "operaciones", v.account_label, v.known_until
    if not v.probable and not v.unknown:
        if last_data is None:
            text = "Sin datos de open banking, una compra posterior no deja ningún rastro aquí"
        else:
            text = (
                "Sin cargos pendientes de identificar en lo recibido del banco (último dato "
                f"nuevo: {last_data}). Una compra que aún no haya llegado no se puede detectar"
            )
        return [Check(area, OK, f"Operaciones de {who} importadas hasta {until}. {text}")]
    since_csv = f"desde el último CSV ({until})"
    probable = _count(v.probable, "probable compra", "probables compras")
    overdue = (
        v.oldest_charge is not None
        and (today - date.fromisoformat(v.oldest_charge)).days >= UNSEEN_CHARGE_GRACE_DAYS
    )
    if not overdue:
        n = v.probable + v.unknown
        oldest = ""
        if v.probable:
            oldest = f", la probable compra {'más antigua ' if v.probable > 1 else ''}del "
            oldest += v.oldest_charge
        return [
            Check(
                area,
                OK,
                f"{_count(n, 'cargo', 'cargos')} en {who} {since_csv}{oldest}: {probable}, "
                f"{v.unknown} sin identificar. {_doubt(n, 'lo', 'los')}",
            )
        ]
    when = f"la más antigua del {v.oldest_charge}" if v.probable > 1 else f"del {v.oldest_charge}"
    out = [
        Check(
            area,
            WARN,
            f"{probable} en {who} {since_csv}, {when}. {_doubt(v.probable, 'la', 'las')}",
            TRADES_HINT,
        )
    ]
    if v.unknown:
        cargos = _count(v.unknown, "cargo", "cargos")
        out.append(
            Check(
                area,
                OK,
                f"{cargos} sin identificar en {who} {since_csv}. {_doubt(v.unknown, 'lo', 'los')}",
            )
        )
    return out


def check_recent_trades(cfg: Config, clock: Clock) -> list[Check]:
    """Infiere con señales locales: lo que aún no ha llegado no se puede detectar."""
    from personal_os.finance import investments

    today = _today(cfg, clock)
    tz = ZoneInfo(cfg.timezone)
    placeholders = [fallback for _, _, fallback in bootstrap.BANKS.values()]
    with _finance_service(cfg, clock, "fin_trades") as svc:
        if svc is None:
            return []
        views = investments.trade_visibility(
            svc, today=today.isoformat(), placeholders=placeholders
        )
    out = []
    for v in views:
        last_data = (
            from_iso(v.last_bank_data).astimezone(tz).date().isoformat()
            if v.last_bank_data
            else None
        )
        out += _trade_visibility_checks(v, today, last_data)
        days = (today - date.fromisoformat(v.known_until)).days
        if days > STALE_TRADES_DAYS:
            out.append(
                Check(
                    "operaciones",
                    WARN,
                    f"El último CSV de {v.account_label} llega hasta {v.known_until} (hace {days} "
                    "días): las operaciones posteriores son desconocidas",
                    TRADES_HINT,
                )
            )
        if v.mismatched:
            out.append(
                Check(
                    "operaciones",
                    WARN,
                    f"La valoración del {v.valuation_as_of} declara participaciones distintas de "
                    f"las operaciones importadas en "
                    f"{_count(len(v.mismatched), 'posición', 'posiciones')} de "
                    f"{v.account_label}: podría faltar una compra o ser una bonificación en "
                    "títulos, que no se importa como operación",
                    TRADES_HINT,
                )
            )
    return out


def check_open_banking(cfg: Config, clock: Clock) -> list[Check]:
    """Vigencia de la autorización y último dato nuevo, sin red ni clave privada. Nunca muestra
    `session_id` ni uids de cuenta."""
    from personal_os.finance import investments

    sessions = bootstrap.bank_sessions(cfg)
    if sessions is None:
        return [Check("open banking", SKIP, "No configurado")]
    if not sessions:
        return [
            Check(
                "open banking",
                WARN,
                "No hay ningún banco autorizado",
                "uv run pos finance bank-auth <banco>",
            )
        ]
    now = clock.now()
    tz = ZoneInfo(cfg.timezone)
    out = []
    with _finance_service(cfg, clock, "fin_imports") as svc:
        for s in sessions:
            renew = f"uv run pos finance bank-auth {s.alias}"
            if s.alias not in bootstrap.BANKS:
                out.append(Check("open banking", WARN, f"{s.alias}: banco desconocido, se ignora"))
                continue
            if s.valid_until and s.valid_until < to_iso(now):
                # La causa ya se conoce: no se añade el aviso de datos antiguos.
                out.append(
                    Check(
                        "open banking",
                        WARN,
                        f"{s.alias}: la autorización caducó el {s.valid_until[:10]}",
                        renew,
                    )
                )
                continue
            if s.valid_until and s.valid_until < to_iso(
                now + timedelta(days=bootstrap.BANK_RENEW_DAYS)
            ):
                out.append(
                    Check(
                        "open banking",
                        WARN,
                        f"{s.alias}: la autorización caduca el {s.valid_until[:10]}",
                        renew,
                    )
                )
            else:
                state = (
                    f"autorización vigente hasta {s.valid_until[:10]}"
                    if s.valid_until
                    else "autorización sin fecha de caducidad conocida: vigencia desconocida"
                )
                out.append(Check("open banking", OK, f"{s.alias}: {state}"))
            if svc is None:
                continue
            institution = bootstrap.BANKS[s.alias][0]
            stamps = [
                investments.last_bank_data(svc, a.id)
                for a in svc.list_accounts()
                if a.institution == institution
            ]
            last = max((t for t in stamps if t), default=None)
            hint = "uv run pos sync --only finance y mira sus errores, sin --force, para respetar el cupo"
            if last is None:
                out.append(
                    Check(
                        "open banking",
                        WARN,
                        f"{s.alias}: aún no ha llegado ningún dato del banco; puede ser una "
                        "consulta fallida, y el doctor no lo distingue",
                        hint,
                    )
                )
            elif _age(clock, last) > timedelta(days=STALE_BANK_DATA_DAYS):
                out.append(
                    Check(
                        "open banking",
                        WARN,
                        f"{s.alias}: sin datos nuevos desde hace {_fmt_age(_age(clock, last))}; "
                        "puede ser falta de movimientos o una consulta fallida, y el doctor no "
                        "lo distingue",
                        hint,
                    )
                )
            else:
                day = from_iso(last).astimezone(tz).date().isoformat()
                out.append(Check("open banking", OK, f"{s.alias}: último dato nuevo del {day}"))
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
        ("offsite", lambda: check_offsite(cfg, clock)),
        ("buzón", lambda: check_mailbox(cfg, clock)),
        ("bridge", lambda: check_bridge(cfg, clock)),
        ("caldav", lambda: check_caldav(cfg, clock, offline=offline)),
        ("sync", lambda: check_sync_health(cfg, clock)),
        ("finanzas", lambda: check_finance(cfg, clock)),
        ("precios", lambda: check_prices(cfg, clock)),
        ("operaciones", lambda: check_recent_trades(cfg, clock)),
        ("open banking", lambda: check_open_banking(cfg, clock)),
    ]
    results: list[Check] = []
    for area, fn in checks:
        try:
            results.extend(fn())
        except Exception as exc:  # una comprobación rota no tumba el diagnóstico
            results.append(Check(area, FAIL, f"Error inesperado: {type(exc).__name__}: {exc}"))
    return results
