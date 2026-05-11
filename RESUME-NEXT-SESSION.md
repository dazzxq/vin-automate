# Resume notes — for next session after context compact

## Current state (2026-05-12 02:00 ICT)

**Architecture pivot:** v1 (SQLite + Cowork-as-runtime) is dead. v2 (3-tier: Mac crawl + VPS PHP/MariaDB backend + Cowork AI) is in design phase.

**v2 plan file:** `PLAN-v2.md` (~80KB, fully written). Codex review session 3 ran 2 rounds:

| Round | Resolved | New issues | Verdict |
|---|---|---|---|
| 1 | — | 1 CRIT + 4 HIGH + 2 MED (7 total) | REVISE |
| 2 | ISSUE-1..7 fixed | 6 new (1 CRIT + 5 HIGH) | REVISE |

**Pending 6 issues (saved in `.codex-round2-pending.json`):**

- **ISSUE-8 (CRITICAL):** Notify claim TTL race. NOTIFY_CLAIM_TTL_SECONDS=60s but Telegram backoff (1+2+4+8+16=31s + Retry-After) can exceed it → second caller reclaims while first still retrying → double-send. Fix: claim-refresh/heartbeat during Phase 2 retries, OR longer TTL bound by worst-case send window.

- **ISSUE-9 (HIGH):** Admin endpoint nginx `allow 127.0.0.1; deny all` NOT in vhost template (§10 Step 5). Fix: add dedicated `location = /api/admin/inject-test` block.

- **ISSUE-10 (HIGH):** Rate-limit prose vs nginx config mismatch. Prose says `/api/health` "must NEVER 429" but vhost still rate-limits it. Missing `limit_req_status 429` and `Retry-After` header config. Fix: exempt /health from limit_req OR weaken prose; add explicit nginx 429 directive.

- **ISSUE-11 (HIGH):** Sequencing bug in deploy runbook. Step 2 rsyncs to `/var/www/tlinh/...` BEFORE Step 3's `mkdir -p`. First-time deploy fails. Fix: move directory creation before rsync OR stage to /tmp first.

- **ISSUE-12 (HIGH):** Deploy script claims "end-to-end automatically" but Cowork UI steps + inject-test seed are still manual. AC #15 false. Fix: split into `infra deploy` (automated) vs `manual Cowork verify`. Add explicit SSH-tunneled inject-test before "Run now".

- **ISSUE-13 (HIGH):** `stage=new` filter doesn't exclude discarded rows. Should be `extracted_at IS NULL AND final_state IS NULL AND retry_count < MAX_RETRIES`. Also: lingering `mark.py archive` reference (mark.py deleted in v2).

## Next session resume steps

1. Read `PLAN-v2.md` + `.codex-round2-pending.json`.
2. Apply fixes for ISSUE-8 to ISSUE-13.
3. Spawn new codex-plan-review session (previous finalized): `node codex-runner.js init --skill-name codex-plan-review ...`
4. Continue rebuttal loop until APPROVE.
5. Then start implementation per §11 task breakdown.

## Files state

| File | State |
|---|---|
| `PLAN.md` | v1 plan, Codex-approved, but architecturally obsolete (SQLite-in-sandbox fail) |
| `PLAN-v2.md` | v2 plan, draft with 6 pending Codex fixes |
| `RESUME-NEXT-SESSION.md` | this file |
| `.codex-round2-pending.json` | full Codex round 2 review output for ref |
| `crawl.py`, `extract.py`, ... | v1 Python code (works on Mac side, will be heavily refactored for HTTP API client in v2) |
| `backend/` | NOT yet created — Task 1-7 of §11 |
| `scripts/` | NOT yet created — Tasks 8, 8a, 9 |

## Open architectural decisions (chốt rồi):

- VPS: 14.225.29.159 (tlinh.duyet.vn, Ubuntu 24.04, nginx + PHP 8.5 + MariaDB 10.11)
- DB user: `tlinh` (separate from root)
- Auth: single bearer token via `SKILLS/.env` + Mac `.env`
- Crawl: Mac local via launchd (not VPS-side)
- Repo strategy: same vin-automate public repo + gitignore `.env*`, `deploy.env`, `SKILLS/.env`, `backend/.env*`, `backend/logs/`, `backend/vendor/`
- Sync: rsync from Mac via `scripts/deploy.sh`
- CF: Global API key from keychain (`security find-generic-password -s cloudflare-global-api-key -a the@duyet.dev`)
- Schedule: Daily (Cowork Daily frequency)

## Active GitHub repo

https://github.com/dazzxq/vin-automate (public)

## Codex session IDs (history)

| Session | Topic | Status |
|---|---|---|
| codex-plan-review-20260511-001 | v1 plan original | APPROVE (16 rounds, 30 issues) |
| codex-plan-review-20260511-002 | v1 onboarding delta | APPROVE (7 rounds, 17 issues) |
| codex-impl-review-20260511-001 | Task 1 impl | APPROVE (2 rounds, 3 issues) |
| codex-impl-review-20260511-002 | Tasks 2-18 impl | APPROVE (7 rounds, 16 issues) |
| codex-plan-review-20260511-003 | **v2 plan — in progress, REVISE round 2** | Pending round 3 |
