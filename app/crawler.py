"""Shared crawl engine used by both the CLI and the web UI."""
from __future__ import annotations

import logging
import random
import re
from urllib.parse import urlencode

from . import db
from .fetcher import Fetcher, StopRequested
from .parser import parse_detail, parse_list, parse_movie_script_vars, parse_magnets

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
        user_agent=cfg["USER_AGENT"],
        proxy=(cfg.get("PROXY") or "").strip(),
        stop_check=stop_check,
    )
    sections = {"censored": "", "uncensored": "/uncensored"}
    cats = [c.strip() for c in str(cfg.get("CATEGORY", "censored")).split(",")
            if c.strip() in sections] or ["censored"]

    def cat_label(c: str) -> str:
        return "无码" if c == "uncensored" else "有码"

    conn = db.connect(cfg["DB_PATH"])
    known = db.known_codes(conn)
    depths = {c: int(db.get_meta(conn, f"max_page:{c}", "0") or 0) for c in cats}

    if mode == "backfill":
        try:
            count = int(str(pages).strip())
        except ValueError:
            count = len(parse_pages(pages))  # e.g. "2-5" -> 4 pages
        count = max(1, min(count, 500))
        plans = []
        for c in cats:
            if depths[c] <= 0:
                log.warning("补历史跳过 %s 频道：尚无采集深度记录，请先用「追新」采集第 1 页",
                            cat_label(c))
                continue
            plans.append((c, list(range(depths[c] + 1, depths[c] + 1 + count))))
            log.info("补历史 %s: 从第 %d 页继续，本次 %d 页（当前深度 %d）",
                     cat_label(c), depths[c] + 1, count, depths[c])
        if not plans:
            raise ValueError("尚无采集深度记录：请先用「追新」模式采集第 1 页")
    else:
        page_list = parse_pages(pages)
        mode = "new"
        plans = [(c, page_list) for c in cats]
    log.info("开始采集: 模式=%s 频道=%s pages=%s base=%s 已入库=%d delay=%.1fs",
             "补历史" if mode == "backfill" else "追新",
             "/".join(cat_label(c) for c in cats), pages, fetcher.base_url,
             len(known), fetcher.delay)

    stats = {"pages": 0, "listed": 0, "new": 0, "updated": 0,
             "magnets": 0, "errors": 0, "stopped": False}

    try:
        for cat, page_list in plans:
            section = sections[cat]
            for page in page_list:
                html = fetcher.get(f"{section}/page/{page}")
                if not html:
                    log.error("第 %d 页抓取失败，跳过", page)
                    stats["errors"] += 1
                    continue
                items = parse_list(html, fetcher.base_url)
                if not items:
                    log.warning("第 %d 页没有条目，已到站点尽头，停止补历史", page)
                    break
                if page > depths[cat]:
                    depths[cat] = page
                    db.set_meta(conn, f"max_page:{cat}", depths[cat])
                    conn.commit()
                stats["pages"] += 1
                stats["listed"] += len(items)
                log.info("[%s] 第 %d 页: %d 部影片", cat_label(cat), page, len(items))

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
        user_agent=cfg["USER_AGENT"],
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
