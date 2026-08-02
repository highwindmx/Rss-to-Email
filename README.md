# RSS-to-Email

本地 RSS 聚合推送工具：定时抓取多个 RSS 源的新条目，每条前加【源标题】前缀、去重后通过邮件发送到指定邮箱。自带网页管理界面。

## 功能

- 多 RSS 源订阅，每个源可配置「源标题」（出现在邮件 `【】` 前缀里）
- 网页前端：配置订阅源 / 轮询间隔 / 邮箱，单独或批量测试抓取，查看运行状态与历史记录
- 前端实时显示后端在线 / 离线状态；后端离线时自动禁用「立即运行 / 重启服务 / 停止服务」操作
- 自节流调度：APScheduler 每分钟 tick，未到轮询间隔则跳过本次
- **两种触发方式（二选一，各自独立的检查窗口）**：前端「触发方式」卡片内按所选模式显示对应子窗格
  - `interval`（默认）：按 `POLL_INTERVAL_MINUTES` 固定间隔轮询；检查窗口用 `CHECK_HOURS`
  - `fixed_times`：在 `SCHEDULE_TIMES` 指定的多个时刻（如 `08:00,12:00,20:00`）强制抓取推送；若服务刚好宕机错过某个时点，恢复后会自动补跑一次；检查窗口用独立的 `CHECK_HOURS_FIXED_TIMES`，与 interval 模式互不干扰
- 去重：按条目 guid 落库，避免重复推送
- 邮件格式：主题 `RSS 更新 YYYY-MM-DD HH:MM（N条）`；正文每条 `N. 【源标题】 条目标题` + 摘要（≤100 字）
- 抓取带超时（连接 5s / 读取 `FETCH_TIMEOUT` 默认 15s），多源并发抓取，避免单源挂起阻塞整轮

## 目录结构

```
.
├── app.py              # Web 服务入口（Flask），start_web.bat 启动它
├── lib/
│   ├── rss_mailer.py   # 核心引擎：配置读写 / 抓取 / 去重 / 发信
│   ├── test_core.py    # 单元测试（pytest）：配置解析 / 调度 / 去重 / 检查窗口隔离等
│   └── test_fetch.py   # 只读测试：打印各 RSS 源解析结果
├── static/             # 前端页面 index.html
├── start_web.bat       # Windows 一键启动（自愈式：清旧进程 / 轮询就绪 / 开浏览器）
├── .env.example        # 配置模板（复制为 .env 后填写）
├── requirements.txt
└── rss_state.db        # 运行时状态（自动生成，已被 .gitignore 屏蔽）
```

## 安装

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## 配置

复制 `.env.example` 为 `.env` 并填写：

- `RSS_URLS`：多个源用逗号分隔，格式 `标题|url`（标题可空；留空则邮件回退用频道自身标题）
- `POLL_INTERVAL_MINUTES`：轮询间隔（分钟），`SCHEDULE_MODE=interval` 时生效
- `SCHEDULE_MODE`：触发方式，`interval`（默认，固定间隔）或 `fixed_times`（每日多时点）
- `SCHEDULE_TIMES`：多时点模式的定时时刻，逗号分隔 `HH:MM`（24 小时制），如 `08:00,12:00,20:00`；仅 `SCHEDULE_MODE=fixed_times` 生效
- `CHECK_HOURS`：固定间隔（`interval`）模式下，只推送最近 N 小时内的条目，更早的直接标为已处理
- `CHECK_HOURS_FIXED_TIMES`：仅 `SCHEDULE_MODE=fixed_times` 时生效，作用同上但**独立于** `CHECK_HOURS`；未设置则回落沿用 `CHECK_HOURS`（前端多时点检查窗口输入框会动态提示当前的回落值）
- `SMTP_HOST` / `SMTP_PORT` / `SENDER_EMAIL` / `SMTP_AUTH_CODE`：发件邮箱（用授权码，不是登录密码）
- `RECIPIENTS`：目标邮箱，多个用逗号分隔

## 运行

双击 `start_web.bat`，浏览器自动打开 http://127.0.0.1:50000 。

> 支持无窗口（后台静默）模式：运行 `start_web.bat --hidden`，服务以 `pythonw` 后台启动、不弹控制台窗口。
> 端口可通过环境变量 `WEB_PORT` 覆盖（start_web.bat 与 app.py 共用同一来源）。

> 注意：改了 `lib/rss_mailer.py`（后端逻辑）后需重启服务；改 `static/` 下的前端只需刷新浏览器。

## 测试

核心逻辑有 `lib/test_core.py` 覆盖（配置解析、调度、去重、检查窗口隔离等，网络全部 mock）：

```bash
.venv\Scripts\python -m pytest lib/test_core.py -q
```

## 说明

- 无公网、无 WebSub 订阅，采用高频轮询近似实时推送。
- `CHECK_HOURS`（`interval` 模式）建议 ≥ 轮询间隔，否则两次检查之间发布、待下次检查时已超窗口的条目会被永久标为「已读」而漏发（前端在检查窗口小于轮询间隔时会显示黄色告警提示）。`fixed_times` 模式使用独立的 `CHECK_HOURS_FIXED_TIMES`，其「检查窗口 ≥ 相邻时点间隔」同理，避免相邻时点间发布的条目被漏掉。
- 首次运行采用更大时间窗口（至少 7 天），避免一次性把历史积压全部标为「已读」。
- `.env` 含凭据，已被 `.gitignore` 屏蔽，**请勿提交**。前端配置接口对 `SMTP_AUTH_CODE` 做了脱敏：GET 不回传明文、POST 留空表示不修改。

Powered by WorkBuddy
