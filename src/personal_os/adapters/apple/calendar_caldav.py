"""Apple adapter — iCloud Calendar vía CalDAV (RFC 4791).

Cliente mínimo y explícito sobre httpx: descubrimiento (principal → calendar-home →
calendario por nombre) y operaciones con precondiciones (If-Match / If-None-Match) para
detectar cambios concurrentes. Implementa `CalendarGateway`.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import quote, urljoin

import httpx

from personal_os.adapters.apple.ical import from_ics, to_ics
from personal_os.core.secrets import Secret
from personal_os.sync.ports import EventPayload, PreconditionFailed, RemoteEvent, RemoteNotFound

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav", "cs": "http://calendarserver.org/ns/"}

_PROPFIND_PRINCIPAL = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>"""

_PROPFIND_HOME = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:prop><c:calendar-home-set/></d:prop></d:propfind>"""

_PROPFIND_CALENDARS = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:prop><d:displayname/><d:resourcetype/><c:supported-calendar-component-set/></d:prop>
</d:propfind>"""

_PROPFIND_ETAGS = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/><d:resourcetype/></d:prop></d:propfind>"""

_REPORT_ALL = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:prop><d:getetag/><c:calendar-data/></d:prop>
<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"/></c:comp-filter></c:filter>
</c:calendar-query>"""


class CalDavError(Exception):
    pass


class CalDavCalendarGateway:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: Secret,
        calendar_name: str,
        timezone: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.calendar_name = calendar_name
        self.timezone = timezone
        self._client = httpx.Client(
            auth=httpx.BasicAuth(username, password.reveal()),
            follow_redirects=True,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": "personal-os/0.1"},
        )
        self._calendar_url: str | None = None

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ HTTP

    def _request(
        self, method: str, url: str, *, body: str | None = None, headers=None
    ) -> httpx.Response:
        try:
            resp = self._client.request(method, url, content=body, headers=headers or {})
        except httpx.HTTPError as exc:
            raise CalDavError(f"{method} {url}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise CalDavError(
                f"iCloud rechazó las credenciales ({resp.status_code}). Revisa apple_id y la "
                "contraseña específica de app (`pos secrets set icloud_app_password`)."
            )
        return resp

    def _propfind(self, url: str, body: str, depth: str) -> list[ET.Element]:
        resp = self._request(
            "PROPFIND",
            url,
            body=body,
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
        )
        if resp.status_code != 207:
            raise CalDavError(f"PROPFIND {url} → {resp.status_code}")
        return ET.fromstring(resp.content).findall("d:response", NS)

    # ------------------------------------------------------------------ descubrimiento

    @property
    def calendar_url(self) -> str:
        if self._calendar_url is None:
            self._calendar_url = self._discover()
        return self._calendar_url

    def _discover(self) -> str:
        (resp,) = self._propfind(self.base_url, _PROPFIND_PRINCIPAL, "0")[:1]
        principal = resp.find(".//d:current-user-principal/d:href", NS)
        if principal is None or not principal.text:
            raise CalDavError("No se encontró current-user-principal")
        principal_url = urljoin(self.base_url, principal.text)

        (resp,) = self._propfind(principal_url, _PROPFIND_HOME, "0")[:1]
        home = resp.find(".//c:calendar-home-set/d:href", NS)
        if home is None or not home.text:
            raise CalDavError("No se encontró calendar-home-set")
        home_url = urljoin(principal_url, home.text)

        matches = []
        for r in self._propfind(home_url, _PROPFIND_CALENDARS, "1"):
            if r.find(".//d:resourcetype/c:calendar", NS) is None:
                continue
            name = r.findtext(".//d:displayname", default="", namespaces=NS)
            comps = {
                c.get("name") for c in r.findall(".//c:supported-calendar-component-set/c:comp", NS)
            }
            if name == self.calendar_name and (not comps or "VEVENT" in comps):
                matches.append(urljoin(home_url, r.findtext("d:href", namespaces=NS)))
        if not matches:
            raise CalDavError(
                f"No existe un calendario de iCloud llamado '{self.calendar_name}'. Créalo en la app Calendario."
            )
        if len(matches) > 1:
            raise CalDavError(f"Hay {len(matches)} calendarios llamados '{self.calendar_name}'")
        return matches[0]

    # ------------------------------------------------------------------ CalendarGateway

    def put(self, payload: EventPayload, href: str | None, etag: str | None) -> tuple[str, str]:
        body = to_ics(payload)
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if href is None:
            href = urljoin(self.calendar_url, quote(payload.uid.replace("@", "-")) + ".ics")
            headers["If-None-Match"] = "*"
        elif etag:
            headers["If-Match"] = etag
        url = urljoin(self.calendar_url, href)
        resp = self._request("PUT", url, body=body, headers=headers)
        if resp.status_code == 412:
            raise PreconditionFailed(href)
        if resp.status_code == 404:
            raise RemoteNotFound(href)
        if resp.status_code not in (200, 201, 204):
            raise CalDavError(f"PUT {href} → {resp.status_code}: {resp.text[:300]}")
        new_etag = resp.headers.get("ETag") or self._etag_of(url)
        return _path(url), new_etag

    def delete(self, href: str, etag: str | None) -> None:
        headers = {"If-Match": etag} if etag else {}
        resp = self._request("DELETE", urljoin(self.calendar_url, href), headers=headers)
        if resp.status_code == 412:
            raise PreconditionFailed(href)
        if resp.status_code not in (200, 204, 404):
            raise CalDavError(f"DELETE {href} → {resp.status_code}")

    def get(self, href: str) -> RemoteEvent | None:
        resp = self._request("GET", urljoin(self.calendar_url, href))
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise CalDavError(f"GET {href} → {resp.status_code}")
        payload, overrides = from_ics(resp.text, self.timezone)
        etag = resp.headers.get("ETag") or self._etag_of(urljoin(self.calendar_url, href))
        return RemoteEvent(href=_path(href), etag=etag, payload=payload, has_overrides=overrides)

    def list_etags(self) -> dict[str, str]:
        out = {}
        for r in self._propfind(self.calendar_url, _PROPFIND_ETAGS, "1"):
            href = r.findtext("d:href", namespaces=NS)
            etag = r.findtext(".//d:getetag", namespaces=NS)
            if href and etag and href.endswith(".ics"):
                out[_path(href)] = etag
        return out

    def list_all(self) -> list[RemoteEvent]:
        resp = self._request(
            "REPORT",
            self.calendar_url,
            body=_REPORT_ALL,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        )
        if resp.status_code != 207:
            raise CalDavError(f"REPORT → {resp.status_code}")
        events = []
        for r in ET.fromstring(resp.content).findall("d:response", NS):
            href = r.findtext("d:href", namespaces=NS)
            data = r.findtext(".//c:calendar-data", namespaces=NS)
            etag = r.findtext(".//d:getetag", namespaces=NS)
            if href and data:
                payload, overrides = from_ics(data, self.timezone)
                events.append(RemoteEvent(_path(href), etag or "", payload, overrides))
        return events

    def _etag_of(self, url: str) -> str:
        responses = self._propfind(url, _PROPFIND_ETAGS, "0")
        etag = responses[0].findtext(".//d:getetag", namespaces=NS) if responses else None
        if not etag:
            raise CalDavError(f"Sin ETag para {url}")
        return etag


def _path(href: str) -> str:
    """Normaliza un href a su ruta (los servidores mezclan URLs absolutas y relativas)."""
    if href.startswith("http"):
        from urllib.parse import urlparse

        return urlparse(href).path
    return href
