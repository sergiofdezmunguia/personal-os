from __future__ import annotations

from datetime import UTC, datetime

import pytest

from personal_os.cli.wiring import MIGRATIONS
from personal_os.core.clock import FixedClock
from personal_os.core.db import Database
from personal_os.core.events import EventLog
from personal_os.tasks.service import TaskService

TZ = "Atlantic/Canary"


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path, monkeypatch):
    """Ningún test toca ~/.config ni ~/.local del usuario."""
    monkeypatch.setenv("POS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("POS_DATA_DIR", str(tmp_path / "data"))
    for key in list(__import__("os").environ):
        if key.startswith("POS_SECRET_"):
            monkeypatch.delenv(key)


@pytest.fixture
def clock() -> FixedClock:
    # Lunes 5 de octubre de 2026, 09:00 en Canarias (08:00 UTC).
    return FixedClock(datetime(2026, 10, 5, 8, 0, tzinfo=UTC))


@pytest.fixture
def db():
    database = Database(":memory:")
    database.migrate(MIGRATIONS)
    yield database
    database.close()


@pytest.fixture
def events(db, clock) -> EventLog:
    return EventLog(db, clock)


@pytest.fixture
def tasks(db, clock, events) -> TaskService:
    return TaskService(db, clock, events, TZ)
