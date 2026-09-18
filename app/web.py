"""Web UI: edit config, trigger/stop crawls, watch logs, browse results."""
from __future__ import annotations

import logging
import sqlite3
import threading
from collections import deque
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

from . import crawler, db, selfupdate, settings, updater
from .fetcher import StopRequested
from .version import __version__

log = logging.getLogger("seedmm.web")

_logs: deque[str] = deque(maxlen=1000)


class _WebLogHandler(logging.Handler):
    def emit(self, record):
        try:
            _logs.append(self.format(record))
        except Exception:
            pass


_job_lock = threading.Lock()
_job: dict = {
    "running": False,
    "stop": False,
    "thread": None,
    "started_at": None,
    "finished_at": None,
    "params": {},
    "stats": {},
    "error": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _crawl_thread(params: dict) -> None:
    cfg = settings.load()  # snapshot taken at start
    try:
        _job["stats"] = crawler.run_job(
            cfg,
            pages=params.get("pages", "1"),
            magnets=bool(params.get("magnets", True)),
            refresh=bool(params.get("refresh", False)),
            stop_check=lambda: _job["stop"],
        )
    except StopRequested:
        pass
    except Exception as exc:  # keep the worker alive no matter what
        log.exception("采集线程异常")
        _job["error"] = str(exc)
    finally:
        _job["running"] = False
        _job["finished_at"] = _now()


def create_app() -> Flask:
    app = Flask(__name__)
    app.json.ensure_ascii = False

    # ---------------- pages ----------------
    @app.get("/")
    def index():
        return render_template("index.html", version=__version__)

    # ---------------- config ----------------
    @app.get("/api/config")
    def get_config():
        return jsonify({"config": settings.load()})

    @app.post("/api/config")
    def post_config():
        body = request.get_json(silent=True) or {}
        try:
            cfg = settings.save(body)
            log.info("配置已更新: %s", {k: v for k, v in body.items() if k != "USER_AGENT"})
            return jsonify({"ok": True, "config": cfg})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    # ---------------- crawl control ----------------
    @app.post("/api/crawl/start")
    def crawl_start():
        body = request.get_json(silent=True) or {}
        pages = str(body.get("pages", "1"))
        try:
            crawler.parse_pages(pages)  # validate early
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        with _job_lock:
            if _job["running"]:
                return jsonify({"ok": False, "error": "已有采集任务在运行"}), 409
            _job.update(
                running=True, stop=False, stats={}, error=None,
                started_at=_now(), finished_at=None,
                params={"pages": pages,
                        "magnets": bool(body.get("magnets", True)),
                        "refresh": bool(body.get("refresh", False))},
            )
            _job["thread"] = threading.Thread(
                target=_crawl_thread, args=(_job["params"],), daemon=True
            )
            _job["thread"].start()
        return jsonify({"ok": True})

    @app.post("/api/crawl/stop")
    def crawl_stop():
        if not _job["running"]:
            return jsonify({"ok": False, "error": "当前没有运行中的任务"}), 409
        _job["stop"] = True
        log.info("已请求停止，等待当前请求完成…")
        return jsonify({"ok": True})

    @app.get("/api/crawl/status")
    def crawl_status():
        return jsonify({
            "running": _job["running"],
            "params": _job["params"],
            "stats": _job["stats"],
            "error": _job["error"],
            "started_at": _job["started_at"],
            "finished_at": _job["finished_at"],
            "log_tail": list(_logs)[-200:],
        })

    # ---------------- data ----------------
    @app.get("/api/movies")
    def movies():
        q = (request.args.get("q") or "").strip()
        try:
            page = max(1, int(request.args.get("page", 1)))
            size = max(1, min(int(request.args.get("size", 20)), 100))
        except ValueError:
            page, size = 1, 20
        conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=10)
        try:
            total, items = db.list_movies(conn, q=q, page=page, size=size)
        finally:
            conn.close()
        return jsonify({"total": total, "page": page, "size": size, "items": items})

    @app.get("/api/movies/<code>")
    def movie_detail(code: str):
        conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=10)
        try:
            movie = db.get_movie(conn, code)
        finally:
            conn.close()
        if not movie:
            return jsonify({"error": "not found"}), 404
        return jsonify(movie)

    # ---------------- update ----------------
    @app.get("/api/update/check")
    def update_check():
        rel = updater.check_for_update()
        return jsonify({
            "current": __version__,
            "latest": ({
                "tag": rel.get("tag_name"),
                "name": rel.get("name"),
                "notes": rel.get("body") or "",
                "url": rel.get("html_url"),
            } if rel else None),
        })

    @app.post("/api/update/apply")
    def update_apply():
        # Preferred: replace ourselves via the Docker API (docker.sock mount).
        if selfupdate.docker_available():
            try:
                result = selfupdate.perform_self_update()
                return jsonify(result)
            except RuntimeError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 500
        # Fallback: no socket mounted -> return host commands.
        return jsonify({
            "ok": False,
            "needs_socket": True,
            "commands": [
                "docker pull ghcr.io/daisyyijin/javbus-crawler:latest",
                "docker compose up -d",
            ],
            "note": "未检测到 Docker API：请在 docker-compose.yml 挂载 "
                    "/var/run/docker.sock:/var/run/docker.sock 以启用网页一键更新，"
                    "或在宿主机执行上述命令。",
        })

    return app


def run(host: str = "0.0.0.0", port: int | None = None) -> None:
    """Serve the web UI (waitress, production WSGI)."""
    from waitress import serve

    handler = _WebLogHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    logging.getLogger().addHandler(handler)

    if port is None:
        port = int(settings.load()["WEB_PORT"])
    app = create_app()
    log.info("Web 界面: http://%s:%d （配置文件 %s）", host, port, settings.CONFIG_PATH)
    serve(app, host=host, port=port, threads=8)
