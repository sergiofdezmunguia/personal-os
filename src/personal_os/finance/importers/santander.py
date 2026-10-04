"""Santander, banca online de particulares: «Descargar movimientos» en Excel (.xls, BIFF).

Estructura observada (hoja «Movimientos»):

    fila 0  <nombre de la cuenta>        FECHA
    fila 1  <IBAN>                       <dd/mm/aaaa hh:mm:ss de la descarga>
    fila 2  Titular                      Saldo
    fila 3  <titular>                    <1.234,56 EUR>
    fila 5  Movimientos
    fila 7  FECHA OPERACIÓN | FECHA VALOR | CONCEPTO | IMPORTE EUR | SALDO
    fila 8… dd/mm/aaaa (texto) | dd/mm/aaaa | texto | número | número   (más reciente primero)

El titular nunca se lee y los números de tarjeta se enmascaran (solo 4 últimos). Las posiciones se localizan por etiqueta, no por fila fija.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

import xlrd

from personal_os.finance.importers import (
    ParsedStatement,
    ParsedTransaction,
    StatementError,
    check_balance_chain,
    mask_card_numbers,
)

SOURCE = "santander_xls"
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_COLUMNS = ["FECHA OPERACIÓN", "FECHA VALOR", "CONCEPTO", "IMPORTE EUR", "SALDO"]
_IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$")


def looks_like(data: bytes) -> bool:
    if not data.startswith(_OLE2_MAGIC):
        return False
    try:
        return _header_row(_sheet(data)) is not None
    except StatementError:
        return False


def parse(data: bytes) -> ParsedStatement:
    sheet = _sheet(data)
    header = _header_row(sheet)
    if header is None:
        raise StatementError(f"No encuentro la cabecera {' | '.join(_COLUMNS)}")

    iban = None
    name = "Cuenta Santander"
    balance_end = None
    for r in range(header):
        for c in range(sheet.row_len(r)):
            value = str(sheet.cell_value(r, c)).strip()
            compact = value.replace(" ", "")
            if _IBAN_RE.match(compact):
                iban = compact
                above = str(sheet.cell_value(r - 1, c)).strip() if r > 0 else ""
                if above:
                    name = above.title()
            elif value.lower() == "saldo" and r + 1 < header:
                balance_end = _parse_money_text(str(sheet.cell_value(r + 1, c)))
    if iban is None:
        raise StatementError("No encuentro el IBAN en la cabecera del extracto")

    txs: list[ParsedTransaction] = []
    for r in range(header + 1, sheet.nrows):
        row = sheet.row_values(r)
        if not row or all(str(v).strip() == "" for v in row):
            continue
        if len(row) < len(_COLUMNS):
            raise StatementError(f"Fila {r + 1}: faltan columnas")
        booking, value, concept, amount, balance = row[:5]
        try:
            txs.append(
                ParsedTransaction(
                    booking_date=_parse_date(booking, sheet.book.datemode),
                    value_date=_parse_date(value, sheet.book.datemode) if value != "" else None,
                    description=mask_card_numbers(" ".join(str(concept).split())),
                    amount_cents=_cents(amount),
                    balance_after_cents=_cents(balance) if balance != "" else None,
                )
            )
        except (ValueError, TypeError) as exc:
            raise StatementError(f"Fila {r + 1}: {exc}") from exc

    txs.reverse()  # el banco da el más reciente primero
    if [t.booking_date for t in txs] != sorted(t.booking_date for t in txs):
        raise StatementError("Las fechas de operación no están ordenadas: formato inesperado")
    check_balance_chain(txs)
    warnings = []
    if balance_end is not None and txs and txs[-1].balance_after_cents != balance_end:
        warnings.append(
            "El saldo de la cabecera no coincide con el del último movimiento "
            "(suelen ser compras con tarjeta pendientes o posteriores al último movimiento)"
        )
    return ParsedStatement(
        source=SOURCE,
        institution="santander",
        account_name=name,
        account_identifier=iban,
        currency="EUR",
        transactions=txs,
        balance_end_cents=balance_end,
        warnings=warnings,
    )


# --------------------------------------------------------------------------- utilidades


def _sheet(data: bytes):
    try:
        book = xlrd.open_workbook(file_contents=data)
    except Exception as exc:
        raise StatementError(f"No es un Excel .xls legible: {exc}") from exc
    try:
        return book.sheet_by_name("Movimientos")
    except xlrd.XLRDError:
        return book.sheet_by_index(0)


def _header_row(sheet) -> int | None:
    for r in range(min(sheet.nrows, 30)):
        values = [str(v).strip().upper() for v in sheet.row_values(r)[: len(_COLUMNS)]]
        if values == _COLUMNS:
            return r
    return None


def _parse_date(value, datemode: int) -> str:
    if isinstance(value, float):  # celda de fecha nativa de Excel
        return xlrd.xldate_as_datetime(value, datemode).date().isoformat()
    text = str(value).strip()
    try:
        return datetime.strptime(text, "%d/%m/%Y").date().isoformat()
    except ValueError:
        raise ValueError(f"fecha no reconocida {text!r} (se espera dd/mm/aaaa)") from None


def _cents(value) -> int:
    if isinstance(value, str):
        return _parse_money_text(value)
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _parse_money_text(text: str) -> int:
    """'1.234,56 EUR' | '-12,30' → céntimos."""
    cleaned = text.replace("EUR", "").replace("€", "").replace(" ", "").strip()
    if not re.fullmatch(r"-?[\d.]*\d(,\d{1,2})?", cleaned):
        raise ValueError(f"importe no reconocido {text!r}")
    normalized = cleaned.replace(".", "").replace(",", ".")
    return int((Decimal(normalized) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
