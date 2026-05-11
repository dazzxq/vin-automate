#!/usr/bin/env python3
"""SQLite-based mutex with TTL + heartbeat. Owner_id propagated via caller
(Claude's session memory in Cowork; positional arg required for heartbeat
and release per PLAN §4.8.3).

Subcommands:
  lock.py acquire <name> [--ttl SECS]    → prints owner_id on success, exit 0
                                          → exit 75 on PK conflict
  lock.py heartbeat <name> <owner_id>    → extends expires_at; exit 75 on lost ownership
  lock.py release <name> <owner_id>      → idempotent DELETE; exit 0 always

Exit codes:
  0   success (or release no-op)
  1   generic error
  75  lock held by another owner (acquire) or ownership lost (heartbeat)
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import uuid

import config
from _logging import get_logger
from db import open_conn


def _setup_logger():
    return get_logger("lock")


def acquire(name: str, ttl_seconds: int) -> int:
    """Insert a new lock row. Cleans up expired rows first. Atomic via PK conflict.

    Prints owner_id (UUID4 hex) to stdout on success. Exits 75 on conflict.
    """
    log = _setup_logger()
    owner_id = uuid.uuid4().hex
    pid = os.getpid()

    with open_conn() as conn:
        # Opportunistic cleanup: remove any expired locks for this name.
        # We commit cleanup separately to keep the transaction surface small.
        cleanup = conn.execute(
            "DELETE FROM locks WHERE name = ? AND expires_at < datetime('now')",
            (name,),
        )
        conn.commit()
        if cleanup.rowcount > 0:
            log.info("[lock] expired lock cleaned (name=%s, rows=%d)",
                     name, cleanup.rowcount)

        try:
            conn.execute(
                """
                INSERT INTO locks (name, owner_id, acquired_at, expires_at, owner_pid)
                VALUES (?, ?, datetime('now'), datetime('now', ?), ?)
                """,
                (name, owner_id, f"+{int(ttl_seconds)} seconds", pid),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # PK conflict — another owner currently holds the lock.
            row = conn.execute(
                "SELECT owner_pid, acquired_at, expires_at FROM locks WHERE name = ?",
                (name,),
            ).fetchone()
            if row:
                log.error(
                    "[lock] held by PID %s since %s (expires %s)",
                    row["owner_pid"], row["acquired_at"], row["expires_at"],
                )
            else:
                log.error("[lock] could not acquire %s (race?)", name)
            return 75

    # Persistent success line — BOOTSTRAP.md Phase A startup detection greps
    # logs/pipeline.log for this exact pattern: "acquire pipeline-run".
    log.info("[lock] acquire %s owner=%s pid=%d ttl=%ds",
             name, owner_id, pid, ttl_seconds)

    # Print owner_id as the SOLE stdout line so callers can $(capture) cleanly.
    print(owner_id)
    return 0


def heartbeat(name: str, owner_id: str, ttl_seconds: int) -> int:
    """Extend expires_at on a row we own. Exits 75 if ownership lost."""
    log = _setup_logger()
    with open_conn() as conn:
        cur = conn.execute(
            """
            UPDATE locks
               SET expires_at = datetime('now', ?)
             WHERE name = ? AND owner_id = ?
            """,
            (f"+{int(ttl_seconds)} seconds", name, owner_id),
        )
        conn.commit()
        if cur.rowcount == 1:
            return 0
        log.error("[lock] lost ownership: owner_id=%s not held", owner_id)
        return 75


def release(name: str, owner_id: str) -> int:
    """DELETE WHERE name AND owner_id. Idempotent — exit 0 regardless of rowcount."""
    log = _setup_logger()
    with open_conn() as conn:
        cur = conn.execute(
            "DELETE FROM locks WHERE name = ? AND owner_id = ?",
            (name, owner_id),
        )
        conn.commit()
        if cur.rowcount == 1:
            log.info("[lock] released %s (owner_id=%s)", name, owner_id)
        else:
            log.info("[lock] release no-op for %s (owner_id mismatch or already released)", name)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_acq = sub.add_parser("acquire")
    p_acq.add_argument("name")
    p_acq.add_argument("--ttl", type=int, default=config.LOCK_TTL_SECONDS,
                       help=f"TTL seconds (default: LOCK_TTL_SECONDS={config.LOCK_TTL_SECONDS})")

    p_hb = sub.add_parser("heartbeat")
    p_hb.add_argument("name")
    p_hb.add_argument("owner_id")
    p_hb.add_argument("--ttl", type=int, default=config.LOCK_TTL_SECONDS)

    p_rel = sub.add_parser("release")
    p_rel.add_argument("name")
    p_rel.add_argument("owner_id")

    args = parser.parse_args(argv)

    if args.cmd == "acquire":
        return acquire(args.name, args.ttl)
    if args.cmd == "heartbeat":
        return heartbeat(args.name, args.owner_id, args.ttl)
    if args.cmd == "release":
        return release(args.name, args.owner_id)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
