#!/usr/bin/env python3
"""Extract article content for rows where extracted_at IS NULL.

trafilatura primary → Jina Reader fallback (r.jina.ai/<url>).
Uses CAS write pattern on extracted_at (PLAN §4.8 + ISSUE-12 defense).
On extract failure → mark_failed bumps retry_count; after MAX_RETRIES,
mark_failed sets final_state='discarded'.

Flags:
  --pending     process rows where extracted_at IS NULL (default)
  --id ID       process a specific row id
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

import httpx
import trafilatura

import config
import db
from _logging import get_logger


HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}
MIN_CONTENT_CHARS = 200
RETRY_BACKOFF_BASE_SECONDS = 1.0


def _setup_logger() -> logging.Logger:
    return get_logger("extract")


def _is_transient_status(status: int) -> bool:
    return status in (408, 425, 429, 502, 503, 504) or 500 <= status < 600


def _try_trafilatura_once(url: str, client: httpx.Client, log: logging.Logger) -> tuple[str | None, bool]:
    """Single trafilatura attempt. Returns (content_or_None, transient_failure).

    transient_failure=True signals the caller to retry; False means a permanent
    failure (4xx other than transient, parse error returning empty content,
    or invalid URL) — no point retrying.
    """
    try:
        resp = client.get(url, timeout=config.EXTRACT_TIMEOUT_SECONDS, follow_redirects=True)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
        log.warning("trafilatura transient fetch error for %s: %s", url, e)
        return None, True
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        log.warning("trafilatura permanent fetch error for %s: %s", url, e)
        return None, False

    if _is_transient_status(resp.status_code):
        log.warning("trafilatura HTTP %d (transient) for %s", resp.status_code, url)
        return None, True
    if resp.status_code >= 400:
        return None, False

    try:
        text = trafilatura.extract(
            resp.text,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
        )
    except Exception as e:  # trafilatura library can raise various
        log.warning("trafilatura extract failed for %s: %s", url, e)
        return None, False

    if text and len(text) >= MIN_CONTENT_CHARS:
        return text[: config.MAX_CONTENT_LENGTH], False
    return None, False  # parsed empty/short — don't retry


def _extract_trafilatura(url: str, client: httpx.Client, log: logging.Logger) -> str | None:
    """Try trafilatura with bounded in-run retries on transient errors only."""
    for attempt in range(1, config.MAX_RETRIES + 1):
        content, transient = _try_trafilatura_once(url, client, log)
        if content:
            return content
        if not transient:
            return None
        if attempt < config.MAX_RETRIES:
            backoff = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.info("trafilatura retry %d/%d in %.1fs", attempt + 1, config.MAX_RETRIES, backoff)
            time.sleep(backoff)
    return None


def _try_jina_once(url: str, client: httpx.Client, log: logging.Logger) -> tuple[str | None, bool]:
    try:
        jina_url = f"https://r.jina.ai/{url}"
        resp = client.get(jina_url, timeout=config.EXTRACT_TIMEOUT_SECONDS, follow_redirects=True)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
        log.warning("Jina transient fetch error for %s: %s", url, e)
        return None, True
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        log.warning("Jina permanent fetch error for %s: %s", url, e)
        return None, False

    if _is_transient_status(resp.status_code):
        log.warning("Jina HTTP %d (transient) for %s", resp.status_code, url)
        return None, True
    if resp.status_code == 200 and len(resp.text) >= MIN_CONTENT_CHARS:
        return resp.text[: config.MAX_CONTENT_LENGTH], False
    return None, False


def _extract_jina(url: str, client: httpx.Client, log: logging.Logger) -> str | None:
    """Try Jina Reader with bounded in-run retries on transient errors only."""
    if not config.JINA_FALLBACK_ENABLED:
        return None
    for attempt in range(1, config.MAX_RETRIES + 1):
        content, transient = _try_jina_once(url, client, log)
        if content:
            return content
        if not transient:
            return None
        if attempt < config.MAX_RETRIES:
            backoff = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.info("Jina retry %d/%d in %.1fs", attempt + 1, config.MAX_RETRIES, backoff)
            time.sleep(backoff)
    return None


def extract_for_row(row: dict, client: httpx.Client, log: logging.Logger) -> bool:
    """Extract content for one row. Returns True on success."""
    article_id = row["id"]
    url = row.get("canonical_url") or row.get("url")
    if not url:
        log.error("row %d has no URL", article_id)
        db.mark_failed(article_id, "extract", "no URL")
        return False

    text = _extract_trafilatura(url, client, log)
    if not text:
        log.info("trafilatura failed/empty for row %d; trying Jina", article_id)
        text = _extract_jina(url, client, log)

    if not text:
        db.mark_failed(article_id, "extract", "no content from trafilatura or Jina")
        return False

    # Use existing title/source if already populated; otherwise leave for caller updates
    title = row.get("title") or ""
    source = row.get("source") or ""
    title_h = db.title_hash(title)

    success = db.mark_extracted(
        article_id, title=title, source=source, content=text, title_h=title_h
    )
    if success:
        log.info("[extracted] row %d (%d chars)", article_id, len(text))
        return True
    # Lost CAS race — already extracted by another process
    log.info("[extract] row %d already extracted; no-op", article_id)
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--pending", action="store_true", default=True,
                   help="extract rows where extracted_at IS NULL (default)")
    g.add_argument("--id", type=int, help="extract this specific row id")
    args = p.parse_args(argv)

    log = _setup_logger()

    with db.open_conn(read_only=True) as conn:
        if args.id is not None:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM articles WHERE id = ?", (args.id,)
            ).fetchall()]
        else:
            rows = [dict(r) for r in conn.execute(
                f"""
                SELECT * FROM articles
                 WHERE extracted_at IS NULL
                   AND final_state IS NULL
                   AND retry_count < ?
                 ORDER BY crawled_at ASC
                 LIMIT ?
                """,
                (config.MAX_RETRIES, config.MAX_ARTICLES_PER_RUN),
            ).fetchall()]

    if not rows:
        log.info("no pending rows to extract")
        return 0

    log.info("extracting %d row(s)", len(rows))
    successes = 0
    with httpx.Client(headers=HTTP_HEADERS) as client:
        for row in rows:
            if extract_for_row(row, client, log):
                successes += 1
    log.info("extract done: %d/%d succeeded", successes, len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
