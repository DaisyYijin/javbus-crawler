"""Global configuration, overridable via environment variables."""
import os

BASE_URL = os.getenv("SEEDMM_BASE", "https://www.seedmm.bond").rstrip("/")

# SQLite database file path. Mount a volume at /data inside the container.
DB_PATH = os.getenv("DB_PATH", "/data/seedmm.db")

# Polite crawling defaults.
DELAY_SECONDS = float(os.getenv("DELAY_SECONDS", "2.0"))
JITTER_SECONDS = float(os.getenv("JITTER_SECONDS", "1.0"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
TIMEOUT = int(os.getenv("TIMEOUT", "30"))

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
)
