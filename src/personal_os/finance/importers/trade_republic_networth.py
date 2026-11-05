"""Trade Republic: PDF «Extracto del patrimonio neto» (documento de la app).

Estructura observada (importes en formato español, 1.234,56):

    EXTRACTO DEL PATRIMONIO NETO
    a DD.MM.AAAA
    CARTERA VALOR EN EUR
    Cuenta de valores <importe>
    Efectivo <importe>
    TOTAL <importe> EUR
    CUENTA DE VALORES
    UNIDADES / … PRECIO POR … VALOR EN EUR
    <unidades> [unidades] <nombre…> <precio> <valor>      ← una posición
    [continuación del nombre] [DD.MM.AAAA]                ← fecha del precio
    ISIN: <isin> [DD.MM.AAAA]
    …
    NÚMERO DE POSICIONES: <n> <total> EUR
    EFECTIVO … Cuenta … <importe> EUR                     ← página 2

Validación: nº de posiciones, suma de valores = total de la cuenta de valores, valores +
efectivo = TOTAL y unidades × precio ≈ valor. Si algo no cuadra, se rechaza.
"""

from __future__ import annotations

import io
import re
from decimal import Decimal

import pdfplumber

from personal_os.finance.importers import (
    ParsedPosition,
    ParsedValuation,
    StatementError,
)

SOURCE = "trade_republic_networth_pdf"
_TITLE = "EXTRACTO DEL PATRIMONIO NETO"
_NUM = r"\d{1,3}(?:\.\d{3})*,\d+"
_POSITION_RE = re.compile(rf"^({_NUM})\s+(?:unidades\s+)?(.*?)\s*({_NUM})\s+({_NUM})$")
_ISIN_RE = re.compile(r"ISIN:\s*([A-Z]{2}[A-Z0-9]{9}\d)")
_DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")


def looks_like(data: bytes) -> bool:
    if not data.startswith(b"%PDF"):
        return False
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = pdf.pages[0].extract_text() or ""
    except Exception:
        return False
    return "TRADE REPUBLIC" in text.upper() and _TITLE in text.upper()


def parse(data: bytes) -> ParsedValuation:
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception as exc:
        raise StatementError(f"No es un PDF legible: {exc}") from exc
    lines = [ln.strip() for ln in text.splitlines()]

    try:
        title = next(i for i, ln in enumerate(lines) if ln.upper() == _TITLE)
        as_of = _date(next(ln for ln in lines[title + 1 : title + 3] if ln.startswith("a ")))
    except (StopIteration, StatementError) as exc:
        raise StatementError("No encuentro la fecha del extracto del patrimonio neto") from exc
    securities = _amount_after(text, r"Cuenta de valores")
    cash = _amount_after(text, r"Efectivo")
    total = _amount_after(text, r"TOTAL")
    count = re.search(rf"NÚMERO DE POSICIONES:\s*(\d+)\s+({_NUM})", text)
    if count is None:
        raise StatementError("No encuentro el número de posiciones")

    positions: list[ParsedPosition] = []
    pending: tuple[Decimal, str, Decimal, int] | None = None
    price_date: str | None = None
    for ln in lines:
        m = _POSITION_RE.match(ln)
        if m and not ln.startswith(("Cuenta", "Efectivo", "TOTAL")):
            shares, name, price, value = m.groups()
            pending = (_dec(shares), name.strip(), _dec(price), _cents(value))
            price_date = None
            continue
        if pending is None:
            continue
        d = _DATE_RE.search(ln)
        if d and price_date is None:
            price_date = f"{d[3]}-{d[2]}-{d[1]}"
        isin = _ISIN_RE.search(ln)
        if isin:
            shares, name, price, value = pending
            positions.append(ParsedPosition(isin[1], name, shares, price, value, price_date))
            pending = None

    if len(positions) != int(count[1]):
        raise StatementError(f"Leídas {len(positions)} posiciones y el extracto declara {count[1]}")
    if sum(p.value_cents for p in positions) != _cents(count[2]) or _cents(count[2]) != securities:
        raise StatementError("La suma de las posiciones no coincide con la cuenta de valores")
    if securities + cash != total:
        raise StatementError("Cuenta de valores + efectivo no coincide con el TOTAL")
    for p in positions:
        tolerance = max(Decimal("0.02"), p.shares * Decimal("0.005"))
        if abs(p.shares * p.price - Decimal(p.value_cents) / 100) > tolerance:
            raise StatementError(f"{p.isin}: unidades × precio no cuadra con su valor")
    return ParsedValuation(
        source=SOURCE,
        institution="trade_republic",
        as_of=as_of,
        securities_cents=securities,
        cash_cents=cash,
        total_cents=total,
        positions=positions,
    )


def _date(line: str) -> str:
    m = _DATE_RE.search(line)
    if m is None:
        raise StatementError("fecha")
    return f"{m[3]}-{m[2]}-{m[1]}"


def _amount_after(text: str, label: str) -> int:
    m = re.search(rf"^{label}\s+({_NUM})", text, re.MULTILINE)
    if m is None:
        raise StatementError(f"No encuentro «{label}» en el resumen")
    return _cents(m[1])


def _dec(text: str) -> Decimal:
    return Decimal(text.replace(".", "").replace(",", "."))


def _cents(text: str) -> int:
    return int((_dec(text) * 100).quantize(Decimal(1)))
