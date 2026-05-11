#!/usr/bin/env python3
"""Crawl RSS sources, resolve canonical URLs, POST candidates to the VPS API.

v2 — no local DB. The VPS endpoint POST /api/articles is idempotent via the
url_hash UNIQUE key, so re-running this script is safe; previously-seen
URLs come back as `created: false`.

Flags:
  --dry-run         Don't POST; print would-insert counts.
  --topic NAME      Restrict to a single topic from config.TOPICS.

Redirect resolution (PLAN-v2 §5.2 — 4-tier cascade):
  1. httpx.head with follow_redirects.
  2. httpx.stream GET (close after redirect chain).
  3. Decode Google News base64 path.
  4. Keep Google URL; Jina handles at extract stage.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import logging
import logging.handlers
import re
import sys
import time
from pathlib import Path
from typing import Optional

import feedparser
import httpx

import config
from api_client import ApiClient, ApiError


GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=vi&gl=VN&ceid=VN:vi"
GOOGLE_NEWS_HOSTS = ("news.google.com",)
DEFAULT_HTTP_TIMEOUT = 10
RSS_RETRY_BACKOFF_BASE_SECONDS = 1.0
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}


def _get_logger() -> logging.Logger:
    log = logging.getLogger("crawl")
    if log.handlers:
        return log
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        config.LOGS_DIR / "crawl.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    log.addHandler(handler)
    # Also tee to stdout so launchd's StandardOutPath captures progress.
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(stream)
    log.setLevel(logging.INFO)
    return log


# --- Redirect resolution ---------------------------------------------------

def _resolve_via_head(url: str, client: httpx.Client) -> Optional[str]:
    try:
        r = client.head(url, follow_redirects=True, timeout=DEFAULT_HTTP_TIMEOUT)
        return str(r.url) if r.status_code < 400 else None
    except (httpx.HTTPError, httpx.InvalidURL):
        return None


def _resolve_via_get_stream(url: str, client: httpx.Client) -> Optional[str]:
    try:
        with client.stream("GET", url, follow_redirects=True, timeout=DEFAULT_HTTP_TIMEOUT) as r:
            return str(r.url)
    except (httpx.HTTPError, httpx.InvalidURL):
        return None


_GN_PATH_RE = re.compile(r"/(?:rss/)?articles/([A-Za-z0-9_\-]+)")


def _resolve_via_google_decode(url: str) -> Optional[str]:
    """Best-effort decode of Google News base64 path segments."""
    if "news.google.com" not in url:
        return None
    m = _GN_PATH_RE.search(url)
    if not m:
        return None
    encoded = m.group(1)
    padding = "=" * (-len(encoded) % 4)
    try:
        decoded = base64.urlsafe_b64decode(encoded + padding)
    except (ValueError, binascii.Error):
        return None
    found = re.search(rb"https?://[^\s\x00-\x1f\"']+", decoded)
    if not found:
        return None
    try:
        return found.group(0).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def resolve_canonical(url: str, client: httpx.Client, log: logging.Logger) -> str:
    is_google = any(host in url for host in GOOGLE_NEWS_HOSTS)

    resolved = _resolve_via_head(url, client)
    if resolved and (not is_google or "news.google.com" not in resolved):
        return resolved

    resolved = _resolve_via_get_stream(url, client)
    if resolved and (not is_google or "news.google.com" not in resolved):
        return resolved

    if is_google:
        decoded = _resolve_via_google_decode(url)
        if decoded:
            return decoded
        log.warning("Could not decode Google News URL: %s", url)

    return url


# --- RSS fetch --------------------------------------------------------------

def _fetch_rss_bytes(url: str, log: logging.Logger) -> bytes | None:
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            with httpx.Client(headers=HTTP_HEADERS, follow_redirects=True) as client:
                resp = client.get(url, timeout=DEFAULT_HTTP_TIMEOUT)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            log.warning("RSS transient fetch error %d/%d for %s: %s",
                        attempt, config.MAX_RETRIES, url, e)
            if attempt < config.MAX_RETRIES:
                time.sleep(RSS_RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
                continue
            return None
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            log.warning("RSS permanent fetch error for %s: %s", url, e)
            return None

        if resp.status_code in (408, 425, 429) or 500 <= resp.status_code < 600:
            log.warning("RSS HTTP %d (transient) %d/%d for %s",
                        resp.status_code, attempt, config.MAX_RETRIES, url)
            if attempt < config.MAX_RETRIES:
                time.sleep(RSS_RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
                continue
            return None
        if resp.status_code >= 400:
            log.warning("RSS HTTP %d (permanent) for %s", resp.status_code, url)
            return None
        return resp.content
    return None


def fetch_google_news(query: str, log: logging.Logger) -> list[dict]:
    url = GOOGLE_NEWS_RSS.format(query=query.replace(" ", "+"))
    body = _fetch_rss_bytes(url, log)
    if body is None:
        log.error("Google News RSS unavailable: %s", url)
        return []
    feed = feedparser.parse(body)
    items: list[dict] = []
    for entry in feed.entries:
        items.append({
            "url": entry.get("link", ""),
            "title": entry.get("title", ""),
            "source": (entry.get("source", {}).get("title", "Google News")
                       if hasattr(entry, "source") else "Google News"),
            "published_at": entry.get("published", "") or None,
        })
    return items


def fetch_rss(rss_url: str, log: logging.Logger,
              keyword_filter: list[str] | None = None) -> list[dict]:
    body = _fetch_rss_bytes(rss_url, log)
    if body is None:
        log.error("RSS unavailable: %s", rss_url)
        return []
    feed = feedparser.parse(body)
    feed_title = feed.feed.get("title", rss_url) if hasattr(feed, "feed") else rss_url
    items: list[dict] = []
    for entry in feed.entries:
        title = entry.get("title", "")
        summary = entry.get("summary", "")
        if keyword_filter:
            haystack = (title + " " + summary).lower()
            if not any(kw.lower() in haystack for kw in keyword_filter):
                continue
        items.append({
            "url": entry.get("link", ""),
            "title": title,
            "source": feed_title,
            "published_at": entry.get("published", "") or None,
        })
    return items


# --- canonicalize -----------------------------------------------------------

_TRACKING_PARAM_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid",
}


def _is_reserved_scheme(url: str) -> bool:
    """Mirror the server-side regex `^\\s*bootstrap-test://` (case-insensitive).

    See vps/src/routes/articles.php Articles::isReservedScheme. Client and
    server MUST agree on this check; a leak through the Mac side would be
    a server-side 403 anyway, but we want the Mac log to flag it locally
    rather than waste a round-trip.
    """
    if not url:
        return False
    return url.lstrip().lower().startswith(config.BOOTSTRAP_URL_SCHEME.lower())


def canonicalize_url(url: str) -> str:
    """Strip fragment + UTM-style tracking params for stable url_hash inputs.

    Must produce a value the VPS-side Hashing::urlHash() will MD5 to the
    same digest. The VPS MD5's the canonical_url field literally — so this
    is the single point of canonicalization.

    Tracking-key comparison is case-insensitive (per Codex impl-review):
    `UTM_SOURCE`, `Fbclid`, etc. all get stripped so two RSS items linking
    the same article through different referrers hash to the same digest.
    """
    if not url:
        return url
    if "#" in url:
        url = url.split("#", 1)[0]
    if "?" not in url:
        return url
    base, _, query = url.partition("?")
    keep = []
    for part in query.split("&"):
        if not part:
            continue
        k = part.split("=", 1)[0]
        if k and k.lower() not in _TRACKING_PARAM_KEYS:
            keep.append(part)
    return base if not keep else base + "?" + "&".join(keep)


# --- Per-topic collect ------------------------------------------------------

def collect_for_topic(topic: dict, dry_run: bool,
                      api: ApiClient, log: logging.Logger) -> dict:
    raw_items: list[dict] = []
    raw_items.extend(fetch_google_news(topic["google_news_query"], log))
    for rss in config.EXTRA_RSS_SOURCES:
        raw_items.extend(fetch_rss(rss, log, keyword_filter=topic["keywords"]))

    log.info("Topic '%s': fetched %d raw items", topic["name"], len(raw_items))

    would_insert = 0
    already_seen = 0
    rejected_reserved = 0
    posted_ids: list[int] = []
    failed = 0
    seen_canonical: set[str] = set()  # in-run dedup before any HTTP call

    with httpx.Client(headers=HTTP_HEADERS, follow_redirects=False) as client:
        for item in raw_items:
            raw_url = item.get("url", "")
            if not raw_url:
                continue
            if _is_reserved_scheme(raw_url):
                log.warning("rejected RSS URL using reserved scheme: %s", raw_url)
                rejected_reserved += 1
                continue

            canonical = canonicalize_url(resolve_canonical(raw_url, client, log))
            if _is_reserved_scheme(canonical):
                log.warning("rejected resolved URL using reserved scheme: %s", canonical)
                rejected_reserved += 1
                continue
            if canonical in seen_canonical:
                already_seen += 1
                continue
            seen_canonical.add(canonical)

            if dry_run:
                would_insert += 1
                continue

            try:
                resp = api.post_article(
                    url=raw_url,
                    canonical_url=canonical,
                    title=item.get("title") or None,
                    source=item.get("source") or None,
                    published_at=item.get("published_at"),
                )
            except ApiError as e:
                log.warning("post_article failed for %s: %s", canonical[:80], e)
                failed += 1
                continue

            if resp.get("created"):
                posted_ids.append(int(resp.get("id", 0)))
                would_insert += 1
            else:
                already_seen += 1

    return {
        "candidates": len(raw_items),
        "posted": would_insert if not dry_run else 0,
        "would_insert": would_insert,
        "already_seen": already_seen,
        "rejected_reserved": rejected_reserved,
        "failed": failed,
        "posted_ids": posted_ids,
    }


# --- CLI --------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dry-run", action="store_true",
                   help="don't POST, just print counts")
    p.add_argument("--topic", default=None,
                   help="restrict to a single topic (default: all)")
    args = p.parse_args(argv)

    log = _get_logger()

    if args.topic:
        t = config.get_topic(args.topic)
        if not t:
            log.error("unknown topic %s", args.topic)
            return 2
        topics = [t]
    else:
        topics = config.TOPICS

    api = ApiClient()
    # Cheap up-front health probe so a broken deploy fails before we waste
    # work resolving canonical URLs.
    try:
        h = api.health()
    except ApiError as e:
        log.error("api unreachable: %s", e)
        return 3
    if h.get("status") != "ok":
        log.error("api returned non-ok health: %s", h)
        return 3

    grand_total = {"posted": 0, "already_seen": 0, "rejected_reserved": 0, "failed": 0}
    for t in topics:
        summary = collect_for_topic(t, args.dry_run, api, log)
        log.info("topic '%s' summary: %s", t["name"], summary)
        for k in grand_total:
            grand_total[k] += summary.get(k, 0)

    log.info("crawl run complete (dry_run=%s): %s", args.dry_run, grand_total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
