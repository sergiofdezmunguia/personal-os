"""Script de Scriptable «Apuntar»: el intérprete de frases en español (parseEvent) con Node."""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.sim_device import NODE, REPO

SCRIPT = REPO / "bridge" / "scriptable" / "apuntar.js"
HARNESS = """
// El script usa `await` de primer nivel (como Scriptable): se evalúa dentro de una función async.
const src = require("fs").readFileSync(process.argv[1], "utf8");
const module = { exports: {} };
new (Object.getPrototypeOf(async function () {}).constructor)("module", src)(module);
const { parseEvent, describe } = module.exports;
const now = new Date(2026, 10, 2, 22, 0);  // lunes 2 de noviembre de 2026
const out = JSON.parse(process.argv[2]).map((s) => {
  const p = parseEvent(s, now);
  return p.error ? "ERROR" : describe(p);
});
console.log(JSON.stringify(out));
"""
CASES = {
    "tengo médico el martes 24 a las 12.30": "🩺 Médico · martes 24 de noviembre, 12:30–13:30",
    "cine con Laura el viernes a las 9 y media de la noche": "🎬 Cine con Laura · viernes 6 de noviembre, 21:30–0:00",
    "vuelo a Madrid mañana a las 7:15": "✈️ Vuelo a Madrid · martes 3 de noviembre, 7:15–9:15",
    "cumple de mamá el 15 de diciembre": "🎂 Cumple de mamá · martes 15 de diciembre (todo el día)",
    "pádel el jueves de 19 a 20:30": "🏋️ Pádel · jueves 5 de noviembre, 19:00–20:30",
    "reunión con el banco pasado mañana a las 10": "💼 Reunión con el banco · miércoles 4 de noviembre, 10:00–11:00",
    "peluquería el 4 a las 5": "💈 Peluquería · miércoles 4 de noviembre, 17:00–18:00",
    "ITV el 20/11 a las 9:00": "📌 ITV · viernes 20 de noviembre, 9:00–10:00",
    "comida familiar el domingo a mediodía": "🍽️ Comida familiar · domingo 8 de noviembre, 14:00–16:00",
    "tengo que ir al dentista el lunes a las 16:00 durante 2 horas": "🩺 Dentista · lunes 9 de noviembre, 16:00–18:00",
    "concierto el 30 de enero a las 21h": "🎵 Concierto · sábado 30 de enero, 21:00–0:00",
    "el lunes 9 tenía festivo": "🎉 Festivo · lunes 9 de noviembre (todo el día)",
    "el viernes 13 es el puente": "🎉 Puente · viernes 13 de noviembre (todo el día)",
    "algo raro sin fecha": "ERROR",
    "el martes 25 cena": "ERROR",  # no hay martes 25 en los próximos 3 meses: mejor preguntar
}


@pytest.mark.skipif(NODE is None, reason="Node.js no disponible")
def test_script_awaits_main_for_shortcuts():
    # Sin `await`, Atajos da el script por terminado sin evento ni respuesta.
    src = SCRIPT.read_text(encoding="utf-8")
    assert "else await main();" in src
    # Desde Atajos la frase llega en Texts (plainTexts) o en Parameter: nunca hay ventanas.
    assert "(args.plainTexts || []).length > 0" in src and "!config.runsInApp" in src


def test_parse_spanish_phrases():
    phrases = list(CASES)
    out = subprocess.run(
        [NODE, "-e", HARNESS, str(SCRIPT), json.dumps(phrases)],
        capture_output=True,
        text=True,
        check=True,
        env={"TZ": "Europe/Madrid", "PATH": "/usr/bin:/bin"},
    )
    got = dict(zip(phrases, json.loads(out.stdout), strict=True))
    assert got == CASES


SHORTCUT_SIM = """
const src = require("fs").readFileSync(process.argv[1], "utf8");
const saved = []; let output = null, alerts = 0;
const g = {
  args: { plainTexts: [process.argv[2]], shortcutParameter: null },
  config: { runsInApp: false, runsWithSiri: false, runsInActionExtension: false },
  Script: { setShortcutOutput: (v) => { output = v; }, complete: () => {} },
  CalendarEvent: class { save() { saved.push(this.title); } },
  Calendar: {}, Alert: class { constructor() { alerts++; } }, module: undefined,
};
new (Object.getPrototypeOf(async function () {}).constructor)(...Object.keys(g), src)(
  ...Object.values(g)).then(() => console.log(JSON.stringify({ saved, output, alerts })));
"""


@pytest.mark.skipif(NODE is None, reason="Node.js no disponible")
def test_from_shortcuts_saves_and_answers_without_ui():
    out = subprocess.run(
        [NODE, "-e", SHORTCUT_SIM, str(SCRIPT), "tengo médico el martes 24 a las 12:30"],
        capture_output=True,
        text=True,
        check=True,
        env={"TZ": "Europe/Madrid", "PATH": "/usr/bin:/bin"},
    )
    got = json.loads(out.stdout)
    assert got["saved"] == ["🩺 Médico"] and got["alerts"] == 0
    assert got["output"].startswith("Apuntado: 🩺 Médico")
