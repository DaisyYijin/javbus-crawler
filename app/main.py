"""Entry point.

Default (no args): start the web UI on 0.0.0.0:8000 (port from config).
Subcommands:
  crawl   one-shot CLI crawl (uses web-configured settings)
  serve   start the web UI explicitly

Update flags:
  --check-update / --update  (see app.updater)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from . import crawler, settings, updater
from .version import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("seedmm")


def cmd_crawl(args: argparse.Namespace) -> int:
    cfg = settings.load()
    if args.delay is not None:
        cfg["DELAY_SECONDS"] = args.delay
    try:
        stats = crawler.run_job(
            cfg, pages=args.pages, magnets=args.magnets, refresh=args.refresh
        )
    except ValueError as exc:  # bad pages spec
        log.error("%s", exc)
        return 2
    return 1 if stats["errors"] and not stats["new"] else 0


def cmd_serve(args: argparse.Namespace) -> int:
    from . import web

    web.run(host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="app.main",
        description="seedmm.bond 采集器（默认启动 Web 界面）",
    )
    ap.add_argument("--check-update", action="store_true",
                    help="check GitHub for a newer release and show its changelog")
    ap.add_argument("--update", action="store_true",
                    help="show the changelog of the newest release and update (host only)")
    sub = ap.add_subparsers(dest="command")

    p_crawl = sub.add_parser("crawl", help="一次性命令行采集（配置来自网页/配置文件）")
    p_crawl.add_argument("--pages", default="1", help="页码或范围, 如 '3' 或 '2-5' (默认 1)")
    p_crawl.add_argument("--no-magnets", dest="magnets", action="store_false",
                         help="跳过磁力抓取")
    p_crawl.add_argument("--refresh", action="store_true",
                         help="重新抓取已入库的番号")
    p_crawl.add_argument("--delay", type=float, default=None,
                         help="临时覆盖请求间隔（秒）")
    p_crawl.set_defaults(func=cmd_crawl)

    p_serve = sub.add_parser("serve", help="启动 Web 界面")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=None,
                         help="覆盖配置文件中的 WEB_PORT")
    p_serve.set_defaults(func=cmd_serve)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.update:
        return updater.run_update()
    if args.check_update:
        rel = updater.check_for_update()
        if rel:
            updater.print_changelog(rel)
        else:
            print(f"已是最新版本 v{__version__}")
        return 0

    if args.command is None:
        # default mode: web UI
        args.command = "serve"
        args.host, args.port = "0.0.0.0", None
        args.func = cmd_serve

    t0 = time.monotonic()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130
    finally:
        if args.command == "crawl":
            log.info("elapsed %.1fs", time.monotonic() - t0)


if __name__ == "__main__":
    sys.exit(main())
