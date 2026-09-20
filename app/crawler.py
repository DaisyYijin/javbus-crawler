"""Shared crawl engine used by both the CLI and the web UI."""
from __future__ import annotations

import logging
import random
import re
from urllib.parse import urlencode

from . import db
from .fetcher import Fetcher, StopRequested
from .parser import (parse_detail, parse_genre_catalog, parse_list,
                     parse_movie_script_vars, parse_magnets)

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
                sizes: list | None = None, dates: list | None = None) -> int | None:
    """Rank magnet names and return the best index (None = no keyword hit)."""
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
    return best_i


def _mfield(m, key: str) -> str:
    """Read a field from a magnet dict OR an attribute-style object."""
    if isinstance(m, dict):
        return str(m.get(key) or "")
    return str(getattr(m, key, "") or "")


def rank_magnets(magnets: list, keywords: list[str],
                 tiebreak: list[str] | None = None) -> list[dict]:
    """Rank ALL magnets best-first, with per-magnet hit details for the UI.

    Same scoring as _pick_index (keyword hits > keyword order > tiebreakers >
    list position). Unmatched magnets keep their original order after the
    matched ones. rank 1 is always the overall winner, i.e. exactly what
    pick_magnet/pick_best would return (including the no-hit -> first magnet
    fallback). Each item: {name, size, date, hits, hit_count, rank, best}.
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
    ranked = sorted((e for e in entries if e["key"] is not None),
                    key=lambda e: e["key"]) + \
             [e for e in entries if e["key"] is None]
    for rank, e in enumerate(ranked, 1):
        e["rank"] = rank
        e["best"] = rank == 1
        e["hit_count"] = len(e["hits"])
        del e["key"]
    return ranked


def pick_magnet(magnets: list[dict], keywords: list[str],
                tiebreak: list[str] | None = None) -> tuple[dict | None, str]:
    """Pick the single magnet to use, honouring keyword quality then priority.

    A magnet matching several keywords (e.g. 中文 AND 高清) beats one matching
    a single keyword; among equals the earlier keyword has priority, then the
    configured tiebreakers (size bigger-first / date newer-first), and the
    list order (newest first) breaks remaining ties. When no keyword matches
    any magnet name, fall back to the first magnet on the list.
    Returns (magnet-or-None, matched-keyword-or-empty-string).
    """
    if not magnets:
        return None, ""
    names = [m.get("name") or "" for m in magnets]
    i = _pick_index(names, keywords, tiebreak,
                    [m.get("size") or "" for m in magnets],
                    [m.get("date") or "" for m in magnets])
    if i is None:
        return magnets[0], ""
    kws = [k.strip() for k in keywords if k.strip()]
    n = names[i].lower()
    matched = next(k for k in kws if k.lower() in n)
    return magnets[i], matched


def pick_best(magnets: list, keywords: list[str],
              tiebreak: list[str] | None = None) -> list:
    """Keep only the single best magnet for storage.

    Uses the same scoring as pick_magnet: most keyword hits first (中文+高清
    beats 中文-only), then keyword priority, then the configured tiebreakers,
    then list order. No match -> the first magnet (newest). Filtering/marking
    should still be computed on the FULL list before calling this.
    """
    if not magnets:
        return []
    names = [getattr(m, "name", "") or "" for m in magnets]
    i = _pick_index(names, keywords, tiebreak,
                    [getattr(m, "size", "") or "" for m in magnets],
                    [getattr(m, "date", "") or "" for m in magnets])
    return [magnets[i if i is not None else 0]]


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

    def combo_label(c: str, g: str) -> str:
        base = "无码" if c == "uncensored" else "有码"
        return f"{base}/类别{g}" if g else base

    def depth_key(c: str, g: str) -> str:
        return f"max_page:{c}:{g}" if g else f"max_page:{c}"

    conn = db.connect(cfg["DB_PATH"])
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
             "magnets": 0, "errors": 0, "stopped": False}

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
                    detail_html = fetcher.get(item.url)
                    if not detail_html:
                        stats["errors"] += 1
                        continue

                    movie = parse_detail(detail_html, item.code, item.url)
                    movie.category = cat
                    magnets_fetched = False
                    if magnets:
                        sv = parse_movie_script_vars(detail_html)
                        if sv.get("gid"):
                            frag = fetcher.get(magnet_ajax_path(sv), referer=item.url)
                            if frag is not None:
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
                    filter_mode = cfg.get("TAG_FILTER_MODE", "mark")
                    if keywords:
                        movie.matched_tags = compute_matched(
                            movie.genres, [m.name for m in movie.magnets], keywords)
                        if filter_mode == "only" and not movie.matched_tags:
                            stats.setdefault("skipped", 0)
                            stats["skipped"] += 1
                            log.info("跳过 %s（不匹配筛选: %s）", item.code, ",".join(keywords))
                            continue

                    # learn per-channel genre name -> site id mapping
                    for gcat, gname, gid in movie.genre_links:
                        db.set_meta(conn, f"genre_id:{gcat}:{gname}", gid)

                    # store ONE magnet per movie: the best pick by keyword
                    # priority + tiebreakers (matching used the full list)
                    movie.magnets = pick_best(movie.magnets, keywords, tiebreak)

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
        stats["stopped"] = True
        log.warning("采集已被用户停止")
    finally:
        conn.commit()
        conn.close()
        log.info("结束: %(pages)d 页, %(new)d 新增, %(updated)d 更新, "
                 "%(magnets)d 磁力, %(errors)d 错误", stats)
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
        if keywords:
            movie.matched_tags = compute_matched(
                movie.genres, [m.name for m in movie.magnets], keywords)
        movie.magnets = pick_best(movie.magnets, keywords, tiebreak)
        db.upsert_movie(conn, movie)
        db.delete_magnets(conn, movie.code)  # single-magnet storage: resync
        db.insert_magnets(conn, movie.magnets, movie.code)
        conn.commit()
        is_new = code not in known
        log.info("%s %s 磁力=%d %s", "补采新增" if is_new else "补采更新",
                 code, len(movie.magnets), movie.title[:40])
        return {"code": code, "title": movie.title, "category": movie.category,
                "new": is_new, "magnets": len(movie.magnets)}
    finally:
        conn.close()


def fetch_code_preview(cfg: dict, code: str, keywords: list[str],
                       tiebreak: list[str] | None = None, stop_check=None) -> dict:
    """Live-fetch a movie by code and rank its FULL magnet list (no DB writes).

    Used by the 精准筛选 preview: shows exactly which magnet the current
    keyword priority + tiebreakers would pick, without storing anything.
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
        "magnets": rank_magnets(movie.magnets, keywords, tiebreak),
    }
