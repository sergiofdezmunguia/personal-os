"""Análisis de gasto para el panel: cómo va el mes frente a lo habitual, en qué se gasta,
dónde centrarse, cuánto se ahorra y dónde está el dinero.

Solo lectura y determinista. Definiciones (ADR 0009):

- **Habitual** = media de los últimos `HABIT_MONTHS` meses cerrados (sin el mes en curso).
- **Gasto** = movimientos de categorías de tipo `expense`. Los traspasos e inversión no son
  gasto. Lo que está sin categoría no entra (se avisa).
- **Fijo** = categorías que se pagan cada mes casi igual (`FIXED`: alquiler, suministros,
  suscripciones, seguros). El resto es **variable**: el ritmo del mes se mide solo con él,
  porque el alquiler del día 1 distorsionaría la comparación.
- **Invertido** = compras − ventas de `fin_trades` (incluye comisiones).
- **Ahorro en efectivo** = ingresos − gasto − invertido.
"""

from __future__ import annotations

import calendar
import re
from collections import defaultdict
from dataclasses import dataclass, field

from personal_os.finance import investments
from personal_os.finance.service import FinanceService, normalize

HABIT_MONTHS = 3
TREND_MONTHS = 12
FIXED = {
    "vivienda", "alquiler", "suministros", "electricidad", "agua", "telefono-internet",
    "suscripciones", "seguros-salud",
}  # fmt: skip
CUT_SHARE = 0.20  # recorte propuesto sobre lo habitual en «dónde centrarme»
NOT_ACTIONABLE = {"otros-gastos", "gastos-compartidos", "efectivo", "comisiones"}
SALARY = "nomina"

_PREFIX_RE = re.compile(
    r"^(transaccion con tarjeta|transferencia|operar|bonificacion|recibos domiciliados|"
    r"rendimientos|interes):\s*"
)
_VERB_RE = re.compile(
    r"^(compra internet en|compra|pago movil en|pago movil|transaccion contactless en|"
    r"recibo|transferencia (inmediata )?(a favor de|de|recibida de))\s+"
)


def merchant(description: str) -> str:
    """Nombre legible del comercio a partir del concepto del banco."""
    d = normalize(description)
    d = _PREFIX_RE.sub("", d)
    d = _VERB_RE.sub("", d)
    d = re.sub(r",?\s*tarj(eta|\.).*$", "", d)
    d = re.sub(r",?\s*comision.*$", "", d)
    d = re.sub(r"\s*[·+]+[\s\d·]*", " ", d)
    d = d.split(",")[0].strip(" .-*")
    return (d or "otros")[:40].capitalize()


# --------------------------------------------------------------------------- piezas


def month_of(day: str) -> str:
    return day[:7]


def shift(month: str, n: int) -> str:
    y, m = map(int, month.split("-"))
    idx = y * 12 + (m - 1) + n
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def days_in(month: str) -> int:
    y, m = map(int, month.split("-"))
    return calendar.monthrange(y, m)[1]


@dataclass(frozen=True)
class Movement:
    id: str
    day: str
    description: str
    merchant: str
    amount_cents: int  # negativo = cargo
    category: str | None
    top: str | None  # categoría principal
    kind: str | None  # expense | income | transfer

    @property
    def fixed(self) -> bool:
        return self.category in FIXED or self.top in FIXED


def _movements(svc: FinanceService, first_day: str, last_day: str) -> list[Movement]:
    rows = svc.db.all(
        "SELECT t.id, t.booking_date, t.description, t.amount_cents, t.category,"
        " COALESCE(c.parent, c.slug) AS top, c.kind FROM fin_transactions t"
        " LEFT JOIN fin_categories c ON c.slug = t.category"
        " WHERE t.booking_date BETWEEN ? AND ? ORDER BY t.booking_date, t.rowid",
        (first_day, last_day),
    )
    return [
        Movement(
            r["id"], r["booking_date"], r["description"], merchant(r["description"]),
            r["amount_cents"], r["category"], r["top"], r["kind"],
        )
        for r in rows
    ]  # fmt: skip


@dataclass(frozen=True)
class MonthFlow:
    month: str
    salary_cents: int
    income_cents: int  # todos los ingresos (nómina incluida)
    fixed_cents: int  # gasto fijo (positivo)
    variable_cents: int  # gasto variable (positivo)
    invested_cents: int  # compras − ventas de inversión
    uncategorized: int
    in_progress: bool = False

    @property
    def expense_cents(self) -> int:
        return self.fixed_cents + self.variable_cents

    @property
    def saving_cents(self) -> int:
        """Lo que queda en efectivo: ingresos − gasto − invertido."""
        return self.income_cents - self.expense_cents - self.invested_cents

    @property
    def saving_rate(self) -> float | None:
        if not self.income_cents:
            return None
        return (self.income_cents - self.expense_cents) / self.income_cents


def _flow(svc: FinanceService, month: str, movs: list[Movement], today: str) -> MonthFlow:
    ms = [m for m in movs if month_of(m.day) == month]
    inv = svc.db.one(
        "SELECT COALESCE(SUM(-(amount_cents + fee_cents + tax_cents)), 0) AS s FROM fin_trades"
        " WHERE kind IN ('buy', 'sell') AND trade_date LIKE ?",
        (month + "-%",),
    )["s"]
    return MonthFlow(
        month=month,
        salary_cents=sum(m.amount_cents for m in ms if m.category == SALARY),
        income_cents=sum(m.amount_cents for m in ms if m.kind == "income"),
        fixed_cents=-sum(m.amount_cents for m in ms if m.kind == "expense" and m.fixed),
        variable_cents=-sum(m.amount_cents for m in ms if m.kind == "expense" and not m.fixed),
        invested_cents=inv,
        uncategorized=sum(1 for m in ms if m.category is None),
        in_progress=month == month_of(today),
    )


def _avg(values: list[int]) -> int:
    return round(sum(values) / len(values)) if values else 0


# --------------------------------------------------------------------------- modelo del panel


@dataclass(frozen=True)
class Pace:
    """Cómo va el mes en curso a día `day`."""

    month: str
    day: int
    days: int
    variable_cents: int  # gasto variable del mes hasta hoy
    variable_habit_to_day_cents: int  # lo que sueles llevar a estas alturas
    variable_habit_cents: int  # lo que sueles gastar en variable en el mes entero
    fixed_cents: int  # fijos ya pagados este mes
    fixed_habit_cents: int
    salary_habit_cents: int
    income_habit_cents: int
    expense_habit_cents: int
    cumulative: list[int]  # gasto total acumulado por día (hasta hoy)
    cumulative_habit: list[int]  # gasto total acumulado habitual por día (mes entero)

    @property
    def delta_cents(self) -> int:
        """Positivo = vas por encima del ritmo habitual."""
        return self.variable_cents - self.variable_habit_to_day_cents

    @property
    def projected_cents(self) -> int:
        """Gasto total previsto a fin de mes: fijos (los pagados o los habituales, lo mayor) +
        variable al ritmo actual. Los primeros días es poco fiable."""
        variable = round(self.variable_cents * self.days / self.day) if self.day else 0
        return max(self.fixed_cents, self.fixed_habit_cents) + variable

    @property
    def projected_saving_cents(self) -> int:
        return self.income_habit_cents - self.projected_cents


@dataclass(frozen=True)
class MerchantStat:
    name: str
    per_month: float  # veces al mes (media de los meses habituales)
    monthly_cents: int  # €/mes
    movements: list[Movement]  # de los meses habituales, más recientes primero


@dataclass(frozen=True)
class CategoryStat:
    slug: str
    name: str
    fixed: bool
    monthly_cents: int  # media de los meses habituales
    to_day_cents: int  # este mes, hasta hoy
    habit_to_day_cents: int  # lo habitual a estas alturas del mes
    before_cents: int | None  # media de los meses anteriores a los habituales (tendencia)
    merchants: list[MerchantStat]

    @property
    def trend(self) -> float | None:
        """Variación de lo habitual frente a los meses anteriores (0.25 = +25 %)."""
        if not self.before_cents:
            return None
        return (self.monthly_cents - self.before_cents) / self.before_cents


@dataclass(frozen=True)
class Focus:
    category: CategoryStat
    saving_cents: int  # si se recorta `CUT_SHARE`
    reasons: list[str]


@dataclass(frozen=True)
class Holding:
    label: str
    cents: int
    note: str = ""


@dataclass(frozen=True)
class Panel:
    today: str
    habit_months: list[str]
    pace: Pace
    categories: list[CategoryStat]  # gasto, de mayor a menor
    focus: list[Focus]
    trend: list[MonthFlow]  # últimos meses, el último es el mes en curso
    habit: MonthFlow  # media de los meses habituales (como un mes «tipo»)
    net_worth: list[investments.NetWorthPoint]
    holdings: list[Holding]  # dónde está el dinero hoy
    uncategorized: int  # movimientos sin categoría en los meses analizados
    warnings: list[str] = field(default_factory=list)

    @property
    def expense_habit_cents(self) -> int:
        return self.habit.expense_cents


def build(svc: FinanceService, *, today: str) -> Panel:
    current = month_of(today)
    habit = [shift(current, -i) for i in range(HABIT_MONTHS, 0, -1)]
    first_trend = shift(current, -(TREND_MONTHS - 1))
    before = [shift(current, -i) for i in range(TREND_MONTHS, HABIT_MONTHS, -1)]
    movs = _movements(svc, shift(current, -TREND_MONTHS) + "-01", today)
    names = {c.slug: c.name for c in svc.list_categories()}
    warnings: list[str] = []

    flows = {m: _flow(svc, m, movs, today) for m in {*habit, *before, current}}
    trend = [flows.get(shift(first_trend, i)) or _flow(svc, shift(first_trend, i), movs, today)
             for i in range(TREND_MONTHS)]  # fmt: skip
    hf = [flows[m] for m in habit]
    with_salary = [f.salary_cents for f in hf if f.salary_cents]
    habit_flow = MonthFlow(
        month="habitual",
        salary_cents=_avg(with_salary),
        income_cents=_avg([f.income_cents for f in hf]),
        fixed_cents=_avg([f.fixed_cents for f in hf]),
        variable_cents=_avg([f.variable_cents for f in hf]),
        invested_cents=_avg([f.invested_cents for f in hf]),
        uncategorized=sum(f.uncategorized for f in hf),
    )
    if not any(f.income_cents for f in hf):
        warnings.append("No hay ingresos en los meses de referencia: ¿falta importar extractos?")

    day = int(today[8:10])
    days = days_in(current)
    expense = [m for m in movs if m.kind == "expense"]

    def cumulative(month: str, *, only_variable: bool, until: int) -> list[int]:
        out, total = [], 0
        by_day = defaultdict(int)
        for m in expense:
            if month_of(m.day) == month and (not only_variable or not m.fixed):
                by_day[int(m.day[8:10])] -= m.amount_cents
        for d in range(1, until + 1):
            total += by_day.get(d, 0)
            out.append(total)
        return out

    def habit_curve(*, only_variable: bool) -> list[int]:
        """Media por día de los meses habituales; un mes más corto arrastra su total."""
        curves = []
        for m in habit:
            c = cumulative(m, only_variable=only_variable, until=days_in(m))
            curves.append(c + [c[-1]] * (days - len(c)) if c else [0] * days)
        return [_avg([c[i] for c in curves]) for i in range(days)]

    var_curve = habit_curve(only_variable=True)
    pace = Pace(
        month=current,
        day=day,
        days=days,
        variable_cents=flows[current].variable_cents,
        variable_habit_to_day_cents=var_curve[day - 1],
        variable_habit_cents=habit_flow.variable_cents,
        fixed_cents=flows[current].fixed_cents,
        fixed_habit_cents=habit_flow.fixed_cents,
        salary_habit_cents=habit_flow.salary_cents,
        income_habit_cents=habit_flow.income_cents,
        expense_habit_cents=habit_flow.expense_cents,
        cumulative=cumulative(current, only_variable=False, until=day),
        cumulative_habit=habit_curve(only_variable=False),
    )

    # Categorías principales de gasto.
    categories = []
    tops = {m.top for m in expense if m.top}
    for top in tops:
        in_habit = [m for m in expense if m.top == top and month_of(m.day) in habit]
        monthly = round(-sum(m.amount_cents for m in in_habit) / HABIT_MONTHS)
        now = -sum(m.amount_cents for m in expense if m.top == top and month_of(m.day) == current)
        to_day = []
        for hm in habit:
            limit = f"{hm}-{min(day, days_in(hm)):02d}"
            to_day.append(-sum(m.amount_cents for m in expense
                               if m.top == top and month_of(m.day) == hm and m.day <= limit))  # fmt: skip
        prior = [m for m in expense if m.top == top and month_of(m.day) in before]
        covered = [b for b in before if any(month_of(m.day) == b for m in movs)]
        before_avg = round(-sum(m.amount_cents for m in prior) / len(covered)) if covered else None
        if not monthly and not now:
            continue
        by_merchant: dict[str, list[Movement]] = defaultdict(list)
        for m in in_habit:
            by_merchant[m.merchant].append(m)
        merchants = sorted(
            (
                MerchantStat(
                    name,
                    round(len(ms) / HABIT_MONTHS, 1),
                    round(-sum(x.amount_cents for x in ms) / HABIT_MONTHS),
                    sorted(ms, key=lambda x: x.day, reverse=True),
                )
                for name, ms in by_merchant.items()
            ),
            key=lambda s: -s.monthly_cents,
        )
        categories.append(
            CategoryStat(
                slug=top,
                name=names.get(top, top),
                fixed=top in FIXED,
                monthly_cents=monthly,
                to_day_cents=now,
                habit_to_day_cents=_avg(to_day),
                before_cents=before_avg,
                merchants=merchants,
            )
        )
    categories.sort(key=lambda c: (-c.monthly_cents, -c.to_day_cents))

    focus = []
    actionable = [
        c
        for c in categories
        if not c.fixed and c.slug not in NOT_ACTIONABLE and c.monthly_cents > 0
    ]
    for c in actionable[:3]:
        reasons = []
        visits = sum(m.per_month for m in c.merchants)
        reasons.append(f"{visits:.0f} pagos al mes de media")
        if c.merchants:
            top_m = c.merchants[0]
            share = top_m.monthly_cents / c.monthly_cents if c.monthly_cents else 0
            if share >= 0.25:
                reasons.append(f"el {share:.0%} en {top_m.name}".replace("%", " %"))
        if c.trend is not None and abs(c.trend) >= 0.1:
            word = "más" if c.trend > 0 else "menos"
            reasons.append(
                f"un {abs(c.trend):.0%} {word} que los meses anteriores".replace("%", " %")
            )
        if (
            c.to_day_cents > c.habit_to_day_cents * 1.1
            and c.to_day_cents - c.habit_to_day_cents > 1000
        ):
            reasons.append("este mes vas por encima")
        focus.append(Focus(c, round(c.monthly_cents * CUT_SHARE), reasons))

    # Dónde está el dinero hoy.
    holdings = []
    for acc in svc.list_accounts():
        bal = investments.cash_balance(svc, acc.id, today)
        if bal is not None:
            holdings.append(Holding(f"Efectivo {acc.name.split(' · ')[0]}", bal))
    for p in investments.portfolios(svc):
        if p.value_cents is not None:
            value = p.value_cents + p.invested_after_cents
            holdings.append(
                Holding("Inversiones", value, f"valoración del {p.as_of}" + (
                    " + lo invertido después" if p.invested_after_cents else "")))  # fmt: skip
        else:
            holdings.append(Holding("Inversiones", p.cost_cents, "a coste (sin valoración)"))
    holdings.sort(key=lambda h: -h.cents)

    analysed = {*habit, current}
    uncategorized = sum(1 for m in movs if m.category is None and month_of(m.day) in analysed)
    if uncategorized:
        warnings.append(
            f"{uncategorized} movimientos sin categoría en los meses analizados no cuentan como gasto"
        )
    return Panel(
        today=today,
        habit_months=habit,
        pace=pace,
        categories=categories,
        focus=focus,
        trend=trend,
        habit=habit_flow,
        net_worth=investments.net_worth(svc, months=TREND_MONTHS, today=today),
        holdings=holdings,
        uncategorized=uncategorized,
        warnings=warnings,
    )
