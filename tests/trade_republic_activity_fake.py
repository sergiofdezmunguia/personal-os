"""Exportación CSV de actividad y PDF «Extracto del patrimonio neto» de Trade Republic con
datos INVENTADOS (ISIN ficticios XF000FAKE…).

Cartera de ejemplo (de junio a septiembre de 2026):
- Plan: FAKE001 dos veces al mes (50 € cada una) y FAKE002 una vez al mes (25 €).
- Compra suelta de 3 FAKE003 (30 € + 1 € de comisión) y venta de 1,5 (18 € − 1 €).
- Efectivo: ingreso inicial, tarjeta, intereses, saveback, recibo y un dividendo.

A 30-09-2026: FAKE001 8 participaciones (coste 400 €), FAKE002 4 (100 €), FAKE003 1,5 (15,50 €).
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, replace

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

HEADER = [
    "datetime", "date", "account_type", "category", "type", "asset_class", "name", "symbol",
    "shares", "price", "amount", "fee", "tax", "currency", "original_amount",
    "original_currency", "fx_rate", "description", "transaction_id", "counterparty_name",
    "counterparty_iban", "payment_reference", "mcc_code",
]  # fmt: skip


@dataclass(frozen=True)
class Row:
    date: str
    type: str
    amount: str
    description: str = ""
    isin: str = ""
    name: str = ""
    asset_class: str = ""
    shares: str = ""
    price: str = ""
    fee: str = ""
    tax: str = ""
    time: str = "10:00:00.000000"
    tid: str = ""
    currency: str = "EUR"

    @property
    def cash_cents(self) -> int:
        return sum(round(float(v or 0) * 100) for v in (self.amount, self.fee, self.tax))


def plan(date: str, isin: str, name: str, price: str, cost: str) -> Row:
    return Row(
        date, "BUY", f"-{cost}", f"Savings plan execution {isin} {name}, quantity: 1",
        isin, name, "FUND", "1", price, time="07:30:00.123Z",
    )  # fmt: skip


def _rows() -> list[Row]:
    rows = [
        Row(
            "2026-06-01",
            "TRANSFER_INSTANT_INBOUND",
            "1000.00",
            "Incoming transfer from ES0000000000000000001234",
        ),
        Row("2026-06-05", "CARD_TRANSACTION", "-12.340000", "Supermercado Ficticio"),
        Row(
            "2026-06-10",
            "BUY",
            "-30.00",
            "Buy trade XF000FAKE003 FAKE Corp, quantity: 3",
            "XF000FAKE003",
            "FAKE Corp",
            "STOCK",
            "3",
            "10.00",
            fee="-1.00",
        ),
    ]
    for month in ("06", "07", "08", "09"):
        rows += [
            plan(f"2026-{month}-02", "XF000FAKE001", "FAKE World ETF", "50.00", "50.00"),
            plan(f"2026-{month}-02", "XF000FAKE002", "FAKE Bonds ETF", "25.00", "25.00"),
            plan(f"2026-{month}-16", "XF000FAKE001", "FAKE World ETF", "50.00", "50.00"),
        ]
    rows += [
        Row(
            "2026-08-20",
            "SELL",
            "18.00",
            "Sell trade XF000FAKE003 FAKE Corp, quantity: 1.5",
            "XF000FAKE003",
            "FAKE Corp",
            "STOCK",
            "-1.5",
            "12.00",
            fee="-1.00",
        ),
        Row(
            "2026-09-15",
            "DIVIDEND",
            "3.00",
            "Cash Dividend for ISIN XF000FAKE001",
            "XF000FAKE001",
            "FAKE World ETF",
            "FUND",
            "6",
            tax="-0.57",
        ),
        Row("2026-09-20", "INTEREST_PAYMENT", "1.23", "Interest payment", tax="-0.23"),
        Row("2026-09-21", "BENEFITS_SAVEBACK", "0.50", "Saveback payment"),
        Row("2026-09-22", "TRANSFER_DIRECT_DEBIT_INBOUND", "-9.99", "Sepa Direct Debit FAKE Gym"),
        Row(
            "2026-09-23", "CARD_TRANSACTION", "-4.000000", "Tienda 4000000000004321 (+34-600111222)"
        ),
        Row("2026-10-02", "CARD_TRANSACTION", "-2.500000", "Café Inventado"),
    ]
    return [
        replace(r, tid=r.tid or f"0000fake-0000-0000-0000-{i:012d}") for i, r in enumerate(rows)
    ]


ROWS = _rows()


def cash_at(rows: list[Row], day: str) -> int:
    return sum(r.cash_cents for r in rows if r.date <= day)


def build_csv(rows: list[Row] = ROWS) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL)
    w.writerow(HEADER)
    for r in reversed(rows):  # TR no exporta en orden cronológico
        stamp = f"{r.date}T{r.time}" + ("" if r.time.endswith("Z") else "Z")
        category = "TRADING" if r.type in ("BUY", "SELL") else "CASH"
        w.writerow(
            [
                stamp, r.date, "DEFAULT", category, r.type, r.asset_class, r.name, r.isin,
                r.shares, r.price, r.amount, r.fee, r.tax, r.currency, "", "", "",
                r.description, r.tid, "Persona Inventada", "ES0000000000000000009999", "", "",
            ]
        )  # fmt: skip
    return buf.getvalue().encode()


@dataclass(frozen=True)
class Position:
    isin: str
    name: str
    units: str  # «8,000000»
    price: str  # «55,00»
    value: str  # «440,00»
    wrap: bool = False  # «unidades» en la línea siguiente (nombre largo), como en el real


POSITIONS = [
    Position("XF000FAKE001", "FAKE World ETF (Acc)", "8,000000", "55,00", "440,00"),
    Position("XF000FAKE002", "FAKE Bonds ETF (Dist)", "4,000000", "26,00", "104,00", wrap=True),
    Position("XF000FAKE003", "FAKE Corp", "1,500000", "12,00", "18,00"),
]


def eur(cents: int) -> str:
    return f"{cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def build_networth(
    *,
    as_of: str = "30.09.2026",
    positions: list[Position] = POSITIONS,
    cash_cents: int | None = None,
    securities: str | None = None,
    declared: int | None = None,
) -> bytes:
    if cash_cents is None:
        d, m, y = as_of.split(".")
        cash_cents = cash_at(ROWS, f"{y}-{m}-{d}")
    sec_cents = sum(int(p.value.replace(".", "").replace(",", "")) for p in positions)
    sec = securities or eur(sec_cents)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    top = 60

    def line(text: str) -> None:
        nonlocal top
        c.drawString(60, A4[1] - top, text)
        top += 14

    line("TRADE REPUBLIC BANK GMBH, SUCURSAL EN ESPAÑA C/ CALLE FALSA 1 - 28000 MADRID")
    line("Persona Inventada")
    line(f"FECHA {as_of}")
    line("CUENTA DE VALORES 0000000042")
    line("EXTRACTO DEL PATRIMONIO NETO")
    line(f"a {as_of}")
    line("CARTERA VALOR EN EUR")
    line(f"Cuenta de valores {sec}")
    line(f"Efectivo {eur(cash_cents)}")
    line(f"TOTAL {eur(sec_cents + cash_cents)} EUR")
    line("CUENTA DE VALORES")
    line("UNIDADES / NOMINAL NOMBRE DEL VALOR PRECIO POR UNIDAD VALOR EN EUR")
    for p in positions:
        if p.wrap:
            line(f"{p.units} {p.name} {p.price} {p.value}")
            line(f"unidades ISIN: {p.isin} {as_of}")
        else:
            line(f"{p.units} unidades {p.name} {p.price} {p.value}")
            line(f"ISIN: {p.isin} {as_of}")
        line("Cuenta de valores en custodia")
    n = len(positions) if declared is None else declared
    line(f"NÚMERO DE POSICIONES: {n} {sec} EUR")
    line("Trade Republic Bank GmbH, Sucursal en España www.ejemplo.invalid Página 1 de 1")
    c.showPage()
    c.save()
    return buf.getvalue()
