"""Sincronización Eventos ⇄ iCloud Calendar (proveedor `apple_calendar`), síncrona vía CalDAV.

Una ejecución:
  1. Entrada: ETags del calendario gestionado. Los recursos con ETag distinto se descargan y
     sus cambios se aplican al dominio (actor `apple`). Recursos desaparecidos ⇒ evento
     cancelado. Recursos desconocidos (creados en el iPhone) ⇒ se adoptan.
  2. Salida: eventos cuyo estado deseado difiere de lo último enviado ⇒ PUT con If-Match
     (o If-None-Match: * al crear). Cancelados ⇒ DELETE.

Mismas reglas de conflicto que Recordatorios: gana el iPhone para ediciones; una cancelación
explícita en el Personal OS se respeta. 100 % determinista.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta

from personal_os.calendar.models import CalendarError, CalendarEvent
from personal_os.calendar.service import CalendarService
from personal_os.core.clock import Clock, to_iso
from personal_os.core.db import Database
from personal_os.core.events import ChangeContext, EventLog
from personal_os.core.ids import new_id
from personal_os.sync.ports import (
    CalendarGateway,
    EventPayload,
    PreconditionFailed,
    RemoteEvent,
    RemoteNotFound,
)
from personal_os.sync.store import (
    DELETED,
    Link,
    LinkStore,
    OperationLog,
    SyncReport,
    SyncRuns,
    state_hash,
)

PROVIDER = "apple_calendar"
ENTITY = "calendar_event"
SYNC_WINDOW_DAYS = 120
_FIELDS = {  # campo del payload → campo del dominio
    "title": "title",
    "notes": "notes",
    "location": "location",
    "start": "starts_at",
    "end": "ends_at",
    "all_day": "all_day",
    "rrule": "rrule",
    "alerts": "alerts",
}


def _comparable(payload: EventPayload) -> dict:
    """Lo que importa para detectar cambios (sin uid ni timezone)."""
    data = asdict(payload)
    data.pop("uid")
    data.pop("timezone")
    data["alerts"] = list(data["alerts"])
    return data


def _h(payload: EventPayload | None) -> str:
    return DELETED if payload is None else state_hash(_comparable(payload))


class CalendarSync:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        events: EventLog,
        calendar: CalendarService,
        gateway: CalendarGateway,
        timezone: str,
    ) -> None:
        self.db = db
        self.clock = clock
        self.events = events
        self.calendar = calendar
        self.gateway = gateway
        self.timezone = timezone
        self.links = LinkStore(db, clock)
        self.ops = OperationLog(db, clock)
        self.runs = SyncRuns(db, clock)

    def desired(self, event: CalendarEvent, link: Link | None) -> EventPayload | None:
        if event.status == "cancelled":
            return None
        uid = (link.last_seen_state or {}).get("uid") if link else None
        return EventPayload(
            uid=uid or f"{event.id}@personal-os",
            title=event.title,
            notes=event.notes,
            location=event.location,
            start=event.starts_at,
            end=event.ends_at,
            all_day=event.all_day,
            timezone=event.timezone,
            rrule=event.rrule,
            alerts=tuple(event.alerts),
        )

    # ================================================================== ejecución

    def run(self, *, push: bool = True) -> SyncReport:
        run_id = new_id("run")
        report = SyncReport(run_id=run_id, provider=PROVIDER)
        self.runs.start(run_id, PROVIDER)
        try:
            with self.db.transaction():
                self._pull(ChangeContext.apple(run_id), report)
            if push:
                self._push(ChangeContext.system(run_id), report)
            self.events.record(
                ChangeContext.system(run_id), "sync.run_completed", "sync", PROVIDER, report.stats
            )
            self.runs.finish(report)
        except Exception as exc:
            report.errors.append(f"{type(exc).__name__}: {exc}")
            self.runs.finish(report, error=str(exc))
            raise
        return report

    # ================================================================== entrada

    def _pull(self, ctx: ChangeContext, report: SyncReport) -> None:
        etags = self.gateway.list_etags()
        linked: set[str] = set()
        for link in self.links.all(PROVIDER, ENTITY):
            if link.external_id is None:
                continue
            linked.add(link.external_id)
            if link.external_id not in etags:
                self._deleted_remotely(ctx, link, report)
                continue
            if etags[link.external_id] == link.etag:
                continue
            remote = self.gateway.get(link.external_id)
            if remote is None:
                self._deleted_remotely(ctx, link, report)
                continue
            self._apply_remote(ctx, link, remote, report)

        for href in sorted(set(etags) - linked):
            remote = self.gateway.get(href)
            if remote is not None:
                self._adopt(ctx, remote, report)

    def _apply_remote(
        self, ctx: ChangeContext, link: Link, remote: RemoteEvent, report: SyncReport
    ) -> None:
        if remote.has_overrides:
            report.warnings.append(
                f"{link.entity_id}: tiene instancias editadas individualmente en el iPhone (no se modelan)"
            )
        new = _comparable(remote.payload)
        old = {k: v for k, v in (link.last_seen_state or {}).items() if k in new}
        changed = [k for k in new if old.get(k) != new[k]] if old else []
        link.etag = remote.etag
        if changed:
            report.inc("external_changes")
            event = self.calendar.get(link.entity_id)
            pushed = link.last_pushed_state or {}
            current = self.desired(event, link)
            current_c = _comparable(current) if current else {}
            for k in changed:
                if pushed and current_c.get(k) != pushed.get(k) and current_c.get(k) != new[k]:
                    report.inc("conflicts")
                    self.events.record(
                        ctx,
                        "sync.conflict_detected",
                        ENTITY,
                        event.id,
                        {
                            "provider": PROVIDER,
                            "field": _FIELDS[k],
                            "core_value": current_c.get(k),
                            "apple_value": new[k],
                            "resolution": "apple_wins",
                        },
                    )
            self.events.record(
                ctx,
                "sync.external_change_detected",
                ENTITY,
                event.id,
                {"provider": PROVIDER, "fields": [_FIELDS[k] for k in changed]},
            )
            if event.status == "confirmed":
                updates = {
                    _FIELDS[k]: (tuple(new[k]) if k == "alerts" else new[k]) for k in changed
                }
                try:
                    self.calendar.update(ctx, event.id, validate_rrule=False, **updates)
                except CalendarError as exc:
                    report.errors.append(f"{event.id}: cambio del iPhone no aplicable: {exc}")
        link.last_seen_state = {**new, "uid": remote.payload.uid}
        link.last_seen_hash = _h(remote.payload)
        link.sync_state = "synced"
        self.links.save(link)

    def _deleted_remotely(self, ctx: ChangeContext, link: Link, report: SyncReport) -> None:
        event = self.calendar.get(link.entity_id)
        if event.status == "confirmed":
            report.inc("deleted_in_apple")
            self.events.record(
                ctx,
                "sync.external_change_detected",
                ENTITY,
                event.id,
                {"provider": PROVIDER, "change": "deleted"},
            )
            self.calendar.cancel(ctx, event.id, reason="deleted_externally")
        link.external_id = None
        link.etag = None
        link.sync_state = "orphaned" if link.last_pushed_hash != DELETED else "synced"
        link.last_seen_state = None
        link.last_seen_hash = None
        self.links.save(link)

    def _adopt(self, ctx: ChangeContext, remote: RemoteEvent, report: SyncReport) -> None:
        p = remote.payload
        try:
            event = self.calendar.create(
                ctx,
                p.title or "(sin título)",
                starts_at=p.start,
                ends_at=p.end,
                all_day=p.all_day,
                notes=p.notes,
                location=p.location,
                rrule=p.rrule,
                alerts=p.alerts,
                origin_module="apple",
                origin_ref=p.uid,
                validate_rrule=False,
            )
        except CalendarError as exc:
            report.warnings.append(f"No se pudo adoptar {remote.href}: {exc}")
            return
        h = _h(p)
        link = Link(
            PROVIDER,
            ENTITY,
            event.id,
            external_id=remote.href,
            etag=remote.etag,
            last_seen_state={**_comparable(p), "uid": p.uid},
            last_seen_hash=h,
            last_pushed_hash=h,
            last_pushed_state=_comparable(p),
            sync_state="synced",
        )
        self.links.save(link)
        report.inc("adopted")

    # ================================================================== salida

    def _push(self, ctx: ChangeContext, report: SyncReport) -> None:
        since = to_iso(self.clock.now() - timedelta(days=SYNC_WINDOW_DAYS))
        for event in self.calendar.for_sync(since):
            link = self.links.get(PROVIDER, ENTITY, event.id)
            desired = self.desired(event, link)
            h = _h(desired)
            if desired is None:
                if link is None or link.external_id is None:
                    continue
                self._send_delete(ctx, event, link, report)
                continue
            if (
                link is not None
                and link.external_id
                and link.last_pushed_hash == h
                and link.sync_state == "synced"
            ):
                continue
            if link is not None and link.external_id and link.last_seen_hash == h:
                link.last_pushed_hash, link.last_pushed_state = h, _comparable(desired)
                link.sync_state = "synced"
                self.links.save(link)
                continue
            self._send_put(ctx, event, link or Link(PROVIDER, ENTITY, event.id), desired, report)

    def _send_put(
        self,
        ctx: ChangeContext,
        event: CalendarEvent,
        link: Link,
        desired: EventPayload,
        report: SyncReport,
    ) -> None:
        op_id = new_id("operation")
        creating = link.external_id is None
        request = {"payload": asdict(desired), "if_match": link.etag, "create": creating}
        try:
            href, etag = self.gateway.put(desired, link.external_id, link.etag)
        except PreconditionFailed:
            # Cambió en iCloud entre nuestra lectura y la escritura: la próxima sync lo recoge.
            self._log_op(ctx, op_id, "put", event.id, link.external_id, request, "conflict")
            report.inc("conflicts")
            report.warnings.append(f"{event.id}: cambió en iCloud durante la sync; se reintentará")
            return
        except RemoteNotFound:
            self._log_op(ctx, op_id, "put", event.id, link.external_id, request, "not_found")
            with self.db.transaction():
                self._deleted_remotely(ChangeContext.apple(ctx.correlation_id), link, report)
            return
        except Exception as exc:
            self._log_op(
                ctx, op_id, "put", event.id, link.external_id, request, "error", {"error": str(exc)}
            )
            report.errors.append(f"{event.id}: {exc}")
            with self.db.transaction():
                link.sync_state, link.last_error = "error", str(exc)
                self.links.save(link)
            return
        with self.db.transaction():
            self._log_op(
                ctx,
                op_id,
                "put",
                event.id,
                href,
                request,
                "created" if creating else "applied",
                {"etag": etag},
            )
            link.external_id, link.etag = href, etag
            link.last_pushed_hash = link.last_seen_hash = _h(desired)
            link.last_pushed_state = _comparable(desired)
            link.last_seen_state = {**_comparable(desired), "uid": desired.uid}
            link.pushed_at = to_iso(self.clock.now())
            link.sync_state, link.last_error = "synced", None
            self.links.save(link)
        report.inc("created" if creating else "updated")

    def _send_delete(
        self, ctx: ChangeContext, event: CalendarEvent, link: Link, report: SyncReport
    ) -> None:
        op_id = new_id("operation")
        request = {"href": link.external_id, "if_match": link.etag}
        try:
            self.gateway.delete(link.external_id, link.etag)  # type: ignore[arg-type]
            status = "deleted"
        except PreconditionFailed:
            # Editado en el iPhone, pero la cancelación es una decisión explícita: se respeta.
            self.events.record(
                ctx,
                "sync.conflict_detected",
                ENTITY,
                event.id,
                {"provider": PROVIDER, "field": "status", "resolution": "core_wins"},
            )
            report.inc("conflicts")
            self.gateway.delete(link.external_id, None)  # type: ignore[arg-type]
            status = "deleted"
        except Exception as exc:
            self._log_op(
                ctx,
                op_id,
                "delete",
                event.id,
                link.external_id,
                request,
                "error",
                {"error": str(exc)},
            )
            report.errors.append(f"{event.id}: {exc}")
            return
        with self.db.transaction():
            self._log_op(ctx, op_id, "delete", event.id, link.external_id, request, status)
            link.external_id = link.etag = None
            link.last_pushed_hash, link.last_pushed_state = DELETED, None
            link.last_seen_hash = link.last_seen_state = None
            link.sync_state = "synced"
            self.links.save(link)
        report.inc("deleted")

    def _log_op(
        self, ctx, op_id, operation, entity_id, external_id, request, status, result=None
    ) -> None:
        self.ops.record(
            op_id=op_id,
            provider=PROVIDER,
            operation=operation,
            entity_type=ENTITY,
            entity_id=entity_id,
            external_id=external_id,
            request=request,
            status=status,
            correlation_id=ctx.correlation_id,
            result=result,
        )
