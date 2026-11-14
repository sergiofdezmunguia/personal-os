"""Sello de fecha del widget (función pura `dataStamp`) con Node, sin Scriptable."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
SCRIPT = Path(__file__).resolve().parents[2] / "bridge/scriptable/personal-os-widget.js"
HARNESS = """
const src = require("fs").readFileSync(process.argv[1], "utf8");
const module = { exports: {} };
// El script usa await en el nivel superior (Scriptable): se ejecuta como función async.
const g = { module, Color: class { static dynamic() { return {}; } }, config: {} };
new (Object.getPrototypeOf(async function () {}).constructor)(...Object.keys(g), src)(...Object.values(g))
  .then(() => {
    const { dataStamp } = module.exports;
    const [now, cases] = JSON.parse(process.argv[2]);
    console.log(JSON.stringify(cases.map((c) => dataStamp(c, new Date(now)))));
  });
"""


def stamps(now: str, cases: list) -> list[dict]:
    out = subprocess.run(
        [NODE, "-e", HARNESS, str(SCRIPT), json.dumps([now, cases])],
        capture_output=True, text=True, check=True,
        env={"TZ": "Europe/Madrid", "PATH": "/usr/bin:/bin"},
    )  # fmt: skip
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="Node.js no disponible")
def test_recent_shows_day_and_time():
    [s] = stamps("2026-11-07T12:00:00+01:00", ["2026-11-07T10:30+01:00"])
    assert s == {"text": "Datos: sáb 7 nov, 10:30", "stale": False}


@pytest.mark.skipif(NODE is None, reason="Node.js no disponible")
def test_threshold_is_36_hours():
    now = "2026-11-08T22:00:00+01:00"
    just_under, at = stamps(now, ["2026-11-07T10:01+01:00", "2026-11-07T10:00+01:00"])
    assert not just_under["stale"]
    assert at == {"text": "Datos antiguos: 7 nov", "stale": True}


@pytest.mark.skipif(NODE is None, reason="Node.js no disponible")
@pytest.mark.parametrize("bad", [None, "", "basura", 12345])
def test_missing_or_bad_date_never_throws(bad):
    [s] = stamps("2026-11-07T12:00:00+01:00", [bad])
    assert s == {"text": "Datos sin fecha", "stale": False}


@pytest.mark.skipif(NODE is None, reason="Node.js no disponible")
def test_summer_offset_and_future_clock_skew():
    summer, future = stamps(
        "2026-07-01T12:00:00+02:00", ["2026-07-01T09:05+02:00", "2026-07-01T13:00+02:00"]
    )
    assert summer["text"] == "Datos: mié 1 jul, 9:05"
    assert not future["stale"]
