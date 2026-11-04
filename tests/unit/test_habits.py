"""Registro diario de Apple Salud (datos inventados)."""

from __future__ import annotations

from personal_os.habits import health

LOG = """2026-11-01; pasos=8.423; ejercicio=45; sueño=7,2; kcal=2.100,0; peso=72,5
2026-11-02; Pasos: 3100 ; ejercicio= 0; sueno=6.5
basura sin fecha
2026-11-03; pasos=12000; ejercicio=60; sueño=8; cosa_rara=3
2026-11-03; pasos=12500; ejercicio=60; sueño=8
2026-13-40; pasos=1
2026-11-04;
"""


def test_parse_tolerates_shortcut_formats():
    days, bad = health.parse(LOG)
    assert bad == 3  # sin fecha, fecha imposible y línea vacía de valores
    assert days["2026-11-01"].values == {
        "pasos": 8423, "ejercicio": 45, "sueno": 7.2, "kcal": 2100, "peso": 72.5,
    }  # fmt: skip
    assert days["2026-11-02"].values == {"pasos": 3100, "ejercicio": 0, "sueno": 6.5}
    assert days["2026-11-03"].get("pasos") == 12500  # gana la última línea del día


def test_summary_averages_and_streaks():
    days, _ = health.parse(LOG)
    s = health.summarize(days, today="2026-11-04", n=7)
    assert [d.day for d in s.days][-1] == "2026-11-03" and len(s.days) == 7
    assert s.averages["pasos"] == round((8423 + 3100 + 12500) / 3, 1)
    assert s.streaks == {"pasos": 1, "ejercicio": 1, "sueno": 1}
