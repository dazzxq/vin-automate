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
- **Brainstorm** 5 ideas per article via Cowork `/idea-brainstormer` slash skill, persisting result back to VPS.

**Out of scope (v2):**
- Multi-user, multi-topic isolation (v2 is single-user, single-topic VinFast)
- Web review UI
- Linux/Windows Mac client (macOS only for local crawler)
- VPS-side AI scoring (cost: would need OpenAI/Anthropic API key; instead reuse Max plan via Cowork)

**Runtime targets:**

- Mac client: macOS 13+, Python 3.11+ in `.venv`. Optional — only needed for crawl scheduling.
- VPS: Ubuntu 24.04 (existing 14.225.29.159), nginx, PHP 8.5+, MariaDB 10.11+, certbot.
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
│ /idea-brainstormer 42 slash skill (on-demand chat):                 │
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
- `final_state`: `discarded` | `archived` | `active`
- `since`: ISO date (default: 7 days ago)
- `last_hours`: int
- `search`: keyword (LIKE on title + content)
- `limit`: int (default 50, max 200)

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

**Phase 1 — Claim (transactional UPDATE, no external I/O):**
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
- `rowcount == 0` → another caller already claimed (or threshold/score/state fails). Return 200 no-op.

**Phase 2 — Send + finalize (after Phase 1 returns rowcount=1):**
1. Build HTML-escaped message from article fields.
2. POST `api.telegram.org/bot<TOKEN>/sendMessage` (with 429 Retry-After + 5xx backoff per §9).
3. On Telegram 200 OK:
   ```sql
   UPDATE articles
      SET notified_at = NOW(), telegram_msg_id = ?
    WHERE id = ?
      AND notify_claim_owner = ?  -- our claim
   ```
4. On Telegram permanent failure (4xx other than 429): release the claim so retry can succeed:
   ```sql
   UPDATE articles SET notify_claimed_at = NULL, notify_claim_owner = NULL
    WHERE id = ? AND notify_claim_owner = ?
   ```
   Then call `/api/articles/{id}/fail` with `stage='notify'` to bump retry_count.

**Stale claim recovery:** if a claim is older than `NOTIFY_CLAIM_TTL_SECONDS` (default 60s) AND `notified_at IS NULL`, the server can opportunistically clear it on the next claim attempt:
```sql
UPDATE articles SET notify_claimed_at = NULL, notify_claim_owner = NULL
 WHERE id = ? AND notify_claimed_at < NOW() - INTERVAL ? SECOND
   AND notified_at IS NULL
```
This handles the case where the server crashed between Phase 1 and Phase 2.

Request body: (none required)

Response 200 (sent):
```json
{"id": 42, "sent": true, "telegram_msg_id": 123, "notified_at": "2026-05-12T08:20:00"}
```

Response 200 (no-op — already notified or claim lost to concurrent caller):
```json
{"id": 42, "sent": false, "reason": "already_notified|already_claimed|below_threshold|no_score|discarded"}
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

Success-path clearing (Codex ISSUE-2): when a stage succeeds (PATCH /extract,
PATCH /score, POST /notify-finalize), the server clears `failed_at` and
`last_error` for that row (but keeps `retry_count` for observability).


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
- GET `/api/articles?stage=new` (rows where `extracted_at IS NULL`).
- For each row: fetch content via trafilatura/Jina.
- PATCH `/api/articles/{id}/extract` with content.

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
│   ├── idea-brainstormer.skill           (rewritten: HTTP-based)
│   └── (NO setup.skill — not needed in v2)
├── cowork-task-prompt.md                 (rewritten: HTTP-based scoring loop)
├── scoring-rubric.md                     (unchanged from v1)
└── brainstorm-guidelines.md              (unchanged from v1)
```

**`SKILLS/idea-brainstormer.skill` content:**
- Invocation: `/idea-brainstormer <id|filter>`
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
   - The `/idea-brainstormer` slash skill body.
5. The path `/path/to/project/SKILLS/.env` is the absolute path to the granted folder. The deploy runbook (§10) documents how to locate it for the user's specific Cowork project (typically `/Users/.../vin-automate-main/SKILLS/.env`).

Hardcoded tokens, environment frontmatter, or interactive prompts are FORBIDDEN — they reintroduce v1-class secret leakage risk.

### 5.4 DNS + HTTPS deployment (Cloudflare + certbot)

**DNS setup (automated via CF API):**
1. Create A record `tlinh.duyet.vn → 14.225.29.159`, `proxied=false` (gray cloud) for cert issuance.
2. After certbot succeeds and renewal works for 1 cycle, optionally set `proxied=true`.
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

**certbot:**
- `certbot --nginx -d tlinh.duyet.vn` (after DNS propagates with proxy off).
- Auto-renew via `systemd-timer` (already configured on this VPS for other certs).

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

### 7.4 Rate limiting (per Codex ISSUE-7 — exempt liveness + lock)

Three buckets in nginx, keyed on `Authorization` header (or `$remote_addr` for health):

| Bucket | Endpoints | Limit | Burst |
|---|---|---|---|
| `tlinh_health` | `/api/health` | 60 req/min | 30 |
| `tlinh_lock` | `/api/lock/*` (acquire, heartbeat, release) | 600 req/min | 100 |
| `tlinh_main` | everything else | 100 req/min | 20 |

**Rationale:** lock heartbeats run on every loop iteration; a 100-row score
batch could easily emit 10+ heartbeats. Sharing the main bucket would cause
spurious 429s on heartbeats, leading to lost lock ownership and aborted runs.
Health checks must NEVER 429 (monitoring).

Hard cap: 1000 req/hour combined per token (return 429 with `Retry-After: 60`).

**Caller behavior on 429:**
- `/api/lock/*` callers: retry with backoff (1s, 2s, 4s). Do NOT treat 429 as
  lock loss — it is transient. Max 3 retries before giving up + abort run.
- Other endpoints: retry once with 1s delay, then bubble up to caller.

nginx config snippet (will go in vhost):
```nginx
limit_req_zone $http_authorization zone=tlinh_main:10m rate=100r/m;
limit_req_zone $http_authorization zone=tlinh_lock:10m rate=600r/m;
limit_req_zone $binary_remote_addr  zone=tlinh_health:1m rate=60r/m;

location /api/health         { limit_req zone=tlinh_health burst=30 nodelay; ... }
location /api/lock/          { limit_req zone=tlinh_lock burst=100 nodelay; ... }
location /api/                { limit_req zone=tlinh_main burst=20  nodelay; ... }
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

### 8.2 Upsert A record (proxied=false for cert issuance)

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

### 8.3 Cert issuance via certbot

```bash
ssh vps-root "certbot --nginx -d ${FQDN} --non-interactive --agree-tos -m ${CF_EMAIL}"
# certbot edits the nginx vhost in place and reloads.
# Auto-renewal is handled by the existing system-wide systemd timer.
```

### 8.4 Optional: flip proxied=true (after cert + 1 renewal cycle)

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

Brainstorm: <code>/idea-brainstormer 42</code>
```

- HTML parse mode (`parse_mode=HTML`).
- `htmlspecialchars($title, ENT_QUOTES|ENT_HTML5, 'UTF-8')` on user-controlled fields.
- 1.2s sleep between sends (per-chat rate limit ~1/sec).
- 429 retry honoring `Retry-After` header.
- 5xx: exponential backoff 1s/2s/4s/8s/16s, max 5 attempts.

Bootstrap test rows (`bootstrap-test://<TOKEN>`) include the token in the title prominently so user can visually verify which test message they received.

---

## 10. Deployment runbook (one-time, reproducible)

**Per Codex ISSUE-5:** every step is run from a single local Mac shell session
that exports `deploy.env`. Run `bash scripts/deploy.sh` (Task 8) to execute
end-to-end automatically, OR follow the steps below manually.

All commands assume `set -euo pipefail`. Reruns are safe (every step is
idempotent — folder creation, DB user, DNS record, nginx vhost, certbot all
short-circuit if already done).

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

### Step 2 — Upload code + schema to VPS

```bash
# Rsync code (idempotent)
rsync -avz --delete --exclude='.git' --exclude='__pycache__' \
  vps/ "${VPS_SSH_ALIAS}":/var/www/tlinh/tlinh.duyet.vn/releases/v1/

# Upload schema.sql separately so Step 3 can find it
scp vps/schema.sql "${VPS_SSH_ALIAS}":/tmp/schema.sql
```

### Step 3 — VPS folder + DB user + schema (idempotent)

```bash
# Use ssh -T with heredoc and inject variables explicitly. NO bash here-string
# interpolation across the SSH boundary; instead pass via env -i + ssh's
# SendEnv/AcceptEnv or just inline via "ssh ... bash -s -- arg1 arg2".

ssh "${VPS_SSH_ALIAS}" "DB_PASS='${DB_PASS}' bash -s" << 'REMOTE'
set -euo pipefail
mkdir -p /var/www/tlinh/tlinh.duyet.vn/{releases/v1/public,releases/v1/src/routes,shared}
chown -R www-data:www-data /var/www/tlinh

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

### Step 4 — Write VPS `.env`

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
NOTIFY_CLAIM_TTL_SECONDS=60
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

### Step 5 — nginx vhost + Let's Encrypt cert (idempotent)

```bash
ssh "${VPS_SSH_ALIAS}" "FQDN='${FQDN}' CF_EMAIL='${CF_EMAIL}' bash -s" << 'REMOTE'
set -euo pipefail
VHOST=/etc/nginx/sites-available/"${FQDN}"

# Write only if missing or differs (idempotent)
TMPF=$(mktemp)
cat > "${TMPF}" <<NGINX
limit_req_zone \$http_authorization zone=tlinh_main:10m   rate=100r/m;
limit_req_zone \$http_authorization zone=tlinh_lock:10m   rate=600r/m;
limit_req_zone \$binary_remote_addr  zone=tlinh_health:1m rate=60r/m;

server {
    listen 80;
    server_name ${FQDN};
    root /var/www/tlinh/${FQDN}/current/public;
    index index.php;
    client_max_body_size 5M;

    location /api/health {
        limit_req zone=tlinh_health burst=30 nodelay;
        try_files \$uri /index.php?\$query_string;
    }
    location /api/lock/ {
        limit_req zone=tlinh_lock burst=100 nodelay;
        try_files \$uri /index.php?\$query_string;
    }
    location /api/ {
        limit_req zone=tlinh_main burst=20 nodelay;
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

if ! cmp -s "${TMPF}" "${VHOST}" 2>/dev/null; then
  mv "${TMPF}" "${VHOST}"
  ln -sfn "${VHOST}" /etc/nginx/sites-enabled/"${FQDN}"
  nginx -t && systemctl reload nginx
else
  rm -f "${TMPF}"
fi

# Cert: only issue if not present
if [ ! -d /etc/letsencrypt/live/"${FQDN}" ]; then
  certbot --nginx -d "${FQDN}" --non-interactive --agree-tos -m "${CF_EMAIL}"
fi
REMOTE
```

### Step 6 — Health check

```bash
curl -fsSL "https://${FQDN}/api/health"
# Expected: {"status":"ok","db":"connected","version":"v2-..."}
```

### Step 7 — Mac client

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

### Step 8 — Cowork-side `SKILLS/.env`

```bash
# Cowork sources this file as the only API_TOKEN path (see §5.3 mandate).
# Same token as Mac, separate file for blast-radius isolation.
cat > SKILLS/.env <<EOF
API_BASE_URL=https://${FQDN}
API_TOKEN=${API_TOKEN}
EOF
chmod 600 SKILLS/.env
echo 'SKILLS/.env' >> .gitignore  # ensure ignored

# In Claude Desktop:
# 1. Create Cowork project pointing at this folder.
# 2. Grant bash + network "Allow all".
# 3. Open Cowork chat, paste `cowork-task-prompt.md` content into `/schedule` UI.
#    Set frequency: Daily. Name: vinfast-pipeline. Save.
# 4. Click "Run now" to verify end-to-end. Telegram receives test message.
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

### Step 10 — Delete `deploy.env` (cleanup)

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
| 8 | Deploy script + nginx vhost template (idempotent end-to-end) | `scripts/deploy.sh`, `vps/nginx-tlinh.conf.tpl` | `bash scripts/deploy.sh` succeeds twice in a row without errors; second run reports all steps no-op; secrets cleaned up |
| 8a | **Telegram nonce helper** | `scripts/telegram-nonce-helper.sh` | Collects bot token + chat_id via deterministic nonce flow; writes to `deploy.env` |
| 9 | DNS automation (idempotent upsert + SSL mode guard per ISSUE-6) | `scripts/dns_setup.sh` | First run creates record; second run no-op or PATCH; refuses to enable proxy if CF SSL mode != strict/full |
| 10 | Mac `api_client.py` (httpx wrapper + retry + 429 handling per ISSUE-7) | `api_client.py` | Unit test: round-trip POST + GET works on staging VPS; 429 retry with backoff |
| 11 | Mac `crawl.py` rewrite (HTTP-based) | `crawl.py` | `python crawl.py --dry-run` reports counts; real run inserts via API; same url_hash as VPS computes |
| 12 | Mac `extract.py` rewrite (HTTP-based, two-level retry, calls /fail on exhaust) | `extract.py` | Extracts rows where `extracted_at IS NULL`; on transient exhaustion calls POST /fail with stage=extract |
| 13 | **Cowork scheduled task prompt** (single auth path per ISSUE-4, exempts /lock from 429 per ISSUE-7) | `cowork-task-prompt.md` | First line sources SKILLS/.env or hard-exits; heartbeat handling respects 429 retry-after; tolerates 429 on lock endpoints without aborting |
| 14 | **Cowork brainstorm skill** (single auth path) | `SKILLS/idea-brainstormer.skill` | `/idea-brainstormer 42` produces 5 ideas, written via PATCH; sources SKILLS/.env |
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

### AC #4 — /idea-brainstormer skill writes brainstorm

In Cowork chat, `/idea-brainstormer 42` reads article via `/api/articles/42`, generates 5 ideas, PATCHes via `/api/articles/42/brainstorm`. DB row has `brainstormed_at NOT NULL` and `ideas` JSON of length 5.

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

### AC #5b — Stale claim recovery

If a claim row has `notify_claimed_at < NOW() - NOTIFY_CLAIM_TTL_SECONDS` (default 60s) AND `notified_at IS NULL`, the next `POST /notify/{id}` clears the stale claim opportunistically and proceeds to Phase 1 normally. Verified by `INSERT INTO articles ... notify_claimed_at = NOW() - INTERVAL 120 SECOND` then triggering `POST /notify/{id}` and confirming new claim succeeds.

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

### AC #11 — Bootstrap verification flow

1. `POST /api/admin/inject-test {"token": "abc123def456"}` from localhost on VPS (per ISSUE-3 spec).
2. Click "Run now" on Cowork scheduled task.
3. Within 10 min, Telegram receives message containing `abc123def456` literally (HTML-escaped title).
4. DB: row with `url=bootstrap-test://abc123def456` has `notified_at NOT NULL`.
5. Re-running `POST /api/admin/inject-test` with same token: returns existing row id (idempotent, no duplicate).

### AC #12 — Fail/retry semantics (per ISSUE-2)

1. Trigger extract on an unreachable URL → `extract.py` retries in-process up to `MAX_RETRIES` then calls `POST /api/articles/{id}/fail` with `stage=extract`.
2. DB: `retry_count = 1`, `failed_at` set, `last_error` populated.
3. Next scheduled run picks up the same row (because `extracted_at IS NULL` and `retry_count < MAX_RETRIES`); fails again → `retry_count = 2`.
4. After `MAX_RETRIES` failed runs: `final_state = 'discarded'`. Row never re-attempted.
5. Manual recovery: `mark.py archive <id>` or direct SQL.

### AC #13 — Single auth path enforced (per ISSUE-4)

- `SKILLS/.env` missing → Cowork bash invocation exits 1 with explicit `[error] SKILLS/.env missing or unreadable` message.
- Hardcoded `API_TOKEN` in any committed skill file or task prompt → CI/manual grep `grep -rE 'API_TOKEN=[a-f0-9]{16,}'` returns zero matches.
- `SKILLS/.env` is in `.gitignore`. Verified by `git check-ignore SKILLS/.env`.

### AC #14 — Rate limiting does not break heartbeat (per ISSUE-7)

- Send 100+ heartbeats in 60 seconds. None receive 429.
- Verify by simulating a long-running score loop with `HEARTBEAT_EVERY_N_ROWS=1` (one heartbeat per row) over 100 rows in 60s.
- `/api/health` always 200 regardless of token activity.

### AC #15 — Deploy runbook is reproducible (per ISSUE-5)

- Fresh VPS (or after `rm -rf /var/www/tlinh`): `bash scripts/deploy.sh` completes end-to-end with no manual intervention.
- Second run of `bash scripts/deploy.sh`: every step short-circuits as "already done". Exits 0.
- `deploy.env` is deleted at end via `shred -u`.

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
| 4 | Brainstorm skill | `/idea-brainstormer 42` in Cowork chat | DB row 42: brainstormed_at + ideas[5] populated. User sees ideas table. |
| 5 | Concurrent acquire | Two shell sessions both call `/api/lock/pipeline-run/acquire` | 2nd → 409. Only 1 row in locks table. |
| 6 | Auth check | `curl /api/articles` (no bearer), then with correct | 401, then 200. |
| 7 | Idempotent score | Two PATCH score on same id | 2nd: updated=false. DB unchanged. |
| 8 | Priority sort | Inject bootstrap-test row + 50 real rows | GET articles returns bootstrap-test first. |
| 9 | TTL reclaim | acquire ttl=1, sleep 2, acquire | 2nd succeeds, new owner_id, locks count = 1. |
| 10 | HTML escape | Inject article with title `<b>&*_[]</b>` | Telegram receives 200. Visual: tag rendered as text. |
| 11 | Bootstrap end-to-end | Inject `bootstrap-test://abc123def456` + Run now Cowork | Telegram message contains "abc123def456" within 10 min. notified_at set. Re-inject same token → existing row id returned, no duplicate. |
| 5a | Notify 2-phase under failure | Force Telegram 400 (e.g. bad chat_id env override): POST /notify/{id} | Phase 1 claims, Telegram fails 400 → claim released (`notify_claimed_at=NULL`), /fail called (retry_count++), HTTP 502 returned to caller. Subsequent POST succeeds Phase 1 again. |
| 5b | Stale claim recovery | `INSERT INTO articles ... notify_claimed_at = NOW() - INTERVAL 120 SECOND, notify_claim_owner='abc'`; then POST /notify/{id} | Stale claim cleared opportunistically. New claim succeeds. notified_at populated. |
| 12 | Fail/retry → discard | Mock httpx transient error N+1 times; trigger N extract runs | After MAX_RETRIES (default 3) failed runs: row's final_state='discarded'. Next extract run skips this row (filter `final_state IS NULL`). |
| 13 | Auth single path | (a) Delete SKILLS/.env, run cowork-task-prompt bash; (b) grep -rE 'API_TOKEN=[a-f0-9]{16,}' SKILLS/ cowork-task-prompt.md | (a) Exits 1 with `[error] SKILLS/.env missing or unreadable`. (b) Zero matches. |
| 14 | Rate limit exemptions | 100 heartbeats in 60s; 100 /api/health in 60s | Zero 429s on /lock and /health. Main bucket: bursts up to 20, then 429 with Retry-After. |
| 15 | Deploy reproducibility | `bash scripts/deploy.sh` twice on same VPS | First run creates everything. Second run: every step short-circuits, exits 0. `deploy.env` shredded at end. |
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

1. **Cloudflare proxy:** keep proxied=false permanently (simpler), or flip to true after cert issuance (CF protection)?
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
