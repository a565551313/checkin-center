"""签到控制中心（standalone 版）- FastAPI 入口。"""
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, crypto, db, runner, scheduler
from .timeutil import business_date

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时必须有加密密钥，否则拒绝启动，避免凭据明文落盘
    crypto.get_fernet()
    config.ensure_dirs()
    db.init_db()
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="签到控制中心", lifespan=lifespan)


# ---------- models ----------
class SettingsIn(BaseModel):
    enabled: bool
    time: str


class AccountIn(BaseModel):
    siteKey: str
    nickname: "str | None" = None
    login: "str | None" = None
    password: "str | None" = None
    apiKey: "str | None" = None
    enabled: bool = True


class AccountUpdate(BaseModel):
    nickname: "str | None" = None
    enabled: "bool | None" = None
    login: "str | None" = None
    password: "str | None" = None
    apiKey: "str | None" = None
    replaceCredentials: bool = False


class ToggleIn(BaseModel):
    enabled: bool


class ManualRunIn(BaseModel):
    accountIds: list


# ---------- helpers ----------
def public_account(a: dict, today_map: dict) -> dict:
    login = crypto.decrypt(a.get("login_enc"))
    has_key = bool(crypto.decrypt(a.get("api_key_enc")))
    return {
        "id": a["id"],
        "siteKey": a["site_key"],
        "siteName": config.SITES[a["site_key"]]["name"],
        "nickname": a.get("nickname"),
        "accountLabel": crypto.mask_login(login),
        "credentialConfigured": bool(login) or has_key,
        "enabled": bool(a["enabled"]),
        "today": today_map.get(a["id"]),
    }


def _check_site(site_key: str):
    if site_key not in config.SITES:
        raise HTTPException(400, f"未知站点：{site_key}")


# ---------- dashboard ----------
@app.get("/api/dashboard")
def dashboard():
    biz = business_date()
    accounts = db.list_accounts()
    today_map = db.latest_results_by_account(biz)
    settings = db.get_settings()
    runs = db.list_runs(10)
    # 列表只带结果摘要，步骤按需拉取
    return {
        "accounts": [public_account(a, today_map) for a in accounts],
        "autoCheckin": {
            "enabled": bool(settings["enabled"]),
            "time": settings["time"],
            "nextRunAt": scheduler.next_run_at(settings),
            "updatedAt": settings["updated_at"],
        },
        "businessDate": biz,
        "runs": runs,
        "running": runner.is_running(),
    }


@app.get("/api/runs")
def runs(limit: int = 20):
    return {"runs": db.list_runs(min(limit, 100))}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "运行记录不存在")
    return run


# ---------- settings ----------
@app.get("/api/settings")
def get_settings():
    s = db.get_settings()
    return {"enabled": bool(s["enabled"]), "time": s["time"], "updatedAt": s["updated_at"]}


@app.put("/api/settings")
def put_settings(body: SettingsIn):
    if not TIME_RE.match(body.time):
        raise HTTPException(400, "时间格式应为 HH:MM（24 小时制）")
    s = db.update_settings(body.enabled, body.time)
    return {"enabled": bool(s["enabled"]), "time": s["time"], "updatedAt": s["updated_at"]}


# ---------- accounts ----------
@app.post("/api/accounts")
def create_account(body: AccountIn):
    _check_site(body.siteKey)
    if body.siteKey == "ebondai":
        if not body.apiKey:
            raise HTTPException(400, "ebondai 需要填写 API Key")
    elif not body.login or not body.password:
        raise HTTPException(400, "需要填写登录账号和密码")
    a = db.create_account(
        body.siteKey,
        (body.nickname or "").strip() or None,
        crypto.encrypt(body.login),
        crypto.encrypt(body.password),
        crypto.encrypt(body.apiKey),
        enabled=body.enabled,
    )
    return {"ok": True, "accountId": a["id"], "message": "账号已添加"}


@app.put("/api/accounts/{account_id}")
def update_account(account_id: str, body: AccountUpdate):
    a = db.get_account(account_id)
    if not a:
        raise HTTPException(404, "账号不存在")
    fields = {}
    if body.nickname is not None:
        fields["nickname"] = body.nickname.strip() or None
    if body.enabled is not None:
        fields["enabled"] = body.enabled
    if body.replaceCredentials:
        fields["login_enc"] = crypto.encrypt(body.login)
        fields["password_enc"] = crypto.encrypt(body.password)
        fields["api_key_enc"] = crypto.encrypt(body.apiKey)
    a = db.update_account(account_id, **fields)
    return {"ok": True, "accountId": account_id, "message": "账号已更新"}


@app.post("/api/accounts/{account_id}/toggle")
def toggle_account(account_id: str, body: ToggleIn):
    a = db.get_account(account_id)
    if not a:
        raise HTTPException(404, "账号不存在")
    db.update_account(account_id, enabled=body.enabled)
    return {"ok": True, "message": "账号已启用" if body.enabled else "账号已停用"}


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: str):
    # 前端负责二次确认；此处删除凭据，历史记录保留账号标签
    if not db.delete_account(account_id):
        raise HTTPException(404, "账号不存在")
    return {"ok": True, "message": "账号和保存的凭据已删除，历史记录已保留"}


# ---------- runs ----------
@app.post("/api/runs/manual")
def manual_run(body: ManualRunIn):
    if not body.accountIds:
        raise HTTPException(400, "请先勾选要签到的账号")
    ret = runner.start_run("manual", account_ids=body.accountIds)
    if not ret["ok"]:
        raise HTTPException(409, ret["error"])
    return {"ok": True, "runId": ret["runId"], "message": "签到已开始"}


@app.post("/api/runs/now")
def run_now():
    """立即执行一次：对所有已启用账号执行自动签到流程。"""
    ret = runner.start_run("scheduled")
    if not ret["ok"]:
        raise HTTPException(409, ret["error"])
    return {"ok": True, "runId": ret["runId"], "message": "自动签到已开始"}


@app.get("/api/scheduler/preview")
def scheduler_preview():
    """调试用：看下一分钟滴答会不会触发（不实际执行）。"""
    ok, reason = scheduler.should_run()
    return {"wouldRun": ok, "reason": reason}


# ---------- static ----------
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


@app.exception_handler(500)
async def _500(request, exc):
    return JSONResponse({"ok": False, "error": "服务器内部错误"}, status_code=500)
