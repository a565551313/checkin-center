import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app import runner


@pytest.fixture(autouse=True)
def reset_runner_state():
    with runner._run_lock:
        runner._active_run_id = None
        runner._current_proc = None
        runner._cancel_requested = False
    yield
    with runner._run_lock:
        runner._active_run_id = None
        runner._current_proc = None
        runner._cancel_requested = False


def _prepare_runner_script(monkeypatch, tmp_path, content):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script_name = runner.config.SITES["jiaobenwang"]["script"]
    (scripts_dir / script_name).write_text(content, encoding="utf-8")
    monkeypatch.setattr(runner.config, "SCRIPTS_DIR", scripts_dir)
    monkeypatch.setattr(runner.config, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(runner.crypto, "safe_decrypt", lambda value: (value, True))
    account = {
        "id": "test-account",
        "site_key": "jiaobenwang",
        "login_enc": "account-user",
        "password_enc": "account-password",
        "api_key_enc": None,
    }
    monkeypatch.setattr(runner.db, "get_account", lambda account_id: account)
    return account


def _run_script(account, tmp_path, timeout=3):
    return runner._run_one_script(
        "test-run",
        account,
        tmp_path / "progress.jsonl",
        {"offset": 0},
        timeout=timeout,
    )


def test_parse_script_json():
    valid = runner._parse_script_json('{"ok": true, "message": "签到成功"}')
    assert valid["ok"] is True
    assert valid["message"] == "签到成功"

    leading = runner._parse_script_json('some debug logs...\n{"ok": true, "points": 50}')
    assert leading["ok"] is True
    assert leading["points"] == 50

    assert runner._parse_script_json("")["ok"] is False
    assert runner._parse_script_json("not a json at all")["ok"] is False


def test_is_network_error():
    assert runner._is_network_error({"error_type": "network"}) is True
    assert runner._is_network_error({"error": "请求超时 Connection timeout"}) is True
    assert runner._is_network_error({"error": "密码错误"}) is False


def test_build_env_is_limited_but_keeps_selected_site_fallback(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/tmp/home")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example")
    monkeypatch.setenv("EBONDAI_EMAIL", "global@example.com")
    monkeypatch.setenv("EBONDAI_API_KEY", "global-ebondai-key")
    monkeypatch.setenv("PIKAQIU_PASSWORD", "other-site-secret")
    monkeypatch.setenv("CHECKIN_FERNET_KEY", "application-secret")
    monkeypatch.setenv("CHECKIN_CRED_DIR", "/global/credentials")
    monkeypatch.setenv("NOTIFY_ENABLED", "true")

    env = runner._build_env("ebondai", None, None, None)

    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/tmp/home"
    assert env["HTTPS_PROXY"] == "http://proxy.example"
    assert env["EBONDAI_EMAIL"] == "global@example.com"
    assert env["EBONDAI_API_KEY"] == "global-ebondai-key"
    assert "PIKAQIU_PASSWORD" not in env
    assert "CHECKIN_FERNET_KEY" not in env
    assert "CHECKIN_CRED_DIR" not in env
    assert "NOTIFY_ENABLED" not in env

    account_env = runner._build_env("ebondai", "db@example.com", "db-password", "db-key")
    assert account_env["EBONDAI_EMAIL"] == "db@example.com"
    assert account_env["EBONDAI_PASSWORD"] == "db-password"
    assert account_env["EBONDAI_API_KEY"] == "db-key"


def test_run_requires_exit_zero_and_explicit_ok_true(monkeypatch, tmp_path):
    account = _prepare_runner_script(
        monkeypatch,
        tmp_path,
        'import json\nprint(json.dumps({"ok": False, "message": "业务未成功"}))\n',
    )

    result = _run_script(account, tmp_path)

    assert result["ok"] is False
    assert "业务未成功" in result["error"]


def test_nonzero_exit_cannot_be_reported_as_success(monkeypatch, tmp_path):
    account = _prepare_runner_script(
        monkeypatch,
        tmp_path,
        'import json, sys\nprint(json.dumps({"ok": True, "message": "签到成功"}))\nsys.exit(7)\n',
    )

    result = _run_script(account, tmp_path)

    assert result["ok"] is False
    assert "退出码 7" in result["error"]
    assert runner._summarize("jiaobenwang", result)[0] == "failed"


def test_zero_exit_without_explicit_success_field_is_failure(monkeypatch, tmp_path):
    account = _prepare_runner_script(
        monkeypatch,
        tmp_path,
        'import json\nprint(json.dumps({"message": "看起来像成功"}))\n',
    )

    result = _run_script(account, tmp_path)

    assert result["ok"] is False
    assert result["error"] == "脚本未明确报告签到成功"
    assert runner._summarize("jiaobenwang", result)[0] == "failed"


def test_large_stdout_and_stderr_are_drained_without_deadlock(monkeypatch, tmp_path):
    account = _prepare_runner_script(
        monkeypatch,
        tmp_path,
        "import json, sys\nsys.stdout.write('x' * 1000000)\n"
        "sys.stderr.write('y' * 1000000)\nsys.stdout.flush()\nsys.stderr.flush()\n"
        "print(json.dumps({'ok': True, 'message': '签到成功'}))\n",
    )

    result = _run_script(account, tmp_path, timeout=5)

    assert result["ok"] is True
    assert result["message"] == "签到成功"


def _process_state(pid):
    """Return the Linux process state, or None once the PID no longer exists."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    return stat.rsplit(")", 1)[1].strip().split()[0]


def test_timeout_kills_process_group_and_reaps_direct_child(monkeypatch, tmp_path):
    child_pid_file = tmp_path / "grandchild.pid"
    child_script = tmp_path / "grandchild.py"
    child_script.write_text(
        "import os, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(child_pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    script_content = (
        "import signal, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(child_script)!r}])\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(60)\n"
    )
    account = _prepare_runner_script(monkeypatch, tmp_path, script_content)
    spawned = []
    real_popen = subprocess.Popen

    def capture_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(runner.subprocess, "Popen", capture_popen)

    result = _run_script(account, tmp_path, timeout=0.25)

    assert "超时" in result["error"]
    assert spawned and spawned[0].poll() is not None
    assert runner._current_proc is None
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text())
    deadline = time.monotonic() + 2
    state = _process_state(child_pid)
    while state in {"R", "S", "D", "T"} and time.monotonic() < deadline:
        time.sleep(0.05)
        state = _process_state(child_pid)
    assert state not in {"R", "S", "D", "T"}


def test_cancel_signals_and_reaps_running_process(monkeypatch, tmp_path):
    started = tmp_path / "started"
    account = _prepare_runner_script(
        monkeypatch,
        tmp_path,
        f"import time\nopen({str(started)!r}, 'w').write('ready')\ntime.sleep(60)\n",
    )
    runner._active_run_id = "test-run"
    result_holder = {}

    def execute():
        result_holder["result"] = _run_script(account, tmp_path, timeout=10)

    thread = threading.Thread(target=execute)
    thread.start()
    deadline = time.monotonic() + 3
    while runner._current_proc is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner._current_proc is not None
    proc = runner._current_proc

    assert runner.cancel_run("test-run")["ok"] is True
    thread.join(timeout=4)

    assert not thread.is_alive()
    assert result_holder["result"]["cancelled"] is True
    assert proc.poll() is not None
    assert runner._current_proc is None


def test_deleting_account_during_run_removes_session_written_by_child(monkeypatch, tmp_path):
    started = tmp_path / "started"
    account = _prepare_runner_script(
        monkeypatch,
        tmp_path,
        "import json, sys, time\nfrom pathlib import Path\n"
        f"Path({str(started)!r}).write_text('started')\n"
        "session_path = Path(sys.argv[sys.argv.index('--session') + 1])\n"
        "time.sleep(0.25)\nsession_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "session_path.write_text('session created after delete')\n"
        "print(json.dumps({'ok': True, 'message': '签到成功'}))\n",
    )
    deleted = {"value": False}
    monkeypatch.setattr(
        runner.db,
        "get_account",
        lambda account_id: None if deleted["value"] else account,
    )
    result_holder = {}

    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", _run_script(account, tmp_path))
    )
    thread.start()
    deadline = time.monotonic() + 3
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started.exists()
    deleted["value"] = True
    thread.join(timeout=4)

    assert not thread.is_alive()
    assert result_holder["result"]["ok"] is True
    assert not (tmp_path / "sessions" / "test-account.session").exists()


def test_summarize_requires_success_flag():
    status, conclusion, *_ = runner._summarize("jiaobenwang", {"message": "not enough"})
    assert status == "failed"
    assert conclusion == "not enough"


def test_summarize():
    status, conclusion, mlabel, mvalue, streak = runner._summarize(
        "jiaobenwang", {"ok": True, "message": "成功", "points": "120"}
    )
    assert status == "success"
    assert mlabel == "贡献分"
    assert mvalue == "120"

    status_fail, conclusion_fail, _, _, _ = runner._summarize(
        "jiaobenwang", {"ok": False, "error": "网络异常"}
    )
    assert status_fail == "failed"
    assert "网络异常" in conclusion_fail
