"""Configuration: persisted in a JSON file, editable from the web UI.

No environment variables. The config file lives next to the data
(/data/config.json in the container, ./data/config.json when running
from a source checkout without /data).
"""
from __future__ import annotations

import json
import os
import threading

DATADIR = "/data" if os.path.isdir("/data") else "data"
CONFIG_PATH = os.path.join(DATADIR, "config.json")

DEFAULTS: dict = {
    "BASE_URL": "https://www.seedmm.bond",
    "DB_PATH": os.path.join(DATADIR, "seedmm.db"),
    "CATEGORY": "censored",           # censored | uncensored
    "DELAY_SECONDS": 2.0,
    "JITTER_SECONDS": 1.0,
    "MAX_RETRIES": 3,
    "TIMEOUT": 30,
    "PROXY": "",                      # e.g. http://127.0.0.1:7890
    "WEB_PORT": 7878,
    "AUTO_CRAWL_ENABLED": False,      # periodic crawling in the web service
    "AUTO_CRAWL_INTERVAL_HOURS": 24.0,
    "AUTO_CRAWL_PAGES": "1-3",
    "USER_AGENT": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}

# Types allowed per key, for validation on save.
_TYPES = {
    "BASE_URL": str,
    "DB_PATH": str,
    "CATEGORY": str,
    "DELAY_SECONDS": float,
    "JITTER_SECONDS": float,
    "MAX_RETRIES": int,
    "TIMEOUT": int,
    "PROXY": str,
    "WEB_PORT": int,
    "AUTO_CRAWL_ENABLED": bool,
    "AUTO_CRAWL_INTERVAL_HOURS": float,
    "AUTO_CRAWL_PAGES": str,
    "USER_AGENT": str,
}

_lock = threading.Lock()


def load() -> dict:
    """Load config merged over defaults; create the file on first run."""
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
        except (OSError, ValueError):
            pass  # corrupted file -> fall back to defaults
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
    if "CATEGORY" in clean and clean["CATEGORY"] not in ("censored", "uncensored"):
        raise ValueError("CATEGORY 只支持 censored / uncensored")
    if clean.get("AUTO_CRAWL_INTERVAL_HOURS", 0) != 0 and clean["AUTO_CRAWL_INTERVAL_HOURS"] <= 0:
        raise ValueError("自动采集间隔必须大于 0")
    if clean.get("AUTO_CRAWL_INTERVAL_HOURS", 24) > 24 * 30:
        raise ValueError("自动采集间隔过长（上限 720 小时）")

    with _lock:
        cfg = load()
        cfg.update(clean)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    return cfg
