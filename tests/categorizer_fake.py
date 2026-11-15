"""Movimientos categorizados INVENTADOS para entrenar y evaluar el categorizador (ADR 0017).

Adaptado del generador sintético de `expense-categorizer`: cada categoría tiene varias
«familias» de comercio para que la evaluación con comercios nuevos tenga sentido (un comercio
nuevo se parece a otros de su categoría, pero no es ninguno de ellos), hay comercios ambiguos
(Amazon, PayPal…) y un 3 % de etiquetas mal puestas. Nada sale de datos reales.
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

from personal_os.finance.categorizer import Example
from personal_os.finance.importers import ParsedStatement, ParsedTransaction

# categoría (slugs de Personal OS) -> (prefijos de comercio, importe medio €, signo, peso)
CATALOG = {
    "supermercado": (["MERCADONA", "CARREFOUR EXPRESS", "LIDL", "HIPERDINO", "ALCAMPO", "SPAR"], 35, -1, 12),
    "restaurantes": (["REST", "BAR", "TABERNA", "PIZZERIA", "BURGER", "SUSHI"], 22, -1, 9),
    "transporte-publico": (["TITSA", "GUAGUAS", "TRANVIA", "METROPOLITANO"], 8, -1, 7),
    "taxi-vtc": (["TAXI", "CABIFY", "UBER *TRIP", "FREENOW"], 14, -1, 3),
    "farmacia": (["FARMACIA", "FCIA", "PARAFARMACIA"], 12, -1, 3),
    "suscripciones": (["SPOTIFY", "NETFLIX.COM", "APPLE.COM/BILL", "MICROSOFT*", "ANTHROPIC"], 11, -1, 4),
    "compras": (["ZARA", "PRIMARK", "PULL&BEAR", "H&M", "MANGO", "DECATHLON"], 40, -1, 4),
    "ocio": (["CINES", "TEATRO", "TICKETMASTER", "BOWLING", "CONCIERTO"], 25, -1, 3),
    "vuelos": (["BINTER", "IBERIA", "RYANAIR", "VUELING"], 140, -1, 1),
    "nomina": (["NOMINA", "TRANSFERENCIA NOMINA"], 1000, 1, 1),
    "intereses": (["INTERESES", "ABONO INTERESES"], 9, 1, 1),
    "inversion": (["PLAN DE INVERSION", "COMPRA ETF", "SAVEBACK"], 300, -1, 2),
}  # fmt: skip
NAMES = ["LOPEZ", "GARCIA", "DEL SOL", "SAN JUAN", "LA PLAZA", "CENTRO", "NORTE", "OLIVO",
         "MARINA", "ALCALA", "PRADO", "RIVAS", "LUNA", "TRIANA", "MAYOR", "ROSALES"]  # fmt: skip
AMBIGUOUS = {"AMAZON.ES": ["compras", "ocio", "supermercado"],
             "PAYPAL *": ["compras", "ocio", "suscripciones", "vuelos"],
             "EL CORTE INGLES": ["compras", "supermercado", "ocio"]}  # fmt: skip
LABEL_NOISE = 0.03
CITIES = ["LA LAGUNA", "S/C TENERIFE", "ADEJE", "LAS PALMAS", "PUERTO CRUZ", ""]


def generate(
    n: int = 1500, seed: int = 7, *, label_noise: float = LABEL_NOISE, ambiguous: float = 0.12
) -> list[Example]:
    """`label_noise`: etiquetas al azar; `ambiguous`: compras en comercios genéricos."""
    rng = random.Random(seed)
    cats = list(CATALOG)
    weights = [CATALOG[c][3] for c in cats]
    start = date(2025, 7, 1)
    out = []
    for _ in range(n):
        cat = rng.choices(cats, weights)[0]
        prefixes, mean, sign, _ = CATALOG[cat]
        prefix = rng.choice(prefixes)
        if rng.random() < ambiguous:
            options = [k for k, v in AMBIGUOUS.items() if cat in v]
            if options:
                prefix = rng.choice(options)
        name = rng.choice(NAMES) if rng.random() < 0.6 else ""
        noise = str(rng.randint(1000, 9999)) if rng.random() < 0.5 else ""
        city = rng.choice(CITIES)
        lead = "Compra" if rng.random() < 0.5 else "Pago Movil En"
        tail = ", Tarjeta ····9999" if rng.random() < 0.3 else ""
        desc = " ".join(p for p in [lead, prefix, name, noise, city] if p) + tail
        amount = sign * max(0.5, rng.lognormvariate(math.log(mean), 0.5))
        label = rng.choice(cats) if rng.random() < label_noise else cat
        out.append(
            Example(
                description=desc,
                amount_cents=round(amount * 100),
                booking_date=(start + timedelta(days=rng.randrange(500))).isoformat(),
                account_kind="broker" if cat in ("intereses", "inversion") else "bank",
                category=label,
            )
        )
    return sorted(out, key=lambda x: x.booking_date)


def statement(
    examples: list[Example], *, iban: str = "ES0000000000000000004321"
) -> ParsedStatement:
    """Los ejemplos como un extracto inventado (sin saldo), para importarlos en una base de
    prueba con `FinanceService.import_statement`."""
    txs = [
        ParsedTransaction(x.booking_date, x.booking_date, x.description, x.amount_cents, None)
        for x in sorted(examples, key=lambda x: x.booking_date)
    ]
    return ParsedStatement("fake", "santander", "Cuenta inventada", iban, "EUR", txs)
