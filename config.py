"""Configuration for VinFast News Pipeline.

All env vars loaded from .env via python-dotenv. Topics + tunables exposed as
module-level constants. Imported by every tool CLI (crawl, extract, list,
mark, notify, lock, db).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
DB_PATH = PROJECT_ROOT / "news.db"
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


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Topics — list of dicts. Add more topics by appending to this list.
TOPICS: list[dict] = [
    {
        "name": "vinfast",
        "keywords": ["VinFast", "VF3", "VF5", "VF6", "VF7", "VF8", "VF9"],
        "google_news_query": "VinFast",
        "min_score_to_notify": 3,
    },
]

# Extra RSS feeds beyond Google News. Crawler will filter by topic keywords.
EXTRA_RSS_SOURCES: list[str] = [
    "https://vnexpress.net/rss/kinh-doanh.rss",
    "https://tuoitre.vn/rss/kinh-te.rss",
    "https://thanhnien.vn/rss/kinh-te.rss",
    "https://electrek.co/feed/",
    "https://insideevs.com/feed/",
]

# Per-topic notify threshold derived from TOPICS for stable external API surface
# (callers should NOT reach into TOPICS[i]["min_score_to_notify"] directly).
MIN_SCORE_TO_NOTIFY: dict[str, int] = {
    t["name"]: t.get("min_score_to_notify", 3) for t in TOPICS
}

# --- Tunables ---------------------------------------------------------------
MAX_ARTICLES_PER_RUN = _int_env("MAX_ARTICLES_PER_RUN", 50)
MAX_RETRIES = _int_env("MAX_RETRIES", 3)
DRY_RUN_DEDUP_WINDOW_HOURS = _int_env("DRY_RUN_DEDUP_WINDOW_HOURS", 48)
TITLE_DEDUP_WINDOW_HOURS = _int_env("TITLE_DEDUP_WINDOW_HOURS", 48)

# Lock TTL (seconds). Heartbeat extends as needed during a long run.
LOCK_TTL_SECONDS = _int_env("LOCK_TTL_SECONDS", 1800)
HEARTBEAT_EVERY_N_ROWS = _int_env("HEARTBEAT_EVERY_N_ROWS", 10)

# Extraction
MAX_CONTENT_LENGTH = _int_env("MAX_CONTENT_LENGTH", 8000)
EXTRACT_TIMEOUT_SECONDS = _int_env("EXTRACT_TIMEOUT_SECONDS", 15)
JINA_FALLBACK_ENABLED = os.getenv("JINA_FALLBACK_ENABLED", "1").strip() != "0"

# Telegram
TELEGRAM_SEND_DELAY_SECONDS = float(os.getenv("TELEGRAM_SEND_DELAY_SECONDS", "1.2"))

# Reserved URL scheme for bootstrap verification rows (per PLAN §4.8 ISSUE-15).
# crawl.py MUST reject any incoming RSS URL matching this prefix.
BOOTSTRAP_URL_SCHEME = "bootstrap-test://"


def have_telegram_credentials() -> bool:
    """True iff both Telegram secrets are present in .env."""
    return bool(TELEGRAM_BOT_TOKEN) and bool(TELEGRAM_CHAT_ID)


def get_topic(name: str) -> dict | None:
    for t in TOPICS:
        if t["name"] == name:
            return t
    return None
