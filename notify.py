#!/usr/bin/env python3
"""Push article to Telegram. CAS write of notified_at (PLAN §4.8).

Message format (REQUIRED — Codex ISSUE-13 verification):
  [#42] | score 4/5 | <source>
  <title>            ← verbatim, escaped (visible TOKEN for bootstrap verification)
  <i>{score_reason}</i>
  <a href="{canonical_url}">Đọc bài</a>

HTML parse mode (NOT MarkdownV2) — fewer escaping pitfalls.
1.2s tail sleep to respect per-chat Telegram rate limit.
Retries 429 honoring Retry-After. Retries transient 5xx with exponential backoff.

Exit codes:
  0   success OR no-op (already notified, score below threshold)
  1   generic error
  2   bad CLI args
  3   Telegram credentials missing
  4   non-retriable Telegram error (e.g. 400 Bad Request)
"""

from __future__ import annotations

import argparse
import html
import logging
import sys
import time

import httpx

import config
import db
from _logging import get_logger


TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"
MAX_TELEGRAM_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0


def _setup_logger() -> logging.Logger:
    return get_logger("notify")


def _build_message(row: dict) -> str:
    """HTML-safe message. Always includes the article TITLE verbatim so the
    bootstrap verification TOKEN (embedded in the title) is visible to the user."""
    article_id = row["id"]
    title = html.escape(row.get("title") or "(no title)")
    source = html.escape(row.get("source") or "")
    reason = html.escape(row.get("score_reason") or "")
    score = row.get("score") or 0
    canonical = row.get("canonical_url") or row.get("url") or ""
    canonical_escaped = html.escape(canonical, quote=True)

    score_emoji = {1: "⬜", 2: "🟨", 3: "🟧", 4: "🟥", 5: "🔴"}.get(score, "⬜")

    parts = [
        f"<b>[#{article_id}]</b> {score_emoji} score {score}/5 | <i>{source}</i>",
        "",
        f"<b>{title}</b>",
    ]
    if reason:
        parts.append(f"<i>{reason}</i>")
    if canonical and not canonical.startswith(config.BOOTSTRAP_URL_SCHEME):
        parts.append(f'<a href="{canonical_escaped}">Đọc bài</a>')
    parts.append("")
    parts.append(f"Brainstorm: <code>/idea-brainstormer {article_id}</code>")
    return "\n".join(parts)


def _send_telegram(text: str, chat_id: str, token: str,
                   log: logging.Logger) -> tuple[int | None, int]:
    """POST to Telegram. Returns (message_id, http_status). Retries internally
    on 429 + 5xx. Raises on non-retriable client errors."""
    api_url = TELEGRAM_API_BASE.format(token=token, method="sendMessage")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    last_status = 0
    for attempt in range(1, MAX_TELEGRAM_RETRIES + 1):
        try:
            resp = httpx.post(api_url, json=payload, timeout=20)
        except httpx.HTTPError as e:
            log.warning("Telegram network error (attempt %d): %s", attempt, e)
            time.sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
            continue
        last_status = resp.status_code
        if resp.status_code == 200:
            data = resp.json()
            if data.get("ok"):
                msg_id = data["result"]["message_id"]
                return msg_id, 200
            log.error("Telegram returned ok=false: %s", data)
            return None, 200
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "1"))
            log.warning("Telegram 429; sleeping %ds (attempt %d)", retry_after, attempt)
            time.sleep(retry_after + 1)
            continue
        if 500 <= resp.status_code < 600:
            backoff = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Telegram %d; backoff %.1fs (attempt %d)",
                        resp.status_code, backoff, attempt)
            time.sleep(backoff)
            continue
        # 4xx non-429: non-retriable
        log.error("Telegram non-retriable %d: %s", resp.status_code, resp.text[:200])
        return None, resp.status_code
    log.error("Telegram exhausted %d retries (last status=%d)",
              MAX_TELEGRAM_RETRIES, last_status)
    return None, last_status


def notify_article(article_id: int, log: logging.Logger) -> int:
    if not config.have_telegram_credentials():
        log.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env")
        return 3

    row = db.get_article(article_id)
    if not row:
        log.error("row %d not found", article_id)
        return 1

    if row.get("notified_at"):
        log.info("[notify.py] row %d already notified; no-op", article_id)
        return 0

    if row.get("final_state"):
        log.info("[notify.py] row %d is %s; skipping", article_id, row["final_state"])
        return 0

    score = row.get("score")
    if score is None:
        log.info("[notify.py] row %d not yet scored; skipping", article_id)
        return 0

    # Resolve threshold via the stable MIN_SCORE_TO_NOTIFY export.
    # Articles do not carry a topic-id column today; use the first topic's
    # threshold. If multi-topic separation becomes important later, tag the
    # row at crawl time and look up by topic name here.
    if config.MIN_SCORE_TO_NOTIFY:
        # Stable: pick the first topic name from MIN_SCORE_TO_NOTIFY (iter order = insert order)
        first_topic = next(iter(config.MIN_SCORE_TO_NOTIFY))
        threshold = config.MIN_SCORE_TO_NOTIFY[first_topic]
    else:
        threshold = 3
    if score < threshold:
        log.info("[notify.py] row %d score=%d below threshold=%d; skipping",
                 article_id, score, threshold)
        return 0

    text = _build_message(row)
    msg_id, http_status = _send_telegram(
        text, config.TELEGRAM_CHAT_ID, config.TELEGRAM_BOT_TOKEN, log
    )
    if msg_id is None:
        if http_status and 400 <= http_status < 500 and http_status != 429:
            db.mark_failed(article_id, "notify", f"Telegram {http_status}")
            return 4
        db.mark_failed(article_id, "notify", f"Telegram exhausted (last={http_status})")
        return 1

    success = db.mark_notified(article_id, msg_id, min_score=threshold)
    if success:
        log.info("[notified] row %d msg_id=%s", article_id, msg_id)
    else:
        # Lost CAS race — concurrent notify
        log.info("[notify.py] row %d already notified; no-op", article_id)

    time.sleep(config.TELEGRAM_SEND_DELAY_SECONDS)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("id", type=int, help="article id to notify")
    args = p.parse_args(argv)
    log = _setup_logger()
    return notify_article(args.id, log)


if __name__ == "__main__":
    sys.exit(main())
