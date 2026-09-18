"""Web UI: edit config, trigger/stop crawls, watch logs, browse results."""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

from . import crawler, db, selfupdate, settings, updater
from .fetcher import StopRequested
from .version import __version__

log = logging.getLogger("seedmm.web")

# ---- admin credentials (deployment-level, via environment) ----------------
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
_COOKIE = "jc_token"

# Session tokens are persisted under /data so logins survive container
# restarts and one-click self-updates.
_SESSIONS_FILE = os.path.join(settings.DATADIR, "sessions.json")


def _load_tokens() -> set[str]:
    try:
        with open(_SESSIONS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return set(data) if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def _save_tokens() -> None:
    try:
        os.makedirs(settings.DATADIR, exist_ok=True)
        tmp = _SESSIONS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(sorted(_tokens), fh)
        os.replace(tmp, _SESSIONS_FILE)
    except OSError as exc:
        log.warning("保存会话失败: %s", exc)


_tokens: set[str] = _load_tokens()


def _admin_password() -> str:
    global ADMIN_PASSWORD
    if not ADMIN_PASSWORD:
        ADMIN_PASSWORD = secrets.token_urlsafe(10)
        log.warning("★ 未设置 ADMIN_PASSWORD，本次随机生成: %s "
                    "（在 docker-compose.yml 的 environment 中固定它）", ADMIN_PASSWORD)
    return ADMIN_PASSWORD


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


def _recompute_thread(keywords: list[str]) -> None:
    """Recompute match marks for the whole library (background)."""
    try:
        conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=60)
        try:
            n = db.recompute_matched(conn, keywords)
        finally:
            conn.close()
        log.info("筛选标记已按关键词 [%s] 重算 %d 部", ",".join(keywords) or "空", n)
    except Exception:
        log.exception("重算筛选标记失败")


def _crawl_thread(params: dict) -> None:
    cfg = settings.load()  # snapshot taken at start
    try:
        _job["stats"] = crawler.run_job(
            cfg,
            pages=params.get("pages", "1"),
            magnets=bool(params.get("magnets", True)),
            refresh=bool(params.get("refresh", False)),
            mode=params.get("mode", "new"),
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

    # ---------------- auth ----------------
    _public = {"/", "/api/login"}

    @app.before_request
    def _require_auth():
        if request.path in _public or request.path.startswith("/static"):
            return None
        token = request.cookies.get(_COOKIE, "")
        if token and token in _tokens:
            return None
        return jsonify({"ok": False, "error": "未登录或会话已过期"}), 401

    @app.post("/api/login")
    def login():
        body = request.get_json(silent=True) or {}
        user = str(body.get("user", ""))
        password = str(body.get("password", ""))
        ok = (hmac.compare_digest(user, ADMIN_USER)
              and hmac.compare_digest(password, _admin_password()))
        if not ok:
            time.sleep(0.8)  # crude brute-force delay
            return jsonify({"ok": False, "error": "用户名或密码错误"}), 401
        token = secrets.token_urlsafe(32)
        _tokens.add(token)
        _save_tokens()
        resp = jsonify({"ok": True, "user": ADMIN_USER})
        resp.set_cookie(_COOKIE, token, httponly=True, samesite="Strict",
                        max_age=7 * 24 * 3600, path="/")
        log.info("管理员 %s 已登录", ADMIN_USER)
        return resp

    @app.post("/api/logout")
    def logout():
        token = request.cookies.get(_COOKIE, "")
        _tokens.discard(token)
        _save_tokens()
        resp = jsonify({"ok": True})
        resp.delete_cookie(_COOKIE, path="/")
        return resp

    # ---------------- pages ----------------
    @app.get("/")
    def index():
        # Not logged in -> standalone login page, the main UI stays hidden.
        token = request.cookies.get(_COOKIE, "")
        if not (token and token in _tokens):
            return render_template("login.html", version=__version__)
        return render_template("index.html", version=__version__)

    # ---------------- config ----------------
    @app.get("/api/config")
    def get_config():
        return jsonify({"config": settings.load()})

    @app.post("/api/config")
    def post_config():
        body = request.get_json(silent=True) or {}
        old_filters = settings.load().get("TAG_FILTERS", "")
        try:
            cfg = settings.save(body)
            log.info("配置已更新: %s", {k: v for k, v in body.items() if k != "USER_AGENT"})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if "TAG_FILTERS" in body and body["TAG_FILTERS"] != old_filters:
            keywords = [k for k in cfg["TAG_FILTERS"].split(",") if k.strip()]
            threading.Thread(target=_recompute_thread, args=(keywords,), daemon=True).start()
        return jsonify({"ok": True, "config": cfg})

    # ---------------- crawl control ----------------
    @app.post("/api/crawl/start")
    def crawl_start():
        body = request.get_json(silent=True) or {}
        mode = body.get("mode", "new")
        if mode not in ("new", "backfill"):
            mode = "new"
        pages = str(body.get("pages", "1"))
        if mode == "new":
            # only "new" uses a page range; backfill takes a page count
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
                params={"pages": pages, "mode": mode,
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
        depths = {}
        try:
            conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=10)
            try:
                depths = {
                    "censored": db.get_meta(conn, "max_page:censored", "0"),
                    "uncensored": db.get_meta(conn, "max_page:uncensored", "0"),
                }
            finally:
                conn.close()
        except Exception:
            depths = {"censored": "0", "uncensored": "0"}
        return jsonify({
            "running": _job["running"],
            "params": _job["params"],
            "stats": _job["stats"],
            "error": _job["error"],
            "started_at": _job["started_at"],
            "finished_at": _job["finished_at"],
            "depths": depths,
            "log_tail": list(_logs)[-200:],
        })

    # ---------------- data ----------------
    @app.get("/api/movies")
    def movies():
        q = (request.args.get("q") or "").strip()
        sort = request.args.get("sort", "new")
        if sort not in ("new", "match", "magnets"):
            sort = "new"
        category = request.args.get("category", "")
        if category not in ("censored", "uncensored"):
            category = ""
        try:
            page = max(1, int(request.args.get("page", 1)))
            size = max(1, min(int(request.args.get("size", 20)), 100))
        except ValueError:
            page, size = 1, 20
        conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=10)
        try:
            total, items = db.list_movies(conn, q=q, page=page, size=size,
                                          sort=sort, category=category)
        finally:
            conn.close()
        return jsonify({"total": total, "page": page, "size": size, "sort": sort, "items": items})

    @app.get("/api/stats")
    def stats():
        category = request.args.get("category", "")
        if category not in ("censored", "uncensored"):
            category = ""
        conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=15)
        try:
            data = db.library_stats(conn, category=category)
        finally:
            conn.close()
        return jsonify(data)

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

    @app.get("/api/export")
    def export_data():
        """Download all movies + magnets as a JSON file."""
        import json as _json

        conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=30)
        try:
            rows = conn.execute(
                "SELECT code, title, url, cover, release_date, duration, director, "
                "studio, label, series, actors, genres, samples, magnet_count, "
                "first_seen, last_seen FROM movies"
            ).fetchall()
            cols = ("code title url cover release_date duration director studio label "
                    "series actors genres samples magnet_count first_seen last_seen").split()
            out = []
            for r in rows:
                m = dict(zip(cols, r))
                for k in ("actors", "genres", "samples"):
                    try:
                        m[k] = _json.loads(m[k])
                    except (TypeError, ValueError):
                        m[k] = []
                m["magnets"] = [
                    {"hash": x[0], "name": x[1], "size": x[2], "date": x[3], "link": x[4]}
                    for x in conn.execute(
                        "SELECT hash, name, size, date, link FROM magnets WHERE code = ?",
                        (m["code"],),
                    )
                ]
                out.append(m)
        finally:
            conn.close()
        body = _json.dumps({"exported_at": _now(), "count": len(out), "movies": out},
                           ensure_ascii=False, indent=2)
        return body, 200, {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Disposition": 'attachment; filename="javbus-crawler-export.json"',
        }

    @app.post("/api/data/clear")
    def data_clear():
        """Erase all crawled movies + magnets and reset the page depth."""
        conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=30)
        try:
            deleted = conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
            conn.execute("DELETE FROM magnets")
            conn.execute("DELETE FROM movies")
            for cat in ("censored", "uncensored"):
                db.set_meta(conn, f"max_page:{cat}", 0)
            conn.commit()
        finally:
            conn.close()
        log.warning("已清空全部采集数据（%d 部影片）并重置采集深度", deleted)
        return jsonify({"ok": True, "deleted": deleted})

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


def _startup_selfcheck() -> None:
    """Verify the data directory and database are usable; log actionable hints."""
    cfg = settings.load()
    db_path = cfg["DB_PATH"]
    datadir = os.path.dirname(db_path) or "."
    if not os.path.isdir(datadir):
        log.error("自检失败: 数据目录 %s 不存在（检查卷挂载）", datadir)
        return
    if not os.access(datadir, os.W_OK):
        log.error("自检失败: 数据目录 %s 不可写。宿主机执行: sudo chown -R 65534:65534 <挂载目录> 后重启容器", datadir)
        return
    try:
        conn = db.connect(db_path)
        conn.close()
        log.info("自检通过: 数据库 %s 可读写", db_path)
    except sqlite3.Error as exc:
        log.error("自检失败: 无法打开数据库 %s (%s)。若文件属主异常，宿主机执行: "
                  "sudo chown -R 65534:65534 <挂载目录> 后重启容器", db_path, exc)


def _auto_crawl_loop() -> None:
    """Background scheduler: start a crawl every AUTO_CRAWL_INTERVAL_HOURS
    (when enabled in the web settings)."""
    next_run: float = 0.0  # 0 = not scheduled yet
    while True:
        time.sleep(30)
        try:
            cfg = settings.load()
            if not cfg.get("AUTO_CRAWL_ENABLED"):
                next_run = 0.0
                continue
            interval = float(cfg.get("AUTO_CRAWL_INTERVAL_HOURS") or 0)
            if interval <= 0:
                continue
            now = time.time()
            if next_run == 0.0:
                next_run = now + interval * 3600
                log.info("自动采集已启用: 每 %.1f 小时采集 %s 页（首次约 %s）",
                         interval, cfg.get("AUTO_CRAWL_PAGES"),
                         datetime.fromtimestamp(next_run, tz=timezone.utc)
                         .strftime("%m-%d %H:%M UTC"))
                continue
            if now < next_run:
                continue
            next_run = now + interval * 3600
            with _job_lock:
                if _job["running"]:
                    log.info("自动采集: 已有任务在运行，跳过本次")
                    continue
                pages = str(cfg.get("AUTO_CRAWL_PAGES") or "1")
                _job.update(
                    running=True, stop=False, stats={}, error=None,
                    started_at=_now(), finished_at=None,
                    params={"pages": pages, "magnets": True, "refresh": False,
                            "auto": True},
                )
                _job["thread"] = threading.Thread(
                    target=_crawl_thread, args=(_job["params"],), daemon=True
                )
                _job["thread"].start()
            log.info("自动采集已启动: pages=%s", pages)
        except Exception:
            log.exception("自动采集调度异常")


def run(host: str = "0.0.0.0", port: int | None = None) -> None:
    """Serve the web UI (waitress, production WSGI)."""
    from waitress import serve

    handler = _WebLogHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    logging.getLogger().addHandler(handler)

    if port is None:
        port = int(settings.load()["WEB_PORT"])
    app = create_app()
    _startup_selfcheck()
    threading.Thread(target=_auto_crawl_loop, daemon=True).start()
    log.info("Web 界面: http://%s:%d （配置文件 %s）", host, port, settings.CONFIG_PATH)
    serve(app, host=host, port=port, threads=8)
