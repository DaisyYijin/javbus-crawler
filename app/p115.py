"""115 网盘集成：扫码登录（可选登录设备）、磁力离线下载、任务监控、
下载完成后重命名（番号 标题）并移动到目标目录。

凭据持久化在 DATADIR/p115.json。所有 115 操作都依赖 p115client，
未安装时 HAS_P115=False，上层接口优雅降级。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time

from . import settings

log = logging.getLogger("seedmm.p115")


def _ensure_p115_cache_home() -> None:
    """p115client creates ~/.p115client.cache.d at import time; when the real
    home cannot host it (container user nobody -> HOME=/nonexistent), point
    HOME at our writable data dir before the import happens."""
    cache = os.path.join(os.path.expanduser("~"), ".p115client.cache.d")
    try:
        os.makedirs(cache, exist_ok=True)
    except OSError:
        os.environ["HOME"] = settings.DATADIR


_ensure_p115_cache_home()

try:
    from p115client import P115Client, check_response
    HAS_P115 = True
except ImportError:  # pragma: no cover - exercised only without the dep
    P115Client = None
    check_response = None
    HAS_P115 = False

AUTH_FILE = os.path.join(settings.DATADIR, "p115.json")

# Curated login devices (subset of p115client AVAILABLE_APPS that accept QR login)
DEVICES: dict[str, str] = {
    "android": "安卓",
    "ios": "苹果",
    "115android": "115 安卓",
    "115ios": "115 苹果",
    "ipad": "iPad",
    "tv": "电视",
    "os_windows": "Windows",
    "os_mac": "Mac",
    "os_linux": "Linux",
    "web": "网页版",
    "alipaymini": "支付宝小程序",
    "wechatmini": "微信小程序",
    "harmony": "鸿蒙",
}

_TASK_STATUS = {0: "等待中", 1: "下载中", 2: "已完成", -1: "失败", -2: "已取消"}
_QR_STATUS = {0: "waiting", 1: "scanned", 2: "success", -1: "expired", -2: "canceled"}
_ILLEGAL_FS = re.compile(r'[\\/:*?"<>|\r\n\t]')

_client = None
_client_lock = threading.Lock()
_user_cache: dict | None = None
_dir_cid: dict[str, int] = {}
_qr: dict = {}
_TIMEOUT = 10  # seconds, applied to every 115 network call


# ---------------------------------------------------------------- auth ----
def _load_auth() -> dict | None:
    try:
        with open(AUTH_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("cookies"):
            return data
    except (OSError, ValueError):
        pass
    return None


def _save_auth(cookies: str, app: str) -> None:
    os.makedirs(settings.DATADIR, exist_ok=True)
    tmp = AUTH_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"cookies": cookies, "app": app}, fh, ensure_ascii=False)
    os.replace(tmp, AUTH_FILE)


def has_auth() -> bool:
    return _load_auth() is not None


def get_client(refresh: bool = False):
    """Return a logged-in P115Client (cached), or None when not logged in."""
    global _client
    if not HAS_P115:
        return None
    if _client is not None and not refresh:
        return _client
    with _client_lock:
        if _client is not None and not refresh:
            return _client
        auth = _load_auth()
        if not auth:
            return None
        try:
            c = P115Client(auth["cookies"], app=auth.get("app") or "android",
                           console_qrcode=False)
            check_response(c.login_status(timeout=_TIMEOUT))
            _client = c
            return c
        except Exception as exc:
            log.warning("115 登录态不可用: %s", exc)
            _client = None
            return None


def logout() -> None:
    global _client, _user_cache
    with _client_lock:
        _client = None
        _user_cache = None
    _dir_cid.clear()
    try:
        os.remove(AUTH_FILE)
    except OSError:
        pass


def status() -> dict:
    global _user_cache
    if not HAS_P115:
        return {"available": False, "logged_in": False}
    if _user_cache is not None:
        return _user_cache
    c = get_client()
    if not c:
        return {"available": True, "logged_in": False}
    try:
        data = check_response(c.user_info(timeout=_TIMEOUT))["data"]
        auth = _load_auth() or {}
        _user_cache = {"available": True, "logged_in": True,
                       "user": data.get("user_name"),
                       "user_id": str(data.get("user_id", "")),
                       "device": auth.get("app", "")}
        return _user_cache
    except Exception as exc:
        log.warning("115 用户信息获取失败: %s", exc)
        return {"available": True, "logged_in": False}


# ------------------------------------------------------------- qr login ----
def _bare_client(app: str):
    """A client good enough for the anonymous QR endpoints (never logs in)."""
    return P115Client("UID=0; CID=0; SEID=0; KID=0", app=app, console_qrcode=False)


def qr_start(device: str) -> dict:
    if not HAS_P115:
        raise RuntimeError("p115client 未安装")
    app = device if device in DEVICES else "android"
    c = _bare_client(app)
    data = check_response(c.login_qrcode_token(app, timeout=_TIMEOUT))["data"]
    url = data.get("qrcode") or f"https://115.com/scan/dg-{data['uid']}"
    _qr.clear()
    _qr.update(client=c, uid=str(data["uid"]), time=data["time"], sign=data["sign"],
               app=app, url=url, created=time.time())
    return {"qrcode": url, "app": app}


def qr_svg() -> str:
    """Render the current login QR as an SVG image (empty when none)."""
    url = _qr.get("url") or ""
    if not url:
        return ""
    import io

    import qrcode
    import qrcode.image.svg

    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


def qr_poll() -> dict:
    c = _qr.get("client")
    if not c or not _qr.get("uid"):
        return {"status": "none"}
    global _client
    try:
        # same session (cookiejar) as the token request is required here
        data = check_response(c.login_qrcode_scan_status(
            {"uid": _qr["uid"], "time": _qr["time"], "sign": _qr["sign"]},
            timeout=_TIMEOUT))["data"] or {}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
    raw = data.get("status")
    if raw is None:
        # 115 intermittently answers {"data": {}} (IP-level rate limiting);
        # treat as retryable "waiting" — a real scan still yields status=2
        return {"status": "waiting"}
    st = _QR_STATUS.get(raw, f"unknown({raw})")
    if st in ("expired", "canceled"):
        _qr.clear()
        return {"status": st}
    if st != "success":
        return {"status": st}
    global _client, _user_cache
    resp = check_response(c.login_qrcode_scan_result(
        _qr["uid"], app=_qr["app"], timeout=_TIMEOUT))
    cookies = "; ".join(f"{x['name']}={x['value']}"
                        for x in resp["data"]["cookie"])
    _save_auth(cookies, _qr["app"])
    _qr.clear()
    _client = None
    _user_cache = None
    return {"status": "success", "user": status().get("user")}


# -------------------------------------------------------- offline tasks ----
def add_magnet(link: str) -> dict:
    """Submit a magnet link to 115 cloud download, into the download dir."""
    c = get_client()
    if not c:
        raise RuntimeError("115 未登录")
    payload = {"url": link}
    dl_dir = (settings.load().get("P115_DOWNLOAD_DIR") or "").strip()
    if dl_dir:
        payload["wp_path_id"] = resolve_dir(c, dl_dir)
    resp = check_response(c.clouddownload_task_add_urls(payload, timeout=_TIMEOUT))
    return {"name": resp.get("name") or link,
            "info_hash": (resp.get("info_hash") or "").upper()}


def list_tasks(page: int = 1, size: int = 30) -> dict:
    c = get_client()
    if not c:
        raise RuntimeError("115 未登录")
    resp = check_response(c.clouddownload_task_list(
        {"page": page, "page_size": size}, timeout=_TIMEOUT))
    items = [{
        "name": t.get("name"),
        "status": _map_task_status(t.get("status")),
        "percent": t.get("percentDone", 0),
        "size": t.get("file_size") or t.get("size"),
        "info_hash": (t.get("info_hash") or "").upper(),
    } for t in resp.get("tasks") or []]
    return {"total": resp.get("count", len(items)), "items": items}


def _map_task_status(raw) -> str:
    try:
        return _TASK_STATUS.get(int(raw), f"未知({raw})")
    except (TypeError, ValueError):
        return "未知"


# ------------------------------------------------------------ organize ----
def _sanitize_name(name: str) -> str:
    """Filesystem-illegal characters (for 115 too) -> spaces."""
    return _ILLEGAL_FS.sub(" ", str(name or "")).strip()


def _find_dir(c, name: str) -> int | None:
    offset = 0
    while True:
        data = check_response(c.fs_files(
            {"cid": 0, "limit": 100, "offset": offset, "show_dir": 1},
            timeout=_TIMEOUT))["data"]
        for item in data.get("list") or []:
            if item.get("n") == name and str(item.get("fc")) == "0":
                return int(item["fid"])
        if offset + 100 >= int(data.get("count", 0)):
            return None
        offset += 100


def resolve_dir(c, name: str) -> int:
    """Find a root-level 115 directory by name, creating it when missing.

    Results are cached per name; use reset_dir_cache() after renames.
    """
    name = (name or "").strip() or "未命名"
    if name in _dir_cid:
        return _dir_cid[name]
    cid = _find_dir(c, name)
    if cid is None:
        cid = int(check_response(c.fs_mkdir(name, pid=0, timeout=_TIMEOUT))["file_id"])
    _dir_cid[name] = cid
    return cid


def reset_dir_cache() -> None:
    _dir_cid.clear()


def organize_pass(max_pages: int = 5) -> int:
    """Rename completed tasks (code + title) and move them to the target dir.

    Tasks are matched back to movies via the magnet info_hash stored in our
    own database; already-organized hashes are remembered in the meta table.
    """
    c = get_client()
    if not c:
        return 0
    conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=30)
    organized = 0
    try:
        tasks: list[dict] = []
        for page in range(1, max_pages + 1):
            resp = check_response(c.clouddownload_task_list(
                {"page": page, "page_size": 50}, timeout=_TIMEOUT))
            batch = resp.get("tasks") or []
            tasks.extend(batch)
            if page * 50 >= int(resp.get("count", len(tasks))):
                break
        for t in tasks:
            if _map_task_status(t.get("status")) != "已完成":
                continue
            ih = (t.get("info_hash") or "").upper()
            if not ih or db_get_meta(conn, f"p115:done:{ih}"):
                continue
            try:
                _organize_one(conn, c, t, ih)
                organized += 1
            except Exception as exc:
                log.warning("115 整理失败 %s: %s", t.get("name"), exc)
    finally:
        conn.close()
    return organized


def db_get_meta(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else ""


def _organize_one(conn, c, task: dict, info_hash: str) -> None:
    fid = task.get("file_id") or task.get("delete_file_id")
    row = conn.execute(
        "SELECT m.code, m.title FROM magnets g JOIN movies m ON m.code = g.code "
        "WHERE g.hash = ?", (info_hash,)).fetchone()
    if row and fid:
        code, title = row
        old = str(task.get("name") or code)
        try:
            finfo = check_response(c.fs_file(fid, timeout=_TIMEOUT))["data"]
            old = str(finfo.get("file_name") or old)
            src_cid = int(finfo.get("cid") or 0)
        except Exception:
            src_cid = None
        parts = old.rsplit(".", 1)
        ext = f".{parts[1]}" if len(parts) == 2 and 0 < len(parts[1]) <= 5 else ""
        new_name = _sanitize_name(f"{code} {title}")[:180] + ext
        if new_name and new_name != old:
            check_response(c.fs_rename((int(fid), new_name), timeout=_TIMEOUT))
            log.info("115 重命名: %s -> %s", old, new_name)
        target = resolve_dir(c, (settings.load().get("P115_TARGET_DIR") or "").strip() or "已整理")
        if target and src_cid is not None and src_cid != target:
            check_response(c.fs_move(int(fid), target, timeout=_TIMEOUT))
            log.info("115 移动: %s -> 目录 %s", new_name, target)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, '1')",
                 (f"p115:done:{info_hash}",))
    conn.commit()
