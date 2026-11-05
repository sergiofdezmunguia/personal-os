"""Satélite por reglas (ADR 0014): precios sintéticos y movimientos inventados."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from decimal import Decimal

import pytest

from personal_os import bootstrap
from personal_os.core.config import SatelliteConfig, WatchItem, write_config_template
from personal_os.core.events import ChangeContext
from personal_os.finance import investments as inv
from personal_os.finance import satellite
from personal_os.finance.importers import detect
from tests import trade_republic_activity_fake as fake

CLI = ChangeContext.cli()
TODAY = "2026-10-05"
CFG = SatelliteConfig(
    benchmark="BENCH.DE",
    watch=(
        WatchItem("XF000FAKE003", "FAKE3.DE", "FAKE Corp"),
        WatchItem("XF000FAKE009", "FALL.DE", "FAKE Cayendo"),
        WatchItem("XF000FAKE001", "WORLD.DE", "FAKE World ETF"),  # está en el plan: núcleo
    ),
)


@pytest.fixture
def svc(clock):
    write_config_template()
    app = bootstrap.open_app(clock=clock)
    s = bootstrap.finance_service(app)
    data = fake.build_csv()
    inv.import_document(s, CLI, detect(data, "f.csv"), file_name="f.csv",
                        file_sha256=hashlib.sha256(data).hexdigest())  # fmt: skip
    yield s
    app.db.close()


def series(start: float, end: float, n: int = 300, *, peak_at: int | None = None, peak: float = 0):
    """n cierres diarios hasta TODAY: lineal de start a end (con un pico opcional)."""
    last = date.fromisoformat(TODAY)
    out = []
    for i in range(n):
        if peak_at is not None:
            v = (
                start + (peak - start) * i / peak_at
                if i <= peak_at
                else peak + (end - peak) * (i - peak_at) / (n - 1 - peak_at)
            )
        else:
            v = start + (end - start) * i / (n - 1)
        out.append(((last - timedelta(days=n - 1 - i)).isoformat(), Decimal(str(round(v, 2)))))
    return out


@pytest.fixture
def prices(svc):
    # FAKE Corp: sube de 10 a 25 y corrige a 20 (−20 %), con la media de 200 aún por debajo.
    satellite.store_history(
        svc, "FAKE3.DE", "EUR", series(10, 20, peak_at=270, peak=25), fetched_at="x"
    )
    # Cayendo: de 20 a 12 sin parar (tendencia rota).
    satellite.store_history(svc, "FALL.DE", "EUR", series(20, 12), fetched_at="x")
    satellite.store_history(svc, "WORLD.DE", "EUR", series(40, 30), fetched_at="x")
    satellite.store_history(svc, "BENCH.DE", "EUR", series(100, 110), fetched_at="x")
    return svc


def test_tax_brackets():
    assert satellite.tax_on(0) == 0 and satellite.tax_on(-500) == 0
    assert satellite.tax_on(600000) == 114000  # 6.000 € al 19 %
    assert satellite.tax_on(1000000) == 114000 + 84000  # 4.000 € más al 21 %


def test_signals(prices):
    s = satellite.signal(prices, CFG.watch[0], today=TODAY)
    assert s.high_52w == Decimal("25") and round(s.drawdown) == 20 and s.healthy_trend
    f = satellite.signal(prices, CFG.watch[1], today=TODAY)
    assert not f.healthy_trend and f.sma200_rising is False and f.rsi14 is not None


def test_budget_and_buy_idea(prices):
    b = satellite.budget(prices, CFG, today=TODAY, monthly_expense_cents=1000)
    assert b.free_cash_cents == b.cash_cents - 6000 - b.plan_cents
    assert b.available_cents == min(b.cap_cents - b.invested_cents, b.free_cash_cents) > 0
    ideas, _ = satellite.ideas(prices, CFG, today=TODAY, budget_=b)
    by = {i.name: i for i in ideas}
    assert by["FAKE Corp"].kind == (
        "comprar" if min(b.available_cents, b.per_trade_cents) >= 5000 else "vigilar"
    )
    assert by["FAKE Corp"].stop == Decimal("17.00") and by["FAKE Corp"].target == Decimal("25.00")
    assert by["FAKE Cayendo"].kind == "vigilar" and "esperar" in by["FAKE Cayendo"].reason
    assert "FAKE World ETF" not in by  # el plan es núcleo: nunca idea del satélite


def test_no_budget_when_cash_does_not_cover_emergency(prices):
    b = satellite.budget(prices, CFG, today=TODAY, monthly_expense_cents=10_000_000)
    assert b.available_cents == 0 and "colchón" in b.why_zero
    ideas, _ = satellite.ideas(prices, CFG, today=TODAY, budget_=b)
    assert all(i.kind != "comprar" for i in ideas)


def test_sell_idea_and_tiny_positions(prices, monkeypatch):
    b = satellite.budget(prices, CFG, today=TODAY, monthly_expense_cents=1000)
    ideas, _ = satellite.ideas(prices, CFG, today=TODAY, budget_=b)
    assert not [i for i in ideas if i.kind == "vender"]  # 15 € de posición: testimonial
    monkeypatch.setattr(satellite, "MIN_POSITION_CENTS", 0)
    ideas, _ = satellite.ideas(prices, CFG, today=TODAY, budget_=b)
    (sell,) = [i for i in ideas if i.kind == "vender"]
    assert sell.name == "FAKE Corp" and "objetivo" in sell.reason  # coste ~10,33 → 20: +94 %


def test_taxes_and_versus(prices):
    t = satellite.taxes(prices, today=TODAY)
    assert t.year == 2026 and t.realized_cents > 0 and t.dividends_cents > 0
    v = satellite.versus(prices, CFG, today=TODAY)
    assert v.since == "2026-06-10" and v.invested_cents == 3100
    assert v.satellite_cents == 1.5 * 2000 + 1700  # 1,5 acciones a 20 € + lo cobrado al vender
    assert v.benchmark_cents > 0


def test_overview_and_notice(prices, clock):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from personal_os.ops import notices

    o = satellite.overview(prices, CFG, today=TODAY, monthly_expense_cents=1000)
    got = notices.ideas(o, datetime(2026, 10, 5, 8, 0, tzinfo=ZoneInfo("Europe/Madrid")))
    assert all(n["id"].startswith("idea:") and ":2026-W41" in n["id"] for n in got)
