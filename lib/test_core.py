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


def test_should_log_skip_exact_10min_with_subsecond(tmp_db, no_dotenv, monkeypatch):
    """亚秒精度的 last 记录不应让心跳间隔漂成 11 分钟：+600s 触发，+595s 不触发。"""
    base = (1_700_000_000 // 60) * 60  # 整分钟基准（60 的整数倍）
    last_ts = base + 0.3  # 带亚秒的真实写入时刻
    conn = core._conn()
    conn.execute("INSERT INTO runs(ts,type,count,status,detail) VALUES (?,?,?,?,?)",
                 (last_ts, "run", 0, "skip", "x"))
    conn.commit(); conn.close()
    # +595s：未到 10 分钟 → False
    monkeypatch.setattr(core.time, "time", lambda: last_ts + 595)
    assert core._should_log_skip() is False
    # +600s：整 10 分钟 → True（修复前亚秒使其差 0.x 秒不达标，顺延到 +660s）
    monkeypatch.setattr(core.time, "time", lambda: last_ts + 600)
    assert core._should_log_skip() is True


def test_startup_log_appears_in_status(tmp_db, no_dotenv):
    """服务启动/重启通过 log_run('start', ...) 写入 runs，并在 get_status 中可见。"""
    core.log_run("start", 0, "ok", "服务启动")
    st = core.get_status()
    start_rows = [r for r in st["runs"] if r["type"] == "start"]
    assert len(start_rows) == 1
    assert start_rows[0]["status"] == "ok"
    assert start_rows[0]["detail"] == "服务启动"


# ---------- 多时点调度（SCHEDULE_MODE=fixed_times）----------
def test_parse_schedule_times():
    """非法项被过滤、重复去重、结果升序且 HH:MM 两位化。"""
    got = core.parse_schedule_times("08:00, 12:30, 08:00, 25:00, 12:61, abc, 8:5,")
    assert got == ["08:00", "12:30"]
    assert core.parse_schedule_times("") == []
    assert core.parse_schedule_times("23:59") == ["23:59"]


def test_get_next_schedule_time(tmp_db, no_dotenv, monkeypatch):
    """下一个定时点：今日剩余取最近未来；今日全过取明日最早。"""
    monkeypatch.setenv("SCHEDULE_MODE", "fixed_times")
    monkeypatch.setenv("SCHEDULE_TIMES", "08:00,12:00,20:00")
    lt = time.localtime()
    mk = lambda h, m: time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, lt.tm_wday, lt.tm_yday, -1))
    # 13:00 → 下一个应为 20:00 今日
    monkeypatch.setattr(core.time, "time", lambda: mk(13, 0))
    n1 = core.get_next_schedule_time()
    assert abs(n1 - mk(20, 0)) < 2
    # 21:00 → 今日全过 → 明日 08:00
    monkeypatch.setattr(core.time, "time", lambda: mk(21, 0))
    n2 = core.get_next_schedule_time()
    tmr = time.localtime(mk(21, 0) + 86400)
    exp = time.mktime((tmr.tm_year, tmr.tm_mon, tmr.tm_mday, 8, 0, 0, tmr.tm_wday, tmr.tm_yday, -1))
    assert abs(n2 - exp) < 2
    # 非多时点模式 → None
    monkeypatch.setenv("SCHEDULE_MODE", "interval")
    assert core.get_next_schedule_time() is None


def _set_last_run(ts):
    conn = core._conn()
    conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES ('last_run', ?)", (str(ts),))
    conn.commit(); conn.close()


def test_scheduler_tick_fixed_times_fires_when_due(tmp_db, no_dotenv, monkeypatch):
    """到定时点且 last_run 早于该点 → 触发 run_once(force=True)。"""
    monkeypatch.setenv("SCHEDULE_MODE", "fixed_times")
    monkeypatch.setenv("SCHEDULE_TIMES", "12:00")
    lt = time.localtime()
    now = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 5, 0, lt.tm_wday, lt.tm_yday, -1))
    last_run = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 11, 0, 0, lt.tm_wday, lt.tm_yday, -1))
    monkeypatch.setattr(core.time, "time", lambda: now)
    _set_last_run(last_run)
    calls = []
    monkeypatch.setattr(core, "run_once", lambda force=False: (calls.append(force) or {"status": "none"}))
    res = core.scheduler_tick()
    assert calls == [True]
    assert res["status"] == "none"


def test_scheduler_tick_fixed_times_idle_after_run(tmp_db, no_dotenv, monkeypatch):
    """已在该点运行过（last_run >= 定时点）→ 本分钟空闲，不重复触发。"""
    monkeypatch.setenv("SCHEDULE_MODE", "fixed_times")
    monkeypatch.setenv("SCHEDULE_TIMES", "12:00")
    lt = time.localtime()
    now = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 5, 0, lt.tm_wday, lt.tm_yday, -1))
    monkeypatch.setattr(core.time, "time", lambda: now)
    _set_last_run(now)  # last_run 已 >= 12:00 → 不应再次触发
    calls = []
    monkeypatch.setattr(core, "run_once", lambda force=False: (calls.append(force) or {"status": "none"}))
    res = core.scheduler_tick()
    assert calls == []
    assert res["status"] == "idle"


def test_scheduler_tick_interval_delegates(tmp_db, no_dotenv, monkeypatch):
    """interval 模式（默认）→ 直接委托 run_once()，不强制。"""
    monkeypatch.setenv("SCHEDULE_MODE", "interval")
    calls = []
    monkeypatch.setattr(core, "run_once", lambda force=False: (calls.append(force) or {"status": "skip"}))
    core.scheduler_tick()
    assert calls == [False]


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


# ---------- 检查窗口按模式隔离（CHECK_HOURS / CHECK_HOURS_FIXED_TIMES）----------
def test_load_config_check_hours_fixed_times_isolated(tmp_db, no_dotenv, monkeypatch):
    """每日多时点的检查窗口独立于固定间隔，二者各自存储。"""
    monkeypatch.setenv("CHECK_HOURS", "24")
    monkeypatch.setenv("CHECK_HOURS_FIXED_TIMES", "72")
    monkeypatch.setenv("SCHEDULE_MODE", "fixed_times")
    cfg = core.load_config()
    assert cfg["CHECK_HOURS"] == 24
    assert cfg["CHECK_HOURS_FIXED_TIMES"] == 72


def test_load_config_check_hours_fixed_times_defaults_to_check_hours(tmp_db, no_dotenv, monkeypatch):
    """未单独设置 CHECK_HOURS_FIXED_TIMES → 回落到 CHECK_HOURS（向后兼容）。"""
    monkeypatch.setenv("CHECK_HOURS", "24")
    cfg = core.load_config()
    assert cfg["CHECK_HOURS_FIXED_TIMES"] == 24


def test_run_once_uses_fixed_check_hours_in_fixed_times_mode(tmp_db, no_dotenv, monkeypatch):
    """fixed_times 模式 → run_once 将 CHECK_HOURS_FIXED_TIMES 传给 fetch_new。"""
    _seed_complete_config(monkeypatch)
    monkeypatch.setenv("SCHEDULE_MODE", "fixed_times")
    monkeypatch.setenv("CHECK_HOURS", "24")
    monkeypatch.setenv("CHECK_HOURS_FIXED_TIMES", "72")
    captured = {}
    monkeypatch.setattr(core, "fetch_new", lambda feeds, ch: captured.update({"ch": ch}) or [])
    monkeypatch.setattr(core, "send", lambda items, cfg: None)
    core.run_once(force=True)
    assert captured["ch"] == 72


def test_run_once_uses_interval_check_hours_in_interval_mode(tmp_db, no_dotenv, monkeypatch):
    """interval 模式 → run_once 仍使用 CHECK_HOURS（与多时点设置隔离）。"""
    _seed_complete_config(monkeypatch)
    monkeypatch.setenv("SCHEDULE_MODE", "interval")
    monkeypatch.setenv("CHECK_HOURS", "24")
    monkeypatch.setenv("CHECK_HOURS_FIXED_TIMES", "72")
    captured = {}
    monkeypatch.setattr(core, "fetch_new", lambda feeds, ch: captured.update({"ch": ch}) or [])
    monkeypatch.setattr(core, "send", lambda items, cfg: None)
    core.run_once(force=True)
    assert captured["ch"] == 24


# ---------- get_status: last_send 仅在实际发信时更新 ----------
def test_last_send_only_on_sent(tmp_db, no_dotenv, monkeypatch):
    _seed_complete_config(monkeypatch)
    # 无新条目 → none：last_run 应更新，但 last_send 必须保持 None
    monkeypatch.setattr(core, "fetch_new", lambda feeds, ch: [])
    monkeypatch.setattr(core, "send", lambda items, cfg: None)
    res = core.run_once(force=True)
    assert res["status"] == "none"
    st = core.get_status()
    assert st["last_run"] is not None
    assert st["last_send"] is None

    # 有新条目 → sent：last_send 应被写入
    monkeypatch.setattr(core, "fetch_new", lambda feeds, ch: [
        {"e": {"id": "g9", "link": "http://x/g9", "title": "t"}, "title": "S"}])
    res2 = core.run_once(force=True)
    assert res2["status"] == "sent"
    st2 = core.get_status()
    assert st2["last_send"] is not None


# ---------- _dedup_by_guid ----------
def test_dedup_by_guid(tmp_db, no_dotenv, monkeypatch):
    # 构造 3 个 items，其中 2 个相同 guid（g1 出现两次），应去重为 2 个且保留首次出现
    items = [
        {"e": {"id": "g1", "link": "http://x/g1", "title": "first"}, "title": "S"},
        {"e": {"id": "g1", "link": "http://x/g1", "title": "dup"}, "title": "S"},
        {"e": {"id": "g2", "link": "http://x/g2", "title": "second"}, "title": "S"},
    ]
    result = core._dedup_by_guid(items)
    assert len(result) == 2
    # 保留首次出现：title 为 first 的条目被保留，dup 被丢弃
    assert result[0]["e"]["title"] == "first"
    assert result[1]["e"]["title"] == "second"


def test_dedup_by_guid_falls_back_to_link(tmp_db, no_dotenv, monkeypatch):
    # 无 id 时以 link 作 guid 去重
    items = [
        {"e": {"link": "http://x/a"}, "title": "S"},
        {"e": {"link": "http://x/a"}, "title": "S"},
        {"e": {"link": "http://x/b"}, "title": "S"},
    ]
    result = core._dedup_by_guid(items)
    assert len(result) == 2
    assert result[0]["e"]["link"] == "http://x/a"
    assert result[1]["e"]["link"] == "http://x/b"
