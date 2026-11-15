"""Diario del satélite (ADR 0016): lo que dijo cada día y cómo le fue después.

- `record` apunta **una vez al día** (idempotente) el régimen de mercado con sus indicadores y,
  por idea, el símbolo, el tipo (comprar/vigilar/vender), el precio, los rangos estimados y los
  tramos. **Nunca importes en euros** ni nada del patrimonio, el efectivo o las posiciones.
- `evaluate` mide, fuera de muestra, el resultado de esas ideas a 1, 3 y 12 meses frente a
  haber entrado en el índice de referencia los mismos días, y la calibración de los rangos
  (¿cae más que el «1 de cada 10» el 10 % de las veces?), con intervalos. Mientras no haya
  casos suficientes, lo dice.
- `regime_changes` saca del diario los cambios de régimen confirmados (dos sesiones seguidas)
  para el aviso del iPhone (`ops/notices.py`): el estado persistido es el propio diario.

Solo lectura salvo `record`, determinista, sin red.
"""

from __future__ import annotations

import json
import math
import random
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from personal_os.finance import market
from personal_os.finance.market_backtest import wilson
from personal_os.finance.service import FinanceService

MIN_CASES = 10  # casos independientes ya cumplidos para enseñar un resultado
CONFIRM_SESSIONS = 2  # un cambio de régimen cuenta si dura dos sesiones seguidas
MAX_SESSION_GAP_DAYS = 5  # más días entre dos apuntes: ya no son «seguidas»
MIN_SESSIONS_BETWEEN = (
    5  # tras un cambio, otro necesita al menos estas sesiones (sin idas y vueltas)
)
BOOT = 2000
SEED = 16
BENCH_MAX_GAP_DAYS = 7  # el índice tiene que tener cierre cerca de las fechas comparadas


# --------------------------------------------------------------------------- apuntar


def recorded(svc: FinanceService, day: str) -> bool:
    return svc.db.one("SELECT 1 AS x FROM fin_satellite_journal WHERE day = ?", (day,)) is not None


def _r(x: float | None, d: int = 4) -> float | None:
    return None if x is None else round(float(x), d)


def _price_day(svc: FinanceService, symbol: str, today: str) -> str | None:
    return svc.db.one(
        "SELECT MAX(price_date) AS d FROM fin_market_prices WHERE symbol = ? AND currency = 'EUR'"
        " AND price_date <= ?",
        (symbol, today),
    )["d"]


def record(svc: FinanceService, o, benchmark: str, *, day: str, recorded_at: str) -> int | None:
    """Apunta lo que dice el satélite hoy (`o` = `satellite.Overview`). Devuelve cuántas ideas
    se apuntaron, o None si ese día ya estaba apuntado. Sin importes: solo mercado y decisión."""
    if recorded(svc, day):
        return None
    reg = o.regime
    indicators = [{"key": c.key, "value": _r(c.value), "vote": c.vote, "text": c.text}
                  for c in (reg.components if reg else [])]  # fmt: skip
    top = {(i.isin, i.kind) for i in o.top}
    days = {s.isin: s.day for s in o.signals}
    rows = []
    for n, i in enumerate(o.ideas, 1):
        pday = days.get(i.isin) or _price_day(svc, i.symbol, day)
        if i.price is None or pday is None:
            continue
        v = o.views.get(i.isin)
        trend = (
            {"short": v.trend.short, "medium": v.trend.medium, "long": v.trend.long} if v else None
        )
        ranges = [{"h": r.h, "cases": r.cases, "end_low": _r(r.end_low, 2), "end_mid": _r(r.end_mid, 2),
                   "end_high": _r(r.end_high, 2), "drop": _r(r.drop, 2), "rise": _r(r.rise, 2)}
                  for r in (v.ranges if v else [])]  # fmt: skip
        tranches = [{"price": t.price, "label": t.label} for t in i.tranches]
        rows.append((day, i.isin, i.kind, n, int((i.isin, i.kind) in top), i.symbol, i.name,
                     str(i.price), pday, None if i.stop is None else str(i.stop),
                     None if i.target is None else str(i.target),
                     json.dumps(trend) if trend else None, json.dumps(ranges),
                     json.dumps(tranches, ensure_ascii=False)))  # fmt: skip
    with svc.db.transaction():
        svc.db.execute(
            "INSERT INTO fin_satellite_journal(day, recorded_at, regime, regime_day, indicators,"
            " benchmark) VALUES (?,?,?,?,?,?)",
            (day, recorded_at, reg.label if reg else "desconocido", reg.day if reg else None,
             json.dumps(indicators, ensure_ascii=False), benchmark),
        )  # fmt: skip
        for row in rows:
            svc.db.execute(
                "INSERT INTO fin_satellite_journal_ideas(day, isin, kind, position, top, symbol,"
                " name, price, price_day, stop, target, trend, ranges, tranches)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
    return len(rows)


@dataclass(frozen=True)
class Entry:
    day: str
    regime: str
    regime_day: str | None
    indicators: list[dict]
    benchmark: str
    ideas: list[dict] = field(default_factory=list)


def entries(svc: FinanceService, *, since: str | None = None) -> list[Entry]:
    rows = svc.db.all(
        "SELECT * FROM fin_satellite_journal WHERE day >= ? ORDER BY day", (since or "",)
    )
    ideas: dict[str, list[dict]] = defaultdict(list)
    for r in svc.db.all(
        "SELECT * FROM fin_satellite_journal_ideas WHERE day >= ? ORDER BY day, position",
        (since or "",),
    ):
        d = dict(r)
        d["ranges"] = json.loads(d["ranges"])
        d["tranches"] = json.loads(d["tranches"])
        d["trend"] = json.loads(d["trend"]) if d["trend"] else None
        ideas[d["day"]].append(d)
    return [Entry(r["day"], r["regime"], r["regime_day"], json.loads(r["indicators"]),
                  r["benchmark"], ideas.get(r["day"], [])) for r in rows]  # fmt: skip


# --------------------------------------------------------------------------- cambios de régimen


@dataclass(frozen=True)
class RegimeChange:
    day: str  # día del diario en que se confirmó
    regime_day: str | None
    before: str
    after: str
    indicators: list[dict]


def regime_changes(svc: FinanceService) -> list[RegimeChange]:
    """Cambios de régimen confirmados, del más antiguo al más reciente. El primer régimen
    conocido es el punto de partida (sin aviso). Contra el ruido:

    - un régimen nuevo tiene que verse en `CONFIRM_SESSIONS` sesiones **seguidas**: un
      «desconocido» o un hueco de más de `MAX_SESSION_GAP_DAYS` días entre apuntes rompe la
      racha (pero no cambia el régimen confirmado);
    - tras un cambio, el siguiente necesita al menos `MIN_SESSIONS_BETWEEN` sesiones: un
      indicador que baila alrededor del umbral no genera avisos de ida y vuelta."""
    rows = svc.db.all(
        "SELECT day, regime, regime_day, indicators FROM fin_satellite_journal ORDER BY day"
    )
    confirmed: str | None = None
    streak_label, streak = None, 0
    last_session: str | None = None
    session_n, changed_at = 0, -(10**9)
    out = []
    for r in rows:
        session = r["regime_day"] or r["day"]
        if session == last_session:
            continue  # misma sesión de mercado apuntada otro día (fin de semana, festivo)
        if (
            last_session is not None
            and (date.fromisoformat(session) - date.fromisoformat(last_session)).days
            > MAX_SESSION_GAP_DAYS
        ):
            streak_label, streak = None, 0  # hueco en el diario: no son sesiones seguidas
        last_session = session
        session_n += 1
        label = r["regime"]
        if label == "desconocido":
            streak_label, streak = None, 0
            continue
        if confirmed is None:
            confirmed = label
            continue
        if label == confirmed:
            streak_label, streak = None, 0
            continue
        streak = streak + 1 if label == streak_label else 1
        streak_label = label
        if streak >= CONFIRM_SESSIONS and session_n - changed_at >= MIN_SESSIONS_BETWEEN:
            out.append(RegimeChange(r["day"], r["regime_day"], confirmed, label,
                                    json.loads(r["indicators"])))  # fmt: skip
            confirmed, streak_label, streak, changed_at = label, None, 0, session_n
    return out


# --------------------------------------------------------------------------- evaluación


@dataclass(frozen=True)
class Result:
    """Ideas de un tipo a un plazo: rendimiento del valor menos el del índice, en puntos."""

    kind: str
    h: int
    n: int
    mean_excess: float
    ci: tuple[float, float] | None  # IC 95 % (bootstrap por meses); None si hay pocos meses
    beat: int  # casos en que el valor lo hizo mejor que el índice

    @property
    def ready(self) -> bool:
        return self.n >= MIN_CASES


@dataclass(frozen=True)
class Calibration:
    h: int
    n: int = 0
    deeper_drop: int = 0  # la peor caída fue más honda que la «1 de cada 10»
    below_low: int = 0  # acabó por debajo del cuantil 10
    above_high: int = 0  # acabó por encima del cuantil 90

    @property
    def ready(self) -> bool:
        return self.n >= MIN_CASES


@dataclass(frozen=True)
class Evaluation:
    first_day: str | None
    last_day: str | None
    days: int
    ideas: int
    results: list[Result]
    calibration: list[Calibration]
    first_due: dict[int, str]  # plazo → fecha aproximada en que se cumple el primer caso
    symbols: int = 0  # valores distintos con alguna idea
    today: str | None = None

    @property
    def ready(self) -> bool:
        return any(r.ready for r in self.results) or any(c.ready for c in self.calibration)

    def lines(self) -> list[str]:
        return evaluation_lines(self)


def _series(svc: FinanceService, symbol: str, since: str, until: str):
    rows = svc.db.all(
        "SELECT price_date, close FROM fin_market_prices WHERE symbol = ? AND currency = 'EUR'"
        " AND price_date BETWEEN ? AND ? ORDER BY price_date",
        (symbol, since, until),
    )
    return [r["price_date"] for r in rows], [float(r["close"]) for r in rows]


def _bench_close(svc: FinanceService, symbol: str, day: str) -> float | None:
    row = svc.db.one(
        "SELECT price_date, close FROM fin_market_prices WHERE symbol = ? AND price_date <= ?"
        " ORDER BY price_date DESC LIMIT 1",
        (symbol, day),
    )
    if row is None:
        return None
    gap = (date.fromisoformat(day) - date.fromisoformat(row["price_date"])).days
    return float(row["close"]) if gap <= BENCH_MAX_GAP_DAYS else None


def _independent(rows: list[dict], dates: list[str], h: int) -> list[tuple[dict, int]]:
    """Una muestra cada `h` sesiones como mínimo (una idea que sale 20 días seguidos es un
    caso, no 20). Devuelve (fila, índice de su sesión en la serie)."""
    out, last = [], -(10**9)
    for r in sorted(rows, key=lambda r: r["price_day"]):
        i = bisect_right(dates, r["price_day"]) - 1
        if i < 0 or dates[i] != r["price_day"]:
            continue
        if i - last < h:
            continue
        last = i
        out.append((r, i))
    return out


def _cluster_ci(cases: list[tuple[str, float]], rng: random.Random) -> tuple[float, float] | None:
    """IC 95 % de la media con bootstrap por meses (las ideas de un mismo mes van juntas)."""
    by_month: dict[str, list[float]] = defaultdict(list)
    for day, x in cases:
        by_month[day[:7]].append(x)
    groups = list(by_month.values())
    if len(groups) < 3:
        return None
    means = []
    for _ in range(BOOT):
        tot = cnt = 0.0
        for _ in groups:
            g = groups[rng.randrange(len(groups))]
            tot += sum(g)
            cnt += len(g)
        means.append(tot / cnt)
    means.sort()
    return market.quantile(means, 0.025), market.quantile(means, 0.975)


def _due(day: str, h: int) -> str:
    """Fecha aproximada (sesiones → días naturales) en que se cumple un plazo."""
    return (date.fromisoformat(day) + timedelta(days=math.ceil(h * 7 / 5) + 1)).isoformat()


def evaluate(svc: FinanceService, *, today: str) -> Evaluation:
    es = entries(svc)
    all_ideas = [(e, i) for e in es for i in e.ideas]
    rng = random.Random(SEED)
    by_symbol: dict[str, list[tuple[Entry, dict]]] = defaultdict(list)
    for e, i in all_ideas:
        by_symbol[i["symbol"]].append((e, i))
    cases: dict[tuple[str, int], list[tuple[str, float, bool]]] = defaultdict(list)
    calib = {h: [0, 0, 0, 0] for h in market.HORIZONS}  # n, caída, bajo, sobre
    for symbol, items in by_symbol.items():
        first = min(i["price_day"] for _, i in items)
        dates, vals = _series(svc, symbol, first, today)
        if not dates:
            continue
        bench_of = {i["price_day"]: e.benchmark for e, i in items}
        for h in market.HORIZONS:
            # Resultado frente al índice: por tipo de idea.
            for kind in ("comprar", "vigilar", "vender"):
                rows = [i for _, i in items if i["kind"] == kind]
                for r, t in _independent(rows, dates, h):
                    if t + h >= len(dates):
                        continue
                    d0, d1 = dates[t], dates[t + h]
                    b0 = _bench_close(svc, bench_of[r["price_day"]], d0)
                    b1 = _bench_close(svc, bench_of[r["price_day"]], d1)
                    if not b0 or not b1:
                        continue
                    ret = vals[t + h] / vals[t] - 1
                    excess = 100 * (ret - (b1 / b0 - 1))
                    cases[(kind, h)].append((d0, excess, ret > b1 / b0 - 1))
            # Calibración de los rangos: da igual el tipo de idea, cuenta el valor.
            rows = [i for _, i in items if any(g["h"] == h for g in i["ranges"])]
            for r, t in _independent(rows, dates, h):
                if t + h >= len(dates):
                    continue
                g = next(g for g in r["ranges"] if g["h"] == h)
                p0 = vals[t]
                window = vals[t + 1 : t + h + 1]
                worst = 100 * (min(window) / p0 - 1)
                final = 100 * (vals[t + h] / p0 - 1)
                c = calib[h]
                c[0] += 1
                c[1] += worst < g["drop"]
                c[2] += final < g["end_low"]
                c[3] += final > g["end_high"]
    results = []
    for kind in ("comprar", "vigilar", "vender"):
        for h in market.HORIZONS:
            cs = cases.get((kind, h), [])
            if not cs:
                continue
            mean = sum(x for _, x, _ in cs) / len(cs)
            ci = _cluster_ci([(d, x) for d, x, _ in cs], rng) if len(cs) >= MIN_CASES else None
            results.append(Result(kind, h, len(cs), mean, ci, sum(b for _, _, b in cs)))
    cal = [Calibration(h, *calib[h]) for h in market.HORIZONS if calib[h][0]]
    first_day = es[0].day if es else None
    first_price = min((i["price_day"] for _, i in all_ideas), default=None)
    return Evaluation(
        first_day=first_day, last_day=es[-1].day if es else None, days=len(es),
        ideas=len(all_ideas), results=results, calibration=cal,
        first_due={h: _due(first_price, h) for h in market.HORIZONS} if first_price else {},
        symbols=len(by_symbol), today=today,
    )  # fmt: skip


# --------------------------------------------------------------------------- texto


def _pp(x: float) -> str:
    s = f"{abs(x):.1f}".replace(".", ",")
    return ("−" if x < 0 else "+") + s if round(x, 1) else s


def _short(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d.day}/{d.month}/{d.year}"


def _rate(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k} de {n} casos ({round(100 * k / n)} %, entre {round(lo)} y {round(hi)} %)"


KIND_TEXT = {
    "comprar": "Ideas de compra",
    "vigilar": "Valores en «vigilar» (lo que habría pasado de comprarlos)",
    "vender": "Tras las ideas de venta, el valor",
}


def evaluation_lines(ev: Evaluation) -> list[str]:
    """Lo que dice el diario, en castellano llano. Sin casos suficientes, lo dice."""
    if not ev.days:
        return ["El diario del satélite aún está vacío: se apunta solo en cada `pos sync`."]
    head = (f"Diario desde el {_short(ev.first_day)}: {ev.days} día{'s' * (ev.days != 1)} "
            f"apuntado{'s' * (ev.days != 1)}, {ev.ideas} apuntes de ideas sobre {ev.symbols} "
            f"valor{'es' * (ev.symbols != 1)}.")  # fmt: skip
    out = [head]
    if not ev.ready:
        due = ev.first_due.get(21)
        best = max((r.n for r in ev.results if r.h == 21), default=0)
        if due and ev.today and due > ev.today:
            when = f" Los primeros casos a 1 mes se cumplen hacia el {_short(due)}."
        elif due:
            when = f" A 1 mes ya hay {best} de {MIN_CASES}."
        else:
            when = ""
        out.append(f"Aún no hay datos suficientes para evaluarlo: hacen falta al menos {MIN_CASES} "
                   f"casos independientes ya cumplidos a un plazo.{when}")  # fmt: skip
        return out
    for r in ev.results:
        if not r.ready:
            continue
        label = market.HORIZONS[r.h]
        ci = (
            f" (entre {_pp(r.ci[0])} y {_pp(r.ci[1])})" if r.ci else " (sin intervalo: pocos meses)"
        )
        if r.kind == "vender":
            verdict = ("vender acertó" if r.ci and r.ci[1] < 0 else
                       "vender se equivocó" if r.ci and r.ci[0] > 0 else
                       "no se distingue del índice: puede ser suerte")  # fmt: skip
        else:
            verdict = ("mejor que el índice" if r.ci and r.ci[0] > 0 else
                       "peor que el índice" if r.ci and r.ci[1] < 0 else
                       "no se distingue del índice: puede ser suerte")  # fmt: skip
        out.append(f"{KIND_TEXT[r.kind]} a {label}: {_pp(r.mean_excess)} puntos frente al índice "
                   f"de media{ci}, {r.n} casos; lo hizo mejor que el índice {r.beat} de {r.n} "
                   f"veces. {verdict[0].upper()}{verdict[1:]}.")  # fmt: skip
    for c in ev.calibration:
        if not c.ready:
            continue
        out.append(f"Rangos a {market.HORIZONS[c.h]}: cayó más que el «1 de cada 10» en "
                   f"{_rate(c.deeper_drop, c.n)}; acabó por debajo del rango en "
                   f"{_rate(c.below_low, c.n)} y por encima en {_rate(c.above_high, c.n)} "
                   "(lo ideal, 10 % cada uno).")  # fmt: skip
    pending = [market.HORIZONS[h] for h in market.HORIZONS
               if not any(r.ready and r.h == h for r in ev.results)]  # fmt: skip
    if pending:
        out.append("Aún sin casos suficientes a " + " ni a ".join(pending) + ".")
    out.append("Casos separados al menos un plazo entre sí por valor. Las acciones van sin "
               "dividendos y el índice, si es un ETF de acumulación, con ellos: la comparación "
               "sale algo en contra de las ideas (1–2 puntos al año). Pocos casos: tómalo como "
               "orientación, no como prueba.")  # fmt: skip
    return out
