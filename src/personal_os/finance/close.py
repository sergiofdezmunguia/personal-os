"""Cierre mensual: ¿está el mes completo y categorizado?, ¿cómo va frente al anterior? y
¿cuadra lo que ha pagado la cuenta remunerada (intereses y saveback)?

Solo lectura y determinista. Lo usan `pos finance close`, el MCP y la skill `monthly-close`.

Remuneración del efectivo (`CashYield`): los intereses se calculan sobre el saldo de cada día
y se abonan a primeros del mes siguiente; el saveback (un % de lo pagado con tarjeta) también
llega al mes siguiente. Por eso lo «recibido» por el mes M se busca en M+1. Son estimaciones:
el banco puede aplicar retenciones, topes o exclusiones que aquí no se ven.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

from personal_os.finance.models import MonthSummary
from personal_os.finance.service import FinanceService, normalize

INTEREST_CATEGORY = "intereses"
CASHBACK_CATEGORY = "bonificaciones"
CARD_MARKERS = ("con tarjeta", "card transaction")
PAYOUT_DAYS = 10  # los intereses del mes M se abonan en los primeros días de M+1


@dataclass(frozen=True)
class CashYield:
    institution: str
    interest_rate: float  # % anual
    saveback_rate: float  # %
    saveback_cap_cents: int | None = None


@dataclass(frozen=True)
class AccountCoverage:
    account_id: str
    label: str
    last_transaction: str | None
    last_import_at: str | None
    complete: bool  # hay movimientos hasta fin de mes o se importó después de que acabara


@dataclass(frozen=True)
class CategoryDelta:
    slug: str | None
    name: str
    total_cents: int
    previous_cents: int

    @property
    def delta_cents(self) -> int:
        return self.total_cents - self.previous_cents


@dataclass(frozen=True)
class YieldCheck:
    account_id: str
    label: str
    average_balance_cents: int
    interest_rate: float
    interest_expected_cents: int  # bruto, al tipo configurado
    interest_received_cents: int | None  # None = aún no abonado / no importado
    card_spend_cents: int
    saveback_rate: float
    saveback_expected_cents: int
    saveback_received_cents: int | None

    @staticmethod
    def _ratio(received, expected) -> float | None:
        if received is None or not expected:
            return None
        return round(received / expected, 3)

    @property
    def interest_ratio(self) -> float | None:
        return self._ratio(self.interest_received_cents, self.interest_expected_cents)

    @property
    def saveback_ratio(self) -> float | None:
        return self._ratio(self.saveback_received_cents, self.saveback_expected_cents)


@dataclass(frozen=True)
class MonthClose:
    month: str
    summary: MonthSummary
    previous: MonthSummary
    coverage: list[AccountCoverage]
    deltas: list[CategoryDelta]  # gasto por categoría principal frente al mes anterior
    yields: list[YieldCheck] = field(default_factory=list)
    by_claude: int = 0  # movimientos del mes categorizados por Claude (revisables)

    @property
    def complete(self) -> bool:
        return all(c.complete for c in self.coverage)

    @property
    def ready(self) -> bool:
        """Mes cerrable: extractos completos y nada sin categoría."""
        return self.complete and self.summary.uncategorized == 0


def _month_bounds(month: str) -> tuple[date, date]:
    y, m = map(int, month.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def _shift(month: str, n: int) -> str:
    y, m = map(int, month.split("-"))
    idx = y * 12 + (m - 1) + n
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _is_card(description: str) -> bool:
    text = normalize(description)
    return any(m in text for m in CARD_MARKERS)


def month_close(
    svc: FinanceService, month: str, *, yields: tuple[CashYield, ...] = ()
) -> MonthClose:
    svc._check_month(month)
    start, end = _month_bounds(month)
    prev_month = _shift(month, -1)
    summary = svc.month_summary(month)
    previous = svc.month_summary(prev_month)

    coverage = []
    for acc in svc.list_accounts():
        row = svc.db.one(
            "SELECT MAX(booking_date) AS last_tx FROM fin_transactions WHERE account_id = ?",
            (acc.id,),
        )
        imp = svc.db.one(
            "SELECT MAX(imported_at) AS last_imp FROM fin_imports WHERE account_id = ?",
            (acc.id,),
        )
        last_tx, last_imp = row["last_tx"], imp["last_imp"]
        complete = bool(
            (last_tx and last_tx >= end.isoformat())
            or (last_imp and last_imp[:10] > end.isoformat())
        )
        coverage.append(AccountCoverage(acc.id, acc.label, last_tx, last_imp, complete))

    prev_by = {c.slug: c.total_cents for c in previous.by_category if c.kind == "expense"}
    cur_by = {c.slug: c for c in summary.by_category if c.kind == "expense"}
    names = {c.slug: c.name for c in previous.by_category}
    deltas = sorted(
        (
            CategoryDelta(
                slug,
                cur_by[slug].name if slug in cur_by else names.get(slug, slug or "?"),
                cur_by[slug].total_cents if slug in cur_by else 0,
                prev_by.get(slug, 0),
            )
            for slug in set(cur_by) | set(prev_by)
        ),
        key=lambda d: d.delta_cents,
    )

    checks = []
    for y in yields:
        for acc in svc.list_accounts():
            if acc.institution == y.institution:
                checks.append(_yield_check(svc, acc, y, month, start, end))

    by_claude = len(svc.list_transactions(month=month, by_claude=True, limit=None))
    return MonthClose(month, summary, previous, coverage, deltas, checks, by_claude)


def _yield_check(svc: FinanceService, acc, y: CashYield, month, start, end) -> YieldCheck:
    rows = svc.db.all(
        "SELECT booking_date, amount_cents, balance_after_cents, description, category"
        " FROM fin_transactions WHERE account_id = ? AND booking_date <= ?"
        " ORDER BY booking_date, rowid",
        (acc.id, (end + timedelta(days=31)).isoformat()),
    )
    # Saldo al cierre de cada día (arrastrando el último conocido).
    end_of_day: dict[str, int] = {}
    opening = None
    for r in rows:
        if r["balance_after_cents"] is None:
            continue
        if r["booking_date"] < start.isoformat():
            opening = r["balance_after_cents"]
        elif r["booking_date"] <= end.isoformat():
            if opening is None and not end_of_day:
                opening = r["balance_after_cents"] - r["amount_cents"]
            end_of_day[r["booking_date"]] = r["balance_after_cents"]
    balance, total, days = opening or 0, 0, 0
    d = start
    while d <= end:
        balance = end_of_day.get(d.isoformat(), balance)
        total += balance
        days += 1
        d += timedelta(days=1)
    average = round(total / days) if days else 0
    interest_expected = round(total * y.interest_rate / 100 / 365)

    in_month = [r for r in rows if r["booking_date"][:7] == month]
    spend = -sum(r["amount_cents"] for r in in_month if _is_card(r["description"]))
    spend = max(spend, 0)
    saveback_expected = round(spend * y.saveback_rate / 100)
    if y.saveback_cap_cents is not None:
        saveback_expected = min(saveback_expected, y.saveback_cap_cents)

    # Lo abonado por el mes M llega en M+1. Si aún no hay movimientos posteriores al día de
    # abono, no se puede saber si falta: queda como pendiente (None).
    next_month = _shift(month, 1)
    payout_until = f"{next_month}-{PAYOUT_DAYS:02d}"
    after = [r for r in rows if r["booking_date"][:7] == next_month]
    known_until = after[-1]["booking_date"] if after else None
    interest = [
        r["amount_cents"]
        for r in after
        if r["category"] == INTEREST_CATEGORY and r["booking_date"] <= payout_until
    ]
    cashback = [r["amount_cents"] for r in after if r["category"] == CASHBACK_CATEGORY]
    settled = known_until is not None and known_until > payout_until
    interest_received = sum(interest) if interest else (0 if settled else None)
    next_end = _month_bounds(next_month)[1].isoformat()
    month_seen = known_until is not None and known_until >= next_end
    saveback_received = sum(cashback) if cashback else (0 if month_seen else None)
    return YieldCheck(
        account_id=acc.id,
        label=acc.label,
        average_balance_cents=average,
        interest_rate=y.interest_rate,
        interest_expected_cents=interest_expected,
        interest_received_cents=interest_received,
        card_spend_cents=spend,
        saveback_rate=y.saveback_rate,
        saveback_expected_cents=saveback_expected,
        saveback_received_cents=saveback_received,
    )
