#!/usr/bin/env python3
"""Mac-side daily crawl + extract orchestrator (launched by launchd).

Runs once per scheduled tick:
  1. crawl.py  — fetches RSS, POSTs candidates to the VPS.
  2. extract.py — pulls server-side stage=new rows and PATCHes content back.

Exit codes (strict contract per Codex impl-review):
  0  — both stages exited 0.
  1  — uncaught Python exception in main() (orchestrator bug or import error).
  2  — one or more stages exited non-zero through normal control flow
       (e.g. api_client.ApiError surfaced as a non-zero return).

Cowork's scheduled daily task (cowork-task-prompt.md) takes over from there:
score + notify operate against rows we just pushed.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_stage(name: str, runner) -> int:
    """Invoke runner([]) and translate SystemExit to its rc. Any other
    exception is re-raised so the top-level handler in main() can turn it
    into the exit-1 contract."""
    print(f"[{stamp()}] stage: {name}", flush=True)
    try:
        rc = runner([])
    except SystemExit as e:
        rc = int(e.code or 0)
    if rc != 0:
        print(f"[{stamp()}] {name} exited rc={rc}", file=sys.stderr, flush=True)
    return rc


def main() -> int:
    # STDOUT banner BEFORE any imports — deploy.sh Step 9's smoke test polls
    # logs/crawl.out for non-empty output within 30s to confirm the job ran.
    # If we deferred the banner until after imports, an import-time error
    # would emit only to stderr and the smoke check would (incorrectly)
    # conclude the job didn't run at all (per Codex impl-review).
    print(f"[{stamp()}] vin-automate main: starting daily crawl + extract", flush=True)

    # Import inside main so import-time errors bubble through the top-level
    # try/except below as exit 1, not silently before the contract takes hold.
    import crawl
    import extract
    overall_rc = 0

    crawl_rc = _run_stage("crawl.py", crawl.main)
    if crawl_rc != 0:
        overall_rc = 2

    # Brief pause so the VPS sees the new rows before extract polls.
    time.sleep(2)

    extract_rc = _run_stage("extract.py --pending", extract.main)
    if extract_rc != 0:
        overall_rc = 2

    print(f"[{stamp()}] vin-automate main: done (rc={overall_rc})", flush=True)
    return overall_rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — top-level catch by design
        print(f"[{stamp()}] vin-automate main UNCAUGHT: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        # Re-raise into stderr trace so logs/crawl.err has the full picture,
        # then exit 1 per the contract.
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
