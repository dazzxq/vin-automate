# News Aggregation Pipeline — Implementation Plan

## 1. Goal & Scope

Build a local-first news aggregation pipeline that:
- Crawls news on configurable topics (initial: VinFast) from Google News RSS + curated RSS sources.
- Extracts article content (trafilatura primary, Jina Reader fallback).
- **Claude in Cowork** scores each article 1–5 against a tunable rubric (no API, no subprocess — Cowork IS the runtime).
- Pushes high-score articles to Telegram for human review.
- On-demand inside Cowork via slash skill `/idea-brainstormer <id>`, Claude brainstorms 5 story ideas per article.
- Persists all state in SQLite. Runs daily via **Claude Cowork scheduled tasks** on macOS.

**Out of scope:** Social platforms (FB, TikTok), multi-user, cloud deployment, real-time push, automated publishing.

**Runtime target:** Python 3.11+ (tool CLIs only), Claude Desktop app with Cowork enabled (Pro/Max plan), macOS 13+ (Ventura or later).

**User profile assumption:** non-technical. No command-line skills required. **Primary onboarding** is conversational via pasting `BOOTSTRAP.md` into a Cowork chat (§2.1.1); `/setup` slash skill exists for post-bootstrap reconfiguration only; Terminal (`bash install.sh && python3 setup_helper.py`) is a fallback path.

---

## 2. Architecture

The runtime is **Claude in Cowork**, not a Python orchestrator. Python provides deterministic tool helpers (crawl, extract, DB writes, Telegram). Claude does the AI reasoning (scoring, brainstorming) inside its agentic session. Cowork's `/schedule` is the scheduler — there is no `launchd`, no `claude -p` subprocess, no Anthropic SDK.

```
Claude Cowork scheduled task (daily, 24h)
  │ (Claude opens the project folder, fresh session)
  ├─ Read cowork-task-prompt.md          ── orchestration brief (uses <OWNER_ID> placeholder)
  ├─ Read scoring-rubric.md              ── 1–5 thang điểm
  │
  ├─ Bash: python lock.py acquire pipeline-run || exit
  │     - SQLite mutex (§4.8); prints owner_id (UUID) to stdout
  │     - exits 75 if held → Cowork aborts (lock not held, no release needed)
  │  ▶ Claude RECORDS the UUID from stdout into its session memory (§4.8.3). Henceforth referenced as <OWNER_ID>.
  │
  ├─ FOR EACH tool step — see §4.8.4 abort taxonomy:
  │     ├─ Run tool
  │     ├─ If tool fails (exit ≠ 0, not a CAS no-op):
  │     │     ├─ If failure was `lock.py heartbeat` exit 75 (LOST OWNERSHIP)
  │     │     │     → abort run, do NOT call release
  │     │     └─ Else (we still own the lock)
  │     │           → Bash: python lock.py release pipeline-run <OWNER_ID>, then abort
  │     └─ Else continue
  │
  ├─ Bash: python crawl.py
  ├─ Bash: python lock.py heartbeat pipeline-run <OWNER_ID>
  ├─ Bash: python extract.py --pending
  ├─ Bash: python lock.py heartbeat pipeline-run <OWNER_ID>
  ├─ Bash: python list.py --stage extracted --not-scored --json
  │     → JSON array, length ≤ MAX_ARTICLES_PER_RUN
  ├─ FOR EACH row:
  │     ├─ Claude applies rubric → (score, reason)
  │     ├─ Bash: python mark.py score <id> <N> "<reason>"   ── CAS; no-op if already scored
  │     └─ Every HEARTBEAT_EVERY_N_ROWS rows: python lock.py heartbeat pipeline-run <OWNER_ID>
  ├─ Bash: python lock.py heartbeat pipeline-run <OWNER_ID>
  ├─ Bash: python list.py --stage scored --min-score 3 --not-notified --json
  ├─ FOR EACH row:
  │     └─ Bash: python notify.py <id>   ── Telegram HTML → CAS sets notified_at
  └─ Bash: python lock.py release pipeline-run <OWNER_ID>   ── success path; literal UUID

On-demand (user invokes /idea-brainstormer skill in any Cowork chat):
  ├─ User: /idea-brainstormer 42   (or /idea-brainstormer with no args / search keyword)
  ├─ Skill resolves args → list.py flags:
  │     - numeric → --id <N>
  │     - no args → --not-brainstormed --today --score-gte 3
  │     - "kw"   → --search "kw"
  ├─ Bash: python list.py <flags> --json
  ├─ Multiple candidates → present table, ask user to pick id
  ├─ Read brainstorm-guidelines.md
  ├─ Bash: python list.py --id <chosen> --json (full content)
  ├─ Claude generates 5 ideas (JSON)
  └─ Bash: python mark.py brainstorm <ID> '<json>'
```

**Why this shape:**
- Python tools = deterministic, version-controlled, unit-testable. **No AI inside any Python file.**
- Claude in Cowork = AI reasoning + orchestration. No retry/backoff for Claude's own thinking; only for tool HTTP calls.
- Splitting AI from tools means tuning the rubric is a markdown edit, not a code change. The Python pipeline never breaks when you refine scoring criteria.

### 2.1 Cowork Setup & Permissions

**Target user profile: non-technical.** No command-line knowledge required. All setup is driven by Cowork chat.

**Primary onboarding path (handles the slash-skill chicken-and-egg per ISSUE-1):** The user does NOT need `/setup` skill discovered first. Instead the repo ships `BOOTSTRAP.md` in the root. The user:
1. Opens Claude Desktop with a Pro/Max plan, adds the project folder to Cowork's trusted folders.
2. Opens `BOOTSTRAP.md`, copies its entire content, pastes into any Cowork chat.
3. Claude follows the pasted instructions: runs `install.sh`, gathers Telegram credentials, **installs both skills into Cowork's actual skill discovery path** (so future invocations of `/setup` and `/idea-brainstormer` work), then VERIFIES the scheduled task by pre-arming a uniquely-tokenized test row, asking user to "Run now" on the saved scheduled task, and confirming the tokenized notification arrives in Telegram (per §2.1.1 Step 5).

After BOOTSTRAP.md runs successfully ONCE, the slash skills are available; user types `/idea-brainstormer 42` directly from then on. The `/setup` slash skill exists as a CONVENIENCE for re-running setup after the initial bootstrap (config changes, replacing Telegram credentials, etc.).

Cowork is assumed to run with **allow-all bash** mode (user grants this once) and **allow-all network** (network egress not gated). With those, the entire setup happens inside the chat.

Permissions required:
- Bash tool: required (for `python *.py`, `brew install`, `curl` setup probes).
- Read/Write tools: required (rubric files, logs, `.env` writes).
- Network: all domains (Homebrew CDN, PyPI, Google News, Telegram, Jina).
- No MCP servers required for v1.

Schedule (created at the end of the BOOTSTRAP.md flow — §2.1.1 Step 5): `/schedule` → **"daily"** → paste `cowork-task-prompt.md` content as the task prompt. BOOTSTRAP.md shows the content inline and instructs the user to copy-paste, since Cowork's scheduled-task UI is not bash-controllable. Verification is mandatory via "Run now" on the saved task (see §2.1.1 Step 5 + ISSUE-6 fix).

### 2.1.1 One-time onboarding flow (paste `BOOTSTRAP.md` into Cowork chat)

The bootstrap flow is **a single pasted prompt**, not a slash skill (slash skill comes AFTER bootstrap registers it). User pastes the entire content of `BOOTSTRAP.md` (Task 14, repo root) into a Cowork chat. Claude follows it step-by-step.

```
[paste BOOTSTRAP.md into Cowork chat]
  │
  ├─ Step 1: OS + arch check
  │     ├─ Bash: uname -s → "Darwin" required
  │     ├─ Bash: sw_vers -productVersion → require 13+
  │     ├─ Bash: uname -m → "arm64" or "x86_64" (records BREW_PREFIX = /opt/homebrew vs /usr/local)
  │     └─ Non-macOS / <13 → show fallback (see §2.1.2) and exit
  │
  ├─ Step 2: Run install.sh — hardened per Task 1
  │     ├─ Bash: bash install.sh
  │     │     install.sh internally:
  │     │       - if Homebrew missing → NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL .../install.sh)"
  │     │         (NONINTERACTIVE=1 suppresses the "press RETURN" prompt; ISSUE-2)
  │     │       - after install: explicit `eval "$($BREW_PREFIX/bin/brew shellenv)"` in current shell (ISSUE-2)
  │     │       - command -v python3.11 || brew install python@3.11
  │     │       - python3.11 -m venv .venv ; .venv/bin/pip install -r requirements.txt
  │     │       - mkdir -p logs ; from db import init_db; init_db()
  │     │       - VERBOSE [check]/[install]/[ok]/[warn]/[error] tagged log lines
  │     │       - If sudo prompt detected (Xcode CLT) → exit code 78 with [error] msg
  │     └─ Claude parses output; on exit 78 → show Terminal fallback (§2.1.2) and stop
  │
  ├─ Step 3: Telegram credentials — deterministic single-session nonce flow (ISSUE-5, ISSUE-7 corrected)
  │     ├─ If .env exists with valid token (validate via curl getMe) AND chat_id (validate via test send) → skip to Step 4
  │     ├─ Else:
  │     │   ├─ Claude shows Vietnamese BotFather walkthrough
  │     │   │   "Mở https://t.me/BotFather, gõ /newbot, đặt tên 'VinFast News Bot', copy token gửi tới bạn"
  │     │   ├─ User pastes token; Claude validates: curl https://api.telegram.org/bot<TOKEN>/getMe
  │     │   │   On 401 → re-prompt up to 3 times then exit with help link
  │     │   ├─ **CORRECT deterministic chat_id detection (replaces the broken offset=-1 polling):**
  │     │   │   STEP 3.1 — Establish baseline: 
  │     │   │     resp = curl ".../getUpdates?timeout=0&limit=100"
  │     │   │     baseline_update_id = max(u.update_id for u in resp.result, default=0)
  │     │   │   STEP 3.2 — Generate nonce: e.g. "VF-7K3M-2A8X" (random, unique per attempt)
  │     │   │   STEP 3.3 — Tell user: "Mở Telegram, gửi tin nhắn này tới bot của bạn: VF-7K3M-2A8X"
  │     │   │   STEP 3.4 — Bounded poll loop (deadline = now + 120s):
  │     │   │     while time.time() < deadline:
  │     │   │       resp = curl ".../getUpdates?offset={baseline_update_id+1}&timeout=10&limit=100"
  │     │   │       # offset=baseline+1 fetches ALL updates newer than baseline, repeatedly until they're consumed
  │     │   │       for u in resp.result:
  │     │   │         msg_text = u.get("message", {}).get("text", "")
  │     │   │         if msg_text.strip() == NONCE:
  │     │   │           chat_id = u["message"]["chat"]["id"]
  │     │   │           chat_name = u["message"]["chat"].get("first_name") or u["message"]["chat"].get("title")
  │     │   │           return (chat_id, chat_name)
  │     │   │       time.sleep(2)
  │     │   │     raise TimeoutError("Nonce not received within 120s")
  │     │   │   STEP 3.5 — Network/error handling:
  │     │   │     - On HTTP 5xx from Telegram: retry with backoff (max 3 attempts), then surface error.
  │     │   │     - On HTTP 401: token revoked mid-flow → re-prompt for token.
  │     │   │     - On timeout: ask user "Đã gửi nonce chưa? Bạn có gửi vào đúng bot không?" + retry with NEW nonce.
  │     │   │   STEP 3.6 — User confirms: "Đã phát hiện chat: {chat_name} ({chat_id}) — đây có phải chat của bạn? [y/N]"
  │     │   │     On y → continue. On N → start over with new nonce.
  │     │   ├─ Test send: curl sendMessage chat_id={chat_id} text="Setup OK — VinFast pipeline ready"
  │     │   │   - If 200 OK → user confirms "Bạn đã nhận tin nhắn này trong Telegram? [y/N]"
  │     │   │   - On y → write .env atomically; on N → diagnose (wrong chat? bot blocked?) + restart Step 3
  │     │   └─ Atomic write .env (write .env.tmp then mv)
  │
  │     **BOOTSTRAP.md and setup_helper.py MUST implement the EXACT same algorithm.** Single source of correctness — both must use baseline_update_id + offset-based polling, NOT offset=-1.
  │
  ├─ Step 4: Install slash skills into Cowork skill discovery path (ISSUE-1)
  │     ├─ Bash: detect Cowork skill dir (README documents exact location — to be confirmed empirically):
  │     │   Likely candidates on macOS:
  │     │     - ~/Library/Application Support/Claude/skills/
  │     │     - ~/.claude/skills/
  │     │   install.sh probes for the correct one or symlinks both.
  │     ├─ Bash: mkdir -p "$SKILL_DIR/idea-brainstormer" "$SKILL_DIR/setup"
  │     ├─ Bash: ln -sf "$PWD/SKILLS/idea-brainstormer.skill" "$SKILL_DIR/idea-brainstormer/SKILL.md"
  │     ├─ Bash: ln -sf "$PWD/SKILLS/setup.skill" "$SKILL_DIR/setup/SKILL.md"
  │     ├─ Verify discoverable: ask user to type "/idea" in a NEW chat — Cowork should autocomplete
  │     │   to /idea-brainstormer. If not, show README §"Skill install troubleshooting".
  │     └─ Log [ok] both skills installed.
  │
  ├─ Step 5: Schedule the Cowork task with REAL scheduler verification (ISSUE-6/8/10/11/17 corrected)
  │     ├─ Bash: cat cowork-task-prompt.md → output displayed inline
  │     ├─ **Idempotency check (ISSUE-17)**: Claude asks user: 
  │     │   "Mở sidebar Cowork → Scheduled. Bạn có thấy task tên `vinfast-pipeline` nào đã tồn tại không? [y/N]"
  │     │   - On **N** (first-run): "Click '+' New Task → Name: vinfast-pipeline; Frequency: Daily; 
  │     │     Paste nội dung ở trên vào prompt; Save."
  │     │   - On **y** (reconfiguration): "Click vào task `vinfast-pipeline` hiện có → Edit → 
  │     │     **Verify the prompt body matches the content above; if it differs, REPLACE the prompt body 
  │     │     entirely** (do NOT create a duplicate task); Save."
  │     │   - **If user reports MULTIPLE tasks named `vinfast-pipeline`**: ask user to delete the older 
  │     │     duplicates from sidebar before continuing (only one task should remain). Claude reminds: 
  │     │     "Cowork không tự dedup; xóa duplicate để tránh chạy 2 lần mỗi ngày."
  │     │
  │     ├─ STEP 5.1 — Pre-arm with a uniquely-tokenized test row (ISSUE-10 priority + ISSUE-11 attribution):
  │     │   - Bash: TOKEN=$(python -c 'import secrets; print(secrets.token_hex(6))')  # e.g. "a7f3e1b2c4d5"
  │     │   - Bash: **TEST_BEFORE_TS=$(date +%s)**  # epoch seconds; reference for Step 5.3 Phase A
  │     │   - Bash: python crawl.py --inject-test "$TOKEN"
  │     │     Inserts ONE row with: url="bootstrap-test://<TOKEN>" (RESERVED scheme — per Task 4),
  │     │     title="[BOOTSTRAP TEST <TOKEN>] sample article", source='bootstrap-inject' (label only),
  │     │     extracted_at=now, **scored_at=now, score=5, score_reason='bootstrap verification'**.
  │     │     Row is immediately notify-eligible. list.py ORDER BY priority (`url LIKE 'bootstrap-test://%'`)
  │     │     ensures verification rows come FIRST regardless of backlog (ISSUE-10/15).
  │     │
  │     ├─ STEP 5.2 — User triggers the SAVED scheduled task:
  │     │   "Mở sidebar Cowork → click vào task 'vinfast-pipeline' bạn vừa save → bấm 'Run now'."
  │     │   (Wait time clarified in 5.3 — adaptive, not a fixed 3-min cap.)
  │     │
  │     ├─ STEP 5.3 — Two-phase adaptive poll (ISSUE-14: fixed 180s too tight for slow first-run):
  │     │   PHASE A — Startup detection (60s window):
  │     │     while time.time() - TEST_BEFORE_TS < 60:
  │     │       grep "lock.py acquire pipeline-run" logs/pipeline.log → check for a log entry whose
  │     │         timestamp > TEST_BEFORE_TS
  │     │       if found: STARTUP_DETECTED = true; record startup_ts; break
  │     │       sleep 5s
  │     │     If STARTUP not detected in 60s → ask user "Đã bấm 'Run now' chưa? Task có hiện trong sidebar?"
  │     │     → diagnose (task name correct? sidebar refresh? Cowork session active?) + retry STEP 5.2.
  │     │
  │     │   PHASE B — Completion wait (10 minutes from startup_ts):
  │     │     while time.time() - startup_ts < 600:
  │     │       row = sqlite3 query: SELECT notified_at, telegram_msg_id FROM articles 
  │     │                            WHERE url='bootstrap-test://<TOKEN>'
  │     │       if row.notified_at IS NOT NULL: → SCHEDULER VERIFIED
  │     │       sleep 10s
  │     │     10-min headroom covers slow first-run (large backlog extract, network lag). With the row 
  │     │     pre-scored + priority-sorted FIRST in the notify pass, normal completion is <30s.
  │     │
  │     │   Note on attribution: signal is uniquely attributable because the row has a unique TOKEN that
  │     │   no concurrent pipeline run can produce. Even if the scheduled run fires DURING the wait,
  │     │   only a run that processes our specific tokenized row can flip its notified_at — and any such
  │     │   run must be using a prompt body that orchestrates the pipeline correctly.
  │     │
  │     ├─ STEP 5.4 — Confirm with user: "Bạn vừa nhận tin nhắn Telegram chứa chuỗi '<TOKEN>' không? [y/N]"
  │     │   (User looks for the token string in the received Telegram message to confirm it's THIS test, 
  │     │    not a stray message.)
  │     │   - On y AND notified_at populated within the adaptive window (Phase A 60s + Phase B 600s from startup_ts) → SCHEDULER VERIFIED.
  │     │   - On N OR timeout:
  │     │     - Diagnose: ask user to screenshot the saved task's prompt body → verify it matches 
  │     │       cowork-task-prompt.md (no truncation, no empty body).
  │     │     - If prompt body wrong: re-paste + re-save, then redo Step 5.1 with a NEW token and Step 5.2-5.4.
  │     │     - If Telegram never arrived: verify chat_id, bot blocked, network.
  │     │   - Setup does NOT declare success until user confirms receipt of the specific token AND DB shows 
  │     │     the tokenized row was notified.
  │
  ├─ Step 6: Completion
  │     ├─ "Setup done + verified end-to-end."
  │     ├─ "Cowork sẽ tự chạy mỗi ngày khi Claude Desktop còn mở."
  │     ├─ "Brainstorm: /idea-brainstormer <id> (slash skill installed at Step 4)"
  │     └─ "Để re-config sau: /setup hoặc paste BOOTSTRAP.md lại."
```

### 2.1.2 Fallback: Terminal-driven setup (when Cowork can't run BOOTSTRAP.md)

Triggers: macOS sudo prompt blocks install.sh (exit 78); allow-all bash not granted; user opens repo in Cowork but skills don't auto-discover even after install; Claude Desktop session crashed mid-flow.

Repo ships `setup_helper.py` — a **Python TUI** the user runs in Terminal. It does everything BOOTSTRAP.md would do except the scheduled-task setup (still requires Cowork UI):

```bash
cd /Users/<you>/Documents/Code/vin-automate
bash install.sh        # auto-installs deps if needed
python3 setup_helper.py   # interactive: validates token, detects chat_id via nonce, writes .env, installs skills
```

`setup_helper.py` MUST:
- Use plain `input()` prompts with clear Vietnamese instructions.
- Implement the same deterministic chat_id flow (nonce + getUpdates poll) as Step 3.
- Write `.env` atomically.
- Install slash skills (same as Step 4).
- Print a final success summary AND show the user the cowork-task-prompt.md content to paste into Cowork UI.

After running `setup_helper.py`, the user opens Cowork manually and pastes cowork-task-prompt.md into `/schedule`. No further Terminal work needed.

This fallback ensures **no non-technical user is stranded** by Cowork limitations.

### 2.2 `--dry-run` Mode (on `crawl.py`)

`crawl.py --dry-run` is the only dry-run mode (used to validate RSS + redirect resolution without writes). Behavior:
- Opens `news.db` in SQLite read-only URI mode for dedup checks.
- If DB is missing OR `articles` table missing → treat as empty catalog. Do NOT create schema.
- Performs RSS fetch + redirect resolution.
- Skips ALL writes: no INSERT.
- Console summary: `N candidates, M would-be-inserted, K already-seen`.

Schema init is a **setup-time** action (Task 3 / `install.sh`), never a runtime action.

### 2.3 File Layout

**Python tools (deterministic, no AI):**
- `crawl.py` — collect URLs, resolve canonical (per §4.5), insert skeleton rows
- `extract.py` — fetch content for rows where `extracted_at IS NULL`
- `mark.py` — CLI to write per-stage timestamps (`score`, `brainstorm`, `discard`, etc.). Uses **compare-and-set SQL** (§4.8).
- `notify.py` — push article to Telegram, set `notified_at`. Uses **compare-and-set SQL** (§4.8).
- `list.py` — query rows by stage; `--json` output for Claude. Default `--limit` = `MAX_ARTICLES_PER_RUN` from config.
- `lock.py` — `lock.py acquire <name>` / `lock.py heartbeat <name> <owner_id>` / `lock.py release <name> <owner_id>` — **SQLite-based run-level mutex with TTL** (see §4.8 + §4.8.1 + §4.8.2). Used by the Cowork task prompt to wrap and heartbeat an entire scheduled run.
- `db.py` — schema, connection helpers, URL/title canonicalization helpers
- `config.py` — load env, expose topics + thresholds
- `install.sh` — venv, deps, schema init, sanity checks

**AI orchestration (Claude reads these at runtime):**
- `cowork-task-prompt.md` — paste this into Cowork's `/schedule` UI (the scheduled daily run)
- `SKILLS/setup.skill` — **post-bootstrap reconfiguration skill** at `/Users/theduyet/Documents/Code/vin-automate/SKILLS/setup.skill`. Invoked via `/setup` AFTER BOOTSTRAP.md has installed it. Re-runs the §2.1.1 flow idempotently for reconfiguration (new Telegram bot, new chat, refresh skill symlinks, re-verify scheduler). NOT the primary onboarding path — BOOTSTRAP.md is.
- `SKILLS/idea-brainstormer.skill` — Cowork **slash-command skill** at `/Users/theduyet/Documents/Code/vin-automate/SKILLS/idea-brainstormer.skill`. Invoked from any Cowork chat via `/idea-brainstormer [ID|filter]`. Loads `brainstorm-guidelines.md`, calls `list.py` to get article(s) from DB (NOT from a snapshot file — DB is source of truth), generates 5-idea JSON, writes via `mark.py brainstorm`.
- `scoring-rubric.md` — 1–5 thang điểm with examples
- `brainstorm-guidelines.md` — 5-idea format spec (JSON shape + field semantics), git-tracked, read by the skill at runtime

**Onboarding (non-tech UX):**
- `BOOTSTRAP.md` — pasteable prompt at repo root. PRIMARY onboarding path: user copies into Cowork chat once. Resolves the slash-skill chicken-and-egg (§2.1.1). Installs the slash skills as part of its flow so subsequent invocations of `/setup` and `/idea-brainstormer` work.
- `setup_helper.py` — Terminal-fallback Python TUI for when Cowork can't run BOOTSTRAP.md (sudo, allow-all-bash denied, etc.). Implements the same credential collection + skill installation logic in interactive `input()` form.

**Operational:**
- `.env` (gitignored, Telegram token + chat ID only — no Anthropic key)
- `.env.example` (template, committed)
- `logs/` (gitkeeped, rotated)
- `news.db` (gitignored)

---

## 3. Database Schema & State Model

### 3.1 Schema

Article lifecycle has **orthogonal facts** (extracted? scored? notified? brainstormed?) that cannot be collapsed into a single `status` enum without losing information (e.g. an article that has been both notified and brainstormed). The schema uses **per-stage timestamps** as the source of truth and reserves a `final_state` enum only for terminal/manual states.

```sql
CREATE TABLE articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT UNIQUE NOT NULL,        -- original URL as seen by crawler
    canonical_url   TEXT,                        -- resolved final URL after redirect
    url_hash        TEXT UNIQUE NOT NULL,        -- MD5 of canonical_url
    title           TEXT,
    title_hash      TEXT,                        -- normalized title hash for fuzzy cross-source dedup
    source          TEXT,
    content         TEXT,
    published_at    TEXT,
    crawled_at      TEXT NOT NULL,

    -- per-stage timestamps: NULL = stage not yet completed
    extracted_at    TEXT,
    scored_at       TEXT,
    notified_at     TEXT,
    brainstormed_at TEXT,
    failed_at       TEXT,                        -- last failure timestamp

    score           INTEGER,                     -- NULL = not scored; 1-5 once scored
    score_reason    TEXT,
    ideas           TEXT,                        -- JSON array, set when brainstormed
    telegram_msg_id INTEGER,
    last_error      TEXT,
    retry_count     INTEGER DEFAULT 0,

    final_state     TEXT                         -- NULL = active; 'discarded' | 'archived'
);

CREATE INDEX idx_extracted_at    ON articles(extracted_at);
CREATE INDEX idx_scored_at       ON articles(scored_at);
CREATE INDEX idx_notified_at     ON articles(notified_at);
CREATE INDEX idx_brainstormed_at ON articles(brainstormed_at);
CREATE INDEX idx_failed_at       ON articles(failed_at);
CREATE INDEX idx_score           ON articles(score);
CREATE INDEX idx_title_hash      ON articles(title_hash);
CREATE INDEX idx_crawled_at      ON articles(crawled_at);
CREATE INDEX idx_final_state     ON articles(final_state);
```

### 3.2 State Transitions

A **skeleton row is inserted as soon as a candidate URL is accepted**, BEFORE extraction, so all failures (including extract failures) have a row to attach to.

```
INSERT skeleton (url, canonical_url, url_hash, crawled_at set; all other timestamps NULL)
   │
   ├── extract success  →  extracted_at = now; title/source/content/title_hash populated
   ├── extract failure  →  failed_at = now, last_error = "extract: ...", retry_count += 1   (skip rest)
   │
   ├── score success    →  scored_at = now, score = N, score_reason = "..."
   ├── score failure    →  failed_at = now, last_error = "score: ...", retry_count += 1     (skip notify)
   │   (note: Claude in Cowork is the scorer — "score failure" here means mark.py failed to write,
   │    or Claude's session ended before completing. AI judgment itself doesn't error.)
   │
   ├── notify success   →  notified_at = now, telegram_msg_id = M
   ├── notify failure   →  failed_at = now, last_error = "notify: ...", retry_count += 1
   │
   └── brainstorm       →  brainstormed_at = now, ideas = "[...]"
        (on-demand from Cowork; can be re-run; overwrites ideas)
```

**Re-processing rules (idempotent reruns):**
- Skeleton row is "to extract" iff `extracted_at IS NULL AND final_state IS NULL AND retry_count < MAX_RETRIES`.
- Row is "to score" iff `extracted_at IS NOT NULL AND scored_at IS NULL AND final_state IS NULL`.
- Row is "to notify" iff `scored_at IS NOT NULL AND notified_at IS NULL AND score >= threshold AND final_state IS NULL`.
- Successful stage clears `failed_at` + `last_error` (keeps `retry_count` for observability).
- `brainstormed_at` is orthogonal to `notified_at` — both can be set.

**Retry semantics (tool-level only):**
- Transient errors (5xx, network, parse error, content too short) — bump `retry_count`, set `failed_at`. Eligible for retry if `retry_count < MAX_RETRIES` (config, default 3).
- After MAX_RETRIES, next run sets `final_state = 'discarded'`. Row preserved for diagnostics; URL-hash dedup still matches → no re-discovery.

### 3.3 `list.py` Stage Projection

`list.py` projects timestamps to a readable stage column:
- `failed` if `failed_at IS NOT NULL` and not yet recovered.
- `brainstormed` if `brainstormed_at IS NOT NULL`.
- `notified` if `notified_at IS NOT NULL`.
- `scored` if `scored_at IS NOT NULL`.
- `extracted` if `extracted_at IS NOT NULL`.
- `new` otherwise.

`final_state` (if set) takes precedence.

`list.py --json` emits machine-readable output for Claude in Cowork to consume.

---

## 4. Critical Concerns

### 4.1 Secrets in source code (BLOCKING)
Original `config.py` had plaintext Telegram token. Unacceptable. No Anthropic key needed (Cowork uses Max plan auth, no SDK).
**Fix:** `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` via `python-dotenv` from `.env`; `.env` in `.gitignore`; ship `.env.example`.

### 4.2 AI runtime — Cowork, NOT subprocess (DECISION)
**User-confirmed decision:** Claude in Cowork IS the scorer and brainstormer. Python modules expose tool CLIs only; Claude calls them via Bash inside its session. No `anthropic` SDK, no `claude -p`, no API key.

Operational rules:
- Scoring rubric in `scoring-rubric.md`; brainstorm rules in `brainstorm-guidelines.md`. Claude reads them at the start of each scheduled run (fresh session, no in-memory persistence).
- The Cowork task prompt (`cowork-task-prompt.md`) is the orchestration brief; user pastes it into Cowork's `/schedule` UI once.
- Claude writes scoring/brainstorm results back to DB via `mark.py` Bash calls. No JSON to parse from Claude's response — DB writes are the contract.

### 4.3 Retry/backoff for tools (HIGH)
- `crawl.py`: HTTP retries on transient 5xx/network when fetching RSS.
- `extract.py`: HTTP retries on transient errors for trafilatura/Jina.
- `notify.py`: retries Telegram 429 (per-chat rate limit) with backoff; respects `Retry-After` header.
- **AI scoring does NOT retry** — if Claude's reasoning fails or session ends, the row stays unscored and next scheduled run picks it up. No backoff math needed at the AI layer.

### 4.4 No JSON parsing fragility
Claude writes results directly to DB via `mark.py score <id> <N> "<reason>"` and `mark.py brainstorm <id> '<json-array>'`. No fence-stripping needed. `mark.py` validates input (score is int 1–5, ideas is parseable JSON of length 5) and rejects malformed writes.

### 4.5 Google News RSS gives redirect URLs (HIGH)
`entry.link` from Google News is `https://news.google.com/rss/articles/CBM...` — a redirect, not the source URL.

**Fix — `resolve_canonical(url) -> str | None` with cascading fallbacks:**
1. **HEAD with redirects.** `httpx.head(url, follow_redirects=True, timeout=10).url`.
2. **HEAD returns 4xx/405 → GET stream.** `httpx.stream("GET", url, follow_redirects=True, timeout=15)`; close after redirect chain resolves (do NOT download body).
3. **Still unresolved → decode Google News URL.** Google encodes source URL inside `news.google.com/rss/articles/CBM...` path as base64. Decode and extract.
4. **Final fallback → keep Google URL** and rely on Jina Reader (`r.jina.ai/<url>`) at extraction stage.

**Validation requirement:** Task 4 must ship `crawler-validation.md` with per-tier success rate across ≥20 representative URLs. AC #9 requires ≥90% resolution rate.

URL canonicalization (§4.6) applied AFTER resolution.

### 4.6 URL canonicalization (MEDIUM)
Same article with `?utm_source=...`, `#anchor`, `www.` vs no-`www.`, trailing `/` are treated as different.
**Fix:** lowercase host, strip fragment, strip `utm_*` / `fbclid` / `gclid`, normalize trailing slash. Use `urllib.parse`.

### 4.7 Cross-source fuzzy dedup (MEDIUM)
Same event covered by 3 papers → 3 Telegram pings.
**Fix:** `title_hash = MD5(normalize(title))` (lowercase, strip punctuation, strip Vietnamese diacritics). Skip insert if `title_hash` exists in last 48h. Document title-hash limitations (paraphrased headlines miss); upgrade to simhash later if needed.

### 4.8 Concurrency hazard (HIGH)
Per-tool locking alone does NOT prevent the real race. Two overlapping Cowork sessions (e.g. user manually clicks "Run now" while the scheduled run is mid-flight) can both:
1. `list.py --not-scored --json` → both get rows `[1,2,3]`.
2. Both score row 1 with different reasons; both call `notify.py 1`; user receives **2 Telegram messages** for the same article.

Naive `fcntl.flock` does NOT solve this either: an `flock` is bound to the **process lifetime / file descriptor** that holds it. A `python lock.py acquire` command that exits cannot keep the lock held while Cowork makes subsequent separate tool calls — the kernel releases the lock as soon as the acquiring process exits.

**The lock must persist across multiple separate process invocations.** That means the lock state must live in a shared persistent store, not in an open file descriptor.

**Two-layer fix:**

1. **Run-level lock via SQLite mutex with TTL.** A `locks` table (see §3.1 addendum below) is the persistent lock store. `lock.py acquire <name>` performs:
   ```sql
   -- Step 1: opportunistically clean expired locks (TTL recovery)
   DELETE FROM locks WHERE expires_at < datetime('now');
   -- Step 2: atomic INSERT (fails on PRIMARY KEY conflict)
   INSERT INTO locks (name, owner_id, acquired_at, expires_at, owner_pid)
   VALUES (?, ?, datetime('now'), datetime('now', '+' || ? || ' seconds'), ?);
   ```
   On success, prints the generated `owner_id` (UUID) to stdout; exit 0.
   On conflict, exits 75 with `[lock] held by PID X since T (expires E)` log line.

   `lock.py release <name> <owner_id>` performs:
   ```sql
   DELETE FROM locks WHERE name = ? AND owner_id = ?;
   ```
   This only releases if the caller is the legitimate owner — protects against accidental release by a stale Cowork session.

   **Owner-id propagation across separate Bash invocations** (see §4.8.3): `acquire` prints the `owner_id` (UUID) to stdout. Claude (in Cowork's agentic loop) records the UUID from the tool result and supplies it as a literal positional argument to every subsequent `heartbeat` and `release` invocation. No shared file on disk; no shell variables. This works because Claude's session memory carries the prior tool result through the rest of the scheduled task.

   **TTL** (default `LOCK_TTL_SECONDS` = 1800s = 30 min; see §4.8.1) ensures recovery if Cowork crashes mid-run without releasing.

   Why SQLite, not `flock`: SQLite mutex persists across separate process invocations (which is the actual operational requirement) and gives natural crash-recovery via TTL expiration. `flock` cannot do this.

2. **Compare-and-set in `mark.py` and `notify.py`** as defense in depth (covers manual brainstorm + retries + lock-TTL races):
   - `mark.py score <id> <N> <reason>` executes:
     ```sql
     UPDATE articles
        SET scored_at = datetime('now'), score = ?, score_reason = ?
      WHERE id = ? AND scored_at IS NULL AND final_state IS NULL;
     ```
     If `rowcount == 0` → log `[mark.py] row <id> already scored or discarded; no-op` and exit 0 (NOT an error).
   - `notify.py <id>` uses same pattern: `UPDATE ... WHERE notified_at IS NULL AND score >= threshold`, no-op if 0.
   - `mark.py brainstorm` is allowed to overwrite (explicit re-brainstorm semantics), no CAS.

Both layers are required: run-lock prevents the read-then-act race for scheduled runs; CAS protects against TTL-expired-but-still-running edge cases and manual flow overlaps.

### 4.8.1 Schema addendum for the `locks` table

```sql
CREATE TABLE locks (
    name        TEXT PRIMARY KEY,           -- e.g. 'pipeline-run'
    owner_id    TEXT NOT NULL,              -- UUID generated by acquire; required for release/heartbeat
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL,              -- TTL: now + LOCK_TTL_SECONDS (default 1800 = 30min)
    owner_pid   INTEGER                     -- informational only, for debugging
);
```

Created by `init_db()` in Task 3 alongside the `articles` table.

### 4.8.2 Heartbeat to handle long-running scheduled runs (HIGH)

A fixed TTL alone is insufficient: a real run that exceeds `LOCK_TTL_SECONDS` (e.g. slow extraction, retry storm) would lose its lock mid-execution and let the next scheduled run race with it.

**Fix — `lock.py heartbeat <name> <owner_id>` operation:**

```sql
UPDATE locks
   SET expires_at = datetime('now', '+' || ? || ' seconds')
 WHERE name = ? AND owner_id = ?;
```

- On `rowcount == 1` → exit 0 (lock extended).
- On `rowcount == 0` → exit 75 with `[lock] lost ownership (likely TTL-reclaimed by another session)`. **The caller (Cowork) MUST abort the run immediately on heartbeat failure** to avoid racing the new owner.

**TTL sizing strategy:**
- `LOCK_TTL_SECONDS` default = **1800 (30 min)** — generous for a healthy run (typical: <5 min), tight enough that a crashed Cowork session releases the lock well before the next daily schedule.
- The Cowork task prompt MUST call `heartbeat` between major stages: after crawl, after extract, before the scoring loop, after every N scored rows (config `HEARTBEAT_EVERY_N_ROWS`, default 10), and before the notify pass. The notify pass is followed directly by `release`, so a post-notify heartbeat is unnecessary (release operates on `name` + `owner_id` regardless of TTL). Each heartbeat resets `expires_at` to `now + LOCK_TTL_SECONDS`.
- If Cowork crashes between heartbeats, `LOCK_TTL_SECONDS` upper-bounds the recovery delay. Worst case: 30 min until next scheduled run can reclaim.

`cowork-task-prompt.md` must explicitly script the heartbeat calls and handle the heartbeat-failure abort path symmetrically with normal completion.

### 4.8.3 Owner-id propagation: Claude's agent memory, NOT a shared file (HIGH — supersedes earlier design)

**Earlier rounds attempted to share `owner_id` across Cowork's separate Bash invocations via a shared `logs/current-run.owner` file. That design is broken** (ISSUE-23): if a TTL-expired session A wakes up after a fresh session B has acquired the lock and overwritten the file, A's "no-arg" heartbeat would read B's `owner_id` and incorrectly succeed (extending B's lock), and A's release would delete B's lock. Ownership isolation collapses.

**Correct mechanism: Claude (in Cowork) captures `owner_id` from `acquire`'s stdout into its own context-window memory, and substitutes the literal UUID into every subsequent `heartbeat` / `release` invocation.** This works because:
- Each Bash tool result remains in Claude's conversation history for the duration of the scheduled-task session.
- Claude's tool-use loop naturally substitutes values from prior tool outputs into subsequent tool inputs — that's standard agentic behavior.
- The `owner_id` never lives on disk in a mutable shared location, so no other session can race-overwrite it.
- If session A is paused past TTL and session B reacquires, A's resumed heartbeat call uses A's UUID literally — `UPDATE WHERE owner_id=A` matches no row (B owns the lock now) → rowcount 0 → exit 75 → Cowork aborts. Safe.

**`lock.py` interface (revised):**

- `lock.py acquire <name> [--ttl SECS]`:
  1. Generate `owner_id` (UUID).
  2. DELETE expired locks; INSERT new row.
  3. Print `owner_id` to stdout on its own line (machine-readable).
  4. **No file write.** Atomicity is purely SQLite-level; no rollback complication.

- `lock.py heartbeat <name> <owner_id>` — **owner_id is REQUIRED, positional.** No default lookup.
  - `UPDATE locks SET expires_at=now+TTL WHERE name=? AND owner_id=?`
  - rowcount 1 → exit 0.
  - rowcount 0 → exit 75 with `[lock] lost ownership: owner_id=<...> not held`.

- `lock.py release <name> <owner_id>` — owner_id REQUIRED.
  - `DELETE FROM locks WHERE name=? AND owner_id=?`
  - rowcount irrelevant for exit code (exit 0); just log whether it actually deleted.

**Cowork prompt contract (see Task 12):**
- Step 1 runs acquire and Claude records the UUID printed to stdout.
- Every subsequent heartbeat / release explicitly includes the recorded UUID as a literal command-line argument. The prompt template uses `<OWNER_ID>` as a placeholder with explicit "substitute the UUID printed by step 1 here" instruction.

This relies on Claude's session memory (which is reliable for the duration of one scheduled task) and removes the entire shared-file failure mode. ISSUE-22's acquire rollback also dissolves — there is no file write step to fail in.

### 4.8.4 Abort taxonomy for `cowork-task-prompt.md` (HIGH)

There are **two distinct classes of abort** in the scheduled run, with different release semantics:

| Abort cause | Ownership status | Release on abort? | Rationale |
|-------------|------------------|-------------------|-----------|
| Tool failure (crawl/extract/list/notify exits ≠ 0) | We still own the lock | **YES — call release** | Lock is ours; releasing immediately restores availability for the next scheduled run / manual retry. Skipping release would block for up to `LOCK_TTL_SECONDS` (30 min) for no safety benefit. |
| Heartbeat failure (lost ownership) | We do NOT own the lock | **NO — do NOT call release** | The lock belongs to another session (TTL-reclaimed). Calling release with our stale owner_id is technically safe (DELETE rowcount=0, no clobber possible since we use our literal recorded UUID — not the current owner's), but logically meaningless and adds noise to logs. Safer to abort cleanly. |

`cowork-task-prompt.md` MUST implement both paths:
- After every tool invocation (except heartbeat): if exit ≠ 0 and exit ≠ "CAS no-op exit 0" → call `python lock.py release pipeline-run <OWNER_ID>` THEN abort.
- After every heartbeat invocation: if exit == 75 → abort WITHOUT calling release.
- On success (last step): call `python lock.py release pipeline-run <OWNER_ID>` then exit normally.

(`<OWNER_ID>` here is a literal placeholder. At runtime, Claude substitutes the UUID captured from `acquire`'s stdout per §4.8.3.)

### 4.9 Telegram Markdown escaping (MEDIUM)
Article titles contain `*`, `_`, `[`, `(`, `.`, `-` → MarkdownV2 returns 400 → article never pushed.
**Fix:** `parse_mode: "HTML"` with `html.escape()` on title/source/reason.

### 4.10 Structured logging (LOW)
All tool CLIs use stdlib `logging` with `RotatingFileHandler` (5 MB × 3 files) to `logs/pipeline.log`. Format: `%(asctime)s %(levelname)s %(name)s: %(message)s`. Cowork can `tail logs/pipeline.log` if it needs to inspect history.

### 4.11 trafilatura.fetch_url timeout (LOW)
Can hang the process.
**Fix:** `httpx.get(timeout=15)` + `trafilatura.extract(html)` (explicit timeout in two stages).

### 4.12 Path handling
All paths via `pathlib.Path(__file__).resolve().parent`. No hardcoded `/Users/...`.

### 4.13 Telegram rate-limit safety (LOW)
Sleep 1.2s between sends. Respect `Retry-After` header on 429.

### 4.14 Cowork-specific risks (HIGH, accepted)
- **Mac asleep / Claude Desktop closed → scheduled task is skipped.** Cowork catches up ONCE next time the app opens, NOT a per-missed-run backfill. User accepted this trade-off; see §8.
- **Auth refresh** — Claude Desktop handles OAuth refresh automatically. If session expires (rare), Cowork surfaces a re-auth prompt at next run.
- **5-hour rolling quota** on Max plan. Hard cap `MAX_ARTICLES_PER_RUN` (config, default 50) to avoid burning the entire quota on one giant scoring burst.
- **Cowork task-prompt drift** — Cowork is documented to "rewrite the prompt based on what it learned" between runs. Convenient but can drift. Mitigation: `cowork-task-prompt.md` is the canonical source (git-tracked). When behavior drifts, re-paste the canonical prompt into the Cowork task.

---

## 5. Task Breakdown

Tasks ordered by dependency. Each is a single commit.

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 1 | **Bootstrap skeleton + hardened `install.sh` v1 for non-tech users** (addresses ISSUE-2). Files: `.env.example`, `.gitignore`, `requirements.txt` (NO `anthropic`), `pathlib`-based paths, `logs/.gitkeep`. **`install.sh` v1 requirements:** (1) `uname -s` == Darwin OR exit. (2) `sw_vers -productVersion` major ≥ 13 OR exit. (3) `uname -m` → set `BREW_PREFIX=/opt/homebrew` (arm64) or `/usr/local` (x86_64). (4) Homebrew bootstrap: if `! command -v brew`, run `NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL .../install.sh)"`. **The `NONINTERACTIVE=1` env var is required** — it suppresses the "press RETURN to continue" prompt that would otherwise stall a non-tty shell. (5) After install OR if brew exists but not on PATH: `eval "$($BREW_PREFIX/bin/brew shellenv)"` (ISSUE-2 — `brew` may not appear on PATH in current shell post-install). (6) `command -v python3.11 \|\| brew install python@3.11; eval "$($BREW_PREFIX/bin/brew shellenv)"` (re-eval to pick up python). (7) `command -v sqlite3 \|\| exit error`. (8) `[ -d .venv ] \|\| python3.11 -m venv .venv`. (9) `.venv/bin/pip install -q -r requirements.txt`. (10) `mkdir -p logs`. (11) Network sanity. (12) **Sudo detection: if Homebrew install requires Xcode CLT → sudo prompt would block; capture this and exit code 78 with `[error] sudo required; please run BOOTSTRAP.md or open Terminal once to run xcode-select --install`.** (13) Print summary banner. Tagged log lines (`[check]/[install]/[ok]/[warn]/[error]`). Idempotent. Schema init added in Task 3 (install.sh v2). | `requirements.txt`, `.env.example`, `.gitignore`, `logs/.gitkeep`, `install.sh` (v1) | (a) Fresh macOS 13+ Apple Silicon without brew → `bash install.sh` succeeds with brew at `/opt/homebrew`. (b) Same on Intel → succeeds with brew at `/usr/local`. (c) Re-run → all `[ok]`, idempotent. (d) macOS 12 → exit non-zero. (e) Non-Darwin → exit non-zero. (f) **NONINTERACTIVE Homebrew install does NOT prompt for RETURN; full install completes without user keyboard input.** (g) **After install, `brew` is callable in the same shell session via the shellenv eval.** (h) Sudo required (Xcode CLT missing) → exit 78 with actionable [error] message. (i) `! test -f news.db` at this stage. |
| 2 | `config.py`: load env via dotenv (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID), expose `TOPICS`, `EXTRA_RSS_SOURCES`, `MIN_SCORE_TO_NOTIFY` (per topic), `MAX_ARTICLES_PER_RUN` (default 50), `MAX_RETRIES` (default 3), `DRY_RUN_DEDUP_WINDOW_HOURS` (default 48), **`LOCK_TTL_SECONDS` (default 1800 — 30 min; heartbeat extends as needed)**, `HEARTBEAT_EVERY_N_ROWS` (default 10) | `config.py` | `python -c "import config; print(config.TOPICS)"` works without env vars set |
| 3 | `db.py` + extend `install.sh` to v2: schema for **both `articles` table (§3.1) and `locks` table (§4.8.1)**, `init_db`, `insert_candidate`, mark helpers (`mark_extracted`, `mark_scored`, `mark_notified`, `mark_brainstormed`, `mark_failed`, `mark_discarded`), `get_article`, `list_by_stage`, URL canonicalization + title-hash helpers, **RO-mode open helper** (`open_conn(read_only: bool)` — tolerates missing file or missing tables without creating schema). **Append `init_db()` step to `install.sh` v2 with tagged log lines: `[check] news.db exists or fresh init`, `[init] creating articles + locks tables`, `[ok] schema initialized`. Idempotent (skip init if tables exist).** | `db.py`, `install.sh` (v2) | `init_db()` creates `news.db` with BOTH tables; `sqlite3 news.db ".schema"` shows `articles` and `locks`; pytest: (a) RO open against missing file returns empty without creating, (b) RO open against DB with no tables returns empty without modifying (mtime unchanged), (c) write helpers raise under `read_only=True`; running `bash install.sh` twice does not corrupt or re-init existing tables |
| 4 | `crawl.py`: Google News + extra RSS, `resolve_canonical` per §4.5 (4-tier), URL+title dedup at collect time, insert skeleton rows. INSERT atomicity comes from `url_hash UNIQUE`. **Flags:** `--dry-run`, `--topic <name>`, **`--inject-test <TOKEN>`** (used by §2.1.1 Step 5.1; REQUIRES a per-attempt hex token). `--inject-test <TOKEN>` inserts ONE row with: `url=bootstrap-test://<TOKEN>`, `canonical_url=bootstrap-test://<TOKEN>`, `url_hash=md5(canonical_url)`, `title="[BOOTSTRAP TEST <TOKEN>] sample article"`, `title_hash=md5(normalize(title))`, `source="bootstrap-inject"` (human-readable label only — NOT used for priority logic), `content="Test content for verification — VinFast Q1 sample"`, `crawled_at=now`, **`extracted_at=now`**, **`scored_at=now`**, **`score=5`**, **`score_reason="bootstrap verification"`**, `notified_at=NULL`, `retry_count=0`, `final_state=NULL`. **Reserved URL scheme (per ISSUE-15)**: `bootstrap-test://` is RESERVED for verification rows. crawl.py's normal RSS-driven flow MUST reject any incoming URL that matches `bootstrap-test://*` with a `[warn]` log — this prevents real content from accidentally producing a privileged-priority row. Idempotent per TOKEN. Ship `crawler-validation.md` with success rate per tier across ≥20 sampled URLs. | `crawl.py`, `crawler-validation.md` | manual run on VinFast topic; ≥90% success; dry-run on missing DB does not create file; `crawl.py --inject-test ABC123` creates 1 row with `url='bootstrap-test://ABC123'`, `score=5`, `scored_at NOT NULL`, `notified_at IS NULL`; `list.py --stage scored --not-notified` returns it FIRST even with 100 backlog rows (priority ORDER BY on the URL scheme); **feeding a malicious RSS that contains `<link>bootstrap-test://attacker</link>` is REJECTED by crawl.py with a `[warn]` log line, no row inserted** |
| 5 | `extract.py`: `--pending` flag fetches content for rows where `extracted_at IS NULL`. trafilatura w/ explicit timeout, Jina fallback. **CAS write pattern** (consistent with §4.8): `UPDATE articles SET extracted_at=now, title=?, source=?, content=?, title_hash=? WHERE id=? AND extracted_at IS NULL` — rowcount 0 → no-op + log. On failure → `mark_failed` + bump retry_count. No separate file lock — CAS plus the run-level SQLite mutex are sufficient. | `extract.py` | run after crawl; rows get content; failures incr retry_count; after MAX_RETRIES, next run sets `final_state='discarded'`; running `extract.py --pending` twice in parallel on the same row → only one writer wins (CAS no-op on the loser) |
| 6 | `notify.py <id>`: single-article Telegram push. HTML mode, `html.escape()` on title/source/reason. **Message format (REQUIRED fields — addresses ISSUE-13):** (1) article id prominently `[#42]` at start; (2) **article title verbatim** (escaped) — required so verification TOKEN (embedded in title for bootstrap-inject rows) reaches the user's Telegram visually; (3) source; (4) score badge + reason; (5) link to canonical_url. 1.2s sleep tail. Retry Telegram 429 respecting `Retry-After`. **Compare-and-set per §4.8**: `UPDATE ... WHERE notified_at IS NULL AND score >= threshold`. If `rowcount == 0`, no-op + log + exit 0. | `notify.py` | `python notify.py 42` sent message visibly contains BOTH `[#42]` AND the full title text; with a bootstrap-inject row whose title contains TOKEN, the TOKEN appears in the rendered Telegram message body; second invocation no-ops; title with `<&*_[](.).` chars works; 429 retry logged |
| 7 | `mark.py`: CLI to set DB state. Subcommands: `mark.py score <id> <N> "<reason>"` (validates 1≤N≤5, **compare-and-set on `scored_at IS NULL AND final_state IS NULL`, no-op if 0 rows**), `mark.py brainstorm <id> '<json>'` (validates JSON array length 5; brainstorm allows overwrite, no CAS), `mark.py discard <id>`, `mark.py archive <id>`. | `mark.py` | `mark.py score 1 4 "good"` updates row; second `mark.py score 1 5 "x"` no-ops with log `[mark.py] row 1 already scored; no-op` exit 0; `mark.py score 1 99 "bad"` exits non-zero; malformed JSON rejected |
| 8 | `list.py`: query by stage + score + time. **Complete flag enumeration:** `--stage new\|extracted\|scored\|notified\|brainstormed\|failed`, `--min-score N` (aliased to `--score-gte`), `--max-score N` (aliased to `--score-lte`), `--score N` (exact), `--not-scored`, `--not-notified`, `--not-brainstormed`, `--final-state discarded\|archived\|active`, `--id N`, **time filters:** `--today`, `--yesterday`, `--last-hours N`, `--since "YYYY-MM-DD"`, **`--search "keyword"` (SQL LIKE on title + content, case-insensitive)**, `--limit N` (default = `MAX_ARTICLES_PER_RUN`), `--json` (machine output) vs default table. **Ordering (per ISSUE-10): all queries apply `ORDER BY CASE WHEN url LIKE 'bootstrap-test://%' THEN 0 ELSE 1 END, scored_at DESC NULLS LAST, crawled_at DESC`** — verification rows ALWAYS sort first. **The `bootstrap-test://` URL scheme is RESERVED** (per ISSUE-15): no real RSS feed produces this scheme; `crawl.py` MUST reject any incoming URL matching `bootstrap-test://*` to keep the sentinel unforgeable. `source='bootstrap-inject'` is kept as a human-readable label but NOT used for priority logic. | `list.py` | `list.py --stage extracted --not-scored --json` valid JSON; default limit enforces MAX_ARTICLES_PER_RUN; `list.py --search "VinFast" --json` matches title OR content; **with 100 backlog rows + 1 row whose url starts with `bootstrap-test://`, `list.py --stage scored --not-notified --limit 50 --json` returns the bootstrap-test row at index 0**; **crawl.py rejects (with log) any RSS-discovered URL starting with `bootstrap-test://`**; pytest covers each flag combination |
| 9 | `lock.py`: **SQLite mutex with TTL + heartbeat. Owner_id is propagated via Claude's session memory, NOT via shared file** (§4.8.3 revised). Three subcommands: (a) `acquire <name> [--ttl SECS]` → DELETE expired + atomic INSERT (transactional). Prints owner_id (UUID) to stdout as the only output line. Exit 0 on success; PK conflict → exit 75. **No file write, no rollback edge cases.** (b) `heartbeat <name> <owner_id>` — **owner_id is REQUIRED positional argument**. `UPDATE locks SET expires_at=now+TTL WHERE name AND owner_id`; rowcount 1 → exit 0; rowcount 0 → exit 75 with `[lock] lost ownership: owner_id=<...> not held`. (c) `release <name> <owner_id>` — owner_id REQUIRED. `DELETE WHERE name AND owner_id`; exit 0 always (idempotent); log whether it actually deleted (rowcount). Default TTL = `LOCK_TTL_SECONDS` (default 1800s = 30min). | `lock.py` | Verify: (a) `OWNER=$(python lock.py acquire pipeline-run)` captures a UUID; (b) `python lock.py heartbeat pipeline-run "$OWNER"` succeeds, `expires_at` extends (verify via SQL); (c) `python lock.py heartbeat pipeline-run not-a-real-uuid` exits 75; (d) immediate 2nd `acquire` exits 75; (e) `--ttl 1` + `sleep 2` + acquire cleans + reclaims (returns DIFFERENT UUID); (f) **after step e, heartbeat with the ORIGINAL UUID exits 75 — this is the real ISSUE-23/24 scenario**; (g) `release` with mismatched UUID exits 0 but rowcount log shows 0 (no clobber); (h) `release` with the correct UUID exits 0 and deletes the row |
| 10 | `scoring-rubric.md`: 1–5 thang điểm in Vietnamese for GenK editorial voice, with worked examples per score level. Self-contained — readable cold by Claude in a fresh Cowork session. | `scoring-rubric.md` | review: a second person scores 3 sample articles consistently using rubric alone |
| 11 | `brainstorm-guidelines.md`: 5-idea format with fields `{title, angle, format, difficulty, viral_potential}` per spec; worked example; explicit "output JSON array length 5 only". | `brainstorm-guidelines.md` | review: same as above |
| 12 | `cowork-task-prompt.md`: canonical orchestration prompt for Cowork `/schedule`. **First instructional sentence**: "Step 1 will run `lock.py acquire pipeline-run` and print a UUID to stdout. RECORD this UUID. In every subsequent `lock.py heartbeat ...` and `lock.py release ...` invocation in this prompt, the literal token `<OWNER_ID>` MUST be replaced with the recorded UUID before running the command." **Two global rules apply to every step in the sequence below** (must appear verbatim near the top of the prompt): (R1) **Every `lock.py heartbeat pipeline-run <OWNER_ID>` invocation: on exit 75, ABORT IMMEDIATELY WITHOUT calling `release` — see §4.8.4.** (R2) **Every non-heartbeat tool invocation: on non-zero exit (excluding CAS no-op exit 0 with `[already scored/notified]` log), call `python lock.py release pipeline-run <OWNER_ID>` THEN abort.** Step sequence (rules R1/R2 apply throughout — not re-spelled per step): (1) Acquire (capture UUID). (2) Read `scoring-rubric.md`. (3) `python crawl.py`. (4) `python lock.py heartbeat pipeline-run <OWNER_ID>`. (5) `python extract.py --pending`. (6) `python lock.py heartbeat pipeline-run <OWNER_ID>`. (7) `python list.py --stage extracted --not-scored --json`. (8) Per row: Claude scores + `python mark.py score <id> <N> "<reason>"`. Every `HEARTBEAT_EVERY_N_ROWS` rows: `python lock.py heartbeat pipeline-run <OWNER_ID>`. (9) `python lock.py heartbeat pipeline-run <OWNER_ID>`. (10) `python list.py --stage scored --min-score <THR> --not-notified --json`. (11) Per row: `python notify.py <id>`. (12) Success final step: `python lock.py release pipeline-run <OWNER_ID>`. | `cowork-task-prompt.md` | second-person read: (a) explicit "<OWNER_ID> = UUID from acquire stdout" instruction at the top; (b) rules R1 and R2 stated globally near the top — applied uniformly to EVERY step (no per-step variability); (c) every heartbeat and release uses `<OWNER_ID>` literally; (d) success final step releases; (e) 5 failure modes documented (acquire-failed, tool-failed-with-release per R2, heartbeat-lost-without-release per R1, CAS no-op recognized as normal, acquire PK-conflict) |
| 13 | **Cowork skill `SKILLS/idea-brainstormer.skill`** — slash-command skill installed at `/Users/theduyet/Documents/Code/vin-automate/SKILLS/idea-brainstormer.skill`. Invoked from any Cowork chat as `/idea-brainstormer [args]`. Designed for **1-skill-covers-brainstorm** today; can be extended later to multi-subcommand `news` skill if user wants. Skill content (frontmatter + instructions): (1) Resolve user args → `list.py` flags: no args → `list.py --not-brainstormed --today --score-gte 3 --json`; numeric arg → `list.py --id <N> --json`; `score=5` → `list.py --score 5 --not-brainstormed --json`; `"keyword"` → `list.py --search "keyword" --json`. (2) If multiple candidates: present table to user, ask which id; if zero: relax filter (e.g. drop score-gte) then retry, else report empty. (3) Read `brainstorm-guidelines.md` from project root for format spec. (4) Run `list.py --id <chosen> --json` to get full content. (5) Apply guidelines → emit 5-idea JSON. (6) Run `python mark.py brainstorm <id> '<json>'`. (7) Print formatted 5-idea table to user. Error handling: article not found, content empty, JSON validation failure all surface with clear message. | `SKILLS/idea-brainstormer.skill`, README cross-link | (a) In Cowork chat: `/idea-brainstormer 42` → produces 5 ideas, sets `brainstormed_at` on row 42, preserves `notified_at`. (b) `/idea-brainstormer` (no args) on a fresh day with new articles → presents candidate table from `list.py --today --score-gte 3`. (c) `/idea-brainstormer "VF8"` → `list.py --search "VF8" --json` finds matches. (d) Re-run on same id → overwrites `ideas` field (intentional, brainstorm allows overwrite per §4.8 CAS exception). |
| 14 | **`BOOTSTRAP.md`** at repo root. PRIMARY onboarding entry point — pasteable prompt for Cowork chat. Self-contained: contains all 6 steps from §2.1.1 spelled out as instructions Claude follows. **Critical responsibilities**: (1) Installs slash skills into Cowork's skill discovery path via symlink/copy (probes for `~/Library/Application Support/Claude/skills/` first, fall back to `~/.claude/skills/`; if neither exists or works, prints a manual-install fallback message). (2) Deterministic chat_id flow via nonce. (3) Mandatory test pipeline run + Telegram delivery confirmation before declaring success. (4) Clear fallback to `setup_helper.py` if Cowork can't proceed (sudo, allow-all-bash denied). The entire content must be **plain Vietnamese** with copy-paste-friendly code blocks for Claude to execute verbatim. | `BOOTSTRAP.md` | (a) Paste full content into a fresh Cowork chat on a fresh macOS 13+ machine → 6 steps execute, slash skills become discoverable, test Telegram message delivered. (b) Mid-flow user closes chat → re-paste resumes correctly (idempotent each step). (c) If skill dir auto-detection fails → BOOTSTRAP.md prints README link + symlink command + asks user to confirm before continuing. |
| 15 | **`SKILLS/setup.skill`** — POST-bootstrap convenience skill at `/Users/theduyet/Documents/Code/vin-automate/SKILLS/setup.skill`. Invoked as `/setup` AFTER BOOTSTRAP.md has installed it. Re-runs §2.1.1 for reconfiguration: idempotent install.sh re-run, optional Telegram credential refresh via deterministic nonce flow per §2.1.1 Step 3 (ISSUE-5/7), re-link skill symlinks if missing, **mandatory scheduler verification via the §2.1.1 Step 5 model** (pre-arm with unique TOKEN via `crawl.py --inject-test <TOKEN>`, ask user to "Run now" on the saved scheduled task in Cowork sidebar, poll for `notified_at` on the tokenized row, user confirms receipt of the token in Telegram), summary. **Must NOT use the deprecated in-chat synthetic pipeline run model** (regression risk for ISSUE-6). | `SKILLS/setup.skill` | (a) After BOOTSTRAP.md ran once, `/setup` in a new chat works. (b) Re-running on configured system → idempotent. (c) Invalid token → nonce re-prompt, no crash. (d) Scheduler verification uses the SAVED task + "Run now" + tokenized row + user confirmation, identical to §2.1.1 Step 5; no in-chat pipeline shortcut. |
| 16 | **`setup_helper.py`** Terminal-fallback TUI for non-tech user when Cowork can't run BOOTSTRAP.md. Uses plain `input()` prompts in Vietnamese. Implements the SAME deterministic chat_id flow + skill install logic as BOOTSTRAP.md. Output: writes `.env`, installs skills, prints next-step instructions (paste cowork-task-prompt.md into `/schedule` UI). **Does NOT attempt Cowork UI automation** — its job is to get the user from "Cowork can't onboard me" to "everything except scheduler is configured; manual UI step remains". | `setup_helper.py` | (a) `python3 setup_helper.py` in Terminal completes credential setup + skill install without Cowork. (b) Same deterministic nonce flow for chat_id. (c) Final stdout includes exact next steps for Cowork scheduler. (d) Refuses to run if `.venv` not present (instructs user to `bash install.sh` first). |
| 17 | **README** — non-tech-first onboarding: paste BOOTSTRAP.md into Cowork chat as PRIMARY path; Terminal-fallback (`bash install.sh && python3 setup_helper.py`) as SECONDARY; manual slash-skill install commands as TERTIARY (`ln -sf $PWD/SKILLS/idea-brainstormer.skill ~/Library/Application\ Support/Claude/skills/idea-brainstormer/SKILL.md`). Telegram message format ([#<id>] prominent), brainstorm usage (`/idea-brainstormer 42`), troubleshooting per failure mode, stale lock recovery via TTL/manual override, all in plain Vietnamese. | `README.md` | — |
| 18 | **E2E validation matrix** producing `E2E-VALIDATION.md` per §7.2 with each row evidenced. | `E2E-VALIDATION.md` | every row ✅ with evidence |

---

## 6. Cost Estimate

- **Anthropic API: $0** (Cowork uses Max plan auth — no API key, no SDK).
- **Max plan: already paid** — marginal cost zero.
- **Telegram Bot API: free.**
- **Jina Reader: free tier 1M tokens/month** (more than enough for fallback usage).
- **Total marginal cost: $0/month.**

Risk: Cowork has a 5-hour rolling quota. Hard-capped via `MAX_ARTICLES_PER_RUN=50` to keep one run's scoring within budget.

---

## 7. Acceptance Criteria

### 7.1 Criteria

1. `python crawl.py --dry-run` produces console report without mutating `news.db` (SHA-256 unchanged before/after, via `shasum -a 256`). Also tolerates missing DB / DB with no `articles` table without creating schema.
2. Cowork scheduled run on a fresh DB:
   - Skeleton rows inserted with canonical URLs.
   - Re-run collects 0 new (URL dedup) and 0 new (title-hash dedup within 48h).
   - Every extracted row eligible for scoring gets `scored_at` + `score` 1–5 + `score_reason`.
   - Every row with `score >= min_score_to_notify` gets `notified_at` + `telegram_msg_id`; message arrives in HTML mode without 400 errors.
   - Number of rows scored in a single run is **mechanically capped at `MAX_ARTICLES_PER_RUN`** via `list.py` default limit.
3. Brainstorm flow: in any Cowork chat, user invokes `/idea-brainstormer <id>` (or `/idea-brainstormer` for picker mode). The skill calls `list.py`, reads `brainstorm-guidelines.md`, generates 5 ideas, calls `mark.py brainstorm`. `brainstormed_at` set, `ideas` populated. Pre-existing `notified_at` and `scored_at` are preserved (orthogonal facts).
4. `python list.py --stage notified --limit 10 --json` returns valid JSON parseable by `jq`. `list.py` without `--limit` returns at most `MAX_ARTICLES_PER_RUN` rows.
5. Cowork scheduled task is created via `/schedule` with `cowork-task-prompt.md` content as the prompt. Step 1 runs `python lock.py acquire pipeline-run` and Claude records the printed UUID. **Every subsequent `heartbeat` and `release` invocation in the prompt substitutes the recorded UUID for `<OWNER_ID>` literally** — no shared file, no shell variables (§4.8.3 revised). Heartbeats occur between every major stage and every `HEARTBEAT_EVERY_N_ROWS` scoring iterations. **Abort taxonomy per §4.8.4:** tool failure → `release <OWNER_ID>` + abort; heartbeat exit 75 → abort WITHOUT release. Success path final step calls release with `<OWNER_ID>`. Runs once per hour while Mac is awake + Claude Desktop is open.
6. No plaintext secrets in committed files. `.env` is gitignored. No `anthropic` package imported anywhere.
7. **Run-level concurrency:** Two overlapping Cowork sessions cannot both score/notify the same row. Demonstrated via (a) `lock.py acquire` returning exit 75 on second attempt, AND (b) `mark.py score <id>` and `notify.py <id>` no-op (exit 0, log line) when the row's stage timestamp is already set (compare-and-set protection).
8. Tool HTTP retries: `notify.py` retries Telegram 429 honoring `Retry-After`; `extract.py` retries trafilatura/Jina on transient errors up to `MAX_RETRIES`.
9. Redirect resolution (§4.5) produces a downstream-usable canonical URL for ≥90% of ≥20 sampled URLs in `crawler-validation.md`. "Downstream-usable" means: either tiers 1–3 yield a non-Google-News canonical URL (preferred), OR tier 4 returns the original Google News URL which `extract.py`'s Jina Reader fallback dereferences at extraction stage. The intent is end-to-end content recovery, not a strict tier-1-3 success rate. **Amended 2026-05-12** after first live validation showed Google News changed its URL format and tier-3 base64 decode is currently a no-op — tier 4 + Jina is the documented fallback (PLAN §4.5 tier 4: "Keep Google URL; rely on Jina Reader at extraction stage"). Future optimization: upgrade tier 3 to a Google News protobuf decoder to bump strict tier-1-3 rate.
10. Stale lock recovery: if a `lock.py acquire` finds a row in the `locks` table whose `expires_at < now`, the opportunistic cleanup step deletes it and the new INSERT succeeds. Logged as `[lock] expired lock cleaned`.

11. **Non-tech onboarding works end-to-end on a fresh macOS 13+ machine via BOOTSTRAP.md.** A user with NO command-line experience can: (a) install Claude Desktop and sign in to Pro/Max; (b) clone or download this repo; (c) add the folder to Cowork's trusted folders; (d) **open `BOOTSTRAP.md`, copy entire content, paste into any Cowork chat**. Claude follows the pasted instructions: install.sh runs (Homebrew + Python + venv + deps + schema), Telegram credentials gathered via deterministic nonce flow, slash skills installed into Cowork's discovery dir, mandatory test pipeline run delivers a real Telegram message which the user confirms received, scheduled task registered with user-confirmed save. After completion, slash skills `/setup` and `/idea-brainstormer` are discoverable. The Terminal-fallback path (`bash install.sh && python3 setup_helper.py`) exists for sudo/allow-all-bash blocks; the README documents both paths in plain Vietnamese.

### 7.2 Validation Matrix (drives Task 18)

Each row is one scenario in `E2E-VALIDATION.md`.

| AC # | Scenario | How to run | Expected evidence |
|------|----------|-----------|-------------------|
| 1a | Crawl dry-run non-destructive (DB exists) | `shasum -a 256 news.db > a && python crawl.py --dry-run && shasum -a 256 news.db > b && diff a b` | diff exits 0; console shows would-be-inserted count |
| 1b | Crawl dry-run with missing DB | `rm -f news.db && python crawl.py --dry-run && ! test -f news.db` | command succeeds exit 0; news.db NOT created; console reports all candidates as "would-be-inserted" |
| 1c | Crawl dry-run with DB present but no `articles` table | `rm -f news.db && sqlite3 news.db "CREATE TABLE dummy(x)" && before=$(stat -f %m news.db) && python crawl.py --dry-run && after=$(stat -f %m news.db) && [ "$before" = "$after" ]` | command succeeds; news.db mtime unchanged |
| 2a | Fresh real run via Cowork | In Cowork sidebar, open the scheduled task; click "Run now". After completion: `sqlite3 news.db "SELECT count(*) FROM articles WHERE scored_at IS NOT NULL"` | non-zero count; DB has rows with extracted_at, scored_at, score, optionally notified_at |
| 2b | URL dedup on rerun | Trigger Cowork run twice in succession | 2nd run's log shows `0 new candidates`; no duplicate `url_hash` rows in DB |
| 2c | Title-hash dedup | Pre-insert a row with duplicate `title_hash` from another source; trigger run | not re-inserted; `logs/pipeline.log` shows `[dedup] title-hash match` |
| 2d | Score threshold | Set `min_score_to_notify=5` in config; trigger run | only score=5 rows have `notified_at` set |
| 2e | Telegram HTML safety | Inject test article with title `Test <b>&*_[](.).</b>` | message arrives in chat; bot does NOT return 400 |
| 3 | Brainstorm preserves notified state via `/idea-brainstormer` skill | Run scheduled task to get a notified article (say id=42). In a fresh Cowork chat: type `/idea-brainstormer 42` | Row 42 has BOTH `notified_at` AND `brainstormed_at` non-null; `ideas` is valid JSON of length 5. Free-form trigger (typing only "brainstorm article 42" without the slash command) is NOT the canonical path and not part of acceptance. |
| 4 | `list.py --json` | `python list.py --stage notified --json \| jq` | exits 0; valid JSON array |
| 5 | Cowork scheduled task exists & runs | In Cowork sidebar, scheduled task "vinfast-pipeline" visible with daily cadence; trigger manually; verify `logs/pipeline.log` has entry timestamped within last 60s | screenshot + log line |
| 6 | No secrets in tree | `grep -rIE '[0-9]{9,}:AAE[a-zA-Z0-9_-]{30,}\|sk-(ant-)?[a-zA-Z0-9_-]{20,}' . --exclude-dir=.venv --exclude='.env' --exclude='news.db'` | zero matches |
| 7a | Run-level SQLite mutex blocks overlapping runs (owner_id propagated via captured UUID) | Shell A: `OWNER_A=$(python lock.py acquire pipeline-run --ttl 600)`. Shell B (fresh): `python lock.py acquire pipeline-run` → exits 75. Shell C (fresh): `python lock.py release pipeline-run "$OWNER_A"` → exits 0, rowcount log shows 1. Shell D: `python lock.py acquire pipeline-run` → exits 0 (returns NEW UUID) | DB is the only shared state; owner_id is passed as a CLI arg, not read from any file. `sqlite3 news.db "SELECT count(*) FROM locks"` returns 0 after Shell C, then 1 after Shell D. |
| 7b | mark.py score CAS no-op | `python mark.py score 1 4 "first"` then `python mark.py score 1 5 "second"` | 1st updates row with score=4; 2nd exits 0 with `[mark.py] row 1 already scored; no-op`; DB still shows score=4 |
| 7c | notify.py CAS no-op | Send notify twice in succession on a fresh row that crosses threshold | 1st sends Telegram message; 2nd exits 0 with `[notify.py] row <id> already notified; no-op`; Telegram receives ONE message only |
| 7d | Stale lock auto-reclaim via TTL | `python lock.py acquire pipeline-run --ttl 1` (1s TTL); `sleep 2`; `python lock.py acquire pipeline-run` from a fresh shell | 2nd acquire SUCCEEDS (new owner_id); log line `[lock] expired lock cleaned`; `sqlite3 news.db "SELECT count(*) FROM locks WHERE name='pipeline-run'"` returns 1 (new lock only) |
| 7e | Heartbeat extends lock (cross-shell, captured UUID) | Shell A: `OWNER_A=$(python lock.py acquire pipeline-run --ttl 60)`. Capture initial `expires_at` via SQL. `sleep 5`. Shell B (fresh): `python lock.py heartbeat pipeline-run "$OWNER_A"`. Re-read `expires_at`. | 2nd `expires_at` > 1st; heartbeat works across shells because the UUID is passed as a CLI argument, not stored in shell state. |
| 7f | Heartbeat fails when another session has TTL-reclaimed (REAL scenario per ISSUE-23/24, no manual file rewrite) | Shell A: `OWNER_A=$(python lock.py acquire pipeline-run --ttl 1)`. `sleep 2` (A's lock TTL-expires). Shell B (fresh): `OWNER_B=$(python lock.py acquire pipeline-run)` (succeeds — cleanup removed A's row, B's row inserted with NEW UUID). Shell C (fresh): `python lock.py heartbeat pipeline-run "$OWNER_A"` (uses A's UUID literally — the actual runtime case) | heartbeat exits 75 with `[lock] lost ownership: owner_id=<A's UUID> not held`; **the test does NOT rewrite any file** — A's UUID simply doesn't match the row in DB (B owns it), so `UPDATE WHERE owner_id=A` matches 0 rows. Also verify: `python lock.py release pipeline-run "$OWNER_A"` exits 0 with rowcount log = 0 (no clobber of B's lock). |
| 7g | Tool failure releases the lock (abort-with-release per §4.8.4, with captured UUID) | Shell A: `OWNER=$(python lock.py acquire pipeline-run)`. Simulate tool failure (e.g. `python crawl.py --nonexistent-flag` exits 2). Follow cowork-task-prompt.md's abort path: Shell B (fresh): `python lock.py release pipeline-run "$OWNER"`. | release exits 0, rowcount log shows 1, `sqlite3 SELECT count(*) FROM locks` returns 0. Lock is NOT held for the full TTL. |
| 7h | acquire conflict is rejected cleanly (no orphan rows) | Shell A: `OWNER_A=$(python lock.py acquire pipeline-run)`. Shell B (fresh): `python lock.py acquire pipeline-run; rc=$?` | rc=75; `sqlite3 SELECT count(*) FROM locks WHERE name='pipeline-run'` returns 1 (only A's row); stderr shows `[lock] held by PID X since T (expires E)`. |
| 8a | Telegram 429 retry | Mock Telegram endpoint returning 429 (Retry-After: 1) twice then 200 | `notify.py` logs 2 retries; final success |
| 8b | extract.py transient retry (in-run + cross-run) | Mock httpx returning transient error (network/5xx) consistently | **Per run:** `_extract_trafilatura` retries up to `MAX_RETRIES` times in-run with exponential backoff, then `_extract_jina` retries up to `MAX_RETRIES`, then `extract_for_row` calls `mark_failed` ONCE — incrementing `retry_count` by 1 per run. **Across runs:** after `MAX_RETRIES` failed runs (i.e., `retry_count >= MAX_RETRIES`), `mark_failed` sets `final_state='discarded'`. The row is then never re-extracted. **Amended 2026-05-12** to reflect the two-level retry model (in-run + cross-run); the original wording conflated them. |
| 9 | Redirect resolution ≥90% | `crawler-validation.md` table | ≥18/20 resolved with per-tier breakdown |
| 10a | MAX_ARTICLES_PER_RUN enforced | Seed DB with 120 extracted-not-scored rows; `python list.py --stage extracted --not-scored --json \| jq length` | output is exactly `MAX_ARTICLES_PER_RUN` (default 50), NOT 120 |
| 10b | Brainstorm flow end-to-end via `/idea-brainstormer` skill (picker + explicit-id modes) | (a) Mode A: `/idea-brainstormer 42` in a fresh Cowork chat — direct id. (b) Mode B: `/idea-brainstormer` (no args) when DB has new articles today — skill calls `list.py --today --score-gte 3 --not-brainstormed`, presents table, user picks id, skill continues. (c) Mode C: `/idea-brainstormer "VinFast Q1"` — skill calls `list.py --search "VinFast Q1"`. | All three modes: Claude reads `brainstorm-guidelines.md` + relevant row content; `mark.py brainstorm` writes valid JSON length 5; `brainstormed_at` set; `notified_at` (if any) preserved. Skill output displays the 5 ideas as a formatted table. |
| 11a | BOOTSTRAP.md happy path on fresh macOS 13+ Apple Silicon without Homebrew/Python | (1) Wipe Homebrew + Python on a test VM. (2) Sign in to Claude Desktop Max. (3) Add folder to Cowork trusted. (4) Open BOOTSTRAP.md, copy entire content, paste into fresh Cowork chat. | Claude runs install.sh with `NONINTERACTIVE=1` (no RETURN prompt), evals brew shellenv so brew is on PATH, installs Python 3.11 + venv + deps + schema. Telegram nonce flow: baseline_update_id captured → user receives nonce → polls `getUpdates?offset=baseline+1` → matches exact nonce → presents detected chat → user confirms → test message sent → user confirms received → .env written. Slash skills symlinked into Cowork's skill dir → typing `/idea` in a new chat autocompletes. **Scheduler verification (§2.1.1 Step 5 adaptive timing):** TEST_BEFORE_TS recorded; `crawl.py --inject-test <TOKEN>`; user clicks "Run now" on SAVED task; Claude polls with two-phase adaptive window (Phase A: 60s for startup-log detection; Phase B: 600s from startup for tokenized notified_at); user confirms Telegram message containing TOKEN. End state: no Terminal touched, slash skills discoverable, .env populated, tokenized Telegram message received, scheduled task saved and exercised end-to-end. |
| 11b | BOOTSTRAP.md on fresh Intel macOS 13+ | Same as 11a but x86_64 architecture | Same outcome with brew at `/usr/local/bin/brew` (arch detection via `uname -m`); shellenv eval uses Intel path. |
| 11c | BOOTSTRAP.md / `/setup` idempotency on configured system — NO duplicate scheduled task (ISSUE-17) | After 11a, run BOOTSTRAP.md again into a new chat (or invoke `/setup`). | Each step detects existing state and skips: install.sh logs `[ok]` lines; Telegram credentials validated, only re-prompted if validation fails; DB schema not re-initialized; Slash skill symlinks not duplicated. **Step 5 prompts user "Task `vinfast-pipeline` đã tồn tại?"; on y → user edits existing task (or confirms prompt matches), does NOT create New Task. End state: exactly ONE scheduled task named `vinfast-pipeline` in Cowork sidebar.** Verification: user counts tasks named `vinfast-pipeline` in sidebar — must be exactly 1, not 2+. |
| 11d | Telegram chat_id deterministic nonce flow with baseline+offset (ISSUE-5 + ISSUE-7 corrected) | Simulate a noisy bot: pre-send 5 random messages to the bot from another account (or via curl) BEFORE running BOOTSTRAP.md. Then paste BOOTSTRAP.md, reach Step 3. Whilst the user is reading the nonce, simulate a 6th random message arriving DURING the polling window. | Step 3.1: Claude records `baseline_update_id` = max of the 5 pre-existing updates. Step 3.4 loop: Claude polls `getUpdates?offset=baseline+1&timeout=10&limit=100` — returns BOTH the 6th random update AND the user's nonce message (whatever order). Loop scans all returned updates, matches the one whose `text.strip() == NONCE` exactly, ignores the others. Correct chat_id extracted. Confirmation prompt → test send → user confirms received. **The 6th concurrent random message does NOT defeat detection.** |
| 11e | Telegram invalid token | Enter "fake123" when prompted | Claude calls getMe → 401 → "Token sai. Mở @BotFather, kiểm tra lại. Paste lại token:" — loop up to 3 attempts then ask user to cancel and retry later. No crash. |
| 11f | Mandatory scheduler verification via SAVED task "Run now" + uniquely-tokenized row + adaptive poll (ISSUE-6/10/11/14/15 corrected) | Paste BOOTSTRAP.md. Reach Step 5. Setup records TEST_BEFORE_TS, runs `crawl.py --inject-test <TOKEN>` where TOKEN is a fresh hex string. **Negative case:** save the scheduled task with an EMPTY prompt body (user mistake). | Claude pre-arms a row carrying TOKEN (visible in title + url='bootstrap-test://<TOKEN>'). Asks user to "Run now". Two-phase adaptive poll: Phase A (≤60s) greps `lock.py acquire pipeline-run` log lines with timestamp > TEST_BEFORE_TS; Phase B (≤600s from startup) polls DB for `notified_at NOT NULL` on the row whose url = `bootstrap-test://<TOKEN>`. With empty prompt body: Phase A may fire (Cowork still spawns) but Phase B never sees notified_at → after 600s setup detects failure → asks user to verify prompt body and re-save → re-arms with NEW token. **Setup does NOT declare success.** Positive case: notified_at populates within Phase B window, user confirms receiving a Telegram message containing the literal TOKEN → success. **Attribution: TOKEN uniqueness + reserved url scheme guarantees the signal can only come from a run that processed THIS specific row.** |
| 11g | Sudo required (Xcode CLT) → Terminal fallback (ISSUE-4) | Fresh macOS without Xcode CLT. Paste BOOTSTRAP.md. | install.sh detects sudo prompt would block (non-interactive shell, no terminal stdin) → exits 78 with `[error] sudo required`. Claude in chat surfaces clear bilingual message + next step: "Mở Terminal, chạy `xcode-select --install` rồi `bash install.sh`, sau đó `python3 setup_helper.py`. Khi xong, quay lại đây paste BOOTSTRAP.md để hoàn tất scheduler." User has zero ambiguity about next action. |
| 11h | Slash skill discovery failed → manual install path documented (ISSUE-1 / ISSUE-4) | BOOTSTRAP.md Step 4 runs `ls "$HOME/Library/Application Support/Claude/skills"` and `ls "$HOME/.claude/skills"` — both fail or skill dir is elsewhere | Claude prints: "Không tự phát hiện thư mục skill của Cowork. Vui lòng xem README mục 'Manual skill install' và chạy lệnh symlink. Sau đó quay lại đây gõ tiếp 'continue'." User runs documented `ln -sf $PWD/SKILLS/idea-brainstormer.skill ...` from README, types "continue" → BOOTSTRAP.md resumes from Step 5. Verification: typing `/idea` in next new chat autocompletes. |
| 11i | Allow-all bash not granted → Terminal fallback (ISSUE-4) | User has Cowork's bash tool gated (asks confirmation per command). Paste BOOTSTRAP.md. | First bash invocation prompts user for permission. If user denies → BOOTSTRAP.md instructs: "Cowork bash bị block. Mở Terminal, cd vào thư mục project, chạy: `bash install.sh && python3 setup_helper.py`. Sau đó quay lại Cowork để paste cowork-task-prompt.md vào /schedule." setup_helper.py covers credential + skill install; user still pastes cowork-task-prompt.md manually. |
| 11j | setup_helper.py end-to-end (Terminal fallback validation) | Run `bash install.sh && python3 setup_helper.py` directly in Terminal on fresh macOS | install.sh succeeds; setup_helper.py runs nonce-based credential flow with `input()` prompts in Vietnamese, writes .env, installs slash skills, prints clear next steps for scheduler. Final stdout: "Setup hoàn tất ngoài scheduler. Mở Cowork, paste nội dung sau vào /schedule: [cowork-task-prompt.md content]". |
| 11k | Non-macOS exit | Paste BOOTSTRAP.md on Linux/Windows Claude Desktop (or simulate `uname=Linux`) | Step 1 detects non-Darwin, prints "Pipeline này hiện chỉ hỗ trợ macOS 13+. Linux/Windows: xem README mục 'Roadmap'." Exits cleanly. |

---

## 8. Known Limitations / Deferred

- **Cowork scheduled tasks do NOT run when Mac is asleep or Claude Desktop is closed.** Cowork catches up ONCE on next app open (not a per-missed-run backfill). If you need 24/7 unattended crawling, the right pivot is hybrid (launchd does crawl+extract; Cowork does score+notify). User accepted Cowork-only for v1.
- No cross-source semantic dedup beyond title-hash. Paraphrased headlines from different papers will both get notified.
- No Telegram bot listener for `/brainstorm <id>` — brainstorm is triggered by talking to Claude in Cowork chat, not by Telegram message.
- No web UI for review — Cowork chat + Telegram + terminal only.
- macOS-only (Cowork requirement).
- Single-user, single-machine. SQLite is fine at this scale.
- Cowork's prompt-rewriting behavior between runs may drift the orchestration. Mitigation: `cowork-task-prompt.md` is canonical and git-tracked; re-paste into Cowork task when behavior diverges.

---

## 9. Open Questions

All resolved by user decision:
- ✅ Cowork as sole runtime (no launchd backup for v1).
- ✅ `scoring-rubric.md` as separate git-tracked file (not in CLAUDE.md, not in task prompt).
- ✅ HTML parse mode for Telegram.
- ✅ 48h title-hash dedup window.
- ✅ No Telegram bot listener (brainstorm via Cowork chat).
