# 签到控制中心（standalone 版）

多站点每日签到的本地控制面板：账号管理、自动签到、手动签到、实时过程时间线、历史记录。
凭据用 Fernet 加密后存本地 SQLite，接口与界面永不返回明文。

支持的站点：

| 站点 key | 站点名 | 凭据 |
|---|---|---|
| `jiaobenwang` | 脚本王 | 登录邮箱 + 密码 |
| `pikaqiu` | 皮卡丘token商店 | 登录邮箱 + 密码 |
| `ebondai` | ebondai | 登录邮箱 + 密码 + API Key |

签到执行复用 `app/scripts/` 下的三个脚本（`--json` / `--progress-file` 接口保持不变）。

## 快速开始

```bash
cd checkin-center
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. 生成加密密钥（必填）
python -m app.crypto   # 复制输出

# 2. 写 .env
cp .env.example .env
# 把上一步的密钥填入 CHECKIN_FERNET_KEY

# 3. 启动
uvicorn app.main:app --host 127.0.0.1 --port 8000
# 浏览器打开 http://127.0.0.1:8000
```

> 没有 `CHECKIN_FERNET_KEY` 时服务拒绝启动，避免凭据以明文落盘。
> 也可以不用 `.env`，直接用环境变量：`CHECKIN_FERNET_KEY=... uvicorn app.main:app`。

## 凭据配置

- 在面板「账号管理」里添加账号：选站点、填备注名、登录邮箱/密码（ebondai 还要填 API Key）。
- 账号列表只显示脱敏标识（如 `5***@qq.com`），编辑时不回填旧密码，留空表示不更换。
- 停用账号：不再参与自动签到，手动签到时置灰不可选。
- 删除账号：前端二次确认；凭据彻底删除，历史记录中的账号标签保留。
- 加密密钥更换后，旧数据库里的凭据将无法解密（需重新录入）。**密钥请妥善备份。**

## 自动签到

面板「自动签到」卡片：开关 + 北京时间 `HH:MM`。

- 内置调度器每分钟检查一次：
  - 未开启 / 还没到点 / 今天已自动跑过 → 跳过；
  - 到了时间且今天没跑过 → 执行；如果错过时间点，下一次检查时补跑。
- 改时间或开关后 1 分钟内生效，无需重启。
- 「立即执行一次」：对全部已启用账号立刻跑一次（走自动签到流程）。
- 网络异常时单个账号约 60 秒后重试一次；仍失败则该账号记为失败，不影响其他账号。

## Google 表格记录（可选，默认关闭）

每次签到完成后，可把「日期 / 站点 / 运行状态 / 签到结论 / 账户余额 / 连签天数 / 备注」追加到 Google 表格。

1. 在 Google Cloud 建 service account，启用 Google Sheets API，下载 JSON 密钥；
2. 把表格分享给该 service account 的邮箱（编辑者权限）；
3. 安装依赖：`pip install google-api-python-client google-auth`；
4. `.env` 中配置：
   ```
   SHEETS_ENABLED=true
   SHEETS_SPREADSHEET_ID=表格 ID（URL 中 /d/ 后面的那串）
   SHEETS_WORKSHEET=签到记录
   SHEETS_CREDENTIALS=/path/to/service-account.json
   ```
5. 重启服务。

表格写入失败只记日志，不影响签到主流程。

## 部署（systemd 示例）

```ini
# /etc/systemd/system/checkin-center.service
[Unit]
Description=Checkin Center
After=network.target

[Service]
User=checkin
WorkingDirectory=/opt/checkin-center
EnvironmentFile=/opt/checkin-center/.env
ExecStart=/opt/checkin-center/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now checkin-center
```

需要公网访问时，建议前面加 Nginx 反向代理并配 HTTPS；不要把 8000 端口直接暴露到公网。

## 安全说明

- 凭据加密存 SQLite（`data/checkin.db`），密钥只走环境变量；
- API 与前端永不返回凭证明文；
- 签到运行时会生成 600 权限的临时凭据文件供脚本读取，跑完立即删除；
- `data/`（数据库、会话、进度文件）与 `.env` 都在 `.gitignore` 中，不会随仓库发布。

## 目录结构

```
checkin-center/
├── app/
│   ├── main.py        # FastAPI：路由 + 生命周期
│   ├── config.py      # 环境变量配置
│   ├── db.py          # SQLite 存取
│   ├── crypto.py      # Fernet 加解密 + 脱敏（python -m app.crypto 生成密钥）
│   ├── runner.py      # 签到执行引擎（子进程跑脚本、实时进度、失败重试）
│   ├── scheduler.py   # 内置调度（每分钟检查）
│   ├── sheets.py      # Google 表格记录（可选）
│   ├── timeutil.py    # 北京时间工具
│   ├── scripts/       # 三个签到脚本（--json / --progress-file）
│   └── static/        # 前端单页（index.html / style.css / app.js）
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```
