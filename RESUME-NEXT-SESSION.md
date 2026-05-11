# Status when you wake up (2026-05-12)

## TL;DR

**Backend is fully deployed and live at https://tlinh.duyet.vn.**
All 18 implementation tasks committed + pushed to GitHub. Phase A
infrastructure deploy succeeded. Bootstrap test row already seeded.

Two things waiting on you:
1. **Phase B Cowork setup** (manual UI clicks in Claude Desktop — I can't do this).
2. **macOS TCC grant** (optional — for daily auto-crawl via launchd).

Everything else is done. The pipeline is ready to test end-to-end.

---

## What's live

| Component | State | Verification |
|---|---|---|
| DNS `tlinh.duyet.vn → 14.225.29.159` | ✅ | `dig +short @1.1.1.1 tlinh.duyet.vn` |
| nginx vhost + wildcard cert | ✅ | `curl -i https://tlinh.duyet.vn/api/health` returns 200 |
| MariaDB `tlinh_news` + 2 tables | ✅ | `articles` + `locks`, with notify_claimed_at columns |
| PHP backend (10 routes) | ✅ | `/api/health` returns `{"status":"ok","db":"connected","version":"v2"}` |
| `tlinh` DB user (DML-only grants) | ✅ | `SELECT, INSERT, UPDATE, DELETE` on `tlinh_news.*` |
| Mac venv + httpx + trafilatura + feedparser | ✅ | `install.sh` ran cleanly |
| Mac `.env` + `SKILLS/.env` (mode 600) | ✅ | Both gitignored |
| Bootstrap test row | ✅ | id=207, token `e3429f9e30474866`, score=5, notified_at=NULL |

---

## What needs your action (when convenient)

### 1. Phase B Cowork setup (required for daily auto-pipeline)

In Claude Desktop:

1. **Create a Cowork project** pointing at `/Users/theduyet/Documents/Code/vin-automate/`.
2. **Grant bash + network "Allow all"**.
3. **Open Cowork chat → `/schedule`** → paste the entire contents of
   `cowork-task-prompt.md`. Set:
   - Frequency: **Daily**
   - Name: **vinfast-pipeline**
   - Save.
4. **Click "Run now"** on `vinfast-pipeline`.
5. **Within 10 minutes**, your Telegram should receive a message containing
   the token `e3429f9e30474866` — that's the bootstrap verification.
6. Once verified, the pipeline runs daily without further action.

### 2. macOS TCC grant (optional — for daily Mac-side auto-crawl)

The launchd job that auto-runs `main.py` daily at 07:00 is currently
**blocked by macOS TCC** because the repo lives in `~/Documents`.

Two options:

**Option A — Grant /bin/bash Full Disk Access:**
- System Settings → Privacy & Security → Full Disk Access → `+` → add `/bin/bash`.
- Then re-run `bash scripts/deploy.sh` — Step 9 will succeed and the launchd job
  will tick at 07:00 every day.

**Option B — Run main.py manually whenever you want:**
```bash
cd /Users/theduyet/Documents/Code/vin-automate
.venv/bin/python main.py
```
The Cowork-side scheduled task already runs daily independent of this, so
you only need to do Option A or B if you want fresh crawled articles each
day (vs. only what the bootstrap row provides).

### 3. Clean up

After Phase B is verified:
```bash
cd /Users/theduyet/Documents/Code/vin-automate
rm -fP deploy.env   # macOS BSD overwrite-before-unlink
```

---

## Codebase summary

22 commits over this session. 7 implementation commits + 1 deploy fix +
~10 plan/setup commits.

```
8564095 Deploy-time hardening fixes (Codex APPROVE, 4 rounds)
3be5853 Tasks 17-18: README v2 + E2E-VALIDATION-v2 (Codex APPROVE, 5 rounds)
2a77787 Tasks 15-16: launchd plist + main.py + install.sh slim (Codex APPROVE, 3 rounds)
de5f292 Tasks 13-14: Cowork prompt + idea-brainstormer skill v2 (Codex APPROVE, 6 rounds)
f70aeb3 Tasks 10-12: Mac client v2 + v1 cleanup (Codex APPROVE, 3 rounds)
87d8e5a Tasks 8+8a+9: deploy / DNS / telegram scripts (Codex APPROVE, 5 rounds)
daf5274 Tasks 3-6: PHP route handlers + Telegram client (Codex APPROVE, 4 rounds)
1685f29 Tasks 2+7: PHP backend skeleton + Hashing (Codex APPROVE, 2 rounds)
ee8708b Task 1: vps/schema.sql (Codex impl-review APPROVE, 2 rounds)
3dad8ae PLAN-v2 approved by Codex (9 rounds, 22 issues resolved)
```

**Codex review trail:**
- Plan review: 9 rounds, 22 issues
- Impl reviews: 8 sessions, ~38 rounds, ~50 issues

Total **~47 review rounds, ~72 issues resolved** before anything merged.

---

## GitHub

https://github.com/dazzxq/vin-automate (commit `8564095`)

---

## If something breaks

| Symptom | Action |
|---|---|
| Telegram never arrives after Run now | Check VPS PHP error log: `ssh vps-root 'journalctl -u php8.5-fpm --since "10 minutes ago"' \| grep notify.` |
| `/api/health` 503 | `ssh vps-root 'mariadb -u tlinh -p\$DB_PASS -e "SELECT 1"'` (using ROOT_DB_PASS from deploy.env if needed) |
| Cowork bash hard-exits | `ls -la SKILLS/.env` — must exist + be mode 600 |
| Need to re-run deploy | `bash scripts/deploy.sh` — idempotent, all steps short-circuit on re-run |

All commands in `E2E-VALIDATION-v2.md` for the §13 acceptance matrix.

---

## Codex sessions (history)

| Session | Rounds | Verdict |
|---|---|---|
| codex-plan-review-20260511-004 | 9 | APPROVE |
| codex-impl-review-20260511-003..010 | 2/2/4/5/3/6/3/5 | All APPROVE |
| codex-impl-review-20260511-011 (deploy-fixes) | 4 | APPROVE |
