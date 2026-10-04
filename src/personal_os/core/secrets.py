"""Almacén de secretos local.

Reglas:
- Los secretos viven en $POS_CONFIG_DIR/secrets.toml con permisos 0600, fuera del repo.
- Alternativa: variable de entorno POS_SECRET_<NOMBRE_EN_MAYÚSCULAS>.
- Se cargan envueltos en `Secret`, cuyo repr/str nunca revela el valor, de modo que un
  log, una excepción o un print accidental no lo filtran.
- Si el fichero es legible por grupo/otros, se rechaza.
"""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path

from personal_os.core.config import config_dir

KNOWN_SECRETS = ("icloud_app_password",)


class SecretError(Exception):
    pass


class Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('********')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


def secrets_path() -> Path:
    return config_dir() / "secrets.toml"


def _check_permissions(path: Path) -> None:
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SecretError(
            f"{path} tiene permisos demasiado abiertos ({stat.filemode(mode)}). "
            f"Ejecuta: chmod 600 {path}"
        )


def _read_all() -> dict[str, str]:
    path = secrets_path()
    if not path.exists():
        return {}
    _check_permissions(path)
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return {k: str(v) for k, v in data.items()}


def get_secret(name: str) -> Secret | None:
    env = os.environ.get(f"POS_SECRET_{name.upper()}")
    if env:
        return Secret(env)
    value = _read_all().get(name)
    return Secret(value) if value else None


def require_secret(name: str) -> Secret:
    secret = get_secret(name)
    if secret is None:
        raise SecretError(
            f"Falta el secreto {name!r}. Configúralo con `pos secrets set {name}` "
            f"(se guarda en {secrets_path()} con permisos 0600)."
        )
    return secret


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def set_secret(name: str, value: str) -> Path:
    if name not in KNOWN_SECRETS:
        raise SecretError(f"Secreto desconocido {name!r}. Conocidos: {', '.join(KNOWN_SECRETS)}")
    if not value or "\n" in value:
        raise SecretError("Valor vacío o con saltos de línea")
    path = secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    current = _read_all()
    current[name] = value
    body = "# Personal OS — secretos locales. NO versionar. Permisos 0600.\n" + "".join(
        f'{k} = "{_toml_escape(v)}"\n' for k, v in sorted(current.items())
    )
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path


def secret_status() -> dict[str, str]:
    """Estado de cada secreto conocido sin revelar valores."""
    out = {}
    for name in KNOWN_SECRETS:
        if os.environ.get(f"POS_SECRET_{name.upper()}"):
            out[name] = "env"
        else:
            try:
                out[name] = "file" if _read_all().get(name) else "missing"
            except SecretError as exc:
                out[name] = f"error: {exc}"
    return out
