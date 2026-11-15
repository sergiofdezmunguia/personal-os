"""Diario del satélite y avisos de cambio de régimen (ADR 0016). Todo inventado."""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from personal_os import bootstrap
from personal_os.core.config import write_config_template
from personal_os.finance import journal, satellite
from personal_os.ops import notices
from tests.unit.test_finance_satellite import CFG, TODAY, _fear_market
from tests.unit.test_finance_satellite import prices as prices
from tests.unit.test_finance_satellite import svc as svc

TZ = ZoneInfo("Atlantic/Canary")


def business_days(start: str, n: int) -> list[str]:
    d, out = date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def add_day(svc, day, regime, *, regime_day=None, ideas=(), indicators=None, bench="BENCH.DE"):
    """Una entrada del diario escrita a mano (para probar lectura y evaluación)."""
    svc.db.execute(
        "INSERT INTO fin_satellite_journal(day, recorded_at, regime, regime_day, indicators,"
        " benchmark) VALUES (?,?,?,?,?,?)",
        (day, day + "T08:00:00Z", regime, regime_day or day,
         json.dumps(indicators or []), bench),
    )  # fmt: skip
    for n, i in enumerate(ideas, 1):
        svc.db.execute(
            "INSERT INTO fin_satellite_journal_ideas(day, isin, kind, position, top, symbol, name,"
            " price, price_day, stop, target, trend, ranges, tranches)"
            " VALUES (?,?,?,?,1,?,?,?,?,NULL,NULL,NULL,?,'[]')",
            (day, i.get("isin", "XF000EVAL001"), i.get("kind", "comprar"), n,
             i.get("symbol", "EVAL.DE"), "FAKE Eval", str(i["price"]), i.get("price_day", day),
             json.dumps(i.get("ranges", []))),
        )  # fmt: skip


# --------------------------------------------------------------------------- apuntar


def test_record_once_per_day_without_money(prices, monkeypatch):
    monkeypatch.setattr(satellite, "MIN_TRADE_CENTS", 1)  # los importes inventados son pequeños
    _fear_market(prices)
    o = satellite.overview(prices, CFG, today=TODAY, monthly_expense_cents=1000)
    assert any(i.amount_cents for i in o.ideas)  # hay importes… que no deben guardarse
    n = journal.record(prices, o, CFG.benchmark, day=TODAY, recorded_at="2026-10-05T08:00:00Z")
    assert n == len(o.ideas) and n >= 2
    assert journal.record(prices, o, CFG.benchmark, day=TODAY, recorded_at="x") is None
    assert journal.recorded(prices, TODAY)
    [e] = journal.entries(prices)
    assert e.regime == "miedo" and e.benchmark == "BENCH.DE"
    assert {i["key"] for i in e.indicators} >= {"vix", "term", "sma200", "breadth"}
    corp = next(i for i in e.ideas if i["name"] == "FAKE Corp")
    assert corp["kind"] == "comprar" and corp["top"] == 1 and corp["price_day"] == TODAY
    assert all(set(t) == {"price", "label"} for t in corp["tranches"]) and corp["tranches"]
    # Nada de euros ni del patrimonio: ni columnas ni valores.
    cols = [r["name"] for t in ("fin_satellite_journal", "fin_satellite_journal_ideas")
            for r in prices.db.all(f"PRAGMA table_info({t})")]  # fmt: skip
    assert not any("cents" in c or "amount" in c for c in cols)
    dump = json.dumps([dict(r) for r in prices.db.all("SELECT * FROM fin_satellite_journal_ideas")])
    for i in o.ideas:
        if i.amount_cents:
            assert str(i.amount_cents) not in dump and "amount" not in dump
    assert "Ya tienes" not in dump and "vendiste" not in dump  # sin avisos personales


def test_sync_step_records_once_and_never_breaks(svc, clock, monkeypatch):
    app = bootstrap.open_app(clock=clock)
    app.config = dataclasses.replace(
        app.config, finance=dataclasses.replace(app.config.finance, satellite=CFG)
    )
    assert bootstrap.satellite_journal(app) is None  # sin cierres: nada que apuntar
    assert not journal.recorded(svc, TODAY)
    from tests.unit.test_finance_satellite import series

    satellite.store_history(svc, "FAKE3.DE", "EUR", series(10, 20), fetched_at="x")
    r = bootstrap.satellite_journal(app)
    assert r is not None and not r.errors and journal.recorded(svc, TODAY)
    assert bootstrap.satellite_journal(app) is None  # una vez al día

    def boom(*a, **k):
        raise RuntimeError("fallo inventado")

    monkeypatch.setattr(satellite, "overview", boom)
    svc.db.execute("DELETE FROM fin_satellite_journal_ideas")
    svc.db.execute("DELETE FROM fin_satellite_journal")
    r = bootstrap.satellite_journal(app)
    assert r.errors and "diario" in r.errors[0]
    app.db.close()


# --------------------------------------------------------------------------- cambios de régimen


def test_regime_changes_need_two_sessions_in_a_row(svc):
    seq = [("2026-09-01", "neutral"), ("2026-09-02", "miedo"),  # una sola sesión: ruido
           ("2026-09-03", "neutral"), ("2026-09-04", "miedo"),
           ("2026-09-07", "desconocido"),  # rompe la racha
           ("2026-09-08", "miedo"), ("2026-09-09", "miedo")]  # fmt: skip
    for d, label in seq:
        add_day(svc, d, label)
    [c] = journal.regime_changes(svc)
    assert (c.before, c.after, c.day) == ("neutral", "miedo", "2026-09-09")
    # Fin de semana: la misma sesión apuntada otro día no confirma nada.
    add_day(svc, "2026-09-12", "neutral", regime_day="2026-09-11")
    add_day(svc, "2026-09-13", "neutral", regime_day="2026-09-11")
    assert len(journal.regime_changes(svc)) == 1
    # Dos sesiones de neutral, pero recién cambiado: sin ida y vuelta hasta pasadas 5 sesiones.
    add_day(svc, "2026-09-14", "neutral")
    assert len(journal.regime_changes(svc)) == 1
    for d in ("2026-09-15", "2026-09-16"):
        add_day(svc, d, "neutral")
    assert len(journal.regime_changes(svc)) == 1
    add_day(svc, "2026-09-17", "neutral")
    c = journal.regime_changes(svc)[-1]
    assert (c.before, c.after, c.day) == ("miedo", "neutral", "2026-09-17")


def test_regime_flapping_gives_few_changes_and_gaps_break_streaks(svc):
    labels = ["neutral", "miedo", "miedo", "neutral", "neutral", "miedo", "miedo", "neutral",
              "neutral"]  # fmt: skip
    for d, label in zip(business_days("2026-09-01", 9), labels, strict=True):
        add_day(svc, d, label)
    assert len(journal.regime_changes(svc)) <= 2  # no cuatro avisos en nueve sesiones


def test_gap_in_journal_is_not_two_sessions_in_a_row(svc):
    for d, label in (("2026-09-01", "neutral"), ("2026-09-02", "miedo"), ("2026-10-20", "miedo")):
        add_day(svc, d, label)
    assert journal.regime_changes(svc) == []


def test_first_regime_is_baseline(svc):
    add_day(svc, "2026-09-01", "euforia")
    add_day(svc, "2026-09-02", "euforia")
    assert journal.regime_changes(svc) == []


IND = [{"key": "vix", "value": 31.0, "vote": -1, "text": "VIX en 31"},
       {"key": "drawdown", "value": 12.0, "vote": -1, "text": "S&P 500 a −12 % de su máximo de 52 semanas"},
       {"key": "breadth", "value": 0.5, "vote": 0, "text": "50 % de los valores"}]  # fmt: skip


def test_regime_notice_once_short_and_honest(svc):
    for d, label in (("2026-10-01", "neutral"), ("2026-10-02", "miedo"), ("2026-10-05", "miedo")):
        add_day(svc, d, label, indicators=IND)
    now = datetime(2026, 10, 5, 9, 0, tzinfo=TZ)
    n = notices.regime(svc, now)
    assert n["id"] == "regimen:miedo:2026-10-05" and n["kind"] == "mercado"
    assert n["title"] == "🧭 Mercado con miedo"
    assert "VIX en 31" in n["body"] and "50 %" not in n["body"]
    assert "contexto, no una señal" in n["body"] and "no dio ventaja" in n["body"]
    assert "Tus reglas no cambian" in n["body"] and len(n["body"]) < 260
    # Otra sync más tarde: el mismo id (el iPhone no lo repite); pasados 3 días, ya no está.
    assert notices.regime(svc, now + timedelta(hours=6))["id"] == n["id"]
    assert notices.regime(svc, now + timedelta(days=4)) is None
    # Solo con satélite; y no entra en las alertas de la pestaña Hoy (kind distinto).
    built = notices.build(svc, _Panel(), now, satellite=SimpleNamespace(top=[]))
    assert [x["id"] for x in built if x["id"].startswith("regimen:")] == [n["id"]]
    assert not any(x["id"].startswith("regimen:") for x in notices.build(svc, _Panel(), now))


def test_regime_notice_texts_for_euphoria_and_back_to_neutral(svc):
    eup = [{"key": "vix", "value": 12.0, "vote": 1, "text": "VIX en 12"}]
    for d, label in (("2026-09-01", "neutral"), ("2026-09-02", "euforia"),
                     ("2026-09-03", "euforia")):  # fmt: skip
        add_day(svc, d, label, indicators=eup)
    n = notices.regime(svc, datetime(2026, 9, 3, 9, 0, tzinfo=TZ))
    assert n["title"] == "🧭 Mercado eufórico" and "pasa a euforia: VIX en 12." in n["body"]
    assert "contexto, no una señal con ventaja" in n["body"]
    for d in ("2026-09-04", "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10"):
        add_day(svc, d, "neutral")
    n = notices.regime(svc, datetime(2026, 9, 10, 9, 0, tzinfo=TZ))
    assert n["title"] == "🧭 Mercado en calma"
    assert n["body"].startswith("Se acaba la euforia: el mercado vuelve a neutral.")


class _Panel:
    """Lo mínimo para que `notices.build` no genere los avisos de gasto (fuera de su hora)."""

    def __getattr__(self, name):
        raise AssertionError(name)


@pytest.fixture(autouse=True)
def _no_spend_notices(monkeypatch):
    monkeypatch.setattr(notices, "morning", lambda *a, **k: None)
    monkeypatch.setattr(notices, "weekly", lambda *a, **k: None)
    monkeypatch.setattr(notices, "alerts", lambda *a, **k: [])


# --------------------------------------------------------------------------- evaluación


def _market(svc, n=420, *, stock_daily=0.002, start="2025-01-01"):
    days = business_days(start, n)
    stock = [(d, Decimal(str(round(10 * (1 + stock_daily) ** k, 4)))) for k, d in enumerate(days)]
    bench = [(d, Decimal(100)) for d in days]
    satellite.store_history(svc, "EVAL.DE", "EUR", stock, fetched_at="x")
    satellite.store_history(svc, "BENCH.DE", "EUR", bench, fetched_at="x")
    return days, dict(stock)


def test_evaluation_says_when_there_is_not_enough(svc):
    ev = journal.evaluate(svc, today=TODAY)
    assert not ev.ready and "vacío" in ev.lines()[0]
    days, px = _market(svc, n=40)
    for d in days[:5]:  # cinco días seguidos: un solo caso, no cinco
        add_day(svc, d, "neutral", ideas=[{"price": px[d]}])
    ev = journal.evaluate(svc, today=days[-1])
    assert not ev.ready and ev.days == 5 and ev.ideas == 5
    r = next(r for r in ev.results if r.h == 21)
    assert r.n == 1
    lines = ev.lines()
    assert "Aún no hay datos suficientes" in lines[1] and "1 mes" in lines[1]


def test_evaluation_against_index_and_calibration(svc):
    days, px = _market(svc)
    # Una idea de compra cada día hábil durante 300 sesiones; rangos inventados: la mitad con
    # una caída «1 de cada 10» imposible de superar (−90 %) y la otra mitad con una que siempre
    # se supera (+50 %: cualquier mínimo queda por debajo).
    for k, d in enumerate(days[:300]):
        drop = -90.0 if (k // 21) % 2 == 0 else 50.0
        rng = [{"h": 21, "cases": 600, "end_low": -5.0, "end_mid": 1.0, "end_high": 3.0,
                "drop": drop, "rise": 8.0}]  # fmt: skip
        add_day(svc, d, "neutral", ideas=[{"price": px[d], "ranges": rng},
                                           {"price": px[d], "kind": "vigilar", "isin": "XF000EVAL002"}])  # fmt: skip
    ev = journal.evaluate(svc, today=days[-1])
    assert ev.ready
    buy = next(r for r in ev.results if r.kind == "comprar" and r.h == 21)
    # Independientes: una cada 21 sesiones (300 / 21 → 15), y todas cumplidas.
    assert buy.n == 15 and buy.beat == 15
    assert buy.mean_excess == pytest.approx(100 * (1.002**21 - 1), rel=1e-6)
    assert buy.ci and buy.ci[0] > 0
    assert all(not r.ready for r in ev.results if r.h == 252)  # a 12 meses, pocos casos
    cal = next(c for c in ev.calibration if c.h == 21)
    assert cal.n == 15 and cal.deeper_drop == 7  # las de +50 %: k//21 impar → 7 de 15
    assert cal.above_high == 15 and cal.below_low == 0  # +4,3 % en un mes: sobre el +3 %
    text = "\n".join(ev.lines())
    assert "Ideas de compra a 1 mes: +4,3 puntos" in text and "Mejor que el índice" in text
    assert "7 de 15 casos (47 %" in text and "lo ideal, 10 %" in text
    assert "Aún sin casos suficientes a 3 meses ni a 12 meses" in text
    assert "«vigilar»" in text


def test_sell_ideas_read_the_other_way(svc):
    days, px = _market(svc, stock_daily=-0.002)
    for d in days[:300]:
        add_day(svc, d, "neutral", ideas=[{"price": px[d], "kind": "vender"}])
    ev = journal.evaluate(svc, today=days[-1])
    sell = next(r for r in ev.results if r.kind == "vender" and r.h == 21)
    assert sell.mean_excess < 0 and sell.ci[1] < 0
    assert "Vender acertó" in "\n".join(ev.lines())


def test_panel_card_only_when_there_is_something_to_show(prices, svc, monkeypatch):
    from personal_os.ops import panel_tabs

    o = satellite.overview(prices, CFG, today=TODAY, monthly_expense_cents=1000)
    empty = journal.evaluate(prices, today=TODAY)
    html = panel_tabs.tab_ideas(panel_tabs.Extras(satellite=o, journal=empty))
    assert "Acierta el satélite" not in html
    ready = dataclasses.replace(empty, days=30, ideas=30, first_day="2026-01-02",
                                results=[journal.Result("comprar", 21, 12, 1.5, (-2.0, 4.0), 7)])  # fmt: skip
    html = panel_tabs.tab_ideas(panel_tabs.Extras(satellite=o, journal=ready))
    assert "Acierta el satélite" in html and "puede ser suerte" in html


def test_cli_journal_prints_entries_and_evaluation(svc):
    from typer.testing import CliRunner

    from personal_os.cli.main import app as cli

    write_config_template()
    add_day(svc, "2026-10-01", "miedo", ideas=[{"price": "20.00"}])
    res = CliRunner().invoke(cli, ["finance", "journal"])
    assert res.exit_code == 0, res.output
    assert "2026-10-01  mercado miedo" in res.output and "FAKE Eval" in res.output
    assert "Aún no hay datos suficientes" in res.output


def test_sell_idea_is_recorded_without_its_reason(prices, monkeypatch):
    """Una idea de venta (sin señal propia) toma la sesión de su último cierre y no guarda el
    motivo («ganas un…»), que habla de la posición del usuario."""
    o = satellite.overview(prices, CFG, today=TODAY, monthly_expense_cents=1000)
    corp = next(i for i in o.ideas if i.name == "FAKE Corp")
    sell = dataclasses.replace(corp, kind="vender", reason="Ganas un 31 %: objetivo alcanzado.",
                               amount_cents=987654, isin="XF000FAKE099")  # fmt: skip
    o = dataclasses.replace(o, ideas=[sell])
    assert journal.record(prices, o, CFG.benchmark, day=TODAY, recorded_at="x") == 1
    [i] = journal.entries(prices)[0].ideas
    assert i["kind"] == "vender" and i["price_day"] == TODAY
    dump = json.dumps(i)
    assert "Ganas" not in dump and "987654" not in dump


def test_not_ready_says_how_many_cases_once_due(svc):
    days, px = _market(svc, n=80)
    for d in days[:3]:
        add_day(svc, d, "neutral", ideas=[{"price": px[d]}])
    lines = journal.evaluate(svc, today=days[-1]).lines()
    assert "A 1 mes ya hay 1 de 10." in lines[1] and "se cumplen hacia" not in lines[1]
    assert "3 apuntes de ideas sobre 1 valor." in lines[0]
