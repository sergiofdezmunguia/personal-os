"""Cliente CalDAV contra un servidor simulado (httpx.MockTransport) con respuestas tipo iCloud."""

from __future__ import annotations

import httpx
import pytest

from personal_os.adapters.apple.calendar_caldav import CalDavCalendarGateway, CalDavError
from personal_os.core.secrets import Secret
from personal_os.sync.ports import EventPayload, PreconditionFailed
from tests.conftest import TZ

HOME = "https://p52-caldav.icloud.com/123/calendars/"
CAL = HOME + "ABC-POS/"


def multistatus(*responses: str) -> str:
    return (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        + "".join(responses)
        + "</d:multistatus>"
    )


class Server:
    def __init__(self, calendars=(("Personal OS", "ABC-POS/"), ("Trabajo", "WORK/"))):
        self.calendars = calendars
        self.objects: dict[str, tuple[str, str]] = {}
        self.n = 0
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.headers.get("authorization") is None:
            return httpx.Response(401)
        url, m = str(req.url), req.method
        if m == "PROPFIND" and url == "https://caldav.icloud.com/":
            return httpx.Response(
                207,
                text=multistatus(
                    "<d:response><d:href>/</d:href><d:propstat><d:prop><d:current-user-principal><d:href>/123/principal/</d:href></d:current-user-principal></d:prop></d:propstat></d:response>"
                ),
            )
        if m == "PROPFIND" and url.endswith("/123/principal/"):
            return httpx.Response(
                207,
                text=multistatus(
                    f"<d:response><d:href>/123/principal/</d:href><d:propstat><d:prop><c:calendar-home-set><d:href>{HOME}</d:href></c:calendar-home-set></d:prop></d:propstat></d:response>"
                ),
            )
        if m == "PROPFIND" and url == HOME:
            items = "".join(
                f'<d:response><d:href>/123/calendars/{path}</d:href><d:propstat><d:prop><d:displayname>{name}</d:displayname><d:resourcetype><d:collection/><c:calendar/></d:resourcetype><c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set></d:prop></d:propstat></d:response>'
                for name, path in self.calendars
            )
            return httpx.Response(207, text=multistatus(items))
        if m == "PROPFIND" and url == CAL:
            items = "".join(
                f"<d:response><d:href>{h}</d:href><d:propstat><d:prop><d:getetag>{e}</d:getetag></d:prop></d:propstat></d:response>"
                for h, (e, _) in self.objects.items()
            )
            return httpx.Response(207, text=multistatus(items))
        path = req.url.path
        if m == "PUT":
            if req.headers.get("if-none-match") == "*" and path in self.objects:
                return httpx.Response(412)
            if (
                "if-match" in req.headers
                and self.objects.get(path, ("",))[0] != req.headers["if-match"]
            ):
                return httpx.Response(412)
            self.n += 1
            self.objects[path] = (f'"e{self.n}"', req.content.decode())
            return httpx.Response(201, headers={"ETag": f'"e{self.n}"'})
        if m == "GET":
            if path not in self.objects:
                return httpx.Response(404)
            etag, body = self.objects[path]
            return httpx.Response(
                200, text=body, headers={"ETag": etag, "Content-Type": "text/calendar"}
            )
        if m == "DELETE":
            if (
                "if-match" in req.headers
                and self.objects.get(path, ("",))[0] != req.headers["if-match"]
            ):
                return httpx.Response(412)
            self.objects.pop(path, None)
            return httpx.Response(204)
        return httpx.Response(405)


def gateway(server: Server, name="Personal OS") -> CalDavCalendarGateway:
    return CalDavCalendarGateway(
        "https://caldav.icloud.com",
        "me@icloud.com",
        Secret("pw"),
        name,
        TZ,
        transport=httpx.MockTransport(server),
    )


P = EventPayload(
    "evt_1@personal-os",
    "Dentista",
    "",
    "",
    "2026-10-06T18:00",
    "2026-10-06T19:00",
    False,
    TZ,
    None,
    (15,),
)


def test_discovers_calendar_by_name():
    assert gateway(Server()).calendar_url == CAL


def test_missing_calendar_is_explicit_error():
    with pytest.raises(CalDavError, match="No existe un calendario"):
        _ = gateway(Server(), name="Otro").calendar_url


def test_put_get_list_delete_with_etags():
    server = Server()
    gw = gateway(server)
    href, etag = gw.put(P, None, None)
    assert href == "/123/calendars/ABC-POS/evt_1-personal-os.ics" and etag == '"e1"'
    assert server.requests[-1].headers["if-none-match"] == "*"
    assert gw.list_etags() == {href: etag}
    got = gw.get(href)
    assert got.payload == P and got.etag == etag
    _, etag2 = gw.put(P, href, etag)
    with pytest.raises(PreconditionFailed):
        gw.put(P, href, etag)  # ETag viejo
    with pytest.raises(PreconditionFailed):
        gw.delete(href, etag)
    gw.delete(href, etag2)
    assert gw.get(href) is None


def test_auth_failure_message():
    class Deny(Server):
        def __call__(self, req):
            return httpx.Response(401)

    with pytest.raises(CalDavError, match="credenciales"):
        _ = gateway(Deny()).calendar_url


def test_password_never_in_repr():
    gw = gateway(Server())
    assert "pw" not in repr(gw.__dict__)
