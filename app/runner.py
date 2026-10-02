"""签到执行引擎。

- 每个账号起一个子进程跑 app/scripts 下的签到脚本
  （--json + --progress-file + --session，凭据走环境变量 + 临时凭据文件）
- 实时 tail 进度文件写入 steps 表（面板时间线的数据源）
- 支持任务中止（Cancel）、失败重试、异步通知推送与 Google Sheets 归档
- 同一时间只允许一个 run 在跑
"""
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from . import config, crypto, db, notifier
from .timeutil import business_date, now_bj

_run_lock = threading.Lock()
_active_run_id: Optional[str] = None
_current_proc: Optional[subprocess.Popen] = None
_cancel_requested: bool = False

SITE_NAME_TO_KEY = {v["name"]: k for k, v in config.SITES.items()}
SITE_NAME_TO_KEY.update({"ebondai.com": "ebondai"})


def is_running() -> bool:
    with _run_lock:
        return _active_run_id is not None


def get_active_run_id() -> Optional[str]:
    with _run_lock:
        return _active_run_id


def cancel_run(run_id: str) -> Dict[str, Any]:
    """终止当前正在运行的签到任务。"""
    global _cancel_requested, _current_proc
    with _run_lock:
        if _active_run_id != run_id or not _active_run_id:
            return {"ok": False, "error": "任务不在运行状态或 ID 不匹配"}
        _cancel_requested = True
        proc = _current_proc

    if proc:
        # The runner thread owns waiting/reaping the process. Signal its full
        # process group here so cancellation also reaches script descendants.
        _signal_process_tree(proc, signal.SIGTERM)

    return {"ok": True, "message": "已发送终止指令"}


def start_run(mode: str, account_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """启动一次签到（后台线程）。mode: manual / scheduled。"""
    global _active_run_id, _cancel_requested
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
        _cancel_requested = False

    t = threading.Thread(target=_execute_run, args=(run_id, mode, accounts, biz), daemon=True)
    t.start()
    return {"ok": True, "runId": run_id}


def start_failed_retry() -> Dict[str, Any]:
    """对今日尚未成功的所有已启用账号发起重试。"""
    biz = business_date()
    accounts = [a for a in db.list_accounts() if a["enabled"]]
    today_map = db.latest_results_by_account(biz)
    
    target_ids = []
    for a in accounts:
        res = today_map.get(a["id"])
        if not res or res["status"] != "success":
            target_ids.append(a["id"])
            
    if not target_ids:
        return {"ok": False, "error": "今日所有启用账号均已签到成功，无需重试"}
    return start_run("manual", account_ids=target_ids)


def _drain_progress(run_id: str, progress_path: Path, state: Dict[str, Any]) -> None:
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


def _write_cred_files(site_key: str, login: Optional[str], password: Optional[str],
                      api_key: Optional[str], tmpdir: Path) -> Tuple[str, Optional[str]]:
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
    try:
        os.chmod(cred_path, 0o600)
    except OSError:
        pass

    key_path = None
    if site_key == "ebondai":
        key_path = tmpdir / "key.md"
        key_lines = []
        if api_key:
            key_lines.append(f"{labels['api_key']}：{api_key}")
        key_path.write_text("\n".join(key_lines) + "\n", encoding="utf-8")
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
    return str(cred_path), (str(key_path) if key_path else None)


def _build_env(site_key: str, login: Optional[str], password: Optional[str],
               api_key: Optional[str]) -> Dict[str, str]:
    # Keep only variables needed by Python, TLS/proxy configuration and the
    # selected site's legacy environment-credential fallback. In particular,
    # do not pass credentials for unrelated sites or application secrets.
    runtime_names = {
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
        "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        "PYTHONUTF8", "PYTHONIOENCODING", "PYTHONUNBUFFERED",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    }
    env = {name: value for name, value in os.environ.items() if name in runtime_names}
    mapping = config.SITES[site_key]["cred_env"]
    for env_name in mapping.values():
        if env_name in os.environ:
            env[env_name] = os.environ[env_name]
    if login and "login" in mapping:
        env[mapping["login"]] = login
    if password and "password" in mapping:
        env[mapping["password"]] = password
    if api_key and "api_key" in mapping:
        env[mapping["api_key"]] = api_key
    return env


def _signal_process_tree(proc: subprocess.Popen, sig: Optional[int]) -> None:
    """Send a signal to the script process group (or the direct child on Windows)."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, sig)
        elif sig is None:
            proc.kill()
        elif getattr(signal, "SIGKILL", None) is not None and sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
    except ProcessLookupError:
        pass
    except OSError:
        # Preserve a best-effort direct-child fallback if group signaling fails.
        try:
            if proc.poll() is None:
                force_kill = sig is None or (
                    getattr(signal, "SIGKILL", None) is not None and sig == signal.SIGKILL
                )
                proc.kill() if force_kill else proc.terminate()
        except OSError:
            pass


def _process_tree_exists(proc: subprocess.Popen) -> bool:
    if os.name != "posix":
        return proc.poll() is None
    try:
        os.killpg(proc.pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate_process_tree(proc: subprocess.Popen, grace: float = 1.0) -> None:
    """Terminate descendants, escalate to SIGKILL, and always reap the direct child."""
    _signal_process_tree(proc, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace)
    while _process_tree_exists(proc) and time.monotonic() < deadline:
        proc.poll()  # Reap the direct child promptly if it has already exited.
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    if _process_tree_exists(proc):
        _signal_process_tree(proc, getattr(signal, "SIGKILL", None))

    # communicate() drains both output streams and reaps the direct child. A
    # descendant that escaped the process group must not be allowed to hold the
    # pipes open indefinitely, so bound this final drain and close our readers.
    try:
        proc.communicate(timeout=1.0)
    except subprocess.TimeoutExpired:
        _signal_process_tree(proc, getattr(signal, "SIGKILL", None))
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def _validate_script_result(result: Dict[str, Any], returncode: int) -> Dict[str, Any]:
    """Require both a successful process exit and an explicit JSON success flag."""
    has_success_field = "ok" in result
    if returncode != 0:
        result["ok"] = False
        detail = result.get("error") or result.get("message")
        if detail:
            result["error"] = f"{detail}（脚本退出码 {returncode}）"
        else:
            result["error"] = f"签到脚本异常退出（退出码 {returncode}）"
    elif result.get("ok") is not True:
        result["ok"] = False
        if not result.get("error"):
            if has_success_field:
                result["error"] = result.get("message") or "脚本未明确报告签到成功"
            else:
                result["error"] = "脚本未明确报告签到成功"
    return result


def _parse_script_json(stdout: str) -> Dict[str, Any]:
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


def _is_network_error(res: Dict[str, Any]) -> bool:
    if res.get("error_type") == "network":
        return True
    err = str(res.get("error") or "")
    return "网络异常" in err or "Network" in err or "timeout" in err.lower() or "ConnectionError" in err


def _sleep_with_cancel_check(seconds: float) -> bool:
    """分片 sleep 并检测是否收到取消指令。返回 True 表示被取消。"""
    end_time = time.time() + seconds
    while time.time() < end_time:
        if _cancel_requested:
            return True
        time.sleep(0.2)
    return False


def _run_one_script(run_id: str, acct: Dict[str, Any], progress_path: Path,
                   state: Dict[str, Any], timeout: int = config.RUNNER_TIMEOUT) -> Dict[str, Any]:
    global _current_proc
    if _cancel_requested:
        return {"ok": False, "error": "任务已取消", "cancelled": True}

    site_key = acct["site_key"]
    site = config.SITES.get(site_key)
    if not site:
        return {"ok": False, "error": f"未知站点配置: {site_key}"}

    login, login_ok = crypto.safe_decrypt(acct.get("login_enc"))
    password, pwd_ok = crypto.safe_decrypt(acct.get("password_enc"))
    api_key, key_ok = crypto.safe_decrypt(acct.get("api_key_enc"))

    if not (login_ok and pwd_ok and key_ok):
        return {"ok": False, "error": "凭据解密失败（密钥可能已变更），请在账号管理中重新保存凭据"}

    session_path = config.SESSIONS_DIR / f"{acct['id']}.session"
    res: Optional[Dict[str, Any]] = None
    tmpdir = Path(tempfile.mkdtemp(prefix="checkin-cred-"))
    try:
        cred_path, key_path = _write_cred_files(site_key, login, password, api_key, tmpdir)
        env = _build_env(site_key, login, password, api_key)
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

        def _once() -> Dict[str, Any]:
            global _current_proc
            if _cancel_requested:
                return {"ok": False, "error": "任务已取消", "cancelled": True}
            
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=(os.name == "posix"),
            )
            with _run_lock:
                _current_proc = proc

            deadline = time.monotonic() + max(0.0, float(timeout))
            try:
                while True:
                    if _cancel_requested:
                        _terminate_process_tree(proc)
                        return {"ok": False, "error": "任务已手动终止", "cancelled": True}

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        _terminate_process_tree(proc)
                        return {
                            "ok": False,
                            "error": f"签到脚本执行超时（{timeout} 秒）",
                        }

                    _drain_progress(run_id, progress_path, state)
                    try:
                        out, err = proc.communicate(timeout=min(0.2, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue

                if _cancel_requested:
                    _terminate_process_tree(proc)
                    return {"ok": False, "error": "任务已手动终止", "cancelled": True}

                result = _parse_script_json(out)
                if not result.get("ok") and not result.get("error") and err.strip():
                    result["error"] = err.strip().splitlines()[-1][:300]
                return _validate_script_result(result, proc.returncode)
            finally:
                # Even a normally exiting script must not leave descendants
                # behind; communicate above also detects descendants retaining
                # inherited output pipes and routes them through timeout cleanup.
                if proc.poll() is None or _process_tree_exists(proc):
                    _terminate_process_tree(proc)
                with _run_lock:
                    if _current_proc == proc:
                        _current_proc = None
                _drain_progress(run_id, progress_path, state)

        res = _once()
        # 网络异常且未取消：退避重试一次
        if _is_network_error(res) and not _cancel_requested:
            db.add_steps(run_id, [{
                "ts": now_bj().isoformat(timespec="seconds"),
                "site_key": site_key,
                "message": f"网络异常，将在 {config.RUNNER_RETRY_DELAY} 秒后重试一次",
            }])
            was_cancelled = _sleep_with_cancel_check(config.RUNNER_RETRY_DELAY)
            if not was_cancelled:
                _drain_progress(run_id, progress_path, state)
                res = _once()
                if _is_network_error(res):
                    res["error"] = (res.get("error") or "网络异常") + "（已重试一次）"
        return res
    finally:
        # A concurrent account deletion can unlink the session while this
        # script is still running. Remove any session the child writes afterward.
        try:
            account = db.get_account(acct["id"])
        except Exception:  # noqa: BLE001 - do not mask the script's own result
            account = True
        if account is None:
            try:
                session_path.unlink(missing_ok=True)
            except OSError:
                if res is not None:
                    res["ok"] = False
                    res["error"] = "账号已删除，但本地会话清理失败"
        shutil.rmtree(tmpdir, ignore_errors=True)


def _summarize(site_key: str, res: Dict[str, Any]) -> Tuple[str, str, Optional[str], Any, Optional[int]]:
    """把脚本的 --json 结果映射为面板展示字段。返回 (status, conclusion, metric_label, metric_value, streak_days)"""
    if res.get("cancelled"):
        return ("cancelled", "任务已手动终止", None, None, None)
    err = res.get("error")
    if err:
        return ("failed", str(err)[:300], None, None, None)
    if res.get("ok") is not True:
        return ("failed", str(res.get("message") or "脚本未明确报告签到成功")[:300],
                None, None, None)
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
    if site_key == "jingdong":
        conclusion = msg or "签到成功"
        return ("success", conclusion, "京豆", res.get("beans"),
                res.get("current_streak"))
    # ebondai
    conclusion = msg or "签到成功"
    return ("success", conclusion, "账户余额",
            res.get("account_balance"), res.get("current_streak"))


def _execute_run(run_id: str, mode: str, accounts: List[Dict[str, Any]], biz: str) -> None:
    global _active_run_id, _cancel_requested, _current_proc
    progress_path = config.PROGRESS_DIR / f"{run_id}.jsonl"
    progress_path.touch(exist_ok=True)
    state = {"offset": 0}
    succeeded = failed = 0
    results_for_report = []
    
    try:
        for acct in accounts:
            if _cancel_requested:
                db.add_steps(run_id, [{
                    "ts": now_bj().isoformat(timespec="seconds"),
                    "site_key": None,
                    "message": "用户手动终止了签到任务",
                }])
                break

            site_key = acct["site_key"]
            site_info = config.SITES.get(site_key, {"name": site_key})
            site_name = site_info["name"]
            
            db.add_steps(run_id, [{
                "ts": now_bj().isoformat(timespec="seconds"),
                "site_key": site_key,
                "message": f"正在执行 {site_name} 签到",
            }])
            
            try:
                res = _run_one_script(run_id, acct, progress_path, state)
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "error": f"执行异常：{exc}"}
                
            status, conclusion, mlabel, mvalue, streak = _summarize(site_key, res)
            if status == "success":
                succeeded += 1
            else:
                failed += 1

            login_plain, _ = crypto.safe_decrypt(acct.get("login_enc"))
            label = crypto.mask_login(login_plain)
            
            db.add_result(
                run_id, acct["id"], site_key, label, biz, status,
                conclusion=conclusion, metric_label=mlabel, metric_value=mvalue,
                streak_days=streak,
                note=None if status == "success" else conclusion,
            )
            
            results_for_report.append({
                "date": biz,
                "site": site_name,
                "site_key": site_key,
                "status": "成功" if status == "success" else ("已终止" if status == "cancelled" else "失败"),
                "conclusion": conclusion,
                "metric_value": mvalue if mvalue is not None else "—",
                "streak": streak if streak is not None else "—",
                "note": f"账号 {label}",
            })

        final_status = "cancelled" if _cancel_requested else ("completed" if failed == 0 else "completed")
        if not accounts:
            final_status = "completed"
        db.finish_run(run_id, final_status, total=len(accounts),
                      succeeded=succeeded, failed=failed)
    except Exception as exc:  # noqa: BLE001
        db.finish_run(run_id, "failed", error=str(exc)[:300])
    finally:
        # 归档与通知推送
        try:
            from . import sheets
            sheets.record(results_for_report)
        except Exception:
            pass

        try:
            notifier.send_run_report(mode, biz, len(accounts), succeeded, failed, results_for_report)
        except Exception:
            pass

        # 定期清理历史过期数据
        try:
            db.clean_old_records()
        except Exception:
            pass

        with _run_lock:
            _active_run_id = None
            _current_proc = None
            _cancel_requested = False

        try:
            progress_path.unlink(missing_ok=True)
        except OSError:
            pass
