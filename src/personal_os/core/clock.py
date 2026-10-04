"""Reloj inyectable. Todo el código obtiene la hora a través de un Clock para que los tests
sean deterministas."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime:
        """Instante actual, siempre timezone-aware en UTC."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Reloj controlable para tests."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FixedClock requiere un datetime con zona horaria")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> None:
        self._now += timedelta(**kwargs)

    def set(self, value: datetime) -> None:
        self._now = value.astimezone(UTC)


def to_iso(value: datetime) -> str:
    """Serialización canónica de instantes: UTC, precisión de segundos, sufijo Z."""
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def local_today(clock: Clock, tz: str) -> str:
    return clock.now().astimezone(ZoneInfo(tz)).date().isoformat()
