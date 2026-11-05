"""Script de Scriptable «Personal OS Avisos»: qué avisos muestra y cuándo (Node)."""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.sim_device import NODE, REPO

SCRIPT = REPO / "bridge" / "scriptable" / "avisos.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js no disponible")

# Simula Scriptable: buzón en iCloud, estado local, calendario, recordatorios y notificaciones.
SIM = """
const src = require("fs").readFileSync(process.argv[1], "utf8");
const input = JSON.parse(process.argv[2]);
const files = { "/icloud/personal-os/panel/avisos.json": JSON.stringify({ notices: input.notices }) };
if (input.state) files["/local/personal-os-avisos.json"] = JSON.stringify(input.state);
const shown = []; let output = null;
const fm = (root) => ({
  documentsDirectory: () => root, joinPath: (a, b) => a + "/" + b,
  fileExists: (p) => p in files, readString: (p) => files[p],
  writeString: (p, s) => { files[p] = s; }, downloadFileFromiCloud: async () => {},
});
const g = {
  module: { exports: {} },
  args: {},
  config: { runsInApp: false },
  Script: { name: () => input.scriptName, setShortcutOutput: (v) => { output = v; }, complete: () => {} },
  FileManager: { iCloud: () => fm("/icloud"), local: () => fm("/local") },
  Notification: class { async schedule() { shown.push({ title: this.title, body: this.body, id: this.identifier }); } },
  CalendarEvent: { today: async () => input.events.map((e) => ({ ...e, startDate: new Date(e.startDate) })) },
  Reminder: { allDueToday: async () => input.tasks },
  Date: class extends Date { constructor(...a) { super(...(a.length ? a : [input.now])); } },
};
new (Object.getPrototypeOf(async function () {}).constructor)(...Object.keys(g), src)(
  ...Object.values(g)).then(async () => {
    if (input.scriptName !== "Personal OS Avisos") await g.module.exports.deliver(new Date(input.now));
    console.log(JSON.stringify({ shown, output, state: JSON.parse(files["/local/personal-os-avisos.json"] || "null") }));
  });
"""

MORNING = {
    "id": "dia:2026-11-05",
    "title": "☀️ Buenos días",
    "body": "Ayer: 12,00 € en 1 pago (Bar inventado 12,00 €).",
    "deliver_after": "2026-11-05T07:00:00Z",
    "expires": "2026-11-05T12:30:00Z",
    "agenda": True,
}
ALERT = {
    "id": "subida:streaming:2026-11-04",
    "title": "📈 Sube una cuota",
    "body": "Streaming inventado sube.",
    "deliver_after": "2026-11-04T08:00:00Z",
    "expires": "2026-11-07T08:00:00Z",
    "agenda": False,
}
EVENTS = [
    {"title": "🩺 Médico", "isAllDay": False, "startDate": "2026-11-05T12:30:00Z"},
    {"title": "🎉 Festivo", "isAllDay": True, "startDate": "2026-11-05T00:00:00Z"},
]


def run(now, *, notices=(MORNING, ALERT), state=None, script="Personal OS Avisos"):
    payload = {
        "now": now,
        "notices": list(notices),
        "state": state,
        "scriptName": script,
        "events": EVENTS,
        "tasks": [{"title": "Tarea inventada"}],
    }
    out = subprocess.run(
        [NODE, "-e", SIM, str(SCRIPT), json.dumps(payload)],
        capture_output=True,
        text=True,
        check=True,
        env={"TZ": "UTC", "PATH": "/usr/bin:/bin"},
    )
    return json.loads(out.stdout)


def test_shows_due_notices_once_with_agenda():
    got = run("2026-11-05T08:00:00Z")
    assert [n["title"] for n in got["shown"]] == ["☀️ Buenos días", "📈 Sube una cuota"]
    morning = got["shown"][0]["body"]
    assert morning.endswith("Hoy: 🎉 Festivo · 12:30 🩺 Médico · 1 tarea")
    assert got["output"] == "Personal OS: 2 avisos"
    again = run("2026-11-05T14:00:00Z", state=got["state"])
    assert again["shown"] == [] and again["output"] == "Personal OS: sin avisos nuevos"


def test_respects_the_window():
    early = run("2026-11-05T06:00:00Z", notices=[MORNING])
    assert early["shown"] == []
    late = run("2026-11-05T13:00:00Z", notices=[MORNING])  # caducado: ya no se muestra
    assert late["shown"] == [] and late["state"]["delivered"] == {}


def test_imported_from_sync_does_not_run_main():
    # «Personal OS Sync» lo importa y llama a deliver(): no debe completar el script.
    got = run("2026-11-05T08:00:00Z", notices=[ALERT], script="Personal OS Sync")
    assert [n["id"] for n in got["shown"]] == ["pos-subida:streaming:2026-11-04"]
    assert got["output"] is None


def test_old_state_is_pruned():
    got = run(
        "2026-11-05T08:00:00Z", notices=[], state={"delivered": {"viejo": "2026-09-01T00:00:00Z"}}
    )
    assert got["state"]["delivered"] == {}
