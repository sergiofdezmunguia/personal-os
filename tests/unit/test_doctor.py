from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from personal_os import bootstrap
from personal_os.cli.main import app as cli
from personal_os.core import backup as bk
from personal_os.core import secrets
from personal_os.core.clock import FixedClock
from personal_os.core.config import config_dir, data_dir
from personal_os.ops.doctor import FAIL, OK, SKIP, WARN, repo_bridge_version, run_doctor
from personal_os.sync.store import Cursors

NOW = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)


@pytest.fixture
def clock():
    return FixedClock(NOW)


@pytest.fixture
def healthy(tmp_path, clock):
    """Instalación sana: config, BD, backup reciente, buzón, bridge instalado y snapshot reciente."""
    scriptable = tmp_path / "Scriptable"
    (scriptable / "personal-os" / "outbox").mkdir(parents=True)
    (scriptable / "personal-os" / "inbox").mkdir(parents=True)
    (scriptable / bootstrap.BRIDGE_SCRIPT_NAME).write_text(bootstrap.BRIDGE_SOURCE.read_text())
    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / "config.toml").write_text(
        'timezone = "Atlantic/Canary"\n[apple]\n'
        f'mailbox_dir = "{scriptable / "personal-os"}"\n'
        f'[backup]\ndir = "{tmp_path / "offsite"}"\n'
    )
    application = bootstrap.open_app(clock=clock)
    cur = Cursors(application.db)
    cur.set("apple_reminders", "snapshot_taken_at", "2026-10-05T07:00:00Z")
    cur.set("apple_reminders", "bridge_version", repo_bridge_version())
    cur.set("apple_reminders", "device_timezone", "Atlantic/Canary")
    cur.set("apple_reminders", "list_found", "1")
    application.db.close()
    bk.create_backup(
        data_dir() / "pos.db", tmp_path / "offsite", FixedClock(NOW - timedelta(hours=2))
    )
    return {"scriptable": scriptable, "tmp": tmp_path}


def by_area(checks, area):
    return [c for c in checks if c.area == area]


def statuses(checks, area):
    return {c.status for c in by_area(checks, area)}


def test_healthy_install_has_no_failures(healthy, clock):
    checks = run_doctor(offline=True, clock=clock)
    assert not [c for c in checks if c.status == FAIL], checks
    assert statuses(checks, "base de datos") == {OK}
    assert statuses(checks, "backups") == {OK}
    assert statuses(checks, "bridge") == {OK}
    assert statuses(checks, "zona horaria") == {OK}
    assert statuses(checks, "caldav") == {SKIP}


def test_missing_database_and_config(clock):
    checks = run_doctor(offline=True, clock=clock)
    assert statuses(checks, "config") == {WARN}
    assert statuses(checks, "base de datos") == {FAIL}


def test_timezone_mismatch_is_a_failure(healthy, clock):
    application = bootstrap.open_app(clock=clock)
    Cursors(application.db).set("apple_reminders", "device_timezone", "Europe/Madrid")
    application.db.close()
    (c,) = by_area(run_doctor(offline=True, clock=clock), "zona horaria")
    assert (
        c.status == FAIL and "Europe/Madrid" in c.message and 'timezone = "Europe/Madrid"' in c.hint
    )


def test_stale_mailbox_and_old_bridge(healthy, clock):
    outbox = healthy["scriptable"] / "personal-os" / "outbox"
    batch = outbox / "batch-bat_01OLD.json"
    batch.write_text("{}")
    old = (NOW - timedelta(days=2)).timestamp()
    os.utime(batch, (old, old))
    installed = healthy["scriptable"] / bootstrap.BRIDGE_SCRIPT_NAME
    installed.write_text(installed.read_text().replace(repo_bridge_version(), "0.9.0"))
    checks = run_doctor(offline=True, clock=clock)
    assert any(c.status == WARN and "sin procesar" in c.message for c in by_area(checks, "buzón"))
    assert any(c.status == WARN and "0.9.0" in c.message for c in by_area(checks, "bridge"))


def test_stale_snapshot_and_missing_list(healthy, clock):
    clock.advance(days=3)
    application = bootstrap.open_app(clock=clock)
    Cursors(application.db).set("apple_reminders", "list_found", "0")
    application.db.close()
    bridge = by_area(run_doctor(offline=True, clock=clock), "bridge")
    assert any(c.status == WARN and "Último snapshot" in c.message for c in bridge)
    assert any(c.status == FAIL and "lista" in c.message for c in bridge)


def test_backups_old_corrupt_or_same_disk(healthy, clock, tmp_path):
    clock.advance(days=3)
    (latest,) = bk.list_backups(tmp_path / "offsite")
    latest.path.write_bytes(b"roto")
    checks = by_area(run_doctor(offline=True, clock=clock), "backups")
    assert any(c.status == WARN and "hace" in c.message for c in checks)
    assert any(c.status == FAIL and "NO se verifica" in c.message for c in checks)


def test_backups_on_same_disk_warn(tmp_path, clock):
    bootstrap.open_app(clock=clock).db.close()
    bk.create_backup(data_dir() / "pos.db", data_dir() / "backups", clock)
    checks = by_area(run_doctor(offline=True, clock=clock), "backups")
    assert any(c.status == WARN and "mismo disco" in c.message for c in checks)


def test_open_secret_permissions_fail(healthy, clock):
    path = secrets.set_secret("icloud_app_password", "x-y")
    os.chmod(path, 0o644)
    (c,) = by_area(run_doctor(offline=True, clock=clock), "secretos")
    assert c.status == FAIL and "chmod 600" in c.hint


def test_cli_doctor_exit_codes(healthy):
    runner = CliRunner()
    ok = runner.invoke(cli, ["doctor", "--offline"])
    assert ok.exit_code == 0, ok.output
    (config_dir() / "config.toml").write_text('timezone = "Marte/Olympus"\n')
    bad = runner.invoke(cli, ["doctor", "--offline"])
    assert bad.exit_code == 1 and "Zona horaria inválida" in bad.output


def test_cli_backup_restore_requires_confirmation(healthy):
    runner = CliRunner()
    res = runner.invoke(cli, ["backup", "restore", "latest"])
    assert res.exit_code == 2 and "--yes" in res.output
    res = runner.invoke(cli, ["backup", "restore", "latest", "--yes"])
    assert res.exit_code == 0 and "Restaurado" in res.output
