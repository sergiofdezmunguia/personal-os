"""Inversiones: importar actividad y valoraciones de Trade Republic, y calcular posiciones,
aportaciones (plan de inversión) y patrimonio neto. ADR 0008.

- La actividad (CSV) trae el efectivo y las operaciones. El efectivo pasa por
  `FinanceService.import_statement` (enlaza lo ya importado del PDF de cuenta sin duplicar);
  compras, ventas y dividendos van a `fin_trades`, deduplicados por id del banco.
- La valoración (PDF del patrimonio neto) es la única fuente del valor de mercado: no se
  consultan precios en internet. Entre valoraciones, las inversiones se cuentan a coste.
- Coste por precio medio ponderado: una venta reduce el coste en proporción a lo vendido.

Las consultas son de solo lectura y deterministas.
"""

from __future__ import annotations

import calendar
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from statistics import median

from personal_os.core.events import ChangeContext
from personal_os.core.ids import new_id
from personal_os.finance.importers import (
    Parsed,
    ParsedActivity,
    ParsedStatement,
    ParsedValuation,
)
from personal_os.finance.models import Account, FinanceError, ImportResult
from personal_os.finance.service import FinanceService

VALUATION_WINDOW_DAYS = 7  # una valoración vale como «a fin de mes» si es de su última semana
PLAN_LOOKBACK_MONTHS = 3  # el plan se deduce de las ejecuciones de los últimos meses completos
PLAN_TOLERANCE = 0.9  # un mes cumple si cada ISIN recibe al menos el 90 % de lo habitual

# --------------------------------------------------------------------------- importar


def import_document(
    svc: FinanceService, ctx: ChangeContext, parsed: Parsed, *, file_name: str, file_sha256: str
) -> ImportResult:
    """Importa cualquier fichero reconocido por `importers.detect`."""
    if isinstance(parsed, ParsedActivity):
        return import_activity(svc, ctx, parsed, file_name=file_name, file_sha256=file_sha256)
    if isinstance(parsed, ParsedValuation):
        return import_valuation(svc, ctx, parsed, file_name=file_name, file_sha256=file_sha256)
    return svc.import_statement(ctx, parsed, file_name=file_name, file_sha256=file_sha256)


def _resolve_account(
    svc: FinanceService, ctx: ChangeContext, institution: str, template: ParsedStatement
) -> Account:
    """Los ficheros de TR sin IBAN van a la cuenta de TR que ya existe (o se crea una)."""
    accounts = [a for a in svc.list_accounts() if a.institution == institution]
    if len(accounts) > 1:
        accounts = [a for a in accounts if a.kind == "broker"]
    if len(accounts) > 1:
        raise FinanceError(
            f"Hay {len(accounts)} cuentas de {institution}: no sé a cuál pertenece el fichero"
        )
    if accounts:
        return accounts[0]
    with svc.db.transaction():
        return svc._ensure_account(ctx, template, svc._now())


def import_activity(
    svc: FinanceService,
    ctx: ChangeContext,
    activity: ParsedActivity,
    *,
    file_name: str,
    file_sha256: str,
) -> ImportResult:
    with svc.db.transaction():
        account = _resolve_account(svc, ctx, activity.institution, activity.cash)
        result = svc.import_statement(
            ctx, activity.cash, file_name=file_name, file_sha256=file_sha256, account_id=account.id
        )
        if result.already_imported:
            return result
        now = svc._now()
        new = 0
        for t in activity.trades:
            cur = svc.db.execute(
                "INSERT OR IGNORE INTO fin_trades(id, account_id, import_id, external_id,"
                " trade_date, executed_at, kind, isin, name, asset_class, shares, price,"
                " amount_cents, fee_cents, tax_cents, savings_plan, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id("trade"),
                    account.id,
                    result.import_id,
                    t.external_id,
                    t.trade_date,
                    t.executed_at,
                    t.kind,
                    t.isin,
                    t.name,
                    t.asset_class,
                    str(t.shares),
                    None if t.price is None else str(t.price),
                    t.amount_cents,
                    t.fee_cents,
                    t.tax_cents,
                    int(t.savings_plan),
                    now,
                ),
            )
            new += cur.rowcount
        svc.events.record(
            ctx,
            "finance.trades.imported",
            "fin_import",
            result.import_id,
            {"account_id": account.id, "trades_new": new, "trades_total": len(activity.trades)},
        )
    return ImportResult(
        import_id=result.import_id,
        account=result.account,
        rows_total=result.rows_total,
        rows_new=result.rows_new,
        rows_duplicate=result.rows_duplicate,
        categorized=result.categorized,
        already_imported=False,
        warnings=list(activity.warnings) + result.warnings,
        rows_linked=result.rows_linked,
        trades_new=new,
    )


def import_valuation(
    svc: FinanceService,
    ctx: ChangeContext,
    valuation: ParsedValuation,
    *,
    file_name: str,
    file_sha256: str,
) -> ImportResult:
    template = ParsedStatement(
        source=valuation.source,
        institution=valuation.institution,
        account_name="Trade Republic · Cuenta",
        account_identifier="",
        currency="EUR",
        transactions=[],
        account_kind="broker",
    )
    now = svc._now()
    n = len(valuation.positions)
    with svc.db.transaction():
        account = _resolve_account(svc, ctx, valuation.institution, template)
        if svc.db.one(
            "SELECT 1 FROM fin_imports WHERE account_id = ? AND file_sha256 = ?",
            (account.id, file_sha256),
        ):
            return ImportResult(
                None, account, n, 0, n, 0, True,
                ["Este fichero ya se había importado; no se ha hecho nada"],
            )  # fmt: skip
        warnings = list(valuation.warnings)
        old = svc.db.one(
            "SELECT id FROM fin_valuations WHERE account_id = ? AND as_of = ?",
            (account.id, valuation.as_of),
        )
        if old is not None:
            svc.db.execute("DELETE FROM fin_valuations WHERE id = ?", (old["id"],))
            warnings.append(f"Sustituye a la valoración anterior del {valuation.as_of}")
        import_id = new_id("import")
        svc.db.execute(
            "INSERT INTO fin_imports(id, account_id, source, file_name, file_sha256,"
            " period_from, period_to, rows_total, rows_new, rows_duplicate,"
            " balance_end_cents, actor, imported_at) VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?)",
            (
                import_id, account.id, valuation.source, file_name, file_sha256,
                valuation.as_of, valuation.as_of, n, n, valuation.cash_cents, ctx.actor, now,
            ),
        )  # fmt: skip
        val_id = new_id("valuation")
        svc.db.execute(
            "INSERT INTO fin_valuations(id, account_id, import_id, as_of, securities_cents,"
            " cash_cents, total_cents, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                val_id, account.id, import_id, valuation.as_of, valuation.securities_cents,
                valuation.cash_cents, valuation.total_cents, now,
            ),
        )  # fmt: skip
        for p in valuation.positions:
            svc.db.execute(
                "INSERT INTO fin_valuation_positions(valuation_id, isin, name, shares, price,"
                " value_cents, price_date) VALUES (?,?,?,?,?,?,?)",
                (val_id, p.isin, p.name, str(p.shares), str(p.price), p.value_cents, p.price_date),
            )
        warnings += _valuation_checks(svc, account.id, valuation)
        svc.events.record(
            ctx,
            "finance.valuation.imported",
            "fin_valuation",
            val_id,
            {"account_id": account.id, "as_of": valuation.as_of, "positions": n},
        )
    return ImportResult(import_id, account, n, n, 0, 0, False, warnings)


def _valuation_checks(svc: FinanceService, account_id: str, v: ParsedValuation) -> list[str]:
    """Contrasta la foto con lo importado: participaciones por ISIN y efectivo."""
    if not svc.db.one("SELECT 1 FROM fin_trades WHERE account_id = ? LIMIT 1", (account_id,)):
        return ["Aún no hay operaciones importadas: importa también la exportación CSV"]
    warnings = []
    held = {h.isin: h.shares for h in holdings(svc, account_id, as_of=v.as_of).values()}
    listed = {p.isin: p.shares for p in v.positions}
    wrong = [
        i
        for i in set(held) | set(listed)
        if abs(held.get(i, Decimal(0)) - listed.get(i, Decimal(0))) > Decimal("0.0001")
    ]
    if wrong:
        warnings.append(
            f"{len(wrong)} posiciones no cuadran con las operaciones importadas "
            "(¿falta una exportación CSV más reciente?)"
        )
    cash = cash_balance(svc, account_id, v.as_of)
    if cash is not None and cash != v.cash_cents:
        warnings.append("El efectivo del extracto no coincide con el saldo de los movimientos")
    return warnings


# --------------------------------------------------------------------------- posiciones


@dataclass
class Holding:
    isin: str
    name: str
    asset_class: str
    shares: Decimal = Decimal(0)
    cost_cents: int = 0  # coste de lo que se tiene (precio medio)
    realized_cents: int = 0  # ganancia materializada en ventas
    dividends_cents: int = 0  # netos de impuestos


def holdings(
    svc: FinanceService, account_id: str, *, as_of: str | None = None, include_closed=False
) -> dict[str, Holding]:
    sql = "SELECT * FROM fin_trades WHERE account_id = ?"
    params: list = [account_id]
    if as_of:
        sql += " AND trade_date <= ?"
        params.append(as_of)
    out: dict[str, Holding] = {}
    for t in svc.db.all(sql + " ORDER BY trade_date, executed_at", params):
        h = out.setdefault(t["isin"], Holding(t["isin"], t["name"], t["asset_class"]))
        cash = t["amount_cents"] + t["fee_cents"] + t["tax_cents"]
        shares = Decimal(t["shares"])
        if t["kind"] == "buy":
            h.shares += shares
            h.cost_cents += -cash
        elif t["kind"] == "sell":
            removed = round(h.cost_cents * (-shares / h.shares)) if h.shares > 0 else 0
            h.cost_cents -= removed
            h.realized_cents += cash - removed
            h.shares += shares
        else:
            h.dividends_cents += cash
    if include_closed:
        return out
    return {k: h for k, h in out.items() if h.shares != 0}


def cash_balance(svc: FinanceService, account_id: str, day: str) -> int | None:
    row = svc.db.one(
        "SELECT balance_after_cents FROM fin_transactions WHERE account_id = ?"
        " AND booking_date <= ? AND balance_after_cents IS NOT NULL"
        " ORDER BY booking_date DESC, rowid DESC LIMIT 1",
        (account_id, day),
    )
    return None if row is None else row["balance_after_cents"]


@dataclass(frozen=True)
class PositionView:
    isin: str
    name: str
    shares: Decimal
    price: Decimal | None
    value_cents: int | None  # None = no está en la valoración
    cost_cents: int

    @property
    def gain_cents(self) -> int | None:
        return None if self.value_cents is None else self.value_cents - self.cost_cents

    @property
    def gain_pct(self) -> float | None:
        if self.value_cents is None or not self.cost_cents:
            return None
        return round(100 * (self.value_cents - self.cost_cents) / self.cost_cents, 2)


@dataclass(frozen=True)
class Portfolio:
    account_id: str
    label: str
    as_of: str | None  # fecha de la última valoración
    positions: list[PositionView]
    value_cents: int | None
    cost_cents: int
    cash_cents: int | None
    invested_after_cents: int  # comprado (neto) después de la valoración, a coste
    dividends_cents: int
    realized_cents: int
    warnings: list[str] = field(default_factory=list)

    @property
    def gain_cents(self) -> int | None:
        return None if self.value_cents is None else self.value_cents - self.cost_cents

    @property
    def gain_pct(self) -> float | None:
        if self.value_cents is None or not self.cost_cents:
            return None
        return round(100 * (self.value_cents - self.cost_cents) / self.cost_cents, 2)


def _latest_valuation(svc: FinanceService, account_id: str, *, until: str | None = None):
    sql, params = "SELECT * FROM fin_valuations WHERE account_id = ?", [account_id]
    if until:
        sql += " AND as_of <= ?"
        params.append(until)
    return svc.db.one(sql + " ORDER BY as_of DESC LIMIT 1", params)


def portfolios(svc: FinanceService) -> list[Portfolio]:
    """Cartera de cada cuenta con operaciones: valor (última valoración) frente a coste."""
    out = []
    for acc in svc.list_accounts():
        if not svc.db.one("SELECT 1 FROM fin_trades WHERE account_id = ? LIMIT 1", (acc.id,)):
            continue
        val = _latest_valuation(svc, acc.id)
        everything = holdings(svc, acc.id, include_closed=True)
        dividends = sum(h.dividends_cents for h in everything.values())
        realized = sum(h.realized_cents for h in everything.values())
        if val is None:
            current = holdings(svc, acc.id)
            positions = [
                PositionView(h.isin, h.name, h.shares, None, None, h.cost_cents)
                for h in current.values()
            ]
            out.append(
                Portfolio(
                    acc.id, acc.label, None, positions, None,
                    sum(p.cost_cents for p in positions), None, 0, dividends, realized,
                    ["Sin valoración: importa el PDF «Extracto del patrimonio neto»"],
                )
            )  # fmt: skip
            continue
        at = holdings(svc, acc.id, as_of=val["as_of"])
        rows = svc.db.all(
            "SELECT * FROM fin_valuation_positions WHERE valuation_id = ? ORDER BY value_cents DESC",
            (val["id"],),
        )
        positions = []
        for r in rows:
            h = at.get(r["isin"])
            positions.append(
                PositionView(
                    r["isin"],
                    h.name if h else r["name"],
                    Decimal(r["shares"]),
                    Decimal(r["price"]),
                    r["value_cents"],
                    h.cost_cents if h else 0,
                )
            )
        after = svc.db.one(
            "SELECT COALESCE(SUM(amount_cents + fee_cents + tax_cents), 0) AS s FROM fin_trades"
            " WHERE account_id = ? AND trade_date > ? AND kind IN ('buy', 'sell')",
            (acc.id, val["as_of"]),
        )["s"]
        out.append(
            Portfolio(
                account_id=acc.id,
                label=acc.label,
                as_of=val["as_of"],
                positions=positions,
                value_cents=val["securities_cents"],
                cost_cents=sum(p.cost_cents for p in positions),
                cash_cents=val["cash_cents"],
                invested_after_cents=-after,
                dividends_cents=dividends,
                realized_cents=realized,
            )
        )
    return out


# --------------------------------------------------------------------------- aportaciones


@dataclass(frozen=True)
class PlanLine:
    isin: str
    name: str
    amount_cents: int  # importe habitual de cada ejecución
    per_month: int  # ejecuciones habituales al mes
    monthly_cents: int  # mediana de lo invertido al mes (incluye ejecuciones extra)


@dataclass(frozen=True)
class MonthContribution:
    month: str
    plan_cents: int  # compras del plan (coste, con comisiones)
    manual_cents: int  # otras compras
    sold_cents: int  # ventas (neto recibido, positivo)
    by_isin: dict[str, int]
    expected_plan_cents: int | None  # según el plan deducido (None si no se evalúa)
    plan_short: list[str]  # ISIN del plan por debajo de lo habitual ese mes
    in_progress: bool  # mes en curso

    @property
    def net_cents(self) -> int:
        return self.plan_cents + self.manual_cents - self.sold_cents

    @property
    def plan_ok(self) -> bool | None:
        """None = no se evalúa (mes en curso o anterior al plan actual)."""
        if self.expected_plan_cents is None or self.in_progress:
            return None
        return not self.plan_short


def _month(day: str) -> str:
    return day[:7]


def _shift(month: str, n: int) -> str:
    y, m = map(int, month.split("-"))
    idx = y * 12 + (m - 1) + n
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def deduce_plan(svc: FinanceService, today: str) -> list[PlanLine]:
    """El plan de inversión según las ejecuciones de los últimos meses completos. Por ISIN:
    importe más repetido, nº de ejecuciones al mes más repetido y la mediana de lo invertido
    al mes. Un ISIN que no se ha ejecutado en al menos dos de esos meses no es plan."""
    current = _month(today)
    first = _shift(current, -PLAN_LOOKBACK_MONTHS)
    rows = svc.db.all(
        "SELECT isin, name, trade_date, -(amount_cents + fee_cents + tax_cents) AS cost"
        " FROM fin_trades WHERE kind = 'buy' AND savings_plan = 1"
        " AND trade_date >= ? AND trade_date < ? ORDER BY trade_date",
        (first + "-01", current + "-01"),
    )
    by_isin: dict[str, list] = defaultdict(list)
    for r in rows:
        by_isin[r["isin"]].append(r)
    plan = []
    for isin, rs in by_isin.items():
        per_month: dict[str, list[int]] = defaultdict(list)
        for r in rs:
            per_month[_month(r["trade_date"])].append(r["cost"])
        if len(per_month) < 2:
            continue
        totals = [sum(per_month.get(_shift(first, i), [])) for i in range(PLAN_LOOKBACK_MONTHS)]
        plan.append(
            PlanLine(
                isin=isin,
                name=rs[-1]["name"],
                amount_cents=Counter(r["cost"] for r in rs).most_common(1)[0][0],
                per_month=Counter(len(v) for v in per_month.values()).most_common(1)[0][0],
                monthly_cents=round(median(totals)),
            )
        )
    return sorted(plan, key=lambda p: -p.monthly_cents)


def contributions(svc: FinanceService, *, months: int, today: str) -> list[MonthContribution]:
    """Compras y ventas por mes. El cumplimiento del plan se evalúa solo en los meses de los
    que se deduce (los últimos completos): antes el plan pudo ser otro."""
    current = _month(today)
    first = _shift(current, -(months - 1))
    plan_from = _shift(current, -PLAN_LOOKBACK_MONTHS)
    plan = deduce_plan(svc, today)
    rows = svc.db.all(
        "SELECT kind, isin, savings_plan, trade_date,"
        " amount_cents + fee_cents + tax_cents AS cash FROM fin_trades"
        " WHERE kind IN ('buy', 'sell') AND trade_date >= ?",
        (first + "-01",),
    )
    out = []
    for i in range(months):
        m = _shift(first, i)
        rs = [r for r in rows if _month(r["trade_date"]) == m]
        by_isin: dict[str, int] = defaultdict(int)
        planned: dict[str, int] = defaultdict(int)
        for r in rs:
            by_isin[r["isin"]] += -r["cash"]
            if r["kind"] == "buy" and r["savings_plan"]:
                planned[r["isin"]] += -r["cash"]
        evaluated = bool(plan) and m >= plan_from
        out.append(
            MonthContribution(
                month=m,
                plan_cents=sum(planned.values()),
                manual_cents=-sum(
                    r["cash"] for r in rs if r["kind"] == "buy" and not r["savings_plan"]
                ),
                sold_cents=sum(r["cash"] for r in rs if r["kind"] == "sell"),
                by_isin=dict(by_isin),
                expected_plan_cents=sum(p.monthly_cents for p in plan) if evaluated else None,
                plan_short=[
                    p.isin
                    for p in plan
                    if evaluated and planned.get(p.isin, 0) < PLAN_TOLERANCE * p.monthly_cents
                ],
                in_progress=m == current,
            )
        )
    return out


# --------------------------------------------------------------------------- patrimonio


@dataclass(frozen=True)
class NetWorthPoint:
    month: str
    day: str  # fecha a la que se calcula (fin de mes, u hoy en el mes en curso)
    cash: dict[str, int]  # efectivo por cuenta (etiqueta → céntimos)
    investments_cents: int
    investments_basis: str  # 'mercado' (valoración de esa semana) | 'coste'
    missing: list[str]  # cuentas sin datos a esa fecha (el total las excluye)

    @property
    def cash_cents(self) -> int:
        return sum(self.cash.values())

    @property
    def total_cents(self) -> int:
        return self.cash_cents + self.investments_cents


def net_worth(svc: FinanceService, *, months: int, today: str) -> list[NetWorthPoint]:
    """Efectivo de todas las cuentas + inversiones, a fin de cada mes. Las inversiones van a
    valor de mercado si hay una valoración de la última semana del mes (más lo comprado
    después, a coste); si no, a coste."""
    current = _month(today)
    accounts = svc.list_accounts()
    out = []
    for i in range(months):
        m = _shift(current, -(months - 1) + i)
        y, mo = map(int, m.split("-"))
        day = today if m == current else date(y, mo, calendar.monthrange(y, mo)[1]).isoformat()
        cash, missing = {}, []
        invest, basis = 0, "coste"
        for acc in accounts:
            bal = cash_balance(svc, acc.id, day)
            if bal is None:
                missing.append(acc.label)
            else:
                cash[acc.label] = bal
            if not svc.db.one(
                "SELECT 1 FROM fin_trades WHERE account_id = ? AND trade_date <= ? LIMIT 1",
                (acc.id, day),
            ):
                continue
            val = _latest_valuation(svc, acc.id, until=day)
            window = (date.fromisoformat(day) - timedelta(days=VALUATION_WINDOW_DAYS)).isoformat()
            if val is not None and val["as_of"] >= window:
                after = svc.db.one(
                    "SELECT COALESCE(SUM(amount_cents + fee_cents + tax_cents), 0) AS s"
                    " FROM fin_trades WHERE account_id = ? AND trade_date > ? AND trade_date <= ?"
                    " AND kind IN ('buy', 'sell')",
                    (acc.id, val["as_of"], day),
                )["s"]
                invest += val["securities_cents"] - after
                basis = "mercado"
            else:
                invest += sum(h.cost_cents for h in holdings(svc, acc.id, as_of=day).values())
        out.append(NetWorthPoint(m, day, cash, invest, basis, missing))
    return out
