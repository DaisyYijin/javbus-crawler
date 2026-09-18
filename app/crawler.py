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


def parse_pages(spec: str) -> list[int]:
    """'3' -> [3]; '2-5' -> [2,3,4,5]."""
    m = re.fullmatch(r"(\d+)(?:-(\d+))?", str(spec).strip())
    if not m:
        raise ValueError(f"页码格式无效: {spec!r}（示例: '3' 或 '2-5'）")
    start = int(m.group(1))
    end = int(m.group(2) or start)
    if start < 1 or end < start:
        raise ValueError(f"页码范围无效: {spec!r}")
    return list(range(start, end + 1))


def compute_matched(tags: list[str], magnet_names: list[str], keywords: list[str]) -> list[str]:
    """Return the filter keywords hit by genre tags or magnet link names."""
    hits = []
    for kw in keywords:
        k = kw.strip()
        if not k:
            continue
        if any(k in t for t in tags) or any(k in n for n in magnet_names):
            hits.append(k)
    return hits


def run_job(
    cfg: dict,
    pages: str = "1",
    magnets: bool = True,
    refresh: bool = False,
    stop_check=None,
) -> dict:
    """Run one crawl job with a config snapshot. Returns stats."""
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
    category = cfg.get("CATEGORY", "censored")
    section = "/uncensored" if category == "uncensored" else ""
    conn = db.connect(cfg["DB_PATH"])
    known = db.known_codes(conn)
    log.info("开始采集: 频道=%s pages=%s base=%s 已入库=%d delay=%.1fs",
             "无码" if section else "有码", pages, fetcher.base_url,
             len(known), fetcher.delay)

    stats = {"pages": 0, "listed": 0, "new": 0, "updated": 0,
             "magnets": 0, "errors": 0, "stopped": False}

    try:
        for page in parse_pages(pages):
            html = fetcher.get(f"{section}/page/{page}")
            if not html:
                log.error("第 %d 页抓取失败，跳过", page)
                stats["errors"] += 1
                continue
            items = parse_list(html, fetcher.base_url)
            if not items:
                log.warning("第 %d 页没有条目（可能已到末尾）", page)
                continue
            stats["pages"] += 1
            stats["listed"] += len(items)
            log.info("第 %d 页: %d 部影片", page, len(items))

            for item in items:
                if not refresh and item.code in known:
                    continue
                detail_html = fetcher.get(item.url)
                if not detail_html:
                    stats["errors"] += 1
                    continue

                movie = parse_detail(detail_html, item.code, item.url)
                if magnets:
                    sv = parse_movie_script_vars(detail_html)
                    if sv.get("gid"):
                        frag = fetcher.get(magnet_ajax_path(sv), referer=item.url)
                        movie.magnets = parse_magnets(frag) if frag else []
                    else:
                        log.warning("%s: 未找到 gid 参数，跳过磁力", item.code)

                # tag filtering: match genres + magnet names against keywords
                keywords = [k for k in str(cfg.get("TAG_FILTERS") or "").split(",") if k.strip()]
                mode = cfg.get("TAG_FILTER_MODE", "all")
                if keywords:
                    movie.matched_tags = compute_matched(
                        movie.genres, [m.name for m in movie.magnets], keywords)
                    if mode == "only" and not movie.matched_tags:
                        stats.setdefault("skipped", 0)
                        stats["skipped"] += 1
                        log.info("跳过 %s（不匹配筛选: %s）", item.code, ",".join(keywords))
                        continue

                is_new = item.code not in known
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
