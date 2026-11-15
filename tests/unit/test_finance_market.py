"""Lectura de mercado (ADR 0015): series inventadas (paseos aleatorios con semilla fija)."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta
from itertools import pairwise

import httpx
import pytest

from personal_os.adapters.quotes import QuoteError, QuotesClient, breadth_history
from personal_os.finance import market
from personal_os.finance import market_backtest as bt


def days_until(end: str, n: int) -> list[str]:
    """n días laborables que acaban en `end`."""
    d = date.fromisoformat(end)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


def walk(n: int, *, seed: int = 1, vol: float = 0.01, drift: float = 0.0003,
         start: float = 100.0, vol_last: float | None = None, last: int = 0) -> list[float]:  # fmt: skip
    rng = random.Random(seed)
    out = [start]
    for i in range(n - 1):
        v = vol_last if vol_last is not None and i >= n - 1 - last else vol
        out.append(out[-1] * math.exp(rng.gauss(drift, v)))
    return out


def line(n: int, a: float, b: float) -> list[float]:
    return [a + (b - a) * i / (n - 1) for i in range(n)]


# --------------------------------------------------------------------------- régimen


def test_regime_fear_euphoria_neutral_and_unknown():
    crash = line(200, 100, 120) + line(60, 120, 95)  # −21 % desde máximos, bajo su media
    fear = market.regime_from_values(vix=34, vix3m=28, index=crash, breadth=0.22)
    assert fear.label == "miedo" and fear.fear_votes >= 4
    assert "miedo" in fear.summary.lower()
    calm = walk(260, vol=0.003, drift=0.001)
    euph = market.regime_from_values(vix=12, vix3m=15, index=calm, breadth=0.82)
    assert euph.label == "euforia" and euph.fear_votes == 0
    mid = market.regime_from_values(vix=18, vix3m=20, index=walk(260), breadth=0.55)
    assert mid.label == "neutral"
    # Solo dos indicadores con dato: desconocido.
    unk = market.regime_from_values(vix=40, vix3m=None, index=None, breadth=0.1)
    assert unk.label == "desconocido" and "Estructura VIX/VIX3M" in unk.missing


def test_regime_ignores_stale_sources():
    today = "2026-10-05"
    ds = days_until(today, 260)
    series = {
        market.INDEX: list(zip(ds, line(200, 100, 120) + line(60, 120, 95), strict=True)),
        market.VIX: [(today, 35.0)],
        market.VIX3M: [(today, 30.0)],
        market.BREADTH: [("2026-09-01", 0.2)],  # hace más de una semana: no cuenta
    }
    r = market.regime(series, today=today)
    assert r.label == "miedo" and r.missing == ["Amplitud (valores sobre su media de 200)"]
    # La estructura solo compara el mismo día.
    series[market.VIX3M] = [("2026-10-02", 30.0)]
    assert market.regime(series, today=today).components[1].value is None
    # Nada al día: desconocido, sin romper.
    assert market.regime({}, today=today).label == "desconocido"
    # Lo que pase después de hoy no cuenta.
    later = market.regime({market.VIX: [("2026-10-10", 50.0)]}, today=today)
    assert later.components[0].value is None


# --------------------------------------------------------------------------- tendencia


def test_trend_up_down_and_correction():
    up = market.trend(line(300, 50, 100))
    assert (up.short, up.medium, up.long, up.structure) == ("alcista",) * 3 + ("crecientes",)
    assert up.golden
    down = market.trend(line(300, 100, 50))
    assert down.long == "bajista" and down.structure == "decrecientes"
    assert "de fondo es bajista" in down.summary
    corr = market.trend(line(280, 50, 100) + line(20, 100, 88))
    assert corr.long == "alcista" and corr.short == "bajista"
    assert "Corrección dentro de una tendencia alcista" in corr.summary
    assert market.trend([1.0] * 30).long is None
    assert "Sin historia" in market.trend([1.0] * 5).summary


# --------------------------------------------------------------------------- rangos


def test_ranges_are_ordered_and_explained():
    vals = walk(3000)
    rs = market.ranges(vals, days_until("2026-10-05", 3000))
    assert [r.h for r in rs] == [21, 63, 252]
    for r in rs:
        assert r.drop < r.end_low < r.end_mid < r.end_high < r.rise
        assert r.drop < 0 < r.rise
        assert r.cases >= market.MIN_WINDOWS[r.h] and r.years > 11
    # A más plazo, rango más ancho.
    assert rs[0].end_high - rs[0].end_low < rs[1].end_high - rs[1].end_low
    assert "1 de cada 10 casos parecidos" in rs[1].text and "3 meses" in rs[1].text


def test_ranges_need_enough_history():
    assert market.ranges(walk(300)) == []
    short = market.ranges(walk(900))
    assert [r.h for r in short] == [21, 63]  # 12 meses pide 3 años de ventanas


def test_ranges_widen_with_current_volatility():
    calm = market.ranges(walk(2500, seed=3))[0]
    stormy = market.ranges(walk(2500, seed=3, vol_last=0.04, last=63))[0]
    assert stormy.drop < calm.drop * 1.5 and stormy.rise > calm.rise * 1.5


def test_estimate_has_no_lookahead():
    """El rango de un día pasado no cambia si se altera lo que pasó después."""
    vals = walk(2000, seed=7)
    t = 1400
    a = market.estimate(market.profile(vals, 63), upto=t)
    changed = vals[: t + 1] + [v * 3 for v in vals[t + 1 :]]
    b = market.estimate(market.profile(changed, 63), upto=t)
    assert a == b


def test_quantile_and_helpers():
    assert market.quantile([1, 2, 3, 4], 0.5) == 2.5
    with pytest.raises(ValueError):
        market.quantile([], 0.5)
    assert market.pct(-12.4) == "−12 %" and market.pct(3) == "+3 %" and market.pct(0) == "0 %"
    assert market.sma([1, 2, 3], 5) is None and market.realized_vol([1.0, 1.0]) is None


# --------------------------------------------------------------------------- tramos


def test_tranches_split_amount_and_respect_stop():
    vals = line(200, 100, 120) + line(80, 120, 100)
    rs = market.ranges(walk(2000))
    tr = market.tranches(price=100, vals=vals, ranges_=rs, stop=85, regime_label="neutral",
                         amount_cents=30001)  # fmt: skip
    assert 2 <= len(tr) <= 3 and tr[0].label == "a precio de hoy"
    assert sum(t.amount_cents for t in tr) == 30001  # nunca más dinero
    assert all(t.price > 85 for t in tr)  # ninguno por debajo del stop
    assert all(b.price <= a.price * (1 - market.MIN_GAP) for a, b in pairwise(tr))
    # En euforia, el primer tramo ya no es a precio de hoy.
    eu = market.tranches(price=100, vals=vals, ranges_=rs, stop=85, regime_label="euforia",
                         amount_cents=30000)  # fmt: skip
    assert eu and eu[0].label != "a precio de hoy" and sum(t.amount_cents for t in eu) == 30000
    # Sin importe (vigilar): niveles sin euros; con el stop pegado, solo hoy.
    assert all(t.amount_cents == 0 for t in market.tranches(
        price=100, vals=vals, ranges_=rs, stop=None, regime_label="neutral", amount_cents=0))  # fmt: skip
    only = market.tranches(price=100, vals=vals, ranges_=rs, stop=99.5, regime_label="euforia",
                           amount_cents=100)  # fmt: skip
    assert only == []  # en euforia, nada a precio de hoy aunque no quede otro nivel


def test_view_entry_level():
    vals = line(300, 50, 100)
    v = market.view("X.DE", days_until("2026-10-05", 300), vals, dip=15)
    assert v.entry_from == 85.0 and v.trend.long == "alcista" and len(v.support_window) == 120


# --------------------------------------------------------------------------- evidencia


def test_evidence_is_honest():
    assert market.fear_edge() is False  # la prueba de 2026-11-15 no da ventaja
    lines = market.evidence_lines()
    assert any("No es una ventaja demostrada" in x for x in lines)
    assert any("comprar cada mes" in x for x in lines)
    fake = {"series": 2, "periodo": "2000–2001",
            "entradas": {"miedo + tendencia sana|63": {"n": 9, "exceso_pp": 3, "ic": [1, 5]},
                         "miedo + tendencia sana|252": {"n": 9, "exceso_pp": 6, "ic": [2, 9]}}}  # fmt: skip
    assert market.fear_edge(fake) and any("ventaja real" in x for x in market.evidence_lines(fake))
    assert market.calibration_note(252) and market.calibration_note(21) is None
    assert market.evidence_lines({}) == []


# --------------------------------------------------------------------------- prueba histórica


def _regime_series(n: int, end: str = "2026-10-05"):
    ds = days_until(end, n)
    idx = walk(n, seed=11)
    vix = [15 + 25 * (i % 400 < 60) for i in range(n)]  # miedo a ratos
    return ds, {
        market.INDEX: list(zip(ds, idx, strict=True)),
        market.VIX: list(zip(ds, [float(v) for v in vix], strict=True)),
        market.VIX3M: list(zip(ds, [20.0] * n, strict=True)),
    }


def test_regime_history_has_no_lookahead():
    ds, reg = _regime_series(1200)
    a = bt.regime_history(reg)
    cut = ds[800]
    changed = {k: [(d, v * (5 if d > cut else 1)) for d, v in s] for k, s in reg.items()}
    b = bt.regime_history(changed)
    assert {d: lbl for d, lbl in a.items() if d <= cut} == {
        d: lbl for d, lbl in b.items() if d <= cut
    }
    assert "miedo" in a.values()


def test_backtest_runs_end_to_end():
    n = 2600
    ds, reg = _regime_series(n)
    assets = {"A": list(zip(ds, walk(n, seed=21), strict=True)),
              "B": list(zip(ds, walk(n, seed=22, drift=-0.0001), strict=True))}  # fmt: skip
    r = bt.run(assets, reg, since=ds[300])
    assert r.symbols == ["A", "B"] and set(r.regimes) <= {
        "miedo",
        "neutral",
        "euforia",
        "desconocido",
    }
    by = {(s.condition, s.h) for s in r.entries}
    assert ("cualquier día", 63) in by and ("miedo", 63) in by
    any_day = next(s for s in r.entries if s.condition == "cualquier día" and s.h == 63)
    assert abs(any_day.excess) < 2 and any_day.ci_low <= any_day.ci_high
    assert all(len(d.ratios) == 2 for d in r.dca)
    cal = {c.h: c for c in r.calibration}
    assert cal[21].n > 50
    # En un paseo aleatorio estable los rangos a 1 mes deben acertar cerca del 10 %.
    assert 3 < cal[21].rate(cal[21].below_low) < 20


def test_dca_beats_waiting_in_a_steady_rise():
    n = 800
    ds = days_until("2026-10-05", n)
    vals = line(n, 50, 150)
    conds = [None] * 300 + [{"cualquier día"} for _ in range(n - 300)]
    for i in range(300, n, 120):
        conds[i] = {"cualquier día", "miedo"}
    assert bt.dca_vs_wait(ds, vals, conds, "miedo") < 1
    assert bt.dca_vs_wait(ds, vals, conds, "cualquier día") == pytest.approx(1, rel=0.01)


def test_wilson_interval():
    lo, hi = bt.wilson(10, 100)
    assert lo < 10 < hi and lo > 4 and hi < 18
    assert all(math.isnan(x) for x in bt.wilson(0, 0))


# --------------------------------------------------------------------------- fuente de amplitud


CSV = """Date,S&P500_Price,Breadth_Index_Raw,Breadth_Index_200MA,Breadth_Index_8MA
2026-10-01,700,0.60,0.61,0.62
2026-10-02,701,0.58,0.61,nan
2026-10-05,702,0.40,0.60,0.55
"""


def _client(handler) -> QuotesClient:
    return QuotesClient(httpx.Client(transport=httpx.MockTransport(handler)))


def test_breadth_history_parses_public_csv():
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, text=CSV)

    h = breadth_history(_client(handler))
    assert h.symbol == market.BREADTH and h.currency == "RATIO"
    assert [(d, float(v)) for d, v in h.closes] == [("2026-10-01", 0.62), ("2026-10-05", 0.55)]
    assert not seen[0].query  # nada del usuario en la petición


def test_breadth_history_fails_cleanly():
    with pytest.raises(QuoteError):
        breadth_history(_client(lambda r: httpx.Response(503)))
    with pytest.raises(QuoteError):
        breadth_history(_client(lambda r: httpx.Response(200, text="Date,Otra\n2026-10-01,1\n")))
