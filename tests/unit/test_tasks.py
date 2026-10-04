from __future__ import annotations

from datetime import date

import pytest

from personal_os.core.events import ChangeContext
from personal_os.tasks import recurrence
from personal_os.tasks.models import TaskError

CLI = ChangeContext.cli()


# ------------------------------------------------------------------ recurrencias


def test_normalize_rrule_canonical_and_validated():
    assert recurrence.normalize_rrule("rrule:byday=mo,th;freq=weekly") == "FREQ=WEEKLY;BYDAY=MO,TH"
    for bad in ["FREQ=HOURLY", "FREQ=WEEKLY;BYDAY=1MO", "FREQ=DAILY;BYSETPOS=1", "INTERVAL=2"]:
        with pytest.raises(TaskError):
            recurrence.normalize_rrule(bad)


def test_build_rrule_shortcuts():
    assert recurrence.build_rrule("weekly", 2, "mo") == "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO"
    assert recurrence.build_rrule("weekdays") == "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    assert recurrence.describe("FREQ=MONTHLY;INTERVAL=3") == "cada 3 meses"


def test_schedule_anchor_keeps_calendar_and_skips_missed():
    rule = "FREQ=WEEKLY;BYDAY=MO"
    kw = dict(
        anchor="schedule", series_start=date(2026, 10, 5), completed_on=None, occurrences_so_far=1
    )
    # Completada a tiempo → siguiente lunes.
    assert recurrence.next_date(
        rule, previous_scheduled=date(2026, 10, 5), today=date(2026, 10, 5), **kw
    ) == date(2026, 10, 12)
    # Completada tres semanas tarde → primer lunes >= hoy, sin generar las perdidas.
    assert recurrence.next_date(
        rule, previous_scheduled=date(2026, 10, 5), today=date(2026, 10, 28), **kw
    ) == date(2026, 11, 2)


def test_completion_anchor_counts_from_completion():
    nxt = recurrence.next_date(
        "FREQ=MONTHLY;INTERVAL=3",
        anchor="completion",
        series_start=date(2026, 1, 10),
        previous_scheduled=date(2026, 1, 10),
        completed_on=date(2026, 2, 20),
        today=date(2026, 2, 20),
        occurrences_so_far=1,
    )
    assert nxt == date(2026, 5, 20)


def test_count_and_until_end_series():
    kw = dict(
        anchor="schedule",
        series_start=date(2026, 10, 5),
        previous_scheduled=date(2026, 10, 6),
        completed_on=None,
        today=date(2026, 10, 6),
    )
    assert recurrence.next_date("FREQ=DAILY;COUNT=2", occurrences_so_far=2, **kw) is None
    assert recurrence.next_date("FREQ=DAILY;UNTIL=20261006", occurrences_so_far=2, **kw) is None


# ------------------------------------------------------------------ servicio


def test_create_simple_task_has_one_open_occurrence(tasks, events):
    task, occ = tasks.create_task(
        CLI, "Llamar al fontanero", due_date="2026-10-06", due_time="10:00"
    )
    assert task.status == "active" and occ.is_open
    assert (occ.due_date, occ.due_time, occ.seq) == ("2026-10-06", "10:00", 1)
    types = [e.type for e in events.query(entity_id=task.id)] + [
        e.type for e in events.query(entity_id=occ.id)
    ]
    assert set(types) == {"task.created", "task.occurrence.created"}


def test_validation(tasks):
    with pytest.raises(TaskError):
        tasks.create_task(CLI, "  ")
    with pytest.raises(TaskError):
        tasks.create_task(CLI, "x", due_date="2026-13-01")
    with pytest.raises(TaskError):
        tasks.create_task(CLI, "x", due_time="10:00")


def test_complete_simple_task_closes_it(tasks):
    task, occ = tasks.create_task(CLI, "Pagar IBI", due_date="2026-10-06")
    done, nxt = tasks.complete(CLI, occ.id)
    assert done.status == "completed" and done.completed_via == "cli" and nxt is None
    assert tasks.get_task(task.id).status == "completed"
    # Idempotente
    again, _ = tasks.complete(CLI, occ.id)
    assert again.completed_at == done.completed_at


def test_recurring_first_date_aligns_with_rule(tasks):
    # 2026-10-08 es jueves; regla de lunes → primera ocurrencia el lunes 12.
    task, occ = tasks.create_task(
        CLI, "Reciclaje", due_date="2026-10-08", rrule="FREQ=WEEKLY;BYDAY=MO"
    )
    assert task.due_date == occ.due_date == "2026-10-12"


def test_complete_recurring_generates_next(tasks, clock):
    task, occ = tasks.create_task(
        CLI,
        "Sacar reciclaje",
        due_date="2026-10-05",
        due_time="20:00",
        rrule="FREQ=WEEKLY;BYDAY=MO",
    )
    _, nxt = tasks.complete(ChangeContext.apple("cor_x"), occ.id)
    assert nxt is not None and (nxt.seq, nxt.due_date, nxt.due_time) == (2, "2026-10-12", "20:00")
    assert tasks.get_occurrence(occ.id).completed_via == "apple"
    assert tasks.open_occurrence(task.id).id == nxt.id


def test_rescheduled_occurrence_does_not_shift_series(tasks):
    _, occ = tasks.create_task(CLI, "Riego", due_date="2026-10-05", rrule="FREQ=WEEKLY")
    tasks.reschedule(CLI, occ.id, due_date="2026-10-07")
    _, nxt = tasks.complete(CLI, occ.id)
    assert nxt.due_date == "2026-10-12"


def test_skip_and_external_delete_continue_series(tasks):
    task, occ = tasks.create_task(CLI, "Aspirar", due_date="2026-10-05", rrule="FREQ=DAILY")
    nxt = tasks.skip(CLI, occ.id)
    assert tasks.get_occurrence(occ.id).status == "skipped" and nxt.due_date == "2026-10-06"
    nxt2 = tasks.close_deleted_externally(ChangeContext.apple("cor"), nxt.id)
    closed = tasks.get_occurrence(nxt.id)
    assert (closed.status, closed.closed_reason) == ("cancelled", "deleted_externally")
    assert nxt2.due_date == "2026-10-07" and tasks.get_task(task.id).status == "active"


def test_reopen_cancels_generated_next(tasks):
    task, occ = tasks.create_task(CLI, "Filtro", due_date="2026-10-05", rrule="FREQ=MONTHLY")
    _, nxt = tasks.complete(CLI, occ.id)
    reopened = tasks.reopen(ChangeContext.apple("c"), occ.id)
    assert reopened.is_open and reopened.completed_at is None
    assert tasks.get_occurrence(nxt.id).closed_reason == "previous_reopened"
    assert tasks.open_occurrence(task.id).id == occ.id


def test_reopen_simple_task_reactivates_it(tasks):
    task, occ = tasks.create_task(CLI, "Simple")
    tasks.complete(CLI, occ.id)
    tasks.reopen(CLI, occ.id)
    assert tasks.get_task(task.id).status == "active"


def test_cancel_task_cancels_open_occurrence(tasks):
    task, occ = tasks.create_task(CLI, "Serie", due_date="2026-10-05", rrule="FREQ=DAILY")
    tasks.cancel_task(CLI, task.id)
    assert tasks.get_task(task.id).status == "cancelled"
    assert tasks.get_occurrence(occ.id).status == "cancelled"
    assert tasks.open_occurrence(task.id) is None


def test_series_with_count_completes_task(tasks):
    task, occ = tasks.create_task(
        CLI, "Dos veces", due_date="2026-10-05", rrule="FREQ=DAILY;COUNT=2"
    )
    _, second = tasks.complete(CLI, occ.id)
    _, third = tasks.complete(CLI, second.id)
    assert third is None and tasks.get_task(task.id).status == "completed"


def test_update_task_and_resolve_prefix(tasks):
    task, occ = tasks.create_task(CLI, "Antes")
    tasks.update_task(CLI, task.id, title="Después", notes="nota")
    assert tasks.get_task(task.id).title == "Después"
    t, o = tasks.resolve_id(occ.id[:12])
    assert t.id == task.id and o.id == occ.id
