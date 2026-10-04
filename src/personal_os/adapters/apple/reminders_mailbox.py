"""Apple adapter — Recordatorios vía buzón de ficheros en iCloud Drive.

Implementa `RemindersGateway` sobre el protocolo pos-reminders/1
(docs/protocol-reminders-v1.md). El otro extremo es bridge/scriptable/personal-os-sync.js.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from personal_os.sync.ports import (
    MailboxStatus,
    ObservedReminder,
    ReminderAck,
    ReminderCommand,
    ReminderResult,
    RemindersInbox,
    RemindersSnapshot,
    ReminderState,
    ReminderUpsert,
)

PROTOCOL = "pos-reminders/1"
_NAME_RE = re.compile(r"^(batch|ack|snapshot)-([A-Za-z0-9_]+)\.json$")


class MailboxError(Exception):
    pass


def encode_command(cmd: ReminderCommand) -> dict:
    data = {
        "op_id": cmd.op_id,
        "type": cmd.type,
        "marker": cmd.marker,
        "external_id": cmd.external_id,
        "expected": cmd.expected.to_json() if cmd.expected else None,
    }
    if isinstance(cmd, ReminderUpsert):
        data["fields"] = cmd.fields.to_json()
    return data


def decode_ack(data: dict) -> ReminderAck:
    return ReminderAck(
        batch_id=data["batch_id"],
        run_id=data["run_id"],
        processed_at=data["processed_at"],
        results=tuple(
            ReminderResult(
                op_id=r["op_id"],
                status=r["status"],
                marker=r.get("marker"),
                external_id=r.get("external_id"),
                state=ReminderState.from_json(r["state"]) if r.get("state") else None,
                error=r.get("error"),
            )
            for r in data.get("results", [])
        ),
    )


def decode_snapshot(data: dict) -> RemindersSnapshot:
    return RemindersSnapshot(
        run_id=data["run_id"],
        taken_at=data["taken_at"],
        list_name=data.get("list_name", ""),
        list_found=bool(data.get("list_found")),
        list_error=data.get("list_error"),
        device_timezone=data.get("device_timezone"),
        window_days=int(data.get("window_days", 0)),
        applied_batches=tuple(data.get("applied_batches", [])),
        reminders=tuple(
            ObservedReminder(
                external_id=r["external_id"],
                marker=r.get("marker"),
                state=ReminderState.from_json(r["state"]),
                completion_date=r.get("completion_date"),
                creation_date=r.get("creation_date"),
            )
            for r in data.get("reminders", [])
        ),
    )


class MailboxRemindersGateway:
    def __init__(self, root: Path, list_name: str) -> None:
        self.root = Path(root)
        self.list_name = list_name
        self.outbox = self.root / "outbox"
        self.inbox = self.root / "inbox"

    def ensure_dirs(self) -> None:
        if not self.root.parent.exists():
            raise MailboxError(
                f"No existe {self.root.parent}. ¿Está instalado iCloud para Windows y "
                "sincronizada la carpeta de Scriptable?"
            )
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.inbox.mkdir(parents=True, exist_ok=True)

    # --- escritura ---------------------------------------------------------------------

    def submit(self, batch_id: str, commands: Sequence[ReminderCommand]) -> None:
        if not commands:
            return
        self.ensure_dirs()
        body = {
            "protocol": PROTOCOL,
            "batch_id": batch_id,
            "created_at": datetime.now(UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "list_name": self.list_name,
            "commands": [encode_command(c) for c in commands],
        }
        _write_atomic(self.outbox / f"batch-{batch_id}.json", body)

    # --- lectura -----------------------------------------------------------------------

    def _files(self, folder: Path, kind: str) -> list[Path]:
        if not folder.exists():
            return []
        out = []
        for p in folder.iterdir():
            m = _NAME_RE.match(p.name)
            if m and m.group(1) == kind:
                out.append(p)
        return sorted(out)

    def collect(self) -> RemindersInbox:
        acks, snaps, invalid = [], [], []
        for path in self._files(self.inbox, "ack"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("protocol") != PROTOCOL:
                    raise ValueError(f"protocolo {data.get('protocol')!r}")
                acks.append(decode_ack(data))
            except (ValueError, KeyError, OSError) as exc:
                invalid.append(f"{path.name}: {exc}")
        for path in self._files(self.inbox, "snapshot"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("protocol") != PROTOCOL:
                    raise ValueError(f"protocolo {data.get('protocol')!r}")
                snaps.append(decode_snapshot(data))
            except (ValueError, KeyError, OSError) as exc:
                invalid.append(f"{path.name}: {exc}")
        snaps.sort(key=lambda s: (s.taken_at, s.run_id))
        acks.sort(key=lambda a: a.batch_id)
        return RemindersInbox(
            acks=tuple(acks), snapshots=tuple(snaps), invalid_files=tuple(invalid)
        )

    def status(self) -> MailboxStatus:
        return MailboxStatus(
            location=str(self.root),
            pending_batches=tuple(
                _NAME_RE.match(p.name).group(2)  # type: ignore[union-attr]
                for p in self._files(self.outbox, "batch")
            ),
            acks=len(self._files(self.inbox, "ack")),
            snapshots=len(self._files(self.inbox, "snapshot")),
        )


def _write_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
