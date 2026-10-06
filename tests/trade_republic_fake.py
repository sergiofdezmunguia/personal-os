"""Genera PDFs con la maquetación del «Extracto de cuenta» de Trade Republic y datos INVENTADOS.

Reproduce lo que importa al parser: cabecera con IBAN, resumen (PRODUCTO / BALANCE INICIAL…),
tabla con columnas FECHA | TIPO | DESCRIPCIÓN | ENTRADA | SALIDA | BALANCE, movimientos de
varias líneas con la fecha en línea («12 jul») o apilada (día / mes / año), pie de página y
notas legales al final.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

FAKE_IBAN = "DE00000000000000000042"
X_DATE, X_TYPE, X_DESC, X_IN, X_OUT, X_BAL = 74, 101, 149, 420, 450, 490
MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sept", "oct", "nov", "dic"]


@dataclass
class Tx:
    date: str  # YYYY-MM-DD
    kind: tuple[str, str]  # TIPO en dos líneas, p. ej. ("Transacción", "con tarjeta")
    desc: list[str]  # líneas de descripción
    amount: float  # positivo = entrada


def eur(v: float) -> str:
    return f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def build(
    txs: list[Tx],
    *,
    initial: float = 1000.0,
    per_page: int = 6,
    stacked_from_page: int | None = None,
    tamper_balance_at: int | None = None,
    final_override: float | None = None,
) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    height = A4[1]

    def y(top: float) -> float:  # pdfplumber mide desde arriba; reportlab desde abajo
        return height - top

    balances, bal = [], initial
    for t in txs:
        bal = round(bal + t.amount, 2)
        balances.append(bal)
    entradas = sum(t.amount for t in txs if t.amount > 0)
    salidas = -sum(t.amount for t in txs if t.amount < 0)
    final = final_override if final_override is not None else (balances[-1] if txs else initial)

    def header(first: bool) -> float:
        c.setFont("Helvetica", 7)
        c.drawString(74, y(40), "TRADE REPUBLIC BANK GMBH, SUCURSAL EN ESPAÑA C/ CALLE FALSA 1")
        if not first:
            c.drawString(X_DATE, y(157), "FECHA")
            c.drawString(X_TYPE, y(157), "TIPO")
            c.drawString(X_DESC, y(157), "DESCRIPCIÓN")
            c.drawString(X_BAL, y(157), "BALANCE")
            return 175
        c.drawString(74, y(140), "Persona Inventada")
        c.drawString(389, y(140), "FECHA 01 jul 2026 - 30 sept 2026")
        c.drawString(389, y(149), f"IBAN {FAKE_IBAN}")
        c.drawString(74, y(220), "RESUMEN DE ESTADO DE CUENTA")
        c.drawString(74, y(242), "PRODUCTO")
        c.drawString(175, y(242), "BALANCE INICIAL")
        c.drawString(480, y(242), "BALANCE FINAL")
        c.drawString(74, y(256), "Cuenta corriente")
        for x, v in ((175, initial), (280, entradas), (380, salidas), (480, final)):
            c.drawString(x, y(256), f"{eur(v)} €")
        c.drawString(74, y(320), "TRANSACCIONES DE CUENTA")
        c.drawString(X_DATE, y(346), "FECHA")
        c.drawString(X_TYPE, y(346), "TIPO")
        c.drawString(X_DESC, y(346), "DESCRIPCIÓN")
        c.drawString(X_BAL, y(346), "BALANCE")
        return 365

    def footer(page: int, pages: int) -> None:
        c.drawString(74, y(780), "Trade Republic Bank GmbH, Sucursal en España www.ejemplo.invalid")
        c.drawString(74, y(800), f"Creado en 2026-10-05 10:00:00 Página {page} de {pages}")

    chunks = [txs[i : i + per_page] for i in range(0, len(txs), per_page)] or [[]]
    idx = 0
    for p, chunk in enumerate(chunks, start=1):
        top = header(p == 1)
        stacked = stacked_from_page is not None and p >= stacked_from_page
        for t in chunk:
            yy, mm, dd = t.date.split("-")
            month = MONTHS[int(mm) - 1]
            bal = balances[idx] + (1.0 if tamper_balance_at == idx else 0.0)
            money_x = X_IN if t.amount > 0 else X_OUT
            if stacked:
                c.drawString(X_DATE, y(top), dd)
                c.drawString(X_TYPE, y(top + 4), t.kind[0])
                c.drawString(X_DATE, y(top + 8), month)
                c.drawString(X_DESC, y(top + 8), " ".join(t.desc[:1]))
                c.drawString(money_x, y(top + 11), f"{eur(t.amount)} €")
                c.drawString(X_BAL, y(top + 11), f"{eur(bal)} €")
                c.drawString(X_TYPE, y(top + 11), t.kind[1])
                c.drawString(X_DATE, y(top + 15), yy)
                for k, extra in enumerate(t.desc[1:]):
                    c.drawString(X_DESC, y(top + 19 + 4 * k), extra)
                top += 40 + 4 * max(0, len(t.desc) - 2)
            else:
                c.drawString(X_DATE, y(top), f"{dd} {month}")
                c.drawString(X_TYPE, y(top), t.kind[0])
                c.drawString(X_DESC, y(top), t.desc[0])
                c.drawString(money_x, y(top + 12), f"{eur(t.amount)} €")
                c.drawString(X_BAL, y(top + 12), f"{eur(bal)} €")
                c.drawString(X_DATE, y(top + 24), yy)
                c.drawString(X_TYPE, y(top + 24), t.kind[1])
                for k, extra in enumerate(t.desc[1:]):
                    c.drawString(X_DESC, y(top + 12 + 12 * k), extra)
                top += 40 + 12 * max(0, len(t.desc) - 2)
            idx += 1
        if p == len(chunks):
            c.drawString(74, y(top + 30), "NOTAS SOBRE EL EXTRACTO")
            c.drawString(74, y(top + 42), "Por favor, compruebe su extracto de cuenta.")
        footer(p, len(chunks))
        c.showPage()
    c.save()
    return buf.getvalue()


CARD = ("Transacción", "con tarjeta")
SAMPLE = [
    Tx(
        "2026-07-01",
        ("Transferencia", ""),
        ["Ingreso aceptado: ES0000000000000000001234", "a DE00000000000000000042"],
        500.0,
    ),
    Tx("2026-07-02", CARD, ["Supermercado Ficticio"], -23.45),
    Tx("2026-07-02", CARD, ["Supermercado Ficticio"], -23.45),
    Tx(
        "2026-07-05",
        ("Operar", "Savings plan"),
        ["Savings plan execution XF000FAKE001 Fondo Inventado World", "cantidad: 0.123456"],
        -50.0,
    ),
    Tx("2026-07-31", ("Interés", ""), ["Your interest payment"], 1.23),
    Tx(
        "2026-08-03",
        ("Transferencia", ""),
        ["Transferencia Bizum a Amigo Inventado (+34-600000123)"],
        -15.0,
    ),
    Tx("2026-08-10", CARD, ["Cafetería de Prueba", "Tarjeta 4000000000004321"], -3.2),
    Tx("2026-08-15", ("Bonificación", ""), ["Saveback"], 0.5),
    Tx(
        "2026-09-01",
        ("Recibos", "domiciliados"),
        ["Sepa Direct Debit transfer to Compañía Inventada"],
        -9.99,
    ),
    Tx("2026-09-12", CARD, ["Gasolinera Imaginaria"], -40.0),
    Tx(
        "2026-09-30",
        ("Operar", "Savings plan"),
        ["Savings plan execution XF000FAKE002 ETF Ficticio", "cantidad: 0.5"],
        -25.0,
    ),
]
