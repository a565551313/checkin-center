import os
import pytest
from datetime import datetime
from zoneinfo import ZoneInfo
from cryptography.fernet import Fernet

os.environ["CHECKIN_FERNET_KEY"] = Fernet.generate_key().decode()

from app import config, db, scheduler, timeutil


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_sched.db"
    monkeypatch.setattr(config, "DB_PATH", test_db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    config.ensure_dirs()
    db.init_db()


def test_should_run_disabled():
    db.update_settings(False, "08:00")
    ok, reason = scheduler.should_run()
    assert ok is False
    assert "未开启" in reason


def test_next_run_at():
    db.update_settings(True, "23:59")
    nxt = scheduler.next_run_at()
    assert nxt is not None
    assert "23:59:00" in nxt
