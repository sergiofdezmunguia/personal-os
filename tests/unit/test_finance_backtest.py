"""Prueba histórica del satélite con series inventadas."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from personal_os.core.config import SatelliteConfig
from personal_os.finance import backtest, satellite

CFG = SatelliteConfig(dip=15, stop=15, take_profit=25)


def days(values):
    start = date(2022, 1, 3)
    return [
        ((start + timedelta(days=i)).isoformat(), Decimal(str(v))) for i, v in enumerate(values)
    ]


def ramp(a, b, n):
    return [round(a + (b - a) * i / (n - 1), 2) for i in range(n)]


def test_dip_then_recovery_hits_target():
    # Sube 300 sesiones (100 → 160), cae a 128 (−20 %) y se recupera a 170.
    s = days(ramp(100, 160, 300) + ramp(158, 128, 15) + ramp(130, 170, 60))
    bench = days([100] * 375)
    (t,) = backtest.run_symbol("UP", s, dict(bench), CFG)
    assert t.reason == "objetivo" and t.entry < Decimal(137) and t.exit >= Decimal("160")
    assert t.pnl_cents > 0 and t.bench_pnl_cents == -200  # índice plano: solo comisiones


def test_dip_that_keeps_falling_hits_stop():
    s = days(ramp(100, 160, 300) + ramp(158, 80, 60))
    (t, *_rest) = backtest.run_symbol("DOWN", s, {}, CFG)
    assert t.reason == "stop" and t.pnl_cents < -14000 and t.bench_pnl_cents is None


def test_result_metrics_and_tax():
    a = days(ramp(100, 160, 300) + ramp(158, 128, 15) + ramp(130, 170, 60))
    b = days(ramp(100, 160, 300) + ramp(158, 80, 60))
    r = backtest.run({"UP": a, "DOWN": b}, days([100] * 400), CFG)
    assert len(r.closed) >= 2 and 0 < r.win_rate < 100
    assert r.tax_cents >= 0 and r.net_cents == r.pnl_cents - r.tax_cents


def test_outcome_in_euros_after_fees_and_tax():
    i = satellite.Idea(
        "comprar", "X", "X", "X.DE", Decimal(100), "", 100000, Decimal(85), Decimal(125)
    )
    o = satellite.outcome(i)
    assert o.gain_cents == round((25000 - 200) * 0.81) and o.loss_cents == 15000 + 200
    v = satellite.Idea("vigilar", "X", "X", "X.DE", Decimal(100), "", 0, Decimal(85), Decimal(125))
    assert satellite.outcome(v).amount_cents == 100000  # sin importe: por cada 1.000 €
