"""内置调度：每分钟检查一次，自动签到核心。

规则（北京时间）：
- 自动签到未开启 → 跳过
- 当天还没到设定时间 → 跳过
- 当天已经自动跑过一次（成功或失败都算）→ 跳过
- 到了时间且当天没跑过 → 执行；若启动时已错过设定时间则补跑
"""
from datetime import timedelta
from typing import Optional, Tuple
from apscheduler.schedulers.background import BackgroundScheduler

from . import db, runner
from .timeutil import business_date, now_bj

_scheduler: Optional[BackgroundScheduler] = None


def should_run() -> Tuple[bool, str]:
    """返回 (是否执行, 原因)。纯判断，不产生副作用。"""
    settings = db.get_settings()
    if not settings or not settings["enabled"]:
        return False, "自动签到未开启"
    try:
        hh, mm = settings["time"].split(":")
        hour, minute = int(hh), int(mm)
    except (ValueError, AttributeError):
        return False, f"时间格式错误：{settings and settings['time']}"
    now = now_bj()
    biz = business_date(now)
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < scheduled:
        return False, "尚未到自动签到时间"
    if db.scheduled_run_done_today(biz):
        return False, "今日已自动签到"
    return True, "到点执行"


def tick() -> None:
    ok, _ = should_run()
    if not ok:
        return
    if runner.is_running():
        return
    accounts = [a for a in db.list_accounts() if a["enabled"]]
    if not accounts:
        return
    runner.start_run("scheduled")


def next_run_at(settings=None) -> Optional[str]:
    """计算下一次自动运行时间的 ISO 字符串（北京时间），未开启返回 None。"""
    settings = settings or db.get_settings()
    if not settings or not settings["enabled"]:
        return None
    try:
        hh, mm = settings["time"].split(":")
        hour, minute = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    now = now_bj()
    biz = business_date(now)
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    
    if now < scheduled:
        return scheduled.isoformat()
    # 已经过了今天的时间点：如果今天还没跑过，说明会在下一个调度周期补跑
    if not db.scheduled_run_done_today(biz):
        return now.isoformat()
    # 今天已经跑过了，下一次运行在明天
    return (scheduled + timedelta(days=1)).isoformat()


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    _scheduler.add_job(tick, "interval", minutes=1, id="auto-checkin-tick",
                       max_instances=1, coalesce=True)
    _scheduler.start()


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
