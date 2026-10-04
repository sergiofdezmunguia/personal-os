"""Backups de la base de datos SQLite: crear, verificar, listar, podar y restaurar.

- La copia usa la API de backup online de SQLite: consistente aunque haya WAL o escrituras.
- Cada backup es `pos-<UTC>[-etiqueta].db.gz` + manifiesto `.json` (sha256, integridad,
  filas por tabla, migraciones). Se verifica tras escribirse en el destino.
- Restaurar: verifica, guarda una copia `pre-restore` del estado actual, elimina WAL/SHM
  obsoletos, sustituye de forma atómica y migra hacia delante.
- Copia fuera de este disco: el mismo `.db.gz` cifrado con age (`.db.gz.age`) junto a su
  manifiesto; ver `core/offsite.py`. Listar, podar y verificar funcionan igual sobre ambos.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from personal_os.core.clock import Clock, to_iso
from personal_os.core.db import Database

FORMAT = "pos-backup/1"
_NAME_RE = re.compile(r"^pos-(\d{8}T\d{6}Z)(?:-(\d+))?(?:-([a-z][a-z-]*))?\.db\.gz(?:\.age)?$")
ENCRYPTED_SUFFIX = ".age"
PRE_RESTORE = "pre-restore"
KEEP_PRE_RESTORE = 5


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class DbStats:
    integrity: str
    tables: dict[str, int]
    migrations: list[str]


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    created_at: datetime
    label: str | None
    manifest: dict | None

    @property
    def manifest_path(self) -> Path:
        return _manifest_path(self.path)

    @property
    def encrypted(self) -> bool:
        return self.path.name.endswith(ENCRYPTED_SUFFIX)


@dataclass
class VerifyResult:
    backup: BackupInfo
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class RestoreResult:
    restored: BackupInfo
    pre_restore: BackupInfo | None
    migrations_applied: list[str]
    stats: DbStats


def _manifest_path(gz: Path) -> Path:
    return gz.with_name(gz.name.removesuffix(ENCRYPTED_SUFFIX).removesuffix(".db.gz") + ".json")


def inspect_db(path: Path) -> DbStats:
    """Integridad, filas por tabla y migraciones aplicadas (solo lectura)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        tables = {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
        migrations = []
        if "schema_migrations" in names:
            migrations = [
                f"{ns}/{name}"
                for ns, name in conn.execute(
                    "SELECT namespace, name FROM schema_migrations ORDER BY namespace, version"
                )
            ]
        return DbStats(integrity=integrity, tables=tables, migrations=migrations)
    finally:
        conn.close()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_copy(src: Path, dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)


# --------------------------------------------------------------------------- crear


def create_backup(
    db_path: Path, dest_dir: Path, clock: Clock, *, label: str | None = None
) -> BackupInfo:
    if not Path(db_path).exists():
        raise BackupError(f"No existe la base de datos {db_path}")
    if label is not None and not re.fullmatch(r"[a-z][a-z-]*", label):
        raise BackupError("La etiqueta solo admite minúsculas y guiones")
    dest_dir.mkdir(parents=True, exist_ok=True)
    now = clock.now().astimezone(UTC)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    suffix = f"-{label}" if label else ""
    name = f"pos-{stamp}{suffix}.db.gz"
    n = 1
    while (dest_dir / name).exists():  # dos backups en el mismo segundo
        name = f"pos-{stamp}-{n}{suffix}.db.gz"
        n += 1

    with tempfile.TemporaryDirectory(prefix="pos-backup-") as tmp:
        raw = Path(tmp) / "pos.db"
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        dst = sqlite3.connect(raw)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        stats = inspect_db(raw)
        if stats.integrity != "ok":
            raise BackupError(
                f"La base de datos de origen no supera integrity_check: {stats.integrity}"
            )
        gz = Path(tmp) / name
        with raw.open("rb") as fin, gzip.open(gz, "wb", compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout)
        manifest = {
            "format": FORMAT,
            "created_at": to_iso(now),
            "label": label,
            "source": str(db_path),
            "sha256_db": _sha256(raw),
            "size_db": raw.stat().st_size,
            "size_gz": gz.stat().st_size,
            "integrity": stats.integrity,
            "tables": stats.tables,
            "migrations": stats.migrations,
        }
        target = dest_dir / name
        _atomic_copy(gz, target)
        mpath = _manifest_path(target)
        tmp_manifest = Path(tmp) / mpath.name
        tmp_manifest.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        _atomic_copy(tmp_manifest, mpath)

    info = BackupInfo(
        path=target, created_at=now.replace(microsecond=0), label=label, manifest=manifest
    )
    result = verify_backup(info)
    if not result.ok:
        raise BackupError(f"El backup recién escrito no se verifica: {'; '.join(result.problems)}")
    return info


# --------------------------------------------------------------------------- leer


def list_backups(dest_dir: Path) -> list[BackupInfo]:
    """Backups del directorio, del más antiguo al más reciente."""
    if not dest_dir.exists():
        return []
    out = []
    for p in dest_dir.iterdir():
        m = _NAME_RE.match(p.name)
        if not m:
            continue
        created = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        mpath = _manifest_path(p)
        manifest = None
        if mpath.exists():
            try:
                manifest = json.loads(mpath.read_text(encoding="utf-8"))
            except ValueError:
                manifest = None
        out.append(BackupInfo(path=p, created_at=created, label=m.group(3), manifest=manifest))
    return sorted(out, key=lambda b: (b.created_at, b.path.name))


def latest_backup(dest_dir: Path, *, include_labeled: bool = False) -> BackupInfo | None:
    items = [b for b in list_backups(dest_dir) if include_labeled or b.label is None]
    return items[-1] if items else None


def resolve_backup(dest_dir: Path, ref: str) -> BackupInfo:
    """'latest', nombre de fichero, ruta o prefijo único del nombre."""
    if ref == "latest":
        found = latest_backup(dest_dir)
        if found is None:
            raise BackupError(f"No hay backups en {dest_dir}")
        return found
    candidates = list_backups(dest_dir)
    path = Path(ref)
    if path.is_absolute() and path.exists():
        candidates = list_backups(path.parent)
        matches = [b for b in candidates if b.path == path]
    else:
        matches = [b for b in candidates if b.path.name.startswith(ref)]
    if len(matches) != 1:
        raise BackupError(f"'{ref}' no identifica un único backup ({len(matches)} coincidencias)")
    return matches[0]


def _decompress(src: Path, dest: Path) -> None:
    with gzip.open(src, "rb") as fin, dest.open("wb") as fout:
        shutil.copyfileobj(fin, fout)


def verify_backup(info: BackupInfo, identity: object | None = None) -> VerifyResult:
    """Verifica un backup. Si está cifrado hace falta `identity` (pyrage x25519.Identity)."""
    result = VerifyResult(backup=info)
    if info.manifest is None:
        result.problems.append("sin manifiesto (.json) o ilegible")
        return result
    if info.manifest.get("format") != FORMAT:
        result.problems.append(f"formato desconocido {info.manifest.get('format')!r}")
        return result
    with tempfile.TemporaryDirectory(prefix="pos-verify-") as tmp:
        raw = Path(tmp) / "pos.db"
        gz = info.path
        if info.encrypted:
            if identity is None:
                result.problems.append("cifrado: hace falta la clave age para verificarlo")
                return result
            import pyrage

            gz = Path(tmp) / "backup.db.gz"
            try:
                gz.write_bytes(pyrage.decrypt(info.path.read_bytes(), [identity]))
            except (OSError, pyrage.DecryptError) as exc:
                result.problems.append(f"no se puede descifrar: {exc}")
                return result
        try:
            _decompress(gz, raw)
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            result.problems.append(f"no se puede descomprimir: {exc}")
            return result
        if _sha256(raw) != info.manifest.get("sha256_db"):
            result.problems.append("sha256 distinto del manifiesto")
            return result
        try:
            stats = inspect_db(raw)
        except sqlite3.DatabaseError as exc:
            result.problems.append(f"no es una base de datos válida: {exc}")
            return result
        if stats.integrity != "ok":
            result.problems.append(f"integrity_check: {stats.integrity}")
        if stats.tables != info.manifest.get("tables"):
            result.problems.append("el número de filas no coincide con el manifiesto")
    return result


# --------------------------------------------------------------------------- retención


def select_to_keep(
    backups: Sequence[BackupInfo], *, keep_daily: int, keep_weekly: int, keep_monthly: int
) -> set[Path]:
    """Abuelo-padre-hijo: el más reciente de cada uno de los últimos N días / semanas ISO /
    meses con backup. Los etiquetados no cuentan aquí (ver `prune`)."""
    plain = sorted(
        (b for b in backups if b.label is None), key=lambda b: b.created_at, reverse=True
    )
    keep: set[Path] = set()
    for key_fn, limit in (
        (lambda d: d.date(), keep_daily),
        (lambda d: d.isocalendar()[:2], keep_weekly),
        (lambda d: (d.year, d.month), keep_monthly),
    ):
        seen: list = []
        for b in plain:
            k = key_fn(b.created_at)
            if k in seen:
                continue
            if len(seen) >= limit:
                break
            seen.append(k)
            keep.add(b.path)
    if plain:
        keep.add(plain[0].path)  # el más reciente siempre
    return keep


def prune(
    dest_dir: Path, *, keep_daily: int, keep_weekly: int, keep_monthly: int
) -> tuple[list[Path], list[str]]:
    """Borra los backups que la política no conserva. Devuelve (borrados, avisos)."""
    backups = list_backups(dest_dir)
    keep = select_to_keep(
        backups, keep_daily=keep_daily, keep_weekly=keep_weekly, keep_monthly=keep_monthly
    )
    pre = [b for b in backups if b.label == PRE_RESTORE]
    keep |= {b.path for b in pre[-KEEP_PRE_RESTORE:]}
    keep |= {
        b.path for b in backups if b.label not in (None, PRE_RESTORE)
    }  # manuales etiquetados: nunca
    removed, warnings = [], []
    for b in backups:
        if b.path in keep:
            continue
        try:
            b.path.unlink()
            with contextlib.suppress(FileNotFoundError):
                b.manifest_path.unlink()
            removed.append(b.path)
        except OSError as exc:
            warnings.append(f"No se pudo borrar {b.path.name}: {exc}")
    return removed, warnings


def needs_auto_backup(
    dest_dir: Path, clock: Clock, *, max_age: timedelta = timedelta(hours=24)
) -> bool:
    last = latest_backup(dest_dir)
    return last is None or clock.now() - last.created_at >= max_age


# --------------------------------------------------------------------------- restaurar


def restore_into(info: BackupInfo, target: Path) -> None:
    """Escribe el contenido del backup en `target` (atómico; elimina WAL/SHM obsoletos)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if info.encrypted:
        raise BackupError("Backup cifrado: descífralo antes con `pos backup fetch`")
    tmp = target.with_name(target.name + ".restore.tmp")
    _decompress(info.path, tmp)
    with tmp.open("rb") as fh:
        os.fsync(fh.fileno())
    # Un -wal antiguo se aplicaría sobre la base restaurada y la corrompería.
    for suffix in ("-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            target.with_name(target.name + suffix).unlink()
    os.replace(tmp, target)


def restore_backup(
    info: BackupInfo,
    db_path: Path,
    dest_dir: Path,
    clock: Clock,
    migrations: Sequence[tuple[str, str]],
) -> RestoreResult:
    """Restaura `info` sobre `db_path`. El llamador debe impedir accesos concurrentes."""
    check = verify_backup(info)
    if not check.ok:
        raise BackupError(f"Backup no válido, no se restaura: {'; '.join(check.problems)}")
    pre = None
    if Path(db_path).exists():
        try:
            pre = create_backup(db_path, dest_dir, clock, label=PRE_RESTORE)
        except (BackupError, sqlite3.DatabaseError):
            # La base actual está dañada (probablemente por eso se restaura): se guarda tal cual.
            stamp = clock.now().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
            dest_dir.mkdir(parents=True, exist_ok=True)
            _atomic_copy(Path(db_path), dest_dir / f"pos-{stamp}-damaged.db")
    restore_into(info, db_path)
    db = Database(db_path)
    try:
        applied = db.migrate(migrations)
    finally:
        db.close()
    stats = inspect_db(db_path)
    if stats.integrity != "ok":
        raise BackupError(f"La base restaurada no supera integrity_check: {stats.integrity}")
    return RestoreResult(restored=info, pre_restore=pre, migrations_applied=applied, stats=stats)
