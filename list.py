#!/usr/bin/env python3
"""Query articles by stage, score, time, search. JSON output for Claude.

Stage projection (per PLAN §3.3): timestamps → readable stage column.
Ordering (per PLAN §4.8/§4.8.5 ISSUE-10/15): rows with reserved
url scheme `bootstrap-test://%` ALWAYS sort first so scheduler
verification rows never starve behind real backlog.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta

import config
from db import open_conn


STAGE_KEYS = ("new", "extracted", "scored", "notified", "brainstormed", "failed")


def _project_stage(row: dict) -> str:
    if row.get("final_state"):
        return row["final_state"]
    if row.get("failed_at") and not row.get("notified_at"):
        return "failed"
    if row.get("brainstormed_at"):
        return "brainstormed"
    if row.get("notified_at"):
        return "notified"
    if row.get("scored_at"):
        return "scored"
    if row.get("extracted_at"):
        return "extracted"
    return "new"


def _build_query(args) -> tuple[str, list]:
    where: list[str] = []
    params: list = []

    if args.id is not None:
        where.append("id = ?")
        params.append(args.id)

    if args.stage:
        # Translate stage to a predicate on timestamps + final_state.
        if args.stage == "new":
            where.append("extracted_at IS NULL AND failed_at IS NULL AND final_state IS NULL")
        elif args.stage == "extracted":
            where.append("extracted_at IS NOT NULL AND scored_at IS NULL AND final_state IS NULL")
        elif args.stage == "scored":
            where.append("scored_at IS NOT NULL AND notified_at IS NULL AND final_state IS NULL")
        elif args.stage == "notified":
            where.append("notified_at IS NOT NULL AND final_state IS NULL")
        elif args.stage == "brainstormed":
            where.append("brainstormed_at IS NOT NULL")
        elif args.stage == "failed":
            where.append("failed_at IS NOT NULL AND final_state IS NULL")

    if args.score is not None:
        where.append("score = ?")
        params.append(args.score)
    if args.min_score is not None:
        where.append("score >= ?")
        params.append(args.min_score)
    if args.max_score is not None:
        where.append("score <= ?")
        params.append(args.max_score)

    if args.not_scored:
        where.append("scored_at IS NULL")
    if args.not_notified:
        where.append("notified_at IS NULL")
    if args.not_brainstormed:
        where.append("brainstormed_at IS NULL")

    if args.final_state:
        if args.final_state == "active":
            where.append("final_state IS NULL")
        else:
            where.append("final_state = ?")
            params.append(args.final_state)

    if args.today:
        where.append("crawled_at >= datetime('now', 'start of day', 'localtime')")
    if args.yesterday:
        where.append(
            "crawled_at >= datetime('now', 'start of day', '-1 day', 'localtime') "
            "AND crawled_at < datetime('now', 'start of day', 'localtime')"
        )
    if args.last_hours is not None:
        where.append("crawled_at >= datetime('now', ?)")
        params.append(f"-{int(args.last_hours)} hours")
    if args.since:
        # Validate format then pass through
        try:
            datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            print(f"[error] --since must be YYYY-MM-DD, got {args.since}", file=sys.stderr)
            sys.exit(2)
        where.append("crawled_at >= ?")
        params.append(args.since)

    if args.search:
        like = f"%{args.search}%"
        where.append("(title LIKE ? OR content LIKE ?)")
        params.extend([like, like])

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    # Priority order: verification rows (bootstrap-test://%) first; then most
    # recently scored; then most recently crawled.
    sql = f"""
        SELECT * FROM articles
        {where_clause}
        ORDER BY
            CASE WHEN url LIKE '{config.BOOTSTRAP_URL_SCHEME}%' THEN 0 ELSE 1 END,
            COALESCE(scored_at, '') DESC,
            crawled_at DESC
        LIMIT ?
    """
    params.append(args.limit)
    return sql, params


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stage", choices=STAGE_KEYS)
    # --score-gte is an alias of --min-score; --score-lte is an alias of --max-score.
    p.add_argument("--min-score", "--score-gte", dest="min_score", type=int)
    p.add_argument("--max-score", "--score-lte", dest="max_score", type=int)
    p.add_argument("--score", type=int, help="exact score match")
    p.add_argument("--not-scored", action="store_true")
    p.add_argument("--not-notified", action="store_true")
    p.add_argument("--not-brainstormed", action="store_true")
    p.add_argument("--final-state", choices=("discarded", "archived", "active"))
    p.add_argument("--id", type=int)
    p.add_argument("--today", action="store_true")
    p.add_argument("--yesterday", action="store_true")
    p.add_argument("--last-hours", type=int)
    p.add_argument("--since", type=str, help="YYYY-MM-DD")
    p.add_argument("--search", type=str)
    p.add_argument("--limit", type=int, default=config.MAX_ARTICLES_PER_RUN)
    p.add_argument("--json", action="store_true", help="emit JSON instead of table")
    args = p.parse_args(argv)

    sql, params = _build_query(args)

    with open_conn(read_only=True) as conn:
        try:
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        except sqlite3.OperationalError:
            rows = []

    for r in rows:
        r["stage"] = _project_stage(r)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, default=str))
        return 0

    if not rows:
        print("(no rows)")
        return 0

    # Table mode — concise human-readable output
    print(f"{'ID':>4} {'STAGE':<13} {'SCORE':>5} {'SOURCE':<20} TITLE")
    print("-" * 100)
    for r in rows:
        title = (r.get("title") or "")[:60]
        source = (r.get("source") or "")[:20]
        score = r.get("score")
        print(f"{r['id']:>4} {r['stage']:<13} {str(score) if score is not None else '-':>5} {source:<20} {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
