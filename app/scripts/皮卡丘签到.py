#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
皮卡丘token商店（sub.pikaqiu.shop）每日签到脚本 —— 纯 HTTP 直连，不依赖浏览器。

站点是 Sub2API 架构（前端 Vue SPA + /api/v1 接口），本脚本直接调用其后端接口：
  1) POST /api/v1/auth/login          email + password 换取 access_token / refresh_token
  2) POST /api/v1/auth/refresh        用 refresh_token 续期（免重复登录）
  3) GET  /api/v1/auth/me             校验会话 + 读余额
  4) GET  /api/v1/benefits/checkins   读签到活动与"今日是否已签到"
  5) POST /api/v1/benefits/checkins/{campaign_id}/check-in   执行签到

会话（token）落盘复用；只有失效时才重新登录。脚本只做签到，不充值、不兑换、
不新建/修改 API Key、不改密码、不下载任何资源。

用法：
    python3 皮卡丘签到.py                # 一条命令跑完
    python3 皮卡丘签到.py --json         # 机器可读输出
    python3 皮卡丘签到.py --relogin      # 强制重新登录
    python3 皮卡丘签到.py --cred <文件>  # 指定凭据文件（默认自动查找）
    python3 皮卡丘签到.py --session <文件>  # 指定会话文件

退出码：0 成功（含"今日已签到"）／2 登录失败／3 网络或接口异常／4 凭据文件缺失。
"""

import argparse
import json
import os
import re
import stat
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    print("缺少依赖 requests，请先执行：pip install requests", file=sys.stderr)
    sys.exit(4)

BASE_URL = "https://sub.pikaqiu.shop"
API = BASE_URL + "/api/v1"
TIMEZONE = "Asia/Shanghai"          # 站点按上海日（UTC+8）判定"今天"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

CRED_FILENAME = "pikaqiu-shop-credentials.md"
SESSION_FILENAME = "pikaqiu-session.json"
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "today-checkin")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

CST = timezone(timedelta(hours=8))


# --------------------------------------------------------------------------
# 凭据读取（密码永不打印、永不写入日志）
# --------------------------------------------------------------------------
def _field(text, labels):
    """从 markdown 凭据文件里取一个字段，兼容 '- 密码：xxx' / '**密码**: xxx' 等写法。"""
    for raw in text.splitlines():
        line = raw.strip()
        line = re.sub(r"^[-*+\s]+", "", line)          # 去掉列表符号
        line = re.sub(r"^\*+|\*+$", "", line).strip()  # 去掉加粗星号
        for label in labels:
            if not line.startswith(label):
                continue
            rest = line[len(label):]
            # 标签后面必须紧跟分隔符/括号，避免「账号身份：…」被 label「账号」误命中
            if rest and rest[0] not in "：:（(【[] \t":
                continue
            # 兼容「登录邮箱：xxx」「登录名（同时是用户名与邮箱）：xxx」这类写法：
            # 取标签之后的第一个冒号，冒号后面就是取值。
            m = re.search(r"[:：]\s*(.+)$", rest)
            if m and m.group(1).strip():
                return m.group(1).strip()
    return None


def find_cred_file(explicit):
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    candidates = []
    env_dir = os.environ.get("CHECKIN_CRED_DIR")
    if env_dir:
        candidates.append(os.path.join(env_dir, CRED_FILENAME))
    candidates += [
        os.path.join(CONFIG_DIR, CRED_FILENAME),
        os.path.join(SCRIPT_DIR, CRED_FILENAME),
        os.path.join(SCRIPT_DIR, "accounts", CRED_FILENAME),
        os.path.join(os.getcwd(), CRED_FILENAME),
        os.path.join(os.getcwd(), "accounts", CRED_FILENAME),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def load_credentials(path):
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    email = os.environ.get("PIKAQIU_EMAIL") or _field(
        text, ["登录邮箱", "邮箱", "Email", "email", "账号"])
    password = os.environ.get("PIKAQIU_PASSWORD") or _field(
        text, ["密码", "Password", "password"])
    if not email or not password:
        return None, None
    return email, password


# --------------------------------------------------------------------------
# 会话落盘
# --------------------------------------------------------------------------
def default_session_path():
    return os.path.join(CONFIG_DIR, SESSION_FILENAME)


def ensure_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def write_private(path, data):
    ensure_dir(os.path.dirname(path))
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_session(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("access_token"):
            return data
    except (OSError, ValueError):
        pass
    return None


def save_session(path, payload):
    payload = dict(payload)
    payload["saved_at"] = datetime.now(CST).isoformat(timespec="seconds")
    write_private(path, json.dumps(payload, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------
# 接口调用
# --------------------------------------------------------------------------
def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": BASE_URL + "/login",
    })
    return s


def unwrap(resp):
    """Sub2API 统一返回 {code, message, data}，code==0 才算成功。"""
    try:
        body = resp.json()
    except ValueError:
        raise RuntimeError("接口返回的不是 JSON（HTTP %s）：%s"
                           % (resp.status_code, resp.text[:200]))
    if isinstance(body, dict) and "code" in body:
        if body.get("code") != 0:
            raise RuntimeError("接口报错 code=%s message=%s"
                               % (body.get("code"), body.get("message")))
        return body.get("data")
    return body


def api_login(http, email, password):
    resp = http.post(API + "/auth/login",
                     json={"email": email, "password": password}, timeout=30)
    data = unwrap(resp)
    if not isinstance(data, dict) or not data.get("access_token"):
        raise RuntimeError("登录接口未返回 access_token")
    return data


def api_refresh(http, refresh_token):
    resp = http.post(API + "/auth/refresh",
                     json={"refresh_token": refresh_token}, timeout=30)
    data = unwrap(resp)
    if not isinstance(data, dict) or not data.get("access_token"):
        raise RuntimeError("刷新接口未返回 access_token")
    return data


def api_me(http, token):
    resp = http.get(API + "/auth/me", headers={"Authorization": "Bearer " + token},
                    params={"timezone": TIMEZONE}, timeout=30)
    if resp.status_code == 401:
        return None
    return unwrap(resp)


def api_campaigns(http, token):
    resp = http.get(API + "/benefits/checkins",
                    headers={"Authorization": "Bearer " + token},
                    params={"timezone": TIMEZONE}, timeout=30)
    if resp.status_code == 401:
        return None
    data = unwrap(resp)
    return data if isinstance(data, list) else []


def api_checkin(http, token, campaign_id):
    resp = http.post(API + "/benefits/checkins/%s/check-in" % campaign_id,
                     headers={"Authorization": "Bearer " + token},
                     json={}, timeout=30)
    if resp.status_code == 401:
        return None
    return unwrap(resp)


# --------------------------------------------------------------------------
# 进度事件（--progress-file）：面板"签到过程"时间线的数据源。
# 每行一个 JSON：{"ts": ..., "site": ..., "message": ...}，实时 flush。
# 不传 --progress-file 时完全无影响。
# --------------------------------------------------------------------------
def _open_progress_file(path):
    if not path:
        return None
    try:
        return open(path, "a", encoding="utf-8")
    except OSError:
        return None


def _progress_emit(fh, site, message):
    if not fh:
        return
    try:
        fh.write(json.dumps({
            "ts": datetime.now(CST).isoformat(timespec="seconds"),
            "site": site,
            "message": message,
        }, ensure_ascii=False) + "\n")
        fh.flush()
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def resolve_token(http, session_path, email, password, force_login, log, step=None):
    """返回 (token, 是否重新登录)。优先复用本地 token，失效再登录。"""
    step = step or (lambda m: None)
    saved = None if force_login else load_session(session_path)
    if saved:
        token = saved.get("access_token")
        if api_me(http, token) is not None:
            log("复用本地会话（未重新登录）")
            step("登录状态有效，复用本地会话")
            return token, False
        step("登录状态失效")
        refresh_token = saved.get("refresh_token")
        if refresh_token:
            try:
                data = api_refresh(http, refresh_token)
                save_session(session_path, data)
                log("本地 token 过期，已用 refresh_token 续期")
                step("已用 refresh_token 续期成功")
                return data["access_token"], False
            except Exception:
                log("refresh_token 已失效，重新登录")
        else:
            log("本地会话已失效，重新登录")
        step("正在重新登录")
    else:
        step("无本地会话，正在登录")
    data = api_login(http, email, password)
    save_session(session_path, data)
    log("登录成功（%s）" % ("强制重新登录" if force_login else "首次/重新登录"))
    step("重新登录成功" if (saved or force_login) else "登录成功")
    return data["access_token"], True


def main():
    parser = argparse.ArgumentParser(
        description="皮卡丘token商店每日签到（HTTP 直连，不用浏览器）")
    parser.add_argument("--cred", help="凭据文件路径（默认自动查找）")
    parser.add_argument("--session", help="会话文件路径（默认 ~/.config/today-checkin/%s）"
                                         % SESSION_FILENAME)
    parser.add_argument("--relogin", action="store_true", help="忽略本地会话，强制重新登录")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--quiet", action="store_true", help="只输出一行结论")
    parser.add_argument("--progress-file", help="进度事件输出文件（JSONL，供面板展示签到过程时间线）")
    args = parser.parse_args()

    logs = []

    def log(msg):
        logs.append(msg)

    progress_fh = _open_progress_file(args.progress_file)

    def step(message):
        _progress_emit(progress_fh, "皮卡丘token商店", message)

    result = {"site": "皮卡丘token商店", "host": BASE_URL, "ok": False}

    cred_path = find_cred_file(args.cred)
    if not cred_path:
        print("找不到凭据文件 %s。请用 --cred 指定，或放到 %s/"
              % (CRED_FILENAME, CONFIG_DIR), file=sys.stderr)
        return 4
    email, password = load_credentials(cred_path)
    if not email or not password:
        print("凭据文件 %s 里没解析到邮箱/密码字段。" % cred_path, file=sys.stderr)
        return 4
    result["account"] = email

    session_path = args.session or default_session_path()
    http = make_session()
    started = time.time()
    step("正在执行皮卡丘token商店签到")

    try:
        token, relogin = resolve_token(http, session_path, email, password,
                                       args.relogin, log, step)
        result["relogin"] = relogin

        me = api_me(http, token)
        if me is None:
            log("会话在取用户信息时失效，重新登录后重试")
            step("登录状态失效，正在重新登录")
            data = api_login(http, email, password)
            step("重新登录成功")
            save_session(session_path, data)
            token, relogin = data["access_token"], True
            result["relogin"] = True
            me = api_me(http, token)
        if isinstance(me, dict):
            result["balance"] = me.get("balance")
            result["username"] = me.get("username") or me.get("email")

        step("正在查询签到活动")
        campaigns = api_campaigns(http, token)
        if campaigns is None:
            raise RuntimeError("读取签到活动时被拒绝（HTTP 401）")

        target = None
        for item in campaigns:
            camp = item.get("campaign") or {}
            if item.get("eligible") and not item.get("checked_today"):
                target = item
                break
        if target is None and campaigns:
            target = campaigns[0]

        if target is None:
            result["message"] = "站点没有可用的签到活动"
            result["ok"] = True
        elif target.get("checked_today"):
            stats = target.get("stats") or {}
            step("今日已签到，无需重复签到")
            result.update({
                "ok": True,
                "checked_today": True,
                "already": True,
                "campaign": (target.get("campaign") or {}).get("name"),
                "current_streak": stats.get("current_streak"),
                "longest_streak": stats.get("longest_streak"),
                "total_days": stats.get("total_days"),
                "total_amount": stats.get("total_amount"),
                "message": "今日已签到（无需重复签到）",
            })
        elif not target.get("eligible"):
            result.update({
                "ok": True, "checked_today": False, "already": False,
                "campaign": (target.get("campaign") or {}).get("name"),
                "message": "当前账号不满足该签到活动条件（eligible=false），未提交签到请求",
            })
        else:
            camp_id = (target.get("campaign") or {}).get("id")
            step("正在提交签到请求")
            data = api_checkin(http, token, camp_id)
            if data is None:
                raise RuntimeError("提交签到时被拒绝（HTTP 401）")
            record = data.get("record") or data
            stats = data.get("stats") or {}
            result.update({
                "ok": True,
                "checked_today": True,
                "already": False,
                "campaign": (target.get("campaign") or {}).get("name"),
                "reward": record.get("total_amount"),
                "balance_after": record.get("balance_after"),
                "current_streak": stats.get("current_streak",
                                            record.get("streak_after")),
                "total_days": stats.get("total_days",
                                        record.get("total_days_after")),
                "checked_in_at": record.get("checked_in_at"),
                "message": "签到成功，奖励 %s" % record.get("total_amount"),
            })
            step("签到成功")
    except requests.RequestException as exc:
        result["error"] = "网络异常：%s" % exc
        _emit(args, result, logs, started, code=3)
        return 3
    except RuntimeError as exc:
        result["error"] = str(exc)
        _emit(args, result, logs, started, code=2 if "登录" in str(exc) else 3)
        return 2 if "登录" in str(exc) else 3

    result["session_file"] = session_path
    _emit(args, result, logs, started, code=0)
    return 0


def _emit(args, result, logs, started, code):
    result["elapsed"] = round(time.time() - started, 2)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.quiet:
        print(result.get("message") or result.get("error"))
        return
    tag = "OK " if result.get("ok") else "ERR"
    print("[%s] 皮卡丘token商店 每日签到" % tag)
    for line in logs:
        print("  · " + line)
    if result.get("message"):
        print("  结论：" + result["message"])
    if result.get("error"):
        print("  错误：" + result["error"])
    if result.get("balance") is not None:
        print("  账户余额：%s" % result["balance"])
    if result.get("checked_today"):
        print("  连签：%s 天 | 累计签到：%s 天 | 累计奖励：%s"
              % (result.get("current_streak"), result.get("total_days"),
                 result.get("total_amount", result.get("reward", "-"))))
    if result.get("session_file"):
        print("  会话文件：%s" % result["session_file"])
    print("  耗时：%ss" % result["elapsed"])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
