"""Genera .xls con la estructura del extracto de Santander y datos INVENTADOS.

Nunca se usan extractos reales en los tests ni en el repositorio.
"""

from __future__ import annotations

import io

import xlwt

FAKE_IBAN = "ES0000000000000000001234"
FAKE_HOLDER = "TITULAR DE PRUEBA"


def build(
    rows: list[tuple[str, str, str, float, float]],
    *,
    header_balance: str | None = None,
    iban: str = FAKE_IBAN,
) -> bytes:
    """rows: (fecha_op dd/mm/aaaa, fecha_valor, concepto, importe, saldo), del más ANTIGUO al
    más reciente (el fichero se escribe al revés, como lo da el banco)."""
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Movimientos")
    newest_balance = rows[-1][4] if rows else 0.0
    shown = (
        header_balance
        or f"{newest_balance:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " EUR"
    )
    ws.write(0, 2, "CUENTA ONLINE SANTANDER")
    ws.write(0, 3, "FECHA")
    ws.write(1, 2, iban)
    ws.write(1, 3, "05/10/2026 10:00:00")
    ws.write(2, 2, "Titular")
    ws.write(2, 3, "Saldo")
    ws.write(3, 2, FAKE_HOLDER)
    ws.write(3, 3, shown)
    ws.write(5, 0, "Movimientos")
    for c, h in enumerate(["FECHA OPERACIÓN", "FECHA VALOR", "CONCEPTO", "IMPORTE EUR", "SALDO"]):
        ws.write(7, c, h)
    for i, row in enumerate(reversed(rows)):
        for c, v in enumerate(row):
            ws.write(8 + i, c, v)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def chain(
    start: float, items: list[tuple[str, str, float]]
) -> list[tuple[str, str, str, float, float]]:
    """[(fecha, concepto, importe)] → filas con saldo encadenado desde `start`."""
    out, bal = [], start
    for date, concept, amount in items:
        bal = round(bal + amount, 2)
        out.append((date, date, concept, amount, bal))
    return out


SEPTEMBER = chain(
    1000.00,
    [
        ("01/09/2026", "Transferencia recibida de EMPRESA FICTICIA SL Concepto Nomina", 1500.00),
        (
            "02/09/2026",
            "Compra Mercadona Ficticia, Ciudad, Tarjeta 4000000000009999 , Comision 0,00",
            -45.30,
        ),
        ("03/09/2026", "Compra Café Inventado, Ciudad, Tarjeta 1234XXXXXXXX9999", -3.20),
        ("03/09/2026", "Compra Café Inventado, Ciudad, Tarjeta 1234XXXXXXXX9999", -3.20),
        ("05/09/2026", "Pago Movil En Gasolinera Falsa, Ciudad", -60.00),
        ("10/09/2026", "Transferencia a favor de TRADE REPUBLIC BANK GMBH", -200.00),
        ("15/09/2026", "Retirada de efectivo en cajero", -50.00),
        ("28/09/2026", "Recibo Electrica Imaginaria SA", -72.15),
    ],
)
OCTOBER = chain(
    SEPTEMBER[-1][4],
    [
        ("01/10/2026", "Transferencia recibida de EMPRESA FICTICIA SL Concepto Nomina", 1500.00),
        ("02/10/2026", "Compra MERCADONA FICTICIA, Ciudad, Tarjeta 1234XXXXXXXX9999", -61.10),
    ],
)
