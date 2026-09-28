# 签到控制中心（Checkin Center）

多站点每日自动签到的轻量级本地控制面板：多账号管理、自动定时调度、手动按需签到、SSE 实时过程时间线、多渠道通知推送、历史记录归档与 CSV 导出。
凭据经 Fernet 本地加密后安全存入 SQLite，接口与 Web 前端永不回显凭证明文。

支持站点：

| 站点 key | 站点名 | 凭据需求 | 备注 |
|---|---|---|---|
| `jiaobenwang` | 脚本王 | 登录名/邮箱 + 密码 | 每日赚取贡献分 |
| `pikaqiu` | 皮卡丘token商店 | 登录邮箱 + 密码 | 连签天数与余额累计 |
| `ebondai` | ebondai | 登录邮箱 + 密码 + API Key | 每日保活调用与福利余额兑换 |

---

## 🌟 核心特性与优化

1. **安全第一**：凭据由 Fernet 密钥对称加密入库；支持解密容错隔离；支持可选的 Web 访问密码保护；接口添加安全响应头。
2. **动态插件架构**：后端提供 `/api/sites` 动态元数据，前端表单自动根据站点字段定义渲染，新增站点零侵入。
3. **实时日志流（SSE）**：基于 Server-Sent Events 实现无延迟实时日志时间线，支持手动终止（Cancel）运行中的任务。
4. **多渠道通知分发**：支持 Telegram Bot、Bark (iOS)、Server酱、PushPlus、飞书、钉钉、企业微信机器人及自定义 Webhook 异步推送。
5. **看板数据总览**：账号总数、今日已签、今日失败、今日待签一目了然；支持账号按站点筛选、模糊搜索、一键全选与一键重试失败。
6. **历史记录与导出**：支持分页浏览、运行状态筛选、一键导出今日结果为 UTF-8 BOM CSV。
7. **深色模式**：自动适配系统外观偏好，并支持手动一键切换深色/浅色主题。
8. **自动化测试与容器化**：内置 20+ 个 Pytest 自动化测试用例；提供开箱即用的 Dockerfile 与 docker-compose.yml。

---

## 快速开始

### 方式 A：本地直接运行

```bash
cd checkin-center
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. 生成加密密钥（必填）
python -m app.crypto   # 复制输出的 32 位 Fernet Base64 密钥

# 2. 配置 .env
cp .env.example .env
# 将上一步生成的密钥填入 CHECKIN_FERNET_KEY

# 3. 启动服务（会自动加载 .env）
uvicorn app.main:app --host 127.0.0.1 --port 8000
# 浏览器访问 http://127.0.0.1:8000
```

### 方式 B：Docker 一键部署

```bash
# 1. 生成密钥并写入 .env
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
cp .env.example .env
# 在 .env 中填入 CHECKIN_FERNET_KEY

# 2. 启动容器
docker compose up -d
```

---

## 环境变量配置说明（`.env`）

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `CHECKIN_FERNET_KEY` | **必填** | 凭据对称加密密钥（通过 `python -m app.crypto` 生成） |
| `CHECKIN_WEB_PASSWORD` | 空 | 可选：控制面板访问密码（留空则不开启密码验证） |
| `CHECKIN_HOST` | `127.0.0.1` | 监听地址 |
| `CHECKIN_PORT` | `8000` | 监听端口 |
| `CHECKIN_TIMEZONE` | `Asia/Shanghai`| 业务时区（默认北京时间） |
| `CHECKIN_SCRIPT_TIMEOUT`| `300` | 脚本单次执行超时上限（秒） |
| `CHECKIN_RETRY_DELAY` | `5` | 发生网络异常时的退避重试间隔（秒） |
| `NOTIFY_ENABLED` | `false` | 是否开启签到汇总通知推送 |
| `TELEGRAM_BOT_TOKEN` | 空 | Telegram Bot Token |
| `TELEGRAM_CHAT_ID` | 空 | Telegram Chat ID |
| `BARK_URL` | 空 | Bark 推送地址或 Device Key |
| `SERVERCHAN_KEY` | 空 | Server酱 Turbo SendKey |
| `PUSHPLUS_TOKEN` | 空 | PushPlus (推送加) Token |
| `FEISHU_WEBHOOK` | 空 | 飞书群机器人 Webhook |
| `DINGTALK_WEBHOOK` | 空 | 钉钉群机器人 Webhook |
| `WECOM_WEBHOOK` | 空 | 企业微信群机器人 Webhook |
| `CUSTOM_WEBHOOK_URL` | 空 | 自定义 HTTP POST Webhook 接口 |

---

## 自动化测试

项目内置完整的 Pytest 自动化测试套件：

```bash
pytest -v
```

---

## 目录结构

```
checkin-center/
├── app/
│   ├── main.py        # FastAPI：路由、中间件、鉴权、SSE 流与 CSV 导出
│   ├── config.py      # 环境变量与站点元数据定义（自动加载 .env）
│   ├── db.py          # SQLite 存取：WAL 模式、忙等待、索引优化与数据清理
│   ├── crypto.py      # Fernet 加解密、容错安全解密与脱敏函数
│   ├── runner.py      # 签到执行引擎：子进程管理、任务终止、失败重试
│   ├── scheduler.py   # 定时调度：自动签到与过点补跑
│   ├── notifier.py    # 多渠道消息推送分发（Telegram/Bark/飞书/钉钉等）
│   ├── sheets.py      # Google 表格记录（可选）
│   ├── timeutil.py    # 北京时间工具
│   ├── scripts/       # 各站点签到脚本
│   └── static/        # 前端单页（HTML / CSS / JS，支持深色模式与 SSE）
├── tests/             # 自动化测试用例
├── Dockerfile         # Docker 构建镜像文件
├── docker-compose.yml # Docker Compose 编排文件
├── requirements.txt   # 依赖列表
├── pyproject.toml     # Pytest 与工具配置
├── .env.example       # 环境变量配置模板
└── README.md
```
