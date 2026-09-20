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


def fetch_genre_catalog(cfg: dict) -> dict:
    """Fetch both channels' genre catalogs from the site index pages.

    Returns {"censored": [{"group", "genres": [(name, id)]}], "uncensored": [...]}.
    On failure/blocked the affected channel is an empty list (callers fall
    back to the learned-genre cache).
    """
    import requests as _rq

    from .fetcher import BROWSER_UA

    base = str(cfg.get("BASE_URL") or "").rstrip("/")
    out: dict = {"censored": [], "uncensored": []}
    for cat, path in (("censored", "/genre"), ("uncensored", "/uncensored/genre")):
        try:
            r = _rq.get(base + path, headers={"User-Agent": BROWSER_UA}, timeout=15)
            if r.status_code == 200 and not looks_blocked(r.text):
                out[cat] = parse_genre_catalog(r.text)
        except _rq.RequestException:
            continue
    return out


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


def pick_magnet(magnets: list[dict], keywords: list[str]) -> tuple[dict | None, str]:
    """Pick the single magnet to use, honouring keyword priority.

    `keywords` are tried left-to-right (first = highest priority); the first
    magnet whose name contains the keyword wins.  When no keyword matches any
    magnet name, fall back to the first magnet on the list (newest first).
    Returns (magnet-or-None, matched-keyword-or-empty-string).
    """
    for kw in keywords:
        k = kw.strip()
        if not k:
            continue
        kl = k.lower()
        for m in magnets:
            if kl in (m.get("name") or "").lower():
                return m, k
    return (magnets[0] if magnets else None), ""


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
    combos = [(c, g) for c in cats for g in (genres_by_cat[c] or [""])]

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

                    is_new = item.code not in known
                    if refresh and magnets_fetched and not is_new:
                        # drop links that vanished from the site so the table
                        # stays in sync with movies.magnet_count
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


def crawl_code(cfg: dict, code: str, magnets: bool = True, stop_check=None) -> dict:
    """Crawl a single movie by code (tries the censored path, then /uncensored)."""
    code = code.strip().upper()
    if not _CODE_RE.fullmatch(code):
        raise ValueError(f"番号格式无效: {code!r}（示例: BANK-248）")
    fetcher = Fetcher(
        base_url=cfg["BASE_URL"],
        delay=cfg["DELAY_SECONDS"],
        jitter=cfg["JITTER_SECONDS"],
        max_retries=cfg["MAX_RETRIES"],
        timeout=cfg["TIMEOUT"],
        proxy=(cfg.get("PROXY") or "").strip(),
        stop_check=stop_check,
    )
    conn = db.connect(cfg["DB_PATH"])
    try:
        known = db.known_codes(conn)
        for section, cat in (("", "censored"), ("/uncensored", "uncensored")):
            html = fetcher.get(f"{section}/{code}")
            if not html:
                continue
            movie = parse_detail(html, code, f"{fetcher.base_url}{section}/{code}")
            if not movie.title:
                continue  # shell/redirect page, try the next section
            movie.category = cat
            for gcat, gname, gid in movie.genre_links:
                db.set_meta(conn, f"genre_id:{gcat}:{gname}", gid)
            if magnets:
                sv = parse_movie_script_vars(html)
                if sv.get("gid"):
                    frag = fetcher.get(magnet_ajax_path(sv), referer=movie.url)
                    movie.magnets = parse_magnets(frag) if frag else []
            keywords = [k for k in str(cfg.get("TAG_FILTERS") or "").split(",") if k.strip()]
            if keywords:
                movie.matched_tags = compute_matched(
                    movie.genres, [m.name for m in movie.magnets], keywords)
            db.upsert_movie(conn, movie)
            db.insert_magnets(conn, movie.magnets, movie.code)
            conn.commit()
            is_new = code not in known
            log.info("%s %s 磁力=%d %s", "补采新增" if is_new else "补采更新",
                     code, len(movie.magnets), movie.title[:40])
            return {"code": code, "title": movie.title, "category": cat,
                    "new": is_new, "magnets": len(movie.magnets)}
        raise ValueError(f"站点上找不到 {code}（有码/无码两个频道都试过了）")
    finally:
        conn.close()
