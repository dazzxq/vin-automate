"""Mac-side runtime tunables for vin-automate v2.

The VPS is the single source of truth for stateful tunables (LOCK_TTL_SECONDS,
MAX_RETRIES, etc.) — those are configured in shared/.env on the VPS by
deploy.sh. This module only holds:
  - Crawl topics + RSS feed URLs (lists, not per-deployment secrets).
  - Mac-process tunables that the Mac client uses locally and the VPS does
    not see (HTTP timeouts, in-process retry budgets, etc.).

`.env` (mode 600) supplies API_BASE_URL + API_TOKEN to api_client.ApiClient.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
LOGS_DIR = PROJECT_ROOT / "logs"

load_dotenv(ENV_PATH, override=False)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# --- Topics (only VinFast for v1) -----------------------------------------

TOPICS: list[dict] = [
    {
        "name": "vinfast",
        "keywords": ["VinFast", "VF3", "VF5", "VF6", "VF7", "VF8", "VF9"],
        "google_news_query": "VinFast",
    },
]

EXTRA_RSS_SOURCES: list[str] = [
    "https://vnexpress.net/rss/kinh-doanh.rss",
    "https://tuoitre.vn/rss/kinh-te.rss",
    "https://thanhnien.vn/rss/kinh-te.rss",
    "https://electrek.co/feed/",
    "https://insideevs.com/feed/",
]


# --- Tunables (Mac-side only) ---------------------------------------------

MAX_ARTICLES_PER_RUN = _int_env("MAX_ARTICLES_PER_RUN", 50)
MAX_RETRIES          = _int_env("MAX_RETRIES", 3)

EXTRACT_TIMEOUT_SECONDS   = _int_env("EXTRACT_TIMEOUT_SECONDS", 15)
JINA_FALLBACK_ENABLED     = os.getenv("JINA_FALLBACK_ENABLED", "1").strip() != "0"
MAX_CONTENT_LENGTH        = _int_env("MAX_CONTENT_LENGTH", 8000)


# Reserved URL scheme — Mac MUST drop any RSS URL with this prefix (the VPS
# /api/articles endpoint also rejects it, but defense-in-depth).
BOOTSTRAP_URL_SCHEME = "bootstrap-test://"


def get_topic(name: str) -> dict | None:
    for t in TOPICS:
        if t["name"] == name:
            return t
    return None
