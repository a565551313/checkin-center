"""Google 表格记录（可选模块，默认关闭）。

启用方式见 README：SHEETS_ENABLED=true + service account 凭据。
关闭时 record() 直接返回，不做任何事。
"""
import json
import logging

from . import config

log = logging.getLogger(__name__)

COLUMNS = ["日期", "站点", "运行状态", "签到结论", "账户余额", "连签天数", "备注"]


def record(rows: list) -> bool:
    """rows: [{'date','site','status','conclusion','metric_value','streak','note'}]"""
    if not config.SHEETS_ENABLED:
        return False
    if not rows:
        return True
    try:
        return _append(rows)
    except Exception as exc:  # noqa: BLE001 - 表格失败不影响签到主流程
        log.warning("写入 Google 表格失败：%s", exc)
        return False


def _append(rows: list) -> bool:
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        log.warning("未安装 google-api-python-client，跳过表格记录。"
                    "需要时 pip install google-api-python-client google-auth")
        return False

    creds_src = config.SHEETS_CREDENTIALS
    if not creds_src:
        log.warning("SHEETS_CREDENTIALS 未配置，跳过表格记录")
        return False
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    import os
    if os.path.isfile(creds_src):
        creds = service_account.Credentials.from_service_account_file(creds_src, scopes=scopes)
    else:
        info = json.loads(creds_src)
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)

    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    values = [[r["date"], r["site"], r["status"], r["conclusion"],
               r["metric_value"], r["streak"], r["note"]] for r in rows]
    service.spreadsheets().values().append(
        spreadsheetId=config.SHEETS_SPREADSHEET_ID,
        range=f"{config.SHEETS_WORKSHEET}!A:G",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()
    return True
