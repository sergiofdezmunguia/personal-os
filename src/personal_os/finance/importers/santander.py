"""Santander, banca online de particulares: «Descargar movimientos» en Excel.

Dos formatos con la misma maquetación: el clásico `.xls` (BIFF) y la exportación nueva
`.xlsx` (`export_excel.xlsx`, columnas «Fecha operación | Fecha valor | Concepto | Importe |
Saldo | Divisa», importes como texto con signo menos tipográfico «−»). El `.xlsx` se lee con
la librería estándar (zip + XML), sin dependencias.

Estructura observada (hoja «Movimientos»):

    fila 0  <nombre de la cuenta>        FECHA
    fila 1  <IBAN>                       <dd/mm/aaaa hh:mm:ss de la descarga>
    fila 2  Titular                      Saldo
    fila 3  <titular>                    <1.234,56 EUR>
    fila 5  Movimientos
    fila 7  FECHA OPERACIÓN | FECHA VALOR | CONCEPTO | IMPORTE EUR | SALDO
    fila 8… dd/mm/aaaa (texto) | dd/mm/aaaa | texto | número | número   (más reciente primero)

Entre formatos, algunas compras con tarjeta cambian de fecha de operación (no de fecha valor,
importe ni saldo): el servicio las reconoce como el mismo movimiento.

El titular nunca se lee y los números de tarjeta se enmascaran (solo 4 últimos). Las posiciones se localizan por etiqueta, no por fila fija.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

import xlrd

from personal_os.finance.importers import (
    ParsedStatement,
    ParsedTransaction,
    StatementError,
    check_balance_chain,
    mask_identifiers,
)

SOURCE = "santander_xls"
SOURCE_XLSX = "santander_xlsx"
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"
_COLUMNS = ["FECHA OPERACIÓN", "FECHA VALOR", "CONCEPTO", "IMPORTE", "SALDO"]
_IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$")
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def looks_like(data: bytes) -> bool:
    if not data.startswith((_OLE2_MAGIC, _ZIP_MAGIC)):
        return False
    try:
        return _header_row(_grid(data)[0]) is not None
    except StatementError:
        return False


def parse(data: bytes) -> ParsedStatement:
    grid, datemode = _grid(data)
    source = SOURCE if data.startswith(_OLE2_MAGIC) else SOURCE_XLSX
    header = _header_row(grid)
    if header is None:
        raise StatementError(f"No encuentro la cabecera {' | '.join(_COLUMNS)}")

    iban = None
    name = "Cuenta Santander"
    balance_end = None
    for r in range(header):
        for c, cell in enumerate(grid[r]):
            value = str(cell).strip()
            compact = value.replace(" ", "")
            if _IBAN_RE.match(compact):
                iban = compact
                above = _cell(grid, r - 1, c) if r > 0 else ""
                if above:
                    name = above.title()
            elif value.lower() == "saldo" and r + 1 < header:
                balance_end = _parse_money_text(_cell(grid, r + 1, c))
    if iban is None:
        raise StatementError("No encuentro el IBAN en la cabecera del extracto")

    currency_col = next(
        (i for i, v in enumerate(grid[header]) if str(v).strip().upper() == "DIVISA"), None
    )
    txs: list[ParsedTransaction] = []
    for r in range(header + 1, len(grid)):
        row = grid[r]
        if not row or all(str(v).strip() == "" for v in row):
            continue
        if len(row) < len(_COLUMNS):
            raise StatementError(f"Fila {r + 1}: faltan columnas")
        if currency_col is not None and str(row[currency_col]).strip() not in ("", "EUR"):
            raise StatementError(f"Fila {r + 1}: divisa no soportada (solo EUR)")
        booking, value, concept, amount, balance = row[:5]
        try:
            txs.append(
                ParsedTransaction(
                    booking_date=_parse_date(booking, datemode),
                    value_date=_parse_date(value, datemode) if value != "" else None,
                    description=mask_identifiers(" ".join(str(concept).split())),
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
        source=source,
        institution="santander",
        account_name=name,
        account_identifier=iban,
        currency="EUR",
        transactions=txs,
        balance_end_cents=balance_end,
        warnings=warnings,
    )


# --------------------------------------------------------------------------- utilidades


def _grid(data: bytes) -> tuple[list[list], int]:
    """Celdas de la hoja de movimientos (filas × columnas) y el `datemode` de Excel."""
    if data.startswith(_ZIP_MAGIC):
        return _xlsx_grid(data), 0
    try:
        book = xlrd.open_workbook(file_contents=data)
    except Exception as exc:
        raise StatementError(f"No es un Excel .xls legible: {exc}") from exc
    try:
        sheet = book.sheet_by_name("Movimientos")
    except xlrd.XLRDError:
        sheet = book.sheet_by_index(0)
    return [sheet.row_values(r) for r in range(sheet.nrows)], book.datemode


def _xlsx_grid(data: bytes) -> list[list]:
    """Primera hoja de un .xlsx con la librería estándar. Texto como str, números como float."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
            shared = []
            if "xl/sharedStrings.xml" in names:
                for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{_NS}si"):
                    shared.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
            sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise StatementError(f"No es un Excel .xlsx legible: {exc}") from exc
    grid: list[list] = []
    for row in sheet.iter(f"{_NS}row"):
        r = int(row.get("r", len(grid) + 1)) - 1
        while len(grid) <= r:
            grid.append([])
        cells: list = []
        for c in row.iter(f"{_NS}c"):
            col = _col_index(re.match(r"[A-Z]+", c.get("r", "A"))[0])
            while len(cells) < col:
                cells.append("")
            v = c.find(f"{_NS}v")
            kind = c.get("t")
            if kind == "s" and v is not None:
                cells.append(shared[int(v.text)])
            elif kind == "inlineStr":
                cells.append("".join(t.text or "" for t in c.iter(f"{_NS}t")))
            elif v is None or v.text is None:
                cells.append("")
            elif kind in ("str", "b", "e"):
                cells.append(v.text)
            else:
                cells.append(float(v.text))
        grid[r] = cells
    return grid


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _cell(grid: list[list], r: int, c: int) -> str:
    return str(grid[r][c]).strip() if c < len(grid[r]) else ""


def _header_row(grid: list[list]) -> int | None:
    for r in range(min(len(grid), 30)):
        values = [str(v).strip().upper().removesuffix(" EUR") for v in grid[r][: len(_COLUMNS)]]
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
    """'1.234,56 EUR' | '-12,30' | '−12,30' (menos tipográfico) → céntimos."""
    cleaned = (
        text.replace("EUR", "").replace("€", "").replace(" ", "").replace("\u2212", "-").strip()
    )
    if not re.fullmatch(r"-?[\d.]*\d(,\d{1,2})?", cleaned):
        raise ValueError(f"importe no reconocido {text!r}")
    normalized = cleaned.replace(".", "").replace(",", ".")
    return int((Decimal(normalized) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
