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

import requests

from . import crawler, parser, settings

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
# merge to the 3 buckets 115's own web UI uses (等待中 counts as 下载中,
# 已取消 as 下载失败)
_TASK_STATE = {0: "downloading", 1: "downloading", 2: "done", -1: "failed", -2: "failed"}
_QR_STATUS = {0: "waiting", 1: "scanned", 2: "success", -1: "expired", -2: "canceled"}
_ILLEGAL_FS = re.compile(r'[\\/:*?"<>|\r\n\t]')

_client = None
_client_lock = threading.Lock()
_organize_lock = threading.Lock()
_auth_lost_at = 0.0
_AUTH_LOST_ERRNOS = {401, 4102, 990001}
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
    """Return a logged-in P115Client (cached), or None when not logged in.

    The client is wrapped in _AuthWatch so an expired cookie discovered
    mid-call drops the cache and the app degrades to "not logged in"
    (the web UI will show the QR login prompt again).
    """
    global _client
    if not HAS_P115:
        return None
    if _client is not None and not refresh:
        return _AuthWatch(_client)
    with _client_lock:
        if _client is not None and not refresh:
            return _AuthWatch(_client)
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
            return _AuthWatch(c)
        except Exception as exc:
            log.warning("115 登录态不可用: %s", exc)
            _client = None
            return None


def _is_auth_loss(exc: Exception) -> bool:
    errno = getattr(exc, "errno", None)
    try:
        if errno is not None and int(errno) in _AUTH_LOST_ERRNOS:
            return True
    except (TypeError, ValueError):
        pass
    text = str(exc)
    return any(k in text for k in ("登录已失效", "未登录", "请登录", "not logged in"))


def _drop_auth(reason: str) -> None:
    global _client, _auth_lost_at
    with _client_lock:
        if _client is None:
            return
        _client = None
    now = time.time()
    if now - _auth_lost_at >= 60:
        _auth_lost_at = now
        log.error("%s", reason)


class _AuthWatch:
    """Proxy over P115Client: on an auth-loss API error drop the cache.

    The next get_client() then re-runs login_status and fails -> callers
    see "not logged in" instead of a stream of confusing per-call errors.
    """

    __slots__ = ("_c",)

    def __init__(self, c):
        self._c = c

    def __getattr__(self, name):
        attr = getattr(self._c, name)
        if not callable(attr):
            return attr
        def method(*args, **kwargs):
            try:
                return attr(*args, **kwargs)
            except Exception as exc:
                if _is_auth_loss(exc):
                    _drop_auth("115 cookie 已失效，请重新扫码登录")
                raise
        return method


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


def status() -> dict:
    global _user_cache, _user_cache_at
    if not HAS_P115:
        return {"available": False, "logged_in": False}
    if _user_cache is not None and time.time() - _user_cache_at < _USER_CACHE_TTL:
        return _user_cache
    c = get_client()
    if not c:
        return {"available": True, "logged_in": False}
    # user_info4 = webapi.115.com/user/info: the FULL profile. Real fields
    # (verified live): avatar is user_face / user_face_ori, VIP status is
    # is_vip (number), user_name — NOT user_pic / vip_end_time
    data: dict = {}
    try:
        resp = check_response(c.user_info4(timeout=_TIMEOUT))
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    except Exception as exc:
        log.warning("115 完整用户信息获取失败，改用精简接口: %s", exc)
    if not data.get("user_face") and not data.get("user_name"):
        try:
            data = check_response(c.user_info(timeout=_TIMEOUT))["data"] or data
        except Exception as exc:
            log.warning("115 用户信息获取失败: %s", exc)
            return {"available": True, "logged_in": False}
    auth = _load_auth() or {}
    # capacity: dedicated proapi endpoint; fall back to files/index_info.
    # Real shape (verified live via /api/p115/space-debug): data.all_total /
    # all_use / all_remain, each {"size": <bytes>, "size_format": "5.11PB"}
    def _space(sp: dict) -> tuple:
        def num(key: str) -> int:
            v = (sp or {}).get(key)
            return int(v.get("size") or 0) if isinstance(v, dict) else int(v or 0)
        return num("all_total"), num("all_use")

    total = used = 0
    try:
        resp = check_response(c.user_space_info(timeout=_TIMEOUT))
        sp = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        total, used = _space(sp)
    except Exception as exc:
        log.warning("115 容量信息获取失败(user_space_info): %s", exc)
    if not total:
        try:
            si = check_response(c.fs_index_info(timeout=_TIMEOUT)).get("data") or {}
            total, used = _space(si.get("space_info") or {})
        except Exception as exc:
            log.warning("115 容量信息获取失败(fs_index_info): %s", exc)
    # vip expiry is NOT in user_info4; try the nav endpoint
    vip_level = int(data.get("is_vip") or 0)
    vip_end = int(data.get("vip_end_time") or 0)
    if vip_level and not vip_end:
        try:
            nav = check_response(c.user_info3(timeout=_TIMEOUT)).get("data") or {}
            vip_end = int(nav.get("vip_end_time") or nav.get("vip_end") or 0)
        except Exception:
            pass
    _user_cache = {
        "available": True, "logged_in": True,
        "user": data.get("user_name") or data.get("username"),
        "user_id": str(data.get("user_id", "")),
        "device": auth.get("app", ""),
        "icon": (data.get("user_face") or data.get("user_face_ori")
                 or data.get("user_pic") or ""),
        "total_size": total,
        "used_size": used,
        "vip_level": vip_level,
        "vip_end": vip_end,
        # full payload for the frontend's heuristic avatar/vip picking
        "raw": data,
    }
    _user_cache_at = time.time()
    return _user_cache


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
        "state": _task_state(t.get("status")),
        "status": _map_task_status(t.get("status")),
        "percent": t.get("percentDone", 0),
        "size": t.get("file_size") or t.get("size"),
        "info_hash": (t.get("info_hash") or "").upper(),
    } for t in resp.get("tasks") or []]
    return {"total": resp.get("count", len(items)), "items": items}


def _task_state(raw) -> str:
    try:
        return _TASK_STATE.get(int(raw or 0), "downloading")
    except (TypeError, ValueError):
        return "downloading"


def _map_task_status(raw) -> str:
    try:
        return _TASK_STATUS.get(int(raw), f"未知({raw})")
    except (TypeError, ValueError):
        return "未知"


def del_tasks(info_hashes: list[str], *, purge_files: bool = False) -> int:
    """Remove offline-task records from the 115 list.

    purge_files=True also deletes the downloaded files (used when swapping
    a dead magnet, so half-downloaded junk doesn't pile up in the dir).
    """
    c = get_client()
    if not c:
        raise RuntimeError("115 未登录")
    # 115's task_del is CASE-SENSITIVE and the list returns lowercase
    # info_hash — upper-case hashes (what the UI shows) silently no-op
    hashes = [h.strip().lower() for h in info_hashes if h and h.strip()]
    if not hashes:
        return 0
    deleted = 0
    # batch in groups of 20 to keep the form payload modest
    for i in range(0, len(hashes), 20):
        batch = hashes[i:i + 20]
        payload: dict = {f"hash[{j}]": h for j, h in enumerate(batch)}
        if purge_files:
            payload["flag"] = 1
        try:
            check_response(c.clouddownload_task_del(payload, timeout=_TIMEOUT))
            deleted += len(batch)
        except Exception as exc:
            log.warning("115 删除任务记录失败 %s…: %s", batch[0], exc)
    return deleted


# -------------------------------------------------------- download watch ----
_TRACK_PREFIX = "p115:track:"
_LAST_RELEASE_KEY = "p115:last_release_at"
_VANISH_GRACE_S = 180  # fresh submissions may lag behind the 115 task list
_last_prog_log: dict[str, int] = {}  # ih -> ts of the last progress log line

_EXT_STRIP_RE = re.compile(
    r"\.(?:MP4|MKV|AVI|WMV|MOV|TS|M2TS|M2V|ISO|RMVB|RM|FLV|MPG|MPG4|DIVX|H264|SRT|ASS|JPG|PNG|NFO|TXT|URL|HTML?)$", re.I)
# trailing quality/edition decorations: -C -CH -UCD -CD1 -4K -1080P ... the
# glued alternatives cover magnet-style names like 'fit-008ch' (no separator)
_TAIL_TOKEN_RE = re.compile(
    r"(?:[-_. ](?:CD\d{1,2}|PART\d{1,2}|C|CH|UC|UCD|UNCENSORED|LEAK|4K|8K|1080P|720P|2160P)$"
    r"|(?<=\d)(?:CH|UCD|UC|C)$)", re.I)
_CODE_FC2_RE = re.compile(r"\bFC2[-_ ]?PPV[-_ ]?(\d{6,9})\b", re.I)
# AAA-100 / T28-619 / 259LUXU-1234 / 300MIUM-703 (numeric prefixes glue to
# the letters on either side: letters+digits like T28 or digits+letters like
# 259LUXU both count as a label)
_CODE_DASH_RE = re.compile(
    r"\b((?:\d{1,3})?[A-Z]{2,10}(?:\d{1,4})?|[A-Z]\d{1,4})[-_ ](\d{2,5})\b")
_CODE_PLAIN_RE = re.compile(r"\b([A-Z]{2,8})(\d{2,5})\b")
# flat multi-part video files keep the CD/PART marker in their name even
# though extract_code strips it: 'AAA-100 CD1.mp4' + 'AAA-100 CD2.mp4'
_PART_FILE_RE = re.compile(r"[-_. ](?:CD|PART)\d{1,2}$", re.I)


def extract_code(text) -> str | None:
    """Pull a normalized jav code out of a free-form task/file name.

    Handles the usual variants: 'aaa-100-ch' (-C/-CH/-UCD/-CD1/-4K tails),
    'AAA100' (no dash), 'T28-619' / '259LUXU-1234' (numeric prefixes) and
    'FC2-PPV-1234567'. Returns e.g. 'AAA-100', or None when nothing looks
    like a code. The caller still verifies the result against the DB.
    """
    s = str(text or "").upper()
    if not s:
        return None
    s = _EXT_STRIP_RE.sub("", s)
    for _ in range(4):
        stripped = _TAIL_TOKEN_RE.sub("", s).rstrip()
        if stripped == s:
            break
        s = stripped
    m = _CODE_FC2_RE.search(s)
    if m:
        return f"FC2-PPV-{m.group(1)}"
    m = _CODE_DASH_RE.search(s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = _CODE_PLAIN_RE.search(s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return None


def _is_part_file(name: str) -> bool:
    """True for multi-part video names like 'AAA-100 CD1.mp4' / '-PART2'."""
    stem = _EXT_STRIP_RE.sub("", str(name or "")).rstrip()
    return bool(_PART_FILE_RE.search(stem))


def metatube_title(code: str) -> str | None:
    """Look a code up on the configured MetaTube server; None when unset/miss.

    Used at organize time: MetaTube's title usually beats the scraped one
    (proper punctuation, episode tagging, etc.).
    """
    cfg = settings.load()
    base = str(cfg.get("METATUBE_URL") or "").strip().rstrip("/")
    if not base:
        return None
    token = str(cfg.get("METATUBE_TOKEN") or "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = requests.get(f"{base}/v1/movies/javbus/{code}", headers=headers, timeout=8)
        if r.status_code != 200:
            return None
        data = (r.json() or {}).get("data") or {}
    except Exception:
        return None
    title = str(data.get("title") or "").strip()
    return title or None


def track_add(code: str, info_hash: str, link: str, name: str = "",
              *, tried: set[str] | None = None, retries: int = 0) -> None:
    """Remember a submitted download so the watch loop can monitor it."""
    ih = (info_hash or "").upper()
    if not ih:
        return
    rec = {"code": code, "link": link, "name": name,
           "submitted_at": int(time.time()),
           "last_prog_at": int(time.time()), "last_percent": -1,
           "retries": retries,
           "tried": sorted({h.upper() for h in (tried or set())} | {ih})}
    conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=10)
    try:
        # a fresh submission invalidates any done/fail marks a previous
        # attempt of the same magnet left behind — a stale p115:done made
        # organize_pass release the slot instantly (and the serial gate
        # believe the movie was organized) without doing anything
        conn.execute("DELETE FROM meta WHERE key IN (?, ?)",
                     (f"p115:done:{ih}", f"p115:fail:{ih}"))
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                     (_TRACK_PREFIX + ih, json.dumps(rec, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()


def track_records() -> dict[str, dict]:
    """All tracked downloads keyed by upper info_hash."""
    conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=10)
    try:
        rows = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE ?",
            (_TRACK_PREFIX + "%",)).fetchall()
    finally:
        conn.close()
    out: dict[str, dict] = {}
    for key, raw in rows:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict):
            out[key[len(_TRACK_PREFIX):].upper()] = data
    return out


def _track_update(ih: str, **fields) -> None:
    conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=10)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?",
                           (_TRACK_PREFIX + ih,)).fetchone()
        if not row:
            return
        try:
            rec = json.loads(row[0])
        except ValueError:
            return
        rec.update(fields)
        conn.execute("UPDATE meta SET value = ? WHERE key = ?",
                     (json.dumps(rec, ensure_ascii=False), _TRACK_PREFIX + ih))
        conn.commit()
    finally:
        conn.close()


def track_del(ih: str) -> None:
    conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=10)
    try:
        cur = conn.execute("DELETE FROM meta WHERE key = ?", (_TRACK_PREFIX + ih,))
        if cur.rowcount:  # slot actually freed -> stamp the serial cooldown timer
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                         (_LAST_RELEASE_KEY, str(int(time.time()))))
        conn.commit()
    finally:
        conn.close()


def last_release_at() -> int:
    """Unix ts of the most recent download-slot release (0 when never)."""
    conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=10)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?",
                           (_LAST_RELEASE_KEY,)).fetchone()
    finally:
        conn.close()
    try:
        return int(row[0]) if row else 0
    except ValueError:
        return 0


def gaveup_codes() -> set[str]:
    """Codes whose downloads were abandoned (every magnet failed)."""
    conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=10)
    try:
        rows = conn.execute(
            "SELECT value FROM meta WHERE key LIKE 'p115:gaveup:%'").fetchall()
    finally:
        conn.close()
    out: set[str] = set()
    for (raw,) in rows:
        try:
            code = str((json.loads(raw) or {}).get("code") or "")
        except ValueError:
            continue
        if code:
            out.add(code)
    return out


def is_done(info_hash: str) -> bool:
    """True when the organize pass stamped p115:done for this download.

    The marker is only written after the file actually landed in the
    library (or organize gave up on a poison task), so the serial gate
    can use it as proof of a real organize — a track record merely
    disappearing is not proof (watch_pass releases the slot when a task
    falls out of the 115 list too).
    """
    conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=10)
    try:
        return bool(db_get_meta(conn, f"p115:done:{(info_hash or '').upper()}"))
    finally:
        conn.close()


def vanish_release(ih: str, code: str) -> None:
    """Task gone from the 115 list past the grace window: free the slot
    and stamp a gaveup marker so the serial gate stops waiting on it."""
    track_del(ih)
    conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=10)
    try:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                     (f"p115:gaveup:{ih}", json.dumps(
                         {"code": code,
                          "reason": "任务从115任务列表消失（被手动删除或被115拒绝）",
                          "at": int(time.time())}, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()
    log.warning("115 任务 %s（%.8s）已从任务列表消失，停止跟踪并放行串行槽",
                code or "?", ih)


def magnet_candidates(code: str, exclude: set[str]) -> dict | None:
    """Best remaining magnet for a code, skipping already-tried hashes.

    Live-fetches the full magnet list from the site (the DB keeps only the
    single best magnet per code, so a re-pick needs fresh candidates); falls
    back to the stored magnet when the site is unreachable.
    """
    cfg = settings.load()
    exclude = {h.upper() for h in exclude}
    left: list[dict] = []
    try:
        movie = crawler._fetch_code_movie(crawler._make_fetcher(cfg), code, True)
    except Exception as exc:
        log.info("换磁力：实时抓取 %s 磁力失败，回退本库: %s", code, exc)
        movie = None
    if movie:
        for m in movie.magnets:
            h = (parser.magnet_hash(m.link) or "").upper()
            if h and h not in exclude:
                left.append({"hash": h, "link": m.link, "name": m.name,
                             "size": m.size, "date": m.date})
    if not left:
        conn = sqlite3.connect(cfg["DB_PATH"], timeout=10)
        try:
            for h, link, name, size, date in conn.execute(
                    "SELECT hash, link, name, size, date FROM magnets WHERE code = ?",
                    (code,)):
                if (h or "").upper() not in exclude:
                    left.append({"hash": (h or "").upper(), "link": link,
                                 "name": name, "size": size, "date": date})
        finally:
            conn.close()
    if not left:
        return None
    keywords = [k for k in str(cfg.get("TAG_FILTERS") or "").split(",") if k.strip()]
    try:
        tiebreak = crawler.parse_tiebreak(cfg.get("MAGNET_TIEBREAK"))
    except ValueError:
        tiebreak = ["size", "date"]
    fallback = crawler.parse_fallback(cfg.get("MAGNET_FALLBACK"))
    best, _kw = crawler.pick_magnet(left, keywords, tiebreak, fallback)
    return best


def _swap_magnet(ih: str, rec: dict, max_retries: int) -> str:
    """Retry a dead download with the next-best magnet ('swapped'|'gaveup')."""
    code = str(rec.get("code") or "")
    tried = {h.upper() for h in (rec.get("tried") or [])} | {ih}
    retries = int(rec.get("retries") or 0)

    def giveup(reason: str) -> str:
        track_del(ih)
        conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=10)
        try:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                         (f"p115:gaveup:{ih}", json.dumps(
                             {"code": code, "reason": reason, "at": int(time.time())},
                             ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()
        log.warning("115 放弃下载 %s（%s）: 共尝试 %d 个磁力", code, reason, retries + 1)
        return "gaveup"

    if retries >= max_retries:
        return giveup("重试次数用尽")
    m = magnet_candidates(code, tried)
    if not m:
        return giveup("没有更多候选磁力")
    try:
        res = add_magnet(m["link"])
    except Exception as exc:
        # keep the old track record: the next round will retry the swap
        log.warning("换磁力：%s 提交新磁力失败，保留旧任务下轮重试: %s", code, exc)
        return "swapped"
    try:
        del_tasks([ih], purge_files=True)
    except Exception as exc:
        log.info("换磁力：删除旧任务 %s 失败（忽略）: %s", ih, exc)
    track_del(ih)
    track_add(code, res.get("info_hash") or m["hash"], m["link"], res.get("name") or "",
              tried=tried | {m["hash"]}, retries=retries + 1)
    log.info("115 换磁力 %s: %.8s -> %.8s（第 %d 次重试）",
             code, ih, m["hash"], retries + 1)
    return "swapped"


def watch_pass() -> dict:
    """One monitoring round over tracked downloads.

    Finished -> hold the slot until the organize pass lands the file, so the
    serial pipeline downloads one movie at a time (freed right away when
    P115_AUTO_ORGANIZE is off); failed / stalled / over the per-magnet time
    cap -> swap to the next magnet; a finished batch is organized right away
    when the P115_AUTO_ORGANIZE switch is on.
    """
    cfg = settings.load()
    stats = {"checked": 0, "done": 0, "swapped": 0, "gaveup": 0, "organized": 0}
    recs = track_records()
    if not recs or not has_auth():
        return stats
    c = get_client()
    if not c:
        return stats
    tasks: dict[str, dict] = {}
    for page in range(1, 6):
        resp = check_response(c.clouddownload_task_list(
            {"page": page, "page_size": 50}, timeout=_TIMEOUT))
        batch = resp.get("tasks") or []
        for t in batch:
            ih = (t.get("info_hash") or "").upper()
            if ih:
                tasks[ih] = t
        if page * 50 >= int(resp.get("count", len(batch))):
            break
    now = int(time.time())
    stall_s = max(1, int(cfg.get("P115_DL_STALL_MIN", 30))) * 60
    max_min = max(0, int(cfg.get("P115_DL_MAX_MIN", 2)))
    max_retries = max(0, int(cfg.get("P115_DL_MAX_RETRIES", 3)))
    organize = False
    for ih, rec in recs.items():
        stats["checked"] += 1
        t = tasks.get(ih)
        if not t:
            # Not in the 115 task list. A fresh submission can lag behind
            # this API by minutes, so releasing a young record on a single
            # miss made the serial gate believe the movie was already
            # organized ("整理刚完成") while nothing had landed at all.
            if now - int(rec.get("submitted_at") or now) < _VANISH_GRACE_S:
                continue  # inside the grace window: keep watching it
            vanish_release(ih, str(rec.get("code") or ""))
            continue
        try:
            status = int(t.get("status"))
        except (TypeError, ValueError):
            status = 0
        if status == 2:  # finished
            stats["done"] += 1
            if cfg.get("P115_AUTO_ORGANIZE"):
                # keep the slot until organize lands the file, so the serial
                # download gate won't submit the next movie too early
                if not rec.get("finished_at"):
                    _track_update(ih, finished_at=now, last_percent=100)
                    organize = True
            else:
                track_del(ih)  # nobody will organize it: free the slot now
        elif status in (-1, -2):  # failed / canceled
            stats[_swap_magnet(ih, rec, max_retries)] += 1
        else:  # waiting / downloading -> per-magnet time cap + stall guard
            pct = t.get("percentDone") or 0
            fresh = pct != rec.get("last_percent")
            if fresh:
                _track_update(ih, last_percent=pct, last_prog_at=now)
            submitted = int(rec.get("submitted_at") or now)
            # visibility: a silent multi-minute download looks exactly like a
            # wedged pipeline — surface progress once a minute per record
            if now - _last_prog_log.get(ih, 0) >= 60:
                _last_prog_log[ih] = now
                log.info("115 下载中 %s: %s %.0f%%（已 %d 分钟，上限 %d）",
                         rec.get("code"), rec.get("name"), float(pct),
                         (now - submitted) // 60, max_min)
            if max_min > 0 and now - submitted > max_min * 60:
                log.info("115 任务超时 %s: %s %.8s 已下载 %d 分钟（上限 %d），换磁力",
                         rec.get("code"), rec.get("name"), ih,
                         (now - submitted) // 60, max_min)
                stats[_swap_magnet(ih, rec, max_retries)] += 1
            elif not fresh and now - int(rec.get("last_prog_at")
                                         or rec.get("submitted_at") or now) > stall_s:
                log.info("115 任务卡住 %s: %s %.8s 超过 %d 分钟无进度，换磁力",
                         rec.get("code"), rec.get("name"), ih, stall_s // 60)
                stats[_swap_magnet(ih, rec, max_retries)] += 1
    if organize and cfg.get("P115_AUTO_ORGANIZE"):
        try:
            r = organize_pass()
            stats["organized"] = int(r.get("organized") or 0)
            if stats["organized"] or r.get("ads") or r.get("rejected"):
                log.info("115 下载完成后自动整理：影片 %d · 广告 %d · 拒收 %d",
                         stats["organized"], r.get("ads") or 0, r.get("rejected") or 0)
        except Exception:
            log.exception("115 下载完成后整理失败")
    return stats


# ------------------------------------------------------------ organize ----
_ORG_MAX_FAILS = 5                   # per-task organize attempts before give-up
_AD_EXTS = {".url", ".txt", ".htm", ".html", ".lnk", ".exe", ".apk", ".bat", ".cmd",
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ini", ".nfo"}
_AD_PAT = re.compile(
    r"广告|推广|宣传|官网|发布页|最新地址|新地址|永久地址|备用地址|防失联|失联|地址发布|"
    r"电报|飞机群|telegram|t\.me|qq群|微信群|扫码|二维码|请访问|"
    r"www\.|https?://|\.(com|net|org|xyz|top|icu|cc|tv|info|club|site|shop|fun|online|vip)\b|"
    r"娱乐城|押注|六合彩|赌博|赌场|赌城|彩票|免费领|福利群|资源群|中文不卡|高清资源|必看|"
    r"磁力|磁链|bt下载|bbs|论坛|导航站|搜索引擎|字幕网|每日更新|大更新|"
    r"永久域名|永久发布|免费观看|免费下载|免费看|在线看|手机看|app下载|收藏本站|"
    r"公众号|威信|薇信|防迷路|请记住", re.I)


def looks_ad(name: str) -> bool:
    """Heuristic: is this file/task name promotional spam?"""
    n = str(name or "").strip()
    if not n:
        return False
    ext = n.rsplit(".", 1)
    if len(ext) == 2 and len(ext[1]) <= 5 and f".{ext[1].lower()}" in _AD_EXTS:
        return True
    if _AD_PAT.search(n):
        return True
    # spam often pads spaces between characters to dodge keyword filters;
    # retry on the whitespace-squeezed variant ("最 新 地 址" etc.)
    squeezed = re.sub(r"\s+", "", n)
    return squeezed != n and bool(_AD_PAT.search(squeezed))


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


def _sanitize_name(name: str) -> str:
    """Filesystem-illegal characters (for 115 too) -> spaces."""
    return _ILLEGAL_FS.sub(" ", str(name or "")).strip()


def _find_dir(c, name: str, pid: int = 0) -> int | None:
    offset = 0
    while True:
        items, done = _fs_page(c, pid, offset)
        for item in items:
            if item.get("n") == name:
                fid = _dir_id(item, pid)
                if fid is not None:
                    return fid
        if done:
            return None
        offset += 100


def _split_path(path: str) -> list[str]:
    """'待整理 / 备份' -> ['待整理', '备份']（支持 / 与 \\ 分隔，去空白段）。"""
    return [p.strip() for p in str(path or "").replace("\\", "/").split("/") if p.strip()]


def _dir_cached(path: str) -> int | None:
    """Cached cid for a dir path (None when absent / past the TTL)."""
    key = "/".join(_split_path(path) or ["未命名"])
    hit = _dir_cid.get(key)
    if hit and time.time() - hit[1] < _DIR_TTL:
        return hit[0]
    return None


def resolve_dir(c, name: str) -> int:
    """Resolve a 115 directory by path ("一级/二级"), creating missing levels.

    Results are cached per path (with a TTL — see _DIR_TTL); use
    reset_dir_cache() to force a refresh. A cache miss joins the shared
    fs_files listing budget (walking = listings, and un-paced walks are
    what starved fs_file into its throttle loop).
    """
    hit = _dir_cached(name)
    if hit:
        return hit
    _pace_list(_list_gap())
    parts = _split_path(name) or ["未命名"]
    key = "/".join(parts)
    cid = 0
    for part in parts:
        nxt = _find_dir(c, part, cid)
        if nxt is None:
            # 115 fs_files occasionally answers with an EMPTY page instead
            # of an error (transient throttle glitch) — that looks exactly
            # like "dir missing". Re-check once before creating, otherwise
            # each glitch plants a duplicate directory.
            nxt = _find_dir(c, part, cid)
        if nxt is None:
            nxt = int(check_response(c.fs_mkdir(part, pid=cid, timeout=_TIMEOUT))["file_id"])
        cid = nxt
    _dir_cid[key] = (cid, time.time())
    return cid


def _dir_id(item: dict, cwd: int = -1) -> int | None:
    """Directory id of a listing entry, or None when not usable.

    Web-shaped entries carry the dir id in fid; bare-list shapes may only
    have cid — but entries whose resolved id equals the CURRENT folder are
    self-references (the folder itself), never navigable children.
    """
    if "fc" in item and str(item.get("fc")) != "0":
        return None
    for k in ("fid", "cid", "file_id", "id"):
        v = item.get(k)
        if v is None:
            continue
        try:
            fid = int(v)
        except (TypeError, ValueError):
            return None
        if fid != cwd:
            return fid
        # equal to cwd: fall through to the next candidate field
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
            fid = _dir_id(item, cid)
            if fid is None:
                continue
            # fid as STRING: 19-digit ids exceed JS Number precision (2^53)
            out.append({"fid": str(fid), "name": item.get("n") or ""})
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


def _to_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def list_files(cid: int = 0, path: str = "") -> dict:
    """List files (not dirs) inside a 115 folder.

    `path` (a config-style path like "JAV/待整理") wins over `cid` and is
    resolved via resolve_dir — handy for inspecting the download dir.
    """
    c = get_client()
    if not c:
        raise RuntimeError("115 未登录")
    p = (path or "").strip()
    if p:
        cid = resolve_dir(c, p)
    files: list[dict] = []
    dir_count = 0
    offset = 0
    while True:
        items, done = _fs_page(c, cid, offset)
        for item in items:
            # dir vs file: fc=="0" means dir; entries without fc but with a
            # size ("s") are files; otherwise fall back to the dir-id probe
            fc = item.get("fc")
            if fc is not None:
                is_dir = str(fc) == "0"
            elif "s" in item:
                is_dir = False
            else:
                is_dir = _dir_id(item, cid) is not None
            if is_dir:
                dir_count += 1
                continue
            files.append({
                "fid": str(item.get("fid") or item.get("file_id") or item.get("id") or ""),
                "name": item.get("n") or "",
                "size": _to_int(item.get("s")),
                "t": _to_int(item.get("t")),
            })
        if done:
            break
        offset += 100
    files.sort(key=lambda f: f["t"], reverse=True)
    return {"cid": cid, "files": files, "dir_count": dir_count}


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

    Serialized via _organize_lock: two background loops (the 30s organize
    loop and watch_pass finishing a download) can both trigger a pass; the
    loser just skips instead of running two interleaved passes.
    """
    stats = {"at": int(time.time()), "scanned": 0, "organized": 0,
             "ads": 0, "rejected": 0}
    if not _organize_lock.acquire(blocking=False):
        stats["busy"] = True
        log.info("115 整理已在进行中，跳过本次触发")
        return stats
    try:
        return _organize_pass_locked(max_pages)
    finally:
        _organize_lock.release()


def _organize_pass_locked(max_pages: int = 5) -> dict:
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
            if not ih:
                continue
            if db_get_meta(conn, f"p115:done:{ih}"):
                track_del(ih)  # already organized: release the download slot
                continue
            stats["scanned"] += 1
            try:
                _organize_one(conn, c, t, ih, stats)
            except Exception as exc:
                log.warning("115 整理失败 %s: %s", t.get("name"), exc)
                # a task that keeps RAISING (not the counted retry paths
                # inside) must not hold the serial slot forever either
                _org_fail_bump(conn, ih, str(t.get("name") or ""),
                               f"整理异常[{type(exc).__name__}: {exc}]")
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


def _lookup_movie(conn, code: str | None):
    """movies 表查询，容忍大小写差异（库内 'mida-790' vs 提取的 'MIDA-790'）。"""
    if not code:
        return None
    row = conn.execute("SELECT code, title FROM movies WHERE code = ?",
                       (code,)).fetchone()
    if not row:
        row = conn.execute("SELECT code, title FROM movies WHERE UPPER(code) = ?",
                           (code,)).fetchone()
    return row


def _org_fail_bump(conn, info_hash: str, label: str, why: str,
                   max_fails: int = _ORG_MAX_FAILS) -> None:
    """Count one failed organize attempt; release the slot (with a done
    mark) after max_fails so a poison task can't wedge the serial pipeline
    forever. Throttle-class failures pass a higher cap: budget starvation
    is a waiting problem, not a poison task."""
    fails = _to_int(db_get_meta(conn, f"p115:fail:{info_hash}")) + 1
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                 (f"p115:fail:{info_hash}", str(fails)))
    if fails >= max_fails:
        log.error("115 整理: %s 连续失败 %d 次，放行任务（不再自动重试）",
                  label, fails)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, '1')",
                     (f"p115:done:{info_hash}",))
    else:
        log.info("115 整理: %s %s，稍后重试（%d/%d）", label, why, fails,
                 max_fails)
    conn.commit()
    if fails >= max_fails:
        track_del(info_hash)


def _organize_one(conn, c, task: dict, info_hash: str, stats: dict) -> None:
    cfg = settings.load()
    fid = task.get("file_id") or task.get("delete_file_id")
    if not fid:
        # Finished but the list entry carries no file reference yet (the
        # file_id field lags behind the status flip). Faking a done mark
        # here released the slot while nothing had landed — the serial
        # gate then believed the movie was organized. Retry instead: the
        # 30s organize loop re-runs this until the reference shows up (or
        # _ORG_MAX_FAILS releases the task for good).
        _org_fail_bump(conn, info_hash, str(task.get("name") or ""),
                       "暂无文件信息")
        return
    row = conn.execute(
        "SELECT m.code, m.title FROM magnets g JOIN movies m ON m.code = g.code "
        "WHERE g.hash = ?", (info_hash,)).fetchone()
    if not row:
        # hash lookup missed (magnet rescraped / entry purged): fall back to
        # pulling the code out of the task name — covers variants like
        # 'aaa-100-ch xxx.mp4' — then verify it against our library
        row = _lookup_movie(conn, extract_code(task.get("name")))
    if row:
        code, title = row
        title = metatube_title(code) or title
        old = str(task.get("name") or code)
        target_name = (cfg.get("P115_TARGET_DIR") or "").strip() or "已整理"
        reject = resolve_dir(c, (cfg.get("P115_REJECT_DIR") or "").strip() or "冗余")
        target = resolve_dir(c, target_name)
        base = _sanitize_name(f"{code} {title}")
        # Optimistic folder-first: ONE listing both proves the id is a folder
        # AND fetches its contents — saving the paced fs_file round-trip on
        # the common (folder) download. An empty listing means "file or
        # throttled"; fs_file disambiguates afterwards.
        handled = False
        try:
            handled = _sweep_folder(conn, c, int(fid), old, target, reject,
                                    _list_gap(), stats,
                                    target_path=target_name, prefer_name=base,
                                    min_size=_ORG_MIN_SIZE)
        except Exception:
            handled = False  # e.g. the id isn't a folder at all
        if not handled:
            # listing came back empty / raised: file, or throttled. fs_file
            # joins the throttled fs_files budget before firing.
            _pace_list(_list_gap())
            try:
                data = check_response(c.fs_file(fid, timeout=_TIMEOUT))["data"]
                if isinstance(data, list):
                    # 115's get_info wraps DIRECTORY info in a bare list —
                    # unwrap the first dict; anything else is unknown shape
                    data = next((d for d in data if isinstance(d, dict)), None)
                if not isinstance(data, dict):
                    raise RuntimeError(
                        f"get_info 返回未知形状: {type(data).__name__}")
                if "fc" in data:
                    # listing convention: fc == 0 -> folder, else file class
                    is_folder = str(data["fc"]) == "0"
                else:
                    # no category field: decide from evidence, never a blind
                    # guess — files carry hashes / sizes, dirs do not
                    is_folder = not ("sha1" in data or "file_size" in data
                                     or "size" in data)
                old = str(data.get("file_name") or old)
                size = _to_int(data.get("size") or data.get("file_size"))
            except Exception as exc:
                # folder-vs-file is UNKNOWN and must not be guessed. Retry:
                # the error text says whether it IS throttling (empty
                # answer), a shape surprise, or something else (auth / …)
                _org_fail_bump(conn, info_hash, old,
                               f"文件信息获取失败[{type(exc).__name__}: {exc}]",
                               max_fails=_ORG_MAX_FAILS * 3)
                return
            if is_folder:
                # a folder whose listing came back empty -> throttled
                _org_fail_bump(conn, info_hash, old, "目录列表受限（疑似限流）")
                return
            handled = _sweep_file(conn, c, int(fid), old, size, target,
                                  reject, stats, target_path=target_name,
                                  prefer_name=base)
            if not handled:
                # listing throttled -> info incomplete; retry on a later pass
                _org_fail_bump(conn, info_hash, old, "列表受限")
                return
        # fall through to the common tail: stamp done + release the slot
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
    conn.execute("DELETE FROM meta WHERE key = ?", (f"p115:fail:{info_hash}",))
    conn.commit()
    track_del(info_hash)  # serial pipeline: free the download slot


# ------------------------------------------------------ sweep (backfill) ----
_VIDEO_EXTS = {".mp4", ".mkv", ".wmv", ".avi", ".rmvb", ".rm", ".ts", ".m2ts",
               ".mov", ".flv", ".webm", ".mpg", ".mpeg", ".vob", ".iso"}
_MAIN_MIN_SIZE = 1024 * 1024 * 1024  # 1 GiB: feature film threshold
_ORG_MIN_SIZE = 100 * 1024 * 1024    # real-time floor: tasks are picked on purpose
_SWEEP_LIST_GAP = 55.0               # 115 fs_files throttled to ~1 req/min
_sweep_state = {"running": False, "started_at": 0, "finished_at": 0}


def _list_gap() -> float:
    """Pacing gap between fs_files listings. Configurable via
    P115_LIST_GAP_SEC (0/absent = the built-in default); smaller values are
    an experiment — 115 punishes too-fast requests with EMPTY lists."""
    try:
        v = int(settings.load().get("P115_LIST_GAP_SEC") or 0)
        if v > 0:
            return float(max(5, v))
    except Exception:
        pass
    return _SWEEP_LIST_GAP
_sweep_last_list = 0.0


def _pace_list(sleep_s: float) -> None:
    """Space out fs_files listings: hammered 115 answers empty lists."""
    global _sweep_last_list
    wait = sleep_s - (time.time() - _sweep_last_list)
    if wait > 0:
        time.sleep(wait)
    _sweep_last_list = time.time()


def _sweep_list(c, cid: int, sleep_s: float) -> list:
    _pace_list(sleep_s)
    items, _done = _fs_page(c, cid, 0, 1000)
    return items


def _ext_of(name: str) -> str:
    parts = str(name or "").rsplit(".", 1)
    if len(parts) == 2 and 0 < len(parts[1]) <= 5:
        return f".{parts[1].lower()}"
    return ""


def _item_fid(item: dict):
    # files carry "fid", directories only "cid" in fs_files listings
    fid = (item.get("fid") or item.get("file_id")
           or item.get("cid") or item.get("id"))
    try:
        return int(fid)
    except (TypeError, ValueError):
        return None


def _best_name(conn, folder: str, fname: str) -> str:
    """Best 'CODE Title' for a feature file ('' keeps its own name)."""
    code = extract_code(folder) or extract_code(fname)
    row = _lookup_movie(conn, code)
    if not row:
        return ""
    title = metatube_title(row[0]) or row[1] or ""
    return _sanitize_name(f"{row[0]} {title}").strip()


def _sweep_file(conn, c, fid: int, name: str, size: int,
                target: int, reject: int, stats: dict, *,
                target_path: str = "", prefer_name: str = "") -> bool:
    """One loose file: feature -> '已整理/CODE Title/' subdir, junk -> reject."""
    ext = _ext_of(name)
    base = ""
    if prefer_name:
        base = _sanitize_name(prefer_name)
    else:
        row = _lookup_movie(conn, extract_code(name))
        if row:
            # library hit: rename (this also strips spam glued to the name)
            title = metatube_title(row[0]) or row[1] or ""
            base = _sanitize_name(f"{row[0]} {title}").strip()
        elif looks_ad(name) or ext not in _VIDEO_EXTS or size < _MAIN_MIN_SIZE:
            check_response(c.fs_move(fid, reject, timeout=_TIMEOUT))
            stats["rejected"] += 1
            log.info("115 整理: %s -> 冗余目录", name)
            return True
    # 115 offline tasks name a single-file download after the magnet's dn=
    # ('fit-008ch' — no extension). Renaming that to a bare 'CODE Title'
    # produced an extensionless file sharing its parent dir's EXACT name
    # (已整理/X/X, unplayable and indistinguishable from double nesting).
    # The file already passed the ad gates and carries a library code, so
    # default the missing extension to .mp4.
    if base and not ext:
        ext = ".mp4"
    new = base + ext if base else ""
    if new and new != name and new.rstrip():
        check_response(c.fs_rename((fid, new), timeout=_TIMEOUT))
        log.info("115 重命名: %s -> %s", name, new)
        name = new
    if not base:
        base = (name[:-len(ext)]
                if ext and name.lower().endswith(ext) else name)
    tgt = target
    if target_path and base:
        tgt = resolve_dir(c, f"{target_path}/{base}")
    check_response(c.fs_move(fid, tgt, timeout=_TIMEOUT))
    stats["organized"] += 1
    log.info("115 整理: 正片 %s -> 已整理", name)
    return True


def _sweep_folder(conn, c, fid: int, name: str, target: int, reject: int,
                  sleep_s: float, stats: dict, *, target_path: str = "",
                  prefer_name: str = "",
                  min_size: int = _MAIN_MIN_SIZE) -> bool:
    """One folder: keep the single largest feature video; everything left
    over moves to the reject dir as ONE folder (冗余/<原目录名>/…), so ads
    and promos stay grouped under the download's own name.

    Multi-part folders are kept intact: several codes, files from several
    sub-dirs, or flat CD1/CD2 files sharing one code (extract_code strips
    the -CD1 tail, so they all resolve to the same code) — they file
    directly under 已整理/<原目录名>/, never inside a same-named sub-dir.
    Bundles that carry exactly one file per title are split apart: each
    library-known movie is renamed and filed under its own
    已整理/CODE Title/; unknown titles sink with the ads.

    Returns True when fully handled, False when the listing came back empty
    (115 throttling) and no decision should be made yet.  With target_path
    set ("已整理"), the feature / intact folder lands inside a per-movie
    sub-directory: '已整理/CODE Title/CODE Title.ext'.
    """
    items = _sweep_list(c, fid, sleep_s)
    if not items:
        # a download dir is never truly empty: an empty answer means the
        # listing got throttled — deciding now could junk the feature
        stats["skipped"] = stats.get("skipped", 0) + 1
        log.info("115 整理: %s 列表为空(疑似限流)，本次跳过", name)
        return False
    files, subdirs = [], []
    for it in items:
        sub_fid = _item_fid(it)
        if sub_fid is None:
            continue
        entry = (sub_fid, str(it.get("n") or ""), _to_int(it.get("s")))
        (subdirs if str(it.get("fc", "")) == "0" else files).append(entry)
    # descend one level (CD1/CD2-style layouts), remembering each file's parent
    deep = []
    sub_all_empty = True  # every LISTED subdir came back empty
    listed_subs = 0
    for sub_fid, n_sub, _s in subdirs:
        if looks_ad(n_sub):
            # promo dirs (最新地址 / 网址发布 / 宣传图…) never hold the
            # feature — each skipped descent saves a full listing gap
            continue
        listed_subs += 1
        sub_items = _sweep_list(c, sub_fid, sleep_s)
        if sub_items:
            sub_all_empty = False
        for it in sub_items:
            gfid = _item_fid(it)
            if gfid is None or str(it.get("fc", "")) == "0":
                continue
            deep.append((gfid, str(it.get("n") or ""), _to_int(it.get("s")),
                         sub_fid))
    candidates = [(f, n, s, fid) for f, n, s in files
                  if _ext_of(n) in _VIDEO_EXTS and s >= min_size]
    candidates += [v for v in deep
                   if _ext_of(v[1]) in _VIDEO_EXTS and v[2] >= min_size]
    codes = {extract_code(n) or n for _f, n, _s, _p in candidates}
    parents = {p for _f, _n, _s, p in candidates}
    # flat multi-part files (CD1/CD2 side by side) share a single code and a
    # single parent after tail-stripping — they must stay intact, not be
    # reduced to one file with the rest junked
    multi_part = len(candidates) > 1 and any(
        _is_part_file(n) for _f, n, _s, _p in candidates)

    def nested(base: str) -> int:
        # per-movie sub-dir under the target root; resolve_dir itself joins
        # the fs_files budget on a cache miss, a hit costs nothing
        if not (target_path and base):
            return target
        return resolve_dir(c, f"{target_path}/{base}")

    def junk_shell(label: str) -> bool:
        # Move the emptied shell (ads / leftovers) to reject. Tolerate the
        # move failing — e.g. the 10-min sweep ALREADY junked this shell and
        # 115 refuses a move into the dir the folder already lives in. The
        # movie itself is organized at that point; leftover placement must
        # never wedge the serial slot, so report handled either way.
        try:
            check_response(c.fs_move(fid, reject, timeout=_TIMEOUT))
        except Exception as exc:
            log.warning("115 整理: %s 移入冗余失败（可能已在冗余目录内，忽略）: %s",
                        label, exc)
            return False
        stats["rejected"] += 1
        return True

    if not candidates:
        if not files and listed_subs and sub_all_empty:
            # only empty subdir answers: likely throttled, retry later
            stats["skipped"] = stats.get("skipped", 0) + 1
            log.info("115 整理: %s 子目录列表为空(疑似限流)，本次跳过", name)
            return False
        # nothing feature-sized anywhere (ad-only subdirs are never listed):
        # the whole folder is junk
        if junk_shell(name):
            log.info("115 整理: 文件夹 %s 无正片 -> 冗余目录", name)
        return True
    # group the candidates by code: bundles that carry one file per title
    # can be split apart, each movie filed under its own 已整理/CODE Title/
    groups: dict[str, list] = {}
    for cand in candidates:
        groups.setdefault(extract_code(cand[1]) or cand[1], []).append(cand)
    single_each = all(len(v) == 1 for v in groups.values())
    splittable = len(codes) > 1 and single_each and not multi_part
    if len(codes) == 1 and len(parents) == 1 and not multi_part:
        # keep the single LARGEST candidate: the listing order is arbitrary,
        # so candidates[0] could keep a low-res copy and junk the real 4K
        # feature
        ffid, fname, _size, fsrc = max(candidates, key=lambda v: v[2] or 0)
        ext = _ext_of(fname)
        base = (_sanitize_name(prefer_name) if prefer_name
                else _best_name(conn, name, fname))
        new = base + ext if base else ""
        if new and new != fname:
            check_response(c.fs_rename((ffid, new), timeout=_TIMEOUT))
            log.info("115 重命名: %s -> %s", fname, new)
            fname = new
        if not base:
            base = fname[:-len(ext)] if ext else fname
        tgt = nested(base)
        if fsrc != tgt:
            check_response(c.fs_move(ffid, tgt, timeout=_TIMEOUT))
        stats["organized"] += 1
        log.info("115 整理: 正片 %s -> 已整理", fname)
        # the leftovers (ads, promo images, sub-dirs, the shell itself)
        # travel as ONE folder: 冗余/<原目录名>/ keeps each download's junk
        # grouped (two folders both holding 广告.url must not melt into two
        # same-named loose files at the reject root), and one fs_move beats
        # N under the fs_files rate limit
        if junk_shell(name):
            log.info("115 整理: 正片外的剩余内容随文件夹 %s 整体 -> 冗余目录", name)
        return True
    # multi-title bundle (spam magnets stuff several movies into one
    # download): file each library-known movie under its own
    # 已整理/CODE Title/, renamed by its own entry — a bundled fit-008ch.mp4
    # used to sit inside the intact folder unrenamed; unknown titles and
    # all the ad junk sink with the shell
    filed = 0
    if splittable:
        for ffid, fname, _sz, _par in candidates:
            base = _best_name(conn, fname, fname)
            if not base:
                continue  # not in our library: goes to reject with the shell
            ext = _ext_of(fname)
            new = base + ext
            try:
                if new != fname:
                    check_response(c.fs_rename((ffid, new), timeout=_TIMEOUT))
                    log.info("115 重命名: %s -> %s", fname, new)
                check_response(c.fs_move(ffid, nested(base), timeout=_TIMEOUT))
            except Exception as exc:
                # e.g. that movie was already filed from another download:
                # park the duplicate in reject instead of wedging the folder
                log.warning("115 整理: 拆分归档 %s 失败（%s），该文件转入冗余目录",
                            fname, exc)
                check_response(c.fs_move(ffid, reject, timeout=_TIMEOUT))
                stats["rejected"] += 1
                continue
            filed += 1
            stats["organized"] += 1
            log.info("115 整理: 正片 %s -> 已整理", new)
        if filed:
            if junk_shell(name):
                log.info("115 整理: 捆绑下载 %s 的广告与未识别影片随文件夹整体 -> 冗余目录",
                         name)
            return True
    # CD sets / several cuts / unknown bundles: the folder IS the unit. File
    # it directly under the target root -> 已整理/<原目录名>/…; moving it into
    # a same-named sub-dir duplicated the layer (已整理/X/X/…)
    try:
        check_response(c.fs_move(fid, target, timeout=_TIMEOUT))
    except Exception as exc:
        log.warning("115 整理: 文件夹 %s 移入已整理失败（%s），整体转入冗余目录",
                    name, exc)
        check_response(c.fs_move(fid, reject, timeout=_TIMEOUT))
        stats["rejected"] += 1
        return True
    stats["organized"] += 1
    log.info("115 整理: 多分段/多影片文件夹 %s 整体 -> 已整理", name)
    return True


def sweep_existing(sleep_s: float | None = None, max_folders: int = 0) -> dict:
    """Organize whatever already sits in DOWNLOAD_DIR, ignoring task history.

    Independent from organize_pass (which only sees offline-download tasks
    and skips hashes marked done): it walks the download dir itself.  Every
    folder keeps exactly one feature film (largest video >= 1 GiB), renamed
    to 'CODE Title' and filed as '已整理/CODE Title/CODE Title.ext'; ads,
    promo images, clips and the remaining folder shell move to the reject
    dir as one unit ('冗余/<原目录名>/…').  Multi-part folders are kept
    intact directly under '已整理/<原目录名>/'; bundles with one file per
    title are split, each movie filed under its own sub-directory.  Empty
    listings (115 throttling) skip the item for a later pass.  fs_files
    listings are throttled (~1 req/min on 115); the whole pass never raises.
    """
    stats = {"at": int(time.time()), "scanned": 0, "organized": 0,
             "rejected": 0, "skipped": 0, "errors": 0, "duration_s": 0}
    # Same lock as organize_pass: a task-level organize and a directory sweep
    # racing on one folder both create 已整理/CODE Title/ (115 permits
    # same-named dirs) -> duplicate dirs, duplicate renames/moves.
    if not _organize_lock.acquire(blocking=False):
        log.info("115 整理已在进行中，跳过本次目录扫描")
        stats["busy"] = True
        _sweep_state.update(running=False, finished_at=stats["at"])
        return stats
    t0 = time.time()
    if sleep_s is None:
        sleep_s = _list_gap()
    _sweep_state.update(running=True, started_at=stats["at"], finished_at=0)
    conn = None
    try:
        log.info("115 开始整理（目录扫描）…")
        c = get_client()
        if not c:
            raise RuntimeError("115 未登录")
        conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=30)
        cfg = settings.load()
        dl = resolve_dir(c, (cfg.get("P115_DOWNLOAD_DIR") or "").strip() or "待整理")
        target_name = (cfg.get("P115_TARGET_DIR") or "").strip() or "已整理"
        target = resolve_dir(c, target_name)
        reject = resolve_dir(c, (cfg.get("P115_REJECT_DIR") or "").strip() or "冗余")
        items = _sweep_list(c, dl, sleep_s)
        if not items:
            # throttled first listing: say so instead of failing silently
            log.info("115 整理: 下载目录列表为空（可能限流），本次跳过")
        for item in items:
            if max_folders and stats["scanned"] >= max_folders:
                break
            ifid = _item_fid(item)
            name = str(item.get("n") or "")
            if ifid is None or not name:
                continue
            stats["scanned"] += 1
            try:
                if str(item.get("fc", "")) == "0":
                    _sweep_folder(conn, c, ifid, name, target, reject,
                                  sleep_s, stats, target_path=target_name)
                else:
                    _sweep_file(conn, c, ifid, name, _to_int(item.get("s")),
                                target, reject, stats,
                                target_path=target_name)
            except Exception as exc:
                stats["errors"] += 1
                log.warning("115 整理 %s 失败: %s", name, exc)
    except Exception as exc:
        stats["errors"] += 1
        stats["error"] = str(exc)
        log.warning("115 整理中断: %s", exc)
    finally:
        if conn is not None:
            conn.close()
        stats["at"] = int(time.time())
        stats["duration_s"] = int(time.time() - t0)
        _sweep_state.update(running=False, finished_at=stats["at"])
        # always log the outcome, even an all-skipped (throttled) pass
        log.info("115 整理完成: 扫描 %d · 影片 %d · 冗余 %d · 跳过 %d · 错误 %d · 耗时 %d 秒",
                 stats["scanned"], stats["organized"], stats["rejected"],
                 stats["skipped"], stats["errors"], stats["duration_s"])
        try:
            conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=30)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                    ("p115:last_sweep", json.dumps(stats, ensure_ascii=False)))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            log.exception("保存整理结果失败")
        _organize_lock.release()
    return stats


def start_sweep() -> tuple[bool, str]:
    """Kick sweep_existing in a background thread; refuse when busy/logged out."""
    if _sweep_state.get("running"):
        return False, "整理已在进行中"
    if not has_auth():
        return False, "115 未登录"
    _sweep_state["running"] = True  # claim now to fence double-clicks
    threading.Thread(target=sweep_existing, daemon=True).start()
    return True, ""


def sweep_busy() -> bool:
    """True while a sweep pass is running (manual button or auto loop)."""
    return bool(_sweep_state.get("running"))


def sweep_status() -> dict:
    """Running flag + last stored sweep result (for the UI)."""
    out = {"running": bool(_sweep_state.get("running")),
           "started_at": _sweep_state.get("started_at") or 0}
    try:
        conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=10)
        try:
            raw = db_get_meta(conn, "p115:last_sweep")
        finally:
            conn.close()
        data = json.loads(raw)
        if isinstance(data, dict):
            out["result"] = data
    except (ValueError, sqlite3.Error):
        pass
    return out
