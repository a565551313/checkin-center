"""签到控制中心 —— FastAPI 入口与路由。"""
import csv
import io
import json
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Request, Response, Header, Depends
from fastapi.responses import JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, crypto, db, runner, scheduler
from .timeutil import business_date, now_bj

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Web 访问 Token 内存集（简单安全，服务重启自动失效）
_valid_tokens = set()


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


# ---------- 中间件：安全响应头与访问鉴权 ----------
@app.middleware("http")
async def security_and_auth_middleware(request: Request, call_next):
    # 1. 静态资源与鉴权接口放行
    path = request.url.path
    if (
        not config.CHECKIN_WEB_PASSWORD
        or not path.startswith("/api/")
        or path in ("/api/auth/login", "/api/auth/status", "/api/health")
    ):
        response = await call_next(request)
        _add_security_headers(response)
        return response

    # 2. 检查 Header 或 Cookie 中的 Token
    auth_header = request.headers.get("Authorization", "")
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    if not token:
        token = request.cookies.get("checkin_token", "")

    if not token or token not in _valid_tokens:
        return JSONResponse(
            status_code=401,
            content={"ok": False, "authRequired": True, "error": "请先登录控制面板"},
        )

    response = await call_next(request)
    _add_security_headers(response)
    return response


def _add_security_headers(response: Response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"


# ---------- models ----------
class LoginIn(BaseModel):
    password: str


class SettingsIn(BaseModel):
    enabled: bool
    time: str


class AccountIn(BaseModel):
    siteKey: str
    nickname: Optional[str] = None
    login: Optional[str] = None
    password: Optional[str] = None
    apiKey: Optional[str] = None
    enabled: bool = True


class AccountUpdate(BaseModel):
    nickname: Optional[str] = None
    enabled: Optional[bool] = None
    login: Optional[str] = None
    password: Optional[str] = None
    apiKey: Optional[str] = None
    replaceCredentials: bool = False


class ToggleIn(BaseModel):
    enabled: bool


class ManualRunIn(BaseModel):
    accountIds: List[str]


# ---------- helpers ----------
def public_account(a: dict, today_map: dict) -> dict:
    login, login_ok = crypto.safe_decrypt(a.get("login_enc"))
    key, key_ok = crypto.safe_decrypt(a.get("api_key_enc"))
    decrypt_error = not (login_ok and key_ok)

    site_info = config.SITES.get(a["site_key"], {"name": a["site_key"]})
    return {
        "id": a["id"],
        "siteKey": a["site_key"],
        "siteName": site_info["name"],
        "nickname": a.get("nickname"),
        "accountLabel": "[凭据解密失败]" if decrypt_error else crypto.mask_login(login),
        "credentialConfigured": (bool(login) or bool(key)) and not decrypt_error,
        "decryptError": decrypt_error,
        "enabled": bool(a["enabled"]),
        "today": today_map.get(a["id"]),
    }


def _check_site(site_key: str):
    if site_key not in config.SITES:
        raise HTTPException(400, f"未知站点：{site_key}")


# ---------- auth ----------
@app.get("/api/auth/status")
def auth_status(request: Request):
    if not config.CHECKIN_WEB_PASSWORD:
        return {"authEnabled": False, "authenticated": True}
    token = request.cookies.get("checkin_token", "")
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    return {
        "authEnabled": True,
        "authenticated": token in _valid_tokens,
    }


@app.post("/api/auth/login")
def auth_login(body: LoginIn, response: Response):
    if not config.CHECKIN_WEB_PASSWORD:
        return {"ok": True, "message": "无需认证"}
    if secrets.compare_digest(body.password, config.CHECKIN_WEB_PASSWORD):
        token = secrets.token_hex(24)
        _valid_tokens.add(token)
        response.set_cookie(
            "checkin_token",
            token,
            httponly=True,
            samesite="lax",
            max_age=86400 * 30,
        )
        return {"ok": True, "token": token, "message": "登录成功"}
    raise HTTPException(401, "访问密码错误")


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    token = request.cookies.get("checkin_token", "")
    _valid_tokens.discard(token)
    response.delete_cookie("checkin_token")
    return {"ok": True, "message": "已退出登录"}


# ---------- health & metadata ----------
@app.get("/api/health")
def health():
    return {"status": "ok", "businessDate": business_date()}


@app.get("/api/sites")
def get_sites():
    """返回所有支持站点的元数据和动态表单字段定义。"""
    sites_list = []
    for k, v in config.SITES.items():
        sites_list.append({
            "key": k,
            "name": v["name"],
            "fields": v.get("fields", []),
        })
    return {"sites": sites_list}


# ---------- dashboard ----------
@app.get("/api/dashboard")
def dashboard():
    biz = business_date()
    accounts = db.list_accounts()
    today_map = db.latest_results_by_account(biz)
    settings = db.get_settings()
    runs, _ = db.list_runs(limit=10)
    stats = db.get_summary_stats(biz)
    
    return {
        "accounts": [public_account(a, today_map) for a in accounts],
        "stats": stats,
        "autoCheckin": {
            "enabled": bool(settings["enabled"]),
            "time": settings["time"],
            "nextRunAt": scheduler.next_run_at(settings),
            "updatedAt": settings["updated_at"],
        },
        "businessDate": biz,
        "runs": runs,
        "running": runner.is_running(),
        "activeRunId": runner.get_active_run_id(),
    }


# ---------- runs ----------
@app.get("/api/runs")
def get_runs(limit: int = 20, offset: int = 0, status: Optional[str] = None,
             mode: Optional[str] = None):
    runs_list, total = db.list_runs(
        limit=min(limit, 100), offset=max(offset, 0), status=status, mode=mode
    )
    return {"runs": runs_list, "total": total, "limit": limit, "offset": offset}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "运行记录不存在")
    return run


@app.post("/api/runs/{run_id}/cancel")
def cancel_run_api(run_id: str):
    """手动终止当前执行中的签到任务。"""
    ret = runner.cancel_run(run_id)
    if not ret["ok"]:
        raise HTTPException(400, ret["error"])
    return ret


@app.get("/api/runs/{run_id}/stream")
async def run_stream(run_id: str):
    """Server-Sent Events (SSE)：实时推送签到步骤与完成状态。"""
    import asyncio

    async def event_generator():
        last_step_id = 0
        while True:
            steps = db.get_steps_after(run_id, last_step_id)
            for s in steps:
                last_step_id = max(last_step_id, s["id"])
                yield f"data: {json.dumps({'type': 'step', 'step': s}, ensure_ascii=False)}\n\n"
            
            run = db.get_run(run_id)
            if not run:
                break
            if run["status"] != "running":
                yield f"data: {json.dumps({'type': 'finish', 'run': run}, ensure_ascii=False)}\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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


@app.post("/api/runs/retry-failed")
def retry_failed():
    """重试今日尚未成功或失败的已启用账号。"""
    ret = runner.start_failed_retry()
    if not ret["ok"]:
        raise HTTPException(400, ret["error"])
    return {"ok": True, "runId": ret["runId"], "message": "已发起重试签到"}


# ---------- export ----------
@app.get("/api/export/today.csv")
def export_today_csv():
    """导出今日签到结果为 CSV 文件。"""
    biz = business_date()
    today_map = db.latest_results_by_account(biz)
    accounts = {a["id"]: a for a in db.list_accounts()}
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["业务日期", "站点", "账号备注/脱敏标识", "运行状态", "签到结论", "余额/分值", "连签天数", "完成时间"])
    
    for acct_id, res in today_map.items():
        acct = accounts.get(acct_id, {})
        site_name = config.SITES.get(res["site_key"], {}).get("name", res["site_key"])
        writer.writerow([
            res["business_date"],
            site_name,
            acct.get("nickname") or res["account_label"],
            "成功" if res["status"] == "success" else "失败",
            res["conclusion"] or "",
            f"{res['metric_label'] or ''} {res['metric_value'] or ''}".strip(),
            res["streak_days"] or "",
            res["completed_at"],
        ])
    
    output.seek(0)
    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=checkin-{biz}.csv"},
    )


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
    if not db.delete_account(account_id):
        raise HTTPException(404, "账号不存在")
    return {"ok": True, "message": "账号和保存的凭据已删除，历史记录已保留"}


# ---------- static ----------
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


@app.exception_handler(500)
async def _500(request, exc):
    return JSONResponse({"ok": False, "error": "服务器内部错误"}, status_code=500)
