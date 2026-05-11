#!/usr/bin/env python3
"""CLI to write per-stage timestamps from outside (Claude in Cowork calls these).

Subcommands:
  mark.py score <id> <N> "<reason>"     — CAS: only if scored_at IS NULL
  mark.py brainstorm <id> '<json>'      — Overwrite (re-brainstorm OK)
  mark.py discard <id>                  — set final_state='discarded'
  mark.py archive <id>                  — set final_state='archived'

All CAS subcommands exit 0 on success OR no-op (with log line).
Invalid input (bad score, bad JSON) → exit 2.
"""

from __future__ import annotations

import argparse
import json
import sys

import db
from _logging import get_logger


def _setup_logger():
    return get_logger("mark")


def cmd_score(args) -> int:
    log = _setup_logger()
    if not (1 <= args.score <= 5):
        log.error("score must be 1..5, got %d", args.score)
        return 2
    updated = db.mark_scored(args.id, args.score, args.reason)
    if updated:
        log.info("[mark.py] row %d scored %d/5", args.id, args.score)
    else:
        log.info("[mark.py] row %d already scored or discarded; no-op", args.id)
    return 0


def cmd_brainstorm(args) -> int:
    log = _setup_logger()
    try:
        ideas = json.loads(args.ideas_json)
    except json.JSONDecodeError as e:
        log.error("invalid JSON: %s", e)
        return 2

    if not isinstance(ideas, list) or len(ideas) != 5:
        log.error("expected JSON array of exactly 5 ideas, got %s of length %s",
                  type(ideas).__name__, len(ideas) if isinstance(ideas, list) else "n/a")
        return 2

    required = {"title", "angle", "format", "difficulty", "viral_potential"}
    for i, item in enumerate(ideas):
        if not isinstance(item, dict):
            log.error("idea %d not an object", i)
            return 2
        missing = required - set(item.keys())
        if missing:
            log.error("idea %d missing fields: %s", i, sorted(missing))
            return 2

    canonical = json.dumps(ideas, ensure_ascii=False, sort_keys=False)
    updated = db.mark_brainstormed(args.id, canonical)
    if updated:
        log.info("[mark.py] row %d brainstormed (5 ideas)", args.id)
    else:
        log.info("[mark.py] row %d not updated (row missing or discarded)", args.id)
    return 0


def cmd_discard(args) -> int:
    log = _setup_logger()
    if db.mark_discarded(args.id):
        log.info("[mark.py] row %d discarded", args.id)
    else:
        log.info("[mark.py] row %d already discarded; no-op", args.id)
    return 0


def cmd_archive(args) -> int:
    log = _setup_logger()
    if db.mark_archived(args.id):
        log.info("[mark.py] row %d archived", args.id)
    else:
        log.info("[mark.py] row %d already archived; no-op", args.id)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_score = sub.add_parser("score")
    p_score.add_argument("id", type=int)
    p_score.add_argument("score", type=int)
    p_score.add_argument("reason", type=str)
    p_score.set_defaults(func=cmd_score)

    p_brain = sub.add_parser("brainstorm")
    p_brain.add_argument("id", type=int)
    p_brain.add_argument("ideas_json", type=str)
    p_brain.set_defaults(func=cmd_brainstorm)

    p_disc = sub.add_parser("discard")
    p_disc.add_argument("id", type=int)
    p_disc.set_defaults(func=cmd_discard)

    p_arch = sub.add_parser("archive")
    p_arch.add_argument("id", type=int)
    p_arch.set_defaults(func=cmd_archive)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
