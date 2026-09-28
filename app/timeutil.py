"""北京时间工具。"""
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import TIMEZONE


def now_bj() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))


def business_date(dt=None) -> str:
    return (dt or now_bj()).strftime("%Y-%m-%d")


def fmt_hms(dt=None) -> str:
    return (dt or now_bj()).strftime("%H:%M:%S")
