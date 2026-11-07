"""Cliente de solo lectura de Enable Banking (open banking PSD2, ADR 0010).

Solo habla HTTP: devuelve el JSON del banco tal cual, sin interpretarlo (eso lo hace
`finance/importers/enablebanking.py`). Credenciales en `<config_dir>/enablebanking/`:

- `<app_id>.pem`: clave privada de la aplicación (firma los JWT, RS256). Nunca se imprime.
- `sessions.json`: `{"<alias>": {"session_id", "valid_until", "accounts": [uid…]}}`, creado
  al autorizar cada banco (docs/finance.md, «Open banking»). Caduca a los ~90–180 días.
"""

from __future__ import annotations

import base64
import contextlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

API = "https://api.enablebanking.com"
MAX_PAGES = 50  # tope de seguridad al paginar


class BankingError(Exception):
    pass


@dataclass(frozen=True)
class BankSession:
    alias: str
    session_id: str
    valid_until: str  # instante ISO
    accounts: tuple[str, ...]


def load_sessions(cred_dir: Path) -> list[BankSession]:
    """Sesiones de `sessions.json`, sin red y sin cargar la clave privada."""
    path = cred_dir / "sessions.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    return [
        BankSession(
            alias=alias,
            session_id=s["session_id"],
            valid_until=s.get("valid_until", ""),
            accounts=tuple(s.get("accounts", [])),
        )
        for alias, s in sorted(raw.items())
    ]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class EnableBankingClient:
    def __init__(self, cred_dir: Path, *, http: httpx.Client | None = None) -> None:
        self.cred_dir = cred_dir
        keys = sorted(cred_dir.glob("*.pem")) if cred_dir.is_dir() else []
        if len(keys) != 1:
            raise BankingError(
                f"Falta la clave de Enable Banking en {cred_dir} (un único <app_id>.pem)"
            )
        self.app_id = keys[0].stem
        self._key = serialization.load_pem_private_key(keys[0].read_bytes(), password=None)
        self._http = http or httpx.Client(base_url=API, timeout=30)

    # --- sesiones -------------------------------------------------------------------
    @property
    def sessions_path(self) -> Path:
        return self.cred_dir / "sessions.json"

    def sessions(self) -> list[BankSession]:
        return load_sessions(self.cred_dir)

    # --- HTTP -------------------------------------------------------------------------
    def _jwt(self) -> str:
        now = int(time.time())
        header = {"typ": "JWT", "alg": "RS256", "kid": self.app_id}
        payload = {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now,
                   "exp": now + 3600}  # fmt: skip
        signing = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
        sig = self._key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{signing}.{_b64(sig)}"

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            r = self._http.get(
                path, params=params, headers={"Authorization": f"Bearer {self._jwt()}"}
            )
        except httpx.HTTPError as exc:
            raise BankingError(f"Sin conexión con Enable Banking ({type(exc).__name__})") from exc
        if r.status_code in (401, 403):
            raise BankingError(
                f"El banco rechaza el acceso (HTTP {r.status_code}): la autorización ha "
                "caducado o se ha revocado; renuévala con `pos finance bank-auth <banco>`"
            )
        if r.status_code == 429:
            raise BankingError("Límite de consultas del banco alcanzado (HTTP 429); prueba mañana")
        if r.status_code >= 400:
            raise BankingError(f"Enable Banking respondió HTTP {r.status_code} en {path}")
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        try:
            r = self._http.post(path, json=body, headers={"Authorization": f"Bearer {self._jwt()}"})
        except httpx.HTTPError as exc:
            raise BankingError(f"Sin conexión con Enable Banking ({type(exc).__name__})") from exc
        if r.status_code >= 400:
            detail = ""
            with contextlib.suppress(ValueError):
                detail = str(r.json().get("message") or r.json().get("error") or "")[:200]
            raise BankingError(
                f"Enable Banking respondió HTTP {r.status_code} en {path} {detail}".strip()
            )
        return r.json()

    # --- autorización (renovar una sesión) ---------------------------------------------
    def redirect_urls(self) -> list[str]:
        """Direcciones de retorno registradas en la aplicación de Enable Banking."""
        return list(self._get("/application").get("redirect_urls", []))

    def start_auth(self, aspsp: str, country: str, *, redirect_url: str, valid_until: str,
                   state: str) -> str:  # fmt: skip
        """Pide el enlace de autorización del banco. Devuelve la URL que abre el usuario."""
        body = {
            "access": {"valid_until": valid_until},
            "aspsp": {"name": aspsp, "country": country},
            "state": state,
            "redirect_url": redirect_url,
            "psu_type": "personal",
        }
        return self._post("/auth", body)["url"]

    def create_session(self, code: str) -> BankSession:
        """Canjea el `code` de la redirección por una sesión (cuentas autorizadas)."""
        d = self._post("/sessions", {"code": code})
        return BankSession(
            alias="",
            session_id=d["session_id"],
            valid_until=(d.get("access") or {}).get("valid_until", ""),
            accounts=tuple(a["uid"] for a in d.get("accounts", []) if a.get("uid")),
        )

    def save_session(self, alias: str, session: BankSession) -> None:
        """Sustituye la sesión `alias` en sessions.json (0600), conservando las demás."""
        raw = json.loads(self.sessions_path.read_text()) if self.sessions_path.exists() else {}
        raw[alias] = {"session_id": session.session_id, "valid_until": session.valid_until,
                      "accounts": list(session.accounts)}  # fmt: skip
        tmp = self.sessions_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2))
        tmp.chmod(0o600)
        tmp.replace(self.sessions_path)

    def balances(self, account_uid: str) -> list[dict]:
        return self._get(f"/accounts/{account_uid}/balances").get("balances", [])

    def transactions(self, account_uid: str, *, date_from: str | None = None) -> list[dict]:
        """Todos los movimientos desde `date_from` (YYYY-MM-DD), siguiendo la paginación."""
        out: list[dict] = []
        params: dict = {"date_from": date_from} if date_from else {}
        for _ in range(MAX_PAGES):
            page = self._get(f"/accounts/{account_uid}/transactions", params)
            out.extend(page.get("transactions") or [])
            key = page.get("continuation_key")
            if not key:
                return out
            params = {**params, "continuation_key": key}
        raise BankingError("Demasiadas páginas de movimientos; algo raro pasa con la paginación")
