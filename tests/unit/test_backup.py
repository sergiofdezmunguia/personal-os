from __future__ import annotations

import gzip
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from personal_os import bootstrap
from personal_os.core import backup as bk
from personal_os.core.clock import FixedClock
from personal_os.core.config import load_config, write_config_template
from personal_os.core.db import Database
from personal_os.core.events import ChangeContext
from personal_os.ops.drill import run_drill

CLI = ChangeContext.cli()


@pytest.fixture
def clock():
    return FixedClock(datetime(2026, 10, 5, 8, 0, tzinfo=UTC))


@pytest.fixture
def app(clock):
    write_config_template()
    application = bootstrap.open_app(clock=clock)
    yield application
    application.db.close()


def add_tasks(app, *titles):
    svc = bootstrap.task_service(app)
    return [svc.create_task(CLI, t, due_date="2026-10-06")[0] for t in titles]


def backup_dir(app) -> Path:
    return app.config.backup_dir


# ------------------------------------------------------------------ crear y verificar


def test_create_backup_writes_verified_gz_and_manifest(app, clock):
    add_tasks(app, "A", "B")
    info = bk.create_backup(app.config.db_path, backup_dir(app), clock)
    assert info.path.name == "pos-20261005T080000Z.db.gz" and info.manifest_path.exists()
    m = json.loads(info.manifest_path.read_text())
    assert m["format"] == "pos-backup/1" and m["integrity"] == "ok"
    assert m["tables"]["tasks"] == 2 and "core/0001_events.sql" in m["migrations"]
    assert bk.verify_backup(info).ok


def test_backup_is_consistent_with_uncheckpointed_wal(app, clock):
    """Los datos que solo están en el WAL (sin checkpoint) también entran en el backup."""
    add_tasks(app, "en el WAL")
    assert Path(str(app.config.db_path) + "-wal").stat().st_size > 0
    info = bk.create_backup(app.config.db_path, backup_dir(app), clock)
    assert info.manifest["tables"]["tasks"] == 1


def test_same_second_backups_get_unique_names(app, clock):
    a = bk.create_backup(app.config.db_path, backup_dir(app), clock)
    b = bk.create_backup(app.config.db_path, backup_dir(app), clock)
    assert a.path != b.path and len(bk.list_backups(backup_dir(app))) == 2


def test_verify_detects_corruption_and_tampering(app, clock):
    info = bk.create_backup(app.config.db_path, backup_dir(app), clock)
    data = bytearray(info.path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    info.path.write_bytes(bytes(data))
    assert not bk.verify_backup(info).ok

    other = bk.create_backup(app.config.db_path, backup_dir(app), clock)
    m = json.loads(other.manifest_path.read_text())
    m["tables"]["tasks"] = 999
    other.manifest_path.write_text(json.dumps(m))
    (reloaded,) = [b for b in bk.list_backups(backup_dir(app)) if b.path == other.path]
    problems = bk.verify_backup(reloaded).problems
    assert any("filas" in p for p in problems)


def test_missing_manifest_is_reported(app, clock):
    info = bk.create_backup(app.config.db_path, backup_dir(app), clock)
    info.manifest_path.unlink()
    (b,) = bk.list_backups(backup_dir(app))
    assert not bk.verify_backup(b).ok


# ------------------------------------------------------------------ restaurar


def test_restore_returns_exact_state_and_keeps_pre_restore(app, clock):
    add_tasks(app, "antes")
    info = bk.create_backup(app.config.db_path, backup_dir(app), clock)
    add_tasks(app, "después 1", "después 2")
    app.db.close()

    clock.advance(minutes=5)
    result = bk.restore_backup(
        info, app.config.db_path, backup_dir(app), clock, bootstrap.MIGRATIONS
    )

    reopened = bootstrap.open_app(clock=clock)
    titles = [t.title for t in bootstrap.task_service(reopened).list_tasks()]
    reopened.db.close()
    assert titles == ["antes"]
    assert result.pre_restore is not None and result.pre_restore.label == "pre-restore"
    assert result.pre_restore.manifest["tables"]["tasks"] == 3  # el estado sustituido no se pierde


def test_restore_discards_stale_wal(app, clock, tmp_path):
    add_tasks(app, "en backup")
    info = bk.create_backup(app.config.db_path, backup_dir(app), clock)
    add_tasks(app, "solo en WAL")  # queda en el -wal del fichero actual
    wal = Path(str(app.config.db_path) + "-wal")
    stale = wal.read_bytes()
    app.db.close()
    wal.write_bytes(stale)  # simulamos un -wal huérfano
    bk.restore_backup(info, app.config.db_path, backup_dir(app), clock, bootstrap.MIGRATIONS)
    stats = bk.inspect_db(app.config.db_path)
    assert stats.integrity == "ok" and stats.tables["tasks"] == 1


def test_restore_old_schema_migrates_forward(tmp_path, clock):
    old = Database(tmp_path / "old.db")
    old.migrate([("core", "personal_os.core")])  # base de una versión anterior
    old.close()
    info = bk.create_backup(tmp_path / "old.db", tmp_path / "b", clock)
    target = tmp_path / "data" / "pos.db"
    result = bk.restore_backup(info, target, tmp_path / "b", clock, bootstrap.MIGRATIONS)
    assert result.pre_restore is None
    assert {"tasks/0001_tasks.sql", "calendar/0001_calendar.sql"} <= set(result.migrations_applied)


def test_restore_refuses_invalid_backup(app, clock):
    info = bk.create_backup(app.config.db_path, backup_dir(app), clock)
    info.path.write_bytes(gzip.compress(b"no soy sqlite"))
    with pytest.raises(bk.BackupError, match="no se restaura"):
        bk.restore_backup(info, app.config.db_path, backup_dir(app), clock, bootstrap.MIGRATIONS)


def test_restore_over_corrupt_database_keeps_damaged_copy(app, clock):
    add_tasks(app, "buena")
    info = bk.create_backup(app.config.db_path, backup_dir(app), clock)
    app.db.close()
    for suffix in ("-wal", "-shm"):
        Path(str(app.config.db_path) + suffix).unlink(missing_ok=True)
    app.config.db_path.write_bytes(b"basura" * 1000)
    result = bk.restore_backup(
        info, app.config.db_path, backup_dir(app), clock, bootstrap.MIGRATIONS
    )
    assert result.pre_restore is None
    assert list(backup_dir(app).glob("pos-*-damaged.db"))
    assert bk.inspect_db(app.config.db_path).tables["tasks"] == 1


# ------------------------------------------------------------------ retención


def _fake(dest: Path, when: datetime, label: str | None = None) -> None:
    name = f"pos-{when:%Y%m%dT%H%M%SZ}{'-' + label if label else ''}.db.gz"
    (dest / name).write_bytes(b"x")


def test_retention_grandfather_father_son(tmp_path):
    start = datetime(2026, 1, 1, 3, tzinfo=UTC)
    for day in range(200):  # dos backups al día durante 200 días
        _fake(tmp_path, start + timedelta(days=day))
        _fake(tmp_path, start + timedelta(days=day, hours=12))
    _fake(tmp_path, start, label="pre-restore")
    _fake(tmp_path, start, label="manual")
    keep = bk.select_to_keep(bk.list_backups(tmp_path), keep_daily=7, keep_weekly=4, keep_monthly=3)
    kept = sorted(p.name for p in keep)
    assert len(kept) <= 7 + 4 + 3
    assert kept[-1] == "pos-20260719T150000Z.db.gz"  # el más reciente
    removed, warnings = bk.prune(tmp_path, keep_daily=7, keep_weekly=4, keep_monthly=3)
    remaining = {b.path.name for b in bk.list_backups(tmp_path)}
    assert not warnings and "pos-20260101T030000Z-manual.db.gz" in remaining
    assert "pos-20260101T030000Z-pre-restore.db.gz" in remaining
    assert len(removed) == 400 - len(kept)


def test_needs_auto_backup(tmp_path, clock):
    assert bk.needs_auto_backup(tmp_path, clock)
    _fake(tmp_path, clock.now() - timedelta(hours=3))
    assert not bk.needs_auto_backup(tmp_path, clock)
    clock.advance(hours=22)
    assert bk.needs_auto_backup(tmp_path, clock)


# ------------------------------------------------------------------ automático y simulacro


def test_auto_backup_runs_once_per_day(app, clock):
    first, _ = bootstrap.auto_backup(app)
    second, _ = bootstrap.auto_backup(app)
    assert first is not None and second is None
    clock.advance(hours=25)
    third, _ = bootstrap.auto_backup(app)
    assert third is not None


def test_auto_backup_can_be_disabled(app):
    app.config = replace(app.config, backup=replace(app.config.backup, auto_daily=False))
    assert bootstrap.auto_backup(app) == (None, [])


def test_drill_restores_and_reads_with_domain_services(app):
    add_tasks(app, "uno", "dos")
    bootstrap.calendar_service(app).create(CLI, "Evento", starts_at="2026-10-06T10:00")
    report = run_drill(app)
    assert report.ok, report.steps
    names = [name for name, _, _ in report.steps]
    assert names == [
        "backup",
        "verificación",
        "restauración",
        "filas tras migrar",
        "lectura con el dominio",
    ]
    assert "2 tareas y 1 eventos" in report.steps[-1][2]


def test_drill_fails_on_broken_backup(app, clock):
    info = bk.create_backup(app.config.db_path, backup_dir(app), clock)
    info.path.write_bytes(b"roto")
    report = run_drill(app, info.path.name)
    assert not report.ok and report.steps[-1][0] == "verificación"


def test_drill_does_not_touch_real_database(app):
    add_tasks(app, "real")
    before = bk.inspect_db(app.config.db_path).tables
    run_drill(app)
    assert bk.inspect_db(app.config.db_path).tables == before


def test_config_backup_section(tmp_path):
    path = write_config_template()
    path.write_text(f'[backup]\ndir = "{tmp_path}/bk"\nauto_daily = false\nkeep_daily = 3\n')
    cfg = load_config()
    assert (
        cfg.backup_dir == tmp_path / "bk"
        and cfg.backup.auto_daily is False
        and cfg.backup.keep_daily == 3
    )
