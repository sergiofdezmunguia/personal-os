"""Lectura de mercado para el satélite (ADR 0015): régimen de miedo/euforia, tendencia por
valor, rangos estimados de bajada y subida y tramos de entrada.

Funciones puras y deterministas sobre listas de cierres (del más antiguo al más reciente).
Los umbrales son convenciones conocidas fijadas de antemano, **no ajustadas a los datos**.
Todo son rangos con probabilidad sacados de la historia, nunca predicciones.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import deque
from dataclasses import dataclass, field
from datetime import date, timedelta

# Símbolos públicos (lo único que se envía a las fuentes).
VIX = "^VIX"
VIX3M = "^VIX3M"
INDEX = "^GSPC"
BREADTH = "BREADTH200.SP500"  # % de valores del S&P 500 sobre su media de 200 (TraderMonty)
REGIME_SYMBOLS = (VIX, VIX3M, INDEX)
FRESH_DAYS = 7  # un indicador sin dato en 7 días no cuenta
MIN_COMPONENTS = 3

# Umbrales a priori: (miedo, euforia). Ver la tabla del ADR 0015.
VIX_FEAR, VIX_EUPHORIA = 25.0, 14.0
TERM_FEAR, TERM_EUPHORIA = 1.0, 0.85
SMA_FEAR, SMA_EUPHORIA = -5.0, 10.0  # % frente a la media de 200
DD_FEAR = 10.0  # % desde el máximo de 52 semanas
BREADTH_FEAR, BREADTH_EUPHORIA = 0.35, 0.75
RVOL_FEAR, RVOL_EUPHORIA = 25.0, 10.0  # % anualizada, 20 sesiones

HORIZONS = {21: "1 mes", 63: "3 meses", 252: "12 meses"}
VOL_SHORT = 63
VOL_LONG = 1260
MIN_WINDOWS = {21: 504, 63: 504, 252: 756}
SUPPORT_WINDOW = 120
MIN_GAP = 0.03  # entre tramos, al menos un 3 % de distancia


# --------------------------------------------------------------------------- utilidades


def sma(vals: list[float], n: int, end: int | None = None) -> float | None:
    """Media de las `n` sesiones que acaban en `end` (incluida; por defecto la última)."""
    end = len(vals) - 1 if end is None else end
    if end + 1 < n or end < 0:
        return None
    return sum(vals[end - n + 1 : end + 1]) / n


def realized_vol(vals: list[float], n: int = 20) -> float | None:
    """Volatilidad anualizada (%) de los últimos `n` rendimientos diarios."""
    if len(vals) <= n:
        return None
    rets = [math.log(b / a) for a, b in zip(vals[-n - 1 : -1], vals[-n:], strict=True)]
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    return 100 * math.sqrt(var * 252)


def quantile(sorted_vals: list[float], q: float) -> float:
    """Cuantil con interpolación lineal sobre una lista ya ordenada."""
    if not sorted_vals:
        raise ValueError("lista vacía")
    pos = q * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def pct(x: float, *, sign: bool = True) -> str:
    """12.34 → «+12 %» al estilo del panel (signo menos tipográfico)."""
    r = round(x)
    s = "−" if r < 0 else ("+" if sign and r > 0 else "")
    return f"{s}{abs(r)} %"


# --------------------------------------------------------------------------- régimen


@dataclass(frozen=True)
class Component:
    key: str
    label: str
    value: float | None  # None: sin dato
    vote: int  # −1 miedo · 0 · +1 euforia
    text: str


@dataclass(frozen=True)
class Regime:
    label: str  # miedo | neutral | euforia | desconocido
    components: list[Component]
    day: str | None = None

    @property
    def available(self) -> list[Component]:
        return [c for c in self.components if c.value is not None]

    @property
    def fear_votes(self) -> int:
        return sum(1 for c in self.available if c.vote < 0)

    @property
    def euphoria_votes(self) -> int:
        return sum(1 for c in self.available if c.vote > 0)

    @property
    def missing(self) -> list[str]:
        return [c.label for c in self.components if c.value is None]

    @property
    def summary(self) -> str:
        n = len(self.available)
        if self.label == "desconocido":
            return (f"Sin datos suficientes para leer el mercado ({n} de {len(self.components)} "
                    "indicadores al día).")  # fmt: skip
        head = {
            "miedo": "Hay miedo en el mercado",
            "euforia": "Hay euforia en el mercado",
            "neutral": "Mercado sin miedo ni euforia claros",
        }[self.label]
        return (f"{head}: {self.fear_votes} de {n} indicadores marcan miedo y "
                f"{self.euphoria_votes} euforia.")  # fmt: skip


def _component(key, label, value, fear: bool, euphoria: bool, text: str) -> Component:
    if value is None:
        return Component(key, label, None, 0, "sin dato")
    return Component(key, label, value, -1 if fear else (1 if euphoria else 0), text)


def regime_from_values(
    *,
    vix: float | None,
    vix3m: float | None,
    index: list[float] | None,
    breadth: float | None,
    day: str | None = None,
) -> Regime:
    """Régimen a partir de los últimos valores (todos con datos solo hasta el día evaluado)."""
    comps = [
        _component("vix", "VIX", vix, vix is not None and vix >= VIX_FEAR,
                   vix is not None and vix <= VIX_EUPHORIA,
                   f"VIX en {vix:.0f}" if vix is not None else ""),
    ]  # fmt: skip
    ratio = vix / vix3m if vix is not None and vix3m else None
    comps.append(
        _component("term", "Estructura VIX/VIX3M", ratio,
                   ratio is not None and ratio >= TERM_FEAR,
                   ratio is not None and ratio <= TERM_EUPHORIA,
                   (f"el miedo a corto plazo supera al de 3 meses ({ratio:.2f})" if ratio and ratio >= TERM_FEAR
                    else f"VIX/VIX3M en {ratio:.2f}") if ratio is not None else "")
    )  # fmt: skip
    idx = index or []
    s200 = sma(idx, 200)
    dist = 100 * (idx[-1] / s200 - 1) if s200 else None
    comps.append(
        _component("sma200", "S&P 500 frente a su media de 200", dist,
                   dist is not None and dist <= SMA_FEAR,
                   dist is not None and dist >= SMA_EUPHORIA,
                   f"S&P 500 a {pct(dist)} de su media de 200" if dist is not None else "")
    )  # fmt: skip
    dd = None
    if len(idx) >= 200:
        high = max(idx[-252:])
        dd = 100 * (1 - idx[-1] / high)
    comps.append(
        _component("drawdown", "Caída del S&P 500 desde máximos", dd,
                   dd is not None and dd >= DD_FEAR, False,
                   (f"S&P 500 a −{dd:.0f} % de su máximo de 52 semanas" if round(dd)
                    else "S&P 500 en máximos") if dd is not None else "")
    )  # fmt: skip
    comps.append(
        _component("breadth", "Amplitud (valores sobre su media de 200)", breadth,
                   breadth is not None and breadth <= BREADTH_FEAR,
                   breadth is not None and breadth >= BREADTH_EUPHORIA,
                   f"{breadth * 100:.0f} % de los valores del S&P 500 sobre su media de 200"
                   if breadth is not None else "")
    )  # fmt: skip
    rv = realized_vol(idx) if len(idx) > 20 else None
    comps.append(
        _component("rvol", "Volatilidad del último mes", rv,
                   rv is not None and rv >= RVOL_FEAR,
                   rv is not None and rv <= RVOL_EUPHORIA,
                   f"volatilidad del último mes del {rv:.0f} % anual" if rv is not None else "")
    )  # fmt: skip
    avail = [c for c in comps if c.value is not None]
    net = sum(c.vote for c in avail)
    if len(avail) < MIN_COMPONENTS:
        label = "desconocido"
    elif net <= -2:
        label = "miedo"
    elif net >= 2:
        label = "euforia"
    else:
        label = "neutral"
    return Regime(label, comps, day)


def regime(series: dict[str, list[tuple[str, float]]], *, today: str) -> Regime:
    """Régimen de hoy con lo guardado. Un indicador sin dato de los últimos `FRESH_DAYS` no
    cuenta; con menos de `MIN_COMPONENTS`, «desconocido»."""
    since = (date.fromisoformat(today) - timedelta(days=FRESH_DAYS)).isoformat()

    def fresh(symbol: str) -> list[tuple[str, float]]:
        s = [(d, v) for d, v in series.get(symbol, []) if d <= today]
        return s if s and s[-1][0] >= since else []

    vix, vix3m, idx, br = fresh(VIX), fresh(VIX3M), fresh(INDEX), fresh(BREADTH)
    v3 = None
    if vix and vix3m:  # la estructura compara el mismo día
        by_day = dict(vix3m)
        v3 = by_day.get(vix[-1][0])
    days = [s[-1][0] for s in (vix, idx, br) if s]
    return regime_from_values(
        vix=vix[-1][1] if vix else None,
        vix3m=v3,
        index=[v for _, v in idx][-260:] if idx else None,
        breadth=br[-1][1] if br else None,
        day=max(days) if days else None,
    )


# --------------------------------------------------------------------------- tendencia


@dataclass(frozen=True)
class Trend:
    short: str | None  # alcista | bajista | lateral (None: sin historia)
    medium: str | None
    long: str | None
    structure: str | None  # crecientes | decrecientes | mixta
    golden: bool | None  # media de 50 sobre la de 200

    @property
    def summary(self) -> str:
        parts = [f"{name} {v}" for name, v in (("largo", self.long), ("medio", self.medium),
                                                ("corto", self.short)) if v]  # fmt: skip
        if not parts:
            return "Sin historia suficiente para ver la tendencia."
        text = "Tendencia: " + ", ".join(parts) + "."
        if self.long == "alcista" and self.short == "bajista":
            text += " Corrección dentro de una tendencia alcista."
        elif self.long == "bajista" and self.short == "alcista":
            text += " Rebote dentro de una tendencia bajista: ojo."
        elif self.long == "bajista":
            text += " La tendencia de fondo es bajista."
        if self.structure == "crecientes":
            text += " Máximos y mínimos crecientes."
        elif self.structure == "decrecientes":
            text += " Máximos y mínimos decrecientes."
        return text


def _direction(vals: list[float], n: int, lag: int) -> str | None:
    now, before = sma(vals, n), sma(vals, n, len(vals) - 1 - lag)
    if now is None or before is None:
        return None
    price = vals[-1]
    if price > now and now > before:
        return "alcista"
    if price < now and now < before:
        return "bajista"
    return "lateral"


def trend(vals: list[float]) -> Trend:
    structure = None
    if len(vals) >= 120:
        a, b = vals[-120:-60], vals[-60:]
        hh, hl = max(b) > max(a), min(b) > min(a)
        structure = (
            "crecientes" if hh and hl else ("decrecientes" if not hh and not hl else "mixta")
        )
    s50, s200 = sma(vals, 50), sma(vals, 200)
    return Trend(
        short=_direction(vals, 20, 10),
        medium=_direction(vals, 50, 20),
        long=_direction(vals, 200, 20),
        structure=structure,
        golden=None if s50 is None or s200 is None else s50 > s200,
    )


# --------------------------------------------------------------------------- rangos


def vol_weight(h: int) -> float:
    """Peso de la volatilidad reciente frente a la de 5 años (la volatilidad vuelve a su media)."""
    return math.exp(-h / 126)


@dataclass(frozen=True)
class Profile:
    """Muestras normalizadas de una serie para un horizonte `h`: para cada inicio t con
    volatilidad conocida, (t, final, peor caída, mayor subida) en log / volatilidad de t."""

    h: int
    starts: list[int]
    end: list[float]
    drop: list[float]
    rise: list[float]
    sigma: list[float | None]  # volatilidad diaria mezclada por índice de sesión


def _rolling_sigma(vals: list[float], h: int) -> list[float | None]:
    """σ diaria en cada sesión t (con datos hasta t): mezcla de 63 sesiones y 5 años."""
    n = len(vals)
    rets = [0.0] + [math.log(vals[i] / vals[i - 1]) for i in range(1, n)]
    c1 = [0.0] * (n + 1)
    c2 = [0.0] * (n + 1)
    for i, r in enumerate(rets):
        c1[i + 1] = c1[i] + r
        c2[i + 1] = c2[i] + r * r

    def sd(lo: int, hi: int) -> float:  # rendimientos lo..hi incluidos
        k = hi - lo + 1
        m = (c1[hi + 1] - c1[lo]) / k
        return math.sqrt(max(0.0, (c2[hi + 1] - c2[lo]) / k - m * m) * k / (k - 1))

    w = vol_weight(h)
    out: list[float | None] = [None] * n
    for t in range(n):
        if t < 252:  # al menos un año para la de largo plazo
            continue
        short = sd(t - VOL_SHORT + 1, t)
        long_ = sd(max(1, t - VOL_LONG + 1), t)
        s = math.sqrt(w * short * short + (1 - w) * long_ * long_)
        out[t] = s if s > 0 else None
    return out


def _window_extremes(vals: list[float], h: int) -> tuple[list[float], list[float]]:
    """Mínimo y máximo de vals[t+1 .. t+h] para cada t (deques monótonas, O(n))."""
    n = len(vals)
    mins, maxs = [math.nan] * n, [math.nan] * n
    dmin: deque[int] = deque()
    dmax: deque[int] = deque()
    for j in range(n - 1, 0, -1):  # ventana (t, t+h] con t = j - 1
        while dmin and vals[dmin[-1]] >= vals[j]:
            dmin.pop()
        dmin.append(j)
        while dmax and vals[dmax[-1]] <= vals[j]:
            dmax.pop()
        dmax.append(j)
        t = j - 1
        while dmin[0] > t + h:
            dmin.popleft()
        while dmax[0] > t + h:
            dmax.popleft()
        mins[t], maxs[t] = vals[dmin[0]], vals[dmax[0]]
    return mins, maxs


def profile(vals: list[float], h: int) -> Profile:
    sigma = _rolling_sigma(vals, h)
    mins, maxs = _window_extremes(vals, h)
    starts, end, drop, rise = [], [], [], []
    scale = math.sqrt(h)
    for t in range(len(vals) - h):
        s = sigma[t]
        if s is None:
            continue
        k = s * scale
        starts.append(t)
        end.append(math.log(vals[t + h] / vals[t]) / k)
        drop.append(math.log(mins[t] / vals[t]) / k)
        rise.append(math.log(maxs[t] / vals[t]) / k)
    return Profile(h, starts, end, drop, rise, sigma)


@dataclass(frozen=True)
class Range:
    """Rango estimado a `h` sesiones, en % sobre el precio de hoy."""

    h: int
    label: str
    cases: int  # ventanas de la historia usadas (solapadas)
    years: float  # años de historia
    end_low: float  # cuantil 10 del resultado final
    end_mid: float  # mediana
    end_high: float  # cuantil 90
    drop: float  # cuantil 10 de la peor caída dentro del plazo (negativo)
    rise: float  # cuantil 90 de la mayor subida dentro del plazo

    @property
    def text(self) -> str:
        drop = (f"llegó a caer más de {abs(round(self.drop))} % en algún momento"
                if round(self.drop) < 0 else "apenas cayó")  # fmt: skip
        return (f"A {self.label}: en 1 de cada 10 casos parecidos {drop} y en 1 de cada 10 llegó "
                f"a subir más de {round(self.rise)} %. 8 de cada 10 acabaron entre "
                f"{pct(self.end_low)} y {pct(self.end_high)}.")  # fmt: skip


def _to_pct(z: float, k: float) -> float:
    return 100 * (math.exp(z * k) - 1)


def estimate(p: Profile, *, upto: int | None = None, min_cases: int | None = None,
             years: float = 0.0) -> Range | None:  # fmt: skip
    """Rango para la sesión `upto` (por defecto la última), usando solo ventanas ya cerradas
    entonces (inicio + h ≤ upto) y su volatilidad de ese día."""
    last = len(p.sigma) - 1 if upto is None else upto
    s = p.sigma[last]
    if s is None:
        return None
    k = bisect_right(p.starts, last - p.h)
    need = MIN_WINDOWS.get(p.h, 2 * p.h) if min_cases is None else min_cases
    if k < need:
        return None
    scale = s * math.sqrt(p.h)
    end, drop, rise = sorted(p.end[:k]), sorted(p.drop[:k]), sorted(p.rise[:k])
    return Range(
        h=p.h, label=HORIZONS.get(p.h, f"{p.h} sesiones"), cases=k, years=years,
        end_low=_to_pct(quantile(end, 0.10), scale),
        end_mid=_to_pct(quantile(end, 0.50), scale),
        end_high=_to_pct(quantile(end, 0.90), scale),
        drop=_to_pct(quantile(drop, 0.10), scale),
        rise=_to_pct(quantile(rise, 0.90), scale),
    )  # fmt: skip


def ranges(vals: list[float], days: list[str] | None = None) -> list[Range]:
    years = 0.0
    if days:
        years = (date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days / 365.25
    out = []
    for h in HORIZONS:
        if len(vals) < 252 + h + 2:
            continue
        r = estimate(profile(vals, h), years=round(years, 1))
        if r:
            out.append(r)
    return out


# --------------------------------------------------------------------------- tramos


@dataclass(frozen=True)
class Tranche:
    price: float
    label: str
    amount_cents: int  # 0 si la idea no propone importe


def tranches(
    *,
    price: float,
    vals: list[float],
    ranges_: list[Range],
    stop: float | None,
    regime_label: str,
    amount_cents: int,
) -> list[Tranche]:
    """Hasta tres niveles escalonados para repartir (nunca ampliar) el importe de la idea:
    hoy, el soporte de las últimas 120 sesiones y la caída «1 de cada 10» a 3 meses. Ninguno por
    debajo del stop. En euforia no se propone tramo a precio de hoy (puede quedar vacío)."""
    cands: list[tuple[float, str]] = [(price, "a precio de hoy")]
    if len(vals) >= SUPPORT_WINDOW:
        cands.append((min(vals[-SUPPORT_WINDOW:]), "en el mínimo de los últimos 6 meses"))
    by_h = {r.h: r for r in ranges_}
    if 21 in by_h:
        cands.append((price * (1 + by_h[21].drop / 100), "en la caída «1 de cada 10» a 1 mes"))
    if 63 in by_h:
        cands.append((price * (1 + by_h[63].drop / 100), "en la caída «1 de cada 10» a 3 meses"))
    levels: list[tuple[float, str]] = []
    for lvl, label in sorted(cands, key=lambda c: -c[0]):
        if lvl > price or (stop is not None and lvl <= stop):
            continue
        if levels and lvl > levels[-1][0] * (1 - MIN_GAP):
            continue
        levels.append((lvl, label))
    if regime_label == "euforia" and levels and levels[0][1] == "a precio de hoy":
        levels = levels[1:]  # prudencia: en euforia nada a precio de hoy
    levels = levels[:3]
    if not levels:
        return []
    each = amount_cents // len(levels)
    out = [Tranche(round(lvl, 2), label, each) for lvl, label in levels]
    if amount_cents:  # el resto de la división, al primer tramo
        first = out[0]
        out[0] = Tranche(first.price, first.label, each + amount_cents - each * len(levels))
    return out


# --------------------------------------------------------------------------- vista por valor


@dataclass(frozen=True)
class View:
    symbol: str
    trend: Trend
    ranges: list[Range] = field(default_factory=list)
    entry_from: float | None = None  # precio en el que la regla de caída empezaría a cumplirse
    support_window: list[float] = field(default_factory=list, repr=False)  # para los tramos


def view(symbol: str, days: list[str], vals: list[float], *, dip: float) -> View:
    year = vals[-252:]
    return View(
        symbol=symbol,
        trend=trend(vals),
        ranges=ranges(vals, days),
        entry_from=round(max(year) * (1 - dip / 100), 2) if year else None,
        support_window=vals[-SUPPORT_WINDOW:],
    )


# --------------------------------------------------------------------------- evidencia

# Resultado de la prueba histórica (`scripts/market_backtest.py`, ADR 0015) del 2026-11-15:
# 9 índices y 20 valores grandes (incluidos perdedores), 2000–2026. Se copia a mano lo que
# imprime el script; el panel lo enseña tal cual. Exceso en puntos frente a entrar cualquier
# día; IC 95 % con bootstrap por años.
EVIDENCE: dict = {
    "fecha": "2026-11-15",
    "periodo": "2000–2026",
    "series": 29,
    "entradas": {
        "miedo|63": {"n": 1291, "exceso_pp": 0.5, "ic": [-2.9, 4.5]},
        "miedo + tendencia sana|63": {"n": 835, "exceso_pp": 0.9, "ic": [-1.8, 3.9]},
        "neutral|63": {"n": 2524, "exceso_pp": -0.2, "ic": [-1.9, 1.4]},
        "euforia|63": {"n": 1752, "exceso_pp": -0.1, "ic": [-1.2, 0.8]},
        "regla actual|63": {"n": 724, "exceso_pp": 0.7, "ic": [-1.8, 3.2]},
        "regla actual + miedo|63": {"n": 380, "exceso_pp": 1.4, "ic": [-2.4, 5.3]},
        "miedo|252": {"n": 432, "exceso_pp": -4.1, "ic": [-11.7, 3.0]},
        "miedo + tendencia sana|252": {"n": 381, "exceso_pp": -2.4, "ic": [-10.0, 4.9]},
        "neutral|252": {"n": 717, "exceso_pp": -0.8, "ic": [-7.1, 5.1]},
        "euforia|252": {"n": 547, "exceso_pp": 2.0, "ic": [-2.8, 6.8]},
        "regla actual|252": {"n": 379, "exceso_pp": -0.2, "ic": [-8.1, 7.2]},
        "regla actual + miedo|252": {"n": 255, "exceso_pp": -1.1, "ic": [-11.4, 8.8]},
    },
    "dca": {  # valor final esperando la señal / comprando cada mes (media geométrica)
        "miedo + tendencia sana": {"ratio": 0.909, "ic": [0.868, 0.94], "gana": 2, "de": 29},
        "miedo": {"ratio": 0.942, "ic": [0.922, 0.96], "gana": 4, "de": 29},
        "regla actual": {"ratio": 0.965, "ic": [0.945, 0.986], "gana": 6, "de": 29},
    },
    "tramos": {"n": 357, "exceso_pp": -0.6, "ic": [-2.0, 0.8], "mejor_pct": 44},
    "calibracion": {  # % de veces fuera del cuantil (lo ideal: 10)
        "21": {"n": 9171, "bajo_q10": 10.1, "sobre_q90": 8.3, "caida": 10.4, "subida": 7.6},
        "63": {"n": 3054, "bajo_q10": 11.1, "sobre_q90": 7.2, "caida": 10.9, "subida": 7.2},
        "252": {"n": 741, "bajo_q10": 12.7, "sobre_q90": 4.6, "caida": 15.0, "subida": 3.4},
    },
}


def _num(x: float, d: int = 1) -> str:
    s = f"{abs(x):.{d}f}".replace(".", ",")
    return ("−" if x < 0 else "+") + s if round(x, d) else s


def fear_edge(ev: dict | None = None) -> bool:
    """¿Respalda la prueba histórica entrar con miedo y tendencia sana? Solo si mejora a 3 y a
    12 meses con el intervalo entero por encima de cero."""
    ev = EVIDENCE if ev is None else ev
    e = ev.get("entradas", {})
    keys = ("miedo + tendencia sana|63", "miedo + tendencia sana|252")
    return all(k in e and e[k]["ic"][0] > 0 for k in keys)


def calibration_note(h: int, ev: dict | None = None) -> str | None:
    """Aviso si en la prueba la caída «1 de cada 10» a este plazo se quedó corta o larga."""
    ev = EVIDENCE if ev is None else ev
    c = ev.get("calibracion", {}).get(str(h))
    if not c or abs(c["caida"] - 10) <= 3:
        return None
    word = "más" if c["caida"] > 10 else "menos"
    return (f"Ojo: en la prueba histórica, a {HORIZONS.get(h, h)} cayó {word} de lo estimado "
            f"{round(c['caida'])} de cada 100 veces (no 10): tómalo como mínimo orientativo.")  # fmt: skip


def evidence_lines(ev: dict | None = None) -> list[str]:
    """Lo que dice la prueba histórica, en castellano llano."""
    ev = EVIDENCE if ev is None else ev
    if not ev:
        return []
    e, out = ev["entradas"], []
    a, b = e.get("miedo + tendencia sana|63"), e.get("miedo + tendencia sana|252")
    if a and b:
        verdict = ("Parece una ventaja real." if fear_edge(ev)
                   else "No es una ventaja demostrada: puede ser suerte.")  # fmt: skip
        out.append(
            f"Entrar con miedo y tendencia sana, frente a entrar cualquier día: "
            f"{_num(a['exceso_pp'])} puntos a 3 meses (entre {_num(a['ic'][0])} y "
            f"{_num(a['ic'][1])}) y {_num(b['exceso_pp'])} a 12 meses (entre {_num(b['ic'][0])} y "
            f"{_num(b['ic'][1])}). {verdict}"
        )
    d = ev.get("dca", {}).get("miedo + tendencia sana")
    if d:
        diff = round(100 * (d["ratio"] - 1))
        out.append(
            f"Guardar el dinero hasta que haya miedo con tendencia sana acabó con un {abs(diff)} % "
            f"{'menos' if diff < 0 else 'más'} que comprar cada mes (esperar ganó en {d['gana']} "
            f"de {d['de']} series)."
        )
    t = ev.get("tramos")
    if t:
        out.append(
            f"Repartir la compra en tramos frente a comprarlo todo: {_num(t['exceso_pp'])} puntos "
            f"a 12 meses (entre {_num(t['ic'][0])} y {_num(t['ic'][1])}). Reparte el riesgo de "
            "equivocarte de día; no mejora el resultado medio."
        )
    c = ev.get("calibracion", {})
    if "21" in c and "63" in c:
        line = (f"Rangos a 1 y 3 meses: cayó más que el «1 de cada 10» el {round(c['21']['caida'])} % "
                f"y el {round(c['63']['caida'])} % de las veces (lo ideal, 10 %); subió más que el "
                f"suyo el {round(c['21']['subida'])} % y el {round(c['63']['subida'])} %: las "
                "subidas se estiman algo largas.")  # fmt: skip
        if "252" in c:
            k = c["252"]
            line += (f" A 12 meses, peor: cayó más de lo estimado el {round(k['caida'])} % de las "
                     f"veces y subió más el {round(k['subida'])} %.")  # fmt: skip
        out.append(line)
    out.append(f"Prueba con {ev['series']} índices y valores grandes, {ev['periodo']}, "
               "con cierres sin dividendos.")  # fmt: skip
    return out
