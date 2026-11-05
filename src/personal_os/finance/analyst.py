"""Analista de gastos: cobros recurrentes (suscripciones), subidas de precio, posibles cobros
duplicados y los resúmenes de ayer y de la semana. Solo lectura y determinista (ADR 0011).

- **Recurrente mensual**: el mismo comercio cobra ≥ `MIN_CHARGES` veces, como mucho una vez
  por mes natural, con ~1 mes entre cobros (25–35 días en ≥ 70 % de los intervalos).
- **Recurrente anual**: ≥ 2 cobros a 350–380 días con importe parecido (±20 %).
- **Activa**: el último cobro no es más antiguo que el intervalo + margen.
- **Subida**: con importe estable antes (±3 %), el último cobro es ≥ 3 % y ≥ 0,50 € mayor.
- **Posible duplicado**: dos cargos del mismo comercio, mismo importe y mismo día (≥ 5 €).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import pairwise
from statistics import median

from personal_os.finance.insights import (
    HABIT_MONTHS,
    Movement,
    _movements,
    month_of,
    shift,
)
from personal_os.finance.service import FinanceService

MIN_CHARGES = 3
MONTHLY_GAP = (25, 35)
YEARLY_GAP = (350, 380)
STABLE = 0.03
RISE_MIN_CENTS = 50
DUPLICATE_MIN_CENTS = 500
HISTORY_MONTHS = 15


@dataclass(frozen=True)
class Recurring:
    merchant: str
    category: str | None
    cadence: str  # "mensual" | "anual"
    charges: list[tuple[str, int]]  # (día, céntimos positivos), del más antiguo al último
    active: bool
    next_day: str | None  # próximo cobro previsto
    previous_cents: int | None = None  # importe estable anterior, si ha subido
    first_seen: str = ""

    @property
    def last_cents(self) -> int:
        return self.charges[-1][1]

    @property
    def last_day(self) -> str:
        return self.charges[-1][0]

    @property
    def monthly_cents(self) -> int:
        return self.last_cents if self.cadence == "mensual" else round(self.last_cents / 12)

    @property
    def rise_cents(self) -> int:
        return self.last_cents - self.previous_cents if self.previous_cents else 0

    @property
    def variable(self) -> bool:
        """Importe que cambia cada mes (luz, agua…): no se le buscan subidas."""
        amounts = [c for _, c in self.charges[-4:]]
        mid = median(amounts)
        return any(abs(a - mid) > mid * 0.15 for a in amounts)


@dataclass(frozen=True)
class Duplicate:
    day: str
    merchant: str
    cents: int
    count: int


_MONTHS_RE = re.compile(
    r"\b(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|"
    r"noviembre|diciembre|ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic)\b"
)
_NOISE_RE = re.compile(r"\b(recibo|cuota|mensualidad|pago|factura|ref|n[ºo]?)\b|[^a-z ]")


def series_key(merchant: str) -> str:
    """Clave de agrupación: sin números, meses ni «recibo/cuota», que cambian cada mes."""
    k = _MONTHS_RE.sub(" ", merchant.lower())
    k = _NOISE_RE.sub(" ", k)
    words = k.split()
    return " ".join(words[:3]) or merchant.lower()


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _charges(svc: FinanceService, today: str, months: int) -> list[Movement]:
    first = shift(month_of(today), -months) + "-01"
    return [
        m
        for m in _movements(svc, first, today)
        if m.amount_cents < 0 and m.kind not in ("transfer", "income")
    ]


def _monthly(ms: list[Movement], today: str) -> Recurring | None:
    per_month: dict[str, list[Movement]] = defaultdict(list)
    for m in ms:
        per_month[month_of(m.day)].append(m)
    doubles = sum(len(v) > 1 for v in per_month.values())
    # Algún mes con dos cobros (un recibo devuelto y repetido, un mes adelantado) se tolera.
    if len(per_month) < MIN_CHARGES or doubles > max(1, len(per_month) // 6):
        return None
    if any(len(v) > 2 for v in per_month.values()):
        return None
    charges = [(m.day, -m.amount_cents) for m in sorted(ms, key=lambda x: x.day)]
    if doubles:
        seen: set[str] = set()
        charges = [c for c in charges if not (month_of(c[0]) in seen or seen.add(month_of(c[0])))]
    gaps = [_days(a[0], b[0]) for a, b in pairwise(charges)]
    ok = sum(MONTHLY_GAP[0] <= g <= MONTHLY_GAP[1] for g in gaps)
    if ok < max(2, 0.7 * len(gaps)):
        return None
    # Las mensualidades se parecen: si los importes bailan mucho no es una cuota.
    mid = median(c for _, c in charges)
    if any(c < mid * 0.5 or c > mid * 1.5 for _, c in charges[-3:]):
        return None
    last = charges[-1][0]
    step = round(median(gaps))
    active = _days(last, today) <= step + 10
    previous = None
    before = [c for _, c in charges[:-1]][-3:]
    if len(before) >= 2:
        base = median(before)
        stable = all(abs(c - base) <= base * STABLE for c in before)
        rise = charges[-1][1] - base
        if stable and rise >= RISE_MIN_CENTS and rise >= base * STABLE:
            previous = round(base)
    nxt = (date.fromisoformat(last) + timedelta(days=step)).isoformat() if active else None
    return Recurring(
        merchant=ms[-1].merchant,
        category=ms[-1].category,
        cadence="mensual",
        charges=charges,
        active=active,
        next_day=nxt,
        previous_cents=previous,
        first_seen=charges[0][0],
    )


def _yearly(ms: list[Movement], today: str) -> Recurring | None:
    charges = [(m.day, -m.amount_cents) for m in sorted(ms, key=lambda x: x.day)]
    for (d1, c1), (d2, c2) in pairwise(charges):
        if YEARLY_GAP[0] <= _days(d1, d2) <= YEARLY_GAP[1] and abs(c2 - c1) <= c1 * 0.2:
            yearly = [(d1, c1), (d2, c2)]
            active = _days(d2, today) <= YEARLY_GAP[1] + 20
            nxt = (date.fromisoformat(d2) + timedelta(days=365)).isoformat() if active else None
            prev = c1 if c2 - c1 >= RISE_MIN_CENTS and c2 - c1 >= c1 * STABLE else None
            return Recurring(ms[0].merchant, ms[-1].category, "anual", yearly, active, nxt,
                             prev, d1)  # fmt: skip
    return None


def recurring(
    svc: FinanceService, *, today: str, include_inactive: bool = False
) -> list[Recurring]:
    """Cobros recurrentes detectados, de mayor a menor coste mensual."""
    groups: dict[str, list[Movement]] = defaultdict(list)
    for m in _charges(svc, today, HISTORY_MONTHS):
        if "sin concepto" in m.description.lower():
            continue
        groups[series_key(m.merchant)].append(m)
    out = []
    for ms in groups.values():
        r = _monthly(ms, today) or _yearly(ms, today)
        found = [r] if r else []
        if not r:
            # Varias cuotas del mismo comercio (p. ej. dos suscripciones de Apple): por importe.
            by_amount: dict[int, list[Movement]] = defaultdict(list)
            for m in ms:
                by_amount[m.amount_cents].append(m)
            found = [x for sub in by_amount.values() if len(sub) < len(ms)
                     for x in [_monthly(sub, today)] if x]  # fmt: skip
        out += [x for x in found if x.active or include_inactive]
    out.sort(key=lambda r: (-r.active, -r.monthly_cents, r.merchant))
    return out


def duplicates(svc: FinanceService, *, today: str, days: int = 7) -> list[Duplicate]:
    """Posibles cobros duplicados en los últimos `days` días."""
    since = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
    seen: dict[tuple[str, str, int], int] = defaultdict(int)
    for m in _charges(svc, today, 1):
        if m.day >= since and -m.amount_cents >= DUPLICATE_MIN_CENTS:
            seen[(m.day, m.merchant, -m.amount_cents)] += 1
    return [Duplicate(d, mer, c, n) for (d, mer, c), n in sorted(seen.items()) if n > 1]


# --------------------------------------------------------------------------- resúmenes


@dataclass(frozen=True)
class Spend:
    """Gasto de un periodo: solo categorías de gasto; lo sin categoría se cuenta aparte."""

    first_day: str
    last_day: str
    expense_cents: int
    payments: int
    top: list[tuple[str, int]]  # (comercio, céntimos), los mayores
    by_category: list[tuple[str, int]]  # (categoría principal, céntimos)
    uncategorized_cents: int
    uncategorized: int


def spend(svc: FinanceService, first_day: str, last_day: str) -> Spend:
    names = {c.slug: c.name for c in svc.list_categories()}
    ms = [m for m in _movements(svc, first_day, last_day) if m.amount_cents < 0]
    exp = [m for m in ms if m.kind == "expense"]
    unc = [m for m in ms if m.category is None]
    by_merchant: dict[str, int] = defaultdict(int)
    by_cat: dict[str, int] = defaultdict(int)
    for m in exp:
        by_merchant[m.merchant] -= m.amount_cents
        by_cat[names.get(m.top or "", m.top or "otros")] -= m.amount_cents
    top = sorted(by_merchant.items(), key=lambda x: -x[1])[:3]
    return Spend(
        first_day,
        last_day,
        -sum(m.amount_cents for m in exp),
        len(exp),
        top,
        sorted(by_cat.items(), key=lambda x: -x[1]),
        -sum(m.amount_cents for m in unc),
        len(unc),
    )


@dataclass(frozen=True)
class Week:
    spend: Spend
    habit_cents: int  # gasto semanal habitual (media de las `HABIT_MONTHS` × 13 semanas previas)
    rises: list[Recurring] = field(default_factory=list)
    new_recurring: list[Recurring] = field(default_factory=list)


def week(svc: FinanceService, *, last_day: str) -> Week:
    """La semana que termina en `last_day` (7 días) frente a las semanas anteriores."""
    end = date.fromisoformat(last_day)
    first = (end - timedelta(days=6)).isoformat()
    prior_first = (end - timedelta(days=7 + 7 * 4 * HABIT_MONTHS)).isoformat()
    prior_last = (end - timedelta(days=7)).isoformat()
    prior = spend(svc, prior_first, prior_last)
    weeks = 4 * HABIT_MONTHS
    rec = recurring(svc, today=last_day)
    return Week(
        spend=spend(svc, first, last_day),
        habit_cents=round(prior.expense_cents / weeks),
        rises=[r for r in rec if r.previous_cents and r.last_day >= first],
        new_recurring=[
            r
            for r in rec
            if r.cadence == "mensual" and len(r.charges) == MIN_CHARGES and r.last_day >= first
        ],
    )
