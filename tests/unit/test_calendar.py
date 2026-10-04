from __future__ import annotations

import pytest

from personal_os.adapters.apple.ical import from_ics, to_ics
from personal_os.calendar.models import CalendarError
from personal_os.calendar.service import CalendarService
from personal_os.core.events import ChangeContext
from personal_os.sync.calendar_sync import CalendarSync
from personal_os.sync.ports import EventPayload
from personal_os.sync.store import LinkStore, OperationLog
from tests.conftest import TZ
from tests.fake_caldav import FakeCalDav

CLI = ChangeContext.cli()


@pytest.fixture
def cal(db, clock, events) -> CalendarService:
    return CalendarService(db, clock, events, TZ)


@pytest.fixture
def remote() -> FakeCalDav:
    return FakeCalDav(TZ)


@pytest.fixture
def sync(db, clock, events, cal, remote) -> CalendarSync:
    return CalendarSync(db, clock, events, cal, remote, TZ)


def payload(**kw) -> EventPayload:
    base = dict(
        uid="abc@personal-os",
        title="Dentista",
        notes="",
        location="",
        start="2026-10-06T18:00",
        end="2026-10-06T19:00",
        all_day=False,
        timezone=TZ,
        rrule=None,
        alerts=(),
    )
    return EventPayload(**{**base, **kw})


# ------------------------------------------------------------------ iCalendar


@pytest.mark.parametrize(
    "p",
    [
        payload(notes="Revisión\nanual", location="Calle Mayor 1", alerts=(15, 60)),
        payload(all_day=True, start="2026-10-06", end="2026-10-08"),
        payload(rrule="FREQ=WEEKLY;BYDAY=TU", alerts=(0,)),
    ],
)
def test_ical_roundtrip(p):
    back, overrides = from_ics(to_ics(p), TZ)
    assert back == p and overrides is False


def test_ical_timezone_and_valarm_encoding():
    text = to_ics(payload(alerts=(30,)))
    assert "DTSTART;TZID=Atlantic/Canary:20261006T180000" in text
    assert "BEGIN:VTIMEZONE" in text and "TRIGGER:-PT30M" in text


def test_ical_parses_apple_specifics():
    ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:ios-1
DTSTART:20261006T170000Z
DURATION:PT90M
SUMMARY:Creado en iPhone
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT10M
END:VALARM
BEGIN:VALARM
ACTION:NONE
TRIGGER;VALUE=DATE-TIME:19760401T005545Z
X-APPLE-DEFAULT-ALARM:TRUE
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:ios-1
RECURRENCE-ID:20261013T170000Z
DTSTART:20261013T180000Z
SUMMARY:Movido
END:VEVENT
END:VCALENDAR
"""
    p, overrides = from_ics(ics, TZ)
    # 17:00 UTC = 18:00 en Canarias (horario de verano)
    assert (p.start, p.end, p.alerts, p.title) == (
        "2026-10-06T18:00",
        "2026-10-06T19:30",
        (10,),
        "Creado en iPhone",
    )
    assert overrides is True


# ------------------------------------------------------------------ servicio


def test_create_defaults_and_validation(cal):
    ev = cal.create(CLI, "Reunión", starts_at="2026-10-06T10:00", alerts=[15, 15, 5])
    assert ev.ends_at == "2026-10-06T11:00" and ev.alerts == (5, 15)
    day = cal.create(CLI, "Vacaciones", starts_at="2026-10-10", all_day=True)
    assert day.ends_at == "2026-10-11"
    with pytest.raises(CalendarError):
        cal.create(CLI, "Mal", starts_at="2026-10-06T10:00", ends_at="2026-10-06T09:00")
    with pytest.raises(CalendarError):
        cal.create(CLI, "Mal", starts_at="2026-10-06")  # falta hora sin all_day
    with pytest.raises(CalendarError):
        cal.create(CLI, "Mal", starts_at="2026-10-06T10:00", rrule="FREQ=HOURLY")


def test_update_and_cancel_emit_events(cal, events):
    ev = cal.create(CLI, "X", starts_at="2026-10-06T10:00")
    cal.update(CLI, ev.id, title="Y", starts_at="2026-10-06T11:00", ends_at="2026-10-06T12:00")
    cal.cancel(CLI, ev.id)
    types = [e.type for e in events.query(entity_id=ev.id)]
    assert types == ["calendar_event.cancelled", "calendar_event.updated", "calendar_event.created"]
    with pytest.raises(CalendarError):
        cal.update(CLI, ev.id, ends_at="2026-10-06T10:30")  # quedaría antes del inicio


# ------------------------------------------------------------------ sincronización


def test_create_pushes_with_our_uid_and_alarm(sync, cal, remote, db, clock):
    ev = cal.create(CLI, "Dentista", starts_at="2026-10-06T18:00", alerts=[60])
    report = sync.run()
    assert report.stats["created"] == 1
    r = remote.only()
    assert r.payload.uid == f"{ev.id}@personal-os" and r.payload.alerts == (60,)
    link = LinkStore(db, clock).get("apple_calendar", "calendar_event", ev.id)
    assert (link.external_id, link.etag, link.sync_state) == (r.href, r.etag, "synced")
    assert sync.run().stats == {}  # idempotente


def test_core_edit_uses_if_match(sync, cal, remote):
    ev = cal.create(CLI, "Dentista", starts_at="2026-10-06T18:00")
    sync.run()
    cal.update(CLI, ev.id, location="Clínica")
    assert sync.run().stats["updated"] == 1
    assert remote.only().payload.location == "Clínica"


def test_phone_edit_is_applied_to_core(sync, cal, remote, events):
    ev = cal.create(CLI, "Dentista", starts_at="2026-10-06T18:00")
    sync.run()
    remote.phone_edit(
        remote.only().href, start="2026-10-07T09:00", end="2026-10-07T10:00", alerts=(5,)
    )
    report = sync.run()
    assert report.stats["external_changes"] == 1 and "updated" not in report.stats
    ev = cal.get(ev.id)
    assert (ev.starts_at, ev.ends_at, ev.alerts) == ("2026-10-07T09:00", "2026-10-07T10:00", (5,))
    (change,) = events.query(entity_id=ev.id, type_prefix="sync.external_change_detected")
    assert change.actor == "apple"


def test_phone_delete_cancels_event(sync, cal, remote):
    ev = cal.create(CLI, "Dentista", starts_at="2026-10-06T18:00")
    sync.run()
    remote.phone_delete(remote.only().href)
    assert sync.run().stats["deleted_in_apple"] == 1
    assert cal.get(ev.id).status == "cancelled"


def test_core_cancel_deletes_remote(sync, cal, remote, db, clock):
    ev = cal.create(CLI, "Dentista", starts_at="2026-10-06T18:00")
    sync.run()
    cal.cancel(CLI, ev.id)
    assert sync.run().stats["deleted"] == 1 and remote.items == {}
    (op, *_) = OperationLog(db, clock).recent(entity_id=ev.id)
    assert (op.operation, op.status) == ("delete", "deleted")


def test_phone_created_event_is_adopted(sync, cal, remote):
    remote.phone_add(payload(uid="IOS-UID-1", title="Cumple de Ana", alerts=(1440,)))
    report = sync.run()
    assert report.stats["adopted"] == 1
    (ev,) = cal.list()
    assert (ev.title, ev.origin_module, ev.origin_ref) == ("Cumple de Ana", "apple", "IOS-UID-1")
    cal.update(CLI, ev.id, notes="comprar regalo")
    sync.run()
    r = remote.only()
    assert r.payload.uid == "IOS-UID-1" and r.payload.notes == "comprar regalo"


def test_conflict_same_field_phone_wins(sync, cal, remote, events):
    ev = cal.create(CLI, "Original", starts_at="2026-10-06T18:00")
    sync.run()
    cal.update(CLI, ev.id, title="Desde el PC")
    remote.phone_edit(remote.only().href, title="Desde el iPhone")
    report = sync.run()
    assert report.stats["conflicts"] == 1
    assert (
        cal.get(ev.id).title == "Desde el iPhone"
        and remote.only().payload.title == "Desde el iPhone"
    )
    (c,) = events.query(entity_id=ev.id, type_prefix="sync.conflict_detected")
    assert c.payload["field"] == "title"


def test_disjoint_changes_merge(sync, cal, remote):
    ev = cal.create(CLI, "Original", starts_at="2026-10-06T18:00")
    sync.run()
    cal.update(CLI, ev.id, notes="del PC")
    remote.phone_edit(remote.only().href, location="del iPhone")
    sync.run()
    p = remote.only().payload
    assert (p.notes, p.location) == ("del PC", "del iPhone")
    assert cal.get(ev.id).location == "del iPhone"


def test_cancel_wins_over_phone_edit(sync, cal, remote):
    ev = cal.create(CLI, "X", starts_at="2026-10-06T18:00")
    sync.run()
    cal.cancel(CLI, ev.id)
    href = remote.only().href
    remote.phone_edit(href, title="editado")
    # La entrada aplica el título (evento ya cancelado: se registra, no se reactiva) y se borra.
    sync.run()
    assert remote.items == {}


def test_recurring_event_native_rrule(sync, cal, remote):
    cal.create(
        CLI, "Pilates", starts_at="2026-10-06T19:00", rrule="FREQ=WEEKLY;BYDAY=TU", alerts=[30]
    )
    sync.run()
    assert remote.only().payload.rrule == "FREQ=WEEKLY;BYDAY=TU"
