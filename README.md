# VinFast News Pipeline

[![macOS 13+](https://img.shields.io/badge/macOS-13%2B-blue)](#requirements)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](#license)
[![Codex-reviewed](https://img.shields.io/badge/codex--reviewed-30%20rounds%2C%2063%20issues-brightgreen)](#code-review-trail)

> Pipeline tự động crawl tin VinFast theo chủ đề, **để Claude trong Cowork chấm điểm 1–5 và brainstorm 5 ideas**, push high-score lên Telegram, lưu SQLite. Chạy mỗi ngày qua Cowork's `/schedule`. Non-tech onboarding: paste `BOOTSTRAP.md` vào Cowork chat — không cần CLI knowledge.

---

## Mục lục

- [TL;DR](#tldr)
- [Tại sao tồn tại](#tại-sao-tồn-tại)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Quickstart (3 đường)](#quickstart-3-đường)
- [Daily usage](#daily-usage)
- [File layout](#file-layout)
- [Cost](#cost)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Roadmap](#roadmap)
- [Development & contributing](#development--contributing)
- [Code review trail](#code-review-trail)
- [License](#license)

---

## TL;DR

```bash
# 1. Cài Claude Desktop (Pro/Max). Add folder này vào Cowork trusted folders.
# 2. Mở Cowork chat mới, paste TOÀN BỘ nội dung BOOTSTRAP.md.
# 3. Claude tự cài Homebrew + Python + venv + schema, hỏi token Telegram qua
#    deterministic nonce flow, install slash skills, verify scheduler end-to-end.
# Done. Pipeline chạy mỗi ngày (Cowork Daily).
```

Sau đó:
- Nhận tin Telegram khi có bài VinFast score >= 3.
- Mở Cowork chat, gõ `/idea-brainstormer 42` để gen 5 ideas cho bài #42.

---

## Tại sao tồn tại

Nếu bạn là **biên tập viên / phóng viên công nghệ Việt Nam** theo dõi sát thị trường VinFast, mỗi sáng phải làm việc lặp đi lặp lại:

1. Mở 5-7 báo (VnExpress, TuoiTre, Thanh Nien, Electrek, InsideEVs...) xem có tin VinFast mới không.
2. Đánh giá tin nào hay (5/5), tin nào nhạt (1/5).
3. Với tin hay, brainstorm góc khai thác.

Pipeline này tự động hóa **bước 1 và 2**, và làm **bước 3 on-demand** khi bạn muốn:

- **Crawl**: Google News RSS query "VinFast" + 5 RSS feeds Việt + quốc tế, dedup theo URL và title cross-source.
- **Score**: Claude (Sonnet/Opus tier qua Pro/Max plan của bạn) chấm 1-5 theo `scoring-rubric.md` (GenK editorial voice).
- **Notify**: Tin >= 3/5 → push Telegram cho bạn xem trong vài giây.
- **Brainstorm**: Khi thấy tin hay, gõ `/idea-brainstormer <id>` trong Cowork → Claude gen 5 ideas chi tiết.

**Không subprocess `claude -p`, không Anthropic SDK, không API key.** Claude trong Cowork IS the runtime — bạn xài quota Pro/Max sẵn có.

---

## Architecture

```
                  ┌─────────────────────────────────────────────────┐
                  │  Claude Desktop (Pro/Max)                       │
                  │                                                 │
                  │   ┌─────────────────────────────────────────┐   │
                  │   │ Cowork Scheduled Task (daily)           │   │
                  │   │  ├─ Read scoring-rubric.md              │   │
                  │   │  ├─ lock.py acquire pipeline-run        │   │
                  │   │  ├─ Bash: python crawl.py               │───┼─▶ Google News RSS
                  │   │  ├─ Bash: python extract.py --pending   │───┼─▶ VN/EV RSS feeds
                  │   │  ├─ Bash: python list.py --json         │   │       (trafilatura
                  │   │  ├─ Claude reasons score 1-5            │   │        + Jina)
                  │   │  ├─ Bash: python mark.py score          │   │
                  │   │  ├─ Bash: python notify.py <id>         │───┼─▶ Telegram bot
                  │   │  └─ lock.py release pipeline-run        │   │
                  │   └─────────────────────────────────────────┘   │
                  │                                                 │
                  │   ┌─────────────────────────────────────────┐   │
                  │   │ /idea-brainstormer 42 (on-demand chat)  │   │
                  │   │   → list.py + brainstorm-guidelines     │   │
                  │   │   → Claude reasons 5 ideas (JSON)       │   │
                  │   │   → mark.py brainstorm 42 '<json>'      │   │
                  │   └─────────────────────────────────────────┘   │
                  └─────────────────┬───────────────────────────────┘
                                    │ Bash tool calls
                                    ▼
                            ┌───────────────────┐
                            │  Python tools     │ ── No AI here.
                            │   (deterministic) │
                            │                   │
                            │  crawl.py         │
                            │  extract.py       │
                            │  notify.py        │
                            │  mark.py          │
                            │  list.py          │
                            │  lock.py          │
                            │  db.py            │
                            └─────────┬─────────┘
                                      │
                                      ▼
                            ┌───────────────────┐
                            │  news.db (SQLite) │
                            │                   │
                            │  articles table   │
                            │  - per-stage      │
                            │    timestamps     │
                            │  - retry_count    │
                            │  - final_state    │
                            │                   │
                            │  locks table      │
                            │  - SQLite mutex   │
                            │    + TTL          │
                            └───────────────────┘
```

**Hai trục chính:**

1. **AI = Claude in Cowork.** Python modules là pure deterministic tools (crawl, extract, DB writes, Telegram POST). Không có "scorer.py" hay "brainstorm.py" gọi API — Claude tự reason theo `scoring-rubric.md` / `brainstorm-guidelines.md` trong agentic loop.

2. **State = SQLite single file.** Articles có per-stage timestamps (extracted_at, scored_at, notified_at, brainstormed_at, failed_at), không phải single status enum. Orthogonal facts — một bài có thể vừa notified vừa brainstormed.

**Concurrency model:**

- Run-level SQLite mutex với TTL + heartbeat (`locks` table). 2 Cowork sessions overlap → session B's acquire exit 75 + abort cleanly.
- Per-row compare-and-set trong `mark.py` / `notify.py` / `extract.py` → 2 sessions không thể double-score hay double-send Telegram cho cùng 1 row.
- Owner_id qua Claude's session memory (không file persistence): acquire prints UUID, Claude capture từ tool result, substitute literal UUID vào mỗi heartbeat/release.

Đọc `PLAN.md` để hiểu rõ design decisions (Codex-approved sau 23 review rounds, 47 issues resolved).

---

## Requirements

- **macOS 13+** (Ventura hoặc mới hơn). Linux/Windows: roadmap.
- **Claude Desktop** với **Pro hoặc Max plan**. Free plan không có Cowork.
- **Cowork** đã grant folder access cho project folder.
- **Allow-all bash** + allow-all network trong Cowork (1 lần grant).
- **Internet** cho RSS fetch + Telegram API + Homebrew/PyPI lần setup đầu.

**KHÔNG yêu cầu:**
- ❌ Anthropic API key
- ❌ Knowledge về CLI / shell / Python
- ❌ launchd / cron / external scheduler

---

## Quickstart (3 đường)

### Path A — Cowork-first 🚀 (Recommended)

1. Tải Claude Desktop từ https://claude.ai/download và đăng nhập Pro/Max.
2. Clone repo này (hoặc download ZIP, giải nén). Mở Claude Desktop:
   - Cowork tab → Settings → Folder access → **Add this folder**.
   - Bash tool & Network tool: chọn **Allow all** (toggle 1 lần).
3. Mở `BOOTSTRAP.md`, copy toàn bộ nội dung.
4. Trong Cowork tab, mở chat mới (`+ New chat`).
5. Paste vào chat. Claude tự chạy 6 bước setup (~5-10 phút):
   - OS + arch detection
   - `install.sh` (auto Homebrew + Python 3.11 + venv + deps + schema)
   - Telegram credentials (BotFather walkthrough + nonce-based chat_id detect)
   - Slash skills install (symlink vào Cowork's skill dir)
   - **MANDATORY scheduler verification**: pre-arm tokenized test row → user click "Run now" trên saved task → poll Phase A (60s startup) + Phase B (10min completion) → user confirm Telegram message chứa TOKEN
   - Completion summary

Done. Pipeline tự chạy mỗi ngày.

### Path B — Terminal fallback

Dùng nếu Cowork bị block (sudo prompt cho Xcode CLT, allow-all bash chưa grant, etc.):

```bash
git clone https://github.com/dazzxq/vin-automate.git
cd vin-automate
bash install.sh                    # auto deps + schema
.venv/bin/python setup_helper.py   # interactive Telegram setup + skills install
```

`setup_helper.py` in ra `cowork-task-prompt.md` content và hướng dẫn paste vào Cowork's `/schedule` UI (Cowork's scheduled-task UI không thể tự động hóa từ bash).

### Path C — Manual skill install (cuối cùng)

Nếu `/idea-brainstormer` không autocomplete sau Path A hoặc B:

```bash
# Tìm thư mục skill (một trong hai):
SKILL_DIR="$HOME/Library/Application Support/Claude/skills"
# Hoặc: SKILL_DIR="$HOME/.claude/skills"

mkdir -p "$SKILL_DIR/idea-brainstormer" "$SKILL_DIR/setup"
ln -sf "$PWD/SKILLS/idea-brainstormer.skill" "$SKILL_DIR/idea-brainstormer/SKILL.md"
ln -sf "$PWD/SKILLS/setup.skill" "$SKILL_DIR/setup/SKILL.md"
```

Restart Cowork session và gõ `/idea` — autocomplete sẽ thấy.

---

## Daily usage

### Nhận alert tin VinFast

Telegram bot tự gửi tin format:

```
[#42] 🔴 score 5/5 | VnExpress

VinFast công bố Q1/2026 vượt expect tại thị trường VN

Số liệu cụ thể, góc khai thác market share VinFast vs Tesla...

Đọc bài
Brainstorm: /idea-brainstormer 42
```

Copy số `[#42]` (article ID).

### Brainstorm 5 ideas

Mở Claude Desktop → Cowork chat → gõ:

| Lệnh | Tác dụng |
|---|---|
| `/idea-brainstormer 42` | Brainstorm article id 42 trực tiếp |
| `/idea-brainstormer` | Hiện top candidates hôm nay, hỏi pick id nào |
| `/idea-brainstormer "VF8"` | Search title/content theo keyword |
| `/idea-brainstormer score=5` | Hiện chỉ tin score=5 chưa brainstorm |
| `/idea-brainstormer yesterday` | Hiện tin hôm qua |

Claude gen 5 ideas theo `brainstorm-guidelines.md`, lưu vào DB, hiển thị bảng so sánh format/difficulty/viral.

### Re-config sau (đổi bot, refresh skills, etc.)

Gõ `/setup` trong bất kỳ Cowork chat nào.

### Kiểm tra DB từ terminal (optional)

```bash
.venv/bin/python list.py                                          # Hôm nay, top
.venv/bin/python list.py --stage notified --limit 20              # Đã push Telegram
.venv/bin/python list.py --stage brainstormed --json | jq         # Đã brainstorm
.venv/bin/python list.py --search "VF8 Mỹ"                        # Tìm keyword
sqlite3 news.db "SELECT count(*) FROM articles WHERE final_state='discarded'"
```

---

## File layout

```
vin-automate/
├── PLAN.md                  Design doc (Codex-approved, 23 review rounds)
├── BOOTSTRAP.md             ★ PRIMARY onboarding — paste vào Cowork chat
├── README.md                File này
├── install.sh               Bash installer (Homebrew, Python, venv, schema)
├── setup_helper.py          Terminal-fallback TUI
├── requirements.txt         Pinned Python deps (no Anthropic SDK)
├── .env.example             Template (chỉ TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
├── .env                     (gitignored) Real secrets
├── news.db                  (gitignored) SQLite state
│
├── config.py                Env load, TOPICS, MIN_SCORE_TO_NOTIFY, tunables
├── db.py                    Schema + helpers (CAS, RO mode, URL/title canonical)
├── crawl.py                 RSS + 4-tier redirect resolver + dedup + --inject-test
├── extract.py               trafilatura + Jina fallback, two-level retry
├── notify.py                Telegram HTML + CAS + 429 retry
├── mark.py                  CLI: score / brainstorm / discard / archive
├── list.py                  Query DB, --json output, ORDER BY priority bootstrap-test://
├── lock.py                  SQLite mutex + TTL + heartbeat (owner_id via caller memory)
├── _logging.py              Shared RotatingFileHandler → logs/pipeline.log
│
├── scoring-rubric.md        1-5 thang điểm GenK voice (Vietnamese)
├── brainstorm-guidelines.md 5-idea JSON contract
├── cowork-task-prompt.md    Pasteable vào Cowork /schedule
│
├── SKILLS/
│   ├── idea-brainstormer.skill   Slash skill: brainstorm 5 ideas (3 modes)
│   └── setup.skill               Slash skill: re-run setup
│
├── E2E-VALIDATION.md        Validation matrix (status legend)
├── crawler-validation.md    Redirect resolver per-tier success rate report
│
└── logs/                    Rotating logs (gitignored except .gitkeep)
    └── pipeline.log
```

---

## Cost

| Item | Marginal cost |
|---|---|
| Claude API | **$0** — Cowork uses Pro/Max plan (no SDK, no API key) |
| Pro/Max subscription | Already paid by user |
| Telegram Bot API | Free |
| Jina Reader (extraction fallback) | Free tier 1M tokens/month — more than enough |
| Homebrew / Python / disk | Free |
| **Total** | **$0/month** |

**Risk:** Cowork has a 5-hour rolling quota on Pro/Max plans. Pipeline hard-caps `MAX_ARTICLES_PER_RUN=50` rows per scheduled run to prevent burning the entire quota on one giant scoring burst.

---

## Troubleshooting

### Telegram 400 Bad Request

Title contains special chars breaking HTML parser. `notify.py` already calls `html.escape()` — if vẫn vỡ, kiểm tra raw payload:
```bash
.venv/bin/python -c "import db, notify; row = db.get_article(42); print(notify._build_message(row))"
```

### Redirect resolution thất bại

Google News URLs trả về Google's bridge page thay vì source. `crawl.py` resolve qua 4-tier cascade. Tier 3 (Google News base64 decoder) hiện là no-op vì Google đã đổi format → fallback sang tier 4 (giữ Google URL → Jina Reader handle ở extract stage). End-to-end vẫn work cho Google News URLs.

### Mac sleep / Claude Desktop closed

Cowork scheduled task **chỉ chạy** khi Mac thức + Claude Desktop mở. Sleep qua đêm → miss run. Khi mở lại app, Cowork catch-up **1 lần** (không backfill từng ngày trước đó).

Mitigation:
- System Settings → Battery → "Prevent automatic sleeping when display is off"
- Hoặc mở Claude Desktop khi check tin sáng

### Cowork quota exhausted

Max plan có 5-hour rolling quota. Nếu cạn, tạm dừng task trong Cowork sidebar → đợi quota refresh → resume.

### Stale lock recovery

Nếu Cowork session crash giữa chừng và để lại lock row, TTL = 30 phút → tự expire. Run kế tiếp tự reclaim.

Manual override:
```bash
sqlite3 news.db "DELETE FROM locks WHERE name='pipeline-run'"
```

### Slash skill không autocomplete

1. Verify symlink:
   ```bash
   ls -la ~/Library/Application\ Support/Claude/skills/idea-brainstormer/
   ```
2. Nếu không, cài thủ công theo Path C.
3. Restart Claude Desktop session.

### Re-run BOOTSTRAP.md tạo duplicate scheduled task?

Bước 5.2 hỏi: "Task `vinfast-pipeline` đã tồn tại?" → y thì EDIT, không tạo mới. Nếu lỡ tạo duplicate, vào Cowork sidebar, xóa task trùng, giữ 1.

### Tin nhiều báo cùng đưa → spam Telegram?

`crawl.py` dedup theo `title_hash` trong 48h window. Tin paraphrase (title khác chữ) vẫn lọt → known limitation. Có thể upgrade thành simhash.

### Telegram bot không nhận tin nhắn lúc setup nonce flow

- Đã bấm `/start` cho bot trong Telegram chưa?
- Có gửi đúng nonce string (copy nguyên văn)?
- BotFather token vẫn còn hiệu lực? (test: `curl https://api.telegram.org/bot<TOKEN>/getMe`)

---

## Known limitations

- **macOS only.** Cowork là Claude Desktop feature, không có trên Linux/Windows hiện tại.
- **Single user, single machine.** SQLite không multi-write reliable across hosts.
- **Mac sleep ⇒ miss runs.** Cowork không có background daemon — chỉ catch-up 1 lần khi mở app.
- **Title-hash dedup only.** Tin paraphrase (cùng nội dung, title khác) sẽ qua dedup.
- **No web UI for review.** Telegram + Cowork chat + terminal only.
- **Google News tier-3 decoder is a no-op.** Tier 4 (Jina Reader) handles instead. Strict tier 1-3 rate ~40%; effective downstream-usable rate 100%.
- **Cowork quota.** Max plan 5-hour rolling cap. Pipeline self-throttles via `MAX_ARTICLES_PER_RUN=50`.

---

## Roadmap

**v1 (current):**
- ✅ Cowork-first onboarding
- ✅ Per-stage timestamps state model
- ✅ SQLite mutex + TTL + heartbeat
- ✅ Compare-and-set CAS on all writes
- ✅ Reserved URL scheme for verification rows
- ✅ Deterministic nonce flow for chat_id
- ✅ Two-level retry (in-run httpx + cross-run row retry_count)

**v1.x (potential):**
- ⬜ Google News protobuf URL decoder (tier 3 → ≥90% strict)
- ⬜ Title simhash dedup (cross-source paraphrase detection)
- ⬜ Web review UI (read-only article browser)
- ⬜ Multi-topic separation (topic_id column on articles)
- ⬜ Daily digest .md (top-N articles, for cà phê sáng)
- ⬜ Telegram bot listener for inline `/brainstorm <id>` command

**v2 (speculative):**
- ⬜ Linux support (replace Cowork with `claude -p` CLI as fallback)
- ⬜ Multi-user / shared DB
- ⬜ Auto-publish to CMS (after human approval)

---

## Development & contributing

Project có **CLAUDE.md workflow** (xem repo root): mỗi commit phải qua `/codex-impl-review` (adversarial review giữa Claude + Codex CLI) trước khi commit.

### Per-commit workflow

```
1. Implement task
2. Smoke test
3. /codex-impl-review (Claude calls Codex CLI for adversarial review)
4. Fix all valid issues
5. Re-review until Codex APPROVE
6. git commit
```

Quá trình implement codebase này:
- **Plan phase** (PLAN.md): 23 review rounds, 47 issues resolved.
- **Task 1 impl phase**: 2 review rounds, 3 issues resolved.
- **Tasks 2-18 impl phase**: 7 review rounds, 16 issues resolved.
- **Tổng**: 30 review rounds, 63 issues found and fixed before merge.

### Smoke test locally

```bash
bash install.sh                                    # idempotent
.venv/bin/python -c "from db import init_db; init_db()"

# Lock test
OWNER=$(.venv/bin/python lock.py acquire test-lock --ttl 10)
.venv/bin/python lock.py heartbeat test-lock "$OWNER"
.venv/bin/python lock.py release test-lock "$OWNER"

# Crawl dry-run
.venv/bin/python crawl.py --dry-run --topic vinfast

# Inject + list
.venv/bin/python crawl.py --inject-test $(.venv/bin/python -c 'import secrets; print(secrets.token_hex(6))')
.venv/bin/python list.py --stage scored --not-notified
```

### Test bằng dữ liệu thật (cần Telegram credentials)

Sau khi đã setup .env qua BOOTSTRAP.md hoặc setup_helper.py:

```bash
# Force-trigger pipeline end-to-end
.venv/bin/python crawl.py --topic vinfast
.venv/bin/python extract.py --pending
# (manually score top row via mark.py since AI scoring requires Cowork)
.venv/bin/python list.py --stage scored --not-notified
.venv/bin/python notify.py <id>     # send to Telegram
```

---

## Code review trail

Mỗi commit trong repo này đã qua adversarial review giữa Claude (Opus 4.7) và Codex CLI. Session artifacts ở `.codex-review/` (gitignored). Verdict log:

| Phase | Rounds | Issues raised | Issues resolved |
|---|---|---|---|
| Plan review (PLAN.md) | 16 + 7 | 30 + 17 | 30 + 17 |
| Task 1 impl review | 2 | 3 | 3 |
| Tasks 2-18 impl review | 7 | 16 | 16 |
| **Total** | **32** | **66** | **66** |

Notable architectural decisions (caught + corrected during plan phase):
1. **Owner_id via Claude session memory, NOT shared file.** Earlier attempt to share via `logs/current-run.owner` broke ownership isolation when a TTL-expired session A wakes after session B acquired. Fixed by having Claude capture UUID from acquire stdout and substitute as literal CLI arg in subsequent heartbeat/release.
2. **Reserved URL scheme `bootstrap-test://`.** For scheduler verification we inject a tokenized row with `url=bootstrap-test://<HEX>`. crawl.py REJECTS any RSS URL with this scheme to prevent collision. list.py ORDER BY puts these rows first so verification doesn't starve behind backlog.
3. **Two-level retry.** Per-run httpx retries (transient errors only) + cross-run row retry_count → final_state='discarded' after `MAX_RETRIES`.
4. **No `--score-gte` duplicate.** Argparse refused conflicting option strings; aliased under `--min-score`.
5. **macOS-native `shasum -a 256`** not `sha256sum` (not on stock macOS).
6. **NONINTERACTIVE=1 Homebrew install + arch-aware `BREW_PREFIX` + explicit `eval shellenv`.**
7. **Deterministic nonce flow** for chat_id detection: baseline_update_id + offset=baseline+1 polling (NOT offset=-1 which only returns latest update).
8. **MANDATORY scheduler verification via "Run now" on SAVED task**, not in-chat manual pipeline. Pre-armed tokenized row + dual-evidence poll + Telegram receipt confirmation.

---

## License

MIT. Use at own risk.

---

## Credits

- Architecture: collaborative design between user + Claude Opus 4.7
- Adversarial review: Codex CLI (OpenAI), 30+ rounds
- Editorial voice: GenK.vn
- Reverse-engineered Cowork affordances + scheduling: Anthropic Help Center docs
