"""Trade Republic: «Extracto de cuenta» en PDF (app o web → Documentos).

Estructura observada (por página, A4):

    cabecera:  ... FECHA <periodo> / IBAN <iban> / BIC ...
    RESUMEN DE ... DE CUENTA: PRODUCTO | BALANCE INICIAL | ENTRADA | SALIDA | BALANCE FINAL
    TRANSACCIONES DE CUENTA:  FECHA | TIPO | DESCRIPCIÓN | ENTRADA | SALIDA | BALANCE

Cada movimiento ocupa varias líneas; TIPO y DESCRIPCIÓN pueden partirse entre ellas:

    12 jul  Transacción   <descripción…>
            …             <descripción…>   12,34 €   1.234,56 €
    2026    con tarjeta   <descripción…>

Por eso las columnas se separan por su posición horizontal (cabeceras TIPO y DESCRIPCIÓN)
y no por el texto. El PDF no conserva de forma fiable en qué columna (entrada/salida) va el
importe: el signo se deduce del saldo (sube ⇒ entrada, baja ⇒ salida), empezando por el
BALANCE INICIAL del resumen. Si un importe no cuadra en ningún sentido, se rechaza el
extracto; al final el saldo debe coincidir con el BALANCE FINAL.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import pdfplumber

from personal_os.finance.importers import (
    ParsedStatement,
    ParsedTransaction,
    StatementError,
    mask_identifiers,
)

SOURCE = "trade_republic_pdf"
_MONTHS = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dic": 12,
}  # fmt: skip
_MONEY_RE = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}$")
_IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$")
_FOOTER_MARK = "GmbH,"


def looks_like(data: bytes) -> bool:
    if not data.startswith(b"%PDF"):
        return False
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = pdf.pages[0].extract_text() or ""
    except Exception:
        return False
    return "TRADE REPUBLIC" in text.upper() and "TRANSACCIONES DE CUENTA" in text.upper()


@dataclass
class _Record:
    day: int
    month: int
    year: int | None = None
    type_parts: list[str] = field(default_factory=list)
    desc_parts: list[str] = field(default_factory=list)
    money: list[str] = field(default_factory=list)


def parse(data: bytes) -> ParsedStatement:
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as exc:
        raise StatementError(f"No es un PDF legible: {exc}") from exc
    with pdf:
        iban, product, initial, final = _summary(pdf.pages[0])
        records: list[_Record] = []
        current: _Record | None = None  # un movimiento puede partirse entre páginas
        for page in pdf.pages:
            current = _page_records(page, records, current)

    if not records:
        raise StatementError("No encuentro movimientos en el extracto")
    txs: list[ParsedTransaction] = []
    balance = initial
    for r in records:
        if r.year is None or len(r.money) != 2:
            raise StatementError(
                f"Movimiento del {r.day}/{r.month} con formato inesperado "
                f"({len(r.money)} importes, año {'sí' if r.year else 'no'})"
            )
        amount, after = _cents(r.money[0]), _cents(r.money[1])
        if balance + amount == after:
            signed = amount
        elif balance - amount == after:
            signed = -amount
        else:
            raise StatementError(
                f"El saldo no encadena el {r.year}-{r.month:02d}-{r.day:02d}: "
                "el extracto parece incompleto o editado"
            )
        balance = after
        kind = " ".join(r.type_parts)
        desc = " ".join(r.desc_parts)
        txs.append(
            ParsedTransaction(
                booking_date=f"{r.year:04d}-{r.month:02d}-{r.day:02d}",
                value_date=None,
                description=mask_identifiers(f"{kind}: {desc}" if kind else desc),
                amount_cents=signed,
                balance_after_cents=after,
            )
        )
    if balance != final:
        raise StatementError("El saldo del último movimiento no coincide con el BALANCE FINAL")
    if [t.booking_date for t in txs] != sorted(t.booking_date for t in txs):
        raise StatementError("Las fechas no están ordenadas: formato inesperado")
    return ParsedStatement(
        source=SOURCE,
        institution="trade_republic",
        account_name=f"Trade Republic · {product}",
        account_identifier=iban,
        currency="EUR",
        transactions=txs,
        balance_end_cents=final,
        account_kind="broker",
    )


# --------------------------------------------------------------------------- piezas


def _lines(words, min_top: float = 0.0) -> list[list[dict]]:
    """Agrupa palabras en líneas (misma altura, ±2 pt), de arriba abajo y de izquierda a derecha."""
    rows: list[list[dict]] = []
    for w in sorted((w for w in words if w["top"] > min_top), key=lambda w: (w["top"], w["x0"])):
        if rows and abs(rows[-1][0]["top"] - w["top"]) <= 2:
            rows[-1].append(w)
        else:
            rows.append([w])
    return [sorted(r, key=lambda w: w["x0"]) for r in rows]


def _summary(page) -> tuple[str, str, int, int]:
    words = page.extract_words()
    iban = None
    for line in _lines(words):
        texts = [w["text"] for w in line]
        if "IBAN" in texts:
            i = texts.index("IBAN")
            if i + 1 < len(texts) and _IBAN_RE.match(texts[i + 1]):
                iban = texts[i + 1]
    if iban is None:
        raise StatementError("No encuentro el IBAN de la cuenta en la cabecera")
    header = next((ln for ln in _lines(words) if [w["text"] for w in ln][:1] == ["PRODUCTO"]), None)
    if header is None:
        raise StatementError("No encuentro el resumen (PRODUCTO / BALANCE INICIAL…)")
    below = _lines(words, min_top=header[0]["top"] + 3)[0]
    money = [w["text"] for w in below if _MONEY_RE.match(w["text"])]
    if len(money) != 4:
        raise StatementError("El resumen de la cuenta no tiene el formato esperado")
    product = " ".join(w["text"] for w in below if w["x0"] < 150 and w["text"] != "€")
    return iban, product or "Cuenta", _cents(money[0]), _cents(money[3])


def _page_records(page, records: list[_Record], current: _Record | None) -> _Record | None:
    """Añade a `records` los movimientos de la página; devuelve el que queda a medias.

    Cada movimiento es una franja vertical que empieza en un día de la columna FECHA (con su
    mes en la misma línea o justo debajo) y acaba donde empieza el siguiente. Así da igual
    que la fecha venga en una línea («12 jul») o apilada (día / mes / año).
    """
    words = page.extract_words()
    tipo = [w for w in words if w["text"] == "TIPO"]
    desc = [w for w in words if w["text"] == "DESCRIPCIÓN"]
    if not tipo or not desc:
        return current
    x_type, x_desc, top = tipo[-1]["x0"] - 2, desc[-1]["x0"] - 2, tipo[-1]["top"] + 3
    body = sorted((w for w in words if w["top"] > top), key=lambda w: (w["top"], w["x0"]))
    date_col = [w for w in body if w["x0"] < x_type]

    # Fin de la tabla: pie de página o texto en el margen que no es día/mes/año.
    end = page.height
    for w in body:
        if w["text"] == _FOOTER_MARK:
            end = min(end, w["top"] - 2)
    for w in date_col:
        if not (re.fullmatch(r"\d{1,4}", w["text"]) or _month(w["text"])):
            end = min(end, w["top"] - 2)
            break
    body = [w for w in body if w["top"] < end]
    date_col = [w for w in date_col if w["top"] < end]

    anchors = []
    for w in date_col:
        if re.fullmatch(r"\d{1,2}", w["text"]):
            month = next(
                (m for m in date_col if 0 <= m["top"] - w["top"] <= 20 and _month(m["text"])),
                None,
            )
            if month is not None:
                anchors.append((w, month))

    bounds = [a[0]["top"] - 0.5 for a in anchors] + [end]
    bands = []
    if anchors and current is not None:
        bands.append((current, top, bounds[0]))  # continuación de la página anterior
    elif not anchors and current is not None:
        bands.append((current, top, end))
    for k, (day, month) in enumerate(anchors):
        rec = _Record(day=int(day["text"]), month=_month(month["text"]))
        records.append(rec)
        bands.append((rec, bounds[k], bounds[k + 1]))

    for rec, lo, hi in bands:
        for w in body:
            if not lo <= w["top"] < hi:
                continue
            text = w["text"]
            if w["x0"] < x_type:
                if re.fullmatch(r"\d{4}", text) and rec.year is None:
                    rec.year = int(text)
                continue  # día y mes ya leídos
            if _MONEY_RE.match(text):
                rec.money.append(text)
            elif text == "€":
                continue
            elif w["x0"] < x_desc:
                rec.type_parts.append(text)
            else:
                rec.desc_parts.append(text)
    last = records[-1] if records else None
    return last if last is not None and last.year is None else None


def _month(token: str) -> int | None:
    """'jul' | 'sept.' | 'Sep' → número de mes."""
    return _MONTHS.get(token.lower().rstrip("."))


def _cents(text: str) -> int:
    negative = text.startswith("-")
    euros, cents = text.lstrip("-").replace(".", "").split(",")
    value = int(euros) * 100 + int(cents)
    return -value if negative else value
