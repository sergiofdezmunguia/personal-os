"""Respuesta de open banking (Enable Banking, PSD2) → `ParsedStatement` (ADR 0010).

Puro: recibe los dicts que devuelve la API (`/balances` y `/transactions`). La cuenta
destino la decide quien importa (la ya existente de esa entidad), así que no hace falta
consultar `/details` y se ahorra una petición del cupo diario de PSD2.

- Solo movimientos contabilizados (`BOOK`); los pendientes cambian y se reciben más tarde.
- El signo sale de `credit_debit_indicator` (Santander manda importes sin signo).
- La API no da el saldo tras cada movimiento: se reconstruye hacia atrás desde el saldo
  contable actual. El servicio lo contrasta con el saldo diario ya importado y rechaza la
  importación si no cuadra.
- Sin id del banco (`entry_reference`) el movimiento lleva un id provisional (`prov:…`),
  estable mientras el día entero quepa en la consulta. Un id real posterior (CSV de TR) lo
  sustituye, y si ya existe un movimiento igual (mismo importe a ±`match_existing_days`
  días) no se duplica.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from personal_os.finance.importers import (
    PROVISIONAL,
    ParsedStatement,
    ParsedTransaction,
    StatementError,
    mask_identifiers,
)

SOURCE = "enablebanking"
# Saldo contable por orden de preferencia (CLBD = cierre contable; OTHR lo usa TR).
BALANCE_TYPES = ("CLBD", "ITBD", "XPCD", "OTHR", "CLAV", "ITAV")


def _cents(amount: str) -> int:
    try:
        value = Decimal(str(amount))
    except InvalidOperation as exc:
        raise StatementError(f"Importe ilegible en la respuesta del banco: {amount!r}") from exc
    return int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def _signed(tx: dict) -> int:
    cents = abs(_cents(tx["transaction_amount"]["amount"]))
    indicator = tx.get("credit_debit_indicator")
    if indicator == "DBIT":
        return -cents
    if indicator == "CRDT":
        return cents
    raise StatementError(f"Movimiento sin indicador de cargo/abono ({indicator!r})")


def _description(tx: dict, fallback: str) -> str:
    parts = [p for p in (tx.get("remittance_information") or []) if p and p.strip()]
    for party in ("creditor", "debtor"):
        name = (tx.get(party) or {}).get("name")
        if name and not parts:
            parts.append(name)
    text = " ".join(" ".join(parts).split())
    return mask_identifiers(text) if text else fallback


def pick_balance(balances: list[dict]) -> tuple[int, str | None]:
    by_type = {b.get("balance_type"): b for b in balances}
    for kind in BALANCE_TYPES:
        if kind in by_type:
            b = by_type[kind]
            return _cents(b["balance_amount"]["amount"]), b.get("reference_date")
    raise StatementError("El banco no ha devuelto un saldo contable")


def parse(
    *,
    institution: str,
    account_name: str,
    account_kind: str,
    currency: str = "EUR",
    balances: list[dict],
    transactions: list[dict],
    fallback_description: str,
) -> ParsedStatement:
    balance, _ = pick_balance(balances)

    booked = [t for t in transactions if t.get("status", "BOOK") == "BOOK"]
    for t in booked:
        if (t.get("transaction_amount") or {}).get("currency", currency) != currency:
            raise StatementError("Movimiento en otra divisa: no está soportado")
    # La API los da del más reciente al más antiguo; se invierte conservando el orden del día.
    booked.sort(key=lambda t: t["booking_date"], reverse=True)
    booked.reverse()

    amounts = [_signed(t) for t in booked]
    after: list[int] = [0] * len(booked)
    running = balance
    for i in range(len(booked) - 1, -1, -1):
        after[i] = running
        running -= amounts[i]

    ordinal: Counter = Counter()
    txs = []
    for t, amount, bal in zip(booked, amounts, after, strict=True):
        ext = t.get("entry_reference") or t.get("transaction_id")
        if not ext:
            key = (t["booking_date"], amount)
            n = ordinal[key]
            ordinal[key] += 1
            raw = f"{t['booking_date']}|{amount}|{n}"
            ext = PROVISIONAL + hashlib.sha256(raw.encode()).hexdigest()[:24]
        txs.append(
            ParsedTransaction(
                booking_date=t["booking_date"],
                value_date=t.get("value_date"),
                description=_description(t, fallback_description),
                amount_cents=amount,
                balance_after_cents=bal,
                external_id=ext,
            )
        )
    return ParsedStatement(
        source=SOURCE,
        institution=institution,
        account_name=account_name,
        account_identifier="",  # la cuenta la fija quien importa (ya existe: account_id)
        currency=currency,
        transactions=txs,
        balance_end_cents=balance,
        account_kind=account_kind,
        adopt_days=3,
        match_existing_days=3,
    )
