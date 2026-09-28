import os
import pytest
from cryptography.fernet import Fernet

os.environ["CHECKIN_FERNET_KEY"] = Fernet.generate_key().decode()

from app import db, config


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_checkin.db"
    monkeypatch.setattr(config, "DB_PATH", test_db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db.init_db()


def test_settings_crud():
    s = db.get_settings()
    assert s["enabled"] == 1
    assert s["time"] == "08:42"

    updated = db.update_settings(False, "09:30")
    assert updated["enabled"] == 0
    assert updated["time"] == "09:30"


def test_accounts_crud():
    acct = db.create_account(
        site_key="jiaobenwang",
        nickname="测试账号",
        login_enc="enc_login",
        password_enc="enc_pwd",
        api_key_enc=None,
        enabled=True,
    )
    assert acct is not None
    assert acct["site_key"] == "jiaobenwang"
    assert acct["nickname"] == "测试账号"

    fetched = db.get_account(acct["id"])
    assert fetched["id"] == acct["id"]

    all_accts = db.list_accounts()
    assert len(all_accts) == 1

    updated = db.update_account(acct["id"], nickname="新昵称", enabled=False)
    assert updated["nickname"] == "新昵称"
    assert updated["enabled"] == 0

    deleted = db.delete_account(acct["id"])
    assert deleted is True
    assert db.get_account(acct["id"]) is None


def test_runs_and_results():
    biz = "2026-09-29"
    run_id = db.create_run(mode="manual", business_date=biz)
    assert run_id is not None

    db.add_steps(run_id, [{"ts": "2026-09-29T08:00:00", "site_key": "jiaobenwang", "message": "步骤1"}])
    steps = db.get_steps_after(run_id, after_id=0)
    assert len(steps) == 1
    assert steps[0]["message"] == "步骤1"

    res_id = db.add_result(
        run_id=run_id,
        account_id="acc-1",
        site_key="jiaobenwang",
        account_label="u***@qq.com",
        business_date=biz,
        status="success",
        conclusion="签到成功",
        metric_label="贡献分",
        metric_value="100",
        streak_days=5,
    )
    assert res_id > 0

    db.finish_run(run_id, status="completed", total=1, succeeded=1, failed=0)
    run = db.get_run(run_id)
    assert run["status"] == "completed"
    assert run["total"] == 1
    assert len(run["results"]) == 1

    latest = db.latest_results_by_account(biz)
    assert "acc-1" in latest
    assert latest["acc-1"]["status"] == "success"

    runs, total = db.list_runs(limit=10)
    assert total == 1
    assert len(runs) == 1


def test_summary_stats():
    biz = "2026-09-29"
    a1 = db.create_account("jiaobenwang", "A1", "enc", "enc", None, enabled=True)
    a2 = db.create_account("pikaqiu", "A2", "enc", "enc", None, enabled=True)
    a3 = db.create_account("ebondai", "A3", "enc", "enc", "enc", enabled=False)

    stats = db.get_summary_stats(biz)
    assert stats["totalAccounts"] == 3
    assert stats["enabledAccounts"] == 2
    assert stats["todayPending"] == 2
    assert stats["todaySuccess"] == 0
    assert stats["todayFailed"] == 0

    # 签到 A1 成功
    run_id = db.create_run("manual", biz)
    db.add_result(run_id, a1["id"], "jiaobenwang", "label", biz, "success")
    stats2 = db.get_summary_stats(biz)
    assert stats2["todaySuccess"] == 1
    assert stats2["todayPending"] == 1
