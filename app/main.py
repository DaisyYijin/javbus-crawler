"""Entry point: crawl listing pages, fetch details + magnets, store in SQLite."""
from __future__ import annotations

import argparse
import logging
import random
import re
import sys
import time
from urllib.parse import urlencode

from . import db, settings
from .fetcher import Fetcher
from .parser import parse_detail, parse_list, parse_movie_script_vars, parse_magnets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("seedmm")


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
    m = re.fullmatch(r"(\d+)(?:-(\d+))?", spec.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"invalid pages spec: {spec!r} (use '3' or '2-5')")
    start = int(m.group(1))
    end = int(m.group(2) or start)
    if start < 1 or end < start:
        raise argparse.ArgumentTypeError(f"invalid pages range: {spec!r}")
    return list(range(start, end + 1))


def crawl(args: argparse.Namespace) -> int:
    fetcher = Fetcher(args.base_url)
    conn = db.connect(args.db)
    known = db.known_codes(conn)
    log.info("db=%s known=%d base=%s", args.db, len(known), fetcher.base_url)

    stats = {"pages": 0, "listed": 0, "new": 0, "updated": 0, "magnets": 0, "errors": 0}

    for page in parse_pages(args.pages):
        html = fetcher.get(f"/page/{page}")
        if not html:
            log.error("page %d: fetch failed, aborting this page", page)
            stats["errors"] += 1
            continue
        items = parse_list(html, fetcher.base_url)
        if not items:
            log.warning("page %d: no movie boxes found (end of listing?)", page)
            continue
        stats["pages"] += 1
        stats["listed"] += len(items)
        log.info("page %d: %d movies", page, len(items))

        for item in items:
            if not args.refresh and item.code in known:
                continue
            detail_html = fetcher.get(item.url)
            if not detail_html:
                stats["errors"] += 1
                continue

            movie = parse_detail(detail_html, item.code, item.url)
            if args.magnets:
                sv = parse_movie_script_vars(detail_html)
                if sv.get("gid"):
                    frag = fetcher.get(magnet_ajax_path(sv), referer=item.url)
                    movie.magnets = parse_magnets(frag) if frag else []
                else:
                    log.warning("%s: no gid var found, magnets skipped", item.code)

            is_new = item.code not in known
            db.upsert_movie(conn, movie)
            stats["magnets"] += db.insert_magnets(conn, movie.magnets, movie.code)
            conn.commit()
            known.add(item.code)
            stats["new" if is_new else "updated"] += 1
            log.info("%s %s magnets=%d %s",
                     "NEW " if is_new else "UPD ", movie.code, len(movie.magnets), movie.title[:50])

    log.info("done: %(pages)d pages, %(listed)d listed, %(new)d new, "
             "%(updated)d updated, %(magnets)d magnets, %(errors)d errors", stats)
    conn.close()
    return 1 if stats["errors"] and not stats["new"] else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Crawl seedmm.bond into SQLite")
    ap.add_argument("--pages", default="1", help="page number or range, e.g. '3' or '2-5' (default: 1)")
    ap.add_argument("--db", default=settings.DB_PATH, help=f"SQLite path (default: {settings.DB_PATH})")
    ap.add_argument("--base-url", default=settings.BASE_URL, help="site base URL")
    ap.add_argument("--delay", type=float, default=settings.DELAY_SECONDS,
                    help="seconds between requests (default: %(default)s)")
    ap.add_argument("--no-magnets", dest="magnets", action="store_false",
                    help="skip magnet fetching")
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch movies already in the database")
    args = ap.parse_args(argv)

    settings.DELAY_SECONDS = args.delay
    t0 = time.monotonic()
    try:
        return crawl(args)
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130
    finally:
        log.info("elapsed %.1fs", time.monotonic() - t0)


if __name__ == "__main__":
    sys.exit(main())
