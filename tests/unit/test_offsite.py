from __future__ import annotations

import gzip
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from personal_os import bootstrap
from personal_os.cli.main import app as cli
from personal_os.core import backup as bk
from personal_os.core import offsite
from personal_os.core.clock import FixedClock
from personal_os.core.config import ConfigError, config_dir, load_config, write_config_template
from personal_os.core.events import ChangeContext
from personal_os.ops.doctor import FAIL, OK, SKIP, WARN, run_doctor

CLI = ChangeContext.cli()
NOW = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)


@pytest.fixture
def clock():
    return FixedClock(NOW)


@pytest.fixture
def remote_dir(tmp_path) -> Path:
    d = tmp_path / "icloud" / "PersonalOS-backups"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def configured(remote_dir):
    """Config con copia externa y clave generada."""
    write_config_template()
    public = offsite.generate_identity(config_dir() / "backup-identity.txt")
    (config_dir() / "config.toml").write_text(
        f'[backup]\noffsite_dir = "{remote_dir}"\noffsite_recipient = "{public}"\n'
    )
    return public


@pytest.fixture
def app(configured, clock):
    application = bootstrap.open_app(clock=clock)
    svc = bootstrap.task_service(application)
    svc.create_task(CLI, "A", due_date="2026-10-06")
    svc.create_task(CLI, "B", due_date="2026-10-06")
    yield application
    application.db.close()


def identity():
    return offsite.load_identity(load_config().offsite_identity_path)


def recipient():
    return offsite.parse_recipient(load_config().backup.offsite_recipient)


# ------------------------------------------------------------------ claves


def test_keygen_creates_private_key_0600_and_never_overwrites(tmp_path):
    path = tmp_path / "k" / "id.txt"
    public = offsite.generate_identity(path)
    assert path.stat().st_mode & 0o777 == 0o600 and public.startswith("age1")
    assert str(offsite.load_identity(path).to_public()) == public
    before = path.read_text()
    with pytest.raises(offsite.OffsiteError, match="ya existe"):
        offsite.generate_identity(path)
    assert path.read_text() == before


def test_identity_with_open_permissions_or_garbage_is_rejected(tmp_path):
    path = tmp_path / "id.txt"
    offsite.generate_identity(path)
    path.chmod(0o644)
    with pytest.raises(offsite.OffsiteError, match="permisos"):
        offsite.load_identity(path)
    path.write_text("no es una clave\n")
    path.chmod(0o600)
    with pytest.raises(offsite.OffsiteError):
        offsite.load_identity(path)
    with pytest.raises(offsite.OffsiteError, match="no válida"):
        offsite.parse_recipient("age1nope")


# ------------------------------------------------------------------ subir, verificar, bajar


def test_push_writes_encrypted_copy_that_verifies_only_with_the_key(app, clock, remote_dir):
    info = bk.create_backup(app.config.db_path, app.config.backup_dir, clock)
    remote = offsite.push(info, remote_dir, recipient())
    assert remote.path.name == "pos-20261005T080000Z.db.gz.age" and remote.encrypted
    assert remote.manifest_path.exists()
    data = remote.path.read_bytes()
    assert data.startswith(b"age-encryption.org/v1") and data != info.path.read_bytes()
    (listed,) = bk.list_backups(remote_dir)
    assert listed.encrypted and listed.manifest["tables"]["tasks"] == 2
    assert bk.verify_backup(listed, identity()).ok
    assert "clave" in bk.verify_backup(listed).problems[0]
    other = offsite.load_identity(_other_identity(app.config.config_dir))
    assert "descifrar" in bk.verify_backup(listed, other).problems[0]


def _other_identity(where: Path) -> Path:
    path = where / "otra.txt"
    offsite.generate_identity(path)
    return path


@pytest.mark.skipif(shutil.which("age") is None, reason="binario age no instalado")
def test_encrypted_copy_decrypts_with_official_age_binary(app, clock, remote_dir, tmp_path):
    info = bk.create_backup(app.config.db_path, app.config.backup_dir, clock)
    remote = offsite.push(info, remote_dir, recipient())
    out = tmp_path / "out.db.gz"
    subprocess.run(
        [
            "age",
            "-d",
            "-i",
            str(app.config.offsite_identity_path),
            "-o",
            str(out),
            str(remote.path),
        ],
        check=True,
    )
    assert out.read_bytes() == info.path.read_bytes()
    assert gzip.decompress(out.read_bytes())[:16] == b"SQLite format 3\x00"


def test_tampered_remote_copy_fails_verification(app, clock, remote_dir):
    info = bk.create_backup(app.config.db_path, app.config.backup_dir, clock)
    remote = offsite.push(info, remote_dir, recipient())
    data = bytearray(remote.path.read_bytes())
    data[-10] ^= 0xFF
    remote.path.write_bytes(bytes(data))
    assert not bk.verify_backup(bk.list_backups(remote_dir)[0], identity()).ok


def test_push_to_missing_destination_fails_clearly(app, clock, tmp_path):
    info = bk.create_backup(app.config.db_path, app.config.backup_dir, clock)
    with pytest.raises(offsite.OffsiteError, match="no existe"):
        offsite.push(info, tmp_path / "no-montado", recipient())


def test_restore_refuses_encrypted_backup_directly(app, clock, remote_dir):
    info = bk.create_backup(app.config.db_path, app.config.backup_dir, clock)
    remote = offsite.push(info, remote_dir, recipient())
    with pytest.raises(bk.BackupError, match="fetch"):
        bk.restore_into(remote, app.config.db_path)


def test_disaster_recovery_from_remote_copy_only(app, clock, remote_dir):
    """Se pierden la base y los backups locales: solo queda la copia externa y la clave."""
    info = bk.create_backup(app.config.db_path, app.config.backup_dir, clock)
    offsite.push(info, remote_dir, recipient())
    expected = info.manifest["tables"]
    app.db.close()
    shutil.rmtree(app.config.backup_dir)
    for suffix in ("", "-wal", "-shm"):
        Path(str(app.config.db_path) + suffix).unlink(missing_ok=True)

    remote = bk.resolve_backup(remote_dir, "latest")
    local = offsite.fetch(remote, app.config.backup_dir, identity())
    assert not local.encrypted and bk.verify_backup(local).ok
    result = bk.restore_backup(
        local, app.config.db_path, app.config.backup_dir, clock, bootstrap.MIGRATIONS
    )
    assert result.stats.tables == expected and result.pre_restore is None
    with pytest.raises(offsite.OffsiteError, match="Ya existe"):
        offsite.fetch(remote, app.config.backup_dir, identity())
    app.db = bootstrap.open_app(clock=clock).db  # para el cierre del fixture


def test_fetch_with_wrong_key_leaves_nothing_behind(app, clock, remote_dir):
    info = bk.create_backup(app.config.db_path, app.config.backup_dir, clock)
    remote = offsite.push(info, remote_dir, recipient())
    dest = app.config.data_dir / "recuperado"
    other = offsite.load_identity(_other_identity(app.config.config_dir))
    with pytest.raises(offsite.OffsiteError, match="descifrar"):
        offsite.fetch(remote, dest, other)
    assert list(dest.iterdir()) == []


# ------------------------------------------------------------------ sync y retención


def _sync(app):
    b = app.config.backup
    return offsite.sync(
        app.config.backup_dir,
        b.offsite_dir,
        recipient(),
        keep_daily=b.keep_daily,
        keep_weekly=b.keep_weekly,
        keep_monthly=b.keep_monthly,
    )


def test_sync_is_idempotent_and_self_heals_after_failure(app, clock, remote_dir):
    bk.create_backup(app.config.db_path, app.config.backup_dir, clock)
    assert _sync(app).pushed is not None  # primera subida
    assert len(bk.list_backups(remote_dir)) == 1
    assert _sync(app).pushed is None  # ya está fuera

    clock.advance(days=1)
    bk.create_backup(app.config.db_path, app.config.backup_dir, clock)
    remote_dir.rename(remote_dir.with_name("desmontado"))
    with pytest.raises(offsite.OffsiteError):
        _sync(app)
    remote_dir.with_name("desmontado").rename(remote_dir)
    assert _sync(app).pushed.path.name == "pos-20261006T080000Z.db.gz.age"


def test_sync_applies_retention_on_remote(app, clock, remote_dir):
    start = NOW
    for day in range(20):
        c = FixedClock(start + timedelta(days=day))
        offsite.push(
            bk.create_backup(app.config.db_path, app.config.backup_dir, c), remote_dir, recipient()
        )
    app.config = replace(
        app.config,
        backup=replace(app.config.backup, keep_daily=5, keep_weekly=0, keep_monthly=0),
    )
    result = _sync(app)
    remaining = bk.list_backups(remote_dir)
    assert len(remaining) == 5 and len(result.removed) == 15 and not result.warnings
    assert all(b.encrypted for b in remaining)
    assert len(list(remote_dir.glob("*.json"))) == 5  # se borran también los manifiestos


def test_auto_backup_pushes_remote_copy_and_never_breaks_sync(app, clock, remote_dir):
    info, pushed, warnings = bootstrap.auto_backup(app)
    assert info is not None and pushed.path.name == info.path.name + ".age" and not warnings

    clock.advance(hours=25)
    remote_dir.rename(remote_dir.with_name("desmontado"))
    info, pushed, warnings = bootstrap.auto_backup(app)
    assert info is not None and pushed is None
    assert any("Copia externa fallida" in w for w in warnings)

    remote_dir.with_name("desmontado").rename(remote_dir)
    info, pushed, warnings = bootstrap.auto_backup(app)  # sin backup nuevo, pero se recupera
    assert info is None and pushed is not None and not warnings


# ------------------------------------------------------------------ config


def test_config_requires_dir_and_recipient_together(tmp_path):
    path = write_config_template()
    path.write_text(f'[backup]\noffsite_dir = "{tmp_path}"\n')
    with pytest.raises(ConfigError, match="van juntos"):
        load_config()
    path.write_text(
        f'[backup]\ndir = "{tmp_path}"\noffsite_dir = "{tmp_path}"\noffsite_recipient = "x"\n'
    )
    with pytest.raises(ConfigError, match="mismo directorio"):
        load_config()


# ------------------------------------------------------------------ doctor


def offsite_checks(clock):
    return [c for c in run_doctor(offline=True, clock=clock) if c.area == "offsite"]


def test_doctor_offsite_not_configured_is_skipped_and_same_disk_warns(clock):
    write_config_template()
    bootstrap.open_app(clock=clock).db.close()
    cfg = load_config()
    bk.create_backup(cfg.db_path, cfg.backup_dir, clock)
    checks = run_doctor(offline=True, clock=clock)
    assert {c.status for c in checks if c.area == "offsite"} == {SKIP}
    assert any("mismo disco" in c.message for c in checks)


def test_doctor_offsite_healthy(app, clock):
    bootstrap.auto_backup(app)
    checks = run_doctor(offline=True, clock=clock)
    assert {c.status for c in checks if c.area == "offsite"} == {OK}
    assert not any("mismo disco" in c.message for c in checks)


def test_doctor_offsite_missing_copies_and_stale(app, clock):
    assert {c.status for c in offsite_checks(clock)} == {WARN}
    bootstrap.auto_backup(app)
    clock.advance(days=3)
    assert any(c.status == WARN and "última hace" in c.message for c in offsite_checks(clock))


def test_doctor_offsite_key_problems(app, clock):
    bootstrap.auto_backup(app)
    key = app.config.offsite_identity_path
    original = key.read_text()

    key.unlink()
    assert any(
        c.status == WARN and "No existe la clave" in c.message for c in offsite_checks(clock)
    )

    offsite.generate_identity(key)  # una clave distinta
    assert any(c.status == FAIL and "no corresponde" in c.message for c in offsite_checks(clock))

    key.write_text(original)
    key.chmod(0o644)
    assert any(c.status == FAIL and "permisos" in c.message for c in offsite_checks(clock))


def test_doctor_offsite_unreachable_or_through_symlink(app, clock, remote_dir, tmp_path):
    bootstrap.auto_backup(app)
    link = tmp_path / "enlace"
    link.symlink_to(remote_dir.parent)
    cfg = config_dir() / "config.toml"
    cfg.write_text(cfg.read_text().replace(str(remote_dir), str(link / remote_dir.name)))
    assert any(c.status == WARN and "enlace simbólico" in c.message for c in offsite_checks(clock))

    cfg.write_text(cfg.read_text().replace(str(link / remote_dir.name), str(tmp_path / "nada")))
    (check,) = offsite_checks(clock)
    assert check.status == FAIL and "no accesible" in check.message


# ------------------------------------------------------------------ CLI


def test_cli_keygen_offsite_list_verify_fetch(tmp_path, remote_dir):
    runner = CliRunner()
    write_config_template()
    res = runner.invoke(cli, ["backup", "keygen"])
    assert res.exit_code == 0 and "gestor de contraseñas" in res.output
    public = next(w for w in res.output.split() if w.startswith("age1"))
    assert runner.invoke(cli, ["backup", "keygen"]).exit_code == 1  # nunca sobrescribe

    (config_dir() / "config.toml").write_text(
        f'[backup]\noffsite_dir = "{remote_dir}"\noffsite_recipient = "{public}"\n'
    )
    bootstrap.open_app().db.close()
    res = runner.invoke(cli, ["backup", "create"])
    assert res.exit_code == 0 and "copia cifrada" in res.output
    res = runner.invoke(cli, ["backup", "offsite"])
    assert res.exit_code == 0 and "al día" in res.output
    res = runner.invoke(cli, ["backup", "list", "--offsite"])
    assert ".db.gz.age" in res.output
    assert runner.invoke(cli, ["backup", "verify", "--offsite"]).exit_code == 0

    local = load_config().backup_dir
    shutil.rmtree(local)
    res = runner.invoke(cli, ["backup", "fetch"])
    assert res.exit_code == 0 and len(bk.list_backups(local)) == 1
