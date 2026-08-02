from flask import Flask, request, jsonify, send_from_directory
from pathlib import Path
from apscheduler.schedulers.background import BackgroundScheduler
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import rss_mailer as core
import time, threading
from werkzeug.serving import make_server

SERVER = None  # 主服务引用；重启时先关闭监听 socket 再 execv，避免 Windows 下多进程抢端口

BASE = Path(__file__).parent
STATIC = BASE / "static"
app = Flask(__name__, static_folder=str(STATIC))

ALLOWED = {"RSS_URLS", "POLL_INTERVAL_MINUTES", "SMTP_HOST", "SMTP_PORT",
           "SENDER_EMAIL", "SMTP_AUTH_CODE", "RECIPIENTS", "CHECK_HOURS",
           "SCHEDULE_MODE", "SCHEDULE_TIMES"}


@app.route("/")
def index():
    return send_from_directory(str(STATIC), "index.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(core.public_config())


@app.route("/api/config", methods=["POST"])
def post_config():
    data = request.get_json(silent=True) or {}
    # 授权码留空表示「不修改」，剔除以免用空串覆盖原值
    if "SMTP_AUTH_CODE" in data and not str(data["SMTP_AUTH_CODE"]).strip():
        data.pop("SMTP_AUTH_CODE")
    upd = {k: str(v) for k, v in data.items() if k in ALLOWED}
    core.set_config(upd)
    return jsonify(core.public_config())


@app.route("/api/test", methods=["POST"])
def api_test():
    data = request.get_json(silent=True) or {}
    urls = data.get("urls")
    if urls is not None:
        urls = [str(u).strip() for u in urls if str(u).strip()]
    return jsonify({"results": core.test_fetch(urls)})


@app.route("/api/check_feed", methods=["POST"])
def api_check_feed():
    """单源只读校验：返回频道标题、条目数、是否有 XML 容错(bozo)。"""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    return jsonify(core.check_feed(url))


@app.route("/api/run", methods=["POST"])
def api_run():
    q = request.args.get("force") == "1"
    j = request.get_json(silent=True) or {}
    force = bool(q or j.get("force"))
    return jsonify(core.run_once(force=force))


@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify(core.get_status())


def _delayed_exit(seconds, fn):
    """延迟执行退出/重启，确保当前 HTTP 响应先完整返回给前端。"""
    def _run():
        time.sleep(seconds)
        fn()
    threading.Thread(target=_run, daemon=True).start()


@app.route("/api/stop", methods=["POST"])
def api_stop():
    _delayed_exit(1.0, lambda: os._exit(0))
    return jsonify({"ok": True, "action": "stop"})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    me = os.path.abspath(__file__)

    def _do_restart():
        # 先关闭监听 socket：Windows 下 os.execv 不会自动释放旧 socket，
        # 不关会导致新旧进程同时 LISTEN 同一端口、连接被错误分派而超时
        try:
            if SERVER is not None:
                SERVER.socket.close()
        except Exception:
            pass
        # 打标：让继承该环境变量的子进程在 __main__ 里把本次记作「重启」而非「启动」
        os.environ["RSS2EMAIL_RESTART"] = "1"
        os.execv(sys.executable, [sys.executable, me])

    _delayed_exit(1.0, _do_restart)
    return jsonify({"ok": True, "action": "restart"})


if __name__ == "__main__":
    sched = BackgroundScheduler()
    sched.add_job(core.scheduler_tick, "interval", minutes=1)
    sched.start()
    # 记录服务启停事件到运行日志（runs 表）；重启经 os.execv 继承 RSS2EMAIL_RESTART
    # 环境变量，故可区分「启动」与「重启」，两者都会重新走 __main__ 落到这里。
    is_restart = os.environ.get("RSS2EMAIL_RESTART") == "1"
    core.log_run("start", 0, "ok", "服务重启" if is_restart else "服务启动")
    os.environ.pop("RSS2EMAIL_RESTART", None)
    port = int(os.environ.get("WEB_PORT", 50000))
    SERVER = make_server("127.0.0.1", port, app)
    SERVER.serve_forever()
