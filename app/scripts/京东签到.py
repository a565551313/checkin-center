#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
京东（m.jd.com）每日京豆签到脚本 —— 纯 HTTP 直连，不依赖浏览器。

凭据：京东登录 Cookie —— pt_key + pt_pin（等同账号密码，妥善保管）：
    pt_key=...;pt_pin=...;

流程（与面板"签到过程"时间线一致）：
  1) 验证 Cookie：GET me-api.jd.com/user_new/info/GetJDUserInfoUnion
  2) 查签到状态：POST api.m.jd.com/client.action?functionId=signBeanIndex
  3) 领取京豆：POST api.m.jd.com/client.action?functionId=signBean
     领豆请求使用兼容参数格式；领取最多尝试 3 次（网络异常/无响应时重试，
     京东给出明确业务拒绝时不再重试，直接透出京东的原话）。

只做每日京豆签到这一件事：不碰购物车、不下单、不领券、不做浏览任务。

用法：
    python3 京东签到.py                 # 一条命令跑完
    python3 京东签到.py --json          # 机器可读输出
    python3 京东签到.py --dry-run       # 只验证 Cookie，不领取京豆
    python3 京东签到.py --cred <文件>   # 指定凭据文件（默认自动查找）
    python3 京东签到.py --session <文件>  # 兼容参数（京东凭据本身就是 Cookie，无需落盘）

凭据来源（按优先级）：
    1) 环境变量 JD_PT_KEY / JD_PT_PIN
    2) 凭据文件里的「pt_key：...」「pt_pin：...」字段
    3) 凭据文件里一整行「Cookie：pt_key=...;pt_pin=...;」

退出码：0 成功（含"今日已签到"）／2 Cookie 失效或被拒／3 网络或接口异常／4 凭据缺失。
"""

import argparse
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

USER_INFO_URL = "https://me-api.jd.com/user_new/info/GetJDUserInfoUnion"
CLIENT_ACTION = "https://api.m.jd.com/client.action"

UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230805.001) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36")

CRED_FILENAME = "jingdong-credentials.md"
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "today-checkin")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

CST = timezone(timedelta(hours=8))

# 领豆最多尝试次数（与面板修复一致：有限重试，每次都有进度提示）
MAX_CLAIM_ATTEMPTS = 3
RETRY_DELAY = 2  # 秒；两次领取尝试之间的等待


# --------------------------------------------------------------------------
# 凭据读取（pt_key / pt_pin 永不打印、永不写入日志）
# --------------------------------------------------------------------------
def _field(text, labels):
    """从凭据文件里取一个字段，兼容 '- pt_key：xxx' / 'pt_key: xxx' 等写法。"""
    for raw in text.splitlines():
        line = raw.strip()
        line = re.sub(r"^[-*+\s]+", "", line)
        line = re.sub(r"^\*+|\*+$", "", line).strip()
        for label in labels:
            if not line.startswith(label):
                continue
            rest = line[len(label):]
            if rest and rest[0] not in "：:（(【[] \t=":
                continue
            m = re.search(r"[:：=]\s*(.+)$", rest)
            if m and m.group(1).strip():
                return m.group(1).strip().rstrip(";")
    return None


def parse_cookie_value(text):
    """从 'pt_key=...;pt_pin=...;' 这样的 Cookie 串里解析出 (pt_key, pt_pin)。"""
    if not text:
        return None, None
    key_m = re.search(r"pt_key=([^;]+)", text)
    pin_m = re.search(r"pt_pin=([^;]+)", text)
    pt_key = key_m.group(1).strip() if key_m else None
    pt_pin = pin_m.group(1).strip() if pin_m else None
    if not pt_key or not pt_pin:
        return None, None
    return pt_key, pt_pin


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
    """返回 (pt_key, pt_pin)；解析不到返回 (None, None)。"""
    env_key = os.environ.get("JD_PT_KEY", "").strip()
    env_pin = os.environ.get("JD_PT_PIN", "").strip()
    if env_key and env_pin:
        return env_key, env_pin
    if not path:
        return None, None
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    pt_key = _field(text, ["pt_key", "PT_KEY"])
    pt_pin = _field(text, ["pt_pin", "PT_PIN"])
    if pt_key and pt_pin:
        return pt_key, pt_pin
    # 兼容一整行 Cookie 的写法：「Cookie：pt_key=...;pt_pin=...;」
    cookie_line = _field(text, ["Cookie", "cookie", "COOKIE"])
    if cookie_line:
        return parse_cookie_value(cookie_line)
    return parse_cookie_value(text)


def cookie_header(pt_key, pt_pin):
    return "pt_key=%s;pt_pin=%s;" % (pt_key, pt_pin)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://m.jd.com/",
    })
    return s


def validate_cookie(http, cookie):
    """校验 Cookie 是否仍有效。网络异常时抛 requests.RequestException。"""
    resp = http.get(
        USER_INFO_URL,
        headers={"Cookie": cookie, "User-Agent": UA,
                 "Referer": "https://m.jd.com/"},
        timeout=30,
    )
    try:
        data = resp.json()
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    return str(data.get("retcode")) in ("0", "200")


def _client_action(http, cookie, function_id, extra_body=None):
    """调 api.m.jd.com/client.action，使用兼容参数格式。"""
    params = {
        "functionId": function_id,
        "appid": "ld",
        "client": "android",
        "clientVersion": "10.6.8",
        "networkType": "wifi",
        "osVersion": "13",
        "uuid": "checkin-center",
    }
    body = {
        "fp": "-1",
        "shshshfp": "-1",
        "shshshfpa": "-1",
        "referUrl": "-1",
        "userAgent": "-1",
        "jda": "-1",
        "rnVersion": "3.9",
    }
    if extra_body:
        body.update(extra_body)
    resp = http.post(
        CLIENT_ACTION,
        params=params,
        data={"body": json.dumps(body, separators=(",", ":"))},
        headers={"Cookie": cookie, "User-Agent": UA,
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Referer": "https://m.jd.com/"},
        timeout=30,
    )
    return resp.text


def parse_claim_response(body_text):
    """解析领豆/状态接口返回。返回 dict(status, beans, message)。

    status 取值：
        success        领取成功
        already        今日已签到
        busy           京东明确业务拒绝（如"签到人数较多"），携带京东原话
        cookie_invalid Cookie 失效 / 未登录
        rejected       其他拒绝（按 Cookie 问题处理）
        unknown        返回无法解析（视为可重试的瞬时问题）
    """
    out = {"status": "unknown", "beans": None, "message": ""}
    text = (body_text or "").strip()
    if not text:
        return out
    try:
        data = json.loads(text)
    except ValueError:
        out["message"] = text[:200]
        return out
    if not isinstance(data, dict):
        out["message"] = text[:200]
        return out

    code = str(data.get("code", ""))
    msg = str(data.get("message") or data.get("msg") or data.get("errMsg") or "").strip()
    out["message"] = msg or text[:200]
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}

    if code in ("0", "200"):
        if "已签" in msg or _payload_flagged(payload, ("isSign", "signed", "todaySigned")):
            out["status"] = "already"
            return out
        beans = _extract_beans(payload, text)
        out["status"] = "success"
        out["beans"] = beans
        return out

    if any(k in msg for k in ("人数较多", "稍晚", "繁忙", "系统繁忙", "稍后再试")):
        out["status"] = "busy"
        return out
    if any(k in msg for k in ("登录", "未登录", "请登录", "cookie", "Cookie",
                              "失效", "过期", "鉴权", "token", "Token")):
        out["status"] = "cookie_invalid"
        return out
    out["status"] = "rejected"
    return out


def _payload_flagged(payload, keys):
    for k in keys:
        v = payload.get(k)
        if v in (True, 1, "1", "true"):
            return True
    return False


def _extract_beans(payload, text):
    """从返回里抠京豆数，抠不到返回 None。"""
    for path in (("dailyAward", "beanAward", "beanCount"),
                 ("beanAward", "beanCount"),
                 ("beanCount",),
                 ("beans",),
                 ("reward",)):
        node = payload
        ok = True
        for p in path:
            if isinstance(node, dict) and p in node:
                node = node[p]
            else:
                ok = False
                break
        if ok and node is not None and str(node).strip().isdigit():
            return str(node).strip()
    m = re.search(r"(\d+)\s*京豆", text)
    if m:
        return m.group(1)
    return None


# --------------------------------------------------------------------------
# 进度事件（--progress-file）：面板"签到过程"时间线的数据源。
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
# 主流程（可被测试直接调用；http 为可注入的会话对象）
# --------------------------------------------------------------------------
def run_checkin(http, pt_key, pt_pin, step, dry_run=False):
    """执行一次京东签到，返回 result dict（含 ok / error / error_type / message）。"""
    cookie = cookie_header(pt_key, pt_pin)
    result = {"site": "京东", "ok": False}

    step("正在验证 Cookie")
    try:
        valid = validate_cookie(http, cookie)
    except requests.RequestException as exc:
        result["error"] = "网络异常：%s" % exc
        result["error_type"] = "network"
        return result
    if not valid:
        step("Cookie 已失效")
        result["error"] = "签到接口拒绝了当前 Cookie，请重新录入后再试"
        result["error_type"] = "cookie_invalid"
        return result

    step("Cookie 有效，正在领取京豆")
    if dry_run:
        result.update({"ok": True, "dry_run": True,
                       "message": "Cookie 有效（仅校验，未领取京豆）"})
        return result

    # 先查今日状态：已签到就直接返回，不重复领取
    try:
        index_raw = _client_action(http, cookie, "signBeanIndex")
    except requests.RequestException as exc:
        result["error"] = "网络异常：%s" % exc
        result["error_type"] = "network"
        return result
    index = parse_claim_response(index_raw)
    if index["status"] == "already":
        step("今日已签到")
        result.update({"ok": True, "already": True, "checked_today": True,
                       "message": "今日已签到"})
        return result
    if index["status"] == "cookie_invalid":
        step("签到被拒绝，需要更新 Cookie 或稍后重试")
        result["error"] = "签到接口拒绝了当前 Cookie，请重新录入后再试"
        result["error_type"] = "cookie_invalid"
        return result

    # 领取京豆：最多尝试 MAX_CLAIM_ATTEMPTS 次
    last_error = None
    for attempt in range(1, MAX_CLAIM_ATTEMPTS + 1):
        step("正在领取京豆（第 %d 次）" % attempt)
        try:
            raw = _client_action(http, cookie, "signBean")
        except requests.RequestException as exc:
            last_error = ("网络异常：%s" % exc, "network")
            if attempt < MAX_CLAIM_ATTEMPTS:
                step("网络异常，稍后重试")
                time.sleep(RETRY_DELAY)
                continue
            break
        parsed = parse_claim_response(raw)
        status = parsed["status"]
        if status == "success":
            beans = parsed["beans"]
            step("签到成功")
            result.update({"ok": True, "checked_today": True, "already": False,
                           "beans": beans})
            result["message"] = ("签到成功，获得 %s 京豆" % beans) if beans else "签到成功"
            return result
        if status == "already":
            step("今日已签到")
            result.update({"ok": True, "already": True, "checked_today": True,
                           "message": "今日已签到"})
            return result
        if status == "busy":
            msg = parsed["message"] or "京东服务繁忙"
            step("京东返回：%s" % msg)
            result["error"] = "京东签到未通过：%s" % msg
            result["error_type"] = "platform_rejected"
            return result
        if status in ("cookie_invalid", "rejected"):
            step("签到被拒绝，需要更新 Cookie 或稍后重试")
            result["error"] = "签到接口拒绝了当前 Cookie，请重新录入后再试"
            result["error_type"] = "cookie_invalid"
            return result
        # unknown：返回无法解析，视为瞬时问题，可重试
        last_error = ("京东返回无法解析：%s" % parsed["message"][:100], "unknown")
        if attempt < MAX_CLAIM_ATTEMPTS:
            step("京东返回异常，稍后重试")
            time.sleep(RETRY_DELAY)

    err_text, err_type = last_error or ("领取京豆失败", "unknown")
    if err_type == "network":
        err_text = "%s（已重试）" % err_text
    result["error"] = err_text
    result["error_type"] = err_type
    return result


def main():
    parser = argparse.ArgumentParser(
        description="京东每日京豆签到（HTTP 直连，不用浏览器）")
    parser.add_argument("--cred", help="凭据文件路径（默认自动查找）")
    parser.add_argument("--session", help="兼容参数：京东凭据本身就是 Cookie，无需落盘")
    parser.add_argument("--dry-run", action="store_true", help="只验证 Cookie，不领取京豆")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--quiet", action="store_true", help="只输出一行结论")
    parser.add_argument("--progress-file", help="进度事件输出文件（JSONL，供面板展示签到过程时间线）")
    args = parser.parse_args()

    logs = []

    def log(msg):
        logs.append(msg)

    progress_fh = _open_progress_file(args.progress_file)

    def step(message):
        _progress_emit(progress_fh, "京东", message)

    result = {"site": "京东", "ok": False}
    started = time.time()

    cred_path = find_cred_file(args.cred)
    pt_key, pt_pin = load_credentials(cred_path)
    if not pt_key or not pt_pin:
        print("缺少京东凭据：需要 pt_key + pt_pin。可用 --cred 指定凭据文件，"
              "或设置环境变量 JD_PT_KEY / JD_PT_PIN。", file=sys.stderr)
        return 4
    # 账号标识只取 pt_pin（用户名），且只在非 --json 输出里脱敏展示
    result["account"] = crypto_mask(pt_pin)

    http = make_session()
    try:
        result = run_checkin(http, pt_key, pt_pin, step, dry_run=args.dry_run)
        result["account"] = crypto_mask(pt_pin)
        code = 0 if result.get("ok") else (2 if result.get("error_type") == "cookie_invalid" else 3)
    except requests.RequestException as exc:
        result["error"] = "网络异常：%s" % exc
        result["error_type"] = "network"
        code = 3

    _emit(args, result, logs, started, code)
    return code


def crypto_mask(pt_pin):
    """脱敏展示 pt_pin（用户名），永远不返回原文：j***1 风格。"""
    s = (pt_pin or "").strip()
    if not s:
        return "未设置"
    if len(s) <= 2:
        return "***"
    return "%s***%s" % (s[0], s[-1])


def _emit(args, result, logs, started, code):
    result["elapsed"] = round(time.time() - started, 2)
    result["exit_code"] = code
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.quiet:
        print(result.get("message") or result.get("error"))
        return
    tag = "OK " if result.get("ok") else "ERR"
    print("[%s] 京东 每日京豆签到" % tag)
    for line in logs:
        print("  · " + line)
    if result.get("message"):
        print("  结论：" + result["message"])
    if result.get("error"):
        print("  错误：" + str(result["error"]))
    if result.get("beans") is not None:
        print("  本次获得京豆：%s" % result["beans"])
    if result.get("account"):
        print("  账号：%s" % result["account"])
    print("  耗时：%ss" % result["elapsed"])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
