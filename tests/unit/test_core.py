from __future__ import annotations

import os
import stat

import pytest

from personal_os.core import secrets
from personal_os.core.config import ConfigError, load_config, write_config_template
from personal_os.core.db import Database
from personal_os.core.events import ChangeContext
from personal_os.core.ids import is_valid_id, new_id


def test_ids_are_prefixed_sortable_and_valid():
    a, b = new_id("task"), new_id("task")
    assert a.startswith("tsk_") and is_valid_id(a) and len(a) == 30
    assert a != b


def test_migrations_are_idempotent(tmp_path):
    from personal_os.cli.wiring import MIGRATIONS

    db = Database(tmp_path / "x.db")
    first = db.migrate(MIGRATIONS)
    assert first and db.migrate(MIGRATIONS) == []


def test_transaction_rolls_back(db):
    with pytest.raises(RuntimeError), db.transaction():
        db.execute("INSERT INTO sync_cursors(provider, key, value) VALUES ('p','k','v')")
        raise RuntimeError
    assert db.one("SELECT * FROM sync_cursors") is None


def test_event_log_records_actor_and_correlation(events):
    ctx = ChangeContext.apple("cor_1")
    events.record(ctx, "x.happened", "task", "tsk_1", {"a": 1})
    (ev,) = events.query(correlation_id="cor_1")
    assert (ev.actor, ev.type, ev.payload) == ("apple", "x.happened", {"a": 1})


def test_config_template_and_rejects_password_in_config():
    path = write_config_template()
    cfg = load_config()
    assert cfg.timezone == "Europe/Madrid" and cfg.apple.reminders_list == "Personal OS"
    path.write_text('[apple]\npassword = "nope"\n')
    with pytest.raises(ConfigError):
        load_config()


def test_secret_is_masked_and_file_is_private():
    path = secrets.set_secret("icloud_app_password", "abcd-efgh-ijkl-mnop")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    secret = secrets.require_secret("icloud_app_password")
    assert secret.reveal() == "abcd-efgh-ijkl-mnop"
    assert "abcd" not in repr(secret) and "abcd" not in f"{secret}"
    assert secrets.secret_status() == {"icloud_app_password": "file"}


def test_secret_rejected_when_permissions_open():
    path = secrets.set_secret("icloud_app_password", "x-y")
    os.chmod(path, 0o644)
    with pytest.raises(secrets.SecretError):
        secrets.get_secret("icloud_app_password")


def test_secret_env_override(monkeypatch):
    monkeypatch.setenv("POS_SECRET_ICLOUD_APP_PASSWORD", "from-env")
    assert secrets.require_secret("icloud_app_password").reveal() == "from-env"


def test_unknown_secret_rejected():
    with pytest.raises(secrets.SecretError):
        secrets.set_secret("otra_cosa", "x")
