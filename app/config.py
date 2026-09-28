"""集中配置：全部走环境变量与 .env 文件，开箱即用的默认值。"""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# 自动加载 .env 文件（若存在）
load_dotenv(BASE_DIR / ".env")

DATA_DIR = Path(os.environ.get("CHECKIN_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "checkin.db"
SESSIONS_DIR = DATA_DIR / "sessions"
PROGRESS_DIR = DATA_DIR / "progress"
SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"

TIMEZONE = os.environ.get("CHECKIN_TIMEZONE", "Asia/Shanghai")

# 可选：Web 访问鉴权密码（留空则不开启密码验证）
CHECKIN_WEB_PASSWORD = os.environ.get("CHECKIN_WEB_PASSWORD", "").strip()

# 站点定义：script 为 app/scripts 下的文件名，
# cred_env 为签到脚本识别的凭据环境变量，
# fields 为前端动态表单渲染所需字段定义。
SITES = {
    "jiaobenwang": {
        "name": "脚本王",
        "script": "脚本王签到.py",
        "cred_env": {"login": "JIAOBENWANG_USER", "password": "JIAOBENWANG_PASSWORD"},
        "cred_labels": {"login": "登录名", "password": "密码"},
        "fields": [
            {
                "key": "login",
                "label": "登录名 / 邮箱",
                "type": "text",
                "placeholder": "user@example.com 或用户名",
                "required": True,
            },
            {
                "key": "password",
                "label": "登录密码",
                "type": "password",
                "placeholder": "新增时必填；编辑时留空不更换",
                "required": True,
            },
        ],
    },
    "pikaqiu": {
        "name": "皮卡丘token商店",
        "script": "皮卡丘签到.py",
        "cred_env": {"login": "PIKAQIU_EMAIL", "password": "PIKAQIU_PASSWORD"},
        "cred_labels": {"login": "登录邮箱", "password": "密码"},
        "fields": [
            {
                "key": "login",
                "label": "登录邮箱",
                "type": "text",
                "placeholder": "user@example.com",
                "required": True,
            },
            {
                "key": "password",
                "label": "登录密码",
                "type": "password",
                "placeholder": "新增时必填；编辑时留空不更换",
                "required": True,
            },
        ],
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
        "fields": [
            {
                "key": "login",
                "label": "登录邮箱",
                "type": "text",
                "placeholder": "user@example.com",
                "required": True,
            },
            {
                "key": "password",
                "label": "登录密码",
                "type": "password",
                "placeholder": "新增时必填；编辑时留空不更换",
                "required": True,
            },
            {
                "key": "apiKey",
                "label": "API Key",
                "type": "password",
                "placeholder": "sk-...",
                "required": True,
                "hint": "准入门槛需当天有 API 调用",
            },
        ],
    },
}

# 脚本运行与重试配置
RUNNER_TIMEOUT = int(os.environ.get("CHECKIN_SCRIPT_TIMEOUT", "300"))
RUNNER_RETRY_DELAY = int(os.environ.get("CHECKIN_RETRY_DELAY", "5"))

# 消息通知配置（支持 Telegram, Bark, Server酱, PushPlus, 飞书, 钉钉, 企业微信, 自定义 Webhook）
NOTIFY_ENABLED = os.environ.get("NOTIFY_ENABLED", "false").lower() == "true"
NOTIFY_TITLE = os.environ.get("NOTIFY_TITLE", "签到控制中心通知")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
BARK_URL = os.environ.get("BARK_URL", "").strip()
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "").strip()
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "").strip()
DINGTALK_WEBHOOK = os.environ.get("DINGTALK_WEBHOOK", "").strip()
WECOM_WEBHOOK = os.environ.get("WECOM_WEBHOOK", "").strip()
CUSTOM_WEBHOOK_URL = os.environ.get("CUSTOM_WEBHOOK_URL", "").strip()

# Google 表格：默认关闭，需要时再按 README 配置
SHEETS_ENABLED = os.environ.get("SHEETS_ENABLED", "false").lower() == "true"
SHEETS_SPREADSHEET_ID = os.environ.get("SHEETS_SPREADSHEET_ID", "")
SHEETS_WORKSHEET = os.environ.get("SHEETS_WORKSHEET", "签到记录")
SHEETS_CREDENTIALS = os.environ.get("SHEETS_CREDENTIALS", "")

HOST = os.environ.get("CHECKIN_HOST", "127.0.0.1")
PORT = int(os.environ.get("CHECKIN_PORT", "8000"))


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
