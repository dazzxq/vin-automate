#!/usr/bin/env python3
"""Crawl RSS sources, resolve canonical URLs, dedup, INSERT skeleton rows.

Flags:
  --dry-run         RO mode, no DB writes
  --topic NAME      restrict to single topic from config.TOPICS
  --inject-test T   insert ONE pre-scored verification row tagged with token T
                    (URL scheme `bootstrap-test://T` is reserved per PLAN §4.8.5).

Redirect resolution (PLAN §4.5) — 4-tier cascade:
  1. httpx.head with follow_redirects
  2. httpx.stream GET (close after redirect chain)
  3. Decode Google News base64 path
  4. Keep Google URL; Jina handles at extract stage
"""

from __future__ import annotations

import argparse
import base64
import logging
import re
import sys
import time
from typing import Optional

import feedparser
import httpx

import config
import db
from _logging import get_logger


RSS_RETRY_BACKOFF_BASE_SECONDS = 1.0


GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=vi&gl=VN&ceid=VN:vi"
GOOGLE_NEWS_HOSTS = ("news.google.com",)
DEFAULT_HTTP_TIMEOUT = 10
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}


def _setup_logger() -> logging.Logger:
    return get_logger("crawl")


# --- Redirect resolution ---------------------------------------------------

def _resolve_via_head(url: str, client: httpx.Client) -> Optional[str]:
    try:
        r = client.head(url, follow_redirects=True, timeout=DEFAULT_HTTP_TIMEOUT)
        if r.status_code < 400 and str(r.url) != url:
            return str(r.url)
        if r.status_code < 400:
            return str(r.url)
    except (httpx.HTTPError, httpx.InvalidURL):
        return None
    return None


def _resolve_via_get_stream(url: str, client: httpx.Client) -> Optional[str]:
    try:
        with client.stream("GET", url, follow_redirects=True, timeout=DEFAULT_HTTP_TIMEOUT) as r:
            final = str(r.url)
            # Close stream immediately; we don't need the body
            return final if final and final != url else final
    except (httpx.HTTPError, httpx.InvalidURL):
        return None


_GN_PATH_RE = re.compile(r"/(?:rss/)?articles/([A-Za-z0-9_\-]+)")


def _resolve_via_google_decode(url: str) -> Optional[str]:
    """Some Google News URLs encode the source URL in a base64 path segment.

    The encoding is not fully documented and varies; this attempts a best-effort
    decode and returns None on failure. If it works, returns the embedded URL.
    """
    if "news.google.com" not in url:
        return None
    m = _GN_PATH_RE.search(url)
    if not m:
        return None
    encoded = m.group(1)
    # Pad to multiple of 4 for base64
    padding = "=" * (-len(encoded) % 4)
    try:
        decoded = base64.urlsafe_b64decode(encoded + padding)
    except (ValueError, base64.binascii.Error):
        return None
    # Look for an embedded http(s) URL
    found = re.search(rb"https?://[^\s\x00-\x1f\"']+", decoded)
    if not found:
        return None
    try:
        return found.group(0).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def resolve_canonical(url: str, client: httpx.Client, log: logging.Logger) -> str:
    """4-tier cascade. Returns best-effort canonical URL (or original on full failure)."""
    is_google = any(host in url for host in GOOGLE_NEWS_HOSTS)

    # Tier 1: HEAD
    resolved = _resolve_via_head(url, client)
    if resolved and (not is_google or "news.google.com" not in resolved):
        return resolved

    # Tier 2: GET stream
    resolved = _resolve_via_get_stream(url, client)
    if resolved and (not is_google or "news.google.com" not in resolved):
        return resolved

    # Tier 3: Decode Google News path
    if is_google:
        decoded = _resolve_via_google_decode(url)
        if decoded:
            return decoded
        log.warning("Could not decode Google News URL: %s", url)

    # Tier 4: Keep original; Jina will handle at extract stage
    return url


# --- RSS feeds -------------------------------------------------------------

def _fetch_rss_bytes(url: str, log: logging.Logger) -> bytes | None:
    """Fetch RSS feed via httpx with bounded retry on transient errors.

    Retries on network errors and 5xx/transient status codes up to MAX_RETRIES.
    Returns the response body bytes on success, None on permanent failure or
    after exhausting retries. We pass bytes (not a URL) to feedparser so we
    can wrap the network call ourselves.
    """
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            with httpx.Client(headers=HTTP_HEADERS, follow_redirects=True) as client:
                resp = client.get(url, timeout=DEFAULT_HTTP_TIMEOUT)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            log.warning("RSS transient fetch error attempt %d/%d for %s: %s",
                        attempt, config.MAX_RETRIES, url, e)
            if attempt < config.MAX_RETRIES:
                time.sleep(RSS_RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
                continue
            return None
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            log.warning("RSS permanent fetch error for %s: %s", url, e)
            return None

        if resp.status_code in (408, 425, 429) or 500 <= resp.status_code < 600:
            log.warning("RSS HTTP %d (transient) attempt %d/%d for %s",
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
            "source": entry.get("source", {}).get("title", "Google News") if hasattr(entry, "source") else "Google News",
            "published_at": entry.get("published", ""),
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
            "published_at": entry.get("published", ""),
        })
    return items


# --- Per-topic collect -----------------------------------------------------

def collect_for_topic(topic: dict, dry_run: bool, log: logging.Logger) -> dict:
    """Return summary: {candidates, would_insert, already_seen, title_dups, rejected_reserved}."""
    raw_items: list[dict] = []
    raw_items.extend(fetch_google_news(topic["google_news_query"], log))
    for rss in config.EXTRA_RSS_SOURCES:
        raw_items.extend(fetch_rss(rss, log, keyword_filter=topic["keywords"]))

    log.info("Topic '%s': fetched %d raw items", topic["name"], len(raw_items))

    seen_hashes: set[str] = set()
    would_insert = 0
    already_seen = 0
    title_dups = 0
    rejected_reserved = 0
    inserted_ids: list[int] = []

    with httpx.Client(headers=HTTP_HEADERS, follow_redirects=False) as client:
        for item in raw_items:
            raw_url = item.get("url", "")
            if not raw_url:
                continue

            # Reject reserved URL scheme from malicious RSS (PLAN §4.8.5 ISSUE-15)
            if raw_url.startswith(config.BOOTSTRAP_URL_SCHEME):
                log.warning("Rejected RSS URL using reserved scheme: %s", raw_url)
                rejected_reserved += 1
                continue

            canonical = db.canonicalize_url(resolve_canonical(raw_url, client, log))

            # Defense in depth: post-resolution reserved scheme guard
            if canonical.startswith(config.BOOTSTRAP_URL_SCHEME):
                log.warning("Rejected resolved URL using reserved scheme: %s", canonical)
                rejected_reserved += 1
                continue

            h = db.url_hash(canonical)
            if h in seen_hashes:
                already_seen += 1
                continue
            seen_hashes.add(h)

            if db.url_exists(canonical):
                already_seen += 1
                continue

            title_h = db.title_hash(item.get("title", ""))
            if title_h and db.recent_title_hash_exists(title_h, config.TITLE_DEDUP_WINDOW_HOURS):
                title_dups += 1
                log.info("[dedup] title-hash match: %s", item.get("title", "")[:60])
                continue

            if dry_run:
                would_insert += 1
                continue

            row_id = db.insert_candidate(
                url=raw_url,
                canonical_url=canonical,
                title=item.get("title") or None,
                title_h=title_h,
                source=item.get("source") or None,
                published_at=item.get("published_at") or None,
            )
            if row_id:
                inserted_ids.append(row_id)
                would_insert += 1
            else:
                already_seen += 1

    return {
        "candidates": len(raw_items),
        "would_insert": would_insert,
        "already_seen": already_seen,
        "title_dups": title_dups,
        "rejected_reserved": rejected_reserved,
        "inserted_ids": inserted_ids,
    }


# --- --inject-test ---------------------------------------------------------

def inject_test(token: str, log: logging.Logger) -> int:
    """Insert a uniquely-tokenized verification row.

    Pre-scored (score=5) so the bootstrap flow can verify the saved scheduled
    task by triggering only the notify stage. Always returns a row id —
    idempotent per token: if the same token has already been used (whether
    notified or not), return the existing row's id with no error.
    """
    # Hex token only (compatible with secrets.token_hex). Per PLAN ISSUE-4
    # we constrain the alphabet to avoid ambiguity in URL parsing.
    if not re.fullmatch(r"[0-9a-fA-F]{4,64}", token):
        log.error("invalid token %r — must be hex (compatible with secrets.token_hex)", token)
        sys.exit(2)

    bootstrap_url = f"{config.BOOTSTRAP_URL_SCHEME}{token}"
    existing = db.get_article_by_url(bootstrap_url)
    if existing:
        # Idempotent: always reuse the existing row regardless of notified state.
        # Re-arming setup with the same token is a no-op; the row stays in place.
        state = "notified" if existing.get("notified_at") else "pending"
        log.info("verification row exists (id=%d, %s) — idempotent reuse",
                 existing["id"], state)
        return existing["id"]

    title = f"[BOOTSTRAP TEST {token}] sample article"
    content = "Test content for verification — VinFast Q1 sample"
    new_id = db.insert_candidate(
        url=bootstrap_url,
        canonical_url=bootstrap_url,
        title=title,
        title_h=db.title_hash(title),
        source="bootstrap-inject",
        content=content,
        score=5,
        score_reason="bootstrap verification",
    )
    if new_id is None:
        log.error("Could not insert verification row for token %s", token)
        sys.exit(1)

    # Set extracted_at + scored_at to NOW via SQL so the row is immediately
    # notify-eligible (skip-extracted, skip-scored). Done in one UPDATE.
    with db.open_conn() as conn:
        conn.execute(
            """
            UPDATE articles
               SET extracted_at = datetime('now'),
                   scored_at    = datetime('now')
             WHERE id = ?
            """,
            (new_id,),
        )
        conn.commit()

    log.info("[inject-test] inserted verification row id=%d url=%s", new_id, bootstrap_url)
    return new_id


# --- Main ------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="read-only; no DB writes")
    p.add_argument("--topic", type=str, help="restrict to this topic name (default: all)")
    p.add_argument("--inject-test", metavar="TOKEN", help="insert ONE verification row")
    args = p.parse_args(argv)

    log = _setup_logger()

    if args.inject_test:
        if args.dry_run:
            log.error("--inject-test cannot be combined with --dry-run")
            return 2
        inject_test(args.inject_test, log)
        return 0

    # Normal RSS crawl path
    topics_to_process = (
        [t for t in config.TOPICS if t["name"] == args.topic]
        if args.topic else list(config.TOPICS)
    )
    if not topics_to_process:
        log.error("no topics matched (--topic=%s)", args.topic)
        return 2

    if args.dry_run:
        log.info("DRY-RUN mode: no DB writes")

    total = {"candidates": 0, "would_insert": 0, "already_seen": 0,
             "title_dups": 0, "rejected_reserved": 0}
    for topic in topics_to_process:
        summary = collect_for_topic(topic, args.dry_run, log)
        for k in total:
            total[k] += summary[k]
        verb = "would insert" if args.dry_run else "inserted"
        log.info(
            "Topic %s: candidates=%d, %s=%d, already_seen=%d, title_dups=%d, rejected_reserved=%d",
            topic["name"], summary["candidates"], verb,
            summary["would_insert"], summary["already_seen"],
            summary["title_dups"], summary["rejected_reserved"],
        )

    verb = "would_insert" if args.dry_run else "inserted"
    log.info(
        "TOTAL: candidates=%d, %s=%d, already_seen=%d, title_dups=%d, rejected_reserved=%d",
        total["candidates"], verb, total["would_insert"],
        total["already_seen"], total["title_dups"], total["rejected_reserved"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
