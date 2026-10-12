"""Trade Republic: CSV «Exportación de transacción» (exportación de la actividad de la app).

Estructura observada: UTF-8, separador coma, campos entre comillas, una fila por movimiento, todo el
historial de la cuenta desde su apertura (no en orden). Columnas que se usan:

    datetime (UTC, ISO con Z) · date (día del movimiento) · category (CASH | TRADING) ·
    type (CARD_TRANSACTION, BUY, SELL, DIVIDEND, TRANSFER_INSTANT_INBOUND…) ·
    asset_class · name · symbol (ISIN) · shares · price · amount · fee · tax · currency ·
    description · transaction_id

Todo movimiento afecta al efectivo: `amount + fee + tax` (decimales con punto; fee y tax
negativos cuando se pagan). Como el fichero empieza en la apertura de la cuenta, el saldo
se reconstruye sumando desde cero; el servicio lo contrasta con lo ya importado y la
valoración (PDF) con su efectivo. Nunca se leen `counterparty_name`/`counterparty_iban`.

Las descripciones de efectivo llevan el mismo prefijo que el PDF «Extracto de cuenta»
(«Transacción con tarjeta: …», «Operar: …»), para que las reglas existentes sigan sirviendo.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from personal_os.finance.importers import (
    ParsedActivity,
    ParsedStatement,
    ParsedTrade,
    ParsedTransaction,
    StatementError,
    mask_identifiers,
)

SOURCE = "trade_republic_csv"
REQUIRED = (
    "datetime", "date", "category", "type", "asset_class", "name", "symbol", "shares",
    "price", "amount", "fee", "tax", "currency", "description", "transaction_id",
)  # fmt: skip

# Prefijo del PDF de cuenta para cada tipo (los no listados usan su nombre: aviso).
PREFIXES = {
    "CARD_TRANSACTION": "Transacción con tarjeta",
    "CARD_TRANSACTION_INTERNATIONAL": "Transacción con tarjeta",
    "TRANSFER_INSTANT_INBOUND": "Transferencia",
    "TRANSFER_INSTANT_OUTBOUND": "Transferencia",
    "TRANSFER_INBOUND": "Transferencia",
    "TRANSFER_OUTBOUND": "Transferencia",
    "TRANSFER_DIRECT_DEBIT_INBOUND": "Recibos domiciliados",
    "BUY": "Operar",
    "SELL": "Operar",
    "DIVIDEND": "Rendimientos",
    "INTEREST_PAYMENT": "Interés",
    "BENEFITS_SAVEBACK": "Bonificación",
    "STOCKPERK": "Bonificación",
}
TRADE_KINDS = {"BUY": "buy", "SELL": "sell", "DIVIDEND": "dividend"}
SAVINGS_PLAN_MARK = "savings plan execution"


def looks_like(data: bytes) -> bool:
    try:
        header = next(csv.reader(io.StringIO(data[:4000].decode("utf-8-sig", "ignore"))))
    except (StopIteration, csv.Error):
        return False
    return set(REQUIRED) <= {h.strip() for h in header}


def parse(data: bytes) -> ParsedActivity:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StatementError("El CSV no está en UTF-8") from exc
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise StatementError("El CSV no tiene movimientos")

    parsed = []
    seen: set[str] = set()
    unknown: set[str] = set()
    for n, row in enumerate(rows, start=2):
        ext = (row.get("transaction_id") or "").strip()
        if not ext:
            raise StatementError(f"Fila {n}: falta transaction_id")
        if ext in seen:
            raise StatementError(f"Fila {n}: transaction_id repetido")
        seen.add(ext)
        if row["currency"] != "EUR":
            raise StatementError(f"Fila {n}: divisa {row['currency']!r} no soportada (solo EUR)")
        try:
            when = datetime.fromisoformat(row["datetime"].replace("Z", "+00:00"))
            day = datetime.strptime(row["date"], "%Y-%m-%d").date().isoformat()
            amount, fee, tax = (_cents(row[k]) for k in ("amount", "fee", "tax"))
        except (ValueError, InvalidOperation) as exc:
            raise StatementError(f"Fila {n}: fecha o importe con formato inesperado") from exc
        if row["type"] not in PREFIXES:
            unknown.add(row["type"])
        parsed.append((when.astimezone(UTC), day, amount, fee, tax, row))

    parsed.sort(key=lambda p: (p[0], p[1]))
    txs: list[ParsedTransaction] = []
    trades: list[ParsedTrade] = []
    balance = 0
    for when, day, amount, fee, tax, row in parsed:
        balance += amount + fee + tax
        prefix = PREFIXES.get(row["type"], row["type"].replace("_", " ").capitalize())
        desc = mask_identifiers(" ".join(row["description"].split()))
        txs.append(
            ParsedTransaction(
                booking_date=day,
                value_date=None,
                description=f"{prefix}: {desc}" if desc else prefix,
                amount_cents=amount + fee + tax,
                balance_after_cents=balance,
                external_id=row["transaction_id"],
            )
        )
        kind = TRADE_KINDS.get(row["type"])
        if kind and row["symbol"] and row["shares"]:
            trades.append(_trade(kind, when, day, amount, fee, tax, row))

    warnings = []
    if unknown:
        warnings.append(
            f"Tipos de movimiento nuevos: {', '.join(sorted(unknown))} (importados con su nombre)"
        )
    cash = ParsedStatement(
        source=SOURCE,
        institution="trade_republic",
        account_name="Trade Republic · Cuenta",
        account_identifier="",  # el CSV no trae IBAN: el servicio usa la cuenta de TR existente
        currency="EUR",
        transactions=txs,
        balance_end_cents=balance,
        account_kind="broker",
        adopt_days=3,
    )
    return ParsedActivity(SOURCE, "trade_republic", cash, trades, warnings)


def _trade(kind, when, day, amount, fee, tax, row) -> ParsedTrade:
    shares = Decimal(row["shares"])
    if kind == "sell" and shares > 0:
        shares = -shares
    return ParsedTrade(
        external_id=row["transaction_id"],
        trade_date=day,
        executed_at=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        kind=kind,
        isin=row["symbol"].strip(),
        name=" ".join(row["name"].split()),
        asset_class=row["asset_class"],
        shares=shares,
        price=Decimal(row["price"]) if row["price"] else None,
        amount_cents=amount,
        fee_cents=fee,
        tax_cents=tax,
        savings_plan=row["description"].lower().startswith(SAVINGS_PLAN_MARK),
    )


def _cents(text: str) -> int:
    """'-12.345678' → -1235 (al céntimo, redondeo bancario); '' → 0."""
    if not text:
        return 0
    return int((Decimal(text) * 100).quantize(Decimal(1)))
