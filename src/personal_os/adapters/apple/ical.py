"""Traducción EventPayload ⇄ iCalendar (RFC 5545) para iCloud Calendar."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from icalendar import Alarm, Event, vRecur
from icalendar import Calendar as ICal

from personal_os.sync.ports import EventPayload

PRODID = "-//Personal OS//personal-os//ES"


def to_ics(payload: EventPayload, now: datetime | None = None) -> str:
    cal = ICal()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    ev = Event()
    ev.add("uid", payload.uid)
    ev.add("dtstamp", (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0))
    ev.add("summary", payload.title)
    if payload.notes:
        ev.add("description", payload.notes)
    if payload.location:
        ev.add("location", payload.location)
    if payload.all_day:
        ev.add("dtstart", date.fromisoformat(payload.start))
        ev.add("dtend", date.fromisoformat(payload.end))
    else:
        tz = ZoneInfo(payload.timezone)
        ev.add("dtstart", datetime.fromisoformat(payload.start).replace(tzinfo=tz))
        ev.add("dtend", datetime.fromisoformat(payload.end).replace(tzinfo=tz))
    if payload.rrule:
        ev.add("rrule", vRecur.from_ical(payload.rrule))
    for minutes in payload.alerts:
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", payload.title)
        alarm.add("trigger", timedelta(minutes=-minutes))
        ev.add_component(alarm)
    cal.add_component(ev)
    if not payload.all_day:
        cal.add_missing_timezones()
    return cal.to_ical().decode("utf-8")


def _local(value: datetime | date, tz: ZoneInfo) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:  # hora flotante: se interpreta en la zona configurada
            value = value.replace(tzinfo=tz)
        return value.astimezone(tz).strftime("%Y-%m-%dT%H:%M")
    return value.isoformat()


def from_ics(text: str, timezone: str) -> tuple[EventPayload, bool]:
    """Devuelve (payload del evento maestro, tiene_excepciones_por_instancia)."""
    cal = ICal.from_ical(text)
    vevents = [c for c in cal.walk("VEVENT")]
    if not vevents:
        raise ValueError("El recurso no contiene VEVENT")
    masters = [e for e in vevents if e.get("recurrence-id") is None]
    master = masters[0] if masters else vevents[0]
    has_overrides = len(vevents) > 1
    tz = ZoneInfo(timezone)

    dtstart = master.decoded("dtstart")
    all_day = not isinstance(dtstart, datetime)
    if master.get("dtend") is not None:
        dtend = master.decoded("dtend")
    elif master.get("duration") is not None:
        dtend = dtstart + master.decoded("duration")
    else:
        dtend = dtstart + (timedelta(days=1) if all_day else timedelta(hours=1))

    rrule = None
    if master.get("rrule") is not None:
        rrule = master.get("rrule").to_ical().decode("utf-8")

    alerts: set[int] = set()
    for alarm in master.walk("VALARM"):
        if str(alarm.get("x-apple-default-alarm", "")).upper() == "TRUE":
            continue
        trig = alarm.get("trigger")
        if trig is None:
            continue
        delta = trig.dt
        if isinstance(delta, timedelta) and delta <= timedelta(0):
            related = str(trig.params.get("RELATED", "START")).upper()
            if related == "START":
                alerts.add(int(-delta.total_seconds() // 60))

    payload = EventPayload(
        uid=str(master.get("uid")),
        title=str(master.get("summary", "")),
        notes=str(master.get("description", "")).rstrip(),
        location=str(master.get("location", "")),
        start=_local(dtstart, tz),
        end=_local(dtend, tz),
        all_day=all_day,
        timezone=timezone,
        rrule=rrule,
        alerts=tuple(sorted(alerts)),
    )
    return payload, has_overrides
