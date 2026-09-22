"""Configuration: persisted in a JSON file, editable from the web UI.

No environment variables. The config file lives next to the data
(/data/config.json in the container, ./data/config.json when running
from a source checkout without /data).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time

DATADIR = os.getenv("JC_DATADIR") or ("/data" if os.path.isdir("/data") else "data")
CONFIG_PATH = os.path.join(DATADIR, "config.json")

DEFAULTS: dict = {
    "BASE_URL": "https://www.seedmm.bond",
    "DB_PATH": os.path.join(DATADIR, "seedmm.db"),
    "CATEGORY": "censored",           # censored | uncensored | comma-separated both
    "GENRE_CENSORED": "",              # censored-site genre ids/slugs, e.g. "42,hd"
    "GENRE_UNCENSORED": "",            # uncensored-site genre ids/slugs
    "DELAY_SECONDS": 2.0,
    "JITTER_SECONDS": 1.0,
    "MAX_RETRIES": 3,
    "TIMEOUT": 30,
    "PROXY": "",                      # e.g. http://127.0.0.1:7890
    "WEB_PORT": 7878,
    "AUTO_CRAWL_ENABLED": False,      # periodic crawling in the web service
    "AUTO_CRAWL_INTERVAL_HOURS": 24.0,
    "AUTO_CRAWL_PAGES": "1-3",
    "TAG_FILTERS": "",                # comma-separated keywords, e.g. "字幕,高清,4K"
    "TAG_FILTER_MODE": "mark",        # all | only | mark
    "MAGNET_TIEBREAK": "size,date",   # magnet pick tiebreakers: size (bigger first), date (newer first)
    "MAGNET_FALLBACK": "largest",     # no keyword hit at all: first (newest) | largest | none
    "AUTO_DOWNLOAD": False,           # auto-submit the picked magnet to 115 after crawl
    "METATUBE_URL": "",               # e.g. http://192.168.1.10:8080
    "METATUBE_TOKEN": "",             # optional bearer token for metatube server
    "P115_DOWNLOAD_DIR": "待整理",    # 115 dir offline downloads land in
    "P115_TARGET_DIR": "已整理",      # 115 dir completed downloads move into
    "P115_REJECT_DIR": "冗余",        # 115 dir ad/spam files get swept into
    "P115_AUTO_ORGANIZE": True,       # organize finished downloads automatically
    "P115_DL_STALL_MIN": 30,          # minutes without progress -> swap magnet
    "P115_DL_MAX_MIN": 120,           # total minutes per magnet -> swap (0 = no cap)
    "P115_DL_MAX_RETRIES": 3,         # magnet swaps before giving up
    "P115_DL_INTERVAL_SEC": 0,        # cooldown seconds between movies (0 = off)
    "P115_LIST_GAP_SEC": 0,           # fs listing gap override, s (0 = default 55)
}

# Types allowed per key, for validation on save.
_TYPES = {
    "BASE_URL": str,
    "DB_PATH": str,
    "CATEGORY": str,
    "GENRE_CENSORED": str,
    "GENRE_UNCENSORED": str,
    "DELAY_SECONDS": float,
    "JITTER_SECONDS": float,
    "MAX_RETRIES": int,
    "TIMEOUT": int,
    "PROXY": str,
    "WEB_PORT": int,
    "AUTO_CRAWL_ENABLED": bool,
    "AUTO_CRAWL_INTERVAL_HOURS": float,
    "AUTO_CRAWL_PAGES": str,
    "TAG_FILTERS": str,
    "TAG_FILTER_MODE": str,
    "MAGNET_TIEBREAK": str,
    "MAGNET_FALLBACK": str,
    "AUTO_DOWNLOAD": bool,
    "METATUBE_URL": str,
    "METATUBE_TOKEN": str,
    "P115_DOWNLOAD_DIR": str,
    "P115_TARGET_DIR": str,
    "P115_REJECT_DIR": str,
    "P115_AUTO_ORGANIZE": bool,
    "P115_DL_STALL_MIN": int,
    "P115_DL_MAX_MIN": int,
    "P115_DL_MAX_RETRIES": int,
    "P115_DL_INTERVAL_SEC": int,
    "P115_LIST_GAP_SEC": int,
}

_lock = threading.RLock()  # reentrant: save() holds it and calls load()
_cache: dict = {"ts": 0.0, "mtime": None, "cfg": None}
_CACHE_TTL = 5.0  # seconds; every API request re-reading the JSON file adds up


def _config_mtime() -> float | None:
    try:
        return os.path.getmtime(CONFIG_PATH)
    except OSError:
        return None


def load() -> dict:
    """Load config merged over defaults; create the file on first run.

    Serves from a short-lived in-process cache (web endpoints call this per
    request). The cache also drops when the file's mtime changes, so external
    writes (tests, editors) are picked up immediately. Returns a fresh copy
    each time so callers may mutate freely.
    """
    with _lock:
        mtime = _config_mtime()
        if (_cache["cfg"] is not None and _cache["mtime"] == mtime
                and time.monotonic() - _cache["ts"] < _CACHE_TTL):
            return dict(_cache["cfg"])
        cfg = _read_config_file()
        _cache.update(ts=time.monotonic(), cfg=cfg, mtime=mtime)
        return dict(cfg)


def _read_config_file() -> dict:
    cfg = dict(DEFAULTS)
    os.makedirs(DATADIR, exist_ok=True)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, dict):
                for key in DEFAULTS:
                    if key in stored:
                        cfg[key] = stored[key]
                # one-shot migration: the serial cooldown moved from minutes
                # to seconds in v0.10.72; carry the old value over scaled
                if ("P115_DL_INTERVAL_SEC" not in stored
                        and "P115_DL_INTERVAL_MIN" in stored):
                    try:
                        cfg["P115_DL_INTERVAL_SEC"] = \
                            max(0, int(stored["P115_DL_INTERVAL_MIN"])) * 60
                    except (TypeError, ValueError):
                        pass
        except (OSError, ValueError) as exc:
            log = logging.getLogger(__name__)
            log.error("配置文件损坏，已回退默认值（原文件已备份为 config.json.bak）: %s", exc)
            try:
                shutil.copy2(CONFIG_PATH, CONFIG_PATH + ".bak")
            except OSError:
                pass
    return cfg


def save(partial: dict) -> dict:
    """Validate and persist the given keys, return the new config."""
    if partial.get("DB_PATH") and partial["DB_PATH"] != load().get("DB_PATH"):
        raise ValueError("DB_PATH 不能修改（数据库位置由挂载目录决定）")
    clean = {}
    for key, value in (partial or {}).items():
        if key not in _TYPES or key == "DB_PATH":
            continue
        try:
            if _TYPES[key] is bool:
                clean[key] = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "on", "yes")
            else:
                clean[key] = _TYPES[key](value)
        except (TypeError, ValueError):
            raise ValueError(f"配置项 {key} 的值无效: {value!r}")
    if clean.get("DELAY_SECONDS", 0) < 0:
        raise ValueError("DELAY_SECONDS 不能为负")
    if clean.get("JITTER_SECONDS", 0) < 0:
        raise ValueError("JITTER_SECONDS 不能为负")
    if "WEB_PORT" in clean and not (1 <= clean["WEB_PORT"] <= 65535):
        raise ValueError("WEB_PORT 必须在 1-65535 之间")
    if "CATEGORY" in clean:
        cats = [c.strip() for c in str(clean["CATEGORY"]).split(",") if c.strip()]
        if not cats or any(c not in ("censored", "uncensored") for c in cats):
            raise ValueError("CATEGORY 只支持 censored / uncensored（可逗号分隔多选）")
        clean["CATEGORY"] = ",".join(cats)
    for gkey in ("GENRE_CENSORED", "GENRE_UNCENSORED"):
        if gkey in clean:
            import re as _re
            parts = [g.strip() for g in str(clean[gkey]).split(",") if g.strip()]
            for g in parts:
                if not _re.fullmatch(r"[A-Za-z0-9_-]{1,32}", g):
                    raise ValueError(f"{gkey} 含无效类别标识: {g!r}（应为数字 ID 或 slug，如 42、hd）")
            clean[gkey] = ",".join(parts)
    if "TAG_FILTER_MODE" in clean:
        mode = str(clean["TAG_FILTER_MODE"])
        if mode not in ("mark", "only", "all"):
            raise ValueError("TAG_FILTER_MODE 只支持 mark / only")
        clean["TAG_FILTER_MODE"] = "mark" if mode == "all" else mode
    if "MAGNET_TIEBREAK" in clean:
        from .crawler import parse_tiebreak

        clean["MAGNET_TIEBREAK"] = ",".join(parse_tiebreak(clean["MAGNET_TIEBREAK"]))
    if "MAGNET_FALLBACK" in clean:
        from .crawler import parse_fallback

        clean["MAGNET_FALLBACK"] = parse_fallback(clean["MAGNET_FALLBACK"])
    if "AUTO_CRAWL_INTERVAL_HOURS" in clean and clean["AUTO_CRAWL_INTERVAL_HOURS"] <= 0:
        raise ValueError("自动采集间隔必须大于 0")
    if clean.get("AUTO_CRAWL_INTERVAL_HOURS", 0) > 24 * 30:
        raise ValueError("自动采集间隔过长（上限 720 小时）")

    with _lock:
        cfg = load()
        cfg.update(clean)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        with _lock:
            _cache.update(ts=time.monotonic(), cfg=cfg, mtime=_config_mtime())
    return cfg
