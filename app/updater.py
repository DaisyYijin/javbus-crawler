"""Online update support.

Checks GitHub Releases for a newer version, shows the changelog,
and performs the update:

- running on the host next to the git checkout:  git pull + docker compose build
- running inside the container: prints the commands to run on the host
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.request

from .version import __version__

log = logging.getLogger("seedmm.update")

REPO = os.getenv("UPDATE_REPO", "DaisyYijin/javbus-crawler")
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_TIMEOUT = int(os.getenv("UPDATE_TIMEOUT", "6"))

IN_CONTAINER = os.path.exists("/.dockerenv")


def get_latest_release() -> dict | None:
    """Fetch the latest GitHub release, or None on any failure."""
    req = urllib.request.Request(
        RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"javbus-crawler/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:  # offline, rate-limited, DNS...
        log.debug("update check failed: %s", exc)
        return None


def _as_tuple(ver: str) -> tuple[int, ...]:
    parts = []
    for token in ver.lstrip("vV").split("."):
        num = ""
        for ch in token:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts)


def check_for_update(current: str = __version__) -> dict | None:
    """Return the release dict if a newer release exists, else None."""
    rel = get_latest_release()
    if not rel:
        return None
    latest = str(rel.get("tag_name") or "")
    if not latest:
        return None
    return rel if _as_tuple(latest) > _as_tuple(current) else None


def _host_update_commands() -> list[str]:
    """How to update this deployment, depending on how it runs."""
    if IN_CONTAINER:
        return [
            "# you are inside the container; run on the host:",
            "  git pull && docker compose build   # source deployment",
            "  docker pull ghcr.io/daisyyijin/javbus-crawler:latest   # image deployment",
        ]
    return ["git pull", "docker compose build"]


def print_update_notice(rel: dict) -> None:
    print(
        f"\n┌─────────────────────────────────────────────────────┐\n"
        f"│  发现新版本 {rel['tag_name']}（当前 v{__version__}）\n"
        f"│  运行 --update 查看更新日志并更新\n"
        f"└─────────────────────────────────────────────────────┘\n",
        file=sys.stderr,
    )


def print_changelog(rel: dict) -> None:
    print(f"\n更新日志 — {rel.get('name') or rel['tag_name']}\n")
    notes = (rel.get("body") or "").strip() or "（本次发布未填写更新日志）"
    print(notes)
    print(f"\n发布页面: {rel.get('html_url', '')}\n")


def run_update() -> int:
    """Show changelog for the newest release, confirm, then update."""
    rel = check_for_update()
    if rel is None:
        print(f"已是最新版本 v{__version__}")
        return 0

    print_changelog(rel)

    if IN_CONTAINER:
        # Cannot rebuild ourselves from inside the container.
        print("当前运行在容器内，无法自行更新镜像。请在宿主机上执行：\n")
        for cmd in _host_update_commands():
            print(f"  {cmd}")
        return 0

    try:
        answer = input("现在执行更新？[y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    if answer != "y":
        print("已取消。")
        return 0

    for cmd in _host_update_commands():
        print(f"$ {cmd}")
        result = subprocess.run(cmd, shell=True)
        if result.returncode != 0:
            print(f"命令失败（exit {result.returncode}），更新中止。")
            return 1
    print(f"\n更新完成。请重新运行采集命令。")
    return 0
