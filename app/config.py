"""集中配置：全部走环境变量，开箱即用的默认值。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CHECKIN_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "checkin.db"
SESSIONS_DIR = DATA_DIR / "sessions"
PROGRESS_DIR = DATA_DIR / "progress"
SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"

TIMEZONE = "Asia/Shanghai"

# 站点定义：script 为 app/scripts 下的文件名，
# cred_env 为签到脚本识别的凭据环境变量。
SITES = {
    "jiaobenwang": {
        "name": "脚本王",
        "script": "脚本王签到.py",
        "cred_env": {"login": "JIAOBENWANG_USER", "password": "JIAOBENWANG_PASSWORD"},
        "cred_labels": {"login": "登录名", "password": "密码"},
    },
    "pikaqiu": {
        "name": "皮卡丘token商店",
        "script": "皮卡丘签到.py",
        "cred_env": {"login": "PIKAQIU_EMAIL", "password": "PIKAQIU_PASSWORD"},
        "cred_labels": {"login": "登录邮箱", "password": "密码"},
    },
    "ebondai": {
        "name": "ebondai",
        "script": "ebondai签到.py",
        "cred_env": {
            "login": "EBONDAI_EMAIL",
            "password": "EBONDAI_PASSWORD",
            "api_key": "EBONDAI_API_KEY",
        },
        "cred_labels": {"login": "登录邮箱", "password": "登录密码", "api_key": "API Key"},
    },
}

# Google 表格：默认关闭，需要时再按 README 配置。
SHEETS_ENABLED = os.environ.get("SHEETS_ENABLED", "false").lower() == "true"
SHEETS_SPREADSHEET_ID = os.environ.get("SHEETS_SPREADSHEET_ID", "")
SHEETS_WORKSHEET = os.environ.get("SHEETS_WORKSHEET", "签到记录")
# service account JSON 文件路径（或 JSON 内容，见 sheets.py）
SHEETS_CREDENTIALS = os.environ.get("SHEETS_CREDENTIALS", "")

HOST = os.environ.get("CHECKIN_HOST", "127.0.0.1")
PORT = int(os.environ.get("CHECKIN_PORT", "8000"))


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
