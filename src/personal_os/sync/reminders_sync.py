"""Sincronización Tareas ⇄ Recordatorios (proveedor `apple_reminders`).

Una ejecución:
  1. Entrada: acks (resultados de nuestros comandos) y el snapshot más reciente.
     Los cambios hechos en el iPhone se aplican al dominio con actor `apple`.
  2. Salida: se calcula el estado deseado de cada ocurrencia relevante y se envía un lote
     con lo que difiere de lo último enviado.

Reglas de conflicto (ver docs/adr/0004-conflictos.md):
  - Gana el iPhone para cambios humanos (completar, título, fecha). Si el Core también había
    cambiado ese mismo campo sin haberlo enviado, se registra `sync.conflict_detected`.
  - Borrado en el iPhone ⇒ esa ocurrencia se cancela; la serie sigue.
  - Ocurrencia cerrada (saltada/cancelada) en el Core ⇒ se borra en Apple.

100 % determinista. Sin LLM.
"""

from __future__ import annotations

from datetime import timedelta

from personal_os.core.clock import Clock, from_iso, to_iso
from personal_os.core.db import Database
from personal_os.core.events import ChangeContext, EventLog
from personal_os.core.ids import new_id
from personal_os.sync.ports import (
    Due,
    ObservedReminder,
    ReminderAck,
    ReminderCommand,
    ReminderDelete,
    RemindersGateway,
    RemindersSnapshot,
    ReminderState,
    ReminderUpsert,
)
from personal_os.sync.store import (
    DELETED,
    Cursors,
    InboxLedger,
    Link,
    LinkStore,
    OperationLog,
    SyncReport,
    SyncRuns,
    state_hash,
)
from personal_os.tasks.models import Occurrence, Task, TaskError
from personal_os.tasks.service import TaskService

PROVIDER = "apple_reminders"
ENTITY = "occurrence"
SYNC_WINDOW_DAYS = 120
INFLIGHT_TIMEOUT = timedelta(hours=24)


def desired_state(task: Task, occ: Occurrence) -> ReminderState | None:
    """Cómo debe verse la ocurrencia en Recordatorios. None = no debe existir."""
    if occ.status in ("skipped", "cancelled"):
        return None
    return ReminderState(
        title=task.title,
        notes=task.notes,
        due=Due(occ.due_date, occ.due_time) if occ.due_date else None,
        is_completed=occ.status == "completed",
    )


def _h(state: ReminderState | None) -> str:
    return DELETED if state is None else state_hash(state.to_json())


class RemindersSync:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        events: EventLog,
        tasks: TaskService,
        gateway: RemindersGateway,
        timezone: str,
    ) -> None:
        self.db = db
        self.clock = clock
        self.events = events
        self.tasks = tasks
        self.gateway = gateway
        self.timezone = timezone
        self.links = LinkStore(db, clock)
        self.ops = OperationLog(db, clock)
        self.ledger = InboxLedger(db, clock)
        self.cursors = Cursors(db)
        self.runs = SyncRuns(db, clock)

    # ================================================================== ejecución

    def run(self, *, push: bool = True) -> SyncReport:
        run_id = new_id("run")
        report = SyncReport(run_id=run_id, provider=PROVIDER)
        self.runs.start(run_id, PROVIDER)
        try:
            inbox = self.gateway.collect()
            for bad in inbox.invalid_files:
                report.warnings.append(f"Fichero ignorado: {bad}")
            ctx = ChangeContext.apple(run_id)
            with self.db.transaction():
                for ack in inbox.acks:
                    if not self.ledger.seen(PROVIDER, "ack", ack.batch_id):
                        self._process_ack(ctx, ack, report)
                        self.ledger.mark(PROVIDER, "ack", ack.batch_id)
                snapshot = self._latest_new_snapshot(inbox.snapshots)
                for snap in inbox.snapshots:
                    self.ledger.mark(PROVIDER, "snapshot", snap.run_id)
                if snapshot is not None:
                    self._process_snapshot(ctx, snapshot, report)
                    self.cursors.set(PROVIDER, "snapshot_taken_at", snapshot.taken_at)
                    # Para diagnóstico (`pos doctor`).
                    self.cursors.set(PROVIDER, "bridge_version", snapshot.bridge_version or "")
                    self.cursors.set(PROVIDER, "device_timezone", snapshot.device_timezone or "")
                    self.cursors.set(PROVIDER, "list_found", "1" if snapshot.list_found else "0")
            if push:
                with self.db.transaction():
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

    def _latest_new_snapshot(self, snapshots) -> RemindersSnapshot | None:
        last = self.cursors.get(PROVIDER, "snapshot_taken_at")
        fresh = [
            s
            for s in snapshots
            if not self.ledger.seen(PROVIDER, "snapshot", s.run_id)
            and (last is None or s.taken_at >= last)
        ]
        return fresh[-1] if fresh else None

    # ================================================================== entrada: acks

    def _process_ack(self, ctx: ChangeContext, ack: ReminderAck, report: SyncReport) -> None:
        report.inc("acks")
        for res in ack.results:
            op = self.ops.get(res.op_id)
            if op is None or op.status != "sent":
                continue  # operaciones ajenas (p. ej. pruebas) o ya procesadas
            link = self.links.get(PROVIDER, ENTITY, op.entity_id)
            if link is None:
                continue
            result_json = {
                "status": res.status,
                "external_id": res.external_id,
                "state": res.state.to_json() if res.state else None,
                "error": res.error,
                "batch_id": ack.batch_id,
            }
            self.ops.finish(op.op_id, res.status, result_json, res.external_id)
            is_current = link.pending_op_id == op.op_id
            if is_current:
                link.pending_op_id = None

            if res.status in ("created", "applied"):
                link.external_id = res.external_id
                self._set_seen(link, res.state, res.marker)
                if is_current:
                    link.sync_state = "synced"
                    link.last_error = None
            elif res.status == "deleted" or (
                res.status == "not_found" and op.operation == "delete"
            ):
                link.external_id = None
                self._set_seen(link, None, None)
                link.sync_state = "synced"
            elif res.status == "not_found":
                # Esperábamos que existiera: se borró en el iPhone.
                self._external_delete(ctx, link, report)
            elif res.status == "conflict":
                report.inc("conflicts")
                occ = self.tasks.get_occurrence(link.entity_id)
                task = self.tasks.get_task(occ.task_id)
                self.events.record(
                    ctx,
                    "sync.conflict_detected",
                    "occurrence",
                    occ.id,
                    {
                        "provider": PROVIDER,
                        "op_id": op.op_id,
                        "sent": op.request.get("fields"),
                        "found_in_apple": res.state.to_json() if res.state else None,
                        "resolution": "apple_wins",
                    },
                )
                if res.state is not None:
                    link.external_id = res.external_id or link.external_id
                    self._apply_observed(ctx, task, occ, link, res.state, None, report)
                    self._set_seen(link, res.state, res.marker)
                link.sync_state = "synced"
            else:  # error
                report.errors.append(f"{op.entity_id}: {res.error}")
                link.sync_state = "error"
                link.last_error = res.error
            self.links.save(link)

    # ================================================================== entrada: snapshot

    def _process_snapshot(
        self, ctx: ChangeContext, snap: RemindersSnapshot, report: SyncReport
    ) -> None:
        report.inc("snapshots")
        if not snap.list_found:
            report.errors.append(
                f"El iPhone no encuentra la lista '{snap.list_name}': {snap.list_error or 'no existe'}"
            )
            return
        if snap.device_timezone and snap.device_timezone != self.timezone:
            report.warnings.append(
                f"Zona horaria del iPhone ({snap.device_timezone}) distinta de la configurada "
                f"({self.timezone}). Las fechas se interpretan como hora local del iPhone."
            )

        by_id = {r.external_id: r for r in snap.reminders}
        by_marker = {r.marker: r for r in snap.reminders if r.marker}
        matched: set[str] = set()

        for link in self.links.all(PROVIDER, ENTITY):
            if link.pending_op_id is not None:
                obs = by_id.get(link.external_id or "") or by_marker.get(link.entity_id)
                if obs:
                    matched.add(obs.external_id)
                op = self.ops.get(link.pending_op_id)
                if obs and op and op.batch_id in snap.applied_batches and op.operation == "upsert":
                    # El ack se perdió pero el snapshot demuestra que el lote se aplicó.
                    self.ops.finish(
                        op.op_id,
                        "applied",
                        {"inferred_from_snapshot": snap.run_id},
                        obs.external_id,
                    )
                    link.pending_op_id = None
                    link.external_id = obs.external_id
                    link.sync_state = "synced"
                    self._set_seen(link, obs.state, obs.marker)
                    self.links.save(link)
                    report.inc("acks_inferred")
                # Si no, el ack dirá el estado: evita confundir un objeto recién pedido (aún
                # no creado) con uno borrado.
                continue
            obs = by_id.get(link.external_id or "") or by_marker.get(link.entity_id)
            if obs is None:
                if link.external_id is not None:
                    self._missing_in_snapshot(ctx, link, report)
                continue
            matched.add(obs.external_id)
            if obs.external_id != link.external_id:
                report.inc("relinked")
                link.external_id = obs.external_id
            if _h(obs.state) != link.last_seen_hash or obs.marker != link.last_seen_marker:
                occ = self.tasks.get_occurrence(link.entity_id)
                task = self.tasks.get_task(occ.task_id)
                if _h(obs.state) != link.last_seen_hash:
                    self._apply_observed(
                        ctx, task, occ, link, obs.state, obs.completion_date, report
                    )
                self._set_seen(link, obs.state, obs.marker)
            self.links.save(link)

        for obs in snap.reminders:
            if obs.external_id in matched:
                continue
            self._unlinked_reminder(ctx, obs, report)

    def _missing_in_snapshot(self, ctx: ChangeContext, link: Link, report: SyncReport) -> None:
        occ = self.tasks.get_occurrence(link.entity_id)
        if occ.is_open:
            self._external_delete(ctx, link, report)
        else:
            # Completada hace más de la ventana del snapshot, o borrada tras cerrarse: ya no
            # hay nada que sincronizar.
            link.external_id = None
            link.sync_state = "synced" if link.last_pushed_hash == DELETED else "orphaned"
            self._set_seen(link, None, None)
        self.links.save(link)

    def _external_delete(self, ctx: ChangeContext, link: Link, report: SyncReport) -> None:
        report.inc("deleted_in_apple")
        occ = self.tasks.get_occurrence(link.entity_id)
        self.events.record(
            ctx,
            "sync.external_change_detected",
            "occurrence",
            occ.id,
            {"provider": PROVIDER, "change": "deleted"},
        )
        link.external_id = None
        link.sync_state = "orphaned"
        self._set_seen(link, None, None)
        if occ.is_open:
            self.tasks.close_deleted_externally(ctx, occ.id)

    def _unlinked_reminder(
        self, ctx: ChangeContext, obs: ObservedReminder, report: SyncReport
    ) -> None:
        if obs.marker and obs.marker.startswith("occ_"):
            # Marcador nuestro sin enlace (p. ej. base de datos restaurada): re-enlazar.
            try:
                occ = self.tasks.get_occurrence(obs.marker)
            except TaskError:
                report.warnings.append(f"Recordatorio con marcador desconocido {obs.marker}")
                return
            link = self.links.get(PROVIDER, ENTITY, occ.id) or Link(PROVIDER, ENTITY, occ.id)
            link.external_id = obs.external_id
            self._set_seen(link, obs.state, obs.marker)
            link.sync_state = "synced"
            self.links.save(link)
            report.inc("relinked")
            return
        if obs.marker or obs.state.is_completed:
            return  # marcadores ajenos (pruebas) o completados sin enlace: se ignoran
        # Recordatorio creado a mano en la lista Personal OS: se adopta como tarea.
        due = obs.state.due
        _task, occ = self.tasks.create_task(
            ctx,
            obs.state.title or "(sin título)",
            notes=obs.state.notes,
            due_date=due.date if due else None,
            due_time=due.time if due else None,
            origin_module="apple",
            origin_ref=obs.external_id,
        )
        link = Link(PROVIDER, ENTITY, occ.id, external_id=obs.external_id, sync_state="pushed")
        self._set_seen(link, obs.state, None)
        self.links.save(link)
        report.inc("adopted")

    # ================================================================== aplicar cambios

    def _apply_observed(
        self,
        ctx: ChangeContext,
        task: Task,
        occ: Occurrence,
        link: Link,
        observed: ReminderState,
        completion_date: str | None,
        report: SyncReport,
    ) -> None:
        """Aplica al dominio lo que cambió en el iPhone desde la última observación."""
        previous = (
            ReminderState.from_json(link.last_seen_state)
            if link.last_seen_state
            else desired_state(task, occ)
        )
        if previous is None:
            previous = observed
        pushed = ReminderState.from_json(link.last_pushed_state) if link.last_pushed_state else None
        current = desired_state(task, occ)

        changed = [
            f
            for f in ("title", "notes", "due", "is_completed")
            if getattr(observed, f) != getattr(previous, f)
        ]
        if not changed:
            return
        report.inc("external_changes")

        # Conflicto: el Core cambió el mismo campo y aún no lo había enviado.
        for f in changed:
            if current is not None and pushed is not None:
                core_changed = getattr(current, f) != getattr(pushed, f)
                if core_changed and getattr(current, f) != getattr(observed, f):
                    report.inc("conflicts")
                    self.events.record(
                        ctx,
                        "sync.conflict_detected",
                        "occurrence",
                        occ.id,
                        {
                            "provider": PROVIDER,
                            "field": f,
                            "core_value": _jsonable(getattr(current, f)),
                            "apple_value": _jsonable(getattr(observed, f)),
                            "resolution": "apple_wins",
                        },
                    )

        self.events.record(
            ctx,
            "sync.external_change_detected",
            "occurrence",
            occ.id,
            {"provider": PROVIDER, "fields": changed},
        )

        if "title" in changed or "notes" in changed:
            self.tasks.update_task(
                ctx,
                task.id,
                title=observed.title if "title" in changed else None,
                notes=observed.notes if "notes" in changed else None,
            )
        if "due" in changed and occ.is_open:
            self.tasks.reschedule(
                ctx,
                occ.id,
                due_date=observed.due.date if observed.due else None,
                due_time=observed.due.time if observed.due else None,
            )
        if "is_completed" in changed:
            fresh = self.tasks.get_occurrence(occ.id)
            if observed.is_completed and fresh.is_open:
                self.tasks.complete(ctx, occ.id, completed_at=completion_date)
                report.inc("completed_in_apple")
            elif not observed.is_completed and fresh.status == "completed":
                self.tasks.reopen(ctx, occ.id)
                report.inc("reopened_in_apple")
            elif observed.is_completed and fresh.status in ("skipped", "cancelled"):
                # Cerrada en el Core y completada en el iPhone: se mantiene el Core y se avisa.
                report.inc("conflicts")
                self.events.record(
                    ctx,
                    "sync.conflict_detected",
                    "occurrence",
                    occ.id,
                    {
                        "provider": PROVIDER,
                        "field": "is_completed",
                        "core_value": fresh.status,
                        "apple_value": True,
                        "resolution": "core_wins",
                    },
                )

    def _set_seen(self, link: Link, state: ReminderState | None, marker: str | None) -> None:
        link.last_seen_state = state.to_json() if state else None
        link.last_seen_hash = state_hash(state.to_json()) if state else None
        link.last_seen_marker = marker

    # ================================================================== salida

    def _push(self, ctx: ChangeContext, report: SyncReport) -> None:
        now = self.clock.now()
        since = to_iso(now - timedelta(days=SYNC_WINDOW_DAYS))
        batch_id = new_id("batch")
        commands: list[ReminderCommand] = []

        for task, occ in self.tasks.occurrences_for_sync(since):
            link = self.links.get(PROVIDER, ENTITY, occ.id)
            desired = desired_state(task, occ)
            h = _h(desired)

            force = False
            if link is not None and link.pending_op_id is not None:
                op = self.ops.get(link.pending_op_id)
                if op and now - from_iso(op.created_at) < INFLIGHT_TIMEOUT:
                    continue
                if op:
                    self.ops.finish(op.op_id, "superseded")
                    report.warnings.append(f"Comando {op.op_id} sin respuesta >24h: se reenvía")
                link.pending_op_id = None
                force = True

            if desired is None:
                exists_in_apple = link is not None and (
                    link.external_id is not None
                    or (
                        link.last_pushed_hash not in (None, DELETED)
                        and link.sync_state != "orphaned"
                    )
                )
                if not exists_in_apple or (
                    link.last_pushed_hash == DELETED and link.external_id is None
                ):
                    continue
                cmd: ReminderCommand = ReminderDelete(
                    op_id=new_id("operation"),
                    marker=occ.id,
                    external_id=link.external_id,
                    expected=ReminderState.from_json(link.last_seen_state)
                    if link.last_seen_state
                    else None,
                )
            else:
                if link is None and not occ.is_open:
                    continue  # se cerró antes de llegar a enviarse
                if (
                    not force
                    and link is not None
                    and link.last_pushed_hash == h
                    and link.sync_state in ("synced", "pushed")
                ):
                    continue
                if (
                    link is not None
                    and link.external_id
                    and link.last_seen_hash == h
                    and link.last_seen_marker == occ.id
                ):
                    # Apple ya tiene exactamente este estado (p. ej. tras aplicar un cambio del iPhone).
                    link.last_pushed_hash, link.last_pushed_state = h, desired.to_json()
                    link.sync_state = "synced"
                    self.links.save(link)
                    continue
                cmd = ReminderUpsert(
                    op_id=new_id("operation"),
                    marker=occ.id,
                    external_id=link.external_id if link else None,
                    expected=(
                        ReminderState.from_json(link.last_seen_state)
                        if link and link.external_id and link.last_seen_state
                        else None
                    ),
                    fields=desired,
                )

            link = link or Link(PROVIDER, ENTITY, occ.id)
            link.pending_op_id = cmd.op_id
            link.last_pushed_hash = h
            link.last_pushed_state = desired.to_json() if desired else None
            link.pushed_at = to_iso(now)
            link.sync_state = "pushed"
            self.links.save(link)
            self.ops.record(
                op_id=cmd.op_id,
                provider=PROVIDER,
                operation=cmd.type,
                entity_type=ENTITY,
                entity_id=occ.id,
                external_id=cmd.external_id,
                request=_command_json(cmd),
                status="sent",
                batch_id=batch_id,
                correlation_id=ctx.correlation_id,
            )
            commands.append(cmd)

        if commands:
            # Si la escritura del lote falla, la transacción se revierte y todo queda pendiente.
            self.gateway.submit(batch_id, commands)
            report.inc("commands_sent", len(commands))


def _jsonable(value: object) -> object:
    return value.to_json() if isinstance(value, Due) else value


def _command_json(cmd: ReminderCommand) -> dict:
    data: dict = {
        "type": cmd.type,
        "marker": cmd.marker,
        "external_id": cmd.external_id,
        "expected": cmd.expected.to_json() if cmd.expected else None,
    }
    if isinstance(cmd, ReminderUpsert):
        data["fields"] = cmd.fields.to_json()
    return data
