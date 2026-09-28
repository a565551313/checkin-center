"""SQLite 存取：标准库 sqlite3，WAL 模式，性能优化与安全隔离。"""
import sqlite3
import threading
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple

from . import config

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 1,
    time TEXT NOT NULL DEFAULT '08:42',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    site_key TEXT NOT NULL,
    nickname TEXT,
    login_enc TEXT,
    password_enc TEXT,
    api_key_enc TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    business_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    error TEXT,
    total INTEGER NOT NULL DEFAULT 0,
    succeeded INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    site_key TEXT NOT NULL,
    account_label TEXT NOT NULL,
    business_date TEXT NOT NULL,
    status TEXT NOT NULL,
    conclusion TEXT,
    metric_label TEXT,
    metric_value TEXT,
    streak_days INTEGER,
    note TEXT,
    completed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    site_key TEXT,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_run ON results(run_id);
CREATE INDEX IF NOT EXISTS idx_results_biz ON results(business_date);
CREATE INDEX IF NOT EXISTS idx_results_biz_account ON results(business_date, account_id);
CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_biz_mode ON runs(business_date, mode, status);
"""


def _connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(str(config.DB_PATH), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def init_db() -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript(SCHEMA)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO settings (id, enabled, time, updated_at)"
                " VALUES (1, 1, '08:42', ?)",
                (now,),
            )
            conn.commit()
        finally:
            conn.close()


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


# ---- settings ----
def get_settings() -> Dict[str, Any]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM settings WHERE id=1").fetchone()
        return _row_to_dict(row) or {"enabled": 1, "time": "08:42", "updated_at": ""}
    finally:
        conn.close()


def update_settings(enabled: bool, time: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE settings SET enabled=?, time=?, updated_at=? WHERE id=1",
                (1 if enabled else 0, time, now),
            )
            conn.commit()
        finally:
            conn.close()
    return get_settings()


# ---- accounts ----
def list_accounts() -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM accounts ORDER BY site_key, created_at"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_account(account_id: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def create_account(site_key: str, nickname: Optional[str], login_enc: Optional[str],
                   password_enc: Optional[str], api_key_enc: Optional[str],
                   enabled: bool = True) -> Optional[Dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    account_id = str(uuid.uuid4())
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO accounts (id, site_key, nickname, login_enc, password_enc,"
                " api_key_enc, enabled, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (account_id, site_key, nickname, login_enc, password_enc, api_key_enc,
                 1 if enabled else 0, now, now),
            )
            conn.commit()
        finally:
            conn.close()
    return get_account(account_id)


def update_account(account_id: str, **fields) -> Optional[Dict[str, Any]]:
    allowed = {"nickname", "login_enc", "password_enc", "api_key_enc", "enabled"}
    sets = []
    vals = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "enabled":
            v = 1 if v else 0
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return get_account(account_id)
    sets.append("updated_at=?")
    vals.append(datetime.now(timezone.utc).isoformat())
    vals.append(account_id)
    with _lock:
        conn = _connect()
        try:
            conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", vals)
            conn.commit()
        finally:
            conn.close()
    return get_account(account_id)


def delete_account(account_id: str) -> bool:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


# ---- runs / results / steps ----
def create_run(mode: str, business_date: str) -> str:
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO runs (id, mode, status, business_date, created_at)"
                " VALUES (?,?,?,?,?)",
                (run_id, mode, "running", business_date, now),
            )
            conn.commit()
        finally:
            conn.close()
    return run_id


def finish_run(run_id: str, status: str, error: Optional[str] = None,
               total: int = 0, succeeded: int = 0, failed: int = 0) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE runs SET status=?, completed_at=?, error=?, total=?,"
                " succeeded=?, failed=? WHERE id=?",
                (status, now, error, total, succeeded, failed, run_id),
            )
            conn.commit()
        finally:
            conn.close()


def add_result(run_id: str, account_id: str, site_key: str, account_label: str,
               business_date: str, status: str, conclusion: Optional[str] = None,
               metric_label: Optional[str] = None, metric_value: Any = None,
               streak_days: Optional[int] = None, note: Optional[str] = None) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO results (run_id, account_id, site_key, account_label,"
                " business_date, status, conclusion, metric_label, metric_value,"
                " streak_days, note, completed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, account_id, site_key, account_label, business_date, status,
                 conclusion, metric_label,
                 None if metric_value is None else str(metric_value),
                 streak_days, note, now),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def add_steps(run_id: str, events: List[Dict[str, Any]]) -> None:
    """events: [{'ts','site_key','message'}]"""
    if not events:
        return
    with _lock:
        conn = _connect()
        try:
            conn.executemany(
                "INSERT INTO steps (run_id, ts, site_key, message) VALUES (?,?,?,?)",
                [(run_id, e.get("ts"), e.get("site_key"), e.get("message")) for e in events],
            )
            conn.commit()
        finally:
            conn.close()


def get_steps_after(run_id: str, after_id: int = 0) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, ts, site_key, message FROM steps WHERE run_id=? AND id > ? ORDER BY id ASC",
            (run_id, after_id),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        run = _row_to_dict(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        if not run:
            return None
        steps = [_row_to_dict(r) for r in conn.execute(
            "SELECT id, ts, site_key, message FROM steps WHERE run_id=? ORDER BY id", (run_id,))]
        results = [_row_to_dict(r) for r in conn.execute(
            "SELECT * FROM results WHERE run_id=? ORDER BY id", (run_id,))]
        run["steps"] = steps
        run["results"] = results
        return run
    finally:
        conn.close()


def list_runs(limit: int = 20, offset: int = 0, status: Optional[str] = None,
              mode: Optional[str] = None) -> Tuple[List[Dict[str, Any]], int]:
    conn = _connect()
    try:
        query = "SELECT * FROM runs"
        count_query = "SELECT COUNT(*) FROM runs"
        params = []
        where = []
        if status:
            where.append("status=?")
            params.append(status)
        if mode:
            where.append("mode=?")
            params.append(mode)
        if where:
            where_sql = " WHERE " + " AND ".join(where)
            query += where_sql
            count_query += where_sql
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        total = conn.execute(count_query, params).fetchone()[0]
        params.extend([limit, offset])
        rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows], total
    finally:
        conn.close()


def scheduled_run_done_today(business_date: str) -> bool:
    """今天（北京时间）是否已经自动跑过一次（成功或失败都算）。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM runs WHERE mode='scheduled'"
            " AND status IN ('completed', 'failed')"
            " AND business_date=? LIMIT 1",
            (business_date,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def latest_results_by_account(business_date: str) -> Dict[str, Dict[str, Any]]:
    """今天每个账号的最新一条结果，用于今日状态展示。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT r.* FROM results r"
            " INNER JOIN (SELECT account_id, MAX(id) AS mid FROM results"
            " WHERE business_date=? GROUP BY account_id) m ON m.mid=r.id",
            (business_date,),
        ).fetchall()
        return {r["account_id"]: _row_to_dict(r) for r in rows}
    finally:
        conn.close()


def get_summary_stats(business_date: str) -> Dict[str, int]:
    """返回看板核心概览统计数据。"""
    accounts = list_accounts()
    total_accounts = len(accounts)
    enabled_accounts = sum(1 for a in accounts if a["enabled"])
    today_map = latest_results_by_account(business_date)
    
    today_success = 0
    today_failed = 0
    for a in accounts:
        if not a["enabled"]:
            continue
        res = today_map.get(a["id"])
        if res:
            if res["status"] == "success":
                today_success += 1
            elif res["status"] in ("failed", "cancelled"):
                today_failed += 1
    today_pending = max(0, enabled_accounts - today_success - today_failed)

    return {
        "totalAccounts": total_accounts,
        "enabledAccounts": enabled_accounts,
        "todaySuccess": today_success,
        "todayFailed": today_failed,
        "todayPending": today_pending,
    }


def clean_old_records(days_to_keep: int = 30, max_runs_to_keep: int = 200) -> None:
    """清理历史运行与步骤数据，防止数据库体积无限制增长。"""
    with _lock:
        conn = _connect()
        try:
            cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days_to_keep)).isoformat()
            # 获取需要保留的最新 runs ID
            preserved = conn.execute(
                "SELECT id FROM runs ORDER BY created_at DESC LIMIT ?",
                (max_runs_to_keep,),
            ).fetchall()
            preserved_ids = [r["id"] for r in preserved]
            if preserved_ids:
                placeholders = ",".join(["?"] * len(preserved_ids))
                # 删除超出保留数量且超过天数的 steps
                conn.execute(
                    f"DELETE FROM steps WHERE run_id NOT IN ({placeholders}) AND ts < ?",
                    (*preserved_ids, cutoff_date),
                )
                conn.commit()
        except Exception:
            pass
        finally:
            conn.close()
