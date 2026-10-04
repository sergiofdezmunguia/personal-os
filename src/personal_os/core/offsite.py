"""Copia de los backups fuera de este disco (p. ej. iCloud Drive), cifrada con age.

- Cada backup local `pos-….db.gz` se sube como `pos-….db.gz.age` (cifrado para la clave
  pública `[backup].offsite_recipient`) junto a su manifiesto `.json` sin cifrar (sha256,
  filas por tabla y migraciones: sin datos personales).
- Escritura atómica (tmp + rename) y relectura byte a byte tras escribir.
- La retención es la misma que la local (`backup.prune` reconoce el sufijo `.age`).
- La clave privada es un fichero de identidad age estándar (0600, fuera del repo): se puede
  descifrar sin el Personal OS con `age -d -i <identidad> fichero.db.gz.age`.
- Este PC solo necesita la clave privada para verificar y recuperar, no para subir.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pyrage

from personal_os.core import backup as bk


class OffsiteError(Exception):
    pass


@dataclass
class OffsiteSyncResult:
    pushed: bk.BackupInfo | None = None
    removed: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- claves


def parse_recipient(value: str) -> pyrage.x25519.Recipient:
    try:
        return pyrage.x25519.Recipient.from_str(value.strip())
    except pyrage.RecipientError as exc:
        raise OffsiteError(f"Clave pública age no válida: {value!r}") from exc


def load_identity(path: Path) -> pyrage.x25519.Identity:
    if not path.exists():
        raise OffsiteError(f"No existe la clave privada {path}")
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise OffsiteError(
            f"{path} tiene permisos demasiado abiertos ({stat.filemode(mode)}). "
            f"Ejecuta: chmod 600 {path}"
        )
    lines = [
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    if len(lines) != 1:
        raise OffsiteError(f"{path} debe contener exactamente una clave AGE-SECRET-KEY-…")
    try:
        return pyrage.x25519.Identity.from_str(lines[0])
    except Exception as exc:
        raise OffsiteError(f"{path} no contiene una clave age válida") from exc


def matches(identity: pyrage.x25519.Identity, recipient: pyrage.x25519.Recipient) -> bool:
    return str(identity.to_public()) == str(recipient)


def generate_identity(path: Path) -> str:
    """Crea la clave privada (0600) y devuelve la pública. Nunca sobrescribe."""
    if path.exists():
        raise OffsiteError(f"{path} ya existe; no se sobrescribe una clave")
    path.parent.mkdir(parents=True, exist_ok=True)
    identity = pyrage.x25519.Identity.generate()
    public = str(identity.to_public())
    created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = f"# created: {created}\n# public key: {public}\n{identity}\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    return public


# --------------------------------------------------------------------------- subir / bajar


def encrypted_name(info: bk.BackupInfo) -> str:
    return info.path.name + bk.ENCRYPTED_SUFFIX


def push(
    info: bk.BackupInfo, offsite_dir: Path, recipient: pyrage.x25519.Recipient
) -> bk.BackupInfo:
    """Cifra y copia un backup local al destino externo, y relee lo escrito."""
    if info.encrypted:
        raise OffsiteError(f"{info.path.name} ya está cifrado")
    if not offsite_dir.is_dir():
        raise OffsiteError(f"El destino externo {offsite_dir} no existe o no está accesible")
    blob = pyrage.encrypt(info.path.read_bytes(), [recipient])
    remote = bk.BackupInfo(
        path=offsite_dir / encrypted_name(info),
        created_at=info.created_at,
        label=info.label,
        manifest=info.manifest,
    )
    _atomic_write(remote.path, blob)
    _atomic_write(remote.manifest_path, info.manifest_path.read_bytes())
    if remote.path.read_bytes() != blob:
        raise OffsiteError(f"Lo leído de {remote.path} no coincide con lo escrito")
    return remote


def fetch(info: bk.BackupInfo, local_dir: Path, identity: pyrage.x25519.Identity) -> bk.BackupInfo:
    """Descifra un backup externo en el directorio local de backups y lo verifica."""
    if not info.encrypted:
        raise OffsiteError(f"{info.path.name} no es un backup cifrado")
    local_dir.mkdir(parents=True, exist_ok=True)
    local = bk.BackupInfo(
        path=local_dir / info.path.name.removesuffix(bk.ENCRYPTED_SUFFIX),
        created_at=info.created_at,
        label=info.label,
        manifest=info.manifest,
    )
    if local.path.exists():
        raise OffsiteError(f"Ya existe {local.path.name} en {local_dir}")
    try:
        data = pyrage.decrypt(info.path.read_bytes(), [identity])
    except pyrage.DecryptError as exc:
        raise OffsiteError(f"No se puede descifrar {info.path.name}: {exc}") from exc
    _atomic_write(local.path, data)
    _atomic_write(local.manifest_path, info.manifest_path.read_bytes())
    check = bk.verify_backup(local)
    if not check.ok:
        local.path.unlink()
        local.manifest_path.unlink(missing_ok=True)
        raise OffsiteError(f"El backup descifrado no se verifica: {'; '.join(check.problems)}")
    return local


def sync(
    local_dir: Path,
    offsite_dir: Path,
    recipient: pyrage.x25519.Recipient,
    *,
    keep_daily: int,
    keep_weekly: int,
    keep_monthly: int,
) -> OffsiteSyncResult:
    """Sube el último backup local si aún no está fuera y aplica la retención en el destino.

    Idempotente: se llama en cada `pos sync`, así que una subida fallida se reintenta sola.
    """
    result = OffsiteSyncResult()
    latest = bk.latest_backup(local_dir)
    if latest is not None and not (offsite_dir / encrypted_name(latest)).exists():
        result.pushed = push(latest, offsite_dir, recipient)
    result.removed, result.warnings = bk.prune(
        offsite_dir, keep_daily=keep_daily, keep_weekly=keep_weekly, keep_monthly=keep_monthly
    )
    return result


def _atomic_write(target: Path, data: bytes) -> None:
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
