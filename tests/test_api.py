import os
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ["CHECKIN_FERNET_KEY"] = Fernet.generate_key().decode()

from app import config, db, runner
from app.main import app


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path, monkeypatch):
    test_db = tmp_path / "test_api.db"
    monkeypatch.setattr(config, "DB_PATH", test_db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CHECKIN_WEB_PASSWORD", "")
    config.ensure_dirs()
    db.init_db()


def test_health_and_sites():
    with TestClient(app) as client:
        res = client.get("/api/health")
        assert res.status_code == 200
        assert res.json()["status"] == "ok"

        res_sites = client.get("/api/sites")
        assert res_sites.status_code == 200
        sites = res_sites.json()["sites"]
        assert len(sites) >= 3
        site_keys = [s["key"] for s in sites]
        assert "jiaobenwang" in site_keys
        assert "pikaqiu" in site_keys
        assert "ebondai" in site_keys


def test_dashboard():
    with TestClient(app) as client:
        res = client.get("/api/dashboard")
        assert res.status_code == 200
        data = res.json()
        assert "accounts" in data
        assert "stats" in data
        assert "autoCheckin" in data


def test_settings_api():
    with TestClient(app) as client:
        res = client.get("/api/settings")
        assert res.status_code == 200
        assert res.json()["time"] == "08:42"

        # PUT valid
        put_res = client.put("/api/settings", json={"enabled": True, "time": "09:15"})
        assert put_res.status_code == 200
        assert put_res.json()["time"] == "09:15"

        # PUT invalid time
        bad_res = client.put("/api/settings", json={"enabled": True, "time": "25:99"})
        assert bad_res.status_code == 400


def test_accounts_api_flow():
    with TestClient(app) as client:
        # Create account
        post_res = client.post(
            "/api/accounts",
            json={
                "siteKey": "jiaobenwang",
                "nickname": "主账号",
                "login": "test@qq.com",
                "password": "secretpassword",
                "enabled": True,
            },
        )
        assert post_res.status_code == 200
        acct_id = post_res.json()["accountId"]

        # Check in dashboard
        dash = client.get("/api/dashboard").json()
        assert len(dash["accounts"]) == 1
        assert dash["accounts"][0]["accountLabel"] == "t***@qq.com"

        # Toggle account
        toggle_res = client.post(f"/api/accounts/{acct_id}/toggle", json={"enabled": False})
        assert toggle_res.status_code == 200
        assert client.get("/api/dashboard").json()["accounts"][0]["enabled"] is False

        # Update account
        put_res = client.put(
            f"/api/accounts/{acct_id}",
            json={"nickname": "更新后的备注", "enabled": True},
        )
        assert put_res.status_code == 200
        assert client.get("/api/dashboard").json()["accounts"][0]["nickname"] == "更新后的备注"

        # Delete account
        del_res = client.delete(f"/api/accounts/{acct_id}")
        assert del_res.status_code == 200
        assert len(client.get("/api/dashboard").json()["accounts"]) == 0


def test_corrupted_credential_resilience():
    """测试某账号凭据损坏时，dashboard 仍能正常渲染而不抛 500。"""
    # 模拟一个使用损坏密文录入的账号
    db.create_account("jiaobenwang", "损坏账号", "corrupted_enc_string", "pwd_enc", None)
    with TestClient(app) as client:
        res = client.get("/api/dashboard")
        assert res.status_code == 200
        data = res.json()
        assert len(data["accounts"]) == 1
        assert data["accounts"][0]["decryptError"] is True
        assert "[凭据解密失败]" in data["accounts"][0]["accountLabel"]


def test_export_csv():
    with TestClient(app) as client:
        res = client.get("/api/export/today.csv")
        assert res.status_code == 200
        assert "text/csv" in res.headers["content-type"]
        assert "业务日期" in res.text


def test_watch_runs_sse_pushes_new_run(monkeypatch):
    """watchRuns：新运行启动时应通过 SSE 推送 runId（直接驱动 generator）。"""
    import asyncio
    import json

    from app.main import watch_runs

    run_id = db.create_run("scheduled", "2026-09-29")
    calls = {"n": 0}

    def fake_active_run_id():
        calls["n"] += 1
        # 连接瞬间与第一次轮询无运行，第二次轮询出现新运行
        return None if calls["n"] <= 2 else run_id

    monkeypatch.setattr(runner, "get_active_run_id", fake_active_run_id)

    async def _collect_one():
        resp = await watch_runs()
        assert "text/event-stream" in resp.headers["content-type"]
        async for chunk in resp.body_iterator:
            return chunk
        pytest.fail("watchRuns 未产生任何事件")

    chunk = asyncio.run(_collect_one())
    text = chunk.decode() if isinstance(chunk, bytes) else chunk
    assert '"runId"' in text
    payload = json.loads(text.replace("data:", "").strip())
    assert payload["type"] == "run"
    assert payload["runId"] == run_id


def test_watch_runs_requires_auth(monkeypatch):
    """设置访问密码时，watchRuns 应走与其它 /api/ 接口一致的鉴权。"""
    monkeypatch.setattr(config, "CHECKIN_WEB_PASSWORD", "s3cret")
    with TestClient(app) as client:
        res = client.get("/api/runs/watch")
        assert res.status_code == 401
        assert res.json()["authRequired"] is True


def test_web_password_auth(monkeypatch):
    monkeypatch.setattr(config, "CHECKIN_WEB_PASSWORD", "mypassword123")
    with TestClient(app) as client:
        # Unauthorized access
        res = client.get("/api/dashboard")
        assert res.status_code == 401
        assert res.json()["authRequired"] is True

        # Bad login
        bad_login = client.post("/api/auth/login", json={"password": "wrongpassword"})
        assert bad_login.status_code == 401

        # Good login
        login_res = client.post("/api/auth/login", json={"password": "mypassword123"})
        assert login_res.status_code == 200
        token = login_res.json()["token"]

        # Authorized access via cookie or Bearer token
        res_ok = client.get("/api/dashboard", headers={"Authorization": f"Bearer {token}"})
        assert res_ok.status_code == 200
