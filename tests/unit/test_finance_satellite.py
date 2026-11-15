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


def test_emergency_only_warns_when_not_blocking(prices, monkeypatch):
    import dataclasses

    monkeypatch.setattr(satellite, "MIN_TRADE_CENTS", 1)  # los importes inventados son pequeños
    cfg = dataclasses.replace(CFG, emergency_blocks=False)
    b = satellite.budget(prices, cfg, today=TODAY, monthly_expense_cents=10_000_000)
    assert b.available_cents > 0  # el colchón ya no bloquea
    ideas, _ = satellite.ideas(prices, cfg, today=TODAY, budget_=b)
    buy = next(i for i in ideas if i.kind == "comprar")
    assert any("Sale de tu colchón" in w for w in buy.warnings)


def test_top_k_keeps_sells_and_best_scores(prices):
    import dataclasses

    o = satellite.overview(prices, dataclasses.replace(CFG, top_k=1), today=TODAY,
                           monthly_expense_cents=1000)  # fmt: skip
    assert [i.name for i in o.top] == ["FAKE Corp"]  # tendencia sana puntúa por encima
    assert [i.name for i in o.others] == ["FAKE Cayendo"]


# --------------------------------------------------------------------------- lectura de mercado (ADR 0015)


def _fear_market(svc):
    """Mercado inventado con miedo: S&P cae un 20 %, VIX alto y por encima del de 3 meses."""
    from personal_os.finance import market

    idx = series(100, 95, peak_at=240, peak=120)
    for sym, data, cur in (
        (market.INDEX, idx, "USD"),
        (market.VIX, [(d, Decimal(34)) for d, _ in idx[-5:]], "USD"),
        (market.VIX3M, [(d, Decimal(28)) for d, _ in idx[-5:]], "USD"),
        (market.BREADTH, [(d, Decimal("0.25")) for d, _ in idx[-5:]], "RATIO"),
    ):
        satellite.store_history(svc, sym, cur, data, fetched_at="x")


def test_overview_adds_market_reading_without_touching_money_rules(prices, monkeypatch):
    monkeypatch.setattr(satellite, "MIN_TRADE_CENTS", 1)  # los importes inventados son pequeños
    before = satellite.overview(prices, CFG, today=TODAY, monthly_expense_cents=1000)
    assert before.regime.label == "desconocido"  # sin datos de mercado: se dice, no se rompe
    _fear_market(prices)
    o = satellite.overview(prices, CFG, today=TODAY, monthly_expense_cents=1000)
    assert o.regime.label == "miedo"
    # Mismas ideas, importes, stops y objetivos que sin lectura de mercado.
    key = [(i.kind, i.isin, i.amount_cents, i.stop, i.target) for i in o.ideas]
    assert key == [(i.kind, i.isin, i.amount_cents, i.stop, i.target) for i in before.ideas]
    corp = next(i for i in o.ideas if i.name == "FAKE Corp")
    assert corp.kind == "comprar"
    assert any("Miedo en el mercado" in c and "no lo da como ventaja" in c for c in corp.context)
    assert any(c.startswith("Tendencia:") for c in corp.context)
    assert corp.tranches and sum(t.amount_cents for t in corp.tranches) == corp.amount_cents
    assert all(t.price > float(corp.stop) for t in corp.tranches)
    assert set(o.views) == {w.isin for w in CFG.watch}
    # Los cierres en USD no se cuelan en la lista de seguimiento (solo EUR).
    assert satellite.closes(prices, "^GSPC", until=TODAY) == []


def test_panel_cli_and_mcp_show_market_reading(prices, monkeypatch):
    monkeypatch.setattr(satellite, "MIN_TRADE_CENTS", 1)
    from personal_os.ops import panel_tabs

    _fear_market(prices)
    o = satellite.overview(prices, CFG, today=TODAY, monthly_expense_cents=1000)
    html = panel_tabs.tab_ideas(panel_tabs.Extras(satellite=o))
    assert "Cómo está el mercado" in html and ">Miedo<" in html
    assert "no ha servido para entrar mejor" in html  # sin ventaja demostrada: se dice
    corp = next(i for i in o.ideas if i.name == "FAKE Corp")
    assert corp.kind == "comprar", [(i.name, i.kind, len(i.tranches)) for i in o.ideas]
    assert "Lo que dice la historia" in html
    # Con dos o más tramos, el panel los enseña; con uno solo, no («por tramos» no tendría sentido).
    import dataclasses

    from personal_os.finance import market

    two = [market.Tranche(20.0, "a precio de hoy", 600), market.Tranche(18.5, "en el mínimo", 600)]
    o2 = dataclasses.replace(o, ideas=[dataclasses.replace(corp, tranches=two)])
    html2 = panel_tabs.tab_ideas(panel_tabs.Extras(satellite=o2))
    assert "por tramos" in html2 and "18,50 €" in html2
    o1 = dataclasses.replace(o, ideas=[dataclasses.replace(corp, tranches=two[:1])])
    assert "por tramos" not in panel_tabs.tab_ideas(panel_tabs.Extras(satellite=o1))
    assert "probabilidades, no predicciones" in html


class FakeHistory:
    """Yahoo y TraderMonty simulados: apunta qué se pide."""

    def __init__(self, *, fail=()):
        self.asked = []
        self.fail = set(fail)
        self.http = None

    def history(self, client, symbol, *, range_):
        from personal_os.adapters.quotes import History, QuoteError

        self.asked.append((symbol, range_))
        if symbol in self.fail:
            raise QuoteError(f"Sin histórico para {symbol}")
        cur = "EUR" if symbol.endswith(".DE") else "USD"
        return History(symbol, cur, series(100, 110, n=300))

    def breadth(self, client):
        from personal_os.adapters.quotes import History, QuoteError

        if "breadth" in self.fail:
            raise QuoteError("Sin datos de amplitud")
        return History(
            "BREADTH200.SP500", "RATIO", [(d, Decimal("0.5")) for d, _ in series(1, 1, n=30)]
        )


def test_sync_downloads_full_history_once_and_never_breaks(svc, monkeypatch):
    from personal_os.adapters import quotes
    from personal_os.finance import market
    from personal_os.sync.store import SyncReport

    fake_ = FakeHistory(fail={"^VIX3M", "breadth"})
    monkeypatch.setattr(quotes, "history", fake_.history)
    monkeypatch.setattr(quotes, "breadth_history", fake_.breadth)
    report = SyncReport(run_id="r", provider="prices")
    bootstrap._update_market_prices(
        svc, CFG, None, report, stamp="2026-10-05T08:00:00Z", today=TODAY
    )
    assert ("^VIX", "max") in fake_.asked and ("FAKE3.DE", "max") in fake_.asked
    assert all(s in {w.symbol for w in CFG.watch} | {CFG.benchmark, *market.REGIME_SYMBOLS}
               for s, _ in fake_.asked)  # fmt: skip  # solo símbolos públicos
    assert any("amplitud" in w for w in report.warnings) and any(
        "^VIX3M" in e for e in report.errors
    )
    assert report.stats["market_regime"] in {"neutral", "miedo", "euforia"}
    fake_.asked.clear()
    bootstrap._update_market_prices(
        svc, CFG, None, report, stamp="2026-10-05T14:00:00Z", today=TODAY
    )
    assert ("^VIX", "1mo") in fake_.asked and ("^VIX3M", "max") in fake_.asked  # falló: reintenta


def test_sync_warns_when_regime_unknown(svc, monkeypatch):
    from personal_os.adapters import quotes
    from personal_os.finance import market
    from personal_os.sync.store import SyncReport

    fake_ = FakeHistory(fail={*market.REGIME_SYMBOLS, "breadth"})
    monkeypatch.setattr(quotes, "history", fake_.history)
    monkeypatch.setattr(quotes, "breadth_history", fake_.breadth)
    report = SyncReport(run_id="r", provider="prices")
    bootstrap._update_market_prices(
        svc, CFG, None, report, stamp="2026-10-05T08:00:00Z", today=TODAY
    )
    assert report.stats["market_regime"] == "desconocido"
    assert any("Lectura de mercado desconocida" in w for w in report.warnings)


def test_gap_range_covers_missing_days():
    from datetime import date

    assert bootstrap._gap_range(None, TODAY, date) == "max"
    assert bootstrap._gap_range("2026-10-01", TODAY, date) == "1mo"
    assert bootstrap._gap_range("2026-08-01", TODAY, date) == "3mo"
    assert bootstrap._gap_range("2026-01-01", TODAY, date) == "1y"
    assert bootstrap._gap_range("2024-01-01", TODAY, date) == "max"


def test_vigilar_has_no_tranches_and_euphoria_never_buys_today(prices):
    from personal_os.finance import market

    _fear_market(prices)
    o = satellite.overview(prices, CFG, today=TODAY, monthly_expense_cents=1000)
    assert all(not i.tranches for i in o.ideas if i.kind == "vigilar")
    flat = market.tranches(price=100, vals=[100, 99, 101] * 50, ranges_=[], stop=85,
                           regime_label="euforia", amount_cents=50000)  # fmt: skip
    assert flat == []


def test_cli_ideas_prints_market_reading():
    from typer.testing import CliRunner

    from personal_os.cli.main import app as cli
    from personal_os.core.config import config_dir

    write_config_template()
    (config_dir() / "config.toml").write_text(
        'timezone = "Atlantic/Canary"\n[[finance.satellite.watch]]\n'
        'isin = "XF000FAKE009"\nsymbol = "FAKE9.DE"\nname = "FAKE Nine"\n'
    )
    res = CliRunner().invoke(cli, ["finance", "ideas"])
    assert res.exit_code == 0, res.output
    assert "Mercado: DESCONOCIDO" in res.output and "no predicciones" in res.output
