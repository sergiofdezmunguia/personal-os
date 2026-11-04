"""Analista de gastos: recurrentes, subidas, duplicados y resúmenes (datos inventados)."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from personal_os import bootstrap
from personal_os.core.config import write_config_template
from personal_os.core.events import ChangeContext
from personal_os.finance import analyst
from personal_os.finance.importers import ParsedStatement, ParsedTransaction

CLI = ChangeContext.cli()
TODAY = "2026-10-05"  # el reloj de los tests


@pytest.fixture
def svc(clock):
    write_config_template()
    application = bootstrap.open_app(clock=clock)
    yield bootstrap.finance_service(application)
    application.db.close()


def _import(svc, items):
    bal, txs = 500000, []
    for day, desc, cents in sorted(items):
        bal += cents
        txs.append(ParsedTransaction(day, day, desc, cents, bal))
    st = ParsedStatement(
        "santander_xls", "santander", "Cuenta Ficticia", "ES0000000000000000001234", "EUR", txs
    )
    svc.import_statement(CLI, st, file_name="f.xls", file_sha256="x")
    for pattern, category in [
        ("super ficticio", "supermercado"), ("streaming", "suscripciones"),
        ("gimnasio", "ocio"), ("luz", "electricidad"), ("seguro", "seguros-salud"),
        ("tienda", "otros-gastos"), ("bar", "restaurantes"),
    ]:  # fmt: skip
        svc.add_rule(CLI, pattern, category)


@pytest.fixture
def data(svc):
    items = [
        ("2025-10-01", "Recibo Seguro Hogar Ficticio", -12000),
        ("2026-10-01", "Recibo Seguro Hogar Ficticio", -13000),  # anual, sube 10 €
        ("2026-10-03", "Compra Tienda Ficticia", -2500),
        ("2026-10-03", "Compra Tienda Ficticia", -2500),  # ¿duplicado?
        ("2026-10-04", "Compra Streaming", -1199),  # sube de 9,99 a 11,99
        ("2026-10-04", "Compra Bar Ficticio", -4000),
        ("2026-10-01", "Recibo Gimnasio Ficticio", -3000),
    ]
    for m, luz in (("06", -4500), ("07", -6000), ("08", -5200), ("09", -4800)):
        items += [
            (f"2026-{m}-01", "Recibo Gimnasio Ficticio", -3000),
            (f"2026-{m}-03", "Compra Super Ficticio", -5000),
            (f"2026-{m}-08", "Compra Streaming", -999),
            (f"2026-{m}-12", "Recibo Luz Ficticia", luz),
            (f"2026-{m}-20", "Compra Super Ficticio", -5000),  # dos al mes: no es cuota
        ]
    _import(svc, items)
    return svc


def test_detects_monthly_and_yearly_recurring(data):
    rec = {r.merchant: r for r in analyst.recurring(data, today=TODAY)}
    assert set(rec) == {"Gimnasio ficticio", "Streaming", "Luz ficticia", "Seguro hogar ficticio"}
    assert rec["Gimnasio ficticio"].cadence == "mensual"
    assert rec["Gimnasio ficticio"].next_day == "2026-10-31"
    assert rec["Seguro hogar ficticio"].cadence == "anual"
    assert rec["Seguro hogar ficticio"].monthly_cents == round(13000 / 12)
    assert rec["Luz ficticia"].variable


def test_price_rise_only_for_stable_amounts(data):
    rec = {r.merchant: r for r in analyst.recurring(data, today=TODAY)}
    assert (rec["Streaming"].previous_cents, rec["Streaming"].rise_cents) == (999, 200)
    assert rec["Seguro hogar ficticio"].rise_cents == 1000
    assert rec["Luz ficticia"].previous_cents is None  # importe variable: no es subida
    assert rec["Gimnasio ficticio"].previous_cents is None


def test_cancelled_subscription_is_inactive(svc):
    _import(svc, [(f"2026-0{m}-08", "Compra Streaming", -999) for m in (3, 4, 5, 6)])
    assert analyst.recurring(svc, today=TODAY) == []
    old = analyst.recurring(svc, today=TODAY, include_inactive=True)
    assert [r.merchant for r in old] == ["Streaming"] and not old[0].active


def test_possible_duplicate_charge(data):
    assert analyst.duplicates(data, today=TODAY) == [
        analyst.Duplicate("2026-10-03", "Tienda ficticia", 2500, 2)
    ]


def test_week_summary(data):
    w = analyst.week(data, last_day="2026-10-04")
    s = w.spend
    assert (s.first_day, s.last_day) == ("2026-09-28", "2026-10-04")
    # gimnasio 30 + seguro 130 + tienda 25×2 + streaming 11,99 + bar 40
    assert s.expense_cents == 3000 + 13000 + 5000 + 1199 + 4000
    assert s.top[0] == ("Seguro hogar ficticio", 13000)
    assert {r.merchant for r in w.rises} == {"Seguro hogar ficticio", "Streaming"}
    assert w.habit_cents > 0


# ------------------------------------------------------------------ avisos para el iPhone

TZ = ZoneInfo("Atlantic/Canary")


def _notices(svc, when: datetime):
    from personal_os.finance import insights
    from personal_os.ops import notices

    panel = insights.build(svc, today=when.date().isoformat())
    return {n["id"].split(":")[0]: n for n in notices.build(svc, panel, when)}


def test_morning_notice_with_yesterday_and_pace(data):
    got = _notices(data, datetime(2026, 10, 5, 7, 10, tzinfo=TZ))
    n = got["dia"]
    assert n["id"] == "dia:2026-10-05" and n["agenda"] is True
    # ayer (4/10): streaming 11,99 + bar 40
    assert n["body"].startswith(
        "Ayer: 51,99 € en 2 pagos (Bar ficticio 40,00 €, Streaming 11,99 €)."
    )
    assert "En octubre llevas" in n["body"]
    assert (n["deliver_after"], n["expires"]) == (
        "2026-10-05T06:00:00Z",
        "2026-10-05T11:30:00Z",
    )  # UTC+1
    # Generado por la tarde ya no toca: los datos de ayer llegaron con la sync de la mañana.
    assert "dia" not in _notices(data, datetime(2026, 10, 5, 19, 10, tzinfo=TZ))


def test_alerts_for_rise_and_duplicate(data):
    got = _notices(data, datetime(2026, 10, 5, 7, 10, tzinfo=TZ))
    assert set(got) == {"dia", "subida", "duplicado"}
    assert got["subida"]["body"] == (
        "Streaming te ha cobrado 11,99 € (antes 9,99 €): 24,00 € más al año."
    )
    assert (
        got["duplicado"]["body"] == "2 cargos de 25,00 € en Tienda ficticia el 3/10. ¿Es correcto?"
    )


def test_weekly_notice_only_on_sunday(data):
    assert "semana" not in _notices(data, datetime(2026, 10, 3, 19, 10, tzinfo=TZ))
    n = _notices(data, datetime(2026, 10, 4, 19, 10, tzinfo=TZ))["semana"]
    assert n["id"] == "semana:2026-10-04"
    assert n["body"].startswith("Esta semana: 262 €")
    assert "⚠️ Streaming ha subido a 11,99 €." in n["body"]


def test_notices_file_is_written(tmp_path):
    from personal_os.ops import notices

    path = notices.write(tmp_path, [{"id": "x"}], generated_at="2026-10-05T07:10")
    assert json.loads(path.read_text())["notices"] == [{"id": "x"}]


# ------------------------------------------------------------------ MCP


def _mcp(svc):
    import asyncio

    from personal_os.mcp_server.server import build_server

    server = build_server(lambda: _APP[0])

    def call(name, **args):
        result = asyncio.run(server.call_tool(name, args))
        if result.is_error:
            raise RuntimeError(result.content[0].text)
        return result.structured_content

    return call


_APP: list = []


@pytest.fixture
def mcp(clock, data):
    _APP[:] = [bootstrap.open_app(clock=clock)]
    yield _mcp(data)
    _APP[0].db.close()


def test_mcp_recurring(mcp):
    out = mcp("finance_recurring")
    assert {r["merchant"] for r in out["recurring"]} >= {"Streaming", "Gimnasio ficticio"}
    assert out["duplicates"][0]["merchant"] == "Tienda ficticia"


def test_mcp_names_only_unlabeled_transactions(mcp, data):
    _import_tr(data)
    tid = next(t.id for t in data.list_transactions(limit=100) if "sin concepto" in t.description)
    out = mcp(
        "name_unlabeled_transaction", transaction_id=tid, merchant="  Tienda   Online Ficticia "
    )
    assert out["description"] == "Tienda Online Ficticia"
    with pytest.raises(Exception, match="ya tiene concepto"):
        mcp("name_unlabeled_transaction", transaction_id=tid, merchant="Otra")


def _import_tr(svc):
    st = ParsedStatement(
        "enablebanking", "trade_republic", "TR Ficticia", "", "EUR",
        [ParsedTransaction("2026-10-04", None, "Trade Republic · sin concepto", -2999, 10000, "prov:x")],
        account_kind="broker",
    )  # fmt: skip
    svc.import_statement(CLI, st, file_name="eb", file_sha256="eb")
