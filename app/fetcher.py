"""HTTP layer: one shared session, retry with backoff, rate limiting."""
import logging
import random
import threading
import time
from urllib.parse import urljoin

import requests

from . import settings

log = logging.getLogger(__name__)


class Fetcher:
    """A rate-limited HTTP GET client for seedmm.bond."""

    def __init__(self, base_url: str | None = None):
        self.base_url = base_url or settings.BASE_URL
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": settings.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-TW,zh;q=0.9,ja;q=0.8,en;q=0.7",
                "Referer": self.base_url + "/",
            }
        )

    # -- rate limiting -----------------------------------------------------
    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
            delay = settings.DELAY_SECONDS + random.uniform(0, settings.JITTER_SECONDS)
            self._next_allowed = time.monotonic() + delay

    # -- fetching ----------------------------------------------------------
    def get(self, path: str, referer: str | None = None) -> str | None:
        """GET a path (absolute URL or site-relative); return HTML text or None."""
        url = path if path.startswith("http") else urljoin(self.base_url + "/", path.lstrip("/"))
        headers = {"Referer": referer} if referer else {}
        last_err: Exception | None = None
        for attempt in range(1, settings.MAX_RETRIES + 1):
            self._throttle()
            try:
                resp = self.session.get(url, headers=headers, timeout=settings.TIMEOUT)
                if resp.status_code == 404:
                    log.warning("404 not found: %s", url)
                    return None
                resp.raise_for_status()
                resp.encoding = resp.apparent_encoding or "utf-8"
                return resp.text
            except requests.RequestException as exc:  # network / HTTP errors
                last_err = exc
                backoff = 2 ** attempt
                log.warning("attempt %d/%d failed for %s (%s), backoff %ds",
                            attempt, settings.MAX_RETRIES, url, exc, backoff)
                time.sleep(backoff)
        log.error("giving up on %s: %s", url, last_err)
        return None
