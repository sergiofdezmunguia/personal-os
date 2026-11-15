"""Prueba histórica de la lectura de mercado (ADR 0015) con datos públicos.

    uv run python scripts/market_backtest.py --cache /tmp/mercado

Descarga de Yahoo (solo símbolos públicos) y del CSV de TraderMonty a la carpeta de caché;
nunca toca la base de datos ni la configuración del usuario. Imprime el informe y el bloque
`EVIDENCE` para copiar en `finance/market.py`.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from personal_os.adapters.quotes import QuotesClient, breadth_history, history
from personal_os.finance import market, market_backtest

INDICES = ["^GSPC", "^NDX", "^DJI", "^RUT", "^STOXX50E", "^GDAXI", "^FTSE", "^N225", "^HSI"]
# Valores grandes de 2000, incluidos los que luego fueron mal (sin sesgo de supervivencia).
STOCKS = ["AAPL", "MSFT", "AMZN", "JPM", "XOM", "JNJ", "KO", "PG", "WMT", "INTC", "GE", "C",
          "PFE", "IBM", "CSCO", "BA", "T", "BAC", "DIS", "MMM"]  # fmt: skip


def load(cache: Path, symbol: str, client: QuotesClient) -> list[tuple[str, float]]:
    path = cache / (symbol.replace("^", "_") + ".json")
    if path.exists():
        return [tuple(x) for x in json.loads(path.read_text())]
    h = (
        breadth_history(client)
        if symbol == market.BREADTH
        else history(client, symbol, range_="max")
    )
    data = [(d, float(v)) for d, v in h.closes]
    path.write_text(json.dumps(data))
    return data


def f(x: float, d: int = 1) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.{d}f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--since", default="2000-01-01")
    args = ap.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    client = QuotesClient()
    reg = {s: load(args.cache, s, client) for s in (*market.REGIME_SYMBOLS, market.BREADTH)}
    assets = {}
    for s in INDICES + STOCKS:
        try:
            assets[s] = load(args.cache, s, client)
        except Exception as exc:  # noqa: BLE001 - un símbolo caído no para la prueba
            print(f"sin datos de {s}: {exc}")
    for s, d in {**reg, **assets}.items():
        print(f"{s:<18} {d[0][0]} → {d[-1][0]}  {len(d)} sesiones")
    r = market_backtest.run(assets, reg, since=args.since)
    print(
        f"\nPeriodo {r.start} → {r.end} · {len(r.symbols)} series · sesiones por régimen {r.regimes}"
    )
    print("\nEntradas (episodios separados h sesiones; IC 95 % bootstrap por años)")
    for s in r.entries:
        print(f"  h={s.h:<3} {s.condition:<24} n={s.n:<5} media {f(s.mean_ret)} % · "
              f"positivas {s.hit_rate:.0f} % · frente a cualquier día {f(s.excess)} pp "
              f"[{f(s.ci_low)}, {f(s.ci_high)}]{' *' if s.significant else ''}")  # fmt: skip
    print("\nEsperar la señal frente a DCA mensual (valor final; media geométrica, IC 95 %)")
    for d in r.dca:
        print(
            f"  {d.condition:<24} {d.geo_mean:.3f} [{d.ci_low:.3f}, {d.ci_high:.3f}] · "
            f"gana a DCA en {d.wins} de {len(d.ratios)}"
        )
    if r.tranches:
        t = r.tranches
        print(
            f"\nTramos frente a comprar todo al día siguiente (12 meses, n={t.n}): {f(t.excess)} pp "
            f"[{f(t.ci_low)}, {f(t.ci_high)}] · mejor en {t.hit_rate:.0f} % de los casos"
        )
    print("\nCalibración de los rangos (ideal: 10 %)")
    for c in r.calibration:
        rows = [("final < q10", c.below_low), ("final > q90", c.above_high),
                ("caída > «1 de 10»", c.deeper_drop), ("subida > «1 de 10»", c.higher_rise)]  # fmt: skip
        txt = " · ".join(
            f"{k} {c.rate(v):.1f} % [{market_backtest.wilson(v, c.n)[0]:.1f}–{market_backtest.wilson(v, c.n)[1]:.1f}]"
            for k, v in rows
        )
        print(f"  h={c.h:<3} n={c.n:<5} {txt}")
    print("\nEVIDENCE = " + json.dumps(evidence(r), ensure_ascii=False, indent=4))


def evidence(r: market_backtest.Report) -> dict:
    by = {(s.condition, s.h): s for s in r.entries}
    out = {"periodo": f"{r.start[:4]}–{r.end[:4]}", "series": len(r.symbols), "entradas": {}}
    for (c, h), s in by.items():
        if c == "cualquier día":
            continue
        out["entradas"][f"{c}|{h}"] = {"n": s.n, "exceso_pp": round(s.excess, 1),
                                        "ic": [round(s.ci_low, 1), round(s.ci_high, 1)]}  # fmt: skip
    out["dca"] = {d.condition: {"ratio": round(d.geo_mean, 3), "ic": [round(d.ci_low, 3),
                  round(d.ci_high, 3)], "gana": d.wins, "de": len(d.ratios)} for d in r.dca}  # fmt: skip
    if r.tranches:
        t = r.tranches
        out["tramos"] = {"n": t.n, "exceso_pp": round(t.excess, 1), "ic": [round(t.ci_low, 1),
                         round(t.ci_high, 1)], "mejor_pct": round(t.hit_rate)}  # fmt: skip
    out["calibracion"] = {
        str(c.h): {"n": c.n, "bajo_q10": round(c.rate(c.below_low), 1),
                   "sobre_q90": round(c.rate(c.above_high), 1),
                   "caida": round(c.rate(c.deeper_drop), 1), "subida": round(c.rate(c.higher_rise), 1)}
        for c in r.calibration
    }  # fmt: skip
    return out


if __name__ == "__main__":
    main()
