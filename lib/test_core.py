"""rss_mailer 核心逻辑测试（网络全部 mock，不真实访问）。

运行：pytest lib/test_core.py -q
"""
import os
import sys
import time

import pytest

# 本测试位于 lib/ 内，同目录即可 import rss_mailer
sys.path.insert(0, os.path.dirname(__file__))

import rss_mailer as core


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """把状态库指向临时文件，避免污染项目根目录的 rss_state.db。"""
    db = tmp_path / "rss_state.db"
    monkeypatch.setattr(core, "DB_PATH", str(db))
    return db


@pytest.fixture
def no_dotenv(monkeypatch):
    """屏蔽 dotenv 对 .env 的加载，使环境变量仅受本测试控制。"""
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)


def _mk_feed(entries):
    return type("FP", (), {"feed": {"title": "Ch"}, "entries": entries, "bozo": False})()


def _mk_entry(guid, age_hours=None):
    e = {"id": guid, "title": f"T-{guid}", "link": f"http://x/{guid}", "summary": "s"}
    if age_hours is not None:
        # published_parsed 用 localtime，使 time.mktime 反解与时间差一致
        e["published_parsed"] = time.localtime(time.time() - age_hours * 3600)
    return e


# ---------- _parse_feeds ----------
def test_parse_feeds():
    assert core._parse_feeds("a.com,b.com") == [
        {"title": "", "url": "a.com"}, {"title": "", "url": "b.com"}]
    assert core._parse_feeds("") == []
    assert core._parse_feeds("标题|http://x.com") == [
        {"title": "标题", "url": "http://x.com"}]
    assert core._parse_feeds(" , a|http://a , ") == [
        {"title": "a", "url": "http://a"}]
    assert core._parse_feeds("T1|http://1, T2|http://2") == [
        {"title": "T1", "url": "http://1"}, {"title": "T2", "url": "http://2"}]


# ---------- _safe_int ----------
def test_safe_int_valid():
    assert core._safe_int("42") == 42
    assert core._safe_int(" 7 ") == 7
    assert core._safe_int("10", 0) == 10


def test_safe_int_fallback():
    assert core._safe_int("", 5) == 5
    assert core._safe_int(None, 5) == 5
    assert core._safe_int("abc", 5) == 5
    assert core._safe_int("3.5", 9) == 9
    assert core._safe_int(None) == 0
    assert core._safe_int("") == 0


# ---------- public_config ----------
def test_public_config(tmp_db, no_dotenv, monkeypatch):
    monkeypatch.setenv("SMTP_AUTH_CODE", "secret123")
    cfg = core.public_config()
    assert "SMTP_AUTH_CODE" not in cfg
    assert cfg["has_smtp_auth_code"] is True

    monkeypatch.setenv("SMTP_AUTH_CODE", "")
    cfg2 = core.public_config()
    assert cfg2["has_smtp_auth_code"] is False


# ---------- fetch_new ----------
def test_fetch_new_dedup(tmp_db, no_dotenv, monkeypatch):
    conn = core._conn()
    conn.execute("INSERT INTO sent(guid) VALUES (?)", ("g1",))
    conn.commit()
    conn.close()

    feeds = [{"title": "S", "url": "http://feed"}]
    monkeypatch.setattr(core, "_fetch_feed", lambda url, timeout=None:
                        _mk_feed([_mk_entry("g1", age_hours=1), _mk_entry("g2", age_hours=1)]))

    new = core.fetch_new(feeds, 24)
    guids = [it["e"]["id"] for it in new]
    assert "g1" not in guids       # 已 in sent → 不进 new
    assert "g2" in guids


def test_fetch_new_first_run_window(tmp_db, no_dotenv, monkeypatch):
    # 全新库 → 首跑；100h 前的条目在 168h 大窗口内 → 仍作 new
    feeds = [{"title": "S", "url": "http://feed"}]
    monkeypatch.setattr(core, "_fetch_feed", lambda url, timeout=None:
                        _mk_feed([_mk_entry("old1", age_hours=100)]))

    new = core.fetch_new(feeds, 24)
    assert [it["e"]["id"] for it in new] == ["old1"]


def test_fetch_new_window_filters_old(tmp_db, no_dotenv, monkeypatch):
    # 设置 last_run 且 sent 非空 → 非首跑；窗口 24h；100h 前条目 → 不进 new
    conn = core._conn()
    conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES ('last_run', ?)",
                 (str(time.time()),))
    conn.execute("INSERT INTO sent(guid) VALUES (?)", ("seed",))
    conn.commit()
    conn.close()

    feeds = [{"title": "S", "url": "http://feed"}]
    monkeypatch.setattr(core, "_fetch_feed", lambda url, timeout=None:
                        _mk_feed([_mk_entry("old1", age_hours=100)]))

    new = core.fetch_new(feeds, 24)
    assert [it["e"]["id"] for it in new] == []


def test_fetch_new_nopub_not_stored(tmp_db, no_dotenv, monkeypatch):
    # 无发布时间的条目 → 作 new，但不应被无条件写入 sent（待 run_once 发送后再落库）
    feeds = [{"title": "S", "url": "http://feed"}]
    monkeypatch.setattr(core, "_fetch_feed", lambda url, timeout=None:
                        _mk_feed([_mk_entry("nopub")]))

    new = core.fetch_new(feeds, 24)
    assert [it["e"]["id"] for it in new] == ["nopub"]

    conn = core._conn()
    cnt = conn.execute("SELECT COUNT(*) FROM sent WHERE guid='nopub'").fetchone()[0]
    conn.close()
    assert cnt == 0


def test_fetch_new_empty_feeds(tmp_db, no_dotenv, monkeypatch):
    called = {"n": 0}
    def fake(url, timeout=None):
        called["n"] += 1
        return _mk_feed([])
    monkeypatch.setattr(core, "_fetch_feed", fake)
    assert core.fetch_new([], 24) == []
    assert called["n"] == 0


# ---------- should_run ----------
def test_should_run(tmp_db, no_dotenv, monkeypatch):
    monkeypatch.setenv("POLL_INTERVAL_MINUTES", "60")
    # 无 last_run → True
    assert core.should_run() is True
    # 刚运行过 → False
    core.mark_run()
    assert core.should_run() is False
    # 2 小时前运行（间隔 60 分）→ True
    conn = core._conn()
    conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES ('last_run', ?)",
                 (str(time.time() - 2 * 3600),))
    conn.commit()
    conn.close()
    assert core.should_run() is True


def test_should_run_bad_env(tmp_db, no_dotenv, monkeypatch):
    # 非法 POLL_INTERVAL_MINUTES → 回落默认 60，不抛异常
    monkeypatch.setenv("POLL_INTERVAL_MINUTES", "abc")
    core.mark_run()
    assert core.should_run() is False


# ---------- run_once ----------
def _seed_complete_config(monkeypatch):
    monkeypatch.setenv("RSS_URLS", "S|http://feed")
    monkeypatch.setenv("SENDER_EMAIL", "me@x.com")
    monkeypatch.setenv("RECIPIENTS", "you@x.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.x.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_AUTH_CODE", "pw")


def test_run_once_force_sends(tmp_db, no_dotenv, monkeypatch):
    _seed_complete_config(monkeypatch)
    monkeypatch.setattr(core, "_fetch_feed", lambda url, timeout=None:
                        _mk_feed([_mk_entry("g1", age_hours=1)]))

    sent_items = []
    monkeypatch.setattr(core, "send", lambda items, cfg: sent_items.extend(items))

    res = core.run_once(force=True)
    assert res["status"] == "sent"
    assert len(sent_items) == 1
    # 发送后落库
    conn = core._conn()
    cnt = conn.execute("SELECT COUNT(*) FROM sent WHERE guid='g1'").fetchone()[0]
    conn.close()
    assert cnt == 1


def test_run_once_skip_when_throttled(tmp_db, no_dotenv, monkeypatch):
    _seed_complete_config(monkeypatch)
    core.mark_run()  # 刚运行过
    monkeypatch.setattr(core, "fetch_new", lambda feeds, ch: [])
    monkeypatch.setattr(core, "send", lambda items, cfg: None)

    res = core.run_once(force=False)
    assert res["status"] == "skip"


def test_run_once_skip_incomplete(tmp_db, no_dotenv, monkeypatch):
    monkeypatch.setenv("RSS_URLS", "example.com")  # 含占位符 → 配置未完成
    monkeypatch.setattr(core, "fetch_new", lambda feeds, ch: [])
    monkeypatch.setattr(core, "send", lambda items, cfg: None)

    res = core.run_once(force=True)
    assert res["status"] == "skip"
