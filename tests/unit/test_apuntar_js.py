"""Script de Scriptable «Apuntar»: el intérprete de frases en español (parseEvent) con Node."""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.sim_device import NODE, REPO

SCRIPT = REPO / "bridge" / "scriptable" / "apuntar.js"
HARNESS = """
const { parseEvent, describe } = require(process.argv[1]);
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
    "algo raro sin fecha": "ERROR",
    "el martes 25 cena": "ERROR",  # no hay martes 25 en los próximos 3 meses: mejor preguntar
}


@pytest.mark.skipif(NODE is None, reason="Node.js no disponible")
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
