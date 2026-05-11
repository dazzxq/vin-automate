# Cowork Scheduled Task — VinFast News Pipeline

**Paste the ENTIRE content of this file** (from the first `## SETUP` heading below to the bottom) into Cowork's `/schedule` "New Task" prompt field. Set frequency to **Hourly**. Save as task name **`vinfast-pipeline`**.

The scheduled task runs once per hour while Mac is awake and Claude Desktop is open. Each run is a fresh Cowork session: no memory of previous runs.

---

## SETUP — read once at the start of every scheduled run

You are the orchestrator of the VinFast news pipeline. Working directory: this project folder (already granted Cowork access).

### Step 1 — Acquire the run-level lock

Run this bash command and **record the UUID** printed to stdout. This UUID is `<OWNER_ID>` for the rest of this run:

```bash
.venv/bin/python lock.py acquire pipeline-run
```

- If exit code is `0` → success. Capture the UUID from stdout (one line).
- If exit code is `75` → another run is in flight. Abort silently. Do NOT call release. Do NOT continue.

### Step 2 — Read the scoring rubric

Read `scoring-rubric.md` from the project root. It defines the 1–5 scale you will apply in Step 8.

### Step 3 — Crawl new candidates

```bash
.venv/bin/python crawl.py
```

### Step 4 — Heartbeat

```bash
.venv/bin/python lock.py heartbeat pipeline-run <OWNER_ID>
```

### Step 5 — Extract content

```bash
.venv/bin/python extract.py --pending
```

### Step 6 — Heartbeat

```bash
.venv/bin/python lock.py heartbeat pipeline-run <OWNER_ID>
```

### Step 7 — Fetch articles to score

```bash
.venv/bin/python list.py --stage extracted --not-scored --json
```

Parse the JSON output. It is an array of article objects with id, title, source, content.

### Step 8 — Score each article

For each article in the array from Step 7:

1. Read `title` + `content`.
2. Apply `scoring-rubric.md` to assign `score` (integer 1–5) and `reason` (≤200 char Vietnamese).
3. Run:

```bash
.venv/bin/python mark.py score <ID> <SCORE> "<REASON>"
```

Every `HEARTBEAT_EVERY_N_ROWS` rows (default 10), also run a heartbeat:

```bash
.venv/bin/python lock.py heartbeat pipeline-run <OWNER_ID>
```

### Step 9 — Heartbeat before notify

```bash
.venv/bin/python lock.py heartbeat pipeline-run <OWNER_ID>
```

### Step 10 — Fetch articles ready to notify

```bash
.venv/bin/python list.py --stage scored --min-score 3 --not-notified --json
```

### Step 11 — Send Telegram for each

For each article id in Step 10's result, run:

```bash
.venv/bin/python notify.py <ID>
```

### Step 12 — Release the lock (success path)

```bash
.venv/bin/python lock.py release pipeline-run <OWNER_ID>
```

Exit normally.

---

## GLOBAL RULES — apply uniformly to every step

**R1: Heartbeat failure means LOST OWNERSHIP.**
Every `lock.py heartbeat pipeline-run <OWNER_ID>` invocation: if exit code is **75**, **abort the entire run IMMEDIATELY and do NOT call `release`**. Another session has TTL-reclaimed the lock; calling release with our stale `<OWNER_ID>` is a no-op but should not be relied on. Exit silently.

**R2: Tool failure releases the lock then aborts.**
Every non-heartbeat tool invocation (`crawl.py`, `extract.py`, `list.py`, `mark.py`, `notify.py`): if exit code is non-zero AND it is NOT a documented CAS no-op (`mark.py` and `notify.py` log `already scored` / `already notified` and exit 0 — those are normal), then:
1. Run `.venv/bin/python lock.py release pipeline-run <OWNER_ID>` (project interpreter, matching the rest of this prompt).
2. Abort the run.
3. Do NOT continue subsequent steps.

**R3: `<OWNER_ID>` is a placeholder.**
Wherever you see `<OWNER_ID>` in a bash command, substitute the literal UUID you captured from Step 1's stdout. You captured it as a tool result; carry it forward in your context for the rest of this run.

**R4: Do not invent extra steps.**
Run exactly the 12 steps in order. Do not call `mark.py brainstorm` from this task; brainstorm is on-demand only via the `/idea-brainstormer` skill.

**R5: Article cap.**
`list.py` already caps at `MAX_ARTICLES_PER_RUN` (default 50) via its `ORDER BY` priority. Do not pass `--limit` — let the default enforce.

**R6: Verification rows.**
Articles whose URL starts with `bootstrap-test://` are verification rows from the bootstrap flow. They appear first in `list.py` results (priority ORDER BY). Treat them as normal articles for the notify step — they will produce a real Telegram message which the user uses to confirm setup works.

---

## Failure modes covered

| Failure | Behavior |
|---|---|
| Acquire fails (exit 75) | Abort silently, do not call release |
| Any tool exits non-zero (not CAS no-op) | R2: release + abort |
| Heartbeat exits 75 | R1: abort WITHOUT release |
| CAS no-op exit 0 (already scored/notified) | Normal — continue |
| Acquire succeeds but Cowork session crashes mid-run | Lock TTL expires (default 30 min); next scheduled run reclaims |
