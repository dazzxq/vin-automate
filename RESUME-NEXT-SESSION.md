# Resume notes — for next session

## Current state (2026-05-12, after Codex APPROVE)

**PLAN-v2.md: APPROVED** by Codex plan-review session
`codex-plan-review-20260511-004` after 9 review rounds (22 issues
ISSUE-8..29 resolved). Plan covers v2 3-tier architecture (Mac crawl +
VPS PHP/MariaDB backend + Cowork AI scoring + Telegram).

### Critical infrastructure context (from user 2026-05-12)
- Wildcard `*.duyet.vn` cert pre-installed at
  `/etc/letsencrypt/live/duyet.vn/` (SAN includes `*.duyet.vn` and apex,
  valid until 2026-06-21). **Step 6 does NOT run certbot** — only verifies.
- VPS SSH alias `vps-root` working (Ubuntu 24.04, nginx, PHP 8.5, MariaDB 10.11).
- CF Global API key in keychain (`security find-generic-password -s cloudflare-global-api-key -a the@duyet.dev -w`).
- User granted bypass_permissions for autonomous run; no need to ask before
  destructive ops within v2 implementation scope.

### Implementation order (per §11 task breakdown)
1. `vps/schema.sql` — DB tables with notify_claimed_at/notify_claim_owner
2-7. PHP backend (bootstrap, Db, Auth, Router, hashing, all routes)
8. `scripts/deploy.sh` + `vps/nginx-tlinh.conf.tpl` (Phase A)
8a. `scripts/telegram-nonce-helper.sh`
9. `scripts/dns_setup.sh`
10. `api_client.py`
11. `crawl.py` rewrite (HTTP-based)
12. `extract.py` rewrite (HTTP-based)
13. `cowork-task-prompt.md` (rewritten HTTP-based)
14. `SKILLS/idea-brainstormer.skill` (rewritten HTTP-based)
15. `com.tlinh.crawl.plist` + `main.py`
16. `install.sh` (slim — venv + deps only)
17. `README.md` v2
18. `E2E-VALIDATION-v2.md`

### Codex impl-review protocol (per CLAUDE.md)
- Each task: implement → `/codex-impl-review` → fix → commit
- Issues from Codex impl-review must be fixed and re-reviewed until APPROVE
  before commit.

### Files state

| File | State |
|---|---|
| `PLAN.md` | v1 plan (architecturally obsolete) |
| `PLAN-v2.md` | **APPROVED** — implementation spec |
| `RESUME-NEXT-SESSION.md` | this file |
| `.codex-round*-pending.json` | Codex review snapshots (gitignored) |
| `crawl.py`, `extract.py`, ... | v1 Python — to be REWRITTEN per Tasks 11-12 |
| `backend/` | NOT yet created — Tasks 1-7 |
| `scripts/` | NOT yet created — Tasks 8, 8a, 9 |

### Active GitHub repo
https://github.com/dazzxq/vin-automate (public)

### Codex sessions (history)

| Session | Topic | Status |
|---|---|---|
| codex-plan-review-20260511-001 | v1 plan original | APPROVE (16 rounds, 30 issues) |
| codex-plan-review-20260511-002 | v1 onboarding delta | APPROVE (7 rounds, 17 issues) |
| codex-impl-review-20260511-001 | v1 Task 1 impl | APPROVE (2 rounds, 3 issues) |
| codex-impl-review-20260511-002 | v1 Tasks 2-18 impl | APPROVE (7 rounds, 16 issues) |
| codex-plan-review-20260511-003 | v2 plan — session 1 | finalized partway, replaced by -004 |
| codex-plan-review-20260511-004 | **v2 plan — APPROVED** | APPROVE (9 rounds, 22 issues) |
