# Cowork Scheduled Task — VinFast News Pipeline (v2, HTTP-based)

**Paste the ENTIRE content of this file** (from the first `## SETUP` heading to
the bottom) into Cowork's `/schedule` "New Task" prompt field. Set frequency
to **Daily**. Save as task name **`vinfast-pipeline`**.

The scheduled task runs once per day. Each run is a fresh Cowork session: no
memory of previous runs. Crawling and extraction happen on the Mac side via
launchd (`com.tlinh.crawl`) — **this task does NOT crawl or extract**. It
only scores and triggers Telegram notifications against rows already in the
VPS pipeline.

---

## SETUP — read once at the start of every scheduled run

You are the orchestrator of the VinFast news pipeline. Working directory:
this project folder (already granted Cowork access).

### Step 0 — Auth + api() preamble (paste at the top of EVERY bash block)

Cowork runs each scheduled-task bash block in a fresh process — env and shell
functions do NOT persist between blocks. Per PLAN-v2 §5.3 mandate, every bash
block in this task MUST start with this exact preamble:

```bash
set -a && . SKILLS/.env && set +a || {
  echo "[error] SKILLS/.env missing or unreadable" >&2
  exit 1
}

# api METHOD PATH [BODY_FILE] — emits one line:
#   STATUS<TAB>RETRY_AFTER<TAB>BODY
#
# Crucial design choice: any JSON body MUST be a FILE PATH (3rd arg),
# never a shell string. Cowork should use its Write tool to materialize
# the JSON to /tmp/.tlinh-payload-N.json (which handles all character
# encoding correctly), then call api() with that path. This avoids
# shell quoting hazards entirely — quotes/backslashes/newlines in
# model-generated REASON or ideas-JSON cannot break the bash command
# because they never touch the bash parser.
#
# We do NOT use 'curl -fsS' because 4xx/5xx then suppress the body and
# prevent branching on 409/429/502. Instead we capture status separately
# via -w, dump the body to a tempfile, and parse Retry-After from headers.
# The Authorization header (containing $API_TOKEN) is fed via `-K -`
# (stdin) so the token never appears in argv.
api() {
  local method="$1" path="$2" body_file="${3:-}"
  local hdrf bodyf rc
  hdrf=$(mktemp /tmp/.tlinh-h.XXXXXX); bodyf=$(mktemp /tmp/.tlinh-b.XXXXXX)
  local cfg
  cfg=$(printf 'url = "%s%s"\nheader = "Authorization: Bearer %s"\nheader = "Accept: application/json"\n' \
    "$API_BASE_URL" "$path" "$API_TOKEN")
  if [ -n "$body_file" ]; then
    if [ ! -f "$body_file" ]; then
      rm -f "$hdrf" "$bodyf"
      printf '0\t\t{"error":"local","detail":"missing body file %s"}\n' "$body_file"
      return 1
    fi
    cfg+=$'\n''header = "Content-Type: application/json"'
    rc=$(printf '%s' "$cfg" | curl -sS -X "$method" --max-time 320 -K - \
        -D "$hdrf" -o "$bodyf" -w '%{http_code}' --data-binary "@$body_file")
  else
    rc=$(printf '%s' "$cfg" | curl -sS -X "$method" --max-time 320 -K - \
        -D "$hdrf" -o "$bodyf" -w '%{http_code}')
  fi
  local retry_after
  retry_after=$(awk 'BEGIN{IGNORECASE=1} /^Retry-After:/ {sub(/^[^:]*:[ \t]*/,""); sub(/\r?$/,""); print; exit}' "$hdrf")
  local out
  out=$(cat "$bodyf")
  rm -f "$hdrf" "$bodyf"
  printf '%s\t%s\t%s\n' "${rc:-0}" "${retry_after:-}" "$out"
}
```

After sourcing, `$API_BASE_URL` + `$API_TOKEN` are in env. **NEVER echo, log,
or embed them into output.** The api() helper keeps the token in stdin
config — it does not appear in argv.

The api() output format is `STATUS<TAB>RETRY_AFTER<TAB>BODY` on one line.
Parse it like:
```bash
read -r STATUS RETRY_AFTER BODY < <(api GET '/api/health')
```

### Step 1 — Acquire the run-level lock

```bash
set -a && . SKILLS/.env && set +a || {
  echo "[error] SKILLS/.env missing or unreadable" >&2
  exit 1
}
api() {
  local method="$1" path="$2" body_file="${3:-}"
  local hdrf bodyf rc
  hdrf=$(mktemp /tmp/.tlinh-h.XXXXXX); bodyf=$(mktemp /tmp/.tlinh-b.XXXXXX)
  local cfg
  cfg=$(printf 'url = "%s%s"\nheader = "Authorization: Bearer %s"\nheader = "Accept: application/json"\n' \
    "$API_BASE_URL" "$path" "$API_TOKEN")
  if [ -n "$body_file" ]; then
    if [ ! -f "$body_file" ]; then
      rm -f "$hdrf" "$bodyf"
      printf '0\t\t{"error":"local","detail":"missing body file %s"}\n' "$body_file"
      return 1
    fi
    cfg+=$'\n''header = "Content-Type: application/json"'
    rc=$(printf '%s' "$cfg" | curl -sS -X "$method" --max-time 320 -K - \
        -D "$hdrf" -o "$bodyf" -w '%{http_code}' --data-binary "@$body_file")
  else
    rc=$(printf '%s' "$cfg" | curl -sS -X "$method" --max-time 320 -K - \
        -D "$hdrf" -o "$bodyf" -w '%{http_code}')
  fi
  local retry_after; retry_after=$(awk 'BEGIN{IGNORECASE=1} /^Retry-After:/ {sub(/^[^:]*:[ \t]*/,""); sub(/\r?$/,""); print; exit}' "$hdrf")
  local out; out=$(cat "$bodyf")
  rm -f "$hdrf" "$bodyf"
  printf '%s\t%s\t%s\n' "${rc:-0}" "${retry_after:-}" "$out"
}

# Lock acquire — retry only on 429 per R3 + §7.4 (max 3 attempts, 1s/2s/4s).
attempt=1
while [ $attempt -le 3 ]; do
  # Body file holds the small JSON literal {"ttl_seconds":1800}. We write
  # it once via Cowork's Write tool (or here-doc as a one-time exception for
  # this fixed-content JSON) and pass the path to api().
  cat > /tmp/.tlinh-lock-body.json <<'JSON'
{"ttl_seconds":1800}
JSON
  RESP=$(api POST /api/lock/pipeline-run/acquire /tmp/.tlinh-lock-body.json)
  STATUS=$(printf '%s' "$RESP" | cut -f1)
  RETRY_AFTER=$(printf '%s' "$RESP" | cut -f2)
  BODY=$(printf '%s' "$RESP" | cut -f3-)
  if [ "$STATUS" = "201" ]; then
    echo "$BODY"
    break
  fi
  if [ "$STATUS" = "429" ]; then
    sleep "${RETRY_AFTER:-$((2 ** (attempt - 1)))}"
    attempt=$((attempt + 1))
    continue
  fi
  if [ "$STATUS" = "409" ]; then
    echo "[info] lock held by another run; abort silently"
    exit 0
  fi
  echo "[error] acquire returned $STATUS: $BODY" >&2
  exit 0   # abort silently per R2 (no lock to release — we never owned it)
done
[ $attempt -gt 3 ] && { echo "[error] acquire 429'd 3 times — abort" >&2; exit 0; }
```

Decision rules:
- HTTP 201 + JSON `{"name":"pipeline-run","owner_id":"<32 hex>","expires_at":...}`
  → success. Extract `owner_id` from BODY (JSON `.owner_id`) and use as `<OWNER_ID>` for the rest of the run.
- HTTP 409 + `{"error":"lock_held",...}` → another run is in flight. Abort silently.
- HTTP 429 → lock-bucket throttle; **retry up to 3 times** with `Retry-After`
  delay (per PLAN-v2 §7.4 + R3). Lock 429s are transient — do NOT abort.
- Any other non-2xx → abort silently (we never held a lock, so nothing to release).

### PREAMBLE for every following bash block

Cowork runs each bash block in a fresh shell. **Every block below MUST
start with the same auth-source + api() definition shown in Step 1.**
For brevity, the snippets below use the placeholder `<PREAMBLE>` — replace
it literally with those two preamble blocks (set -a sourcing + api()
function definition) before running. Without the preamble, both auth and
the api() helper are undefined and the call will fail open.

### Step 2 — Read the scoring rubric

Read `scoring-rubric.md` from the project root. Apply it in Step 4. (No
bash invocation — file read via Cowork's Read tool.)

### Step 3 — Fetch articles ready to score

```bash
<PREAMBLE>
# Non-lock 429 pattern per PLAN-v2 §7.4: retry exactly ONCE with
# max(1, Retry-After) backoff, then bubble up. Lock endpoints (Steps 1/5/9)
# use the 3-retry pattern instead.
attempt=1
while [ $attempt -le 2 ]; do
  RESP=$(api GET '/api/articles?stage=extracted&not_scored=1')
  STATUS=$(printf '%s' "$RESP" | cut -f1)
  RETRY_AFTER=$(printf '%s' "$RESP" | cut -f2)
  BODY=$(printf '%s' "$RESP" | cut -f3-)
  case "$STATUS" in
    200) echo "$BODY"; break ;;
    429) if [ $attempt -lt 2 ]; then
           sleep "${RETRY_AFTER:-1}"; attempt=$((attempt + 1)); continue
         fi
         echo "[error] /articles 429 after 1 retry" >&2; exit 1 ;;
    *)   echo "[fatal] /articles list returned $STATUS: $BODY" >&2; exit 1 ;;
  esac
done
```

Parse `rows[]` from `$BODY`. Each row has `id`, `title`, `content`, `source`.
The server caps at `MAX_ARTICLES_PER_LIST` (default 50) and orders
bootstrap-test rows first so verification doesn't starve. On exit 1, apply
**R2** (release lock + abort) or **R3** (backoff + retry) per the matching
rule below.

### Step 4 — Score each article

For each article in Step 3's rows:

1. Read `title` + `content`.
2. Apply `scoring-rubric.md` → `score` (int 1–5) and `reason` (Vietnamese, ≤ 200 chars).
3. PATCH the score. **Do NOT interpolate the model-produced REASON into a
   bash command line at any point** — quotes, backslashes, or newlines can
   break the bash parser before any encoder runs. Instead, use Cowork's
   **Write tool** (not bash) to materialize the JSON payload to a file,
   then point the api() helper at the file path:

   Cowork action: invoke the **Write tool** with `file_path` = `/tmp/.tlinh-score-body.json`
   and `content` = the literal JSON, for example:
   ```json
   {"score": 4, "reason": "Số liệu Q1 cụ thể, góc phân tích market share \"VinFast vs Tesla\""}
   ```
   Cowork's Write tool handles JSON encoding correctly: nested quotes,
   newlines, and non-ASCII characters all serialize safely.

   Then run:
   ```bash
   <PREAMBLE>
   attempt=1
   while [ $attempt -le 2 ]; do
     RESP=$(api PATCH "/api/articles/<ID>/score" /tmp/.tlinh-score-body.json)
     STATUS=$(printf '%s' "$RESP" | cut -f1)
     RETRY_AFTER=$(printf '%s' "$RESP" | cut -f2)
     BODY=$(printf '%s' "$RESP" | cut -f3-)
     case "$STATUS" in
       200) echo "$BODY"; break ;;          # updated:true OR updated:false (no-op)
       429) if [ $attempt -lt 2 ]; then
              sleep "${RETRY_AFTER:-1}"; attempt=$((attempt + 1)); continue
            fi
            echo "[error] PATCH /score 429 after 1 retry" >&2; exit 1 ;;
       *)   echo "[fatal] PATCH /score returned $STATUS: $BODY" >&2; exit 1 ;;
     esac
   done
   ```

Body shapes (HTTP 200):
- `{"updated":true,"scored_at":"..."}` → scored cleanly.
- `{"updated":false,"reason":"already_scored"|"discarded_or_archived"}` →
  server-side CAS no-op; treat as success and continue.

**Every 10 scored articles**, run Step 5 (heartbeat).

### Step 5 — Heartbeat (mid-loop, every 10 rows)

```bash
<PREAMBLE>
# Heartbeat body has a fixed shape — owner_id is a 32-hex value Cowork
# captured from Step 1 (not model text), ttl_seconds is a constant. We
# write it via Write tool / heredoc to keep the file-body pattern uniform.
cat > /tmp/.tlinh-hb-body.json <<JSON
{"owner_id":"<OWNER_ID>","ttl_seconds":1800}
JSON
RESP=$(api POST "/api/lock/pipeline-run/heartbeat" /tmp/.tlinh-hb-body.json)
STATUS=$(printf '%s' "$RESP" | cut -f1)
RETRY_AFTER=$(printf '%s' "$RESP" | cut -f2)
BODY=$(printf '%s' "$RESP" | cut -f3-)
echo "status=$STATUS body=$BODY"
case "$STATUS" in
  200) ;;
  409) echo "[lost-ownership] R1: abort without release" >&2; exit 2 ;;
  429) sleep "${RETRY_AFTER:-1}"; exit 3 ;;     # R3-lock: caller retries up to 3 times per §7.4
  *)   echo "[fatal] heartbeat $STATUS" >&2; exit 1 ;;
esac
```

Cowork distinguishes exit codes per the run rules:
- `0` → keep going.
- `2` → R1 (lost ownership, HTTP 409): abort WITHOUT release.
- `3` → R3 (transient 429): retry the same call up to 3 times with backoff
  (1s / 2s / 4s plus Retry-After). **If still 429 after 3 attempts: do
  NOT reclassify as lost ownership. Abort the run silently — exit without
  calling release.** The lock TTL (30 min) will reclaim the orphaned lock.
  Rate limiting is NOT proof of ownership loss; the original holder is
  still us, just throttled.
- `1` → R2 (other non-2xx): release + abort.

### Step 6 — Heartbeat before notify

Same call as Step 5 — refresh the claim once between score and notify phases.

### Step 7 — Fetch articles ready to notify

```bash
<PREAMBLE>
attempt=1
while [ $attempt -le 2 ]; do
  RESP=$(api GET '/api/articles?stage=scored&min_score=3&not_notified=1')
  STATUS=$(printf '%s' "$RESP" | cut -f1)
  RETRY_AFTER=$(printf '%s' "$RESP" | cut -f2)
  BODY=$(printf '%s' "$RESP" | cut -f3-)
  case "$STATUS" in
    200) echo "$BODY"; break ;;
    429) if [ $attempt -lt 2 ]; then
           sleep "${RETRY_AFTER:-1}"; attempt=$((attempt + 1)); continue
         fi
         echo "[error] /articles 429 after 1 retry" >&2; exit 1 ;;
    *)   echo "[fatal] /articles list returned $STATUS: $BODY" >&2; exit 1 ;;
  esac
done
```

### Step 8 — Trigger Telegram for each scored article

```bash
<PREAMBLE>
attempt=1
while [ $attempt -le 2 ]; do
  RESP=$(api POST "/api/notify/<ID>")
  STATUS=$(printf '%s' "$RESP" | cut -f1)
  RETRY_AFTER=$(printf '%s' "$RESP" | cut -f2)
  BODY=$(printf '%s' "$RESP" | cut -f3-)
  echo "status=$STATUS body=$BODY"
  case "$STATUS" in
    200) break ;;                                          # sent:true OR sent:false (documented no-op)
    429) if [ $attempt -lt 2 ]; then
           sleep "${RETRY_AFTER:-1}"; attempt=$((attempt + 1)); continue
         fi
         echo "[error] POST /notify 429 after 1 retry" >&2; exit 1 ;;
    *)   echo "[fatal] POST /notify returned $STATUS: $BODY" >&2; exit 1 ;;
  esac
done
```

Notify requests can take up to 241 seconds (Phase 2 budget). The api() helper
sets curl --max-time 320 to accommodate. Body shapes for HTTP 200:
- `{"id":...,"sent":true,"telegram_msg_id":...,"notified_at":"..."}` → sent.
- `{"id":...,"sent":true,"telegram_msg_id":...,"warning":"claim_stolen_after_send"}`
  → message went out but claim was stolen mid-finalize. Rare. Continue.
- `{"id":...,"sent":false,"reason":"already_notified|already_claimed|below_threshold|no_score|discarded|archived"}`
  → no-op. Continue to next article.

HTTP 502 `{"error":"telegram_permanent",...}` falls under the default case
above: it triggers R2 (release + abort). The plan deliberately treats a
permanent Telegram refusal as a stop-the-line event so an oncall human can
investigate before the rest of the batch sends.

### Step 9 — Release the lock (success path)

```bash
<PREAMBLE>
cat > /tmp/.tlinh-release-body.json <<JSON
{"owner_id":"<OWNER_ID>"}
JSON
# Lock endpoint — retry up to 3 times on 429 per PLAN-v2 §7.4 + R3-lock.
attempt=1
while [ $attempt -le 3 ]; do
  RESP=$(api DELETE "/api/lock/pipeline-run" /tmp/.tlinh-release-body.json)
  STATUS=$(printf '%s' "$RESP" | cut -f1)
  RETRY_AFTER=$(printf '%s' "$RESP" | cut -f2)
  BODY=$(printf '%s' "$RESP" | cut -f3-)
  echo "status=$STATUS body=$BODY"
  case "$STATUS" in
    200) break ;;                                      # deleted:true OR deleted:false (idempotent)
    429) sleep "${RETRY_AFTER:-$((2 ** (attempt - 1)))}"; attempt=$((attempt + 1)); continue ;;
    *)   echo "[warn] release returned $STATUS (lock TTL will reclaim)" >&2; break ;;
  esac
done
[ $attempt -gt 3 ] && echo "[warn] release 429'd 3 times — lock TTL (30 min) will reclaim" >&2
```

Exit normally.

---

## GLOBAL RULES — apply uniformly to every step

**R1: Heartbeat 409 means LOST OWNERSHIP.**
On `heartbeat` returning HTTP 409 / `{"error":"lost_ownership"}`, **abort the
entire run IMMEDIATELY and do NOT call release**. Another session has
TTL-reclaimed the lock; calling release with our stale `<OWNER_ID>` is a no-op
but should not be relied on. Exit silently.

**R2: Score/notify HTTP error releases the lock then aborts.**
For any non-CAS HTTP error from `PATCH /score`, `POST /notify`, or any other
non-heartbeat call (excluding the documented HTTP 200 `updated:false` /
`sent:false` no-op responses which are normal):

1. Materialize the release body to a file (file-body pattern required by
   the api() helper signature), then call the release endpoint with the
   same 3-attempt lock-endpoint 429 retry policy as Step 9:
   ```bash
   <PREAMBLE>
   cat > /tmp/.tlinh-release-body.json <<JSON
   {"owner_id":"<OWNER_ID>"}
   JSON
   attempt=1
   while [ $attempt -le 3 ]; do
     RESP=$(api DELETE "/api/lock/pipeline-run" /tmp/.tlinh-release-body.json)
     STATUS=$(printf '%s' "$RESP" | cut -f1)
     RETRY_AFTER=$(printf '%s' "$RESP" | cut -f2)
     case "$STATUS" in
       200) break ;;
       429) sleep "${RETRY_AFTER:-$((2 ** (attempt - 1)))}"; attempt=$((attempt + 1)); continue ;;
       *)   echo "[warn] R2 release returned $STATUS (lock TTL will reclaim)" >&2; break ;;
     esac
   done
   ```
2. Abort the run.
3. Do NOT continue subsequent steps.

**R3: 429 is transient. Retry policy differs by endpoint per PLAN-v2 §7.4:**
- **`/api/lock/*` endpoints** (acquire, heartbeat, release): retry the SAME
  request up to **3 times** with backoff 1s/2s/4s plus the server-provided
  `Retry-After`. After 3 attempts still 429: see ownership-loss handling
  per the specific step (acquire = abort silently; heartbeat = abort
  silently — rate-limit is NOT lock loss; release = log + give up, TTL
  will reclaim).
- **All other endpoints** (`/api/articles`, `/score`, `/notify`,
  `/brainstorm`, `/fail`, `/health`): retry **exactly once** with 1s
  delay (or `Retry-After` if larger). After the single retry, bubble the
  error up to the caller — second 429 triggers R2 (release + abort).
Lock-endpoint 429s in particular do NOT mean lock loss — they're rate-limit
throttling on the lock bucket. Do NOT abort the run on a 429 alone.

**R4: `<OWNER_ID>` is a placeholder.**
Wherever you see `<OWNER_ID>`, substitute the literal 32-hex UUID from the
acquire response in Step 1. You captured it from JSON; carry it forward in
your context for the rest of this run. Do not invent a new value.

**R5: Do not invent extra steps.**
Run exactly Steps 1–9 in order. Do NOT call `PATCH /brainstorm` — brainstorm
is on-demand only via the `/idea-brainstormer` slash skill.

**R6: Article cap.**
The server caps at `MAX_ARTICLES_PER_LIST` (default 50) via `ORDER BY` that
puts bootstrap-test rows first. Do not pass `limit=` — let the default enforce.

**R7: Verification rows.**
Articles whose `url` starts with `bootstrap-test://` are bootstrap verification
rows. They appear first in `list.py` results. Treat them as normal articles in
the notify step — they will produce a real Telegram message containing the
token, which the user uses to confirm setup works.

**R8: No secret leakage.**
Do NOT echo or print `$API_TOKEN` or any header containing it. Do NOT include
`$API_TOKEN` in error messages. The api() helper keeps the token in stdin via
`curl -K -`, never on argv.

---

## Failure modes covered

| Failure | Behavior |
|---|---|
| Step 1 acquire 409 | Abort silently, no release |
| Step 1 acquire 5xx after retries | Abort silently, no release |
| Step 4/8 returns 4xx (not 429) | R2: release + abort |
| Step 4/8 returns documented no-op `updated:false` / `sent:false` | Normal — continue |
| Step 5/6 heartbeat 409 | R1: abort WITHOUT release |
| `/api/lock/*` 429 | R3-lock: retry up to 3x with 1s/2s/4s + Retry-After |
| Non-lock endpoint 429 | R3-non-lock: retry exactly ONCE with max(1, Retry-After) delay |
| Cowork session crashes mid-run | Lock TTL expires (default 1800s); next scheduled run reclaims |
| Notify 502 telegram_permanent | R2: release + abort. Server already /fail'd internally (retry_count++) — Cowork stops the run so oncall can investigate before more articles attempt |
