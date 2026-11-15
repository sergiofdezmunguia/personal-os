"""Prueba histórica de la lectura de mercado (ADR 0015), sin fuga de información.

1. **Entradas**: ¿entrar con miedo y tendencia sana rinde más que entrar cualquier día?
   Señal con datos hasta el cierre del día t; entrada al cierre de la sesión siguiente.
   Episodios separados `h` sesiones (una caída no cuenta diez veces). Intervalo al 95 % con
   *bootstrap* por años de entrada (las caídas llegan a la vez a todos los valores).
2. **Frente a DCA**: la misma aportación mensual invertida al momento o guardada (sin rentar)
   hasta la siguiente señal.
3. **Calibración de los rangos**: cada `h` sesiones, el rango que se habría publicado ese día
   (solo con ventanas ya cerradas) frente a lo que pasó después. Lo ideal: 10 % por debajo del
   cuantil 10 y 10 % por encima del 90.

Puro y determinista (semilla fija). Precios de cierre sin dividendos.
"""

from __future__ import annotations

import math
import random
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from personal_os.finance import market

DIP = 15.0  # regla actual del satélite (ADR 0014, valor por defecto)
CONDITIONS = ("cualquier día", "miedo", "miedo + tendencia sana", "neutral", "euforia",
              "regla actual", "regla actual + miedo")  # fmt: skip
SEED = 15
BOOT = 2000
STALE_DAYS = 7

Series = list[tuple[str, float]]


# --------------------------------------------------------------------------- régimen histórico


def regime_history(series: dict[str, Series]) -> dict[str, str]:
    """Régimen de cada sesión del índice, solo con datos hasta ese día."""
    idx = series.get(market.INDEX, [])
    vix = series.get(market.VIX, [])
    vix3m = dict(series.get(market.VIX3M, []))
    br = series.get(market.BREADTH, [])
    vix_days = [d for d, _ in vix]
    br_days = [d for d, _ in br]
    vals = [v for _, v in idx]

    def last_fresh(days: list[str], s: Series, day: str) -> tuple[str, float] | None:
        k = bisect_right(days, day)
        if not k:
            return None
        d, v = s[k - 1]
        return (
            (d, v) if (date.fromisoformat(day) - date.fromisoformat(d)).days <= STALE_DAYS else None
        )

    out = {}
    for i, (day, _) in enumerate(idx):
        if i < 200:
            continue
        v = last_fresh(vix_days, vix, day)
        b = last_fresh(br_days, br, day)
        r = market.regime_from_values(
            vix=v[1] if v else None,
            vix3m=vix3m.get(v[0]) if v else None,
            index=vals[max(0, i - 259) : i + 1],
            breadth=b[1] if b else None,
            day=day,
        )
        out[day] = r.label
    return out


def _regime_on(reg_days: list[str], regimes: dict[str, str], day: str) -> str:
    k = bisect_right(reg_days, day)
    if not k:
        return "desconocido"
    d = reg_days[k - 1]
    if (date.fromisoformat(day) - date.fromisoformat(d)).days > STALE_DAYS:
        return "desconocido"
    return regimes[d]


# --------------------------------------------------------------------------- señales por valor


def conditions(days: list[str], vals: list[float], regimes: dict[str, str], *,
               dip: float = DIP) -> list[set[str] | None]:  # fmt: skip
    """Para cada sesión, qué condiciones se cumplen al cierre (None: aún sin historia)."""
    reg_days = sorted(regimes)
    n = len(vals)
    sma = [None] * n
    run = 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= 200:
            run -= vals[i - 200]
        if i >= 199:
            sma[i] = run / 200
    out: list[set[str] | None] = [None] * n
    for i in range(252, n):
        if sma[i - 20] is None:
            continue
        healthy = vals[i] >= sma[i] or sma[i] > sma[i - 20]
        dd = 100 * (1 - vals[i] / max(vals[i - 252 : i]))
        reg = _regime_on(reg_days, regimes, days[i])
        c = {"cualquier día"}
        if reg in ("miedo", "neutral", "euforia"):
            c.add(reg)
        if reg == "miedo" and healthy:
            c.add("miedo + tendencia sana")
        if dd >= dip and healthy:
            c.add("regla actual")
            if reg == "miedo":
                c.add("regla actual + miedo")
        out[i] = c
    return out


@dataclass(frozen=True)
class Episode:
    symbol: str
    day: str  # día de la señal
    ret: float  # % a h sesiones desde la entrada (cierre siguiente)
    excess: float  # frente a la media de «cualquier día» de ese valor


def episodes(symbol: str, days: list[str], vals: list[float], conds, h: int) -> dict[str, list[Episode]]:  # fmt: skip
    n = len(vals)
    fwd = [None] * n
    for i in range(n - h - 1):
        fwd[i] = 100 * (vals[i + 1 + h] / vals[i + 1] - 1)
    base = [fwd[i] for i in range(n) if conds[i] is not None and fwd[i] is not None]
    if not base:
        return {}
    mean_all = sum(base) / len(base)
    out: dict[str, list[Episode]] = defaultdict(list)
    last: dict[str, int] = {}
    for i in range(n):
        if conds[i] is None or fwd[i] is None:
            continue
        for c in conds[i]:
            if i - last.get(c, -(10**9)) < h:
                continue
            last[c] = i
            out[c].append(Episode(symbol, days[i], fwd[i], fwd[i] - mean_all))
    return out


@dataclass(frozen=True)
class Stat:
    condition: str
    h: int
    n: int
    mean_ret: float
    hit_rate: float  # % de episodios en positivo
    excess: float  # media frente a «cualquier día»
    ci_low: float
    ci_high: float

    @property
    def significant(self) -> bool:
        return self.ci_low > 0 or self.ci_high < 0


def _cluster_ci(eps: list[Episode], rng: random.Random) -> tuple[float, float]:
    by_year: dict[str, list[float]] = defaultdict(list)
    for e in eps:
        by_year[e.day[:4]].append(e.excess)
    years = list(by_year.values())
    if len(years) < 2:
        return (math.nan, math.nan)
    means = []
    for _ in range(BOOT):
        tot = cnt = 0
        for _ in years:
            g = years[rng.randrange(len(years))]
            tot += sum(g)
            cnt += len(g)
        means.append(tot / cnt)
    means.sort()
    return market.quantile(means, 0.025), market.quantile(means, 0.975)


def entry_stats(per_symbol: dict[str, dict[str, list[Episode]]], h: int) -> list[Stat]:
    rng = random.Random(SEED + h)
    out = []
    for c in CONDITIONS:
        eps = [e for s in per_symbol.values() for e in s.get(c, [])]
        if not eps:
            continue
        lo, hi = _cluster_ci(eps, rng)
        out.append(Stat(c, h, len(eps), sum(e.ret for e in eps) / len(eps),
                        100 * sum(e.ret > 0 for e in eps) / len(eps),
                        sum(e.excess for e in eps) / len(eps), lo, hi))  # fmt: skip
    return out


# --------------------------------------------------------------------------- frente a DCA


@dataclass(frozen=True)
class DcaResult:
    condition: str
    ratios: dict[str, float]  # valor final esperando la señal / DCA, por símbolo
    ci_low: float  # media geométrica del ratio, IC 95 % (bootstrap por valores)
    ci_high: float

    @property
    def wins(self) -> int:
        return sum(r > 1 for r in self.ratios.values())

    @property
    def geo_mean(self) -> float:
        logs = [math.log(r) for r in self.ratios.values()]
        return math.exp(sum(logs) / len(logs)) if logs else math.nan


def dca_vs_wait(days: list[str], vals: list[float], conds, condition: str) -> float | None:
    """Valor final esperando a `condition` / valor final con DCA (aportación mensual)."""
    start = next((i for i, c in enumerate(conds) if c is not None), None)
    if start is None or len(vals) - start < 252:
        return None
    dca_units = wait_units = cash = 0.0
    month = None
    for i in range(start, len(vals)):
        if days[i][:7] != month:
            month = days[i][:7]
            dca_units += 1 / vals[i]
            cash += 1
        if cash and conds[i] is not None and condition in conds[i] and i + 1 < len(vals):
            wait_units += cash / vals[i + 1]
            cash = 0.0
    last = vals[-1]
    return (wait_units * last + cash) / (dca_units * last)


def dca_stats(series: dict[str, tuple[list[str], list[float], list]], condition: str) -> DcaResult:
    ratios = {}
    for sym, (days, vals, conds) in series.items():
        r = dca_vs_wait(days, vals, conds, condition)
        if r:
            ratios[sym] = r
    logs = [math.log(r) for r in ratios.values()]
    rng = random.Random(SEED)
    boots = []
    if len(logs) >= 2:
        for _ in range(BOOT):
            sample = [logs[rng.randrange(len(logs))] for _ in logs]
            boots.append(math.exp(sum(sample) / len(sample)))
        boots.sort()
    lo = market.quantile(boots, 0.025) if boots else math.nan
    hi = market.quantile(boots, 0.975) if boots else math.nan
    return DcaResult(condition, ratios, lo, hi)


# --------------------------------------------------------------------------- calibración


@dataclass
class Calibration:
    h: int
    n: int = 0
    below_low: int = 0  # final por debajo del cuantil 10
    above_high: int = 0  # final por encima del cuantil 90
    deeper_drop: int = 0  # peor caída más honda que la «1 de cada 10»
    higher_rise: int = 0  # mayor subida por encima de la «1 de cada 10»
    by_symbol: dict[str, int] = field(default_factory=dict)

    def rate(self, k: int) -> float:
        return 100 * k / self.n if self.n else math.nan


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Intervalo de Wilson (en %) para una proporción."""
    if not n:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    w = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return 100 * (c - w), 100 * (c + w)


# --------------------------------------------------------------------------- todo junto


@dataclass(frozen=True)
class Report:
    start: str
    end: str
    symbols: list[str]
    regimes: dict[str, int]  # sesiones por régimen
    entries: list[Stat]
    dca: list[DcaResult]
    calibration: list[Calibration]
    tranches: Stat | None = None


def run(assets: dict[str, Series], regime_series: dict[str, Series], *, since: str = "2000-01-01",
        horizons=(63, 252), dip: float = DIP) -> Report:  # fmt: skip
    regimes = regime_history(regime_series)
    counted = defaultdict(int)
    for d, lbl in regimes.items():
        if d >= since:
            counted[lbl] += 1
    prepared = {}
    for sym, s in assets.items():
        s = [(d, v) for d, v in s if v > 0]
        days = [d for d, _ in s]
        vals = [v for _, v in s]
        conds = conditions(days, vals, regimes, dip=dip)
        for i, d in enumerate(days):  # el periodo evaluado empieza en `since`
            if d < since:
                conds[i] = None
        prepared[sym] = (days, vals, conds)
    entries = []
    for h in horizons:
        per = {sym: episodes(sym, d, v, c, h) for sym, (d, v, c) in prepared.items()}
        entries += entry_stats(per, h)
    dca = [dca_stats(prepared, c) for c in ("miedo + tendencia sana", "miedo", "regla actual")]
    cals = []
    for h in market.HORIZONS:
        cal = Calibration(h)
        for sym, (days, vals, _) in prepared.items():
            # La calibración usa toda la historia para estimar, pero solo evalúa desde `since`.
            k = next((i for i, d in enumerate(days) if d >= since), len(days))
            calibrate(sym, vals, h, cal, k)
        cals.append(cal)
    tr = []
    for sym, (days, vals, conds) in prepared.items():
        tr += tranche_episodes(sym, days, vals, conds, dip=dip)
    tstat = None
    if tr:
        lo, hi = _cluster_ci(tr, random.Random(SEED))
        tstat = Stat("tramos frente a todo de golpe", TRANCHE_HOLD, len(tr),
                     sum(e.ret for e in tr) / len(tr), 100 * sum(e.ret > 0 for e in tr) / len(tr),
                     sum(e.excess for e in tr) / len(tr), lo, hi)  # fmt: skip
    end = max((p[0][-1] for p in prepared.values() if p[0]), default=since)
    return Report(since, end, sorted(prepared), dict(counted), entries, dca, cals, tstat)


TRANCHE_WAIT = 63  # tramos no ejecutados en 3 meses: se compran al cierre de ese día
TRANCHE_HOLD = 252


def tranche_episodes(symbol: str, days: list[str], vals: list[float], conds, *,
                     dip: float = DIP, stop: float = 15.0) -> list[Episode]:  # fmt: skip
    """En cada señal de la regla actual (separadas un año): diferencia, en puntos, entre
    repartir la compra en los tramos de `market.tranches` y comprarlo todo al día siguiente,
    medida a 12 meses. Niveles calculados solo con datos hasta la señal."""
    p21, p63 = market.profile(vals, 21), market.profile(vals, 63)
    out = []
    last = -(10**9)
    n = len(vals)
    for i in range(n):
        e = i + 1
        if conds[i] is None or "regla actual" not in conds[i] or e + TRANCHE_HOLD >= n:
            continue
        if i - last < TRANCHE_HOLD:
            continue
        rs = [r for r in (market.estimate(p21, upto=i), market.estimate(p63, upto=i)) if r]
        levels = market.tranches(price=vals[i], vals=vals[: i + 1], ranges_=rs,
                                 stop=vals[i] * (1 - stop / 100), regime_label="neutral",
                                 amount_cents=300)  # fmt: skip
        if len(levels) < 2:
            continue
        last = i
        units = 0.0
        for k, t in enumerate(levels):
            money = t.amount_cents / 100
            if k == 0:
                units += money / vals[e]
                continue
            fill = next((j for j in range(e + 1, e + TRANCHE_WAIT + 1) if vals[j] <= t.price),
                        e + TRANCHE_WAIT)  # fmt: skip
            units += money / vals[fill]
        allin = 3.0 / vals[e]
        end = vals[e + TRANCHE_HOLD]
        diff = 100 * (units * end - allin * end) / (allin * end)
        out.append(Episode(symbol, days[i], diff, diff))
    return out


def calibrate(symbol: str, vals: list[float], h: int, cal: Calibration, first: int = 0) -> None:
    """Evalúa cada `h` sesiones desde `first` el rango que se habría publicado ese día."""
    p = market.profile(vals, h)
    t = first
    n = len(vals)
    while t + h < n:
        r = market.estimate(p, upto=t)
        if r is None:
            t += 1
            continue
        window = vals[t + 1 : t + h + 1]
        end = 100 * (vals[t + h] / vals[t] - 1)
        cal.n += 1
        cal.below_low += end < r.end_low
        cal.above_high += end > r.end_high
        cal.deeper_drop += 100 * (min(window) / vals[t] - 1) < r.drop
        cal.higher_rise += 100 * (max(window) / vals[t] - 1) > r.rise
        cal.by_symbol[symbol] = cal.by_symbol.get(symbol, 0) + 1
        t += h  # evaluaciones sin solaparse
