from __future__ import annotations

import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from personal_os import bootstrap
from personal_os.core.config import write_config_template
from personal_os.core.events import ChangeContext, EventLog
from personal_os.finance.models import FinanceError
from personal_os.mcp_server.server import build_server
from tests.sim_device import SimDevice, requires_node


@pytest.fixture
def device(tmp_path):
    return SimDevice(tmp_path / "iphone", tz="Atlantic/Canary")


@pytest.fixture
def server(device, clock):
    path = write_config_template()
    path.write_text(f'timezone = "Atlantic/Canary"\n[apple]\nmailbox_dir = "{device.mailbox}"\n')
    apps: list[bootstrap.App] = []

    def open_app():
        app = bootstrap.open_app(
            clock=clock
        )  # reloj fijo: el resultado no depende de la fecha real
        apps.append(app)
        return app

    srv = build_server(open_app)
    yield srv
    for a in apps:
        a.db.close()


def call(server, name, **args):
    result = asyncio.run(server.call_tool(name, args))
    assert not result.is_error, result
    return result.structured_content


def test_exposes_minimal_toolset(server):
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert set(tools) == {
        "create_task",
        "list_tasks",
        "complete_task",
        "create_calendar_event",
        "list_calendar_events",
        "sync_apple",
        "list_transactions",
        "finance_summary",
        "list_category_rules",
        "propose_category_rule",
        "categorize_transactions",
        "finance_month_close",
        "investments_overview",
        "finance_recurring",
        "name_unlabeled_transaction",
        "satellite_overview",
        "model_category_suggestions",
    }
    assert tools["list_tasks"].annotations.read_only_hint is True
    assert tools["investments_overview"].annotations.read_only_hint is True


def test_create_list_complete_recurring_task(server):
    created = call(
        server,
        "create_task",
        title="Sacar reciclaje",
        due_date="2026-10-05",
        due_time="20:00",
        repeat="weekly",
        on_days=["MO"],
    )
    assert created["repeat"] == "cada semana (MO)" and created["origin"] == "mcp"
    listed = call(server, "list_tasks")
    assert listed["timezone"] == "Atlantic/Canary" and len(listed["tasks"]) == 1
    done = call(server, "complete_task", task_id=created["id"][:10])
    assert done["next"] == {"due_date": "2026-10-12", "due_time": "20:00"}


def test_changes_are_audited_as_mcp(server):
    created = call(server, "create_task", title="Auditada")
    app = bootstrap.open_app()
    (ev,) = EventLog(app.db, app.clock).query(entity_id=created["id"])
    assert ev.actor == "mcp"


def test_domain_errors_become_tool_errors(server):
    with pytest.raises(ToolError, match="Una hora requiere fecha"):
        asyncio.run(server.call_tool("create_task", {"title": "x", "due_time": "10:00"}))
    with pytest.raises(ToolError, match="No encuentro"):
        asyncio.run(server.call_tool("complete_task", {"task_id": "tsk_NOEXISTE"}))


def test_calendar_event_tools(server):
    ev = call(
        server,
        "create_calendar_event",
        title="Dentista",
        start="2026-10-06T18:00",
        duration_minutes=45,
        alerts_minutes_before=[60, 10],
    )
    assert (ev["ends_at"], ev["alerts"], ev["all_day"]) == ("2026-10-06T18:45", [10, 60], False)
    day = call(server, "create_calendar_event", title="Vacaciones", start="2026-10-10")
    assert day["all_day"] is True and day["ends_at"] == "2026-10-11"
    listed = call(server, "list_calendar_events", from_date="2026-10-01")
    assert [e["title"] for e in listed["events"]] == ["Dentista", "Vacaciones"]


@requires_node
def test_sync_apple_reaches_the_iphone(server, device):
    created = call(server, "create_task", title="Desde Claude", due_date="2026-10-06")
    out = call(server, "sync_apple")
    assert out["reports"][0]["stats"]["commands_sent"] == 1
    device.run()
    call(server, "sync_apple")
    occ_id = call(server, "list_tasks")["tasks"][0]["open_occurrence_id"]
    assert device.by_marker(occ_id)["title"] == "Desde Claude"
    assert created["title"] == "Desde Claude"


def test_default_dates_follow_the_app_clock_not_the_system(server):
    # El reloj fijo dice lunes 5-oct-2026: «hoy» y el mes por defecto salen de él.
    assert call(server, "list_tasks")["today"] == "2026-10-05"
    assert call(server, "list_calendar_events")["today"] == "2026-10-05"
    assert call(server, "finance_summary")["month"] == "2026-10"


def test_satellite_overview_includes_market_reading(clock):
    """Lista inventada y precios sintéticos: el MCP da régimen, vistas y tramos en JSON."""
    from datetime import date, timedelta
    from decimal import Decimal

    from personal_os.finance import market, satellite

    path = write_config_template()
    path.write_text('timezone = "Atlantic/Canary"\n[[finance.satellite.watch]]\n'
                    'isin = "XF000FAKE009"\nsymbol = "FAKE9.DE"\nname = "FAKE Nine"\n')  # fmt: skip
    app = bootstrap.open_app(clock=clock)
    svc = bootstrap.finance_service(app)
    end = date(2026, 10, 5)
    days = [(end - timedelta(days=599 - i)).isoformat() for i in range(600)]
    up = [(d, Decimal(str(round(50 + 50 * i / 599, 2)))) for i, d in enumerate(days)]
    satellite.store_history(svc, "FAKE9.DE", "EUR", up, fetched_at="x")
    satellite.store_history(svc, market.INDEX, "USD", up, fetched_at="x")
    satellite.store_history(svc, market.VIX, "USD", [(days[-1], Decimal(12))], fetched_at="x")
    satellite.store_history(svc, market.VIX3M, "USD", [(days[-1], Decimal(15))], fetched_at="x")
    app.db.close()
    apps = []

    def open_app():
        a = bootstrap.open_app(clock=clock)
        apps.append(a)
        return a

    try:
        out = call(build_server(open_app), "satellite_overview")
    finally:
        for a in apps:
            a.db.close()
    m = out["market"]
    assert m["regime"] == "euforia" and m["evidence"]["fear_edge"] is False
    assert len(m["components"]) == 6 and m["missing"] == [
        "Amplitud (valores sobre su media de 200)"
    ]
    v = out["views"]["XF000FAKE009"]
    assert v["trend"]["long"] == "alcista" and v["entry_from"] == 85.0
    assert "no recomendaciones" in out["disclaimer"]


def test_model_category_suggestions_only_proposes(server, clock):
    """El MCP genera y lista propuestas del modelo, pero no categoriza ni aprueba nada."""
    from tests import categorizer_fake

    app = bootstrap.open_app(clock=clock)
    try:
        svc = bootstrap.finance_service(app)
        examples = categorizer_fake.generate(240, seed=3)
        svc.import_statement(
            ChangeContext.cli(),
            categorizer_fake.statement(examples),
            file_name="f",
            file_sha256="f",
        )
        txs = sorted(
            svc.list_transactions(limit=None), key=lambda t: (t.booking_date, t.description)
        )
        by_key = {(x.booking_date, x.description, x.amount_cents): x.category for x in examples}
        for t in txs[:200]:
            svc.set_category(
                ChangeContext.cli(), t.id, by_key[(t.booking_date, t.description, t.amount_cents)]
            )
    finally:
        app.db.close()
    out = call(server, "model_category_suggestions", refresh=True)
    assert out["model"]["examples"] == 200 and out["refresh"]["candidates"] == 40
    assert out["refresh"]["proposed"] == len(out["suggestions"]) > 0
    first = out["suggestions"][0]
    app = bootstrap.open_app(clock=clock)
    try:
        assert bootstrap.finance_service(app).get_transaction(first["id"]).category is None
        with pytest.raises(FinanceError, match="solo las aprueba"):
            bootstrap.suggestion_service(app).review(
                ChangeContext(actor="mcp"), first["id"], approve=True
            )
    finally:
        app.db.close()
    again = call(server, "model_category_suggestions")
    assert again["suggestions"] == out["suggestions"] and "model" not in again
