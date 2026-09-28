"""统一通知服务：支持多渠道异步推送签到汇总报告。

支持渠道：
- Telegram Bot
- Bark (iOS)
- Server酱 (Turbo)
- PushPlus (推送加)
- 飞书群机器人 Webhook
- 钉钉群机器人 Webhook
- 企业微信群机器人 Webhook
- 自定义 HTTP Webhook
"""
import json
import logging
import threading
from typing import List, Dict, Any

import requests

from . import config
from .timeutil import now_bj

log = logging.getLogger(__name__)


def build_report_text(mode: str, business_date: str, total: int,
                      succeeded: int, failed: int, results: List[Dict[str, Any]]) -> str:
    """生成易读的纯文本/Markdown 签到报告。"""
    mode_str = "自动定时签到" if mode == "scheduled" else "手动触发签到"
    lines = [
        f"【签到控制中心】{mode_str} 报告",
        f"业务日期：{business_date}",
        f"执行时间：{now_bj().strftime('%Y-%m-%d %H:%M:%S')}",
        f"汇总统计：共 {total} 个账号 | ✅ 成功 {succeeded} | ❌ 失败 {failed}",
        "",
        "--- 账号详情 ---",
    ]
    for r in results:
        site = r.get("site") or r.get("site_key") or "未知站点"
        status = r.get("status")
        conclusion = r.get("conclusion") or "—"
        note = r.get("note") or ""
        metric = r.get("metric_value")
        streak = r.get("streak")
        
        detail_parts = [f"状态: {status}"]
        if metric and metric != "—":
            detail_parts.append(f"余额/分值: {metric}")
        if streak and streak != "—":
            detail_parts.append(f"连签: {streak}天")
        if conclusion and conclusion != status:
            detail_parts.append(f"结论: {conclusion}")
        if note:
            detail_parts.append(f"({note})")
            
        icon = "✅" if status in ("成功", "success") else "❌"
        lines.append(f"{icon} [{site}] " + " | ".join(detail_parts))

    return "\n".join(lines)


def _send_telegram(title: str, text: str) -> None:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": f"*{title}*\n\n{text}",
        "parse_mode": "Markdown",
    }
    requests.post(url, json=payload, timeout=15)


def _send_bark(title: str, text: str) -> None:
    if not config.BARK_URL:
        return
    url = config.BARK_URL.rstrip("/")
    # 如果配置的是完整 key 或 自建服务端
    if not url.startswith("http://") and not url.startswith("https://"):
        url = f"https://api.day.app/{url}"
    payload = {
        "title": title,
        "body": text,
        "group": "checkin-center",
        "icon": "https://img.icons8.com/fluency/96/tasklist.png",
    }
    requests.post(url, json=payload, timeout=15)


def _send_serverchan(title: str, text: str) -> None:
    if not config.SERVERCHAN_KEY:
        return
    url = f"https://sctapi.ftqq.com/{config.SERVERCHAN_KEY}.send"
    payload = {"title": title, "desp": text}
    requests.post(url, data=payload, timeout=15)


def _send_pushplus(title: str, text: str) -> None:
    if not config.PUSHPLUS_TOKEN:
        return
    url = "https://www.pushplus.plus/send"
    payload = {
        "token": config.PUSHPLUS_TOKEN,
        "title": title,
        "content": text.replace("\n", "<br>"),
        "template": "html",
    }
    requests.post(url, json=payload, timeout=15)


def _send_feishu(title: str, text: str) -> None:
    if not config.FEISHU_WEBHOOK:
        return
    payload = {
        "msg_type": "text",
        "content": {"text": f"{title}\n\n{text}"},
    }
    requests.post(config.FEISHU_WEBHOOK, json=payload, timeout=15)


def _send_dingtalk(title: str, text: str) -> None:
    if not config.DINGTALK_WEBHOOK:
        return
    payload = {
        "msgtype": "text",
        "text": {"content": f"{title}\n\n{text}"},
    }
    requests.post(config.DINGTALK_WEBHOOK, json=payload, timeout=15)


def _send_wecom(title: str, text: str) -> None:
    if not config.WECOM_WEBHOOK:
        return
    payload = {
        "msgtype": "text",
        "text": {"content": f"{title}\n\n{text}"},
    }
    requests.post(config.WECOM_WEBHOOK, json=payload, timeout=15)


def _send_custom_webhook(title: str, text: str, data: Dict[str, Any]) -> None:
    if not config.CUSTOM_WEBHOOK_URL:
        return
    payload = {
        "title": title,
        "content": text,
        "data": data,
        "timestamp": now_bj().isoformat(),
    }
    requests.post(config.CUSTOM_WEBHOOK_URL, json=payload, timeout=15)


def _dispatch_notifications(title: str, text: str, data: Dict[str, Any]) -> None:
    """分发所有已配置的通知渠道。"""
    channels = [
        ("Telegram", lambda: _send_telegram(title, text)),
        ("Bark", lambda: _send_bark(title, text)),
        ("ServerChan", lambda: _send_serverchan(title, text)),
        ("PushPlus", lambda: _send_pushplus(title, text)),
        ("Feishu", lambda: _send_feishu(title, text)),
        ("DingTalk", lambda: _send_dingtalk(title, text)),
        ("WeCom", lambda: _send_wecom(title, text)),
        ("CustomWebhook", lambda: _send_custom_webhook(title, text, data)),
    ]
    for name, sender in channels:
        try:
            sender()
        except Exception as exc:
            log.warning("通知渠道 [%s] 发送失败: %s", name, exc)


def send_run_report(mode: str, business_date: str, total: int,
                    succeeded: int, failed: int, results: List[Dict[str, Any]]) -> None:
    """异步发送签到汇总通知（不阻塞主执行流）。"""
    # 只要开启了 NOTIFY_ENABLED 或配置了任意一种通知密钥，就触发推送
    has_any_channel = (
        config.NOTIFY_ENABLED
        or bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)
        or bool(config.BARK_URL)
        or bool(config.SERVERCHAN_KEY)
        or bool(config.PUSHPLUS_TOKEN)
        or bool(config.FEISHU_WEBHOOK)
        or bool(config.DINGTALK_WEBHOOK)
        or bool(config.WECOM_WEBHOOK)
        or bool(config.CUSTOM_WEBHOOK_URL)
    )
    if not has_any_channel:
        return

    title = f"{config.NOTIFY_TITLE} ({succeeded}成功/{failed}失败)"
    text = build_report_text(mode, business_date, total, succeeded, failed, results)
    data = {
        "mode": mode,
        "business_date": business_date,
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }

    t = threading.Thread(target=_dispatch_notifications, args=(title, text, data), daemon=True)
    t.start()
