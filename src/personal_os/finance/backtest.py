"""Prueba histórica de las reglas del satélite (ADR 0014): ¿qué habría pasado con la lista
de seguimiento si se hubieran seguido al pie de la letra?

Simulación simple y conservadora, con cierres diarios (sin datos intradía):

- Entrada al cierre del día en que se cumple: caída ≥ `dip` % desde el máximo de las 252
  sesiones anteriores y tendencia sana (precio ≥ media de 200 o media de 200 subiendo frente a
  20 sesiones antes). Una posición abierta por valor; tras salir, `COOLDOWN` sesiones de espera.
- Salida al cierre en que se alcanza el objetivo (máximo previo, como mucho +`take_profit` %) o
  el stop (−`stop` %). Si se acaba el histórico, queda abierta y cuenta a precio de hoy.
- Importe fijo por operación, 1 € de comisión al comprar y al vender (Trade Republic).
- **Frente al índice**: el mismo dinero, las mismas fechas, en el índice de referencia.
- IRPF aproximado: 19 % del resultado neto positivo de cada año (sin compensar entre años).

No incluye dividendos, deslizamientos ni el coste de tener el dinero parado entre operaciones.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

WINDOW = 252
COOLDOWN = 20
FEE_CENTS = 100
TAX = 0.19


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_day: str
    entry: Decimal
    exit_day: str
    exit: Decimal
    reason: str  # objetivo | stop | abierta
    days: int  # sesiones
    pnl_cents: int  # tras comisiones
    bench_pnl_cents: int | None  # lo mismo en el índice, mismas fechas

    @property
    def win(self) -> bool:
        return self.pnl_cents > 0


@dataclass(frozen=True)
class Result:
    trade_cents: int
    trades: list[Trade] = field(default_factory=list)

    @property
    def closed(self) -> list[Trade]:
        return [t for t in self.trades if t.reason != "abierta"]

    @property
    def win_rate(self) -> float | None:
        c = self.closed
        return 100 * sum(t.win for t in c) / len(c) if c else None

    @property
    def avg_pct(self) -> float | None:
        c = self.closed
        return 100 * sum(t.pnl_cents for t in c) / len(c) / self.trade_cents if c else None

    @property
    def avg_days(self) -> float | None:
        c = self.closed
        return sum(t.days for t in c) / len(c) if c else None

    @property
    def pnl_cents(self) -> int:
        return sum(t.pnl_cents for t in self.trades)

    @property
    def bench_pnl_cents(self) -> int | None:
        if any(t.bench_pnl_cents is None for t in self.trades):
            return None
        return sum(t.bench_pnl_cents for t in self.trades)

    @property
    def tax_cents(self) -> int:
        by_year: dict[str, int] = defaultdict(int)
        for t in self.closed:
            by_year[t.exit_day[:4]] += t.pnl_cents
        return round(sum(max(0, v) * TAX for v in by_year.values()))

    @property
    def net_cents(self) -> int:
        return self.pnl_cents - self.tax_cents


def _pnl(cents: int, entry: Decimal, exit_: Decimal) -> int:
    return round(cents * float(exit_ / entry - 1)) - 2 * FEE_CENTS


def run_symbol(symbol: str, closes: list[tuple[str, Decimal]], bench: dict[str, Decimal], cfg, *,
               trade_cents: int = 100000) -> list[Trade]:  # fmt: skip
    vals = [c for _, c in closes]
    days = [d for d, _ in closes]
    bench_days = sorted(bench)

    def bench_on(day: str) -> Decimal | None:
        best = None
        for d in bench_days:  # listas cortas (≈1.300 días): suficiente
            if d > day:
                break
            best = bench[d]
        return best

    trades: list[Trade] = []
    pos: tuple[int, Decimal, Decimal, Decimal] | None = None  # (índice, entrada, objetivo, stop)
    wait_until = 0
    sma = [None] * len(vals)
    running = Decimal(0)
    for i, v in enumerate(vals):
        running += v
        if i >= 200:
            running -= vals[i - 200]
        if i >= 199:
            sma[i] = running / 200
    for i in range(WINDOW, len(vals)):
        price = vals[i]
        if pos is not None:
            j, entry, target, stop = pos
            reason = "objetivo" if price >= target else "stop" if price <= stop else None
            if reason:
                trades.append(
                    _trade(symbol, days, j, i, entry, price, reason, trade_cents, bench_on)
                )
                pos, wait_until = None, i + COOLDOWN
            continue
        if i < wait_until or sma[i] is None or sma[i - 20] is None:
            continue
        high = max(vals[i - WINDOW : i])
        drawdown = 100 * float(1 - price / high)
        healthy = price >= sma[i] or sma[i] > sma[i - 20]
        if drawdown >= cfg.dip and healthy:
            target = min(high, price * Decimal(1 + cfg.take_profit / 100))
            pos = (i, price, target, price * Decimal(1 - cfg.stop / 100))
    if pos is not None:
        j, entry, _, _ = pos
        trades.append(
            _trade(
                symbol, days, j, len(vals) - 1, entry, vals[-1], "abierta", trade_cents, bench_on
            )
        )
    return trades


def _trade(symbol, days, j, i, entry, exit_, reason, cents, bench_on) -> Trade:
    b0, b1 = bench_on(days[j]), bench_on(days[i])
    bench = _pnl(cents, b0, b1) if b0 and b1 else None
    return Trade(
        symbol, days[j], entry, days[i], exit_, reason, i - j, _pnl(cents, entry, exit_), bench
    )


def run(series: dict[str, list[tuple[str, Decimal]]], bench: list[tuple[str, Decimal]], cfg, *,
        trade_cents: int = 100000) -> Result:  # fmt: skip
    b = dict(bench)
    trades: list[Trade] = []
    for symbol, closes in series.items():
        trades += run_symbol(symbol, closes, b, cfg, trade_cents=trade_cents)
    trades.sort(key=lambda t: t.entry_day)
    return Result(trade_cents, trades)


def from_db(svc, cfg, *, today: str, years: int = 5, trade_cents: int = 100000) -> Result:
    from personal_os.finance import satellite

    days = 365 * years + 30
    series = {w.symbol: satellite.closes(svc, w.symbol, until=today, days=days) for w in cfg.watch}
    bench = satellite.closes(svc, cfg.benchmark, until=today, days=days)
    return run({s: c for s, c in series.items() if len(c) > WINDOW + 20}, bench, cfg,
               trade_cents=trade_cents)  # fmt: skip
