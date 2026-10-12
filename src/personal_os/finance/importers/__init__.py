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
from decimal import Decimal


class StatementError(Exception):
    pass


@dataclass(frozen=True)
class ParsedTransaction:
    booking_date: str  # YYYY-MM-DD
    value_date: str | None
    description: str
    amount_cents: int  # negativo = cargo
    balance_after_cents: int | None
    external_id: str | None = None  # id del banco, si lo da (TR CSV): deduplica por él


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
    account_kind: str = "bank"  # bank | broker | card | cash
    # Movimientos con `external_id`: si en la cuenta hay uno sin id con el mismo importe a
    # ±`adopt_days` días (importado de otra fuente), se enlaza en vez de duplicarlo.
    adopt_days: int = 0

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


@dataclass(frozen=True)
class ParsedTrade:
    external_id: str
    trade_date: str  # YYYY-MM-DD
    executed_at: str  # UTC ISO con Z
    kind: str  # buy | sell | dividend
    isin: str
    name: str
    asset_class: str
    shares: Decimal  # negativo en ventas
    price: Decimal | None
    amount_cents: int  # efecto en el efectivo, sin comisión ni impuestos
    fee_cents: int
    tax_cents: int
    savings_plan: bool


@dataclass(frozen=True)
class ParsedActivity:
    """Exportación de actividad de un bróker: movimientos de efectivo + operaciones."""

    source: str
    institution: str
    cash: ParsedStatement
    trades: list[ParsedTrade]
    warnings: list[str] = field(default_factory=list)

    @property
    def period(self) -> tuple[str | None, str | None]:
        return self.cash.period


@dataclass(frozen=True)
class ParsedPosition:
    isin: str
    name: str
    shares: Decimal
    price: Decimal
    value_cents: int
    price_date: str | None


@dataclass(frozen=True)
class ParsedValuation:
    """Foto de la cartera a una fecha (posiciones valoradas + efectivo)."""

    source: str
    institution: str
    as_of: str  # YYYY-MM-DD
    securities_cents: int
    cash_cents: int
    total_cents: int
    positions: list[ParsedPosition]
    warnings: list[str] = field(default_factory=list)

    @property
    def period(self) -> tuple[str | None, str | None]:
        return self.as_of, self.as_of


Parsed = ParsedStatement | ParsedActivity | ParsedValuation


def detect(data: bytes, file_name: str) -> Parsed:
    """Elige el parser según el contenido del fichero."""
    from personal_os.finance.importers import (
        santander,
        trade_republic,
        trade_republic_csv,
        trade_republic_networth,
    )

    if santander.looks_like(data):
        return santander.parse(data)
    if trade_republic.looks_like(data):
        return trade_republic.parse(data)
    if trade_republic_csv.looks_like(data):
        return trade_republic_csv.parse(data)
    if trade_republic_networth.looks_like(data):
        return trade_republic_networth.parse(data)
    raise StatementError(
        f"No reconozco el formato de {file_name}. Soportados: Santander (.xls de la banca "
        "online) y Trade Republic (PDF «Extracto de cuenta», CSV «Exportación de "
        "transacción» y PDF «Extracto del patrimonio neto»)."
    )


def describe(parsed: Parsed) -> str:
    """Una línea para `--dry-run`: qué es y qué contiene (sin datos sensibles)."""
    first, last = parsed.period
    if isinstance(parsed, ParsedValuation):
        return f"{parsed.source}: cartera a {parsed.as_of} · {len(parsed.positions)} posiciones"
    if isinstance(parsed, ParsedActivity):
        return (
            f"{parsed.source}: {len(parsed.cash.transactions)} movimientos de efectivo, "
            f"{len(parsed.trades)} operaciones · {first} → {last}"
        )
    return (
        f"{parsed.source}: {parsed.account_name} ····{parsed.last4} · "
        f"{len(parsed.transactions)} movimientos · {first} → {last}"
    )


_CARD_RE = re.compile(r"(?<!\d)\d{9,15}(\d{4})(?!\d)")


def mask_card_numbers(text: str) -> str:
    """Los bancos incluyen el número completo de la tarjeta en el concepto: se conservan
    solo los 4 últimos (13-19 dígitos seguidos = PAN)."""
    return _CARD_RE.sub(lambda m: "····" + m.group(1), text)


_IBAN_IN_TEXT_RE = re.compile(r"\b([A-Z]{2}\d{2})[A-Z0-9]{8,26}([A-Z0-9]{4})\b")
_PHONE_RE = re.compile(r"\+\d{2,3}-?\d{6,9}(\d{3})\b")


def mask_identifiers(text: str) -> str:
    """Tarjetas (4 últimos), IBAN de terceros (país + 4 últimos) y teléfonos (3 últimos)."""
    text = mask_card_numbers(text)
    text = _IBAN_IN_TEXT_RE.sub(lambda m: f"{m.group(1)[:2]}····{m.group(2)}", text)
    return _PHONE_RE.sub(lambda m: "+··· ···" + m.group(1), text)


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
