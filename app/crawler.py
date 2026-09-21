"""Shared crawl engine used by both the CLI and the web UI."""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlencode

from . import db
from .fetcher import Fetcher, StopRequested
from .parser import (magnet_hash, parse_detail, parse_genre_catalog,
                     parse_list, parse_movie_script_vars, parse_magnets)

log = logging.getLogger("seedmm.crawl")

# JavBus-family sites fill #magnet-table via this AJAX endpoint
# (relative to the site root; params come from the detail page's
# inline "var gid/uc/img" script, see parser.parse_movie_script_vars).
MAGNET_AJAX_PATH = "ajax/uncledatoolsbyajax.php"


def magnet_ajax_path(vars_: dict[str, str]) -> str:
    qs = urlencode({
        "gid": vars_["gid"],
        "lang": vars_.get("lang", "zh"),
        "img": vars_.get("img", ""),
        "uc": vars_.get("uc", "0"),
        "floor": random.randint(1, 1000),
    })
    return f"{MAGNET_AJAX_PATH}?{qs}"


MAX_PAGES_PER_RUN = 500

# site genre filter slugs must look like this (digits or short slugs like "hd")
_GENRE_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")


def parse_genre_list(spec) -> list[str]:
    """'42,hd' -> ['42', 'hd']; invalid entries are dropped."""
    return [g for g in (s.strip() for s in str(spec or "").split(","))
            if _GENRE_RE.fullmatch(g)]


def listing_path(section: str, genre: str, page: int) -> str:
    """URL path for a listing page, optionally filtered by a site genre."""
    if genre:
        return f"{section}/genre/{genre}/{page}"
    return f"{section}/page/{page}"


def genre_name_map(conn) -> dict[tuple[str, str], str]:
    """(channel, site-genre-id) -> display name, for human-readable labels.

    Sources: the cached site catalog (genre_catalog_v2, authoritative) plus
    the mappings learned while crawling detail pages (genre_id:<cat>:<name>
    rows) — together they cover ids the cached catalog no longer lists.
    """
    out: dict[tuple[str, str], str] = {}
    try:
        catalog = json.loads(db.get_meta(conn, "genre_catalog_v2", "") or "{}")
    except ValueError:
        catalog = {}
    for cat in ("censored", "uncensored"):
        for grp in (catalog.get(cat) or []):
            for g in (grp.get("genres") or []):
                gid, name = str(g.get("id") or ""), str(g.get("name") or "")
                if gid and name:
                    out.setdefault((cat, gid), name)
    try:
        rows = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'genre_id:%'").fetchall()
    except Exception:
        rows = []
    for key, gid in rows:
        parts = key.split(":", 2)
        if len(parts) == 3 and str(gid).strip():
            out.setdefault((parts[1], str(gid)), parts[2])
    return out


def looks_blocked(html: str) -> bool:
    """Detect the site's age-verification/risk-control interstitial (it
    answers HTTP 200, so an empty parse alone would be misleading)."""
    return "driver-verify" in html or "Age Verification" in html


def fetch_genre_catalog(cfg: dict) -> tuple[dict, dict]:
    """Fetch both channels' genre catalogs from the site index pages.

    Uses the crawl Fetcher (full browser headers via a persistent session,
    PROXY support, retries) — bare requests get gated by the site's
    age-verification wall.

    Returns (catalog, status): catalog is {"censored": [{"group", "genres":
    [{"name", "id"}]}], "uncensored": [...]}; status is a per-channel
    human-readable error string (empty on success).
    """
    from .fetcher import Fetcher

    fetcher = Fetcher(
        base_url=cfg["BASE_URL"],
        delay=cfg["DELAY_SECONDS"],
        jitter=cfg["JITTER_SECONDS"],
        max_retries=cfg["MAX_RETRIES"],
        timeout=min(cfg["TIMEOUT"], 20),
        proxy=(cfg.get("PROXY") or "").strip(),
    )
    out: dict = {"censored": [], "uncensored": []}
    status = {"censored": "", "uncensored": ""}
    for cat, path in (("censored", "/genre"), ("uncensored", "/uncensored/genre")):
        html = fetcher.get(path)
        if not html:
            status[cat] = "无法连接站点（检查网络 / 代理设置）"
        elif looks_blocked(html):
            status[cat] = "站点拦截了目录页（年龄验证风控），请稍后点「刷新类别」重试"
        else:
            out[cat] = parse_genre_catalog(html)
            if not out[cat]:
                status[cat] = "目录页解析结果为空（站点结构可能变化）"
    return out, status


def parse_pages(spec: str) -> list[int]:
    """'3' -> [3]; '2-5' -> [2,3,4,5]. Range capped at MAX_PAGES_PER_RUN."""
    m = re.fullmatch(r"(\d+)(?:-(\d+))?", str(spec).strip())
    if not m:
        raise ValueError(f"页码格式无效: {spec!r}（示例: '3' 或 '2-5'）")
    start = int(m.group(1))
    end = int(m.group(2) or start)
    if start < 1 or end < start:
        raise ValueError(f"页码范围无效: {spec!r}")
    if end - start + 1 > MAX_PAGES_PER_RUN:
        raise ValueError(f"单次最多 {MAX_PAGES_PER_RUN} 页，收到 {end - start + 1} 页")
    return list(range(start, end + 1))


def compute_matched(tags: list[str], magnet_names: list[str], keywords: list[str]) -> list[str]:
    """Return the filter keywords hit by genre tags or magnet link names.

    Comparison is case-insensitive (magnet names mix "4K"/"4k" freely);
    the returned keywords keep their original spelling for display.
    """
    tags_l = [t.lower() for t in tags]
    names_l = [n.lower() for n in magnet_names]
    hits = []
    for kw in keywords:
        k = kw.strip()
        if not k:
            continue
        kl = k.lower()
        if any(kl in t for t in tags_l) or any(kl in n for n in names_l):
            hits.append(k)
    return hits


def parse_tiebreak(spec) -> list[str]:
    """'size,date' -> ['size', 'date']; unknown tokens raise ValueError."""
    parts = [p.strip().lower() for p in str(spec or "").split(",") if p.strip()]
    for p in parts:
        if p not in ("size", "date"):
            raise ValueError(f"MAGNET_TIEBREAK 含无效规则: {p!r}（可选 size / date）")
    # dedupe, keep order
    return list(dict.fromkeys(parts))


def parse_fallback(spec) -> str:
    """Normalize MAGNET_FALLBACK: first (newest) | largest | none."""
    fb = str(spec or "").strip().lower()
    return fb if fb in ("first", "largest", "none") else "first"


_SIZE_RE = re.compile(r"([\d.]+)\s*(tb|gb|mb|kb|b)", re.I)
_SIZE_UNITS = {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3, "tb": 1024 ** 4}


def parse_size(spec) -> float:
    """'5.23GB' -> bytes (float); unparseable -> 0 (treated as smallest)."""
    m = _SIZE_RE.search(str(spec or ""))
    if not m:
        return 0.0
    try:
        return float(m.group(1)) * _SIZE_UNITS[m.group(2).lower()]
    except ValueError:
        return 0.0


def _date_ordinal(spec) -> int:
    """ISO date string -> sortable day number; unparseable -> 0 (oldest)."""
    try:
        from datetime import date

        return date.fromisoformat(str(spec or "").strip()).toordinal()
    except ValueError:
        return 0


def _score_key(idx: int, name: str, size, date, kws: list, tb: list) -> tuple | None:
    """Composite sort key for one magnet; None when no keyword hits.

    Scoring: the magnet matching MORE keywords wins (中文+高清 beats 中文-only);
    ties go to the higher-priority keyword (earlier in `keywords`), then to the
    configured tiebreakers ("size": bigger first, "date": newer first), and
    finally to the earlier list position (newest first).
    """
    n = (name or "").lower()
    hits = [ki for ki, k in kws if k in n]
    if not hits:
        return None
    key = [-len(hits), hits[0]]
    for t in tb:
        if t == "size":
            key.append(-parse_size(size))
        elif t == "date":
            key.append(-_date_ordinal(date))
    key.append(idx)
    return tuple(key)


def _pick_index(names: list[str], keywords: list[str],
                tiebreak: list[str] | None = None,
                sizes: list | None = None, dates: list | None = None,
                fallback: str = "first") -> int | None:
    """Rank magnet names and return the best index (None = no keyword hit).

    When NOTHING matches, the fallback decides: "first" -> list head (newest),
    "largest" -> biggest size, "none" -> return None meaning "pick nothing".
    """
    kws = [(i, k.strip().lower()) for i, k in enumerate(keywords) if k.strip()]
    tb = tiebreak or []
    sizes = sizes or []
    dates = dates or []
    best_i: int | None = None
    best_key: tuple | None = None
    for idx, name in enumerate(names):
        key = _score_key(idx, name,
                         sizes[idx] if idx < len(sizes) else "",
                         dates[idx] if idx < len(dates) else "", kws, tb)
        if key is None or (best_key is not None and key >= best_key):
            continue
        best_key, best_i = key, idx
    if best_i is not None:
        return best_i
    if not names:
        return None
    if fallback == "largest":
        li, lkey = 0, -parse_size(sizes[0] if sizes else "")
        for idx in range(1, len(names)):
            k = -parse_size(sizes[idx] if idx < len(sizes) else "")
            if k < lkey:
                li, lkey = idx, k
        return li
    return None if fallback == "none" else 0


def _mfield(m, key: str) -> str:
    """Read a field from a magnet dict OR an attribute-style object."""
    if isinstance(m, dict):
        return str(m.get(key) or "")
    return str(getattr(m, key, "") or "")


def rank_magnets(magnets: list, keywords: list[str],
                 tiebreak: list[str] | None = None,
                 fallback: str = "first") -> list[dict]:
    """Rank ALL magnets best-first, with per-magnet hit details for the UI.

    Same scoring as _pick_index (keyword hits > keyword order > tiebreakers >
    list position). Unmatched magnets go after the matched ones, ordered by
    the fallback ("first" keeps list order, "largest" sorts by size desc).
    rank 1 is the overall winner — unless nothing matched and fallback is
    "none", in which case NO row is marked best (nothing gets picked).
    Each item: {name, size, date, hits, hit_count, rank, best}.
    """
    if not magnets:
        return []
    kws = [(i, k.strip()) for i, k in enumerate(keywords) if k.strip()]
    tb = list(tiebreak or [])
    entries = []
    for idx, m in enumerate(magnets):
        name, size, date = _mfield(m, "name"), _mfield(m, "size"), _mfield(m, "date")
        hits = [(i, k) for i, k in kws if k.lower() in name.lower()]
        key = _score_key(idx, name, size, date,
                         [(i, k.lower()) for i, k in kws], tb) if hits else None
        entries.append({"name": name, "size": size, "date": date,
                        "hits": [k for _i, k in hits], "key": key})
    matched = sorted((e for e in entries if e["key"] is not None),
                     key=lambda e: e["key"])
    rest = [e for e in entries if e["key"] is None]
    if not matched and fallback == "largest":
        rest.sort(key=lambda e: -parse_size(e["size"]))
    ranked = matched + rest
    pick_none = not matched and fallback == "none"
    for rank, e in enumerate(ranked, 1):
        e["rank"] = rank
        e["best"] = rank == 1 and not pick_none
        e["hit_count"] = len(e["hits"])
        del e["key"]
    return ranked


def pick_magnet(magnets: list[dict], keywords: list[str],
                tiebreak: list[str] | None = None,
                fallback: str = "first") -> tuple[dict | None, str]:
    """Pick the single magnet to use, honouring keyword quality then priority.

    A magnet matching several keywords (e.g. 中文 AND 高清) beats one matching
    a single keyword; among equals the earlier keyword has priority, then the
    configured tiebreakers (size bigger-first / date newer-first), and the
    list order (newest first) breaks remaining ties. When no keyword matches
    any magnet name, the fallback applies: first (list head), largest
    (biggest size) or none (return (None, '')).
    Returns (magnet-or-None, matched-keyword-or-empty-string).
    """
    if not magnets:
        return None, ""
    names = [m.get("name") or "" for m in magnets]
    i = _pick_index(names, keywords, tiebreak,
                    [m.get("size") or "" for m in magnets],
                    [m.get("date") or "" for m in magnets], fallback)
    if i is None:
        return None, ""
    kws = [k.strip() for k in keywords if k.strip()]
    n = names[i].lower()
    matched = next((k for k in kws if k.lower() in n), "")
    return magnets[i], matched


def pick_best(magnets: list, keywords: list[str],
              tiebreak: list[str] | None = None,
              fallback: str = "first") -> list:
    """Keep only the single best magnet for storage.

    Uses the same scoring as pick_magnet: most keyword hits first (中文+高清
    beats 中文-only), then keyword priority, then the configured tiebreakers,
    then list order. No match -> the configured fallback ("none" keeps NO
    magnet, so the movie is stored without one). Filtering/marking should
    still be computed on the FULL list before calling this.
    """
    if not magnets:
        return []
    names = [_mfield(m, "name") for m in magnets]
    i = _pick_index(names, keywords, tiebreak,
                    [_mfield(m, "size") for m in magnets],
                    [_mfield(m, "date") for m in magnets], fallback)
    return [] if i is None else [magnets[i]]


_QUEUED_PREFIX = "dl:queued:"

# serial pipeline: one download (through organize) at a time. The crawl
# thread and the organize loop's retry_queued can both try to grab the free
# slot at the same moment — this lock makes the check-and-submit atomic.
_SUBMIT_LOCK = threading.Lock()
_SERIAL_POLL_S = 15          # how often the settle loop re-checks the pipeline
_SERIAL_WAIT_MAX_S = 6 * 3600  # hard cap so a wedged pipeline can't hang a crawl
# auto_download outcomes that mean "this movie is (or will be) in the
# pipeline" — run_job must hold the crawl until it settles
_AUTO_WAIT = {"submitted", "tracked", "busy", "cooldown"}


def _queued_update(cfg: dict, code: str, queued: bool) -> None:
    """Persist (or drop) a "wants download, serial slot busy" mark.

    Best-effort: a failed mark write must never break the crawl.
    """
    try:
        conn = db.connect(cfg["DB_PATH"])
        try:
            if queued:
                db.set_meta(conn, _QUEUED_PREFIX + code, "1")
            else:
                conn.execute("DELETE FROM meta WHERE key = ?",
                             (_QUEUED_PREFIX + code,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        log.debug("dl:queued 标记更新失败 %s", code, exc_info=True)


def auto_download(cfg: dict, movie) -> str:
    """AUTO_DOWNLOAD: submit the stored best magnet of a movie to 115.

    Best-effort: never raises, so a 115 failure cannot break the crawl.
    Returns what happened so a caller in wait mode knows whether this movie
    is now in the pipeline: "off"/"skip" nothing pending, "submitted" just
    submitted, "tracked" already downloading, "busy"/"cooldown" the serial
    slot or the interval gate deferred it (a dl:queued:{code} mark is
    persisted so retry_queued() can pick the movie back up once it frees).
    """
    if not cfg.get("AUTO_DOWNLOAD") or not getattr(movie, "magnets", None):
        return "off"
    from . import p115  # deferred: p115 imports this module at load time

    try:
        if not p115.has_auth():
            log.info("%s: 自动云下载已开启但 115 未登录，跳过", movie.code)
            return "skip"
        recs = p115.track_records()
        if any(r.get("code") == movie.code for r in recs.values()):
            _queued_update(cfg, movie.code, False)  # tracked: mark served
            log.info("%s: 已有云下载任务，跳过重复提交", movie.code)
            return "tracked"
        if recs:  # serial pipeline: one download (through organize) at a time
            _queued_update(cfg, movie.code, True)
            log.info("%s: 上一个云下载还未完成整理，稍后自动提交（串行）",
                     movie.code)
            return "busy"
        interval = max(0, int(cfg.get("P115_DL_INTERVAL_SEC", 0)))
        if interval:  # cooldown after the last movie landed in the library
            wait = interval - (int(time.time()) - p115.last_release_at())
            if wait > 0:
                _queued_update(cfg, movie.code, True)
                log.info("%s: 上一部整理完成后冷却中，约 %d 秒后自动提交（串行间隔）",
                         movie.code, wait)
                return "cooldown"
        if movie.code in p115.gaveup_codes():
            _queued_update(cfg, movie.code, False)  # dead magnets: stop retrying
            log.info("%s: 该番号磁力已全部失败过，跳过", movie.code)
            return "skip"
        with _SUBMIT_LOCK:
            # re-check under the lock: retry_queued (organize loop) may have
            # grabbed the slot for this or another code between the checks
            # above and here
            recs = p115.track_records()
            if any(r.get("code") == movie.code for r in recs.values()):
                _queued_update(cfg, movie.code, False)
                return "tracked"
            if recs:
                _queued_update(cfg, movie.code, True)
                return "busy"
            m = movie.magnets[0]
            log.info("开始云下载: %s (%s)", movie.code, m.name[:60])
            result = p115.add_magnet(m.link)
            p115.track_add(movie.code, result.get("info_hash") or magnet_hash(m.link),
                           m.link, result.get("name") or "")
            _queued_update(cfg, movie.code, False)
            log.info("已提交 115 离线任务: %s (%s)",
                     result.get("name") or m.name, movie.code)
            return "submitted"
    except Exception:
        log.exception("%s: 自动云下载失败（不影响采集入库）", movie.code)
        return "skip"


def _stored_movie(cfg: dict, code: str):
    """Rebuild the SimpleNamespace auto_download expects from the DB row,
    or None when the movie/its magnets are gone."""
    try:
        conn = db.connect(cfg["DB_PATH"])
        try:
            stored = db.get_movie(conn, code)
        finally:
            conn.close()
    except Exception:
        log.exception("%s: 读取入库记录失败", code)
        return None
    if not stored or not stored.get("magnets"):
        return None
    return SimpleNamespace(
        code=code,
        magnets=[SimpleNamespace(link=m["link"], name=m.get("name") or "")
                 for m in stored["magnets"]])


def _serial_settle(cfg: dict, code: str, stop_check=None) -> None:
    """Hold the crawl until `code` has been through download AND organize.

    The pipeline rule the user asked for: collect one movie, wait until it
    landed in the 115 library, then collect the next. Every terminal path
    in p115 (organized, organize give-up, magnets exhausted, task vanished,
    organize disabled) releases the track record, so "code gone from
    track_records" is the settle signal. Raises StopRequested when the user
    stops the crawl; gives up after _SERIAL_WAIT_MAX_S so a wedged pipeline
    cannot stall a crawl forever.
    """
    from . import p115  # deferred: p115 imports this module at load time

    deadline = time.time() + _SERIAL_WAIT_MAX_S
    seen = False      # our code entered the pipeline at least once
    gone = 0          # consecutive polls with the record absent (after seen)
    attempts = 0      # consecutive failed (re)submits in the free-slot branch
    ih_last = ""      # our code's last known info_hash (done-marker lookup)
    last = ""
    while time.time() < deadline:
        if stop_check is not None and stop_check():
            raise StopRequested()
        recs = p115.track_records()
        state = ""
        mine = {k: r for k, r in recs.items() if r.get("code") == code}
        if mine:
            seen = True
            gone = 0
            ih_last = next(iter(mine))
            state = "云下载/整理进行中"
        elif seen:
            # The record left the pipeline. _swap_magnet briefly deletes then
            # re-adds it while switching magnets, so require two clean polls;
            # and a vanished record only counts as organized once p115:done
            # exists — watch_pass may have released the slot because the task
            # fell out of the 115 list, which is no proof anything landed.
            gone += 1
            if gone < 2:
                state = "整理刚完成，确认槽位释放"
            elif code in p115.gaveup_codes():
                log.warning("%s: 下载任务已终止（磁力全部失败或任务被移除），"
                            "继续采集下一部", code)
                return
            elif cfg.get("P115_AUTO_ORGANIZE") and ih_last \
                    and not p115.is_done(ih_last):
                state = "任务记录消失但未见整理完成，继续等待"
            else:
                return
        elif code in p115.gaveup_codes():
            log.info("%s: 磁力全部失败，继续采集下一部", code)
            return
        elif recs:
            state = "串行槽被其他下载占用"
        else:
            interval = max(0, int(cfg.get("P115_DL_INTERVAL_SEC", 0)))
            wait = interval - (int(time.time()) - p115.last_release_at())
            if wait > 0:
                state = f"串行间隔冷却中（约 {wait} 秒）"
            else:
                # slot free but our movie not tracked: the first
                # auto_download hit busy/cooldown — submit the stored record
                stored = _stored_movie(cfg, code)
                if stored is None:
                    log.warning("%s: 入库记录或磁力丢失，跳过串行等待", code)
                    return
                status = auto_download(cfg, stored)
                if status in ("submitted", "tracked"):
                    attempts = 0
                    state = "已顺延提交，等待下载整理"
                else:
                    attempts += 1
                    if attempts >= 20:  # ~5 min of failures: stop hogging
                        log.error("%s: 串行提交反复失败，继续采集下一部", code)
                        return
                    state = f"提交未成功（{status}），稍后重试"
        if state and state != last:
            log.info("%s: %s，整理完毕后继续下一部", code, state)
        last = state
        time.sleep(_SERIAL_POLL_S)
    log.error("%s: 等待云下载+整理超过 %d 小时，放弃等待继续采集",
              code, _SERIAL_WAIT_MAX_S // 3600)


def retry_queued(cfg: dict) -> int:
    """AUTO_DOWNLOAD follow-up: resubmit movies the serial slot skipped.

    auto_download only fires while a movie lands in the DB; with the
    pipeline busy it logs "稍后自动提交" and leaves a dl:queued mark.
    The organize loop calls this once per minute to keep that promise.
    Returns how many movies were actually submitted this pass.
    """
    if not cfg.get("AUTO_DOWNLOAD"):
        return 0
    from . import p115  # deferred: p115 imports this module at load time

    try:
        conn = db.connect(cfg["DB_PATH"])
        try:
            rows = conn.execute("SELECT key FROM meta WHERE key LIKE ?",
                                (_QUEUED_PREFIX + "%",)).fetchall()
        finally:
            conn.close()
    except Exception:
        log.exception("待提交队列读取失败")
        return 0
    codes = [r[0][len(_QUEUED_PREFIX):] for r in rows]
    if not codes:
        return 0
    submitted = 0
    for code in codes:
        try:
            stored = _stored_movie(cfg, code)
            if stored is None:
                _queued_update(cfg, code, False)  # movie gone: drop the mark
                continue
            before = {r.get("code") for r in p115.track_records().values()}
            auto_download(cfg, stored)  # re-runs serial/cooldown guards
            if code not in before and code in {
                    r.get("code") for r in p115.track_records().values()}:
                submitted += 1
                log.info("待提交队列: %s 已顺延提交（串行 %d 完成）", code, submitted)
        except Exception:
            log.exception("待提交队列处理失败 %s", code)
    return submitted


def run_job(
    cfg: dict,
    pages: str = "1",
    magnets: bool = True,
    refresh: bool = False,
    mode: str = "new",
    stop_check=None,
) -> dict:
    """Run one crawl job with a config snapshot. Returns stats.

    mode: "new"      – crawl the given page range from page 1 (catch up on new
                       releases; already-known codes are skipped)
          "backfill" – continue the history: auto-start after the deepest page
                       crawled so far (per category); `pages` is the number of
                       pages to walk this run. Stops early at the site's end.
    """
    fetcher = Fetcher(
        base_url=cfg["BASE_URL"],
        delay=cfg["DELAY_SECONDS"],
        jitter=cfg["JITTER_SECONDS"],
        max_retries=cfg["MAX_RETRIES"],
        timeout=cfg["TIMEOUT"],
        proxy=(cfg.get("PROXY") or "").strip(),
        stop_check=stop_check,
    )
    sections = {"censored": "", "uncensored": "/uncensored"}
    cats = [c.strip() for c in str(cfg.get("CATEGORY", "censored")).split(",")
            if c.strip() in sections] or ["censored"]
    genres_by_cat = {c: parse_genre_list(cfg.get(f"GENRE_{c.upper()}")) for c in sections}
    # an empty genre list means "do not crawl this channel" (the UI requires
    # an explicit selection); crawl-everything is no longer an implicit default
    combos: list[tuple[str, str]] = []
    for c in cats:
        if not genres_by_cat[c]:
            log.warning("类别筛选为空，跳过 %s 频道（在「采集设置 → 类别筛选」选择类别后才会采集）",
                        "无码" if c == "uncensored" else "有码")
            continue
        combos.extend((c, g) for g in genres_by_cat[c])
    if not combos:
        raise ValueError("类别筛选为空：请先在「采集设置 → 类别筛选」中选择要采集的类别")

    gnames: dict[tuple[str, str], str] = {}  # populated once conn is open

    def combo_label(c: str, g: str) -> str:
        base = "无码" if c == "uncensored" else "有码"
        if not g:
            return base
        name = gnames.get((c, g))
        return f"{base}/{name}" if name else f"{base}/类别{g}"

    def depth_key(c: str, g: str) -> str:
        return f"max_page:{c}:{g}" if g else f"max_page:{c}"

    conn = db.connect(cfg["DB_PATH"])
    gnames.update(genre_name_map(conn))
    known = db.known_codes(conn)
    depths = {depth_key(c, g): int(db.get_meta(conn, depth_key(c, g), "0") or 0)
              for c, g in combos}

    if mode == "backfill":
        try:
            count = int(str(pages).strip())
        except ValueError:
            count = len(parse_pages(pages))  # e.g. "2-5" -> 4 pages
        count = max(1, min(count, 500))
        plans = []
        for c, g in combos:
            dk = depth_key(c, g)
            if depths[dk] <= 0:
                log.warning("补历史跳过 %s：尚无采集深度记录，请先用「追新」采集第 1 页",
                            combo_label(c, g))
                continue
            plans.append((c, g, list(range(depths[dk] + 1, depths[dk] + 1 + count))))
            log.info("补历史 %s: 从第 %d 页继续，本次 %d 页（当前深度 %d）",
                     combo_label(c, g), depths[dk] + 1, count, depths[dk])
        if not plans:
            raise ValueError("尚无采集深度记录：请先用「追新」模式采集第 1 页")
    else:
        page_list = parse_pages(pages)
        mode = "new"
        plans = [(c, g, page_list) for c, g in combos]
    log.info("开始采集: 模式=%s 频道=%s pages=%s base=%s 已入库=%d delay=%.1fs",
             "补历史" if mode == "backfill" else "追新",
             "/".join(combo_label(c, g) for c, g in combos), pages, fetcher.base_url,
             len(known), fetcher.delay)

    stats = {"pages": 0, "listed": 0, "new": 0, "updated": 0,
             "magnets": 0, "errors": 0, "skipped": 0, "stopped": False}

    try:
        for cat, genre, page_list in plans:
            section = sections[cat]
            dk = depth_key(cat, genre)
            for page in page_list:
                html = fetcher.get(listing_path(section, genre, page))
                if not html:
                    log.error("第 %d 页抓取失败，跳过", page)
                    stats["errors"] += 1
                    continue
                if looks_blocked(html):
                    log.error("第 %d 页触发站点年龄验证/风控（driver-verify），"
                              "请降低采集频率稍后重试", page)
                    stats["errors"] += 1
                    break
                items = parse_list(html, fetcher.base_url)
                if not items:
                    log.warning("第 %d 页没有条目，已到站点尽头，停止补历史", page)
                    break
                if page > depths[dk]:
                    depths[dk] = page
                    db.set_meta(conn, dk, depths[dk])
                    conn.commit()
                stats["pages"] += 1
                stats["listed"] += len(items)
                log.info("[%s] 第 %d 页: %d 部影片", combo_label(cat, genre), page, len(items))

                for item in items:
                    if not refresh and item.code in known:
                        continue
                    try:
                        detail_html = fetcher.get(item.url)
                        if not detail_html:
                            stats["errors"] += 1
                            continue
                        if looks_blocked(detail_html):
                            log.error("%s: 详情页触发站点年龄验证/风控（driver-verify），"
                                      "请降低采集频率稍后重试", item.code)
                            stats["errors"] += 1
                            break

                        movie = parse_detail(detail_html, item.code, item.url)
                        if not movie.title:
                            # blocked/shell pages answer HTTP 200, so an empty
                            # title is the only tell; storing it would poison
                            # the DB and this code would never be retried
                            # (it lands in known below)
                            log.warning("%s: 详情页解析为空标题，疑似风控页，本次不入库"
                                        "（下次追新自动重试）", item.code)
                            stats["errors"] += 1
                            continue
                        movie.category = cat
                        magnets_fetched = False
                        if magnets:
                            sv = parse_movie_script_vars(detail_html)
                            if sv.get("gid"):
                                frag = fetcher.get(magnet_ajax_path(sv), referer=item.url)
                                if not frag:
                                    # magnet fetch failed — an empty list here
                                    # would wrongly count as "no keyword match"
                                    # and make only-mode skip (and mislog) the
                                    # movie; an empty body counts as failure too
                                    log.warning("%s: 磁力列表抓取失败，本次不入库（下次追新自动重试）",
                                                item.code)
                                    stats["errors"] += 1
                                    continue
                                movie.magnets = parse_magnets(frag)
                                magnets_fetched = True
                            else:
                                log.warning("%s: 未找到 gid 参数，跳过磁力", item.code)

                        # tag filtering: match genres + magnet names against keywords
                        keywords = [k for k in str(cfg.get("TAG_FILTERS") or "").split(",") if k.strip()]
                        try:
                            tiebreak = parse_tiebreak(cfg.get("MAGNET_TIEBREAK"))
                        except ValueError:
                            tiebreak = ["size", "date"]
                        fallback = parse_fallback(cfg.get("MAGNET_FALLBACK"))
                        filter_mode = cfg.get("TAG_FILTER_MODE", "mark")
                        if keywords:
                            movie.matched_tags = compute_matched(
                                movie.genres, [m.name for m in movie.magnets], keywords)
                            if filter_mode == "only" and not movie.matched_tags:
                                sample = " / ".join(m.name for m in movie.magnets[:3])
                                if fallback == "none":
                                    stats["skipped"] += 1
                                    log.info("跳过 %s（磁力 %d 条%s，无关键词命中: %s）",
                                             item.code, len(movie.magnets),
                                             f"，如: {sample}" if sample else "",
                                             ",".join(keywords))
                                    continue
                                log.info("%s 无关键词命中，按兜底策略(%s)保留磁力%s",
                                         item.code, fallback,
                                         f": {sample}" if sample else "")

                        # learn per-channel genre name -> site id mapping
                        for gcat, gname, gid in movie.genre_links:
                            db.set_meta(conn, f"genre_id:{gcat}:{gname}", gid)

                        # store ONE magnet per movie: the best pick by keyword
                        # priority + tiebreakers (matching used the full list)
                        movie.magnets = pick_best(movie.magnets, keywords, tiebreak, fallback)

                        is_new = item.code not in known
                        if refresh and magnets_fetched:
                            # keep the table in sync with what we just fetched
                            db.delete_magnets(conn, movie.code)
                        db.upsert_movie(conn, movie)
                        stats["magnets"] += db.insert_magnets(conn, movie.magnets, movie.code)
                        conn.commit()
                        known.add(item.code)
                        stats["new" if is_new else "updated"] += 1
                        log.info("%s %s 磁力=%d %s",
                                 "新增" if is_new else "更新", movie.code,
                                 len(movie.magnets), movie.title[:40])
                    except StopRequested:
                        # cooperative stop is control flow, not a per-movie
                        # failure — re-raise or the remaining items on this
                        # page all cascade through as fake errors
                        raise
                    except Exception:
                        # isolate one bad movie: roll back the half-done
                        # transaction (old magnets deleted, new ones missing)
                        # and keep the batch going
                        conn.rollback()
                        stats["errors"] += 1
                        log.warning("%s: 单条处理失败，跳过该影片 (%s)",
                                    item.code, item.url, exc_info=True)
                        continue
                    if auto_download(cfg, movie) in _AUTO_WAIT:
                        # one movie at a time: hold the crawl until this
                        # download finished organizing, then collect the next
                        _serial_settle(cfg, movie.code, stop_check)
    except StopRequested:
        stats["stopped"] = True
        log.warning("采集已被用户停止")
    except Exception:
        # a mid-write crash (e.g. database is locked) must not commit the
        # half-done transaction (old magnets deleted, new ones missing)
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    else:
        conn.commit()
    finally:
        conn.close()
        log.info("结束: %(pages)d 页, %(new)d 新增, %(updated)d 更新, "
                 "%(magnets)d 磁力, %(errors)d 错误, %(skipped)d 跳过", stats)
    return stats


_CODE_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z_-]{0,30}")


def _fetch_code_movie(fetcher: Fetcher, code: str, with_magnets: bool = True):
    """Fetch a single movie page (censored path first, then /uncensored).

    Pure fetch: no DB writes. Returns the Movie or None when not found.
    """
    for section, cat in (("", "censored"), ("/uncensored", "uncensored")):
        html = fetcher.get(f"{section}/{code}")
        if not html:
            continue
        movie = parse_detail(html, code, f"{fetcher.base_url}{section}/{code}")
        if not movie.title:
            continue  # shell/redirect page, try the next section
        movie.category = cat
        if with_magnets:
            sv = parse_movie_script_vars(html)
            if sv.get("gid"):
                frag = fetcher.get(magnet_ajax_path(sv), referer=movie.url)
                movie.magnets = parse_magnets(frag) if frag else []
        return movie
    return None


def _make_fetcher(cfg: dict, stop_check=None) -> Fetcher:
    return Fetcher(
        base_url=cfg["BASE_URL"],
        delay=cfg["DELAY_SECONDS"],
        jitter=cfg["JITTER_SECONDS"],
        max_retries=cfg["MAX_RETRIES"],
        timeout=cfg["TIMEOUT"],
        proxy=(cfg.get("PROXY") or "").strip(),
        stop_check=stop_check,
    )


def crawl_code(cfg: dict, code: str, magnets: bool = True, stop_check=None) -> dict:
    """Crawl a single movie by code (tries the censored path, then /uncensored)."""
    code = code.strip().upper()
    if not _CODE_RE.fullmatch(code):
        raise ValueError(f"番号格式无效: {code!r}（示例: BANK-248）")
    fetcher = _make_fetcher(cfg, stop_check)
    conn = db.connect(cfg["DB_PATH"])
    try:
        known = db.known_codes(conn)
        movie = _fetch_code_movie(fetcher, code, magnets)
        if movie is None:
            raise ValueError(f"站点上找不到 {code}（有码/无码两个频道都试过了）")
        for gcat, gname, gid in movie.genre_links:
            db.set_meta(conn, f"genre_id:{gcat}:{gname}", gid)
        keywords = [k for k in str(cfg.get("TAG_FILTERS") or "").split(",") if k.strip()]
        try:
            tiebreak = parse_tiebreak(cfg.get("MAGNET_TIEBREAK"))
        except ValueError:
            tiebreak = ["size", "date"]
        fallback = parse_fallback(cfg.get("MAGNET_FALLBACK"))
        if keywords:
            movie.matched_tags = compute_matched(
                movie.genres, [m.name for m in movie.magnets], keywords)
        movie.magnets = pick_best(movie.magnets, keywords, tiebreak, fallback)
        db.upsert_movie(conn, movie)
        db.delete_magnets(conn, movie.code)  # single-magnet storage: resync
        db.insert_magnets(conn, movie.magnets, movie.code)
        conn.commit()
        is_new = code not in known
        log.info("%s %s 磁力=%d %s", "补采新增" if is_new else "补采更新",
                 code, len(movie.magnets), movie.title[:40])
        auto_download(cfg, movie)
        return {"code": code, "title": movie.title, "category": movie.category,
                "new": is_new, "magnets": len(movie.magnets)}
    finally:
        conn.close()


def fetch_code_preview(cfg: dict, code: str, keywords: list[str],
                       tiebreak: list[str] | None = None,
                       fallback: str = "first", stop_check=None) -> dict:
    """Live-fetch a movie by code and rank its FULL magnet list (no DB writes).

    Used by the 精准筛选 preview: shows exactly which magnet the current
    keyword priority + tiebreakers + fallback would pick, without storing.
    """
    code = code.strip().upper()
    if not _CODE_RE.fullmatch(code):
        raise ValueError(f"番号格式无效: {code!r}（示例: BANK-248）")
    movie = _fetch_code_movie(_make_fetcher(cfg, stop_check), code, True)
    if movie is None:
        raise ValueError(f"站点上找不到 {code}（有码/无码两个频道都试过了）")
    return {
        "code": code,
        "title": movie.title,
        "category": movie.category,
        "genres": movie.genres,
        "fallback": fallback,
        "magnets": rank_magnets(movie.magnets, keywords, tiebreak, fallback),
    }
