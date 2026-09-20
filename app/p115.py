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
# path -> (cid, cached_at); entries expire so renames/deletes made in the
# 115 app are picked up without a container restart
_dir_cid: dict[str, tuple[int, float]] = {}
_DIR_TTL = 10 * 60
_qr: dict = {}
_dirs_diag_logged = False
_TIMEOUT = 10  # seconds, applied to every 115 network call
_QR_TIMEOUT = 15  # QR token/result endpoints
# get/status is a 30s LONG POLL: with no state change the server holds the
# connection for ~30s before answering {"data": {}}. A shorter timeout
# NEVER sees the answer and misreads every poll as a network error.
_QR_POLL_TIMEOUT = 35


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
            # login_status returns a BOOL (not a dict): a healthy login
            # answers True, which check_response would misread as an
            # error (P115OSError [Errno 5] True) — check it directly
            if not c.login_status(timeout=_TIMEOUT):
                raise ValueError("cookie 已失效，请重新扫码登录")
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


_user_cache: dict | None = None
_user_cache_at: float = 0.0
_USER_CACHE_TTL = 300  # seconds; usage numbers go stale as tasks download
_user_diag_logged = False


def status() -> dict:
    global _user_cache, _user_cache_at, _user_diag_logged
    if not HAS_P115:
        return {"available": False, "logged_in": False}
    if _user_cache is not None and time.time() - _user_cache_at < _USER_CACHE_TTL:
        return _user_cache
    c = get_client()
    if not c:
        return {"available": True, "logged_in": False}
    try:
        data = check_response(c.user_info(timeout=_TIMEOUT))["data"]
        auth = _load_auth() or {}
        if not _user_diag_logged:
            _user_diag_logged = True
            try:
                log.info("115 user_info 字段: %s", ",".join(sorted(data.keys()))[:300])
            except Exception:
                pass
        # capacity lives in files/index_info, not user_info
        total = used = 0
        try:
            si = check_response(c.fs_index_info(timeout=_TIMEOUT)).get("data") or {}
            space = si.get("space_info") or {}
            total = int(space.get("total_size") or 0)
            used = int(space.get("use_size") or space.get("used_size") or 0)
        except Exception as exc:
            log.warning("115 容量信息获取失败: %s", exc)
        _user_cache = {
            "available": True, "logged_in": True,
            "user": data.get("user_name"),
            "user_id": str(data.get("user_id", "")),
            "device": auth.get("app", ""),
            "icon": (data.get("user_pic") or data.get("icon")
                     or data.get("head_img_url") or ""),
            "total_size": total,
            "used_size": used,
            "vip_end": int(data.get("vip_end_time") or 0),
        }
        _user_cache_at = time.time()
        return _user_cache
    except Exception as exc:
        log.warning("115 用户信息获取失败: %s", exc)
        return {"available": True, "logged_in": False}


# ------------------------------------------------------------- qr login ----
def _bare_client(app: str):
    """A client good enough for the anonymous QR endpoints (never logs in)."""
    return P115Client("UID=0; CID=0; SEID=0; KID=0", app=app, console_qrcode=False)


def _qr_request():
    """A `requests`-backed request engine for the QR endpoints.

    115 risk-controls qrcodeapi.115.com by client IP: datacenter IPs get
    tokens that are DOA (the phone instantly says 二维码已过期) and the
    status endpoint gets tarpitted. p115client's default engine supports
    no proxy, so we hand it one built on `requests` honouring the
    configured PROXY; without PROXY it behaves like a direct connection.
    """
    import requests as rq

    proxy = str(settings.load().get("PROXY") or "").strip()
    session = rq.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}

    def request(url: str, method: str = "GET", *, params=None, data=None,
                json=None, files=None, headers=None, follow_redirects=True,
                raise_for_status=False, stream=False, cookies=None,
                parse=None, async_=False, **kw):
        resp = session.request(method, url, params=params, data=data, json=json,
                               files=files, headers=headers,
                               allow_redirects=follow_redirects, cookies=cookies,
                               timeout=kw.get("timeout") or 10, stream=stream)
        if raise_for_status:
            resp.raise_for_status()
        if callable(parse):
            out = parse(resp, resp.content)
            resp.close()
            return out
        if parse is False:
            out = resp.content
            resp.close()
            return out
        return resp

    return request


def qr_start(device: str) -> dict:
    if not HAS_P115:
        raise RuntimeError("p115client 未安装")
    app = device if device in DEVICES else "android"
    # The QR token must be issued via the WEB path (/api/1.0/web/1.0/token/),
    # per the p115client author's reference implementation. Tokens issued
    # through an app-specific path return HTTP 200 but are never activated —
    # the phone then rejects the QR as "二维码已过期" on scan, and get/status
    # spins on empty data forever. The chosen device only matters for the
    # final scan-result call (device binding of the cookie).
    c = _bare_client(app)
    web_c = _bare_client("web")
    engine = _qr_request()
    data = check_response(web_c.login_qrcode_token("web", timeout=_QR_TIMEOUT,
                                                  request=engine))["data"]
    url = data.get("qrcode") or f"https://115.com/scan/dg-{data['uid']}"
    _qr.clear()
    _qr.update(client=c, web_client=web_c, uid=str(data["uid"]),
               time=data["time"], sign=data["sign"],
               app=app, url=url, created=time.time(), engine=engine)
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
    web_c = _qr.get("web_client") or c
    if not c or not _qr.get("uid"):
        return {"status": "none"}
    global _client
    engine = _qr.get("engine")
    try:
        # query with the SAME web session that issued the token;
        # must use the LONG-poll timeout (see _QR_POLL_TIMEOUT)
        data = check_response(web_c.login_qrcode_scan_status(
            {"uid": _qr["uid"], "time": _qr["time"], "sign": _qr["sign"]},
            timeout=_QR_POLL_TIMEOUT, request=engine))["data"] or {}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
    raw = data.get("status")
    st = _QR_STATUS.get(raw) if raw is not None else None
    if st in ("expired", "canceled"):
        _qr.clear()
        return {"status": st}
    if st != "success":
        # The status feed is unreliable on this endpoint: it can stay EMPTY
        # or stall at "scanned" (status=1) forever, even after the phone
        # confirmed. Probe the result endpoint directly on every poll —
        # a confirmed login hands out the cookie regardless of the feed.
        try:
            return _qr_finish(c, engine)
        except Exception:
            pass  # not confirmed yet (or not scanned at all) — keep waiting
        return {"status": st or "waiting"}
    return _qr_finish(c, engine)


def _cookies_str(raw) -> str:
    """Normalize the scan-result cookie field into "k=v; k=v" form.

    115 has been observed returning it as a list of dicts
    ([{"name": "UID", "value": "1"}, ...]) AND as a plain list of
    "k=v" strings; accept dict / list[dict] / list[str] alike.
    """
    if isinstance(raw, dict):
        return "; ".join(f"{k}={v}" for k, v in raw.items())
    parts = []
    for x in raw or []:
        if isinstance(x, dict):
            parts.append(f"{x.get('name')}={x.get('value')}")
        else:
            parts.append(str(x))
    return "; ".join(p for p in parts if p and "=" in p)


def _qr_finish(c, engine) -> dict:
    """Exchange a confirmed QR uid for cookies and persist the login."""
    global _client, _user_cache
    resp = check_response(c.login_qrcode_scan_result(
        _qr["uid"], app=_qr["app"], timeout=_QR_TIMEOUT, request=engine))
    cookies = _cookies_str(resp["data"].get("cookie"))
    if not cookies:
        raise ValueError(f"扫码结果中没有 cookie: {resp.get('data')}")
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


def del_tasks(info_hashes: list[str]) -> int:
    """Remove offline-task records from the 115 list (files are kept)."""
    c = get_client()
    if not c:
        raise RuntimeError("115 未登录")
    hashes = [h.strip().upper() for h in info_hashes if h and h.strip()]
    if not hashes:
        return 0
    deleted = 0
    # batch in groups of 20 to keep the form payload modest
    for i in range(0, len(hashes), 20):
        batch = hashes[i:i + 20]
        try:
            check_response(c.clouddownload_task_del(batch, timeout=_TIMEOUT))
            deleted += len(batch)
        except Exception as exc:
            log.warning("115 删除任务记录失败 %s…: %s", batch[0], exc)
    return deleted


# ------------------------------------------------------------ organize ----
_AD_EXTS = {".url", ".txt", ".htm", ".html", ".lnk", ".exe", ".apk", ".bat", ".cmd"}
_AD_PAT = re.compile(
    r"广告|推广|宣传|官网|发布页|最新地址|永久地址|防失联|失联|地址发布|"
    r"电报|飞机群|telegram|t\.me|qq群|微信群|扫码|二维码|请访问|"
    r"www\.|https?://|\.(com|net|org|xyz|top|icu|cc|tv|info|club|site|shop|fun|online|vip)\b|"
    r"娱乐城|押注|六合彩|赌博|赌场|赌城|彩票|免费领|福利群|资源群|中文不卡|高清资源|必看|"
    r"磁力|磁链|bt下载|bbs|论坛|导航站|搜索引擎|字幕网|每日更新|大更新", re.I)


def looks_ad(name: str) -> bool:
    """Heuristic: is this file/task name promotional spam?"""
    n = str(name or "").strip()
    if not n:
        return False
    ext = n.rsplit(".", 1)
    if len(ext) == 2 and len(ext[1]) <= 5 and f".{ext[1].lower()}" in _AD_EXTS:
        return True
    return bool(_AD_PAT.search(n))


def _fs_page(c, cid: int, offset: int, limit: int = 100) -> tuple[list, bool]:
    """One page of fs_files, normalized to (items, reached_end).

    115 sometimes answers with data as a bare LIST instead of the usual
    {"list": [...], "count": N} dict (empty folders etc.); accept both.
    """
    data = check_response(c.fs_files(
        {"cid": cid, "limit": limit, "offset": offset, "show_dir": 1},
        timeout=_TIMEOUT))["data"]
    if isinstance(data, list):
        return data or [], True  # bare list carries no pagination info
    items = data.get("list") or []
    return items, offset + limit >= int(data.get("count", 0) or 0)


def _sweep_ads(c, folder_fid: int, reject_cid: int) -> int:
    """Move ad files inside a downloaded folder to the reject dir."""
    moved = 0
    try:
        items, _done = _fs_page(c, folder_fid, 0, 1000)
    except Exception as exc:
        log.warning("115 列出文件夹 %s 失败: %s", folder_fid, exc)
        return 0
    for item in items:
        # fc == 0 -> directory; only judge files, folders keep the torrent layout
        if str(item.get("fc", "")) == "0" or not looks_ad(item.get("n")):
            continue
        if item.get("fid") is None:
            continue
        try:
            check_response(c.fs_move(int(item["fid"]), reject_cid, timeout=_TIMEOUT))
            moved += 1
            log.info("115 广告清理: %s -> 冗余目录", item.get("n"))
        except Exception as exc:
            log.warning("115 移动广告文件失败 %s: %s", item.get("n"), exc)
    return moved


def _sanitize_name(name: str) -> str:
    """Filesystem-illegal characters (for 115 too) -> spaces."""
    return _ILLEGAL_FS.sub(" ", str(name or "")).strip()


def _find_dir(c, name: str, pid: int = 0) -> int | None:
    offset = 0
    while True:
        items, done = _fs_page(c, pid, offset)
        for item in items:
            if item.get("n") == name:
                fid = _dir_id(item)
                if fid is not None:
                    return fid
        if done:
            return None
        offset += 100


def _split_path(path: str) -> list[str]:
    """'待整理 / 备份' -> ['待整理', '备份']（支持 / 与 \\ 分隔，去空白段）。"""
    return [p.strip() for p in str(path or "").replace("\\", "/").split("/") if p.strip()]


def resolve_dir(c, name: str) -> int:
    """Resolve a 115 directory by path ("一级/二级"), creating missing levels.

    Results are cached per path (with a TTL — see _DIR_TTL); use
    reset_dir_cache() to force a refresh.
    """
    parts = _split_path(name) or ["未命名"]
    key = "/".join(parts)
    hit = _dir_cid.get(key)
    if hit and time.time() - hit[1] < _DIR_TTL:
        return hit[0]
    cid = 0
    for part in parts:
        nxt = _find_dir(c, part, cid)
        if nxt is None:
            nxt = int(check_response(c.fs_mkdir(part, pid=cid, timeout=_TIMEOUT))["file_id"])
        cid = nxt
    _dir_cid[key] = (cid, time.time())
    return cid


def _dir_id(item: dict) -> int | None:
    """Directory id of a listing entry, or None when not usable.

    Web-shaped entries carry the dir id in BOTH fid and cid (equal for
    folders); bare-list shapes may only have cid.
    """
    if "fc" in item and str(item.get("fc")) != "0":
        return None
    for k in ("fid", "cid", "file_id", "id"):
        v = item.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
    return None


def list_dirs(cid: int = 0) -> list[dict]:
    """List immediate sub-directories of a 115 folder (for the dir picker)."""
    c = get_client()
    if not c:
        raise RuntimeError("115 未登录")
    out: list[dict] = []
    sample: list = []   # keep a few raw entries for a one-shot diagnostic
    offset = 0
    while True:
        items, done = _fs_page(c, cid, offset)
        if not sample:
            sample = items[:2]
        for item in items:
            fid = _dir_id(item)
            if fid is None:
                continue
            out.append({"fid": fid, "name": item.get("n") or ""})
        if done:
            break
        offset += 100
    if not out and sample:
        global _dirs_diag_logged
        if not _dirs_diag_logged:
            _dirs_diag_logged = True
            try:
                log.info("115 目录列表有条目但无目录被识别，条目示例: %s",
                         json.dumps(sample, ensure_ascii=False)[:400])
            except Exception:
                pass
    return out


def mkdir_dir(cid: int, name: str) -> int:
    """Create a sub-directory inside the given 115 folder (for the picker)."""
    c = get_client()
    if not c:
        raise RuntimeError("115 未登录")
    name = _sanitize_name(name) or "新建文件夹"
    return int(check_response(c.fs_mkdir(name, pid=cid, timeout=_TIMEOUT))["file_id"])


def reset_dir_cache() -> None:
    _dir_cid.clear()


def organize_pass(max_pages: int = 5) -> dict:
    """Rename completed tasks (code + title) and move them to the target dir.

    Tasks are matched back to movies via the magnet info_hash stored in our
    own database; already-organized hashes are remembered in the meta table.
    Returns counters and stores them as the last result in the meta table.
    """
    c = get_client()
    stats = {"at": int(time.time()), "scanned": 0, "organized": 0,
             "ads": 0, "rejected": 0}
    if not c:
        return stats
    conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=30)
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
            stats["scanned"] += 1
            try:
                _organize_one(conn, c, t, ih, stats)
            except Exception as exc:
                log.warning("115 整理失败 %s: %s", t.get("name"), exc)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                     ("p115:last_result", json.dumps(stats, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()
    return stats


def db_get_meta(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else ""


def last_organize_result() -> dict | None:
    """The stored result of the last organize pass (None when never run)."""
    conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=10)
    try:
        raw = db_get_meta(conn, "p115:last_result")
    finally:
        conn.close()
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def _organize_one(conn, c, task: dict, info_hash: str, stats: dict) -> None:
    cfg = settings.load()
    fid = task.get("file_id") or task.get("delete_file_id")
    row = conn.execute(
        "SELECT m.code, m.title FROM magnets g JOIN movies m ON m.code = g.code "
        "WHERE g.hash = ?", (info_hash,)).fetchone()
    if not fid:
        pass  # nothing to act on; only mark done
    elif row:
        stats["organized"] += 1
        code, title = row
        old = str(task.get("name") or code)
        src_cid = None
        try:
            finfo = check_response(c.fs_file(fid, timeout=_TIMEOUT))["data"]
            old = str(finfo.get("file_name") or old)
            src_cid = int(finfo.get("cid") or 0)
            is_folder = str(finfo.get("fc")) == "0"
        except Exception:
            is_folder = False
        # ad files riding along inside the download folder -> reject dir first
        if is_folder:
            reject = resolve_dir(c, (cfg.get("P115_REJECT_DIR") or "").strip() or "冗余")
            stats["ads"] += _sweep_ads(c, int(fid), reject)
        parts = old.rsplit(".", 1)
        ext = f".{parts[1]}" if len(parts) == 2 and 0 < len(parts[1]) <= 5 else ""
        new_name = _sanitize_name(f"{code} {title}")[:180] + ext
        if new_name and new_name != old:
            check_response(c.fs_rename((int(fid), new_name), timeout=_TIMEOUT))
            log.info("115 重命名: %s -> %s", old, new_name)
        target = resolve_dir(c, (cfg.get("P115_TARGET_DIR") or "").strip() or "已整理")
        if target and src_cid is not None and src_cid != target:
            check_response(c.fs_move(int(fid), target, timeout=_TIMEOUT))
            log.info("115 移动: %s -> 目录 %s", new_name, target)
    elif looks_ad(task.get("name")):
        # a completed task we don't know that smells like spam -> reject dir
        reject = resolve_dir(c, (cfg.get("P115_REJECT_DIR") or "").strip() or "冗余")
        try:
            check_response(c.fs_move(int(fid), reject, timeout=_TIMEOUT))
            stats["rejected"] += 1
            log.info("115 广告任务: %s -> 冗余目录", task.get("name"))
        except Exception as exc:
            log.warning("115 移动广告任务失败 %s: %s", task.get("name"), exc)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, '1')",
                 (f"p115:done:{info_hash}",))
    conn.commit()
