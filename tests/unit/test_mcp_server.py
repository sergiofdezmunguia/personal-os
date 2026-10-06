from __future__ import annotations

import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from personal_os import bootstrap
from personal_os.core.config import write_config_template
from personal_os.core.events import EventLog
from personal_os.mcp_server.server import build_server
from tests.sim_device import SimDevice, requires_node


@pytest.fixture
def device(tmp_path):
    return SimDevice(tmp_path / "iphone", tz="Atlantic/Canary")


@pytest.fixture
def server(device):
    path = write_config_template()
    path.write_text(f'timezone = "Atlantic/Canary"\n[apple]\nmailbox_dir = "{device.mailbox}"\n')
    apps: list[bootstrap.App] = []

    def open_app():
        app = bootstrap.open_app()
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
    }
    assert tools["list_tasks"].annotations.read_only_hint is True


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
