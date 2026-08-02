import feedparser, sqlite3, smtplib, ssl, time, os, re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

HERE = Path(__file__).parent
ROOT = HERE.parent  # 项目根目录（本文件位于 lib/ 下，.env 与 rss_state.db 都在根）
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

BASE = ROOT
DB_PATH = os.environ.get("RSS_STATE_DB", str(BASE / "rss_state.db"))


# ---------- 通用工具 ----------
def _safe_int(value, default=0):
    """把环境变量等可能非整型的输入安全地转为 int。

    - None / 空串 / 仅空白  → 返回 default
    - 无法解析为整数的字符串（如 "abc"、"3.5"） → 返回 default
    - 否则返回 int(str(value).strip())
    """
    if value is None:
        return default
    s = str(value).strip()
    if not s:
        return default
    try:
        return int(s)
    except (ValueError, TypeError):
        return default


# ---------- 配置读写 ----------
def _parse_feeds(raw):
    """把 .env 里的 RSS_URLS 解析为 [{title,url}]。
    支持两种写法：纯 url，或 `标题|url`（以首个 | 分隔）。无 | 视为 url-only、标题为空。"""
    feeds = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "|" in part:
            title, url = part.split("|", 1)
            feeds.append({"title": title.strip(), "url": url.strip()})
        else:
            feeds.append({"title": "", "url": part})
    return feeds


def load_config():
    # 每次都从磁盘重新加载 .env，避免「直接改 .env 后不重启服务就看不到更新」的困惑
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=True)
    except Exception:
        pass
    raw = os.environ.get("RSS_URLS") or os.environ.get("RSS_URL", "")
    feeds = _parse_feeds(raw)
    urls = [f["url"] for f in feeds]
    return {
        "RSS_URLS": raw,
        "feeds": feeds,
        "urls": urls,
        "POLL_INTERVAL_MINUTES": _safe_int(os.environ.get("POLL_INTERVAL_MINUTES", 60), 60),
        "SMTP_HOST": os.environ.get("SMTP_HOST", ""),
        "SMTP_PORT": _safe_int(os.environ.get("SMTP_PORT", 465), 465),
        "SENDER_EMAIL": os.environ.get("SENDER_EMAIL", ""),
        "SMTP_AUTH_CODE": os.environ.get("SMTP_AUTH_CODE", ""),
        "RECIPIENTS": os.environ.get("RECIPIENTS", ""),
        "CHECK_HOURS": _safe_int(os.environ.get("CHECK_HOURS", 24), 24),
    }


def public_config():
    """对外（前端）配置：剔除敏感字段 SMTP_AUTH_CODE，并以布尔标记提示其是否已设置。

    供 GET / POST /api/config 使用，避免把授权码明文回传前端。
    """
    cfg = load_config()
    raw = cfg.pop("SMTP_AUTH_CODE", "")
    cfg["has_smtp_auth_code"] = bool(raw)
    return cfg


def set_config(updates: dict):
    """更新 .env 中指定 key（保留其他行与注释），并 reload 环境变量。"""
    env_path = BASE / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    seen = set()
    out = []
    for ln in lines:
        replaced = False
        for k, v in updates.items():
            if ln.split("=", 1)[0].strip() == k:
                out.append(f"{k}={v}")
                seen.add(k)
                replaced = True
                break
        if not replaced:
            out.append(ln)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    load_dotenv(env_path, override=True)


# ---------- 持久化（去重 + 运行历史）----------
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS sent (guid TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute("""CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, type TEXT,
        count INT, status TEXT, detail TEXT)""")
    return conn


# 运行记录瘦身：跳过类（心跳）记录最多每 10 分钟写一条，避免 runs 表被每分钟的 skip 刷屏。
# 真正有意义的记录（已发送 / 无新条目 / 错误 / 测试）不受影响，照常写入。
RUN_LOG_MINUTES = 10


def _should_log_skip():
    """距上一条 runs 记录 ≥ RUN_LOG_MINUTES 分钟才允许写 skip（心跳）记录。"""
    conn = _conn()
    row = conn.execute("SELECT ts FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    if not row:
        return True
    return (time.time() - float(row[0])) >= RUN_LOG_MINUTES * 60


def log_run(rtype, count, status, detail):
    conn = _conn()
    conn.execute("INSERT INTO runs(ts,type,count,status,detail) VALUES (?,?,?,?,?)",
                 (time.time(), rtype, count, status, detail))
    conn.commit()
    conn.close()


def get_status():
    conn = _conn()
    row = conn.execute("SELECT v FROM meta WHERE k='last_run'").fetchone()
    sent_total = conn.execute("SELECT COUNT(*) FROM sent").fetchone()[0]
    runs = conn.execute(
        "SELECT ts,type,count,status,detail FROM runs ORDER BY id DESC LIMIT 20").fetchall()
    conn.close()
    try:
        poll = _safe_int(load_config().get("POLL_INTERVAL_MINUTES", 60), 60)
    except (TypeError, ValueError):
        poll = 60
    return {
        "last_run": float(row[0]) if row else None,
        "sent_total": sent_total,
        "poll_interval_minutes": poll,
        "runs": [{"ts": r[0], "type": r[1], "count": r[2], "status": r[3], "detail": r[4]}
                 for r in runs],
    }


# ---------- 自节流 ----------
def should_run():
    conn = _conn()
    row = conn.execute("SELECT v FROM meta WHERE k='last_run'").fetchone()
    conn.close()
    poll = _safe_int(os.environ.get("POLL_INTERVAL_MINUTES", 60), 60)
    if not row:
        return True
    return (time.time() - float(row[0])) >= poll * 60


def mark_run():
    conn = _conn()
    conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES ('last_run', ?)", (str(time.time()),))
    conn.commit()
    conn.close()


# ---------- 抓取 ----------
def _fetch_feed(url, timeout=None):
    """带超时地抓取 RSS/Atom，返回 feedparser 解析结果。

    - timeout 为 None 时，使用 (连接 5s, 读取 FETCH_TIMEOUT 默认 15s) 的元组超时。
    - 网络/解析异常向上抛，由调用方 try 处理（不在此吞异常）。
    """
    import requests

    if timeout is None:
        timeout = (5, _safe_int(os.environ.get("FETCH_TIMEOUT"), 15))
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "RSS2Email/1.0"})
    resp.raise_for_status()
    return feedparser.parse(resp.content)


def _is_first_run():
    """判断是否为首次运行：meta 无 last_run 或 sent 表为空 → True。

    首跑用更大时间窗口，避免一次性把历史积压全部标为「已读」导致漏发/多发。
    """
    conn = _conn()
    row = conn.execute("SELECT v FROM meta WHERE k='last_run'").fetchone()
    sent_total = conn.execute("SELECT COUNT(*) FROM sent").fetchone()[0]
    conn.close()
    return (not row) or (sent_total == 0)


def _fetch_one(feed, cutoff, first_run):
    """并发 worker：抓取单个源，按 guid 去重 + 时间窗口筛出新增。

    - 已 in sent 的 guid → 计入 old（由主线程回写 sent）。
    - 发布时间早于 cutoff（窗口外）→ 计入 old。
    - 无发布时间(pub 为 None)的条目：作为 new 候选（不计入 old/sent），
      待 run_once 发送后再落库，避免未发送就被误标为已读。
    - 单源异常 try 吞掉返回空，避免拖垮整轮。
    """
    url = feed.get("url", "")
    if not url:
        return {"new": [], "old": []}
    try:
        fp = _fetch_feed(url)
        ch_title = (fp.feed or {}).get("title", "")
        src_title = feed.get("title", "") or ch_title
        # 各 worker 使用独立连接读取 sent 集合（SQLite 连接非线程安全）
        conn = _conn()
        sent = {r[0] for r in conn.execute("SELECT guid FROM sent")}
        conn.close()
        new, old = [], []
        for e in fp.entries:
            guid = e.get("id") or e.get("link")
            pub = e.get("published_parsed") or e.get("updated_parsed")
            pub_ts = time.mktime(pub) if pub else None
            if guid in sent:
                old.append(guid)
                continue
            if pub_ts is not None and pub_ts < cutoff:
                old.append(guid)
                continue
            # 无发布时间或仍在窗口内 → 作 new 候选（不在此处入库 sent）
            new.append({"e": e, "title": src_title})
        return {"new": new, "old": old}
    except Exception:
        # 单源异常吞掉，返回空，不拖垮整轮
        return {"new": [], "old": []}


def fetch_new(feeds, check_hours):
    """并发抓取所有源的新条目。

    - 读取 sent 集合、判定首跑。
    - 首跑窗口 = max(check_hours, 168) 小时（至少 7 天）；非首跑用 check_hours。
    - 用 ThreadPoolExecutor 并发抓取；worker 仅读取 sent，不写库。
    - 主线程汇总 new/old，并把 old(guid) 回写 sent（SQLite 连接非线程安全，落库回主线程）。
    """
    first_run = _is_first_run()
    window_hours = max(check_hours, 168) if first_run else check_hours
    cutoff = time.time() - window_hours * 3600
    new, old = [], []
    if not feeds:
        return new
    with ThreadPoolExecutor(max_workers=min(len(feeds), 8)) as ex:
        futures = [ex.submit(_fetch_one, f, cutoff, first_run) for f in feeds]
        for fu in futures:
            try:
                res = fu.result()
            except Exception:
                res = {"new": [], "old": []}
            new.extend(res.get("new", []))
            old.extend(res.get("old", []))
    # 落库回主线程（SQLite 连接非线程安全）
    if old:
        conn = _conn()
        conn.executemany("INSERT OR IGNORE INTO sent(guid) VALUES (?)",
                         [(g,) for g in old])
        conn.commit()
        conn.close()
    return new


def _strip_tags(s):
    """去掉 HTML 标签，得到纯文本。"""
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _esc(s):
    """HTML 转义，避免标题/摘要中的特殊字符破坏邮件结构。"""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _summary(s, n=100):
    """纯文本摘要，最多 n 个字符，超出截断并补省略号。"""
    t = _strip_tags(s)
    return t if len(t) <= n else t[:n] + "…"


def send(items, cfg):
    recipients = [x.strip() for x in cfg["RECIPIENTS"].split(",") if x.strip()]
    rows = []
    for idx, it in enumerate(items, 1):
        e = it["e"]
        src = it.get("title", "")
        title = _esc(_strip_tags(e.get("title", "(无标题)")))
        summary = _esc(_summary(e.get("summary", ""), 100))
        link = e.get("link", "#")
        src_html = (f'<span style="color:#888;font-size:12px;margin-right:6px">'
                    f'【{_esc(src)}】</span>') if src else ""
        rows.append(
            '<li style="margin-bottom:14px">'
            '<div style="font:600 15px -apple-system,Segoe UI,Microsoft YaHei,sans-serif;color:#111">'
            f'<span style="color:#2f6fed;margin-right:8px">{idx}.</span>'
            f'{src_html}'
            f'<a href="{link}" style="color:#111;text-decoration:none">{title}</a></div>'
            f'<div style="color:#555;font-size:13px;margin:4px 0 0 26px;line-height:1.5">{summary}</div>'
            '</li>'
        )
    body = (
        '<div style="font-family:-apple-system,Segoe UI,Microsoft YaHei,sans-serif">'
        '<ol style="padding-left:0;list-style:none;margin:0">' + "".join(rows) + '</ol>'
        '</div>'
    )
    msg = MIMEMultipart()
    msg["From"] = cfg["SENDER_EMAIL"]
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = f"RSS 更新 {datetime.now():%Y-%m-%d %H:%M}（{len(items)} 条）"
    msg.attach(MIMEText(body, "html"))
    with smtplib.SMTP_SSL(cfg["SMTP_HOST"], int(cfg["SMTP_PORT"]),
                          context=ssl.create_default_context()) as s:
        s.login(cfg["SENDER_EMAIL"], cfg["SMTP_AUTH_CODE"])
        s.send_message(msg, to_addrs=recipients)
    print(f"已发送 {len(items)} 条 -> {recipients}")


def run_once(force=False):
    cfg = load_config()
    try:
        if not cfg["urls"] or "example.com" in cfg["RSS_URLS"] or "your_qq" in cfg["SENDER_EMAIL"]:
            msg = "配置未完成：请填写真实 RSS_URLS / SENDER_EMAIL"
            if _should_log_skip():
                log_run("run", 0, "skip", msg)
            return {"status": "skip", "detail": msg}
        if not force and not should_run():
            msg = f"未到轮询间隔（{cfg['POLL_INTERVAL_MINUTES']} 分）"
            if _should_log_skip():
                log_run("run", 0, "skip", msg)
            return {"status": "skip", "detail": msg}
        items = fetch_new(cfg["feeds"], cfg["CHECK_HOURS"])
        if items:
            send(items, cfg)
            conn = _conn()
            conn.executemany("INSERT OR IGNORE INTO sent(guid) VALUES (?)",
                             [(it["e"].get("id") or it["e"].get("link"),) for it in items])
            conn.commit()
            conn.close()
            log_run("run", len(items), "sent", f"已发送 {len(items)} 条")
            res = {"status": "sent", "count": len(items)}
        else:
            log_run("run", 0, "none", "无新条目")
            res = {"status": "none"}
        mark_run()
        return res
    except Exception as ex:
        log_run("run", 0, "error", str(ex))
        return {"status": "error", "detail": str(ex)}


def check_feed(url):
    """单源只读校验：返回频道标题、条目数、是否有 XML 容错(bozo)。"""
    if not url or not url.strip():
        return {"url": url, "error": "缺少 url"}
    try:
        feed = _fetch_feed(url)
        ch = feed.feed or {}
        if feed.bozo:
            return {"url": url, "title": ch.get("title", ""),
                    "count": len(feed.entries), "bozo": True,
                    "error": str(feed.get("bozo_exception", ""))[:200]}
        return {"url": url, "title": ch.get("title", ""),
                "count": len(feed.entries), "bozo": False, "error": ""}
    except Exception as ex:
        return {"url": url, "error": str(ex)}


def test_fetch(urls=None):
    """只读测试抓取，不发送、不要求邮箱配置。urls 为空时取已保存配置。"""
    if urls is None:
        urls = load_config()["urls"]
    result = []
    for url in urls:
        try:
            feed = _fetch_feed(url)
            ch = feed.feed or {}
            sample = [{"title": e.get("title", ""), "link": e.get("link", "")}
                      for e in feed.entries[:5]]
            result.append({"url": url, "title": ch.get("title", ""),
                           "count": len(feed.entries), "bozo": bool(feed.bozo),
                           "sample": sample})
        except Exception as ex:
            result.append({"url": url, "error": str(ex)})
    log_run("test", 0, "ok", f"测试 {len(urls)} 个源")
    return result


if __name__ == "__main__":
    print(run_once())
