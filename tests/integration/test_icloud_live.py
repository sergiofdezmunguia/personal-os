"""Integración REAL con iCloud Calendar. Opt-in:

    POS_LIVE=1 uv run pytest tests/integration -m live -v

Usa la configuración y el secreto locales reales (~/.config/personal-os). Crea un evento
temporal en el calendario 'Personal OS', lo modifica y lo borra. No toca la base de datos.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from personal_os.core.ids import ulid
from personal_os.sync.ports import EventPayload, PreconditionFailed

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("POS_LIVE") != "1", reason="POS_LIVE=1 para ejecutar"),
]


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setenv("POS_CONFIG_DIR", str(Path.home() / ".config" / "personal-os"))
    from personal_os.bootstrap import calendar_gateway
    from personal_os.core.config import load_config

    gw = calendar_gateway(load_config())
    yield gw
    gw.close()


def test_icloud_calendar_full_cycle(gateway):
    tz = gateway.timezone
    p = EventPayload(
        uid=f"live-test-{ulid()}@personal-os",
        title="[Personal OS] prueba de integración",
        notes="Evento temporal creado por los tests. Se borra solo.",
        location="",
        start="2030-01-15T10:00",
        end="2030-01-15T10:30",
        all_day=False,
        timezone=tz,
        rrule=None,
        alerts=(10,),
    )
    href, _ = gateway.put(p, None, None)
    try:
        assert href in gateway.list_etags()
        remote = gateway.get(href)
        assert remote is not None
        assert (remote.payload.title, remote.payload.start, remote.payload.end) == (
            p.title,
            p.start,
            p.end,
        )
        assert remote.payload.alerts == (10,)

        from dataclasses import replace

        _, etag2 = gateway.put(replace(p, location="Prueba"), href, remote.etag)
        assert etag2 != remote.etag
        with pytest.raises(PreconditionFailed):
            gateway.put(replace(p, location="Viejo"), href, remote.etag)  # ETag caducado
        assert gateway.get(href).payload.location == "Prueba"
    finally:
        gateway.delete(href, None)
    assert gateway.get(href) is None
