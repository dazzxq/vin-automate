"""SQLite schema + helper functions for VinFast News Pipeline.

Implements per-stage timestamps model (PLAN §3) and locks table (PLAN §4.8.1).
All write helpers use compare-and-set patterns (PLAN §4.8 ISSUE-12).
Read-only open mode for crawl --dry-run tolerates missing file or tables.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import config


_SCHEMA_ARTICLES = """
CREATE TABLE IF NOT EXISTS articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT UNIQUE NOT NULL,
    canonical_url   TEXT,
    url_hash        TEXT UNIQUE NOT NULL,
    title           TEXT,
    title_hash      TEXT,
    source          TEXT,
    content         TEXT,
    published_at    TEXT,
    crawled_at      TEXT NOT NULL,
    extracted_at    TEXT,
    scored_at       TEXT,
    notified_at     TEXT,
    brainstormed_at TEXT,
    failed_at       TEXT,
    score           INTEGER,
    score_reason    TEXT,
    ideas           TEXT,
    telegram_msg_id INTEGER,
    last_error      TEXT,
    retry_count     INTEGER DEFAULT 0,
    final_state     TEXT
)
"""

_SCHEMA_LOCKS = """
CREATE TABLE IF NOT EXISTS locks (
    name        TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    owner_pid   INTEGER
)
"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_extracted_at    ON articles(extracted_at)",
    "CREATE INDEX IF NOT EXISTS idx_scored_at       ON articles(scored_at)",
    "CREATE INDEX IF NOT EXISTS idx_notified_at     ON articles(notified_at)",
    "CREATE INDEX IF NOT EXISTS idx_brainstormed_at ON articles(brainstormed_at)",
    "CREATE INDEX IF NOT EXISTS idx_failed_at       ON articles(failed_at)",
    "CREATE INDEX IF NOT EXISTS idx_score           ON articles(score)",
    "CREATE INDEX IF NOT EXISTS idx_title_hash      ON articles(title_hash)",
    "CREATE INDEX IF NOT EXISTS idx_crawled_at      ON articles(crawled_at)",
    "CREATE INDEX IF NOT EXISTS idx_final_state     ON articles(final_state)",
]


def init_db() -> None:
    """Create schema. Idempotent (uses IF NOT EXISTS). Called by install.sh v2."""
    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        conn.execute(_SCHEMA_ARTICLES)
        conn.execute(_SCHEMA_LOCKS)
        for idx in _INDEXES:
            conn.execute(idx)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def open_conn(read_only: bool = False) -> Iterator[sqlite3.Connection]:
    """Open DB connection. RO mode tolerates missing file/tables (returns empty).

    Read-only mode is used by `crawl.py --dry-run` per PLAN §2.2 and AC #1:
    must not create the file and must not create schema.
    """
    if read_only:
        # SQLite URI mode prevents writes; missing file causes the open to fail
        # with the standard sqlite3 error which we translate to an empty stand-in.
        uri = f"file:{config.DB_PATH}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.OperationalError:
            # File missing — return an in-memory connection with no articles table.
            # Caller queries return empty results.
            conn = sqlite3.connect(":memory:")
    else:
        conn = sqlite3.connect(str(config.DB_PATH))

    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# --- URL canonicalization (PLAN §4.6) --------------------------------------

_TRACKING_PARAMS = (
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "referrer",
)


def canonicalize_url(url: str) -> str:
    """Normalize URL: lowercase host, strip fragment, strip tracking params,
    normalize trailing slash. Returns the original on parse failure."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if not parsed.scheme or not parsed.netloc:
        return url

    netloc = parsed.netloc.lower()
    # Drop default ports
    if netloc.endswith(":80") and parsed.scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and parsed.scheme == "https":
        netloc = netloc[:-4]

    query_pairs = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    cleaned_query = urlencode(query_pairs, doseq=True)

    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return urlunparse((
        parsed.scheme.lower(),
        netloc,
        path,
        parsed.params,
        cleaned_query,
        "",  # drop fragment
    ))


def url_hash(canonical_url: str) -> str:
    return hashlib.md5(canonical_url.encode("utf-8")).hexdigest()


# --- Title canonicalization (PLAN §4.7) ------------------------------------

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Lowercase, strip Vietnamese diacritics, strip punctuation, collapse ws."""
    if not title:
        return ""
    decomposed = unicodedata.normalize("NFD", title)
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    no_punct = _PUNCT_RE.sub(" ", stripped.lower())
    return _WS_RE.sub(" ", no_punct).strip()


def title_hash(title: str) -> str:
    return hashlib.md5(normalize_title(title).encode("utf-8")).hexdigest()


# --- Article INSERT --------------------------------------------------------

def insert_candidate(
    *,
    url: str,
    canonical_url: str,
    title: str | None = None,
    title_h: str | None = None,
    source: str | None = None,
    content: str | None = None,
    published_at: str | None = None,
    extracted_at: str | None = None,
    scored_at: str | None = None,
    score: int | None = None,
    score_reason: str | None = None,
) -> int | None:
    """INSERT a skeleton row (or pre-scored row for --inject-test).

    Returns the new row's id, or None if a row with the same url_hash exists
    (INSERT OR IGNORE semantics).
    """
    h = url_hash(canonical_url)
    with open_conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO articles
                (url, canonical_url, url_hash, title, title_hash, source, content,
                 published_at, crawled_at, extracted_at, scored_at, score, score_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?)
            """,
            (url, canonical_url, h, title, title_h, source, content,
             published_at, extracted_at, scored_at, score, score_reason),
        )
        conn.commit()
        return cur.lastrowid if cur.rowcount else None


def url_exists(canonical_url: str) -> bool:
    h = url_hash(canonical_url)
    with open_conn(read_only=True) as conn:
        try:
            row = conn.execute(
                "SELECT 1 FROM articles WHERE url_hash = ? LIMIT 1", (h,)
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is not None


def recent_title_hash_exists(title_h: str, window_hours: int) -> bool:
    """Check whether a row with the same title_hash exists in the last N hours."""
    with open_conn(read_only=True) as conn:
        try:
            row = conn.execute(
                """
                SELECT 1 FROM articles
                 WHERE title_hash = ?
                   AND crawled_at >= datetime('now', ?)
                 LIMIT 1
                """,
                (title_h, f"-{int(window_hours)} hours"),
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is not None


# --- CAS mark helpers ------------------------------------------------------

def mark_extracted(
    article_id: int, *, title: str, source: str, content: str, title_h: str
) -> bool:
    """CAS: only updates if extracted_at IS NULL. Returns True if updated."""
    with open_conn() as conn:
        cur = conn.execute(
            """
            UPDATE articles
               SET extracted_at = datetime('now'),
                   title = COALESCE(title, ?),
                   source = COALESCE(source, ?),
                   content = ?,
                   title_hash = ?,
                   failed_at = NULL,
                   last_error = NULL
             WHERE id = ?
               AND extracted_at IS NULL
               AND final_state IS NULL
            """,
            (title, source, content, title_h, article_id),
        )
        conn.commit()
        return cur.rowcount == 1


def mark_scored(article_id: int, score: int, reason: str) -> bool:
    """CAS: only updates if scored_at IS NULL. Returns True if updated."""
    if not (1 <= score <= 5):
        raise ValueError(f"score must be 1..5, got {score}")
    with open_conn() as conn:
        cur = conn.execute(
            """
            UPDATE articles
               SET scored_at = datetime('now'),
                   score = ?,
                   score_reason = ?,
                   failed_at = NULL,
                   last_error = NULL
             WHERE id = ?
               AND scored_at IS NULL
               AND final_state IS NULL
            """,
            (score, reason, article_id),
        )
        conn.commit()
        return cur.rowcount == 1


def mark_notified(article_id: int, telegram_msg_id: int, min_score: int) -> bool:
    """CAS: only updates if notified_at IS NULL AND score >= min_score."""
    with open_conn() as conn:
        cur = conn.execute(
            """
            UPDATE articles
               SET notified_at = datetime('now'),
                   telegram_msg_id = ?,
                   failed_at = NULL,
                   last_error = NULL
             WHERE id = ?
               AND notified_at IS NULL
               AND score >= ?
               AND final_state IS NULL
            """,
            (telegram_msg_id, article_id, min_score),
        )
        conn.commit()
        return cur.rowcount == 1


def mark_brainstormed(article_id: int, ideas_json: str) -> bool:
    """Overwrite-friendly: brainstorm allows re-runs. Returns True on update."""
    with open_conn() as conn:
        cur = conn.execute(
            """
            UPDATE articles
               SET brainstormed_at = datetime('now'),
                   ideas = ?
             WHERE id = ?
               AND final_state IS NULL
            """,
            (ideas_json, article_id),
        )
        conn.commit()
        return cur.rowcount == 1


def mark_failed(article_id: int, stage: str, err: str) -> None:
    """Bump retry_count, set failed_at + last_error. If retry_count >= MAX,
    set final_state='discarded' (per PLAN §3.2 retry semantics)."""
    with open_conn() as conn:
        conn.execute(
            """
            UPDATE articles
               SET failed_at = datetime('now'),
                   last_error = ?,
                   retry_count = retry_count + 1,
                   final_state = CASE
                       WHEN retry_count + 1 >= ? THEN 'discarded'
                       ELSE final_state
                   END
             WHERE id = ?
            """,
            (f"{stage}: {err}", config.MAX_RETRIES, article_id),
        )
        conn.commit()


def mark_discarded(article_id: int) -> bool:
    with open_conn() as conn:
        cur = conn.execute(
            "UPDATE articles SET final_state='discarded' WHERE id=? AND final_state IS NULL",
            (article_id,),
        )
        conn.commit()
        return cur.rowcount == 1


def mark_archived(article_id: int) -> bool:
    # NULL != 'archived' evaluates to NULL in SQLite, so we must spell out the
    # NULL case explicitly to match active rows (final_state IS NULL).
    with open_conn() as conn:
        cur = conn.execute(
            """
            UPDATE articles
               SET final_state = 'archived'
             WHERE id = ?
               AND (final_state IS NULL OR final_state != 'archived')
            """,
            (article_id,),
        )
        conn.commit()
        return cur.rowcount == 1


# --- Read helpers ----------------------------------------------------------

def get_article(article_id: int) -> dict | None:
    with open_conn(read_only=True) as conn:
        try:
            row = conn.execute(
                "SELECT * FROM articles WHERE id = ?", (article_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return dict(row) if row else None


def get_article_by_url(url: str) -> dict | None:
    with open_conn(read_only=True) as conn:
        try:
            row = conn.execute(
                "SELECT * FROM articles WHERE url = ? OR canonical_url = ?",
                (url, url),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return dict(row) if row else None


_STAGE_PREDICATES = {
    "new": "extracted_at IS NULL AND failed_at IS NULL AND final_state IS NULL",
    "extracted": "extracted_at IS NOT NULL AND scored_at IS NULL AND final_state IS NULL",
    "scored": "scored_at IS NOT NULL AND notified_at IS NULL AND final_state IS NULL",
    "notified": "notified_at IS NOT NULL AND final_state IS NULL",
    "brainstormed": "brainstormed_at IS NOT NULL",
    "failed": "failed_at IS NOT NULL AND final_state IS NULL",
}


def list_by_stage(
    stage: str,
    *,
    limit: int | None = None,
    not_notified: bool = False,
    not_brainstormed: bool = False,
    min_score: int | None = None,
) -> list[dict]:
    """Return articles matching a stage, ordered by bootstrap-test:// priority
    then by scored_at DESC then crawled_at DESC. Thin wrapper around the SQL
    used by `list.py`; exposed for the design-contract API surface (PLAN Task 3).

    Stage is one of: new | extracted | scored | notified | brainstormed | failed.
    """
    import config

    if stage not in _STAGE_PREDICATES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {list(_STAGE_PREDICATES)}")

    where = [_STAGE_PREDICATES[stage]]
    params: list = []
    if not_notified:
        where.append("notified_at IS NULL")
    if not_brainstormed:
        where.append("brainstormed_at IS NULL")
    if min_score is not None:
        where.append("score >= ?")
        params.append(min_score)

    effective_limit = limit if limit is not None else config.MAX_ARTICLES_PER_RUN
    params.append(effective_limit)

    sql = f"""
        SELECT * FROM articles
        WHERE {' AND '.join(where)}
        ORDER BY
            CASE WHEN url LIKE '{config.BOOTSTRAP_URL_SCHEME}%' THEN 0 ELSE 1 END,
            COALESCE(scored_at, '') DESC,
            crawled_at DESC
        LIMIT ?
    """
    with open_conn(read_only=True) as conn:
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        except sqlite3.OperationalError:
            return []
