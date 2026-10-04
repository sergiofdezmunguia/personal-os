"""Identificadores estables con prefijo de tipo: `tsk_01J…`, `occ_01J…`, `evt_01J…`.

El cuerpo es un ULID (48 bits de tiempo en ms + 80 bits aleatorios, Crockford base32):
ordenable por creación, sin coordinación y legible en logs.
"""

from __future__ import annotations

import os
import re
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ID_RE = re.compile(r"^[a-z]{2,5}_[0-9A-HJKMNP-TV-Z]{26}$")

PREFIXES = {
    "task": "tsk",
    "occurrence": "occ",
    "calendar_event": "evt",
    "event": "ev",
    "operation": "op",
    "batch": "bat",
    "run": "run",
    "correlation": "cor",
}


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def ulid(timestamp_ms: int | None = None) -> str:
    ts = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    rand = int.from_bytes(os.urandom(10), "big")
    return _encode(ts, 10) + _encode(rand, 16)


def new_id(kind: str) -> str:
    return f"{PREFIXES[kind]}_{ulid()}"


def is_valid_id(value: str) -> bool:
    return bool(_ID_RE.match(value))
