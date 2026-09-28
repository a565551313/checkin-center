#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
脚本王（www.jiaobenwang.com）每日签到脚本 —— 纯 HTTP 直连，不依赖浏览器。

站点是 WordPress + RiPro 主题，本脚本直接走站点原生接口：
  1) GET  /wp-login.php                     先取 wordpress_test_cookie
  2) POST /wp-login.php                     用「邮箱 + 密码」表单登录，拿 wordpress_logged_in cookie
  3) GET  /user                             校验 cookie 是否仍有效（未登录会跳回首页）
  4) POST /wp-admin/admin-ajax.php          签到：action=user_qiandao（RiPro 主题的签到接口）
  5) GET  /user                             读「现有余额」= 贡献分

cookie 落盘复用（Netscape/Mozilla cookie 格式）；失效了再重新登录。
脚本只做签到，不充值、不兑换、不改密码、不下载任何资源。

用法：
    python3 脚本王签到.py                 # 一条命令跑完
    python3 脚本王签到.py --json          # 机器可读输出
    python3 脚本王签到.py --relogin       # 强制重新登录
    python3 脚本王签到.py --dry-run       # 只校验登录态，不提交签到请求
    python3 脚本王签到.py --cred <文件>   # 指定凭据文件（默认自动查找）
    python3 脚本王签到.py --session <文件>  # 指定 cookie 文件

退出码：0 成功（含"今日已签到"）／2 登录失败／3 网络或接口异常／4 凭据文件缺失。
"""

import argparse
import http.cookiejar
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    print("缺少依赖 requests，请先执行：pip install requests", file=sys.stderr)
    sys.exit(4)

BASE = "https://www.jiaobenwang.com"
LOGIN_URL = BASE + "/wp-login.php"
AJAX_URL = BASE + "/wp-admin/admin-ajax.php"
USER_URL = BASE + "/user"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

CRED_FILENAME = "jiaobenwang-credentials.md"
SESSION_FILENAME = "jiaobenwang-cookies.txt"
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "today-checkin")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

CST = timezone(timedelta(hours=8))


# --------------------------------------------------------------------------
# 凭据读取（密码永不打印、永不写入日志）
# --------------------------------------------------------------------------
def _field(text, labels):
    for raw in text.splitlines():
        line = raw.strip()
        line = re.sub(r"^[-*+\s]+", "", line)
        line = re.sub(r"^\*+|\*+$", "", line).strip()
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
    # 「登录名（同时是用户名与邮箱）」这一行就是登录时填的邮箱/账号
    user = (os.environ.get("JIAOBENWANG_USER")
            or _field(text, ["登录名", "登录邮箱", "邮箱", "Email", "email", "账号"]))
    password = os.environ.get("JIAOBENWANG_PASSWORD") or _field(
        text, ["密码", "Password", "password"])
    if not user or not password:
        return None, None
    return user, password


# --------------------------------------------------------------------------
# cookie 落盘
# --------------------------------------------------------------------------
def default_session_path():
    return os.path.join(CONFIG_DIR, SESSION_FILENAME)


def save_cookies(session, path):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    try:
        os.chmod(os.path.dirname(path), 0o700)
    except OSError:
        pass
    jar = http.cookiejar.MozillaCookieJar(path)
    for cookie in session.cookies:
        jar.set_cookie(cookie)
    jar.save(ignore_discard=True, ignore_expires=True)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_cookies(session, path):
    if not os.path.isfile(path):
        return False
    try:
        jar = http.cookiejar.MozillaCookieJar(path)
        jar.load(ignore_discard=True, ignore_expires=True)
        session.cookies.update(jar)
        return True
    except (OSError, http.cookiejar.LoadError, ValueError):
        return False


# --------------------------------------------------------------------------
# 站点操作
# --------------------------------------------------------------------------
def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    return s


def is_logged_in(http, session_path=None, persist=True):
    """访问 /user，未登录会跳回首页；已登录页面里有退出链接。"""
    try:
        resp = http.get(USER_URL, timeout=30, allow_redirects=True)
    except requests.RequestException:
        raise
    ok = ('class="logout"' in resp.text) or ("wp-login.php?action=logout" in resp.text)
    if ok and persist and session_path:
        save_cookies(http, session_path)
    return ok, resp


def wp_login(http, user, password, session_path):
    http.get(LOGIN_URL, timeout=30)          # 拿 wordpress_test_cookie
    resp = http.post(
        LOGIN_URL,
        data={
            "log": user,
            "pwd": password,
            "wp-submit": "登录",
            "redirect_to": USER_URL,
            "testcookie": "1",
            "rememberme": "forever",
        },
        headers={"Referer": LOGIN_URL,
                 "Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
        allow_redirects=True,
    )
    logged = any("wordpress_logged_in" in c.name for c in http.cookies)
    if not logged:
        err = ""
        m = re.search(r'id="login_error"[^>]*>(.*?)</div>', resp.text, re.S)
        if m:
            err = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        return False, err or "wp-login.php 未返回登录 cookie（账号或密码可能不对）"
    save_cookies(http, session_path)
    return True, ""


def ajax_checkin(http):
    resp = http.post(
        AJAX_URL,
        data={"action": "user_qiandao"},
        headers={
            "Referer": BASE + "/",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
        timeout=30,
    )
    body = resp.text.strip()
    try:
        data = json.loads(body)
    except ValueError:
        return {"status": None, "msg": body[:200], "raw": body[:200],
                "not_logged_in": body in ("0", "-1", "")
                                  or "登录" in body or resp.status_code == 400}
    if isinstance(data, dict):
        data["raw"] = body[:200]
        data["not_logged_in"] = str(data.get("status")) in ("-1",) \
            or "请先登录" in str(data.get("msg", ""))
        return data
    return {"status": None, "msg": body[:200], "raw": body[:200],
            "not_logged_in": True}


def read_points(http):
    try:
        resp = http.get(USER_URL, timeout=30)
    except requests.RequestException:
        return None
    m = re.search(r"现有余额[：:]\s*([0-9]+(?:\.[0-9]+)?)", resp.text)
    if m:
        return m.group(1)
    return None


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
def main():
    parser = argparse.ArgumentParser(
        description="脚本王每日签到（HTTP 直连，不用浏览器）")
    parser.add_argument("--cred", help="凭据文件路径（默认自动查找）")
    parser.add_argument("--session", help="cookie 文件路径（默认 ~/.config/today-checkin/%s）"
                                         % SESSION_FILENAME)
    parser.add_argument("--relogin", action="store_true", help="忽略本地 cookie，强制重新登录")
    parser.add_argument("--dry-run", action="store_true", help="只校验登录态，不提交签到")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--quiet", action="store_true", help="只输出一行结论")
    parser.add_argument("--progress-file", help="进度事件输出文件（JSONL，供面板展示签到过程时间线）")
    args = parser.parse_args()

    logs = []

    def log(msg):
        logs.append(msg)

    progress_fh = _open_progress_file(args.progress_file)

    def step(message):
        _progress_emit(progress_fh, "脚本王", message)

    result = {"site": "脚本王", "host": BASE, "ok": False}
    cred_path = find_cred_file(args.cred)
    if not cred_path:
        print("找不到凭据文件 %s。请用 --cred 指定，或放到 %s/"
              % (CRED_FILENAME, CONFIG_DIR), file=sys.stderr)
        return 4
    user, password = load_credentials(cred_path)
    if not user or not password:
        print("凭据文件 %s 里没解析到登录名/密码字段。" % cred_path, file=sys.stderr)
        return 4
    result["account"] = user

    session_path = args.session or default_session_path()
    http = make_session()
    started = time.time()

    try:
        step("正在执行脚本王签到")
        relogin = False
        if args.relogin or not load_cookies(http, session_path):
            relogin = True
            step("正在重新登录" if args.relogin else "无本地 cookie，正在登录")
        else:
            ok, _ = is_logged_in(http, session_path)
            if ok:
                log("复用本地 cookie（未重新登录）")
                step("登录状态有效，复用本地 cookie")
            else:
                log("本地 cookie 已失效，重新登录")
                step("登录状态失效，正在重新登录")
                relogin = True

        if relogin:
            ok, err = wp_login(http, user, password, session_path)
            if not ok:
                result["error"] = "登录失败：%s" % err
                step("登录失败")
                _emit(args, result, logs, started, code=2)
                return 2
            log("登录成功（wp-login.php 表单登录）")
            step("重新登录成功")

        if args.dry_run:
            result.update({"ok": True, "dry_run": True,
                           "message": "仅校验登录态，未提交签到请求",
                           "points": read_points(http),
                           "relogin": relogin})
            result["session_file"] = session_path
            _emit(args, result, logs, started, code=0)
            return 0

        step("正在提交签到请求")
        data = ajax_checkin(http)
        if data.get("not_logged_in"):
            log("签到接口提示未登录，重新登录后重试一次")
            step("登录状态失效，正在重新登录")
            ok, err = wp_login(http, user, password, session_path)
            if not ok:
                result["error"] = "登录失败：%s" % err
                step("登录失败")
                _emit(args, result, logs, started, code=2)
                return 2
            relogin = True
            step("重新登录成功，正在重新提交签到请求")
            data = ajax_checkin(http)

        status = str(data.get("status"))
        msg = str(data.get("msg") or "").strip()
        result["relogin"] = relogin
        result["ajax_raw"] = data.get("raw")

        if status in ("1", "true"):
            result.update({"ok": True, "checked_today": True, "already": False,
                           "message": msg or "签到成功"})
            step("签到成功")
        elif "已签到" in msg or "明日再来" in msg:
            result.update({"ok": True, "checked_today": True, "already": True,
                           "message": msg or "今日已签到"})
            step("今日已签到")
        elif data.get("not_logged_in"):
            result["error"] = "签到接口仍然返回未登录：%s" % (msg or data.get("raw"))
            _emit(args, result, logs, started, code=2)
            return 2
        else:
            result.update({"ok": False, "message": msg or None,
                           "error": "签到接口返回未预期的结果：%s" % (msg or data.get("raw"))})
            _emit(args, result, logs, started, code=3)
            return 3

        points = read_points(http)
        if points is not None:
            result["points"] = points
        result["session_file"] = session_path
        _emit(args, result, logs, started, code=0)
        return 0

    except requests.RequestException as exc:
        result["error"] = "网络异常：%s" % exc
        _emit(args, result, logs, started, code=3)
        return 3


def _emit(args, result, logs, started, code):
    result["elapsed"] = round(time.time() - started, 2)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.quiet:
        print(result.get("message") or result.get("error"))
        return
    tag = "OK " if result.get("ok") else "ERR"
    print("[%s] 脚本王 每日签到" % tag)
    for line in logs:
        print("  · " + line)
    if result.get("message"):
        print("  结论：" + result["message"])
    if result.get("error"):
        print("  错误：" + result["error"])
    if result.get("points") is not None:
        print("  贡献分余额：%s" % result["points"])
    if result.get("session_file"):
        print("  cookie 文件：%s" % result["session_file"])
    print("  耗时：%ss" % result["elapsed"])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
