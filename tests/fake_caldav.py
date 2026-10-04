"""iCloud Calendar falso: guarda .ics reales y aplica las precondiciones de ETag de CalDAV."""

from __future__ import annotations

import itertools
from dataclasses import replace

from personal_os.adapters.apple.ical import from_ics, to_ics
from personal_os.sync.ports import EventPayload, PreconditionFailed, RemoteEvent, RemoteNotFound

_etags = itertools.count(1)


class FakeCalDav:
    def __init__(self, timezone: str) -> None:
        self.tz = timezone
        self.items: dict[str, tuple[str, str]] = {}  # href -> (etag, ics)
        self.calls: list[str] = []

    def _store(self, href: str, ics: str) -> str:
        etag = f'"{next(_etags)}"'
        self.items[href] = (etag, ics)
        return etag

    # --- CalendarGateway
    def put(self, payload: EventPayload, href: str | None, etag: str | None) -> tuple[str, str]:
        self.calls.append("put")
        if href is None:
            href = f"/123/calendars/pos/{payload.uid.replace('@', '-')}.ics"
            if href in self.items:
                raise PreconditionFailed(href)
        elif href not in self.items:
            raise RemoteNotFound(href)
        elif etag and self.items[href][0] != etag:
            raise PreconditionFailed(href)
        return href, self._store(href, to_ics(payload))

    def delete(self, href: str, etag: str | None) -> None:
        self.calls.append("delete")
        if href in self.items and etag and self.items[href][0] != etag:
            raise PreconditionFailed(href)
        self.items.pop(href, None)

    def get(self, href: str) -> RemoteEvent | None:
        if href not in self.items:
            return None
        etag, ics = self.items[href]
        payload, overrides = from_ics(ics, self.tz)
        return RemoteEvent(href, etag, payload, overrides)

    def list_etags(self) -> dict[str, str]:
        return {h: e for h, (e, _) in self.items.items()}

    def list_all(self) -> list[RemoteEvent]:
        return [self.get(h) for h in self.items]  # type: ignore[misc]

    # --- acciones del "usuario" en el iPhone
    def phone_edit(self, href: str, **changes) -> None:
        current = self.get(href)
        self._store(href, to_ics(replace(current.payload, **changes)))

    def phone_delete(self, href: str) -> None:
        self.items.pop(href)

    def phone_add(self, payload: EventPayload) -> str:
        href = f"/123/calendars/pos/{payload.uid}.ics"
        self._store(href, to_ics(payload))
        return href

    def only(self) -> RemoteEvent:
        (href,) = self.items
        return self.get(href)  # type: ignore[return-value]
