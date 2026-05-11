#!/usr/bin/env python3
"""Fetch full content for articles where extracted_at IS NULL on the VPS.

v2 — no local DB. We GET `/api/articles?stage=new` (server returns rows
matching `extracted_at IS NULL AND final_state IS NULL AND retry_count <
MAX_RETRIES` per PLAN-v2 §4 ISSUE-13), extract via trafilatura -> Jina
fallback, and PATCH `/api/articles/{id}/extract`. On exhausted in-process
retries we POST `/api/articles/{id}/fail` so the server bumps retry_count
(and discards once it hits MAX_RETRIES).

Flags:
  --pending          process server `stage=new` rows (default).
  --id ID            extract only this specific row id.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
import time
from pathlib import Path

import httpx
import trafilatura

import config
from api_client import ApiClient, ApiError


HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}
MIN_CONTENT_CHARS = 200
RETRY_BACKOFF_BASE_SECONDS = 1.0


def _get_logger() -> logging.Logger:
    log = logging.getLogger("extract")
    if log.handlers:
        return log
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        config.LOGS_DIR / "extract.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    log.addHandler(handler)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(stream)
    log.setLevel(logging.INFO)
    return log


def _is_transient_status(status: int) -> bool:
    return status in (408, 425, 429, 502, 503, 504) or 500 <= status < 600


def _try_trafilatura_once(url: str, client: httpx.Client, log: logging.Logger) -> tuple[str | None, bool, str | None]:
    """Returns (content, transient, last_error_code).

    last_error_code is set on every failed path so the caller can propagate
    the actual reason into /api/articles/{id}/fail (per Codex impl-review).
    Possible codes: trafilatura_timeout, trafilatura_network, trafilatura_http,
    trafilatura_http_429, trafilatura_http_5xx, trafilatura_4xx,
    trafilatura_parse_error, trafilatura_empty.
    """
    try:
        resp = client.get(url, timeout=config.EXTRACT_TIMEOUT_SECONDS, follow_redirects=True)
    except httpx.TimeoutException as e:
        log.warning("trafilatura timeout for %s: %s", url, e)
        return None, True, "trafilatura_timeout"
    except (httpx.NetworkError, httpx.RemoteProtocolError) as e:
        log.warning("trafilatura network error for %s: %s", url, e)
        return None, True, "trafilatura_network"
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        log.warning("trafilatura permanent fetch error for %s: %s", url, e)
        return None, False, "trafilatura_http"

    if resp.status_code == 429:
        log.warning("trafilatura HTTP 429 for %s", url)
        return None, True, "trafilatura_http_429"
    if _is_transient_status(resp.status_code):
        log.warning("trafilatura HTTP %d (transient) for %s", resp.status_code, url)
        return None, True, f"trafilatura_http_{resp.status_code}"
    if resp.status_code >= 400:
        return None, False, f"trafilatura_4xx_{resp.status_code}"

    try:
        text = trafilatura.extract(
            resp.text, include_comments=False, include_tables=False, no_fallback=False
        )
    except Exception as e:  # trafilatura raises various
        log.warning("trafilatura extract failed for %s: %s", url, e)
        return None, False, "trafilatura_parse_error"

    if text and len(text) >= MIN_CONTENT_CHARS:
        return text[: config.MAX_CONTENT_LENGTH], False, None
    return None, False, "trafilatura_empty"


def _extract_trafilatura(url: str, client: httpx.Client,
                         log: logging.Logger) -> tuple[str | None, str | None]:
    last_code: str | None = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        content, transient, code = _try_trafilatura_once(url, client, log)
        if content:
            return content, None
        last_code = code
        if not transient:
            return None, last_code
        if attempt < config.MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
    return None, last_code


def _try_jina_once(url: str, client: httpx.Client, log: logging.Logger) -> tuple[str | None, bool, str | None]:
    try:
        jina_url = f"https://r.jina.ai/{url}"
        resp = client.get(jina_url, timeout=config.EXTRACT_TIMEOUT_SECONDS, follow_redirects=True)
    except httpx.TimeoutException as e:
        log.warning("Jina timeout for %s: %s", url, e)
        return None, True, "jina_timeout"
    except (httpx.NetworkError, httpx.RemoteProtocolError) as e:
        log.warning("Jina network error for %s: %s", url, e)
        return None, True, "jina_network"
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        log.warning("Jina permanent fetch error for %s: %s", url, e)
        return None, False, "jina_http"

    if resp.status_code == 429:
        log.warning("Jina HTTP 429 for %s", url)
        return None, True, "jina_http_429"
    if _is_transient_status(resp.status_code):
        log.warning("Jina HTTP %d (transient) for %s", resp.status_code, url)
        return None, True, f"jina_http_{resp.status_code}"
    if resp.status_code == 200 and len(resp.text) >= MIN_CONTENT_CHARS:
        return resp.text[: config.MAX_CONTENT_LENGTH], False, None
    if resp.status_code >= 400:
        return None, False, f"jina_4xx_{resp.status_code}"
    return None, False, "jina_empty"


def _extract_jina(url: str, client: httpx.Client,
                  log: logging.Logger) -> tuple[str | None, str | None]:
    if not config.JINA_FALLBACK_ENABLED:
        return None, "jina_disabled"
    last_code: str | None = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        content, transient, code = _try_jina_once(url, client, log)
        if content:
            return content, None
        last_code = code
        if not transient:
            return None, last_code
        if attempt < config.MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
    return None, last_code


def extract_for_row(row: dict, http: httpx.Client, api: ApiClient,
                    log: logging.Logger) -> bool:
    """Extract one row. Returns True on success (PATCH applied or no-op)."""
    article_id = int(row["id"])
    url = row.get("canonical_url") or row.get("url") or ""
    if not url:
        log.error("row %d has no URL", article_id)
        try:
            api.post_fail(article_id, stage="extract", error_code="extract_no_url",
                          message="row has no canonical_url or url")
        except ApiError as e:
            log.warning("/fail for %d returned %s", article_id, e)
        return False

    text, traf_code = _extract_trafilatura(url, http, log)
    jina_code: str | None = None
    if not text:
        log.info("trafilatura empty/failed for row %d (%s); trying Jina",
                 article_id, traf_code)
        text, jina_code = _extract_jina(url, http, log)

    if not text:
        # Prefer the deepest (Jina) failure code; fall back to trafilatura;
        # finally extract_unknown. Keeps server-side /fail records actionable.
        error_code = jina_code or traf_code or "extract_unknown"
        try:
            resp = api.post_fail(article_id, stage="extract",
                                 error_code=error_code,
                                 message=f"trafilatura={traf_code} jina={jina_code} url={url[:200]}")
            state = "discarded" if resp.get("discarded") else "retrying"
            log.info("[fail] row %d state=%s retry_count=%d code=%s",
                     article_id, state, resp.get("retry_count", -1), error_code)
        except ApiError as e:
            log.warning("/fail for %d returned %s", article_id, e)
        return False

    try:
        resp = api.patch_extract(
            article_id,
            content=text,
            # We do NOT overwrite title/source on PATCH unless the server
            # supports the field — the route does, but only when supplied.
            # Keep upstream values: only fill when missing.
            title=row.get("title") if not row.get("title") else None,
            source=row.get("source") if not row.get("source") else None,
        )
    except ApiError as e:
        # Ambiguous write: the server may have committed the extract update
        # before responding with 5xx / connection-reset. Per Codex impl-review
        # ISSUE-5, re-read the row before deciding whether to /fail. If
        # extracted_at is now set, treat it as success (idempotent no-op);
        # only call /fail if the row is genuinely still unextracted.
        log.warning("PATCH /extract for %d returned %s — verifying server state", article_id, e)
        try:
            fresh = api.get_article(article_id)
        except ApiError as e2:
            log.warning("get_article(%d) after PATCH failure also failed (%s) — bailing without /fail to avoid double-count", article_id, e2)
            return False
        if fresh.get("extracted_at"):
            log.info("[extracted-recovered] row %d: server already has extracted_at=%s",
                     article_id, fresh.get("extracted_at"))
            return True
        try:
            api.post_fail(article_id, stage="extract",
                          error_code="patch_failed",
                          message=str(e)[:400])
        except ApiError as e2:
            log.warning("/fail for %d also failed: %s", article_id, e2)
        return False

    if resp.get("updated"):
        log.info("[extracted] row %d (%d chars)", article_id, len(text))
    else:
        # Server already has extracted_at — race-safe no-op.
        log.info("[extracted-noop] row %d: %s", article_id, resp.get("reason", "?"))
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--pending", action="store_true", default=True,
                   help="extract server-side stage=new rows (default)")
    g.add_argument("--id", type=int,
                   help="extract this specific row id (debug)")
    args = p.parse_args(argv)

    log = _get_logger()
    api = ApiClient()

    if args.id is not None:
        try:
            rows = [api.get_article(args.id)]
        except ApiError as e:
            log.error("get_article(%d) failed: %s", args.id, e)
            return 2
    else:
        try:
            rows = api.list_articles(stage="new", limit=config.MAX_ARTICLES_PER_RUN)
        except ApiError as e:
            log.error("list_articles stage=new failed: %s", e)
            return 2

    if not rows:
        log.info("no pending rows to extract")
        return 0

    log.info("extracting %d row(s)", len(rows))
    successes = 0
    with httpx.Client(headers=HTTP_HEADERS) as http:
        for row in rows:
            if extract_for_row(row, http, api, log):
                successes += 1
    log.info("extract done: %d/%d succeeded", successes, len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
