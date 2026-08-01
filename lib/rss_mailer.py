import feedparser, sqlite3, smtplib, ssl, time, os, re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent  # 项目根目录（本文件位于 lib/ 下，.env 与 rss_state.db 都在根）
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

import os

BASE = ROOT
DB_PATH = os.environ.get("RSS_STATE_DB", str(BASE / "rss_state.db"))


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
    raw = os.environ.get("RSS_URLS") or os.environ.get("RSS_URL", "")
    feeds = _parse_feeds(raw)
    urls = [f["url"] for f in feeds]
    return {
        "RSS_URLS": raw,
        "feeds": feeds,
        "urls": urls,
        "POLL_INTERVAL_MINUTES": int(os.environ.get("POLL_INTERVAL_MINUTES", 60)),
        "SMTP_HOST": os.environ.get("SMTP_HOST", ""),
        "SMTP_PORT": int(os.environ.get("SMTP_PORT", 465)),
        "SENDER_EMAIL": os.environ.get("SENDER_EMAIL", ""),
        "SMTP_AUTH_CODE": os.environ.get("SMTP_AUTH_CODE", ""),
        "RECIPIENTS": os.environ.get("RECIPIENTS", ""),
        "CHECK_HOURS": int(os.environ.get("CHECK_HOURS", 24)),
    }


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
        poll = int(load_config().get("POLL_INTERVAL_MINUTES", 60) or 60)
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
    poll = int(os.environ.get("POLL_INTERVAL_MINUTES", 60))
    if not row:
        return True
    return (time.time() - float(row[0])) >= poll * 60


def mark_run():
    conn = _conn()
    conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES ('last_run', ?)", (str(time.time()),))
    conn.commit()
    conn.close()


# ---------- 抓取 / 发送 ----------
def fetch_new(feeds, check_hours):
    conn = _conn()
    sent = {r[0] for r in conn.execute("SELECT guid FROM sent")}
    cutoff = time.time() - check_hours * 3600
    new, old = [], []
    for feed in feeds:
        url = feed.get("url", "")
        if not url:
            continue
        fp = feedparser.parse(url)
        ch_title = (fp.feed or {}).get("title", "")
        src_title = feed.get("title", "") or ch_title
        for e in fp.entries:
            guid = e.get("id") or e.get("link")
            pub = e.get("published_parsed") or e.get("updated_parsed")
            if guid in sent or (pub and time.mktime(pub) < cutoff):
                old.append(guid)
                continue
            new.append({"e": e, "title": src_title})
    conn.executemany("INSERT OR IGNORE INTO sent(guid) VALUES (?)", [(g,) for g in old])
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
            log_run("run", 0, "skip", msg)
            return {"status": "skip", "detail": msg}
        if not force and not should_run():
            msg = f"未到轮询间隔（{cfg['POLL_INTERVAL_MINUTES']} 分）"
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
        feed = feedparser.parse(url)
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
            feed = feedparser.parse(url)
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
