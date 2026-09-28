"""签到执行引擎。

- 每个账号起一个子进程跑 app/scripts 下的签到脚本
  （--json + --progress-file + --session，凭据走环境变量 + 临时凭据文件）
- 实时 tail 进度文件写入 steps 表（面板时间线的数据源）
- 网络异常约 60 秒后重试一次
- 同一时间只允许一个 run 在跑
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import config, crypto, db
from .timeutil import business_date, now_bj

_run_lock = threading.Lock()
_active_run_id = None

SITE_NAME_TO_KEY = {v["name"]: k for k, v in config.SITES.items()}
# 脚本里 site 字段的写法与 SITES name 不完全一致，做兼容
SITE_NAME_TO_KEY.update({"ebondai.com": "ebondai"})


def is_running() -> bool:
    with _run_lock:
        return _active_run_id is not None


def start_run(mode: str, account_ids=None) -> dict:
    """启动一次签到（后台线程）。mode: manual / scheduled。"""
    global _active_run_id
    with _run_lock:
        if _active_run_id is not None:
            return {"ok": False, "error": "已有签到任务在运行，请稍候"}
        accounts = [a for a in db.list_accounts() if a["enabled"]]
        if account_ids is not None:
            wanted = set(account_ids)
            accounts = [a for a in accounts if a["id"] in wanted]
        if not accounts:
            return {"ok": False, "error": "没有可执行的账号（账号不存在或已停用）"}
        biz = business_date()
        run_id = db.create_run(mode, biz)
        _active_run_id = run_id
    t = threading.Thread(target=_execute_run, args=(run_id, mode, accounts, biz), daemon=True)
    t.start()
    return {"ok": True, "runId": run_id}


def _drain_progress(run_id: str, progress_path: Path, state: dict) -> None:
    try:
        with open(progress_path, "r", encoding="utf-8") as fh:
            fh.seek(state["offset"])
            events = []
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except (ValueError, TypeError):
                    continue
                site_name = e.get("site") or ""
                events.append({
                    "ts": e.get("ts"),
                    "site_key": SITE_NAME_TO_KEY.get(site_name),
                    "message": e.get("message") or "",
                })
            state["offset"] = fh.tell()
    except OSError:
        return
    if events:
        db.add_steps(run_id, events)


def _write_cred_files(site_key: str, login, password, api_key, tmpdir: Path):
    """生成脚本能解析的临时凭据文件（600 权限），返回 (cred_path, key_path)。"""
    site = config.SITES[site_key]
    labels = site["cred_labels"]
    lines = []
    if login:
        lines.append(f"{labels['login']}：{login}")
    if password:
        lines.append(f"{labels['password']}：{password}")
    cred_path = tmpdir / "cred.md"
    cred_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(cred_path, 0o600)
    key_path = None
    if site_key == "ebondai":
        key_path = tmpdir / "key.md"
        key_lines = []
        if api_key:
            key_lines.append(f"{labels['api_key']}：{api_key}")
        key_path.write_text("\n".join(key_lines) + "\n", encoding="utf-8")
        os.chmod(key_path, 0o600)
    return str(cred_path), (str(key_path) if key_path else None)


def _build_env(site_key: str, login, password, api_key) -> dict:
    env = os.environ.copy()
    mapping = config.SITES[site_key]["cred_env"]
    if login and "login" in mapping:
        env[mapping["login"]] = login
    if password and "password" in mapping:
        env[mapping["password"]] = password
    if api_key and "api_key" in mapping:
        env[mapping["api_key"]] = api_key
    # 防止误读线上凭据目录
    env.pop("CHECKIN_CRED_DIR", None)
    return env


def _parse_script_json(stdout: str) -> dict:
    text = stdout.strip()
    if not text:
        return {"ok": False, "error": "脚本无输出"}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    # 兜底：从第一个 { 开始解析
    start = text.find("{")
    if start >= 0:
        try:
            return json.loads(text[start:])
        except ValueError:
            pass
    return {"ok": False, "error": "脚本输出无法解析"}


def _is_network_error(res: dict) -> bool:
    if res.get("error_type") == "network":
        return True
    err = str(res.get("error") or "")
    return "网络异常" in err or "Network" in err or "timeout" in err.lower()


def _run_one_script(run_id: str, acct: dict, progress_path: Path, state: dict,
                   timeout=300) -> dict:
    site_key = acct["site_key"]
    site = config.SITES[site_key]
    login = crypto.decrypt(acct.get("login_enc"))
    password = crypto.decrypt(acct.get("password_enc"))
    api_key = crypto.decrypt(acct.get("api_key_enc"))

    tmpdir = Path(tempfile.mkdtemp(prefix="checkin-cred-"))
    try:
        cred_path, key_path = _write_cred_files(site_key, login, password, api_key, tmpdir)
        env = _build_env(site_key, login, password, api_key)
        session_path = config.SESSIONS_DIR / f"{acct['id']}.session"
        cmd = [
            sys.executable,
            str(config.SCRIPTS_DIR / site["script"]),
            "--json",
            "--progress-file", str(progress_path),
            "--session", str(session_path),
            "--cred", cred_path,
        ]
        if key_path:
            cmd += ["--keyfile", key_path]

        def _once():
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, text=True)
            try:
                while proc.poll() is None:
                    _drain_progress(run_id, progress_path, state)
                    time.sleep(0.3)
                try:
                    out, err = proc.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, err = proc.communicate()
                    return {"ok": False, "error": "脚本执行超时"}
            finally:
                _drain_progress(run_id, progress_path, state)
            res = _parse_script_json(out)
            if not res.get("ok") and not res.get("error") and err.strip():
                res["error"] = err.strip().splitlines()[-1][:300]
            return res

        res = _once()
        # 网络异常：约 60 秒后重试一次
        if _is_network_error(res):
            time.sleep(60)
            _drain_progress(run_id, progress_path, state)
            res = _once()
            if _is_network_error(res):
                res["error"] = (res.get("error") or "网络异常") + "（已重试一次）"
        return res
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _summarize(site_key: str, res: dict):
    """把脚本的 --json 结果映射为面板展示字段。"""
    err = res.get("error")
    if err:
        return ("failed", str(err)[:300], None, None, None)
    msg = res.get("message")
    if site_key == "jiaobenwang":
        conclusion = msg or "签到成功"
        return ("success", conclusion, "贡献分", res.get("points"), None)
    if site_key == "pikaqiu":
        conclusion = msg or "签到成功"
        balance = res.get("balance")
        if balance is None:
            balance = res.get("balance_after")
        return ("success", conclusion, "账户余额", balance, res.get("current_streak"))
    # ebondai
    conclusion = msg or "签到成功"
    return ("success", conclusion, "账户余额",
            res.get("account_balance"), res.get("current_streak"))


def _execute_run(run_id: str, mode: str, accounts: list, biz: str):
    global _active_run_id
    progress_path = config.PROGRESS_DIR / f"{run_id}.jsonl"
    progress_path.touch(exist_ok=True)
    state = {"offset": 0}
    succeeded = failed = 0
    results_for_sheet = []
    try:
        for acct in accounts:
            site_key = acct["site_key"]
            site_name = config.SITES[site_key]["name"]
            db.add_steps(run_id, [{
                "ts": now_bj().isoformat(timespec="seconds"),
                "site_key": site_key,
                "message": f"正在执行{site_name}签到",
            }])
            try:
                res = _run_one_script(run_id, acct, progress_path, state)
            except Exception as exc:  # noqa: BLE001 - 单个账号异常不影响其他账号
                res = {"ok": False, "error": f"执行异常：{exc}"}
            status, conclusion, mlabel, mvalue, streak = _summarize(site_key, res)
            if status == "success":
                succeeded += 1
            else:
                failed += 1
            label = crypto.mask_login(crypto.decrypt(acct.get("login_enc")))
            db.add_result(
                run_id, acct["id"], site_key, label, biz, status,
                conclusion=conclusion, metric_label=mlabel, metric_value=mvalue,
                streak_days=streak,
                note=None if status == "success" else conclusion,
            )
            results_for_sheet.append({
                "date": biz, "site": site_name,
                "status": "成功" if status == "success" else "失败",
                "conclusion": conclusion,
                "metric_value": mvalue if mvalue is not None else "—",
                "streak": streak if streak is not None else "—",
                "note": f"账号 {label}",
            })
        db.finish_run(run_id, "completed", total=len(accounts),
                      succeeded=succeeded, failed=failed)
    except Exception as exc:  # noqa: BLE001
        db.finish_run(run_id, "failed", error=str(exc)[:300])
    finally:
        try:
            from . import sheets
            sheets.record(results_for_sheet)
        except Exception:  # noqa: BLE001 - 表格记录失败不影响主流程
            pass
        with _run_lock:
            _active_run_id = None
        try:
            progress_path.unlink(missing_ok=True)
        except OSError:
            pass
