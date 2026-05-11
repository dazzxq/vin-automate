# E2E Validation v2

This document operationalizes the §13 acceptance matrix from `PLAN-v2.md`.
Each scenario gives the exact command sequence and the expected evidence so
a fresh operator can confirm the pipeline works end-to-end.

Prereqs:
- Phase A complete (`bash scripts/deploy.sh` returned 0).
- Phase B Steps 10–11 complete (Cowork project created, scheduled task saved).
- `deploy.env` still on disk (re-source via `set -a && . deploy.env && set +a`).

All curl examples assume `$API_BASE_URL` + `$API_TOKEN` come from
`SKILLS/.env` or `deploy.env`. Replace `<TOKEN>` with the bootstrap token
you generate per scenario.

---

## AC #1 — Mac crawler inserts rows into VPS

Run from Mac repo root:
```bash
.venv/bin/python crawl.py --topic vinfast
```

Expected:
- stdout includes `"posted": N` for some N >= 0 in the topic summary.
- VPS confirms: `mariadb -u tlinh -p tlinh_news -e 'SELECT count(*) FROM articles WHERE crawled_at > NOW() - INTERVAL 1 HOUR'` returns >= 1 (typically 5–30).

### AC #1-rerun — Idempotency

Re-run `crawl.py` immediately. Second run's `posted` count should be 0 (or
much lower); `already_seen` should grow. No duplicate rows in DB:
```sql
SELECT url_hash, COUNT(*) FROM articles GROUP BY url_hash HAVING COUNT(*) > 1;
-- empty result
```

---

## AC #2 — Mac extractor fills content

```bash
.venv/bin/python extract.py --pending
```

Expected:
- stdout: `[extracted] row N (M chars)` for each successful row.
- DB: `SELECT id, extracted_at, LENGTH(content) FROM articles WHERE extracted_at > NOW() - INTERVAL 5 MINUTE` shows new extracted_at + content length > 200.

---

## AC #3 — Cowork scheduled task scores + notifies

In Claude Desktop's Cowork UI for the `vinfast-pipeline` task, click
**Run now**. Wait up to 10 minutes.

Expected:
- Telegram receives one or more messages for high-score articles.
- DB rows: `notified_at IS NOT NULL` for the corresponding ids.

---

## AC #4 — /idea-brainstormer skill writes brainstorm

In a Cowork chat:
```
/idea-brainstormer 42
```
(use any scored, non-bootstrap article id from `GET /api/articles?stage=scored&limit=10`.)

Expected:
- Cowork displays a 5-idea table.
- `SELECT brainstormed_at, JSON_LENGTH(ideas) FROM articles WHERE id = 42`
  returns timestamp NOT NULL and length 5.

---

## AC #5 — Concurrency safety (run-level lock + per-row CAS + notify 2-phase)

### 5a — Run-level lock conflict
From two Mac shells:
```bash
# Shell A
curl -fsS -X POST -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  "https://$FQDN/api/lock/pipeline-run/acquire" -d '{"ttl_seconds":120}'
# 201 with owner_id

# Shell B (within 120s)
curl -X POST -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' -i \
  "https://$FQDN/api/lock/pipeline-run/acquire" -d '{"ttl_seconds":120}'
# HTTP/2 409 + {"error":"lock_held",...}
```

### 5b — Per-row CAS no-op on second PATCH /score
```bash
ID=<some-extracted-id>
curl -fsS -X PATCH -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  "https://$FQDN/api/articles/$ID/score" -d '{"score":5,"reason":"first"}'
# 200 {"updated":true,...}

curl -fsS -X PATCH -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  "https://$FQDN/api/articles/$ID/score" -d '{"score":3,"reason":"second"}'
# 200 {"updated":false,"reason":"already_scored"}
# DB still has score=5, reason="first"
```

### 5c — Notify 2-phase atomicity under concurrent POST
Mock test via two concurrent shell calls to `POST /api/notify/$ID` for the
same id (must have score >= MIN_SCORE_TO_NOTIFY and notified_at NULL).
Expect exactly ONE response with `sent:true`; the other returns
`sent:false, reason:"already_claimed"`. Only one Telegram message arrives.

### 5b-live — Live-owner heartbeat during slow Telegram
Manual: temporarily edit `vps/src/Telegram.php` to add a `sleep(200)` before
`curl_exec`. While the first POST /notify is sleeping, fire a second from
another shell. Expected: the second gets `already_claimed` because the
heartbeat task is refreshing `notify_claimed_at` every 20s. Revert the
edit before re-deploying.

### 5b-dead — Stale claim recovery
```sql
UPDATE articles SET notify_claimed_at = NOW() - INTERVAL 400 SECOND,
       notify_claim_owner = 'abcdef00000000000000000000000000'
 WHERE id = <some-eligible-id>;
```
Then POST /notify on that id. Expected: Phase 1c reclaims the stale claim
and Phase 2 sends.

### 5a — Permanent Telegram failure (HTTP 4xx other than 429)

Force a permanent failure by temporarily setting an invalid `TELEGRAM_CHAT_ID`
on the VPS (Telegram returns 400 `Bad Request: chat not found`):

```bash
# Backup current chat_id, set to invalid, reload php-fpm:
{ printf 'BAD_ID=999999999999\n'
  cat <<'REMOTE'
ENV=/var/www/tlinh/tlinh.duyet.vn/shared/.env
cp "$ENV" "${ENV}.bak"
sed -i "s|^TELEGRAM_CHAT_ID=.*|TELEGRAM_CHAT_ID=$BAD_ID|" "$ENV"
systemctl reload php8.5-fpm
REMOTE
} | ssh vps-root 'bash -s'

# Pick a FRESH notify-eligible row (retry_count=0 — otherwise the forced
# 400 would bump it to retry_count >= MAX_RETRIES and discard, breaking
# the "subsequent POST succeeds" step below).
ID=$(curl -fsS -H "Authorization: Bearer $API_TOKEN" \
  "https://$FQDN/api/articles?stage=scored&not_notified=1&limit=50" \
  | jq -r '.rows | map(select(.retry_count == 0)) | .[0].id')
if [ -z "$ID" ] || [ "$ID" = "null" ]; then
  echo "[fatal] no row with retry_count=0; seed one via inject-test (AC #11) first" >&2
  exit 1
fi
echo "Using fresh row id=$ID for permanent-failure test"

# First POST — server tries to send, Telegram refuses 400, server releases
# claim + calls /fail internally + returns 502:
curl -i -X POST -H "Authorization: Bearer $API_TOKEN" "https://$FQDN/api/notify/$ID"
# HTTP/2 502
# {"error":"telegram_permanent","status":400,"detail":"Bad Request: chat not found", "reason":"permanent_4xx"}

# Verify state cleared + retry_count bumped:
curl -fsS -H "Authorization: Bearer $API_TOKEN" "https://$FQDN/api/articles/$ID" \
  | jq '{notify_claimed_at, notify_claim_owner, notified_at, retry_count, failed_at}'
# notify_claimed_at: null, notify_claim_owner: null, notified_at: null
# retry_count: 1 (incremented), failed_at: <timestamp>

# Restore the real chat_id:
ssh vps-root 'mv /var/www/tlinh/tlinh.duyet.vn/shared/.env.bak /var/www/tlinh/tlinh.duyet.vn/shared/.env && systemctl reload php8.5-fpm'

# Subsequent POST succeeds Phase 1 again (claim re-available):
curl -fsS -X POST -H "Authorization: Bearer $API_TOKEN" "https://$FQDN/api/notify/$ID" | jq
# {"id":$ID,"sent":true,"telegram_msg_id":...,"notified_at":"..."}
```

### 5b-cap — Retry-After cap

To exercise the real over-cap branch (which logs
`notify.retry_after_exceeded_cap` from `sendMessage`'s case-429 handler),
we must patch `httpPost()` on the DEPLOYED VPS file so the rest of
`sendMessage` runs normally and the cap branch fires:

```bash
# Patch the deployed copy (current symlinked release), not the local repo.
ssh vps-root bash -s <<'REMOTE'
set -euo pipefail
TG=/var/www/tlinh/tlinh.duyet.vn/current/src/Telegram.php
cp "$TG" "$TG.bak"

# Replace the body of httpPost() with a synthetic 429 + Retry-After: 9999.
# This way the case-429 handler in sendMessage() reads retry_after=9999,
# compares to NOTIFY_TELEGRAM_RETRY_AFTER_CAP_SECONDS (60), and triggers
# the cap-exceeded branch — which emits the required error_log line.
python3 - <<'PY' "$TG"
import re, sys
path = sys.argv[1]
s = open(path).read()
patched = re.sub(
    r"(private static function httpPost\([^)]*\): array\s*\{)[\s\S]*?(?=\n    \}\n)",
    r"\1\n        return ['status' => 429, 'body' => '{\"error\":\"mock retry-after\"}', 'headers' => ['retry-after' => '9999']];\n        // INTENTIONAL_DEAD_CODE_FOLLOWS",
    s, count=1,
)
open(path, "w").write(patched)
PY
systemctl reload php8.5-fpm
REMOTE

# Trigger notify on a fresh row.
ID=<some-fresh-id-with-retry_count-0>
curl -i -X POST -H "Authorization: Bearer $API_TOKEN" "https://$FQDN/api/notify/$ID"
# HTTP/2 502 {"error":"telegram_permanent",...,"reason":"retry_after_exceeded_cap"}

# Verify the log line was emitted with the right cap + retry_after.
# PHP's error_log() routes through php-fpm's error.log on Ubuntu by default.
ssh vps-root 'journalctl -u php8.5-fpm --since "5 minutes ago" 2>/dev/null; tail -200 /var/log/nginx/error.log /var/log/php8.5-fpm.log 2>/dev/null' \
  | grep 'notify.retry_after_exceeded_cap'
# notify.retry_after_exceeded_cap: retry_after=9999 cap=60

# Verify DB state cleared + retry_count++:
curl -fsS -H "Authorization: Bearer $API_TOKEN" "https://$FQDN/api/articles/$ID" \
  | jq '{notify_claimed_at, notify_claim_owner, retry_count, failed_at}'
# notify_claimed_at: null, notify_claim_owner: null, retry_count: 1, failed_at: <ts>

# Restore the deployed file:
ssh vps-root 'mv /var/www/tlinh/tlinh.duyet.vn/current/src/Telegram.php.bak \
              /var/www/tlinh/tlinh.duyet.vn/current/src/Telegram.php && systemctl reload php8.5-fpm'
```

### 5b-boot — TTL/heartbeat invariant boot validation
```bash
ssh vps-root 'sed -i "s|^NOTIFY_CLAIM_TTL_SECONDS=.*|NOTIFY_CLAIM_TTL_SECONDS=60|" /var/www/tlinh/tlinh.duyet.vn/shared/.env'
curl https://$FQDN/api/health
# 500 with {"error":"notify_claim_config_invalid",...}
# revert with deploy.sh re-run
```

---

## AC #6 — HTTPS + bearer auth

```bash
curl -fsS "https://$FQDN/api/health"
# 200 {"status":"ok","db":"connected","version":"v2-<sha>"}

curl -i "https://$FQDN/api/articles"
# HTTP/2 401 {"error":"unauthorized"}

curl -i -H "Authorization: Bearer wrong" "https://$FQDN/api/articles"
# HTTP/2 401 {"error":"unauthorized"}

curl -fsS -H "Authorization: Bearer $API_TOKEN" "https://$FQDN/api/articles"
# 200 {"rows":[...],"count":N}
```

---

## AC #7 — Idempotent endpoints

- `POST /api/articles` with same payload twice → 2nd response has `created: false`, 1 row in DB.
- `PATCH /score` twice → 2nd has `updated: false, reason: already_scored`.
- `DELETE /api/lock/foo` twice → both 200 (first `deleted: true`, second `deleted: false`).

---

## AC #8 — Verification rows starve-resistant

After AC #11 inject-test seeded one bootstrap row:
```bash
curl -fsS -H "Authorization: Bearer $API_TOKEN" \
  "https://$FQDN/api/articles?stage=scored&not_notified=1&limit=5" | jq '.rows[0].url'
# "bootstrap-test://<TOKEN>" — bootstrap row is FIRST regardless of real-row count.
```

---

## AC #9 — TTL lock cleanup

```bash
OWNER=$(curl -fsS -X POST -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  "https://$FQDN/api/lock/test-ttl/acquire" -d '{"ttl_seconds":1}' | jq -r .owner_id)
sleep 2
NEW_OWNER=$(curl -fsS -X POST -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  "https://$FQDN/api/lock/test-ttl/acquire" -d '{"ttl_seconds":60}' | jq -r .owner_id)
[ "$OWNER" != "$NEW_OWNER" ] && echo "OK: TTL reclaim works"
# Pass DB_PASS over ssh stdin (keeps it out of argv + lets remote see it).
{ printf 'DB_PASS=%q\n' "$DB_PASS"
  cat <<'REMOTE'
mariadb -u tlinh -p"$DB_PASS" tlinh_news -e 'SELECT count(*) FROM locks WHERE name="test-ttl"'
REMOTE
} | ssh vps-root 'bash -s'
# 1
```

---

## AC #10 — Telegram HTML safety

```bash
{ printf 'DB_PASS=%q\nID=%q\n' "$DB_PASS" "<some-bootstrap-id>"
  cat <<'REMOTE'
mariadb -u tlinh -p"$DB_PASS" tlinh_news -e "UPDATE articles SET title='<b>&*_[]</b>' WHERE id=$ID"
REMOTE
} | ssh vps-root 'bash -s'
curl -fsS -X POST -H "Authorization: Bearer $API_TOKEN" "https://$FQDN/api/notify/<id>"
# 200 sent:true
# Telegram message shows the literal characters "<b>&*_[]</b>" rendered as text, not parsed as HTML
```

---

## AC #11 — Bootstrap verification flow (per ISSUE-3 + ISSUE-12)

```bash
# 1. Pick token + tunnel from Mac
BOOTSTRAP_TOKEN=$(openssl rand -hex 8)
ssh -f -N -L 18443:127.0.0.1:443 vps-root
trap 'pkill -f "ssh -f -N -L 18443:127.0.0.1:443 vps-root" || true' EXIT

# 2. SSH-tunneled POST — passes nginx allow 127.0.0.1
curl -fsSk -X POST "https://127.0.0.1:18443/api/admin/inject-test" \
  -H "Host: $FQDN" \
  -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  --resolve "$FQDN:18443:127.0.0.1" \
  -d "{\"token\":\"$BOOTSTRAP_TOKEN\"}"
# 201 {"id":N,"created":true,"url":"bootstrap-test://<TOKEN>","score":5}

# 3. Direct from Mac WITHOUT tunnel — nginx denies
curl -i -X POST "https://$FQDN/api/admin/inject-test" \
  -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"token\":\"$BOOTSTRAP_TOKEN\"}"
# HTTP/2 403

# 4. Click Run now in Cowork → Telegram receives message containing $BOOTSTRAP_TOKEN within 10 min
# 5. Re-inject same token via tunnel → 200 {"created":false,...} idempotent
```

---

## AC #12 — Fail/retry semantics

Trigger by setting a row's URL to something unreachable:
```sql
UPDATE articles SET url='https://unreachable.invalid', canonical_url='https://unreachable.invalid'
 WHERE id=<some-id-with-extracted_at-null>;
```
Run `.venv/bin/python extract.py --id <id>`. After each call, check:
```sql
SELECT retry_count, failed_at, final_state, last_error FROM articles WHERE id=<id>;
```
- 1st run: `retry_count=1`, `failed_at` set, row still in `stage=new`.
- 2nd run: `retry_count=2`, still in queue.
- 3rd run: `retry_count=3`, `final_state='discarded'`, removed from `stage=new`.

Verify exclusion:
```bash
curl -fsS -H "Authorization: Bearer $API_TOKEN" \
  "https://$FQDN/api/articles?stage=new&limit=200" | jq '.rows[] | select(.id==<id>)'
# empty
curl -fsS -H "Authorization: Bearer $API_TOKEN" \
  "https://$FQDN/api/articles?final_state=discarded&limit=200" | jq '.rows[] | select(.id==<id>)'
# returns the row
```

---

## AC #13 — Single auth path enforced

```bash
mv SKILLS/.env SKILLS/.env.bak
# run any Cowork bash block from cowork-task-prompt.md →
# Expected stderr: "[error] SKILLS/.env missing or unreadable" + exit 1
mv SKILLS/.env.bak SKILLS/.env

grep -rE 'API_TOKEN=[a-f0-9]{16,}' SKILLS/ cowork-task-prompt.md
# zero matches

git check-ignore SKILLS/.env
# returns the path → confirmed gitignored
```

---

## AC #14 — Rate limiting (per ISSUE-10 / 15 / 16 / 20)

Note: per the Step 6 vhost, `/api/lock/` is in BOTH the `tlinh_lock` bucket
(600 req/min + burst 100) AND `tlinh_hourly` (1000 req/h + burst 50). The
heartbeat test below stays comfortably under both.

```bash
# /api/lock heartbeats — pace below the tlinh_hourly bucket. /api/lock/ is
# in BOTH tlinh_lock (600/min + burst 100) AND tlinh_hourly (1000/h ≈ 16.7/min
# + burst 50). 100 req in 60s would exhaust the hourly burst (50) and only
# replenish ~17 tokens/min, so requests 67+ would 429. Test stays under that:
# 30 heartbeats in 60s = well below both buckets' steady-state drain.
seq 1 30 | xargs -P 5 -I _ curl -sS -o /dev/null -w '%{http_code}\n' \
  -X POST -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  "https://$FQDN/api/lock/test-rate/heartbeat" -d '{"owner_id":"deadbeef00000000000000000000beef","ttl_seconds":60}' \
  | sort | uniq -c
# Most are 409 (no lock held) — ZERO 429.

# /api/health — never rate-limited
for i in $(seq 1 1000); do curl -s -o /dev/null -w '%{http_code}\n' "https://$FQDN/api/health"; done | sort | uniq -c
# 1000 of 200, 0 of 429
ssh vps-root 'nginx -T 2>/dev/null | awk "/location = \/api\/health/,/^[[:space:]]*}$/" | grep -c limit_req'
# 0

# tlinh_hourly leaky-bucket — paced 50 /api/articles/min for 20 min isolates
# the hourly zone (well under tlinh_main's 100/min cap).
#
# Capture nginx error.log byte offset BEFORE the test so we count only lines
# emitted during this scenario (not the last 200 lines, which can be clipped
# AND contaminated by unrelated traffic — per Codex impl-review).
START_OFFSET=$(ssh vps-root 'stat -c%s /var/log/nginx/error.log')

START=$(date +%s)
for minute in $(seq 1 20); do
  for req in $(seq 1 50); do
    curl -sS -o /dev/null -w '%{http_code}\n' \
      -H "Authorization: Bearer $API_TOKEN" \
      "https://$FQDN/api/articles?stage=scored&limit=1"
  done | sort | uniq -c >> /tmp/rate-test.log
  # Pace to ~50/min by sleeping until the next-minute boundary.
  ELAPSED=$(( $(date +%s) - START ))
  TARGET=$(( minute * 60 ))
  if [ $ELAPSED -lt $TARGET ]; then sleep $(( TARGET - ELAPSED )); fi
done

# Count zone-attributed lines ONLY from the test window (tail -c +N starts
# reading at byte offset N+1, which is everything written after START_OFFSET):
{ printf 'OFFSET=%q\n' "$START_OFFSET"
  cat <<'REMOTE'
tail -c +$((OFFSET + 1)) /var/log/nginx/error.log | grep -c "zone \"tlinh_main\""
tail -c +$((OFFSET + 1)) /var/log/nginx/error.log | grep -c "zone \"tlinh_hourly\""
REMOTE
} | ssh vps-root 'bash -s'
# Expected: ~0 lines for tlinh_main, >= 200 for tlinh_hourly.
```

---

## AC #15 — Deploy runbook reproducible (per ISSUE-5 / 12 / 17 / 18)

```bash
# 1. Fresh state on VPS
ssh vps-root 'rm -rf /var/www/tlinh && mariadb -u root -e "DROP DATABASE IF EXISTS tlinh_news; DROP USER IF EXISTS \"tlinh\"@\"localhost\""'

# 2. Run Phase A
bash scripts/deploy.sh
# exits 0 with all "ok" lines; final summary mentions deferred Phase B steps

# 3. Re-run — idempotent
bash scripts/deploy.sh
# every step reports "already done" / "unchanged"; exits 0

# 4. deploy.env survives Phase A (per ISSUE-18)
ls -la deploy.env
# present, mode 600

# 5. Phase A does NOT invoke Cowork UI or call /api/admin/inject-test.
# Substring grep is unreliable because deploy.sh legitimately mentions
# those strings in nginx location block, comments, and the Phase B
# summary `cat <<NOTE` block. Use a semantic check instead:
#
# (a) No active curl/ssh call targets the admin endpoint:
grep -E '^[[:space:]]*(curl|ssh)[^#]*api/admin/inject-test' scripts/deploy.sh \
  | grep -v '^[[:space:]]*#'
# (empty — admin endpoint is only mentioned in nginx location config, not invoked)
#
# (b) No launchctl/curl/ssh action mentions "Run now":
grep -E '^[[:space:]]*(launchctl|curl|ssh)[^#]*Run now' scripts/deploy.sh
# (empty — "Run now" only appears in the Phase B summary cat <<NOTE block)
#
# (c) deploy.sh's active call sites are only Phase A scope:
awk '/^[a-z_]+\(\)/,/^}/' scripts/deploy.sh | grep -cE 'inject-test|Run now'
# 0 — no functions invoke either
```

---

## AC #16 — DNS upsert + SSL guard

```bash
# 1. Idempotent upsert
bash scripts/dns_setup.sh
bash scripts/dns_setup.sh
# Second run: "[noop] tlinh.duyet.vn already -> ..." OR "[patch]" if user changed IP.
# No duplicate records: dig +short @1.1.1.1 tlinh.duyet.vn returns exactly one line.

# 2. Duplicate cleanup (test: create a second record manually, then re-run)
# After re-run: dns_setup.sh logs "[delete-dup]" for each extra; only canonical remains.
```

---

## Cleanup after validation

```bash
# Remove test rows (DB_PASS travels over ssh stdin, not argv).
{ printf 'DB_PASS=%q\n' "$DB_PASS"
  cat <<'REMOTE'
mariadb -u tlinh -p"$DB_PASS" tlinh_news -e "DELETE FROM articles WHERE url LIKE 'bootstrap-test://%'"
mariadb -u tlinh -p"$DB_PASS" tlinh_news -e "DELETE FROM locks WHERE name = 'test-ttl'"
REMOTE
} | ssh vps-root 'bash -s'

# Shred deploy.env (final secrets cleanup). macOS lacks coreutils shred:
# use BSD `rm -fP` for best-effort overwrite-before-unlink, or
# `brew install coreutils && gshred -u deploy.env` for the GNU equivalent.
rm -fP deploy.env
```
