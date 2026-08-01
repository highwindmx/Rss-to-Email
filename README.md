# RSS-to-Email

本地 RSS 聚合推送工具：定时抓取多个 RSS 源的新条目，按源分组、去重后通过邮件发送到指定邮箱。自带网页管理界面。

## 功能

- 多 RSS 源订阅，每个源可配置「源标题」（出现在邮件 `【】` 前缀里）
- 网页前端：配置订阅源 / 轮询间隔 / 邮箱，单独或批量测试抓取，查看运行状态与历史记录
- 自节流调度：APScheduler 每分钟 tick，未到轮询间隔则跳过本次
- 去重：按条目 guid 落库，避免重复推送
- 邮件格式：主题 `RSS 更新 YYYY-MM-DD HH:MM（N条）`；正文每条 `N. 【源标题】 条目标题` + 摘要（≤100 字）

## 目录结构

```
.
├── app.py              # Web 服务入口（Flask），start_web.bat 启动它
├── lib/
│   ├── rss_mailer.py   # 核心引擎：配置读写 / 抓取 / 去重 / 发信
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
- `POLL_INTERVAL_MINUTES`：轮询间隔（分钟）
- `CHECK_HOURS`：只推送最近 N 小时内的条目，更早的直接标为已处理
- `SMTP_HOST` / `SMTP_PORT` / `SENDER_EMAIL` / `SMTP_AUTH_CODE`：发件邮箱（用授权码，不是登录密码）
- `RECIPIENTS`：目标邮箱，多个用逗号分隔

## 运行

双击 `start_web.bat`，浏览器自动打开 http://127.0.0.1:50000 。

> 注意：改了 `lib/rss_mailer.py`（后端逻辑）后需重启服务；改 `static/` 下的前端只需刷新浏览器。

## 说明

- 无公网、无 WebSub 订阅，采用高频轮询近似实时推送。
- `CHECK_HOURS` 建议 ≥ 轮询间隔，否则两次检查之间发布、待下次检查时已超窗口的条目会被永久标为「已读」而漏发。
- `.env` 含凭据，已被 `.gitignore` 屏蔽，**请勿提交**。
