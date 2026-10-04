"""Core real + adaptador de buzón real + bridge JS real (simulador de Scriptable).

Cada test recorre el mismo camino que en producción: SQLite → lote JSON → bridge →
"Recordatorios" → ack/snapshot → reconciliación.
"""

from __future__ import annotations

import pytest

from personal_os.adapters.apple.reminders_mailbox import MailboxRemindersGateway
from personal_os.core.events import ChangeContext
from personal_os.sync.reminders_sync import RemindersSync
from personal_os.sync.store import LinkStore, OperationLog
from tests.conftest import TZ
from tests.sim_device import SimDevice, requires_node

pytestmark = requires_node
CLI = ChangeContext.cli()


@pytest.fixture
def device(tmp_path) -> SimDevice:
    return SimDevice(tmp_path / "iphone", tz=TZ)


@pytest.fixture
def sync(db, clock, events, tasks, device) -> RemindersSync:
    gateway = MailboxRemindersGateway(device.mailbox, "Personal OS")
    return RemindersSync(db, clock, events, tasks, gateway, TZ)


def roundtrip(sync: RemindersSync, device: SimDevice):
    """sync (envía) → iPhone ejecuta el bridge → sync (recibe)."""
    sync.run()
    device.run()
    return sync.run()


def link_of(db, clock, occ_id):
    return LinkStore(db, clock).get("apple_reminders", "occurrence", occ_id)


# ------------------------------------------------------------------ PC → iPhone


def test_create_task_appears_in_reminders_and_links(sync, device, tasks, db, clock):
    _, occ = tasks.create_task(
        CLI, "Sacar reciclaje", notes="bolsa amarilla", due_date="2026-10-05", due_time="20:00"
    )
    report = roundtrip(sync, device)

    r = device.by_marker(occ.id)
    assert r is not None and r["title"] == "Sacar reciclaje"
    assert r["notes"].startswith("bolsa amarilla")
    assert device.local_due(r) == ("2026-10-05", "20:00")
    link = link_of(db, clock, occ.id)
    assert (link.external_id, link.sync_state) == (r["identifier"], "synced")
    assert report.errors == []
    # Sin cambios: ni comandos nuevos ni efectos.
    again = roundtrip(sync, device)
    assert again.stats.get("commands_sent", 0) == 0 and len(device.reminders()) == 1


def test_task_without_due_date(sync, device, tasks):
    _, occ = tasks.create_task(CLI, "Comprar bombillas")
    roundtrip(sync, device)
    assert device.local_due(device.by_marker(occ.id)) is None


def test_edit_in_core_updates_reminder(sync, device, tasks):
    task, occ = tasks.create_task(CLI, "Antes", due_date="2026-10-06")
    roundtrip(sync, device)
    tasks.update_task(CLI, task.id, title="Después")
    tasks.reschedule(CLI, occ.id, due_date="2026-10-07", due_time="09:30")
    roundtrip(sync, device)
    r = device.by_marker(occ.id)
    assert r["title"] == "Después" and device.local_due(r) == ("2026-10-07", "09:30")


def test_complete_in_core_marks_reminder_completed(sync, device, tasks):
    _, occ = tasks.create_task(CLI, "Simple", due_date="2026-10-05")
    roundtrip(sync, device)
    tasks.complete(CLI, occ.id)
    roundtrip(sync, device)
    assert device.by_marker(occ.id)["isCompleted"] is True


def test_skip_in_core_deletes_reminder_and_creates_next(sync, device, tasks):
    _, occ = tasks.create_task(CLI, "Regar", due_date="2026-10-05", rrule="FREQ=DAILY")
    roundtrip(sync, device)
    nxt = tasks.skip(CLI, occ.id)
    roundtrip(sync, device)
    assert device.by_marker(occ.id) is None
    assert device.local_due(device.by_marker(nxt.id)) == ("2026-10-06", None)


def test_cancel_task_removes_reminder(sync, device, tasks):
    task, _ = tasks.create_task(CLI, "Ya no", due_date="2026-10-05", rrule="FREQ=WEEKLY")
    roundtrip(sync, device)
    tasks.cancel_task(CLI, task.id)
    roundtrip(sync, device)
    assert device.reminders() == []


# ------------------------------------------------------------------ iPhone → PC


def test_complete_on_iphone_completes_and_generates_next(sync, device, tasks, events):
    task, occ = tasks.create_task(
        CLI,
        "Sacar reciclaje",
        due_date="2026-10-05",
        due_time="20:00",
        rrule="FREQ=WEEKLY;BYDAY=MO",
    )
    roundtrip(sync, device)

    device.complete(occ.id)
    device.run()
    report = sync.run()  # recibe la compleción y envía la siguiente ocurrencia
    assert report.stats["completed_in_apple"] == 1

    done = tasks.get_occurrence(occ.id)
    assert (done.status, done.completed_via) == ("completed", "apple")
    nxt = tasks.open_occurrence(task.id)
    assert (nxt.seq, nxt.due_date, nxt.due_time) == (2, "2026-10-12", "20:00")

    device.run()
    sync.run()
    r = device.by_marker(nxt.id)
    assert r is not None and not r["isCompleted"]
    assert device.local_due(r) == ("2026-10-12", "20:00")
    # Auditoría: la compleción quedó atribuida al iPhone.
    (ev,) = events.query(entity_id=occ.id, type_prefix="task.occurrence.completed")
    assert ev.actor == "apple"


def test_uncomplete_on_iphone_reopens_and_cancels_generated_next(sync, device, tasks):
    task, occ = tasks.create_task(CLI, "Filtro", due_date="2026-10-05", rrule="FREQ=MONTHLY")
    roundtrip(sync, device)
    device.complete(occ.id)
    device.run()
    sync.run()
    nxt = tasks.open_occurrence(task.id)
    device.run()
    sync.run()
    assert device.by_marker(nxt.id) is not None

    device.complete(occ.id, done=False)
    device.run()
    sync.run()
    assert tasks.get_occurrence(occ.id).is_open
    assert tasks.get_occurrence(nxt.id).status == "cancelled"
    device.run()
    sync.run()
    assert device.by_marker(nxt.id) is None  # la siguiente se retiró del iPhone


def test_edit_title_and_due_on_iphone(sync, device, tasks):
    task, occ = tasks.create_task(CLI, "Original", due_date="2026-10-06", due_time="10:00")
    roundtrip(sync, device)
    device.edit_title(occ.id, "Editado en el iPhone")
    device.set_due(occ.id, "2026-10-08", "18:15")
    device.run()
    report = sync.run()
    assert report.stats["external_changes"] == 1
    assert tasks.get_task(task.id).title == "Editado en el iPhone"
    o = tasks.get_occurrence(occ.id)
    assert (o.due_date, o.due_time) == ("2026-10-08", "18:15")
    # El cambio no "rebota": no se reenvía nada.
    assert report.stats.get("commands_sent", 0) == 0


def test_delete_on_iphone_cancels_occurrence_series_continues(sync, device, tasks):
    task, occ = tasks.create_task(CLI, "Aspirar", due_date="2026-10-05", rrule="FREQ=DAILY")
    roundtrip(sync, device)
    device.delete(occ.id)
    device.run()
    report = sync.run()
    assert report.stats["deleted_in_apple"] == 1
    o = tasks.get_occurrence(occ.id)
    assert (o.status, o.closed_reason) == ("cancelled", "deleted_externally")
    nxt = tasks.open_occurrence(task.id)
    device.run()
    sync.run()
    assert device.by_marker(nxt.id) is not None


def test_manual_reminder_is_adopted_and_marked(sync, device, tasks):
    sync.run()
    ident = device.add_manual("Llamar al dentista", notes="mañana")
    device.run()
    report = sync.run()  # adopta y envía el marcador
    assert report.stats["adopted"] == 1
    (task,) = tasks.list_tasks()
    assert (task.title, task.origin_module, task.origin_ref) == (
        "Llamar al dentista",
        "apple",
        ident,
    )
    occ = tasks.open_occurrence(task.id)
    device.run()
    sync.run()
    r = device.by_marker(occ.id)
    assert r["identifier"] == ident and r["notes"].startswith("mañana")
    assert len(device.reminders()) == 1


# ------------------------------------------------------------------ conflictos y robustez


def test_conflict_same_field_iphone_wins_and_is_recorded(sync, device, tasks, events):
    task, occ = tasks.create_task(CLI, "Original", due_date="2026-10-06")
    roundtrip(sync, device)
    tasks.update_task(CLI, task.id, title="Cambio en el PC")  # aún no enviado
    device.edit_title(occ.id, "Cambio en el iPhone")
    device.run()
    report = sync.run()
    assert report.stats["conflicts"] == 1
    assert tasks.get_task(task.id).title == "Cambio en el iPhone"
    (ev,) = events.query(entity_id=occ.id, type_prefix="sync.conflict_detected")
    assert ev.payload["field"] == "title" and ev.payload["resolution"] == "apple_wins"


def test_conflict_detected_by_bridge_precondition(sync, device, tasks, events):
    """El PC envía un cambio basado en un estado viejo: el bridge no lo aplica."""
    task, occ = tasks.create_task(CLI, "Original", due_date="2026-10-06")
    roundtrip(sync, device)
    device.edit_title(occ.id, "Editado en el iPhone")  # el PC aún no lo ha visto
    tasks.update_task(CLI, task.id, notes="nota del PC")
    sync.run()  # envía upsert con expected = estado viejo
    device.run()  # precondición falla → conflict
    report = sync.run()
    assert report.stats["conflicts"] >= 1
    assert device.by_marker(occ.id)["title"] == "Editado en el iPhone"
    assert tasks.get_task(task.id).title == "Editado en el iPhone"
    # En la siguiente vuelta el cambio de notas (sin conflicto) sí llega.
    roundtrip(sync, device)
    assert device.by_marker(occ.id)["notes"].startswith("nota del PC")


def test_disjoint_changes_merge(sync, device, tasks):
    task, occ = tasks.create_task(CLI, "Título", due_date="2026-10-06")
    roundtrip(sync, device)
    tasks.update_task(CLI, task.id, notes="notas desde el PC")
    device.set_due(occ.id, "2026-10-09", None)
    device.run()
    sync.run()
    roundtrip(sync, device)
    r = device.by_marker(occ.id)
    assert r["notes"].startswith("notas desde el PC")
    assert device.local_due(r) == ("2026-10-09", None)


def test_lost_ack_is_recovered_from_snapshot(sync, device, tasks, db, clock):
    _, occ = tasks.create_task(CLI, "Ack perdido", due_date="2026-10-06")
    sync.run()
    device.run()
    device.drop_acks()
    report = sync.run()
    assert report.stats["acks_inferred"] == 1
    link = link_of(db, clock, occ.id)
    assert (
        link.sync_state == "synced" and link.external_id == device.by_marker(occ.id)["identifier"]
    )
    assert tasks.get_occurrence(occ.id).is_open


def test_unprocessed_batch_is_resent_after_timeout(sync, device, tasks, db, clock):
    _, occ = tasks.create_task(CLI, "iPhone apagado", due_date="2026-10-06")
    sync.run()
    assert sync.run().stats.get("commands_sent", 0) == 0  # en vuelo: no se duplica
    clock.advance(hours=25)
    report = sync.run()
    assert report.stats["commands_sent"] == 1 and report.warnings
    device.run()  # procesa ambos lotes: el segundo es idempotente
    sync.run()
    assert len(device.reminders()) == 1
    assert link_of(db, clock, occ.id).sync_state == "synced"


def test_identifier_change_relinks_by_marker(sync, device, tasks, db, clock):
    _, occ = tasks.create_task(CLI, "Re-enlace", due_date="2026-10-06")
    roundtrip(sync, device)
    new_ident = device.change_identifier(occ.id)
    device.run()
    report = sync.run()
    assert report.stats.get("relinked") == 1
    assert link_of(db, clock, occ.id).external_id == new_ident
    assert tasks.get_occurrence(occ.id).is_open


def test_replayed_batch_is_not_reapplied(sync, device, tasks):
    tasks.create_task(CLI, "Una vez", due_date="2026-10-06")
    sync.run()
    batch = next((device.mailbox / "outbox").glob("batch-*.json"))
    content = batch.read_text()
    device.run()
    batch.write_text(content)  # iCloud "resucita" el lote
    device.run()
    sync.run()
    assert len(device.reminders()) == 1


def test_sync_twice_without_device_does_not_duplicate(sync, device, tasks):
    tasks.create_task(CLI, "Una", due_date="2026-10-06")
    first = sync.run()
    second = sync.run()
    assert first.stats["commands_sent"] == 1 and second.stats.get("commands_sent", 0) == 0


def test_operations_are_audited(sync, device, tasks, db, clock):
    _, occ = tasks.create_task(CLI, "Auditada", due_date="2026-10-06")
    roundtrip(sync, device)
    (op,) = OperationLog(db, clock).recent(entity_id=occ.id)
    assert (op.operation, op.status) == ("upsert", "created")
    assert op.request["fields"]["title"] == "Auditada" and op.result["external_id"]


def test_missing_list_is_reported(sync, device, tasks):
    store = device._load()
    store["calendars"] = []
    device._save(store)
    tasks.create_task(CLI, "Sin lista")
    sync.run()
    device.run(expect_error=True)
    report = sync.run()
    assert any("no encuentra la lista" in e for e in report.errors)


def test_timezone_mismatch_warns(db, clock, events, tasks, tmp_path):
    device = SimDevice(tmp_path / "madrid", tz="Europe/Madrid")
    gateway = MailboxRemindersGateway(device.mailbox, "Personal OS")
    sync = RemindersSync(db, clock, events, tasks, gateway, TZ)
    sync.run()
    device.run()
    report = sync.run()
    assert any("Zona horaria" in w for w in report.warnings)
