"""Registro de pagos de Apple Pay (`applepay/pagos.txt`) → pagos normalizados.

Lo escribe la automatización «Transacción» de Atajos en el iPhone, una línea por pago:
`<instante ISO>; <importe> €; <comercio>; <tarjeta>` (separador «;» con o sin espacio,
coma decimal). Las líneas vacías o sin importe/comercio (avisos de Wallet que no son un
pago) se ignoran. Puro: sin base de datos ni red.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

FILE_NAME = "pagos.txt"
_AMOUNT = re.compile(r"^(-?)\s*([0-9.]+(?:,[0-9]+)?|[0-9]+(?:\.[0-9]{1,2})?)\s*€?$")


@dataclass(frozen=True)
class CardPayment:
    at: datetime  # con zona
    amount_cents: int  # positivo = lo pagado
    merchant: str
    card: str


def _cents(text: str) -> int | None:
    m = _AMOUNT.match(text.replace(" ", " ").strip())
    if not m:
        return None
    raw = m.group(2)
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    cents = int(value * 100)
    return -cents if m.group(1) else cents


def parse(text: str) -> tuple[list[CardPayment], int]:
    """Devuelve los pagos válidos y cuántas líneas no vacías se ignoraron."""
    payments, skipped = [], 0
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 3:
            skipped += 1
            continue
        try:
            at = datetime.fromisoformat(parts[0])
        except ValueError:
            skipped += 1
            continue
        cents = _cents(parts[1]) if parts[1] else None
        merchant = " ".join(parts[2].split())
        if at.tzinfo is None or not cents or not merchant:
            skipped += 1
            continue
        card = parts[3] if len(parts) > 3 else ""
        payments.append(CardPayment(at, cents, merchant, card))
    return payments, skipped
