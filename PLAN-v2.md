# PLAN v2 — VinFast News Pipeline (3-tier: Mac + VPS + Cowork)

## Why v2 exists

The v1 plan (PLAN.md) assumed Cowork bash ran on the host Mac, but real Cowork
runtime is a **Linux sandbox with mounted user folder that does not support
POSIX file locking**. Result: SQLite writes from Cowork sandbox fail with
`disk I/O error`. The architecture must move state off the mounted filesystem.

v2 chooses **3-tier separation**:

- **VPS (tlinh.duyet.vn)** = state layer. MariaDB + PHP backend. No sandbox limits.
- **Mac (local)** = crawl/extract layer. Runs launchd or manually; pushes data to VPS via HTTP.
- **Cowork (Claude Desktop)** = AI layer. Reads from VPS, scores/brainstorms, writes back via HTTP.

This isolates each component's failure mode and uses each platform for what it
actually does well.

---

## 1. Goal & Scope

Build a 3-tier news aggregation pipeline:

- **Crawl** VinFast-related news from Google News RSS + curated VN/EV sources (Mac local).
- **Extract** article content (trafilatura → Jina fallback) (Mac local).
- **Persist** state in MariaDB on a VPS, exposed via a small PHP HTTP API.
- **Score 1–5** via Claude in Cowork (scheduled daily task reads queue from VPS, scores, writes back).
- **Push** high-score articles to Telegram (sent by VPS backend on `/api/notify/{id}` trigger).
- **Brainstorm** 5 ideas per article via Cowork `/vf-brainstorm` slash skill, persisting result back to VPS.

**Out of scope (v2):**
- Multi-user, multi-topic isolation (v2 is single-user, single-topic VinFast)
- Web review UI
- Linux/Windows Mac client (macOS only for local crawler)
- VPS-side AI scoring (cost: would need OpenAI/Anthropic API key; instead reuse Max plan via Cowork)

**Runtime targets:**

- Mac client: macOS 13+, Python 3.11+ in `.venv`. Optional — only needed for crawl scheduling.
- VPS: Ubuntu 24.04 (existing 14.225.29.159), nginx, PHP 8.5+, MariaDB 10.11+, certbot timer (only for cert RENEWAL — issuance is NOT performed during deploy because the wildcard `*.duyet.vn` cert is pre-installed at `/etc/letsencrypt/live/duyet.vn/`).
- Cowork: Claude Desktop Pro/Max plan, Linux sandbox bash tool (just curl required).

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│ Mac (local — optional, only for crawl)                              │
│                                                                     │
│ launchd → crawl.py → POST tlinh.duyet.vn/api/articles               │
│ launchd → extract.py → PATCH tlinh.duyet.vn/api/articles/{id}/extract│
│                                                                     │
│ No local DB. Just Python venv + httpx + python-dotenv.              │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ HTTPS bearer auth
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ VPS (14.225.29.159, tlinh.duyet.vn)                                 │
│                                                                     │
│ nginx (vhost) → php-fpm 8.5 → /var/www/tlinh/tlinh.duyet.vn/        │
│                                  current/public/index.php           │
│                                                                     │
│ Bearer auth middleware → PDO MariaDB → tlinh_news DB                │
│   ├── articles table                                                │
│   └── locks table                                                   │
│                                                                     │
│ Endpoints:                                                          │
│   POST   /api/articles                  (Mac inserts skeleton)      │
│   PATCH  /api/articles/{id}/extract     (Mac sets content)          │
│   GET    /api/articles                  (Cowork lists queue)        │
│   GET    /api/articles/{id}             (Cowork reads single)       │
│   PATCH  /api/articles/{id}/score       (Cowork writes score, CAS)  │
│   PATCH  /api/articles/{id}/brainstorm  (Cowork writes ideas)       │
│   POST   /api/notify/{id}               (Cowork triggers Telegram)  │
│   POST   /api/lock/{name}/acquire       (Cowork mutex)              │
│   POST   /api/lock/{name}/heartbeat                                 │
│   DELETE /api/lock/{name}                                           │
│   GET    /api/health                    (liveness probe)            │
│                                                                     │
│ Telegram POST → api.telegram.org (from PHP, not Mac/Cowork)         │
└──────────────────────────▲──────────────────────────────────────────┘
                           │ HTTPS bearer auth
                           │
┌─────────────────────────────────────────────────────────────────────┐
│ Cowork (Claude Desktop, Pro/Max plan)                               │
│                                                                     │
│ Scheduled task daily:                                               │
│   curl GET  ../api/articles?stage=extracted&not_scored=1            │
│     → Claude reasons score per row via scoring-rubric.md            │
│   curl PATCH ../api/articles/{id}/score (per row)                   │
│   curl GET  ../api/articles?stage=scored&min_score=3&not_notified=1 │
│   curl POST ../api/notify/{id} (per row) → VPS sends Telegram       │
│                                                                     │
│ /vf-brainstorm 42 slash skill (on-demand chat):                 │
│   curl GET   ../api/articles/42                                     │
│   → Claude reads + WebSearch + MCP context                          │
│   → Claude generates 5-idea JSON via brainstorm-guidelines.md       │
│   curl PATCH ../api/articles/42/brainstorm -d '<json>'              │
└─────────────────────────────────────────────────────────────────────┘
```

### Why this works

| Aspect | v1 (Cowork-as-runtime) | v2 (3-tier) |
|---|---|---|
| State storage | SQLite on mounted folder → **FAIL** (no fcntl) | MariaDB on VPS → ✅ proper locking |
| 24/7 Telegram | Only when Mac awake + Desktop open | Always (VPS sends) |
| Mac dependency | Hard (Cowork is Mac-only) | Soft (only for crawl, which can move to VPS later) |
| Cowork role | Runtime + scheduler + AI | AI only (its actual sweet spot) |
| Failure isolation | Single point: Cowork sandbox | Each tier fails independently |
| AI cost | Free (Max plan) | Free (Max plan) — same |

---

## 3. Database schema (MariaDB)

```sql
CREATE DATABASE tlinh_news
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER 'tlinh'@'localhost' IDENTIFIED BY '<TLINH_DB_PASS>';
GRANT SELECT, INSERT, UPDATE, DELETE ON tlinh_news.* TO 'tlinh'@'localhost';
FLUSH PRIVILEGES;

USE tlinh_news;

CREATE TABLE articles (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  url             TEXT NOT NULL,
  url_hash        CHAR(32) NOT NULL,
  canonical_url   TEXT,
  title           TEXT,
  title_hash      CHAR(32),
  source          VARCHAR(255),
  content         MEDIUMTEXT,
  published_at    DATETIME NULL,
  crawled_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  extracted_at    DATETIME NULL,
  scored_at       DATETIME NULL,
  -- 2-phase notify (per Codex ISSUE-1): claim BEFORE external send, finalize AFTER.
  notify_claimed_at  DATETIME NULL,                -- set on Phase 1 atomic claim
  notify_claim_owner CHAR(32) NULL,                -- UUID generated server-side at claim
  notified_at     DATETIME NULL,                   -- set on Phase 2 after Telegram 200 OK
  brainstormed_at DATETIME NULL,
  failed_at       DATETIME NULL,
  score           TINYINT UNSIGNED NULL,           -- 1..5 (NULL = unscored)
  score_reason    TEXT,
  ideas           JSON,
  telegram_msg_id BIGINT NULL,
  last_error      TEXT,
  retry_count     INT UNSIGNED NOT NULL DEFAULT 0,
  final_state     VARCHAR(20) NULL,                -- 'discarded' | 'archived' | NULL
  UNIQUE KEY uq_url_hash (url_hash),
  KEY idx_extracted_at (extracted_at),
  KEY idx_scored_at (scored_at),
  KEY idx_notify_claimed_at (notify_claimed_at),
  KEY idx_notified_at (notified_at),
  KEY idx_brainstormed_at (brainstormed_at),
  KEY idx_title_hash (title_hash),
  KEY idx_score (score),
  KEY idx_final_state (final_state)
) ENGINE=InnoDB;

CREATE TABLE locks (
  name        VARCHAR(64) PRIMARY KEY,
  owner_id    CHAR(32) NOT NULL,
  acquired_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at  DATETIME NOT NULL,
  owner_pid   INT NULL,
  KEY idx_expires (expires_at)
) ENGINE=InnoDB;
```

### Field semantics (unchanged from v1)

- `url_hash`: MD5 of `canonical_url`. Used for INSERT idempotency via `INSERT IGNORE` on the UNIQUE key.
- `title_hash`: MD5 of normalized title (lowercased, no diacritics, no punctuation). Used for cross-source fuzzy dedup within a 48h window.
- Per-stage timestamps (`extracted_at`, `scored_at`, etc.): orthogonal facts. An article can be both `notified` and `brainstormed`.
- `retry_count`: incremented on `mark_failed`. When `retry_count >= MAX_RETRIES`, row gets `final_state='discarded'`.
- `score`: 1–5. NULL means not yet scored.
- `ideas`: JSON array of exactly 5 objects with `{title, angle, format, difficulty, viral_potential}`.

### Reserved URL scheme

- `bootstrap-test://<HEX_TOKEN>` is RESERVED for verification rows. Crawl-side MUST reject any incoming URL matching this prefix. Verification rows pre-scored with `score=5` to skip the AI step during E2E.

---

## 4. HTTP API specification

**Base URL:** `https://tlinh.duyet.vn/api`
**Auth:** Bearer token in `Authorization` header. Single shared token (env-stored).
**Format:** JSON request body, JSON response. UTF-8.

### Auth

```
Authorization: Bearer <API_TOKEN>
Content-Type: application/json
```

Missing/invalid token → `401 Unauthorized` with body `{"error": "unauthorized"}`.

### Endpoints

#### POST /api/articles (Mac → VPS insert)

Insert a new article candidate. Idempotent via `url_hash` UNIQUE.

Request:
```json
{
  "url": "https://news.google.com/rss/articles/CBM...",
  "canonical_url": "https://vnexpress.net/...",
  "title": "VinFast Q1 2026...",
  "source": "VnExpress",
  "published_at": "2026-05-12T08:00:00"
}
```

Response 201 (new) or 200 (existing):
```json
{"id": 42, "created": true, "url_hash": "abc...", "title_hash": "def..."}
```

The server computes `url_hash` and `title_hash` server-side (single source of truth).

#### PATCH /api/articles/{id}/extract (Mac → VPS extract update)

Set extracted content. CAS via `extracted_at IS NULL`.

Request:
```json
{
  "content": "Full article text...",
  "title": "Updated title from extraction",
  "source": "VnExpress"
}
```

Response 200:
```json
{"id": 42, "updated": true, "extracted_at": "2026-05-12T08:05:30"}
```

If already extracted, returns `{"updated": false, "reason": "already_extracted"}` (HTTP 200, idempotent).

#### GET /api/articles

Query articles with filters.

Query params:
- `stage`: `new` | `extracted` | `scored` | `notified` | `brainstormed` | `failed`
- `min_score`, `max_score`, `score`: int
- `not_scored`, `not_notified`, `not_brainstormed`: bool (`1`/`0`)
- `final_state`: `discarded` | `archived` | `active` (per Codex ISSUE-21: `active` is a server-side alias for `final_state IS NULL` — i.e. the row is still in the pipeline, not yet terminal. The DB column itself only stores `'discarded'`, `'archived'`, or `NULL`.)
- `since`: ISO date (default: 7 days ago)
- `last_hours`: int
- `search`: keyword (LIKE on title + content)
- `limit`: int (default 50, max 200)

**`stage` predicate definitions (per Codex ISSUE-13) — server-enforced, single source of truth:**

| stage | category | SQL predicate |
|---|---|---|
| `new` | queue | `extracted_at IS NULL AND final_state IS NULL AND retry_count < MAX_RETRIES` |
| `extracted` | queue | `extracted_at IS NOT NULL AND scored_at IS NULL AND final_state IS NULL AND retry_count < MAX_RETRIES` |
| `scored` | queue | `scored_at IS NOT NULL AND notified_at IS NULL AND final_state IS NULL AND retry_count < MAX_RETRIES` |
| `failed` | queue | `failed_at IS NOT NULL AND final_state IS NULL` (transient failures; discarded rows are NOT in this stage) |
| `notified` | terminal | `notified_at IS NOT NULL` |
| `brainstormed` | terminal | `brainstormed_at IS NOT NULL` |

`MAX_RETRIES` is read from `.env` server-side.

**Discard exclusion semantics (per Codex ISSUE-19):**
- **Queue stages** (`new`, `extracted`, `scored`, `failed`) explicitly exclude discarded rows via `final_state IS NULL`. Once a row is discarded, it is removed from every queue stage and cannot be picked up by extract/score/notify clients.
- **Terminal stages** (`notified`, `brainstormed`) intentionally do NOT filter by `final_state`. These stages report observed completed actions: a row that reached `notified_at NOT NULL` succeeded the notify side effect — that historical fact does not get unset if the row is later marked discarded (which would only happen via a separate manual path, since Phase 2 only discards on transient failure before notify succeeds). In practice the union `notified ∩ discarded` is empty by the pipeline's monotonic state progression.

To inspect discarded rows, callers explicitly pass `final_state=discarded`; this filter is orthogonal to the stage filter.

Response 200:
```json
{
  "rows": [
    {
      "id": 42, "url": "...", "canonical_url": "...", "title": "...",
      "source": "VnExpress", "score": 5, "score_reason": "...",
      "scored_at": "2026-05-12T08:10:00", "notified_at": null, ...
    }
  ],
  "count": 1
}
```

Rows ordered by: `bootstrap-test://%` URL scheme first (priority), then `scored_at DESC`, then `crawled_at DESC`.

#### GET /api/articles/{id}

Single article by id. 404 if not found.

#### PATCH /api/articles/{id}/score (Cowork → VPS write score)

CAS: only updates if `scored_at IS NULL AND final_state IS NULL`.

Request:
```json
{"score": 4, "reason": "Số liệu Q1 cụ thể, góc phân tích market share VinFast..."}
```

Validation: `1 <= score <= 5`, reason length ≤ 500.

Response 200 (updated):
```json
{"id": 42, "updated": true, "scored_at": "2026-05-12T08:15:00"}
```

Response 200 (no-op, already scored):
```json
{"id": 42, "updated": false, "reason": "already_scored"}
```

#### PATCH /api/articles/{id}/brainstorm (Cowork → VPS write ideas)

Overwrite-friendly (no CAS) — brainstorm can re-run.

Request:
```json
{
  "ideas": [
    {"title": "...", "angle": "...", "format": "Bài phân tích", "difficulty": "Trung bình", "viral_potential": 4},
    ...5 items
  ]
}
```

Validation: exactly 5 items, each with all 5 fields, `viral_potential` 1-5.

Response 200:
```json
{"id": 42, "updated": true, "brainstormed_at": "2026-05-12T09:00:00"}
```

#### POST /api/notify/{id} (Cowork → VPS trigger Telegram send) — 2-phase claim/send

**Race-safety design (per Codex ISSUE-1):** Telegram send is a side effect that must run AT MOST ONCE per article. Pure CAS on `notified_at` cannot guarantee this if two callers POST concurrently — both pass the `notified_at IS NULL` check, both send, only one wins the UPDATE. **Fix: 2-phase atomic claim before external call:**

**Phase 1 — Claim (transactional UPDATE, no external I/O, with TTL-based reclaim retry — per Codex ISSUE-14):**

The flow has TWO sub-steps. Stale-claim reclaim is integrated, not a side note.

*Phase 1a — Fresh claim attempt:*
```sql
UPDATE articles
   SET notify_claimed_at = NOW(),
       notify_claim_owner = ?  -- server-generated UUID for this request
 WHERE id = ?
   AND notified_at IS NULL
   AND notify_claimed_at IS NULL
   AND score >= ?
   AND final_state IS NULL
```
- `rowcount == 1` → exclusive owner of the send. Continue to Phase 2.
- `rowcount == 0` → either another caller already claimed, or the row is ineligible (threshold/score/state). Proceed to Phase 1b to disambiguate.

*Phase 1b — Disambiguate (single SELECT, no race needed because Phase 2 is the only writer to these fields):*
```sql
SELECT notified_at, notify_claimed_at, score, final_state
  FROM articles
 WHERE id = ?
```
Branch on the result (per Codex ISSUE-24 — `final_state` values are distinguished):
- `notified_at IS NOT NULL` → return 200 `{"sent": false, "reason": "already_notified"}`. Done.
- `final_state = 'discarded'` → return 200 `{"sent": false, "reason": "discarded"}`. Done.
- `final_state = 'archived'` → return 200 `{"sent": false, "reason": "archived"}`. Done.
- `score IS NULL` → return 200 `{"sent": false, "reason": "no_score"}`. Done.
- `score < MIN_SCORE_TO_NOTIFY` → return 200 `{"sent": false, "reason": "below_threshold"}`. Done.
- `notify_claimed_at IS NOT NULL AND notify_claimed_at >= NOW() - INTERVAL NOTIFY_CLAIM_TTL_SECONDS SECOND` → live owner is heartbeating. Return 200 `{"sent": false, "reason": "already_claimed"}`. Done.
- `notify_claimed_at IS NOT NULL AND notify_claimed_at < NOW() - INTERVAL NOTIFY_CLAIM_TTL_SECONDS SECOND` → **stale claim**. Proceed to Phase 1c (one reclaim attempt).

*Phase 1c — Stale-claim reclaim (atomic, CAS on claim age):*
```sql
UPDATE articles
   SET notify_claimed_at = NOW(),
       notify_claim_owner = ?  -- NEW owner UUID
 WHERE id = ?
   AND notified_at IS NULL
   AND notify_claimed_at < NOW() - INTERVAL ? SECOND  -- NOTIFY_CLAIM_TTL_SECONDS
   AND score >= ?
   AND final_state IS NULL
```
- `rowcount == 1` → reclaim succeeded; we now own the claim. Continue to Phase 2.
- `rowcount == 0` → another caller raced us through reclaim. Return 200 `{"sent": false, "reason": "already_claimed", "stale_lost_race": true}`. Done.

**Reclaim is attempted AT MOST ONCE per request.** We do not loop Phase 1a→1b→1c repeatedly — that would risk a livelock if two dead owners race two fresh callers. AC `#5b-dead` verifies the single-attempt path.

**Phase 2 — Send + finalize (after Phase 1 returns rowcount=1):**
1. Build HTML-escaped message from article fields.
2. **Start claim-heartbeat task** (background thread / async loop): every
   `NOTIFY_CLAIM_HEARTBEAT_SECONDS` (default 20s) issue:
   ```sql
   UPDATE articles
      SET notify_claimed_at = NOW()
    WHERE id = ?
      AND notify_claim_owner = ?
      AND notified_at IS NULL
   ```
   If `rowcount == 0` (someone else stole the claim, or `notified_at` got
   set), the heartbeat task signals the main thread to ABORT the send
   immediately. (See §9 for Telegram retry interplay.)
3. POST `api.telegram.org/bot<TOKEN>/sendMessage` (with 429 Retry-After + 5xx backoff per §9). Each retry attempt MUST first check that the claim is still held; if the heartbeat task signaled abort, do NOT continue retrying.
4. On Telegram 200 OK (finalize CAS-bound to our claim — per Codex ISSUE-23, this UPDATE also clears any prior transient-failure markers):
   ```sql
   UPDATE articles
      SET notified_at = NOW(),
          telegram_msg_id = ?,
          notify_claimed_at = NULL,
          notify_claim_owner = NULL,
          failed_at = NULL,
          last_error = NULL
    WHERE id = ?
      AND notify_claim_owner = ?  -- our claim
      AND notified_at IS NULL
   ```
   Note: `retry_count` is intentionally NOT reset — it stays for observability.
   If `rowcount == 0` (claim was stolen mid-send), log
   `notify.claim_stolen_post_send` with `telegram_msg_id` so user can manually
   reconcile, and return 200 `{"sent": true, "warning": "claim_stolen_after_send"}`.
   (See "Claim TTL sizing" below for why this is bounded to be extremely rare.)
5. On Telegram permanent failure (4xx other than 429): release the claim so retry can succeed:
   ```sql
   UPDATE articles SET notify_claimed_at = NULL, notify_claim_owner = NULL
    WHERE id = ? AND notify_claim_owner = ?
   ```
   Then call `/api/articles/{id}/fail` with `stage='notify'` to bump retry_count.
6. Stop heartbeat task (in `finally`).

**Claim TTL sizing (per Codex ISSUE-8):** the TTL must bound the worst-case
Phase-2 send window, NOT just be a magic 60s constant. We compute:

- `NOTIFY_TELEGRAM_MAX_BACKOFF_SECONDS` = 1+2+4+8+16 = 31s (per §9 5xx schedule).
- `NOTIFY_TELEGRAM_RETRY_AFTER_CAP_SECONDS` = 60s (server-side cap on honoring
  Telegram's `Retry-After`; if Telegram asks for more we ABORT and return 502).
- `NOTIFY_TELEGRAM_REQUEST_TIMEOUT_SECONDS` = 30s (per request).
- `NOTIFY_PHASE2_BUDGET_SECONDS` = backoff + retry-after cap + 5 × request
  timeout = 31 + 60 + 150 = **241s** worst case.
- `NOTIFY_CLAIM_HEARTBEAT_SECONDS` = 20s (≤ 1/3 of TTL).
- `NOTIFY_CLAIM_TTL_SECONDS` = max(`NOTIFY_PHASE2_BUDGET_SECONDS` + 60s safety,
  `3 × NOTIFY_CLAIM_HEARTBEAT_SECONDS`) = **300s** (5 minutes).

These are configurable via `.env` but their relative ordering MUST hold:
`heartbeat × 3 ≤ TTL` AND `phase2_budget + 60 ≤ TTL`. The server validates
this on boot and refuses to start if violated.

**Stale-claim recovery semantics (per Codex ISSUE-8 + ISSUE-14):** the
reclaim happens in Phase 1c (above). Because the live owner heartbeats every
20s, a claim older than `NOTIFY_CLAIM_TTL_SECONDS` (300s default) implies ≥
280s of heartbeat silence — which can only happen if the owning process died.
The Phase 1c UPDATE atomically transfers ownership and proceeds to Phase 2;
no separate "release then re-claim" path is needed.

If Telegram's `Retry-After` exceeds `NOTIFY_TELEGRAM_RETRY_AFTER_CAP_SECONDS`,
the server logs `notify.retry_after_exceeded_cap` and returns 502 (release
claim via Phase 2 step 5, call /fail). This prevents an attacker / Telegram
outage from holding a claim indefinitely.

Request body: (none required)

Response 200 (sent):
```json
{"id": 42, "sent": true, "telegram_msg_id": 123, "notified_at": "2026-05-12T08:20:00"}
```

Response 200 (no-op — already notified, claim lost, or ineligible). The `reason` enum (per Codex ISSUE-22) is:

| `reason` | When |
|---|---|
| `already_notified` | `notified_at IS NOT NULL` at Phase 1b SELECT. |
| `discarded` | `final_state = 'discarded'` at Phase 1b SELECT (per Codex ISSUE-24 — retry-exhaustion terminal state). |
| `archived` | `final_state = 'archived'` at Phase 1b SELECT (per Codex ISSUE-24 — operator-driven terminal state, distinct from `discarded`). |
| `no_score` | `score IS NULL` at Phase 1b SELECT. |
| `below_threshold` | `score < MIN_SCORE_TO_NOTIFY` at Phase 1b SELECT. |
| `already_claimed` | Phase 1b SELECT found a live (non-stale) claim held by another owner. |
| `already_claimed` + `stale_lost_race: true` | Phase 1c reclaim raced and lost to a concurrent caller. Same `reason` string but with the extra boolean for observability. |

```json
{"id": 42, "sent": false, "reason": "already_claimed", "stale_lost_race": true}
```

Other no-op cases omit `stale_lost_race`:
```json
{"id": 42, "sent": false, "reason": "already_notified"}
```

Response 200 (sent, but claim was stolen mid-finalize — rare, see Phase 2 step 4):
```json
{"id": 42, "sent": true, "telegram_msg_id": 123, "warning": "claim_stolen_after_send"}
```

Response 502 (Telegram permanent error, claim released, retry_count bumped):
```json
{"error": "telegram_permanent", "status": 400, "detail": "Bad Request: ..."}
```

#### POST /api/articles/{id}/fail (per Codex ISSUE-2)

Record a failure for a specific pipeline stage. Bumps `retry_count`. If
`retry_count + 1 >= MAX_RETRIES`, sets `final_state = 'discarded'`.

Callers:
- Mac `extract.py` after exhausting in-process retries on trafilatura + Jina.
- Cowork on Telegram permanent error (after Phase 2 of notify fails).
- Cowork on score validation failure (e.g. JSON parse error).

Request:
```json
{
  "stage": "extract|score|notify",
  "error_code": "extract_no_content|jina_5xx|telegram_400|json_validation",
  "message": "Detailed reason (≤500 chars)"
}
```

Response 200:
```json
{
  "id": 42,
  "retry_count": 2,
  "max_retries": 3,
  "discarded": false,
  "failed_at": "2026-05-12T08:00:00"
}
```

Response 200 (discarded):
```json
{
  "id": 42,
  "retry_count": 3,
  "max_retries": 3,
  "discarded": true,
  "final_state": "discarded"
}
```

Server SQL (atomic):
```sql
UPDATE articles
   SET failed_at   = NOW(),
       last_error  = CONCAT(?, ': ', ?),         -- "extract: jina 5xx"
       retry_count = retry_count + 1,
       final_state = CASE
                       WHEN retry_count + 1 >= ? THEN 'discarded'
                       ELSE final_state
                     END
 WHERE id = ? AND final_state IS NULL
```

Idempotent: calling `/fail` on a row that has already reached `final_state='discarded'`
returns 200 with `discarded: true` and does NOT increment further.

Success-path clearing (Codex ISSUE-2 + ISSUE-22 + ISSUE-23): every successful
stage transition clears `failed_at = NULL` and `last_error = NULL` in the same
SQL UPDATE that records the success. This is implemented at:

- `PATCH /api/articles/{id}/extract`: the UPDATE that sets `extracted_at` and
  `content` MUST also set `failed_at = NULL, last_error = NULL`.
- `PATCH /api/articles/{id}/score`: the UPDATE that sets `scored_at`,
  `score`, and `score_reason` MUST also set `failed_at = NULL, last_error = NULL`.
- `POST /api/notify/{id}` Phase 2 step 4 finalize UPDATE: explicitly includes
  `failed_at = NULL, last_error = NULL` (see SQL block above).

There is no separate `POST /notify-finalize` endpoint; the finalize happens
internally inside `POST /api/notify/{id}` Phase 2. `retry_count` is kept for
observability across all three paths.


#### POST /api/admin/inject-test (per Codex ISSUE-3)

Insert a uniquely-tokenized verification row for end-to-end scheduler testing.
**Bypasses the URL-scheme reservation that blocks `POST /api/articles`.**

Auth: bearer token same as other endpoints, BUT additionally requires the
caller to be on the localhost loopback (nginx `allow 127.0.0.1; deny all`).
This prevents external bearer-holders from spamming the reserved scheme.
Internal services (Mac launchd, Cowork) trigger via SSH tunnel or VPS-local cron.

Request:
```json
{"token": "abc123def456"}
```

Validation:
- `token` matches regex `^[0-9a-fA-F]{4,64}$` (hex, secrets.token_hex compatible).
- 400 if invalid token.

Behavior:
- Inserts ONE row with:
  - `url` = `bootstrap-test://<token>`
  - `canonical_url` = same
  - `url_hash` = MD5(canonical_url)
  - `title` = `[BOOTSTRAP TEST <token>] sample article`
  - `title_hash` = MD5(normalize(title))
  - `source` = `bootstrap-inject`
  - `content` = `Test content for verification — VinFast Q1 sample`
  - `crawled_at` = NOW()
  - `extracted_at` = NOW()
  - `scored_at` = NOW()
  - `score` = 5
  - `score_reason` = `bootstrap verification`
  - `notified_at` = NULL  (so the notify pipeline still has work to do)
- Idempotent per token: if a row with same `url_hash` exists, return the existing row's id.

Response 201 (new) or 200 (existing):
```json
{"id": 99, "created": true, "url": "bootstrap-test://abc123def456", "score": 5}
```

Implementation task: see §11 Task 3a.


#### POST /api/lock/{name}/acquire

Insert a row in `locks` table with TTL. PK conflict → 409.

Request:
```json
{"ttl_seconds": 1800}
```

Response 201:
```json
{"name": "pipeline-run", "owner_id": "ab12cd...", "expires_at": "..."}
```

Response 409 (held):
```json
{"error": "lock_held", "owner_pid": null, "acquired_at": "...", "expires_at": "..."}
```

Also returns 201 if existing row's `expires_at < NOW()` (TTL cleanup before INSERT).

#### POST /api/lock/{name}/heartbeat

Extend `expires_at`.

Request:
```json
{"owner_id": "ab12cd...", "ttl_seconds": 1800}
```

Response 200:
```json
{"name": "pipeline-run", "expires_at": "..."}
```

Response 409 (lost ownership):
```json
{"error": "lost_ownership"}
```

#### DELETE /api/lock/{name}

Request body:
```json
{"owner_id": "ab12cd..."}
```

Response 200:
```json
{"deleted": true}
```

200 with `deleted: false` if owner mismatch (idempotent — caller can always call release).

#### GET /api/health

No auth required (liveness probe).

Response 200:
```json
{"status": "ok", "db": "connected", "version": "v2-2026-05-12"}
```

### Error response shape

All errors return JSON:
```json
{"error": "code_in_snake_case", "message": "human-readable detail"}
```

HTTP status codes used: 200, 201, 400, 401, 403, 404, 409, 500.

---

## 5. Component breakdown

### 5.1 VPS backend (PHP 8.5 + MariaDB)

**Folder layout:** following existing `dth.duyet.vn` pattern.

```
/var/www/tlinh/tlinh.duyet.vn/
├── current → releases/v1                  (symlink, atomic-swap-friendly)
├── releases/
│   └── v1/
│       ├── public/
│       │   └── index.php                  (router entry point)
│       └── src/
│           ├── bootstrap.php              (env load, init Db + Auth)
│           ├── Db.php                     (PDO wrapper)
│           ├── Auth.php                   (bearer token middleware)
│           ├── Router.php                 (URL → handler dispatch)
│           ├── Telegram.php               (send message helper)
│           ├── Hashing.php                (url_hash, title_hash, normalize_title)
│           └── routes/
│               ├── articles.php           (POST, GET list, GET id, PATCH extract)
│               ├── score.php              (PATCH /score)
│               ├── brainstorm.php         (PATCH /brainstorm)
│               ├── notify.php             (POST /notify)
│               └── lock.php               (acquire / heartbeat / release)
└── shared/
    └── .env                               (DB creds, API_TOKEN, TG_TOKEN, TG_CHAT_ID)
```

**`.env` (on VPS, shared dir):**
```env
DB_HOST=127.0.0.1
DB_NAME=tlinh_news
DB_USER=tlinh
DB_PASS=<random>

API_TOKEN=<32-char-random-bearer>

TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=-1001234567890

TITLE_DEDUP_WINDOW_HOURS=48
MAX_RETRIES=3
LOCK_TTL_SECONDS=1800
MIN_SCORE_TO_NOTIFY=3
MAX_ARTICLES_PER_LIST=50
```

**No framework.** PHP 8.5 native + PDO. No composer dependencies for v1 (keeps deploy simple). Optional: Slim 4 in v1.1 if routing complexity grows.

**Strict types:** `declare(strict_types=1);` in every PHP file. PHPStan-compatible (no runtime mode change required for v1).

**Error handling:** All exceptions caught in `index.php`, returned as JSON error. Log to `error_log()` (nginx error.log).

### 5.2 Mac client (Python, slim)

**Folder layout:** (current repo, simplified)

```
vin-automate-main/
├── .venv/                  (venv from install.sh)
├── .env                    (API_TOKEN, API_BASE_URL — no DB creds)
├── crawl.py                (HTTPS POST, no local DB)
├── extract.py              (HTTPS PATCH, no local DB)
├── api_client.py           (httpx wrapper with bearer auth + retry)
├── config.py               (TOPICS, API_BASE_URL, API_TOKEN)
├── install.sh              (venv + pip install only — no schema init)
├── requirements.txt        (httpx, feedparser, trafilatura, python-dotenv)
└── com.tlinh.crawl.plist   (launchd daily scheduler)
```

**Deleted from v1:**
- `db.py`, `lock.py`, `mark.py`, `list.py`, `notify.py`, `setup_helper.py`, `_logging.py` (replaced by VPS)
- `BOOTSTRAP.md`, `SKILLS/setup.skill`, `cowork-task-prompt.md` (v1-style; replaced by simpler v2 versions)
- `news.db`, `crawler-validation.md`, `E2E-VALIDATION.md` v1 versions (rewritten)

**`crawl.py` behavior:**
- Fetches Google News + extra RSS.
- Resolves canonical URL (4-tier cascade).
- For each candidate:
  - Compute `url_hash` locally (must match VPS hash for idempotency).
  - POST `/api/articles` with payload.
  - 200 = existing row (skip), 201 = new.
- No local state. No locks needed Mac-side (Mac is single writer for crawl; VPS endpoint is idempotent via `url_hash` UNIQUE).

**`extract.py` behavior:**
- GET `/api/articles?stage=new` (server-defined predicate: `extracted_at IS NULL AND final_state IS NULL AND retry_count < MAX_RETRIES` — see §4 GET /api/articles).
- For each row: fetch content via trafilatura/Jina.
- PATCH `/api/articles/{id}/extract` with content.
- On exhausted retries (transient failures): POST `/api/articles/{id}/fail` with `stage=extract`. Server increments `retry_count` and sets `final_state='discarded'` once `retry_count >= MAX_RETRIES`. Discarded rows never re-appear in `stage=new`.

**`api_client.py` behavior:**
- `post_article(payload) -> dict`
- `patch_extract(id, payload) -> dict`
- `get_articles(filters) -> list[dict]`
- Bearer token from env.
- Retry with exponential backoff on `429` / `5xx`.
- Timeout: 30s default.
- Logs request/response status to `logs/api-client.log` (rotating).

### 5.3 Cowork (skills + scheduled task)

**Folder layout:**

```
vin-automate-main/
├── SKILLS/
│   ├── vf-brainstorm.skill                (rewritten: HTTP-based)
│   └── (NO setup.skill — not needed in v2)
├── cowork-task-prompt.md                 (rewritten: HTTP-based scoring loop)
├── scoring-rubric.md                     (unchanged from v1)
└── brainstorm-guidelines.md              (unchanged from v1)
```

**`SKILLS/vf-brainstorm.skill` content:**
- Invocation: `/vf-brainstorm <id|filter>`
- Steps:
  1. Resolve user input → API filter.
  2. `curl -H "Authorization: Bearer $TOKEN" "https://tlinh.duyet.vn/api/articles?<filter>"` → parse JSON.
  3. If multiple → show table, ask user to pick id.
  4. `curl ... /api/articles/<id>` → full article content.
  5. Read `brainstorm-guidelines.md` for format spec.
  6. Use WebSearch + MCP for additional research context (Cowork built-in).
  7. Generate 5-idea JSON.
  8. `curl -X PATCH ... /api/articles/<id>/brainstorm -d '<json>'`.
  9. Display ideas table to user.

**`cowork-task-prompt.md` content (for `/schedule` daily task):**
- Read `scoring-rubric.md`.
- `curl ... /api/lock/pipeline-run/acquire -d '{"ttl_seconds": 1800}'` → capture `owner_id`.
- If 409 → abort silently.
- `curl ... /api/articles?stage=extracted&not_scored=1` → list.
- For each row:
  - Apply rubric → assign score + reason.
  - `curl -X PATCH ... /api/articles/<id>/score -d '<json>'`.
  - Every N rows: `curl ... /heartbeat`.
- `curl ... /api/articles?stage=scored&min_score=3&not_notified=1`.
- For each row: `curl -X POST ... /api/notify/<id>`.
- `curl -X DELETE ... /api/lock/pipeline-run -d '{"owner_id": "..."}'`.
- Error handling: any non-2xx → release lock + abort (R2). Heartbeat 409 → abort without release (R1).

**Auth token in Cowork (MANDATED — per Codex ISSUE-4):**

Single mechanism only. **No hardcoded tokens in skill files or task prompts.**

1. Token lives in `SKILLS/.env` in the project folder (Cowork's granted folder). Format:
   ```
   API_TOKEN=<32-char-hex>
   API_BASE_URL=https://tlinh.duyet.vn
   ```
2. The file is gitignored (`.gitignore` must include `SKILLS/.env`).
3. **First line of every Cowork bash invocation that uses the API**:
   ```bash
   set -a && . /path/to/project/SKILLS/.env && set +a || {
     echo "[error] SKILLS/.env missing or unreadable" >&2
     exit 1
   }
   ```
   - `set -a` exports all variables sourced in the next command.
   - Failure to source = hard exit; do NOT fall back to interactive prompt or hardcoded default.
4. This applies to BOTH:
   - The scheduled task prompt (paste into Cowork's `/schedule` UI).
   - The `/vf-brainstorm` slash skill body.
5. The path `/path/to/project/SKILLS/.env` is the absolute path to the granted folder. The deploy runbook (§10) documents how to locate it for the user's specific Cowork project (typically `/Users/.../vin-automate-main/SKILLS/.env`).

Hardcoded tokens, environment frontmatter, or interactive prompts are FORBIDDEN — they reintroduce v1-class secret leakage risk.

### 5.4 DNS + HTTPS deployment (Cloudflare + existing wildcard cert — per user 2026-05-12)

**DNS setup (automated via CF API):**
1. Create A record `tlinh.duyet.vn → 14.225.29.159`, `proxied=false` initially.
2. Cloudflare proxy can be enabled later (independently of cert state).
3. CF API endpoints:
   - `POST /zones/{zone_id}/dns_records` to create
   - `PATCH /zones/{zone_id}/dns_records/{id}` to toggle proxied

**nginx vhost (`/etc/nginx/sites-enabled/tlinh.duyet.vn`):**
- Listen 80 + 443.
- `root /var/www/tlinh/tlinh.duyet.vn/current/public;`
- `try_files $uri /index.php?$query_string;` for catch-all routing.
- `location ~ \.php$ { fastcgi_pass unix:/run/php/php8.5-fpm.sock; ... }`
- Security headers: `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`.
- `client_max_body_size 5M` (enough for article content).

**HTTPS (NO certbot run — wildcard cert already issued):**
- VPS already has wildcard cert at `/etc/letsencrypt/live/duyet.vn/`
  (verified 2026-05-12 — SAN `DNS:*.duyet.vn, DNS:duyet.vn`, valid until 2026-06-21).
- nginx vhost references `ssl_certificate /etc/letsencrypt/live/duyet.vn/fullchain.pem;`
  and `ssl_certificate_key /etc/letsencrypt/live/duyet.vn/privkey.pem;` directly.
- Cert renewal is handled by an existing system-wide certbot renewal hook for
  the `duyet.vn` cert — `tlinh.duyet.vn` rides on that. No per-host renewal
  configuration needed for v2.
- Step 6 of the deploy runbook MUST verify the cert exists and is valid (not
  expired) before reloading nginx; abort the deploy if the cert is missing.

### 5.5 launchd (Mac)

**`com.tlinh.crawl.plist`** schedules daily crawl + extract:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.tlinh.crawl</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/theduyet/Documents/Code/vin-automate/.venv/bin/python</string>
    <string>/Users/theduyet/Documents/Code/vin-automate/main.py</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/Users/theduyet/Documents/Code/vin-automate/logs/crawl.out</string>
  <key>StandardErrorPath</key><string>/Users/theduyet/Documents/Code/vin-automate/logs/crawl.err</string>
  <key>RunAtLoad</key><false/>
</dict></plist>
```

`main.py` is a thin orchestrator that calls `crawl.py` then `extract.py` in order.

Daily schedule at 7am local. Mac asleep → cron misses run; macOS does NOT catch up (launchd's `StartCalendarInterval` is strict). Acceptable for daily-cadence pipeline.

If user wants finer granularity (e.g. every 6h), use `StartInterval` (seconds) instead of `StartCalendarInterval`.

---

## 6. Concurrency model

### 6.1 Within VPS (multiple HTTP requests in flight)

- MariaDB InnoDB transactions + `UPDATE WHERE col IS NULL` compare-and-set pattern.
- Each CAS update returns rowcount; 0 = no-op (idempotent caller).
- No file locks needed. MariaDB handles serialization at the row level.

### 6.2 Within Cowork session (single scheduled task)

- DB-level mutex via `locks` table:
  - `acquire`: `INSERT INTO locks (...) VALUES (..., NOW() + INTERVAL ttl SECOND)` with PK conflict → 409.
  - Before INSERT, opportunistic `DELETE FROM locks WHERE name = ? AND expires_at < NOW()` to reclaim expired.
- `heartbeat`: `UPDATE locks SET expires_at = NOW() + INTERVAL ttl SECOND WHERE name = ? AND owner_id = ?`. Rowcount 0 → lost ownership.
- `release`: `DELETE WHERE name = ? AND owner_id = ?`. Idempotent.

### 6.3 Mac crawl + Cowork score race

- Mac inserts rows via POST `/api/articles`. VPS idempotent via `url_hash` UNIQUE.
- Cowork reads `stage=extracted&not_scored=1` and scores. CAS on `scored_at IS NULL` ensures double-scoring is no-op.
- Mac extracts (PATCH). CAS on `extracted_at IS NULL` ensures double-extract is no-op.
- All races covered by per-column CAS at the API layer.

### 6.4 Verification rows (bootstrap-test://)

- Same as v1: `--inject-test <HEX>` inserts a pre-scored row with reserved URL scheme.
- Reserved scheme is enforced at the `POST /api/articles` endpoint: server-side reject any incoming URL matching `^bootstrap-test://` unless from a specific admin endpoint `/api/admin/inject-test`.
- `GET /api/articles` ORDER BY priority puts `bootstrap-test://` rows first so verification doesn't starve.

---

## 7. Auth + secrets

### 7.1 Bearer token

- Single shared token: `API_TOKEN` (32-char random hex).
- Generated once during deploy: `openssl rand -hex 32`.
- Stored in **exactly three places**, each gitignored, each `chmod 600`:
  - VPS: `/var/www/tlinh/tlinh.duyet.vn/shared/.env`
  - Mac: `vin-automate/.env`
  - Cowork-side: `vin-automate/SKILLS/.env` (separate file from Mac `.env` to
    keep blast radius small if one is leaked; bootstrap copies the token
    into both during deploy)
- Cowork skills and the scheduled task prompt MUST source `SKILLS/.env` as the
  ONLY auth-loading path (see §5.3 mandate). Hardcoded tokens are forbidden.
- Server-side: nginx forwards the `Authorization` header to PHP; backend
  compares against `API_TOKEN` from VPS `.env` via constant-time comparison
  (`hash_equals` in PHP) to avoid timing attacks.

### 7.2 Token rotation

- Manual: regenerate, update both VPS and Mac/Cowork .env, restart php-fpm.
- For v1 we accept manual rotation. Automated rotation deferred to v2.1.

### 7.3 CORS

- Mac and Cowork sandbox both hit API server-side (not browser-side). No CORS preflight expected, but include permissive `Access-Control-Allow-*` on nginx anyway for future browser-based admin UI.

### 7.4 Rate limiting (per Codex ISSUE-7 + ISSUE-15 + ISSUE-16 — Step 6 vhost is authoritative)

Two leaky-bucket zones in nginx + one global hourly average zone. `/api/health`
is **not** in any zone — health checks never 429.

| Zone | Endpoints | Average rate | Burst | Key |
|---|---|---|---|---|
| (none) | `/api/health` | unlimited | n/a | n/a |
| `tlinh_lock` | `/api/lock/*` (acquire, heartbeat, release) | 600 req/min | 100 | `$http_authorization` |
| `tlinh_main` | everything else under `/api/` (including `/api/admin/inject-test`) | 100 req/min | 20 | `$http_authorization` |
| `tlinh_hourly` | every authed `/api/` endpoint, in addition to the per-zone limit above | **average** 1000 req/h | 50 | `$http_authorization` |

**Rationale:**
- Lock heartbeats run on every loop iteration; a 100-row score batch could
  easily emit 10+ heartbeats. Sharing the main bucket would cause spurious
  429s on heartbeats, leading to lost lock ownership and aborted runs.
- `/api/health` is for monitoring/oncall. It must NEVER 429. Step 6's vhost
  enforces this by giving `/api/health` its own `location = /api/health`
  block with no `limit_req` directive at all (verified by `nginx -T | grep
  -A2 "= /api/health"`).
- `tlinh_hourly` is a **leaky-bucket average rate limit**, NOT a fixed-window
  hard cap (per Codex ISSUE-16). It admits up to `burst=50` over the average
  rate, then queues/rejects. Sustained traffic above 1000 req/h sees 429s.
  Short spikes inside the burst window do not 429. This is what nginx's
  `limit_req` actually does — we do not advertise an exact 1000-count cap
  because the implementation cannot guarantee one.

**Caller behavior on 429:**
- `/api/lock/*` callers: retry with backoff (1s, 2s, 4s). Do NOT treat 429 as
  lock loss — it is transient. Max 3 retries before giving up + abort run.
- Other endpoints: retry once with 1s delay, then bubble up to caller.
- All 429s include `Retry-After: 60` header (nginx vhost adds via `add_header`).

For the exact nginx directives, see §10 Step 6 vhost template — that is the
authoritative source. The table above is descriptive; the snippet below is a
condensed reference, NOT a replacement for the Step 6 template.

```nginx
# Reference only — Step 6 is canonical.
limit_req_zone $http_authorization zone=tlinh_main:10m   rate=100r/m;
limit_req_zone $http_authorization zone=tlinh_lock:10m   rate=600r/m;
limit_req_zone $http_authorization zone=tlinh_hourly:10m rate=1000r/h;
limit_req_status 429;

location = /api/health { try_files $uri /index.php?$query_string; }  # NO limit_req
location /api/lock/    { limit_req zone=tlinh_lock burst=100 nodelay;
                         limit_req zone=tlinh_hourly burst=50 nodelay; ... }
location /api/         { limit_req zone=tlinh_main burst=20 nodelay;
                         limit_req zone=tlinh_hourly burst=50 nodelay; ... }
```

---

## 8. Cloudflare DNS automation (idempotent — per Codex ISSUE-6)

All commands are run from `scripts/dns_setup.sh` (Task 9). Steps lookup existing
records first, upsert (PATCH if exists, POST if not), and are safe to rerun.

### 8.1 Common env (loaded once)

```bash
ZONE_ID="d738588b6a6169bdd8ccde1921f3a62a"
CF_EMAIL="the@duyet.dev"
CF_KEY="$(security find-generic-password -s cloudflare-global-api-key -a the@duyet.dev -w)"
SUBDOMAIN="tlinh"
FQDN="tlinh.duyet.vn"
VPS_IP="14.225.29.159"
```

### 8.2 Upsert A record (proxied=false — direct-to-origin for operational simplicity)

```bash
# 1. Look up existing record id
RECORD_ID=$(curl -fsS \
  "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/dns_records?type=A&name=${FQDN}" \
  -H "X-Auth-Email: ${CF_EMAIL}" -H "X-Auth-Key: ${CF_KEY}" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); rs=d.get("result",[]); print(rs[0]["id"] if rs else "")')

PAYLOAD='{"type":"A","name":"'${SUBDOMAIN}'","content":"'${VPS_IP}'","ttl":120,"proxied":false}'

if [ -n "$RECORD_ID" ]; then
  echo "[upsert] PATCHing existing record $RECORD_ID"
  curl -fsS -X PATCH \
    "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/dns_records/${RECORD_ID}" \
    -H "X-Auth-Email: ${CF_EMAIL}" -H "X-Auth-Key: ${CF_KEY}" \
    -H "Content-Type: application/json" -d "$PAYLOAD"
else
  echo "[upsert] POSTing new record"
  curl -fsS -X POST \
    "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/dns_records" \
    -H "X-Auth-Email: ${CF_EMAIL}" -H "X-Auth-Key: ${CF_KEY}" \
    -H "Content-Type: application/json" -d "$PAYLOAD"
fi

# 2. Wait for propagation (60s typical)
for i in $(seq 1 30); do
  if dig +short ${FQDN} @8.8.8.8 | grep -q "${VPS_IP}"; then
    echo "[ok] DNS resolves to ${VPS_IP}"; break
  fi
  sleep 5
done
```

### 8.3 Cert verification (no issuance — wildcard exists per user 2026-05-12)

```bash
# Wildcard cert for *.duyet.vn already exists on VPS at
# /etc/letsencrypt/live/duyet.vn/. Step 6 nginx vhost references it directly.
# We only VERIFY here — no `certbot` run.
#
# Per Codex ISSUE-25: nginx needs BOTH fullchain.pem AND privkey.pem, and they
# MUST be the matching pair. We verify all FIVE properties before deploy:
#   1. fullchain.pem exists.
#   2. privkey.pem exists and is readable by the nginx user.
#   3. SAN includes *.duyet.vn.
#   4. Cert is not within 24h of expiry.
#   5. fullchain.pem and privkey.pem are a matching pair, verified by comparing
#      SHA-256 fingerprints of the DER-encoded public keys derived from each
#      file (works for RSA and ECDSA — no modulus comparison).
ssh vps-root '
set -euo pipefail
CERT=/etc/letsencrypt/live/duyet.vn/fullchain.pem
KEY=/etc/letsencrypt/live/duyet.vn/privkey.pem

[ -f "$CERT" ] || { echo "[fatal] wildcard cert missing at $CERT" >&2; exit 1; }
[ -f "$KEY" ]  || { echo "[fatal] private key missing at $KEY" >&2; exit 1; }
[ -r "$KEY" ]  || { echo "[fatal] private key not readable (check perms / sudo)" >&2; exit 1; }

# Confirm SAN covers tlinh.duyet.vn (matches *.duyet.vn)
openssl x509 -in "$CERT" -noout -ext subjectAltName | grep -q "DNS:\\*\\.duyet\\.vn" \
  || { echo "[fatal] cert SAN does not include *.duyet.vn" >&2; exit 1; }

# Confirm not expired (24h slack)
openssl x509 -in "$CERT" -noout -checkend 86400 \
  || { echo "[fatal] cert expires within 24h — renew before deploying" >&2; exit 1; }

# Confirm fullchain and privkey are the matching pair.
# We compare the SHA-256 fingerprint of each file's DER-encoded public key
# (works for both RSA and ECDSA keys; no modulus comparison).
CERT_FP=$(openssl x509 -in "$CERT" -noout -pubkey | openssl pkey -pubin -outform der | sha256sum | cut -d" " -f1)
KEY_FP=$(openssl pkey -in "$KEY" -pubout -outform der | sha256sum | cut -d" " -f1)
[ "$CERT_FP" = "$KEY_FP" ] || { echo "[fatal] fullchain.pem and privkey.pem do not match (pubkey fingerprint differs)" >&2; exit 1; }

echo "[ok] wildcard cert valid and keypair matches"
'
# Renewal is handled by the existing system-wide certbot renew timer that
# owns the duyet.vn certificate. tlinh.duyet.vn rides on that cert.
```

### 8.4 Optional: flip proxied=true (deferred operational choice — cert already present)

When enabling Cloudflare proxy in front of Let's Encrypt:
- CF **SSL/TLS mode** must be set to **"Full (strict)"** (CF validates origin cert against Let's Encrypt CA). Setting "Flexible" or "Off" is INSECURE.
- The PATCH below assumes the SSL mode is already set; check `GET /zones/{id}/settings/ssl` first.

```bash
RECORD_ID=$(curl -fsS \
  "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/dns_records?type=A&name=${FQDN}" \
  -H "X-Auth-Email: ${CF_EMAIL}" -H "X-Auth-Key: ${CF_KEY}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"][0]["id"])')

# Verify SSL mode is "full" or "strict"
SSL_MODE=$(curl -fsS \
  "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/settings/ssl" \
  -H "X-Auth-Email: ${CF_EMAIL}" -H "X-Auth-Key: ${CF_KEY}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["value"])')

if [ "$SSL_MODE" != "strict" ] && [ "$SSL_MODE" != "full" ]; then
  echo "[error] Refusing to enable proxy with SSL mode '$SSL_MODE'. Set to 'strict' first." >&2
  exit 1
fi

curl -fsS -X PATCH \
  "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/dns_records/${RECORD_ID}" \
  -H "X-Auth-Email: ${CF_EMAIL}" -H "X-Auth-Key: ${CF_KEY}" \
  -H "Content-Type: application/json" -d '{"proxied":true}'
```

### 8.5 Rollback

To revert the DNS record (proxied=false, or delete):
```bash
# Flip back to DNS-only
curl -fsS -X PATCH ".../dns_records/${RECORD_ID}" ... -d '{"proxied":false}'

# Or delete entirely
curl -fsS -X DELETE ".../dns_records/${RECORD_ID}" ...
```

---

## 9. Telegram message format

VPS-side message builder (PHP):

```html
<b>[#42]</b> 🔴 score 5/5 | <i>VnExpress</i>

<b>VinFast công bố Q1/2026 vượt expect tại thị trường VN</b>
<i>Số liệu cụ thể, góc phân tích market share VinFast vs Tesla...</i>

<a href="https://vnexpress.net/...">Đọc bài</a>

Brainstorm: <code>/vf-brainstorm 42</code>
```

- HTML parse mode (`parse_mode=HTML`).
- `htmlspecialchars($title, ENT_QUOTES|ENT_HTML5, 'UTF-8')` on user-controlled fields.
- 1.2s sleep between sends (per-chat rate limit ~1/sec).
- 429 retry honoring `Retry-After` header (capped — see below).
- 5xx: exponential backoff 1s/2s/4s/8s/16s, max 5 attempts.
- Per-request HTTP timeout: 30s.

**Retry-After cap (per Codex ISSUE-8):** if Telegram returns `Retry-After`
greater than `NOTIFY_TELEGRAM_RETRY_AFTER_CAP_SECONDS` (default 60s), the
server logs `notify.retry_after_exceeded_cap`, aborts the send, releases the
claim (Phase 2 step 5), and returns 502. This prevents one slow Telegram
response from holding a notify claim past the worst-case Phase 2 budget.

**Worst-case Phase 2 budget = 5xx-backoff (31s) + retry-after cap (60s) +
5 × request timeout (150s) = 241s.** The `NOTIFY_CLAIM_TTL_SECONDS` (default
300s) is derived from this budget plus a 60s safety margin. See §4 `POST
/api/notify/{id}` "Claim TTL sizing" for the full derivation.

**Heartbeat during retries:** while Phase 2 is in flight (including sleeps
between retries), a background task refreshes `notify_claimed_at` every
`NOTIFY_CLAIM_HEARTBEAT_SECONDS` (default 20s) and aborts the send if the
claim is stolen. So the stale-claim recovery rule can only fire when the
owner has been silent for ≥ TTL seconds (~5 min), which only happens if the
owning process actually died.

Bootstrap test rows (`bootstrap-test://<TOKEN>`) include the token in the title prominently so user can visually verify which test message they received.

---

## 10. Deployment runbook (one-time, reproducible)

**Per Codex ISSUE-5 + ISSUE-12:** the runbook is split into two phases:

- **Phase A — Infrastructure deploy (automated):** `bash scripts/deploy.sh`
  executes Steps 1–9 (DNS, VPS folders, code upload, DB, .env, nginx + cert verify,
  health check, Mac client) without manual intervention. Idempotent.
- **Phase B — Cowork verify (manual one-time):** Steps 10–13 require manual
  Claude Desktop UI clicks. We seed a bootstrap test row via SSH-tunneled
  `POST /api/admin/inject-test` (deterministic — guarantees the pipeline has
  a notify-eligible row before "Run now") and then verify Telegram receipt.

AC #15 covers Phase A's automation. AC #11 covers Phase B's verification.
Re-running Phase A is safe (every step is idempotent — folder creation, DB
user, DNS record, nginx vhost all short-circuit if already done; cert is pre-issued — Step 6 only verifies validity).

All commands assume `set -euo pipefail`. Phase A runs from a single local Mac
shell session that exports `deploy.env`.

---

### Phase A — Infrastructure deploy (`scripts/deploy.sh`)

### Pre-step — Gather inputs into `deploy.env`

```bash
# All required values are collected ONCE here and consumed by the rest of the
# runbook via `set -a; . deploy.env; set +a`. Never `ssh "$EOF"` with local
# variable substitution issues.

cat > deploy.env <<'EOF'
# ---- Generated values (filled below) ----
API_TOKEN=
DB_PASS=

# ---- Constants ----
VPS_IP=14.225.29.159
VPS_SSH_ALIAS=vps-root
FQDN=tlinh.duyet.vn
SUBDOMAIN=tlinh
CF_ZONE_ID=d738588b6a6169bdd8ccde1921f3a62a
CF_EMAIL=the@duyet.dev

# ---- User-supplied (paste before running runbook) ----
TG_TOKEN=
TG_CHAT_ID=
EOF

# 1. Generate secrets
sed -i '' "s|^API_TOKEN=.*|API_TOKEN=$(openssl rand -hex 32)|" deploy.env
sed -i '' "s|^DB_PASS=.*|DB_PASS=$(openssl rand -hex 16)|" deploy.env

# 2. Telegram credentials — collect from user before continuing.
# If user already has TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID set up via the
# Mac-side flow (see README "Telegram setup"), paste them now:
#   sed -i '' "s|^TG_TOKEN=.*|TG_TOKEN=<paste>|" deploy.env
#   sed -i '' "s|^TG_CHAT_ID=.*|TG_CHAT_ID=<paste>|" deploy.env
# Otherwise, run scripts/telegram-nonce-helper.sh first (Task 8.5).

# 3. CF key from keychain (NOT stored in deploy.env — fetched per-call)
:  # placeholder; key is read inline below

# 4. Sanity: ensure all required values present
. deploy.env
: "${API_TOKEN?}" "${DB_PASS?}" "${TG_TOKEN?}" "${TG_CHAT_ID?}"

set -a  # exports everything for subsequent commands
. deploy.env
set +a

chmod 600 deploy.env  # contains DB pass + bearer
```

After this pre-step, `deploy.env` is the single source of truth for the rest of
the runbook. Every subsequent step references variables already in the shell.

### Step 1 — DNS upsert (idempotent — per §8.2)

```bash
CF_KEY=$(security find-generic-password -s cloudflare-global-api-key -a "${CF_EMAIL}" -w)
bash scripts/dns_setup.sh   # implements §8.2 upsert logic; idempotent
# After return: DNS has tlinh.duyet.vn → 14.225.29.159, proxied=false, propagated.
```

### Step 2 — Create VPS target directories (idempotent, MUST run before Step 3 rsync — per Codex ISSUE-11)

```bash
# Directories MUST exist before rsync uploads into them. This step is split out
# from the DB/schema work in old Step 3 so the runbook is correctly sequenced
# on a fresh VPS.
ssh "${VPS_SSH_ALIAS}" "bash -s" << 'REMOTE'
set -euo pipefail
mkdir -p /var/www/tlinh/tlinh.duyet.vn/releases/v1/{public,src/routes}
mkdir -p /var/www/tlinh/tlinh.duyet.vn/shared
chown -R www-data:www-data /var/www/tlinh
REMOTE
```

### Step 3 — Upload code + schema to VPS

```bash
# Rsync code (idempotent — target dirs created in Step 2)
rsync -avz --delete --exclude='.git' --exclude='__pycache__' \
  vps/ "${VPS_SSH_ALIAS}":/var/www/tlinh/tlinh.duyet.vn/releases/v1/

# Upload schema.sql separately so Step 4 can find it
scp vps/schema.sql "${VPS_SSH_ALIAS}":/tmp/schema.sql
```

### Step 4 — DB user + schema (idempotent)

```bash
# Use ssh -T with heredoc and inject variables explicitly. NO bash here-string
# interpolation across the SSH boundary; instead pass via env -i + ssh's
# SendEnv/AcceptEnv or just inline via "ssh ... bash -s -- arg1 arg2".

ssh "${VPS_SSH_ALIAS}" "DB_PASS='${DB_PASS}' bash -s" << 'REMOTE'
set -euo pipefail
# MariaDB root socket-auth (works on Ubuntu's default debian-sys-maint setup).
# If your VPS root user has a password, switch to that auth here.
mariadb -u root <<SQL
CREATE DATABASE IF NOT EXISTS tlinh_news CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'tlinh'@'localhost' IDENTIFIED BY '${DB_PASS}';
ALTER USER 'tlinh'@'localhost' IDENTIFIED BY '${DB_PASS}';   -- idempotent password set
GRANT SELECT, INSERT, UPDATE, DELETE ON tlinh_news.* TO 'tlinh'@'localhost';
FLUSH PRIVILEGES;
SQL

# Apply schema — uses CREATE TABLE IF NOT EXISTS so idempotent.
mariadb -u tlinh -p"${DB_PASS}" tlinh_news < /tmp/schema.sql
REMOTE
```

### Step 5 — Write VPS `.env`

```bash
ssh "${VPS_SSH_ALIAS}" "API_TOKEN='${API_TOKEN}' DB_PASS='${DB_PASS}' \
  TG_TOKEN='${TG_TOKEN}' TG_CHAT_ID='${TG_CHAT_ID}' bash -s" << 'REMOTE'
set -euo pipefail
ENV_PATH=/var/www/tlinh/tlinh.duyet.vn/shared/.env
umask 077
cat > "${ENV_PATH}" <<EOF
DB_HOST=127.0.0.1
DB_NAME=tlinh_news
DB_USER=tlinh
DB_PASS=${DB_PASS}
API_TOKEN=${API_TOKEN}
TELEGRAM_BOT_TOKEN=${TG_TOKEN}
TELEGRAM_CHAT_ID=${TG_CHAT_ID}
TITLE_DEDUP_WINDOW_HOURS=48
MAX_RETRIES=3
LOCK_TTL_SECONDS=1800
NOTIFY_CLAIM_TTL_SECONDS=300
NOTIFY_CLAIM_HEARTBEAT_SECONDS=20
NOTIFY_TELEGRAM_RETRY_AFTER_CAP_SECONDS=60
NOTIFY_TELEGRAM_REQUEST_TIMEOUT_SECONDS=30
MIN_SCORE_TO_NOTIFY=3
MAX_ARTICLES_PER_LIST=50
EOF
chmod 600 "${ENV_PATH}"
chown www-data:www-data "${ENV_PATH}"

# Symlink current → release v1 (atomic switch)
ln -sfn /var/www/tlinh/tlinh.duyet.vn/releases/v1 \
        /var/www/tlinh/tlinh.duyet.vn/current
chown -R www-data:www-data /var/www/tlinh
REMOTE
```

### Step 6 — nginx vhost (uses existing wildcard cert; idempotent — per Codex ISSUE-9, ISSUE-10 + 2026-05-12 wildcard pivot)

```bash
ssh "${VPS_SSH_ALIAS}" "FQDN='${FQDN}' CF_EMAIL='${CF_EMAIL}' bash -s" << 'REMOTE'
set -euo pipefail
VHOST=/etc/nginx/sites-available/"${FQDN}"

# Write only if missing or differs (idempotent)
TMPF=$(mktemp)
cat > "${TMPF}" <<NGINX
# Rate-limit zones (per Codex ISSUE-10)
limit_req_zone \$http_authorization zone=tlinh_main:10m   rate=100r/m;
limit_req_zone \$http_authorization zone=tlinh_lock:10m   rate=600r/m;
# Global: every 429 carries Retry-After: 60 and is returned with HTTP 429 (not the default 503)
limit_req_status 429;

# 1000 req/hour combined per token, enforced via a parallel zone with longer
# bucket. Burst handles short spikes; sustained traffic above the rate is 429'd.
limit_req_zone \$http_authorization zone=tlinh_hourly:10m rate=1000r/h;

# Add Retry-After: 60 to all 429 responses. (\$limit_req_status is non-empty only on 429.)
map \$status \$retry_after_header { 429 "60"; default ""; }

# HTTP → HTTPS redirect (cert covers all of *.duyet.vn — no acme-challenge needed)
server {
    listen 80;
    server_name ${FQDN};
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl http2;
    server_name ${FQDN};
    root /var/www/tlinh/${FQDN}/current/public;
    index index.php;
    client_max_body_size 5M;

    # Existing wildcard cert (NO certbot run during deploy — per user 2026-05-12)
    ssl_certificate     /etc/letsencrypt/live/duyet.vn/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/duyet.vn/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # Security headers (per §5.4 promise + Codex ISSUE-28)
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options        "DENY" always;
    add_header Referrer-Policy        "strict-origin-when-cross-origin" always;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    add_header Retry-After \$retry_after_header always;

    # Health: NEVER rate-limited (monitoring). Per Codex ISSUE-10.
    location = /api/health {
        # No limit_req here on purpose. AC #14 requires 100 health checks in
        # 60s to always return 200. Health is cheap (PING + 200), no auth.
        try_files \$uri /index.php?\$query_string;
    }

    # Localhost-only admin endpoint (per Codex ISSUE-9). Bearer is still
    # required at the PHP layer, but nginx rejects non-loopback callers FIRST.
    location = /api/admin/inject-test {
        allow 127.0.0.1;
        allow ::1;
        deny all;
        # Still subject to main bucket (avoids local script loop runaway).
        limit_req zone=tlinh_main burst=20 nodelay;
        limit_req zone=tlinh_hourly burst=50 nodelay;
        try_files \$uri /index.php?\$query_string;
    }

    location /api/lock/ {
        limit_req zone=tlinh_lock burst=100 nodelay;
        limit_req zone=tlinh_hourly burst=50 nodelay;
        try_files \$uri /index.php?\$query_string;
    }
    location /api/ {
        limit_req zone=tlinh_main burst=20 nodelay;
        limit_req zone=tlinh_hourly burst=50 nodelay;
        try_files \$uri /index.php?\$query_string;
    }
    location / { try_files \$uri \$uri/ /index.php?\$query_string; }

    location ~ \.php\$ {
        fastcgi_split_path_info ^(.+\.php)(/.+)\$;
        fastcgi_pass unix:/run/php/php8.5-fpm.sock;
        fastcgi_index index.php;
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
        fastcgi_param HTTP_AUTHORIZATION \$http_authorization;
    }
}
NGINX

# Verify wildcard cert + private key BEFORE writing/reloading nginx (per Codex ISSUE-25).
# nginx requires BOTH files and they MUST be the matching pair.
CERT=/etc/letsencrypt/live/duyet.vn/fullchain.pem
KEY=/etc/letsencrypt/live/duyet.vn/privkey.pem
[ -f "$CERT" ] || { echo "[fatal] wildcard cert missing at $CERT — recovery: SSH to VPS as root and re-run the existing certbot DNS-01 flow for *.duyet.vn, e.g. 'certbot certonly --manual --preferred-challenges dns -d \"*.duyet.vn\" -d duyet.vn' (or whatever flow originally issued the cert). This deploy script does NOT issue certs; cert lifecycle is owned outside v2." >&2; rm -f "${TMPF}"; exit 1; }
[ -f "$KEY" ]  || { echo "[fatal] private key missing at $KEY" >&2; rm -f "${TMPF}"; exit 1; }
[ -r "$KEY" ]  || { echo "[fatal] private key not readable (check perms / sudo)" >&2; rm -f "${TMPF}"; exit 1; }
openssl x509 -in "$CERT" -noout -ext subjectAltName | grep -q 'DNS:\*\.duyet\.vn' \
  || { echo "[fatal] cert SAN does not include *.duyet.vn" >&2; rm -f "${TMPF}"; exit 1; }
openssl x509 -in "$CERT" -noout -checkend 86400 \
  || { echo "[fatal] cert expires within 24h — renew before deploying" >&2; rm -f "${TMPF}"; exit 1; }
# Pubkey-fingerprint match (works for RSA + EC keys)
CERT_FP=$(openssl x509 -in "$CERT" -noout -pubkey | openssl pkey -pubin -outform der | sha256sum | cut -d' ' -f1)
KEY_FP=$(openssl pkey -in "$KEY" -pubout -outform der | sha256sum | cut -d' ' -f1)
[ "$CERT_FP" = "$KEY_FP" ] || { echo "[fatal] fullchain.pem and privkey.pem do not match" >&2; rm -f "${TMPF}"; exit 1; }

if ! cmp -s "${TMPF}" "${VHOST}" 2>/dev/null; then
  mv "${TMPF}" "${VHOST}"
  ln -sfn "${VHOST}" /etc/nginx/sites-enabled/"${FQDN}"
  nginx -t && systemctl reload nginx
else
  rm -f "${TMPF}"
fi
# NOTE: no certbot run — wildcard *.duyet.vn cert is already issued at
# /etc/letsencrypt/live/duyet.vn/. Renewal is owned by the existing
# system-wide certbot renew timer for that cert.
REMOTE
```

### Step 7 — Health check

```bash
curl -fsSL "https://${FQDN}/api/health"
# Expected: {"status":"ok","db":"connected","version":"v2-..."}
```

### Step 8 — Mac client

```bash
cd /Users/theduyet/Documents/Code/vin-automate
bash install.sh   # idempotent venv + deps

# Mac-side .env (separate from VPS .env — only contains API_TOKEN + API_BASE_URL)
cat > .env <<EOF
API_BASE_URL=https://${FQDN}
API_TOKEN=${API_TOKEN}
EOF
chmod 600 .env

# Smoke
.venv/bin/python -c "from api_client import ApiClient; print(ApiClient().health())"
# Expected: {'status': 'ok', 'db': 'connected', ...}
```

### Step 9 — launchd (Mac daily crawl)

```bash
# Install plist
cp com.tlinh.crawl.plist ~/Library/LaunchAgents/

# launchd bootstrap (uses modern syntax, not deprecated load/start)
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.tlinh.crawl.plist
launchctl kickstart -k "gui/$(id -u)/com.tlinh.crawl"  # immediate trigger

# Verify: logs/crawl.out has output
tail logs/crawl.out
```

### Step 9.end — Phase A complete

After Step 9, `scripts/deploy.sh` exits 0. Infrastructure is live, DNS+TLS
resolve, Mac launchd is running. **At this point no Telegram message has
been sent yet — that requires Phase B below.**

---

### Phase B — Cowork verify (manual one-time)

**Pre-step — Re-source `deploy.env` into the current shell (per Codex ISSUE-17).**
Phase A ran inside `bash scripts/deploy.sh` (a subshell), so its exported
variables are NOT in the operator's interactive shell when Phase A returns.
Phase B commands reference `${VPS_SSH_ALIAS}`, `${FQDN}`, `${API_TOKEN}`,
`${DB_PASS}` — these MUST be re-loaded before continuing:

```bash
# In the SAME terminal where Phase A was run (deploy.env still on disk):
set -a
. deploy.env
set +a

# Sanity:
: "${VPS_SSH_ALIAS?}" "${FQDN?}" "${API_TOKEN?}" "${DB_PASS?}"
```

If `deploy.env` was deleted or you opened a new terminal, regenerate it from
secrets you saved elsewhere — there is no way to recover `API_TOKEN` from the
VPS (it's hashed-compared, not extractable). `DB_PASS` can be reset via
`mariadb -u root` if lost; `API_TOKEN` requires regenerating both VPS and
client-side .env files.

These steps require manual Claude Desktop UI clicks and cannot be automated
by `deploy.sh` (Cowork project creation and `/schedule` saves are UI-only).

### Step 10 — Cowork-side `SKILLS/.env`

```bash
# Cowork sources this file as the only API_TOKEN path (see §5.3 mandate).
# Same token as Mac, separate file for blast-radius isolation.
cat > SKILLS/.env <<EOF
API_BASE_URL=https://${FQDN}
API_TOKEN=${API_TOKEN}
EOF
chmod 600 SKILLS/.env
echo 'SKILLS/.env' >> .gitignore  # ensure ignored
```

### Step 11 — Cowork project + scheduled task (manual UI clicks)

In Claude Desktop:
1. Create Cowork project pointing at `vin-automate` folder.
2. Grant bash + network "Allow all".
3. Open Cowork chat, paste `cowork-task-prompt.md` content into `/schedule` UI.
4. Set frequency: **Daily**. Name: `vinfast-pipeline`. Save.

Do NOT click "Run now" yet — Step 12 first seeds a verifiable test row.

### Step 12 — Seed bootstrap test row via SSH-tunneled inject-test (per Codex ISSUE-12)

The admin endpoint is localhost-only at nginx level (Step 6 vhost). We tunnel
from Mac to VPS loopback to seed exactly one verifiable row:

```bash
# Pick a unique hex token for this verification run
BOOTSTRAP_TOKEN=$(openssl rand -hex 8)
echo "Verification token: ${BOOTSTRAP_TOKEN}"  # USER: copy this — Telegram message must contain it

# Open SSH tunnel: local 18443 → VPS 127.0.0.1:443 (nginx loopback)
# Run in background; the trap kills it on exit.
ssh -f -N -L 18443:127.0.0.1:443 "${VPS_SSH_ALIAS}"
TUNNEL_PID=$(pgrep -f "ssh -f -N -L 18443:127.0.0.1:443 ${VPS_SSH_ALIAS}" | head -1)
trap 'kill ${TUNNEL_PID} 2>/dev/null || true' EXIT

# POST through the tunnel. -k (insecure) because we connect via tunnel
# but nginx's cert is for tlinh.duyet.vn — set Host header explicitly so
# nginx routes to the right vhost. The 127.0.0.1 source IP is what
# satisfies the nginx allow 127.0.0.1; deny all rule for /api/admin/.
curl -fsSk -X POST "https://127.0.0.1:18443/api/admin/inject-test" \
  -H "Host: ${FQDN}" \
  -H "Authorization: Bearer ${API_TOKEN}" \
  -H "Content-Type: application/json" \
  --resolve "${FQDN}:18443:127.0.0.1" \
  -d "{\"token\":\"${BOOTSTRAP_TOKEN}\"}"

# Expected: 201 {"id":N,"created":true,"url":"bootstrap-test://<TOKEN>","score":5}
# Verify pre-Cowork state: row exists with score=5, notified_at=NULL.
```

### Step 13 — Click "Run now" + verify Telegram

1. In the Cowork UI for `vinfast-pipeline`, click **Run now**.
2. Wait up to 10 minutes for the scheduled task to acquire the lock, fetch
   the bootstrap row (first by ORDER BY priority), POST `/api/notify/{id}`,
   and send to Telegram.
3. **Expected:** Telegram receives a message containing `${BOOTSTRAP_TOKEN}`
   in the title (HTML-escaped). The token in the user's terminal MUST match
   the token in the message — that proves the verification row is the
   actually-notified row.
4. **DB check:**
   ```bash
   ssh "${VPS_SSH_ALIAS}" mariadb -u tlinh -p"${DB_PASS}" tlinh_news \
     -e "SELECT id, notified_at, telegram_msg_id FROM articles \
         WHERE url = 'bootstrap-test://${BOOTSTRAP_TOKEN}'"
   # notified_at NOT NULL, telegram_msg_id NOT NULL
   ```

### Step 14 — Delete `deploy.env` (cleanup)

```bash
shred -u deploy.env  # local cleanup of secrets
```

---

## 11. Task breakdown (for implementation)

Tasks ordered by dependency. Each task = 1 commit, reviewed by Codex.

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 1 | DB schema SQL file (includes notify_claimed_at/notify_claim_owner per ISSUE-1) | `vps/schema.sql` | `mariadb -u tlinh -p < schema.sql` idempotent; `SHOW TABLES` returns `articles, locks`; `DESCRIBE articles` shows notify_claimed_at + notify_claim_owner columns |
| 2 | PHP backend skeleton | `vps/public/index.php`, `vps/src/bootstrap.php`, `vps/src/Db.php`, `vps/src/Auth.php` (hash_equals constant-time compare), `vps/src/Router.php` | `curl /api/health` returns 200 JSON; auth uses constant-time compare |
| 3 | Articles endpoints | `vps/src/routes/articles.php` (POST + GET + GET id + PATCH extract) | Mac can POST + GET; idempotency works; reserved URL scheme `bootstrap-test://` rejected |
| 3a | **Admin inject-test endpoint** (per Codex ISSUE-3) | `vps/src/routes/admin.php` (`POST /api/admin/inject-test`) | localhost-only (nginx allow/deny); idempotent per hex token; inserts pre-scored row with reserved URL scheme |
| 4 | Score + brainstorm endpoints | `vps/src/routes/score.php`, `brainstorm.php` | CAS no-op verified; 2nd PATCH returns updated:false |
| 4a | **Fail endpoint** (per Codex ISSUE-2) | `vps/src/routes/fail.php` (`POST /api/articles/{id}/fail`) | retry_count increments; final_state='discarded' at MAX_RETRIES; success-path clearing of failed_at/last_error |
| 5 | Lock endpoints | `vps/src/routes/lock.php` (acquire/heartbeat/release) | 409 on concurrent acquire; TTL reclaim works; expired-lock cleanup log line |
| 6 | **Notify endpoint with 2-phase claim** (per Codex ISSUE-1) | `vps/src/routes/notify.php`, `vps/src/Telegram.php` | Phase 1 atomic claim via UPDATE returns 1; concurrent callers second one gets rowcount=0 → no Telegram send; stale-claim recovery via TTL works; HTML escape on title verified |
| 7 | Hashing helpers + URL canonicalization | `vps/src/Hashing.php` | url_hash matches Python output on identical canonical inputs (cross-test with Mac client) |
| 8 | Phase A deploy script + nginx vhost template (idempotent Steps 1–9 per ISSUE-12 + ISSUE-18) | `scripts/deploy.sh`, `vps/nginx-tlinh.conf.tpl` | `bash scripts/deploy.sh` succeeds twice in a row without errors; second run reports all steps no-op. **deploy.env is NOT shredded by this script** — it must survive into Phase B (operator-driven `shred -u` at Step 14). Script does NOT call Cowork UI, NOT seed inject-test. |
| 8a | **Telegram nonce helper** | `scripts/telegram-nonce-helper.sh` | Collects bot token + chat_id via deterministic nonce flow; writes to `deploy.env` |
| 9 | DNS automation (idempotent upsert + SSL mode guard per ISSUE-6) | `scripts/dns_setup.sh` | First run creates record; second run no-op or PATCH; refuses to enable proxy if CF SSL mode != strict/full |
| 10 | Mac `api_client.py` (httpx wrapper + retry + 429 handling per ISSUE-7) | `api_client.py` | Unit test: round-trip POST + GET works on staging VPS; 429 retry with backoff |
| 11 | Mac `crawl.py` rewrite (HTTP-based) | `crawl.py` | `python crawl.py --dry-run` reports counts; real run inserts via API; same url_hash as VPS computes |
| 12 | Mac `extract.py` rewrite (HTTP-based, two-level retry, calls /fail on exhaust) | `extract.py` | Extracts rows where `extracted_at IS NULL`; on transient exhaustion calls POST /fail with stage=extract |
| 13 | **Cowork scheduled task prompt** (single auth path per ISSUE-4, exempts /lock from 429 per ISSUE-7) | `cowork-task-prompt.md` | First line sources SKILLS/.env or hard-exits; heartbeat handling respects 429 retry-after; tolerates 429 on lock endpoints without aborting |
| 14 | **Cowork brainstorm skill** (single auth path) | `SKILLS/vf-brainstorm.skill` | `/vf-brainstorm 42` produces 5 ideas, written via PATCH; sources SKILLS/.env |
| 15 | launchd plist + `main.py` orchestrator | `com.tlinh.crawl.plist`, `main.py` | `launchctl kickstart` triggers crawl + extract; logs land in `logs/crawl.out` |
| 16 | install.sh slimmed (venv + deps only, no schema) | `install.sh` | First run installs deps; second run no-op; no news.db created |
| 17 | README v2 | `README.md` | Onboarding instructions accurate; quickstart works end-to-end with one user following |
| 18 | E2E validation runbook (covers all §12 ACs incl. new fail/admin/notify-2-phase) | `E2E-VALIDATION-v2.md` | Every row from §13 has executable scenario + expected evidence |

---

## 12. Acceptance criteria

### AC #1 — Mac crawler inserts rows into VPS

`.venv/bin/python crawl.py --topic vinfast` succeeds without errors; VPS `SELECT count(*) FROM articles WHERE crawled_at > NOW() - INTERVAL 1 HOUR` returns matching count. Re-running: same URLs return 200 (existing), no duplicate rows (url_hash UNIQUE).

### AC #2 — Mac extractor fills content

`.venv/bin/python extract.py --pending` extracts rows where `extracted_at IS NULL`. After: those rows have `extracted_at NOT NULL` and `content` populated.

### AC #3 — Cowork scheduled task scores + notifies

Cowork's saved daily task `vinfast-pipeline`, when clicked "Run now":
- Acquires lock via `/api/lock/pipeline-run/acquire`.
- Fetches extracted articles.
- For each row, Claude assigns score 1–5 + reason; PATCH writes via `/api/articles/{id}/score`.
- For scored rows above threshold: POST `/api/notify/{id}` triggers Telegram send.
- Releases lock.

End state: user receives Telegram messages for high-score articles.

### AC #4 — /vf-brainstorm skill writes brainstorm

In Cowork chat, `/vf-brainstorm 42` reads article via `/api/articles/42`, generates 5 ideas, PATCHes via `/api/articles/42/brainstorm`. DB row has `brainstormed_at NOT NULL` and `ideas` JSON of length 5.

### AC #5 — Concurrency safety (run-level + per-row + notify 2-phase)

- Two simultaneous Cowork sessions (manual `Run now` + scheduled tick) → second `acquire` returns 409 → second session aborts cleanly.
- No duplicate scoring: 2nd `PATCH /score` returns `updated: false`.
- **No duplicate Telegram send (per Codex ISSUE-1): two concurrent `POST /notify/{id}` requests → only ONE sends to Telegram. The other gets `sent: false, reason: already_claimed` because Phase 1 atomic claim rowcount=0.** Verified by intercepting Telegram API or by `mock_telegram=1` env flag during testing.

### AC #5a — Notify 2-phase atomicity under failure

If `POST /notify` succeeds Phase 1 (claim) but the Telegram API call fails permanently (4xx other than 429), the server:
1. Releases the claim (sets `notify_claimed_at` and `notify_claim_owner` back to NULL).
2. Calls `/api/articles/{id}/fail` internally with `stage=notify`.
3. Returns HTTP 502 to the caller.

A subsequent `POST /notify/{id}` then succeeds Phase 1 again (claim re-available) and retries.

### AC #5b — Stale claim recovery (per Codex ISSUE-8)

`NOTIFY_CLAIM_TTL_SECONDS` (default 300s) is sized to bound the worst-case Phase 2 budget (Telegram retry backoff + `Retry-After` cap + 5 × request timeout = 241s) plus 60s safety. A live owner refreshes `notify_claimed_at` every `NOTIFY_CLAIM_HEARTBEAT_SECONDS` (default 20s) for the entire Phase 2 duration, including sleeps between retries.

- **Live-owner safety:** Simulate a slow Telegram by mocking `sendMessage` to sleep 200s. During the sleep, run a second `POST /notify/{id}` from another connection. The second caller's Phase 1 atomic claim fails (`rowcount = 0`) because `notify_claimed_at` is still fresh from heartbeats. Expected: only ONE Telegram send occurs.
- **Dead-owner reclaim:** Kill the PHP worker mid-send (no heartbeats). Wait > `NOTIFY_CLAIM_TTL_SECONDS`. Trigger `POST /notify/{id}` again. Expected: stale-claim recovery clears the abandoned claim and the new caller succeeds Phase 1.
- **Retry-After cap:** Mock Telegram to return `429 Retry-After: 9999`. Expected: server logs `notify.retry_after_exceeded_cap`, releases claim, calls `/fail` with `stage=notify`, returns 502. No claim is held past TTL.
- **Boot validation:** Set `NOTIFY_CLAIM_HEARTBEAT_SECONDS=200` and `NOTIFY_CLAIM_TTL_SECONDS=60` in `.env`. Server boot fails with explicit message `[fatal] NOTIFY_CLAIM_TTL_SECONDS must be ≥ 3 × NOTIFY_CLAIM_HEARTBEAT_SECONDS AND ≥ NOTIFY_PHASE2_BUDGET_SECONDS + 60`.

### AC #6 — HTTPS + bearer auth

- `curl https://tlinh.duyet.vn/api/health` returns 200 (no auth required for health).
- `curl /api/articles` without bearer → 401.
- `curl /api/articles` with wrong bearer → 401.
- `curl /api/articles` with correct bearer → 200.

### AC #7 — Idempotent endpoints

- `POST /api/articles` with same payload twice → 1 row inserted, 2nd response says `created: false`.
- `PATCH /api/articles/{id}/score` twice → 2nd returns `updated: false, reason: already_scored`.
- `DELETE /api/lock/{name}` twice → both return 200.

### AC #8 — Verification rows starve-resistant

Inject row via admin endpoint with `bootstrap-test://<TOKEN>`. `GET /api/articles?stage=scored&not_notified=1` returns it FIRST (priority ORDER BY).

### AC #9 — TTL lock cleanup

`acquire` with TTL=1s; sleep 2; second acquire reclaims (returns 201 with new owner_id). Locks table contains 1 row (new) not 2.

### AC #10 — Telegram HTML safety

Title containing `<b>&*_[]</b>` → server escapes via `htmlspecialchars`; Telegram receives 200 OK, message renders without error.

### AC #11 — Bootstrap verification flow (per ISSUE-3 + ISSUE-12)

1. **Seed (Step 12 of runbook):** From Mac, open SSH tunnel `ssh -L 18443:127.0.0.1:443 vps-root`, then `curl -X POST https://127.0.0.1:18443/api/admin/inject-test` with `Host: tlinh.duyet.vn`, `--resolve tlinh.duyet.vn:18443:127.0.0.1`, bearer token, and JSON body `{"token": "abc123def456"}`. The 127.0.0.1 source satisfies the nginx `allow 127.0.0.1; deny all` rule.
2. **Non-localhost rejected:** From Mac directly (no tunnel), `curl -X POST https://tlinh.duyet.vn/api/admin/inject-test ...` returns 403 (nginx deny).
3. Click "Run now" on Cowork scheduled task (Step 13).
4. Within 10 min, Telegram receives message containing `abc123def456` literally (HTML-escaped title).
5. DB: row with `url=bootstrap-test://abc123def456` has `notified_at NOT NULL` and `telegram_msg_id NOT NULL`.
6. Re-running Step 1 with same token: returns existing row id (idempotent, no duplicate, `created: false`).

### AC #12 — Fail/retry semantics (per ISSUE-2 + ISSUE-13)

1. Trigger extract on an unreachable URL → `extract.py` retries in-process up to `MAX_RETRIES` then calls `POST /api/articles/{id}/fail` with `stage=extract`.
2. DB: `retry_count = 1`, `failed_at` set, `last_error` populated. Row is STILL in `stage=new` because `retry_count < MAX_RETRIES AND final_state IS NULL`.
3. Next scheduled run's `GET /api/articles?stage=new` returns the same row (predicate per §4); extract fails again → `retry_count = 2`.
4. After `MAX_RETRIES` failed calls: server sets `final_state = 'discarded'`. **The discarded row is excluded from `stage=new` by predicate** (`final_state IS NULL` fails). Row never re-attempted by any client.
5. Verify exclusion: `GET /api/articles?stage=new` after discard returns no row for that id. `GET /api/articles?final_state=discarded` returns it.
6. Manual recovery: direct SQL via VPS shell, e.g. `UPDATE articles SET final_state = NULL, retry_count = 0, failed_at = NULL, last_error = NULL WHERE id = ?` to put a discarded row back into the queue. (No `mark.py` tool — that was v1 and is removed in v2.)

### AC #13 — Single auth path enforced (per ISSUE-4)

- `SKILLS/.env` missing → Cowork bash invocation exits 1 with explicit `[error] SKILLS/.env missing or unreadable` message.
- Hardcoded `API_TOKEN` in any committed skill file or task prompt → CI/manual grep `grep -rE 'API_TOKEN=[a-f0-9]{16,}'` returns zero matches.
- `SKILLS/.env` is in `.gitignore`. Verified by `git check-ignore SKILLS/.env`.

### AC #14 — Rate limiting does not break heartbeat (per ISSUE-7 + ISSUE-15 + ISSUE-16)

- **Heartbeats:** Send 100+ heartbeats (`/api/lock/<name>/heartbeat`) in 60 seconds. None receive 429. (Lock bucket: 600 req/min + burst 100.)
- Verify by simulating a long-running score loop with `HEARTBEAT_EVERY_N_ROWS=1` (one heartbeat per row) over 100 rows in 60s.
- **Health unlimited:** Send 1000 `/api/health` requests in 60s. None receive 429. Verify nginx config has `location = /api/health` with NO `limit_req` directive: `nginx -T | awk '/location = \/api\/health/,/^[[:space:]]*}$/'` must not contain `limit_req`.
- **Hourly leaky-bucket — paced load (per Codex ISSUE-20):** `/api/` paths are subject to BOTH `tlinh_main` (100 req/min + burst 20) and `tlinh_hourly` (1000 req/h ≈ 16.67 req/min + burst 50). A 1100-in-60s burst trips `tlinh_main` first and does not isolate `tlinh_hourly`. To verify the hourly zone independently, send a paced load that stays below the main bucket but exceeds the hourly bucket: **send 50 `/api/articles` requests per minute for 20 minutes** (total 1000 requests). Expected:
  - `tlinh_main` never trips (50/min < 100/min cap; well within steady-state).
  - `tlinh_hourly` admits burst (50) immediately, then drains at 16.67/min. Sustained 50/min exceeds drain rate → after ~3–4 minutes, `tlinh_hourly` is empty and subsequent requests receive 429 with `Retry-After: 60`.
  - Approximately the first 50 + (16.67 × 20) ≈ 383 requests succeed; the remaining ~617 receive 429. (Exact count depends on nginx's worker timing; the AC only asserts directional behavior, not an exact pass/fail count.)
  - The test passes if: (a) zero 429s came from `tlinh_main` (check `nginx error_log` for `limiting requests, excess: ... by zone "tlinh_main"`); (b) at least 200 requests received 429 attributed to `tlinh_hourly`.
- **429 envelope:** Every 429 response has `Retry-After: 60` (`curl -i ... | grep -i retry-after`).

### AC #15 — Deploy runbook is reproducible (per ISSUE-5 + ISSUE-12 + ISSUE-17 + ISSUE-18)

**Phase A (automated):**
- Fresh VPS (or after `rm -rf /var/www/tlinh`): `bash scripts/deploy.sh` completes Steps 1–9 (DNS, VPS folders, code upload, DB, .env, nginx vhost + wildcard-cert preflight, health check, Mac client, launchd) with no manual intervention. Exits 0. **No certbot run** — wildcard `*.duyet.vn` cert is pre-installed. Step 6 preflight (per Codex ISSUE-25) aborts BEFORE writing the vhost or reloading nginx if any of these fail: (a) `fullchain.pem` missing, (b) `privkey.pem` missing or unreadable, (c) cert SAN does not include `*.duyet.vn`, (d) cert within 24h of expiry, (e) cert and key are not a matching pair (pubkey-fingerprint mismatch).
- Second run of `bash scripts/deploy.sh`: every step short-circuits as "already done". Exits 0.
- After Phase A: `curl https://${FQDN}/api/health` returns 200; Mac launchd shows the job loaded; no Telegram message has been sent yet (intentional).
- **`deploy.env` is NOT shredded at end of Phase A.** It must persist on Mac disk so the operator can re-source it for Phase B (`set -a; . deploy.env; set +a`). Verify: after `bash scripts/deploy.sh`, `ls -la deploy.env` returns the file with mode `600`.

**Phase B (manual one-time, covered by AC #11):**
- The Cowork project creation, `/schedule` save, and "Run now" click are UI actions that `deploy.sh` cannot perform. The runbook lists them as Steps 10–13.
- The SSH-tunneled `inject-test` (Step 12) seeds the verifiable row deterministically. AC #11 verifies end-to-end Telegram receipt.

**Cleanup:**
- `deploy.env` is deleted at end via `shred -u` (Step 14).

### AC #16 — DNS automation is idempotent (per ISSUE-6)

- `bash scripts/dns_setup.sh` first run creates record.
- Second run detects existing record, PATCHes (no duplicate). Returns 200.
- If SSL mode not strict/full, refuses to enable `proxied=true` with error exit.

---

## 13. E2E validation matrix (drives Task 18)

| AC # | Scenario | How to run | Expected evidence |
|------|----------|-----------|-------------------|
| 1 | Mac inserts row | `.venv/bin/python crawl.py --topic vinfast` | Mac stdout: "inserted N candidates". VPS: `SELECT count(*) FROM articles` matches. |
| 1-rerun | Idempotency | Run crawl.py twice in succession | 2nd run: 0 new (or all existing). url_hash UNIQUE prevents dups. |
| 2 | Mac extract | `.venv/bin/python extract.py --pending` | Rows have content + extracted_at. |
| 3 | Cowork score loop | Cowork sidebar → vinfast-pipeline → "Run now" | All extracted rows get scored. Telegram receives high-score alerts. |
| 4 | Brainstorm skill | `/vf-brainstorm 42` in Cowork chat | DB row 42: brainstormed_at + ideas[5] populated. User sees ideas table. |
| 5 | Concurrent acquire | Two shell sessions both call `/api/lock/pipeline-run/acquire` | 2nd → 409. Only 1 row in locks table. |
| 6 | Auth check | `curl /api/articles` (no bearer), then with correct | 401, then 200. |
| 7 | Idempotent score | Two PATCH score on same id | 2nd: updated=false. DB unchanged. |
| 8 | Priority sort | Inject bootstrap-test row + 50 real rows | GET articles returns bootstrap-test first. |
| 9 | TTL reclaim | acquire ttl=1, sleep 2, acquire | 2nd succeeds, new owner_id, locks count = 1. |
| 10 | HTML escape | Inject article with title `<b>&*_[]</b>` | Telegram receives 200. Visual: tag rendered as text. |
| 11 | Bootstrap end-to-end (SSH-tunneled — ISSUE-12) | Mac: `ssh -L 18443:127.0.0.1:443 vps-root` then `curl -k -X POST https://127.0.0.1:18443/api/admin/inject-test -H "Host: tlinh.duyet.vn" --resolve tlinh.duyet.vn:18443:127.0.0.1 -d '{"token":"abc123def456"}'`; then Run now Cowork | 201 created. Direct `curl https://tlinh.duyet.vn/api/admin/inject-test` from Mac WITHOUT tunnel returns 403 (nginx deny). Telegram message contains "abc123def456" within 10 min. notified_at set. Re-inject same token → existing row id returned, no duplicate. |
| 5a | Notify 2-phase under failure | Force Telegram 400 (e.g. bad chat_id env override): POST /notify/{id} | Phase 1 claims, Telegram fails 400 → claim released (`notify_claimed_at=NULL`), /fail called (retry_count++), HTTP 502 returned to caller. Subsequent POST succeeds Phase 1 again. |
| 5b-live | Live-owner heartbeat (ISSUE-8) | Mock `sendMessage` to sleep 200s; concurrent POST /notify/{id} from second connection during sleep | Second caller's Phase 1 returns rowcount=0 (notify_claimed_at still fresh from heartbeats). Only ONE Telegram send. |
| 5b-dead | Dead-owner reclaim (ISSUE-8) | Kill PHP worker mid-send (heartbeats stop). Wait > NOTIFY_CLAIM_TTL_SECONDS (300s default). POST /notify/{id} again. | Stale-claim recovery clears abandoned claim. New caller succeeds Phase 1. notified_at populated. |
| 5b-cap | Retry-After cap (ISSUE-8) | Mock Telegram to return `429 Retry-After: 9999` | Server logs `notify.retry_after_exceeded_cap`, releases claim, calls /fail with stage=notify, returns 502. Claim NOT held past TTL. |
| 5b-boot | TTL/heartbeat boot validation (ISSUE-8) | Set `NOTIFY_CLAIM_HEARTBEAT_SECONDS=200` and `NOTIFY_CLAIM_TTL_SECONDS=60` in `.env`, restart php-fpm | Boot fails with `[fatal] NOTIFY_CLAIM_TTL_SECONDS must be ≥ 3 × NOTIFY_CLAIM_HEARTBEAT_SECONDS AND ≥ NOTIFY_PHASE2_BUDGET_SECONDS + 60`. |
| 12 | Fail/retry → discard exclusion (ISSUE-13) | Mock httpx transient error N+1 times; trigger N extract runs | After MAX_RETRIES (default 3) failed runs: row's final_state='discarded'. `GET /api/articles?stage=new` (predicate `extracted_at IS NULL AND final_state IS NULL AND retry_count < MAX_RETRIES`) returns ZERO rows for that id. `GET /api/articles?final_state=discarded` returns it. |
| 13 | Auth single path | (a) Delete SKILLS/.env, run cowork-task-prompt bash; (b) grep -rE 'API_TOKEN=[a-f0-9]{16,}' SKILLS/ cowork-task-prompt.md | (a) Exits 1 with `[error] SKILLS/.env missing or unreadable`. (b) Zero matches. |
| 14 | Rate limit exemptions + leaky-bucket hourly (ISSUE-10, ISSUE-15, ISSUE-16, ISSUE-20) | (a) 100 heartbeats in 60s on /api/lock/*; (b) 1000 /api/health in 60s; (c) **Paced** 50 /api/articles/min for 20 min (1000 total) from one bearer | (a) Zero 429s on /lock. (b) Zero 429s on /health; `nginx -T \| awk '/location = \/api\/health/,/^[[:space:]]*}$/'` contains no `limit_req`. (c) Zero 429s attributed to tlinh_main in nginx error_log; at least 200 429s attributed to tlinh_hourly (paced 50/min < tlinh_main 100/min cap, but 50/min > tlinh_hourly 16.67/min drain rate so hourly trips after ~3 min). Every 429 has `Retry-After: 60` header. |
| 15A | Phase A reproducibility (ISSUE-5 + ISSUE-12 + ISSUE-18) | `bash scripts/deploy.sh` twice on same VPS | First run creates DNS, dirs (Step 2), uploads (Step 3), DB+schema (Step 4), .env (Step 5), nginx+cert (Step 6), health-check (Step 7), Mac client (Step 8), launchd (Step 9). Second run: every step short-circuits, exits 0. **`deploy.env` is NOT shredded by Phase A** — it persists on Mac disk so the operator can re-source it for Phase B (per ISSUE-17). Shredding happens at Step 14 after Phase B verifies Telegram. |
| 15C | deploy.env lifecycle (ISSUE-18) | After Phase A: `ls deploy.env` returns 600-perm file. After Step 14 of Phase B: `ls deploy.env` returns "No such file or directory". | deploy.env survives Phase A, is shredded only at Step 14. |
| 15B | Phase B is manual (ISSUE-12) | Inspect `scripts/deploy.sh` content | Script does NOT call Cowork UI, does NOT click "Run now", does NOT seed inject-test. Those are documented in §10 Steps 10–13 as manual. |
| 16 | DNS upsert + SSL guard | (a) bash scripts/dns_setup.sh twice; (b) try enable proxied with SSL mode='off' | (a) First creates, second PATCHes existing, no duplicate. (b) Refuses, exits 1 with `[error] Refusing to enable proxy with SSL mode 'off'`. |

---

## 14. Known limitations / deferred

- Mac-only client. Linux Mac alternative deferred.
- Single user, single topic. Multi-user requires per-user API tokens + per-user DB rows.
- Mac sleep → cron misses run (no catch-up). Acceptable for daily cadence.
- Cowork sandbox bash is Linux; can't auto-deploy from Cowork. Deploy is one-time manual via Mac Terminal.
- Telegram bot is per-chat. Group notifications work; multi-channel routing deferred.
- No web UI for browse/review; Telegram + Cowork chat + Terminal queries only.

---

## 15. Migration from v1

v1 codebase (`db.py`, `lock.py`, `notify.py`, `mark.py`, `list.py`, SQLite, BOOTSTRAP.md, setup.skill) becomes obsolete. Migration path:

1. **Archive v1** — `git mv` v1-only files to `archive/v1/` (preserves history).
2. **Drop tables** — `news.db` no longer used; delete or archive.
3. **Re-issue API token** — generate new bearer, update everywhere.
4. **Deploy v2** — follow runbook in §10.
5. **Verify** — run E2E matrix.

The PLAN.md (v1) is renamed to PLAN-v1.md and PLAN-v2.md becomes the active spec.

---

## 16. Open questions for user

1. **Cloudflare proxy:** keep proxied=false permanently (direct-to-origin, simpler), or flip to true later for CF DDoS protection (requires SSL mode `Full (strict)` since origin already has Let's Encrypt cert)?
2. **launchd schedule:** Daily at 7am ok, or different time/cadence?
3. **API token rotation:** manual for v1, or build automated rotation in v1.1?
4. **Backup strategy:** mariadb dump cron daily into `/var/backups/tlinh/`?
5. **Logging on VPS:** PHP error_log to nginx error.log (default), or dedicated `/var/log/tlinh/`?

For v1 implementation, recommended defaults:
1. Keep proxied=false initially; user can flip later
2. Daily 7am local
3. Manual rotation
4. Yes, daily dump
5. Dedicated /var/log/tlinh/
