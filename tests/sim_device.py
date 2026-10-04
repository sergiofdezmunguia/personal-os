"""Un "iPhone" de pruebas: ejecuta el bridge real (JS) en el simulador de Scriptable y permite
simular acciones del usuario sobre Recordatorios."""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO = Path(__file__).resolve().parents[1]
SIM = REPO / "bridge" / "scriptable" / "sim" / "scriptable-sim.mjs"
BRIDGE = REPO / "bridge" / "scriptable" / "personal-os-sync.js"
NODE = shutil.which("node")

requires_node = pytest.mark.skipif(NODE is None, reason="Node.js no disponible")


class SimDevice:
    def __init__(self, root: Path, tz: str = "Atlantic/Canary", list_name: str = "Personal OS"):
        self.root = root
        self.tz = tz
        self.mailbox = root / "icloud" / "personal-os"
        (root / "icloud").mkdir(parents=True, exist_ok=True)
        self._save({"calendars": [{"identifier": "CAL-POS", "title": list_name}], "reminders": []})

    # ------------------------------------------------------------- ejecución del bridge

    def run(self, *, expect_error: bool = False) -> str:
        out = subprocess.run(
            [NODE, str(SIM), "--root", str(self.root), "--script", str(BRIDGE)],
            capture_output=True,
            text=True,
            env={"TZ": self.tz, "PATH": "/usr/bin:/bin"},
            check=True,
            timeout=30,
        )
        assert ("ERROR" in out.stdout) == expect_error, out.stdout
        return out.stdout.strip()

    # ------------------------------------------------------------- estado

    def _load(self) -> dict:
        return json.loads((self.root / "store.json").read_text())

    def _save(self, store: dict) -> None:
        (self.root / "store.json").write_text(json.dumps(store, indent=2))

    def reminders(self) -> list[dict]:
        return self._load()["reminders"]

    def by_marker(self, marker: str) -> dict | None:
        for r in self.reminders():
            if f"[pos:{marker}]" in (r.get("notes") or ""):
                return r
        return None

    def local_due(self, r: dict) -> tuple[str, str | None] | None:
        if not r.get("dueDate"):
            return None
        d = datetime.fromisoformat(r["dueDate"].replace("Z", "+00:00")).astimezone(
            ZoneInfo(self.tz)
        )
        return d.date().isoformat(), (d.strftime("%H:%M") if r["dueDateIncludesTime"] else None)

    # ------------------------------------------------------------- acciones del usuario

    def _mutate(self, marker: str, fn) -> None:
        store = self._load()
        for r in store["reminders"]:
            if f"[pos:{marker}]" in (r.get("notes") or ""):
                fn(r)
                self._save(store)
                return
        raise AssertionError(f"No hay recordatorio con marcador {marker}")

    def complete(self, marker: str, done: bool = True) -> None:
        def fn(r):
            r["isCompleted"] = done
            r["completionDate"] = datetime.now().astimezone().isoformat() if done else None

        self._mutate(marker, fn)

    def edit_title(self, marker: str, title: str) -> None:
        self._mutate(marker, lambda r: r.update(title=title))

    def set_due(self, marker: str, date: str, time: str | None) -> None:
        def fn(r):
            local = datetime.fromisoformat(f"{date}T{time or '00:00'}").replace(
                tzinfo=ZoneInfo(self.tz)
            )
            r["dueDate"] = local.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")
            r["dueDateIncludesTime"] = time is not None

        self._mutate(marker, fn)

    def delete(self, marker: str) -> None:
        store = self._load()
        store["reminders"] = [
            r for r in store["reminders"] if f"[pos:{marker}]" not in (r.get("notes") or "")
        ]
        self._save(store)

    def add_manual(self, title: str, notes: str = "") -> str:
        store = self._load()
        ident = str(uuid.uuid4()).upper()
        store["reminders"].append(
            {
                "identifier": ident,
                "calendar": "CAL-POS",
                "title": title,
                "notes": notes,
                "dueDate": None,
                "dueDateIncludesTime": True,
                "isCompleted": False,
                "completionDate": None,
                "creationDate": datetime.now().astimezone().isoformat(),
            }
        )
        self._save(store)
        return ident

    def change_identifier(self, marker: str) -> str:
        """Simula que EventKit reasigna el identifier (resincronización completa)."""
        new = str(uuid.uuid4()).upper()
        self._mutate(marker, lambda r: r.update(identifier=new))
        return new

    def drop_acks(self) -> None:
        for p in (self.mailbox / "inbox").glob("ack-*.json"):
            p.unlink()
