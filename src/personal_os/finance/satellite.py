"""Cartera satélite por reglas (ADR 0014): cuánto dinero se puede usar, ideas de compra y
venta sobre la lista de seguimiento, impuestos del año y comparación con el índice.

Solo lectura y determinista. Son **ideas por reglas**, no recomendaciones: las órdenes las da
el usuario en su bróker. Reglas (porcentajes en la configuración `[finance.satellite]`):

- **Presupuesto**: tope = `share` % del patrimonio. Disponible = min(tope − lo ya invertido en
  el satélite, efectivo − `emergency_months` meses de gasto habitual − el plan del mes).
- **Compra**: caída ≥ `dip` % desde el máximo de 52 semanas. «Tendencia sana» si el precio
  sigue por encima de la media de 200 sesiones o esta sube; si no, solo «vigilar» (sin
  importe). Importe = min(disponible, `max_trade` % del tope). Stop a −`stop` %; objetivo:
  volver a máximos (como mucho +`take_profit` %).
- **Venta** (posiciones del satélite): ganancia ≥ `take_profit` % o pérdida ≥ `stop` %.
- Un ISIN que está en el plan de inversión periódico es «núcleo», nunca satélite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from personal_os.finance import investments
from personal_os.finance.service import FinanceService

# IRPF, base del ahorro (España): (hasta, tipo).
TAX_BRACKETS = [(600000, 0.19), (5000000, 0.21), (20000000, 0.23), (30000000, 0.27),
                (None, 0.30)]  # fmt: skip
WASH_DAYS = 61  # regla de los dos meses (art. 33.5 LIRPF)
MIN_POSITION_CENTS = 5000  # posiciones testimoniales (p. ej. compras de prueba): sin ideas de venta
MIN_TRADE_CENTS = 5000  # por debajo, comprar no compensa la comisión


# --------------------------------------------------------------------------- precios


def store_history(svc: FinanceService, symbol: str, currency: str,
                  closes: list[tuple[str, Decimal]], *, fetched_at: str) -> int:  # fmt: skip
    new = 0
    with svc.db.transaction():
        for day, close in closes:
            cur = svc.db.execute(
                "INSERT INTO fin_market_prices(symbol, price_date, close, currency, fetched_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(symbol, price_date) DO UPDATE SET"
                " close = excluded.close, fetched_at = excluded.fetched_at"
                " WHERE fin_market_prices.close <> excluded.close",
                (symbol, day, str(close), currency, fetched_at),
            )
            new += cur.rowcount
    return new


def closes(
    svc: FinanceService, symbol: str, *, until: str, days: int = 400
) -> list[tuple[str, Decimal]]:
    since = (date.fromisoformat(until) - timedelta(days=days)).isoformat()
    rows = svc.db.all(
        "SELECT price_date, close FROM fin_market_prices WHERE symbol = ? AND currency = 'EUR'"
        " AND price_date BETWEEN ? AND ? ORDER BY price_date",
        (symbol, since, until),
    )
    return [(r["price_date"], Decimal(r["close"])) for r in rows]


def close_on(svc: FinanceService, symbol: str, day: str) -> Decimal | None:
    row = svc.db.one(
        "SELECT close FROM fin_market_prices WHERE symbol = ? AND price_date <= ?"
        " ORDER BY price_date DESC LIMIT 1",
        (symbol, day),
    )
    return Decimal(row["close"]) if row else None


def history_start(svc: FinanceService, symbol: str) -> str | None:
    return svc.db.one(
        "SELECT MIN(price_date) AS d FROM fin_market_prices WHERE symbol = ?", (symbol,)
    )["d"]


# --------------------------------------------------------------------------- señales


@dataclass(frozen=True)
class Signal:
    isin: str
    symbol: str
    name: str
    day: str
    price: Decimal
    high_52w: Decimal
    sma50: Decimal | None
    sma200: Decimal | None
    sma200_rising: bool | None
    rsi14: float | None
    change_1m: float | None  # %

    @property
    def drawdown(self) -> float:
        """% por debajo del máximo de 52 semanas."""
        return float(100 * (1 - self.price / self.high_52w)) if self.high_52w else 0.0

    @property
    def healthy_trend(self) -> bool:
        if self.sma200 is None:
            return False
        return self.price >= self.sma200 or bool(self.sma200_rising)


def _sma(values: list[Decimal], n: int) -> Decimal | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def _rsi(values: list[Decimal], n: int = 14) -> float | None:
    if len(values) <= n:
        return None
    gains = losses = Decimal(0)
    for a, b in zip(values[-n - 1 : -1], values[-n:], strict=True):
        d = b - a
        gains += max(d, Decimal(0))
        losses += max(-d, Decimal(0))
    if losses == 0:
        return 100.0
    rs = gains / losses
    return round(float(100 - 100 / (1 + rs)), 1)


def signal(svc: FinanceService, item, *, today: str) -> Signal | None:
    data = closes(svc, item.symbol, until=today)
    if len(data) < 20:
        return None
    last_year = (date.fromisoformat(data[-1][0]) - timedelta(days=365)).isoformat()
    year = [c for d, c in data if d >= last_year]
    vals = [c for _, c in data]
    sma200 = _sma(vals, 200)
    prev200 = _sma(vals[:-20], 200) if len(vals) >= 220 else None
    month_ago = close_on(
        svc, item.symbol, (date.fromisoformat(data[-1][0]) - timedelta(days=30)).isoformat()
    )
    return Signal(
        isin=item.isin, symbol=item.symbol, name=item.name, day=data[-1][0], price=vals[-1],
        high_52w=max(year), sma50=_sma(vals, 50), sma200=sma200,
        sma200_rising=None if prev200 is None or sma200 is None else sma200 > prev200,
        rsi14=_rsi(vals),
        change_1m=float(100 * (vals[-1] / month_ago - 1)) if month_ago else None,
    )  # fmt: skip


# --------------------------------------------------------------------------- presupuesto


@dataclass(frozen=True)
class Budget:
    cash_cents: int
    emergency_cents: int
    plan_cents: int
    net_worth_cents: int
    cap_cents: int  # tope del satélite
    invested_cents: int  # coste de lo que hay en el satélite
    per_trade_cents: int

    @property
    def free_cash_cents(self) -> int:
        return self.cash_cents - self.emergency_cents - self.plan_cents

    @property
    def available_cents(self) -> int:
        return max(0, min(self.cap_cents - self.invested_cents, self.free_cash_cents))

    @property
    def why_zero(self) -> str | None:
        if self.available_cents > 0:
            return None
        if self.free_cash_cents <= 0:
            return ("Tu efectivo no cubre el colchón de emergencia más el plan del mes: primero "
                    "el colchón.")  # fmt: skip
        return "El satélite ya está en su tope."


@dataclass(frozen=True)
class Position:
    isin: str
    name: str
    symbol: str | None
    shares: Decimal
    cost_cents: int
    value_cents: int | None

    @property
    def gain_pct(self) -> float | None:
        if self.value_cents is None or not self.cost_cents:
            return None
        return 100 * (self.value_cents - self.cost_cents) / self.cost_cents


def _cents(x: Decimal) -> int:
    return int((x * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def core_isins(svc: FinanceService, today: str) -> set[str]:
    return {p.isin for p in investments.deduce_plan(svc, today)}


def positions(svc: FinanceService, cfg, *, today: str) -> list[Position]:
    """Posiciones abiertas de la lista de seguimiento que no son del plan (núcleo)."""
    watch = {w.isin: w for w in cfg.watch}
    core = core_isins(svc, today)
    out = []
    for acc in svc.list_accounts():
        for isin, h in investments.holdings(svc, acc.id).items():
            if h.shares <= 0 or isin not in watch or isin in core:
                continue
            w = watch[isin]
            price = close_on(svc, w.symbol, today)
            out.append(Position(isin, w.name, w.symbol, h.shares, h.cost_cents,
                                _cents(h.shares * price) if price else None))  # fmt: skip
    return out


def budget(svc: FinanceService, cfg, *, today: str, monthly_expense_cents: int) -> Budget:
    cash = 0
    for acc in svc.list_accounts():
        bal = investments.cash_balance(svc, acc.id, today)
        cash += bal or 0
    live = sum(
        lp.value_cents for lp in investments.live_portfolios(svc, today=today) if lp.complete
    )
    plan = sum(p.monthly_cents for p in investments.deduce_plan(svc, today))
    net = cash + live
    cap = round(net * cfg.share / 100)
    invested = sum(p.cost_cents for p in positions(svc, cfg, today=today))
    return Budget(
        cash_cents=cash,
        emergency_cents=round(monthly_expense_cents * cfg.emergency_months),
        plan_cents=plan,
        net_worth_cents=net,
        cap_cents=cap,
        invested_cents=invested,
        per_trade_cents=round(cap * cfg.max_trade / 100),
    )


# --------------------------------------------------------------------------- ideas


@dataclass(frozen=True)
class Idea:
    kind: str  # "comprar" | "vigilar" | "vender"
    isin: str
    name: str
    symbol: str
    price: Decimal
    reason: str
    amount_cents: int = 0  # importe propuesto (compra) o valor de la posición (venta)
    stop: Decimal | None = None
    target: Decimal | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Taxes:
    year: int
    realized_cents: int
    dividends_cents: int

    @property
    def base_cents(self) -> int:
        return self.realized_cents + self.dividends_cents

    @property
    def tax_cents(self) -> int:
        return tax_on(self.base_cents)


def tax_on(base_cents: int) -> int:
    """Cuota de la base del ahorro (sin compensaciones de años anteriores)."""
    if base_cents <= 0:
        return 0
    tax, prev = 0.0, 0
    for limit, rate in TAX_BRACKETS:
        top = base_cents if limit is None else min(base_cents, limit)
        if top > prev:
            tax += (top - prev) * rate
        if limit is None or base_cents <= limit:
            break
        prev = limit
    return round(tax)


def _realized(svc: FinanceService, as_of: str) -> tuple[int, int]:
    r = d = 0
    for acc in svc.list_accounts():
        for h in investments.holdings(svc, acc.id, as_of=as_of, include_closed=True).values():
            r += h.realized_cents
            d += h.dividends_cents
    return r, d


def taxes(svc: FinanceService, *, today: str) -> Taxes:
    year = int(today[:4])
    r1, d1 = _realized(svc, today)
    r0, d0 = _realized(svc, f"{year - 1}-12-31")
    return Taxes(year, r1 - r0, d1 - d0)


def recent_loss_sales(svc: FinanceService, *, today: str) -> dict[str, str]:
    """ISIN vendidos con pérdida en los últimos dos meses → día de la venta."""
    since = (date.fromisoformat(today) - timedelta(days=WASH_DAYS)).isoformat()
    out: dict[str, str] = {}
    for t in svc.db.all(
        "SELECT account_id, isin, trade_date FROM fin_trades WHERE kind = 'sell'"
        " AND trade_date >= ? ORDER BY trade_date",
        (since,),
    ):
        before = investments.holdings(svc, t["account_id"], as_of=(
            date.fromisoformat(t["trade_date"]) - timedelta(days=1)).isoformat(), include_closed=True)  # fmt: skip
        after = investments.holdings(
            svc, t["account_id"], as_of=t["trade_date"], include_closed=True
        )
        b = before.get(t["isin"])
        a = after.get(t["isin"])
        if a and (a.realized_cents - (b.realized_cents if b else 0)) < 0:
            out[t["isin"]] = t["trade_date"]
    return out


def ideas(
    svc: FinanceService, cfg, *, today: str, budget_: Budget
) -> tuple[list[Idea], list[Signal]]:
    sigs = [s for w in cfg.watch if (s := signal(svc, w, today=today))]
    held = {p.isin: p for p in positions(svc, cfg, today=today)}
    core = core_isins(svc, today)
    losses = recent_loss_sales(svc, today=today)
    out: list[Idea] = []
    for p in held.values():
        g = p.gain_pct
        if g is None or p.cost_cents < MIN_POSITION_CENTS:
            continue
        price = close_on(svc, p.symbol, today) if p.symbol else None
        if g >= cfg.take_profit:
            out.append(Idea("vender", p.isin, p.name, p.symbol, price, f"Ganas un {g:.0f} %: objetivo alcanzado.", p.value_cents))  # fmt: skip
        elif g <= -cfg.stop:
            out.append(Idea("vender", p.isin, p.name, p.symbol, price, f"Pierdes un {-g:.0f} %: stop alcanzado.", p.value_cents))  # fmt: skip
    room = budget_.available_cents
    for s in sorted(sigs, key=lambda s: -s.drawdown):
        if s.isin in core or s.drawdown < cfg.dip:
            continue
        warn = []
        if s.isin in losses:
            until = (date.fromisoformat(losses[s.isin]) + timedelta(days=WASH_DAYS)).isoformat()
            warn.append(
                f"Lo vendiste con pérdidas el {losses[s.isin]}: si recompras antes del {until}, esa pérdida no se compensa este año."
            )
        if s.isin in held and held[s.isin].cost_cents >= MIN_POSITION_CENTS:
            warn.append("Ya tienes posición en el satélite.")
        reason = f"−{s.drawdown:.0f} % desde su máximo de 52 semanas"
        if s.rsi14 is not None:
            reason += f" · RSI {s.rsi14:.0f}"
        stop = (s.price * Decimal(1 - cfg.stop / 100)).quantize(Decimal("0.01"))
        target = min(s.high_52w, s.price * Decimal(1 + cfg.take_profit / 100)).quantize(
            Decimal("0.01")
        )
        if s.healthy_trend:
            amount = min(room, budget_.per_trade_cents)
            kind = "comprar" if amount >= MIN_TRADE_CENTS else "vigilar"
            if kind == "vigilar":
                warn.append("Sin presupuesto en el satélite ahora mismo.")
            room -= amount if kind == "comprar" else 0
            out.append(Idea(kind, s.isin, s.name, s.symbol, s.price,
                            reason + " · tendencia de fondo sana", amount if kind == "comprar" else 0,
                            stop, target, warn))  # fmt: skip
        else:
            out.append(Idea("vigilar", s.isin, s.name, s.symbol, s.price,
                            reason + " · por debajo de su media de 200 sesiones y bajando: esperar",
                            0, stop, target, warn))  # fmt: skip
    return out, sigs


# --------------------------------------------------------------------------- frente al índice


@dataclass(frozen=True)
class Versus:
    since: str | None
    satellite_cents: int  # valor actual + lo ya cobrado en ventas
    benchmark_cents: int  # lo mismo si cada compra/venta se hubiera hecho en el índice
    invested_cents: int

    @property
    def diff_cents(self) -> int:
        return self.satellite_cents - self.benchmark_cents


def versus(svc: FinanceService, cfg, *, today: str) -> Versus | None:
    watch = {w.isin: w for w in cfg.watch}
    core = core_isins(svc, today)
    trades = [
        t for t in svc.db.all(
            "SELECT isin, trade_date, kind, amount_cents, fee_cents, tax_cents FROM fin_trades"
            " WHERE kind IN ('buy', 'sell') ORDER BY trade_date"
        )
        if t["isin"] in watch and t["isin"] not in core
    ]  # fmt: skip
    if not trades:
        return None
    bench_units = Decimal(0)
    cash_back = invested = 0
    for t in trades:
        price = close_on(svc, cfg.benchmark, t["trade_date"])
        if price is None:
            return None  # sin histórico del índice tan antiguo
        flow = t["amount_cents"] + t["fee_cents"] + t["tax_cents"]  # compras negativas
        bench_units -= Decimal(flow) / 100 / price
        if flow < 0:
            invested -= flow
        else:
            cash_back += flow
    held = positions(svc, cfg, today=today)
    if any(p.value_cents is None for p in held):
        return None
    last = close_on(svc, cfg.benchmark, today)
    if last is None:
        return None
    return Versus(
        since=trades[0]["trade_date"],
        satellite_cents=sum(p.value_cents or 0 for p in held) + cash_back,
        benchmark_cents=_cents(bench_units * last) + cash_back,
        invested_cents=invested,
    )


@dataclass(frozen=True)
class Overview:
    today: str
    budget: Budget
    ideas: list[Idea]
    signals: list[Signal]
    positions: list[Position]
    taxes: Taxes
    versus: Versus | None
    harvest: list[Position]  # en noviembre y diciembre: pérdidas que compensarían ganancias


def overview(svc: FinanceService, cfg, *, today: str, monthly_expense_cents: int) -> Overview:
    b = budget(svc, cfg, today=today, monthly_expense_cents=monthly_expense_cents)
    ideas_, sigs = ideas(svc, cfg, today=today, budget_=b)
    pos = positions(svc, cfg, today=today)
    t = taxes(svc, today=today)
    harvest = []
    if today[5:7] in ("11", "12") and t.base_cents > 0:
        harvest = [p for p in pos if p.value_cents is not None and p.value_cents < p.cost_cents
                   and p.cost_cents >= MIN_POSITION_CENTS]  # fmt: skip
    return Overview(today, b, ideas_, sigs, pos, t, versus(svc, cfg, today=today), harvest)
