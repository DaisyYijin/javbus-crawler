"""HTTP layer: one shared session, retry with backoff, rate limiting."""
from __future__ import annotations

import logging
import random
import threading
import time
from urllib.parse import urljoin

import requests

log = logging.getLogger(__name__)


class StopRequested(Exception):
    """Raised by fetchers when a cooperative stop was requested."""


class Fetcher:
    """A rate-limited HTTP GET client for seedmm.bond."""

    def __init__(
        self,
        base_url: str,
        delay: float = 2.0,
        jitter: float = 1.0,
        max_retries: int = 3,
        timeout: int = 30,
        user_agent: str = "",
        proxy: str = "",
        stop_check=None,
    ):
        self.base_url = base_url.rstrip("/")
        self.delay = delay
        self.jitter = jitter
        self.max_retries = max(1, max_retries)
        self.timeout = timeout
        self.stop_check = stop_check  # callable[[], bool] | None
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self.session = requests.Session()
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
            log.info("使用代理: %s", proxy)
        self.session.headers.update(
            {
                "User-Agent": user_agent
                or ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-TW,zh;q=0.9,ja;q=0.8,en;q=0.7",
                "Referer": self.base_url + "/",
            }
        )

    # -- rate limiting -----------------------------------------------------
    def _throttle(self) -> None:
        if self.stop_check and self.stop_check():
            raise StopRequested()
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
            self._next_allowed = time.monotonic() + self.delay + random.uniform(0, self.jitter)
        if self.stop_check and self.stop_check():
            raise StopRequested()

    # -- fetching ----------------------------------------------------------
    def get(self, path: str, referer: str | None = None) -> str | None:
        """GET a path (absolute URL or site-relative); return HTML text or None."""
        url = path if path.startswith("http") else urljoin(self.base_url + "/", path.lstrip("/"))
        headers = {"Referer": referer} if referer else {}
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
                if resp.status_code == 404:
                    log.warning("404 not found: %s", url)
                    return None
                resp.raise_for_status()
                resp.encoding = resp.apparent_encoding or "utf-8"
                return resp.text
            except StopRequested:
                raise
            except requests.RequestException as exc:
                backoff = 2 ** attempt
                log.warning("attempt %d/%d failed for %s (%s), backoff %ds",
                            attempt, self.max_retries, url, exc, backoff)
                time.sleep(backoff)
        log.error("giving up on %s", url)
        return None
