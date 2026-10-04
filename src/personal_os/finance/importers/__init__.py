"""Parsers de extractos bancarios: bytes del fichero → `ParsedStatement` normalizado.

Puros (sin base de datos ni red). Cada parser valida el formato y la coherencia interna
(p. ej. que el saldo encadene) y falla con `StatementError` si algo no cuadra: es mejor
rechazar un extracto que importar datos dudosos.
"""

from __future__ import annotations

import hashlib
import itertools
import re
from dataclasses import dataclass, field


class StatementError(Exception):
    pass


@dataclass(frozen=True)
class ParsedTransaction:
    booking_date: str  # YYYY-MM-DD
    value_date: str | None
    description: str
    amount_cents: int  # negativo = cargo
    balance_after_cents: int | None


@dataclass(frozen=True)
class ParsedStatement:
    source: str  # p. ej. "santander_xls"
    institution: str
    account_name: str
    account_identifier: str  # IBAN u otro: solo se usa para derivar external_ref/last4
    currency: str
    transactions: list[ParsedTransaction]  # de la más antigua a la más reciente
    balance_end_cents: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def external_ref(self) -> str:
        return hashlib.sha256(self.account_identifier.replace(" ", "").encode()).hexdigest()

    @property
    def last4(self) -> str:
        return self.account_identifier.replace(" ", "")[-4:]

    @property
    def period(self) -> tuple[str | None, str | None]:
        if not self.transactions:
            return None, None
        return self.transactions[0].booking_date, self.transactions[-1].booking_date


def detect(data: bytes, file_name: str) -> ParsedStatement:
    """Elige el parser según el contenido del fichero."""
    from personal_os.finance.importers import santander

    if santander.looks_like(data):
        return santander.parse(data)
    raise StatementError(
        f"No reconozco el formato de {file_name}. Soportado: Santander (.xls de la banca online)."
    )


_CARD_RE = re.compile(r"(?<!\d)\d{9,15}(\d{4})(?!\d)")


def mask_card_numbers(text: str) -> str:
    """Los bancos incluyen el número completo de la tarjeta en el concepto: se conservan
    solo los 4 últimos (13-19 dígitos seguidos = PAN)."""
    return _CARD_RE.sub(lambda m: "····" + m.group(1), text)


def check_balance_chain(txs: list[ParsedTransaction]) -> None:
    """Cada saldo debe ser el anterior más el importe. Detecta extractos editados o incompletos."""
    for prev, cur in itertools.pairwise(txs):
        if prev.balance_after_cents is None or cur.balance_after_cents is None:
            continue
        if prev.balance_after_cents + cur.amount_cents != cur.balance_after_cents:
            raise StatementError(
                f"El saldo no encadena el {cur.booking_date} ({cur.description[:40]!r}): "
                "el extracto parece incompleto o editado"
            )
