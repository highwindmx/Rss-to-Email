# Changelog

## 2026-08-02 — 触发方式子窗格化 + 每日多时点独立检查窗口

本批次围绕「触发方式」前端重组与检查窗口隔离，配套后端字段、单测与文档。

### 前端（`static/index.html`）
- **子窗格化**：原独立的「轮询与检查窗口」卡片并入「触发方式」卡片，作为随模式切换的子窗格：
  - 选 `interval` → 显示「固定间隔设置」子窗格（轮询间隔 + 检查窗口）
  - 选 `fixed_times` → 显示「每日多时点设置」子窗格（时点列表 + 独立的检查窗口输入）
  - 新增 `.subpane` / `.subpane-title` 样式做视觉区分。
- **每日多时点独立检查窗口**：多时点子窗格内新增「检查窗口（小时）」输入，与轮询设置隔离。
- **占位提示**：多时点检查窗口输入框加动态 `placeholder` 与说明文字「当前 N 小时」，随固定间隔的 `CHECK_HOURS` 实时联动（新增 `updateFixedTimesHint()` + `CHECK_HOURS` 输入监听 + `#ftCheckHint`），明确「未设置则沿用固定间隔」。
- **按钮样式**：时点删除按钮由「删除」文字改为 `✕` 并居中；订阅源「测」「✕」按钮文字在按钮内居中（`.row-btn` 改为 `inline-flex` 居中）。
- **告警收敛**：「检查窗口 < 轮询间隔」黄色告警条仅固定间隔模式生效。

### 后端（`lib/rss_mailer.py` / `app.py`）
- `load_config` 新增 `CHECK_HOURS_FIXED_TIMES`：未单独设置时回落到 `CHECK_HOURS`（向后兼容）。
- `run_once` 按 `SCHEDULE_MODE` 选择检查窗口：`fixed_times` 用 `CHECK_HOURS_FIXED_TIMES`，`interval` 用 `CHECK_HOURS`，实现两种模式隔离。
- `app.py` 的 `ALLOWED` 增加 `CHECK_HOURS_FIXED_TIMES`，前端该字段可正常保存/回传。

### 测试（`lib/test_core.py`）
- 新增 4 个用例：load_config 隔离/回落、run_once 按模式选检查窗口（`fixed_times`→72 / `interval`→24）。`pytest` 全量 **28 passed**。

### 文档（`README.md`）
- 功能/配置/说明章节补充两种触发方式各自独立的检查窗口、`CHECK_HOURS_FIXED_TIMES` 说明与回落提示。
- 目录结构补 `lib/test_core.py`；新增「测试」章节给出 `pytest` 运行命令。

### 运维提示
- 后端改动需重启 web 服务生效（前端为静态文件，刷新浏览器即生效）。
- 多时点检查窗口留空不会报错，安全回落到固定间隔的检查窗口；前端已用动态提示消除「悄悄沿用陌生值」的隐患。

---

## 历史提交摘要（本批次之前，同项目）
- `527bc54` feat: 新增「每日多时点」触发方式（与固定间隔二选一）
- `ae1752c` feat: 运行日志新增服务启动/重启记录
- `7df9c9d` fix: 心跳 skip 记录间隔稳定为 10 分钟
- `0c64104` feat: 邮件末尾追加落款「此致 祝好」
- `78d70b4` fix: /api/restart 多进程抢端口缺陷修复
- `78d0cfd` feat: 上次发送时间改为实际发信时间 + 前端错误可视化
- `cb57839` fix: 三个 LOW 风险（bat 端口覆盖 / 离线判定 / 单源重复 guid 去重）
- `3875056` feat: RSS2Email 优化迭代落地（脱敏 / 超时 / 并发 / 类型容错 / 首跑窗口 / pytest / 端口单来源）
