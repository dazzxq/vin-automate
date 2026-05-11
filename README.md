# VinFast News Pipeline

Local-first news aggregation pipeline:
- Crawls VinFast-related news from Google News RSS + curated VN sources.
- Extracts content via trafilatura → Jina Reader fallback.
- **Claude in Cowork** scores 1–5 and brainstorms ideas (no API key, no SDK — uses your Pro/Max plan).
- Pushes high-score articles to Telegram for human review.
- Runs hourly via Cowork's `/schedule` while Claude Desktop is open.

**Target user: non-technical.** Paste `BOOTSTRAP.md` into a Cowork chat, follow conversational prompts. No CLI knowledge required.

**Requirements:** macOS 13+ (Ventura or later), Claude Desktop with Pro/Max plan.

---

## Quickstart (3 paths)

### Path A — Cowork-first (Recommended) 🚀

1. Tải Claude Desktop từ https://claude.ai/download và đăng nhập Pro/Max.
2. Đặt thư mục project này lên máy. Trong Claude Desktop:
   - Mở Cowork (sidebar)
   - Settings → Folder access → **Add this folder**.
3. Mở `BOOTSTRAP.md`, copy toàn bộ nội dung.
4. Mở một Cowork chat mới (`+ New chat` trong Cowork).
5. Paste vào chat. Claude tự thực hiện 6 bước setup (OS check → install.sh → Telegram nonce → skill install → scheduled task verification).
6. Cuối Bước 5 bạn sẽ nhận một tin nhắn Telegram test có chứa TOKEN xác nhận pipeline hoạt động end-to-end.

Done. Pipeline sẽ tự chạy mỗi giờ.

### Path B — Terminal fallback

Dùng nếu Cowork bị block (sudo prompt, allow-all bash chưa grant):

```bash
cd /path/to/this/folder
bash install.sh
.venv/bin/python setup_helper.py
```

Sau đó vẫn cần mở Cowork một lần để paste `cowork-task-prompt.md` vào `/schedule` New Task (Cowork's scheduled-task UI không bash-controllable). `setup_helper.py` in ra hướng dẫn cụ thể ở cuối.

### Path C — Manual skill install (cuối cùng)

Nếu skill autocomplete `/idea-brainstormer` không hoạt động sau Path A hoặc B, cài thủ công:

```bash
# Tìm thư mục skill (một trong hai):
SKILL_DIR="$HOME/Library/Application Support/Claude/skills"
# Hoặc: SKILL_DIR="$HOME/.claude/skills"

mkdir -p "$SKILL_DIR/idea-brainstormer" "$SKILL_DIR/setup"
ln -sf "$PWD/SKILLS/idea-brainstormer.skill" "$SKILL_DIR/idea-brainstormer/SKILL.md"
ln -sf "$PWD/SKILLS/setup.skill" "$SKILL_DIR/setup/SKILL.md"
```

Sau đó restart Cowork session và gõ `/idea` — autocomplete sẽ thấy.

---

## Daily usage

### Nhận alert tin VinFast

Telegram bot tự gửi tin có dạng:

```
[#42] 🔴 score 5/5 | VnExpress

VinFast công bố Q1/2026 vượt expect tại thị trường VN

Số liệu cụ thể, góc khai thác market share VinFast vs Tesla...

Đọc bài
Brainstorm: /idea-brainstormer 42
```

Copy số ID (vd `42`) trong dấu `[#...]`.

### Brainstorm idea

Mở Claude Desktop → Cowork chat → gõ một trong:

| Lệnh | Tác dụng |
|---|---|
| `/idea-brainstormer 42` | Brainstorm article id 42 trực tiếp |
| `/idea-brainstormer` | Hiện top candidates hôm nay, hỏi pick id nào |
| `/idea-brainstormer "VF8"` | Search title/content theo keyword |
| `/idea-brainstormer score=5` | Hiện chỉ tin score=5 chưa brainstorm |
| `/idea-brainstormer yesterday` | Hiện tin hôm qua |

Claude sẽ gen 5 idea theo `brainstorm-guidelines.md`, lưu vào DB, hiển thị bảng so sánh.

### Re-config sau

Gõ `/setup` trong bất kỳ Cowork chat nào.

---

## File layout

| File | Mục đích |
|---|---|
| `BOOTSTRAP.md` | **Pasteable prompt cho Cowork — onboarding lần đầu** |
| `install.sh` | Bash installer (Homebrew + Python + venv + deps + schema) |
| `setup_helper.py` | Terminal TUI fallback (credential + skill install) |
| `cowork-task-prompt.md` | Paste vào Cowork `/schedule` để chạy pipeline hourly |
| `config.py` | Topics, thresholds, tunables (đọc `.env`) |
| `db.py` | SQLite schema + helpers |
| `crawl.py` | RSS fetch, redirect resolve, dedup, insert |
| `extract.py` | trafilatura → Jina fallback |
| `lock.py` | SQLite-based run-level mutex với TTL + heartbeat |
| `notify.py` | Telegram push (HTML mode) |
| `mark.py` | CLI write per-stage timestamps (score/brainstorm/discard/archive) |
| `list.py` | Query DB; `--json` output cho Claude |
| `SKILLS/idea-brainstormer.skill` | Cowork slash skill: brainstorm 5 ideas |
| `SKILLS/setup.skill` | Cowork slash skill: re-run setup |
| `scoring-rubric.md` | Thang điểm 1-5 GenK editorial |
| `brainstorm-guidelines.md` | Format spec cho 5 ideas |
| `PLAN.md` | Design doc (Codex-approved) |
| `news.db` | SQLite state (gitignored) |
| `logs/` | Pipeline logs (gitignored) |
| `.env` | Telegram secrets (gitignored) |

---

## Troubleshooting

### Telegram 400 Bad Request

Title chứa ký tự đặc biệt làm HTML parser vỡ. `notify.py` đã `html.escape()` — nếu vẫn vỡ, kiểm tra raw payload bằng:
```bash
.venv/bin/python -c "import db, notify; row = db.get_article(42); print(notify._build_message(row))"
```

### Redirect resolution thất bại

Google News URL có thể trả về Google's redirect page thay vì source. `crawl.py` resolve qua 4-tier (HEAD → GET stream → base64 decode → Jina fallback). Nếu success rate < 90% sau Task 4 validation, mở issue.

### Mac sleep / Claude Desktop closed

Cowork scheduled task **chỉ chạy** khi Mac thức + Claude Desktop mở. Sleep qua đêm → miss runs. Khi mở lại app, Cowork catch-up **1 lần** (không backfill từng giờ). Đây là constraint của Cowork, không phải bug.

Để giảm miss runs:
- Đặt Mac không ngủ (`System Settings → Battery → "Prevent automatic sleeping when display is off"`).
- Hoặc mở Claude Desktop khi cần kiểm tra.

### Cowork quota exhausted

Max plan có 5-hour rolling quota. Pipeline đã hard-cap `MAX_ARTICLES_PER_RUN=50` để không burn quota. Nếu vẫn cạn, tạm dừng task trong Cowork → đợi quota refresh → resume.

### Stale lock recovery

Nếu một lần Cowork session crash giữa chừng và để lại lock row trong DB, lock tự expire sau `LOCK_TTL_SECONDS` (mặc định 30 phút). Run kế tiếp sẽ tự reclaim.

Manual override (khi muốn force-unlock ngay):
```bash
sqlite3 news.db "DELETE FROM locks WHERE name='pipeline-run'"
```

### Slash skill không autocomplete

1. Verify symlink tồn tại:
   ```bash
   ls -la ~/Library/Application\ Support/Claude/skills/idea-brainstormer/
   ```
2. Nếu không, cài thủ công theo Path C ở trên.
3. Restart Claude Desktop session.

### Re-run BOOTSTRAP.md tạo duplicate scheduled task?

Bước 5.2 của BOOTSTRAP.md hỏi user trước: "Task `vinfast-pipeline` đã tồn tại?" — nếu y thì EDIT, không tạo mới. Nếu lỡ tạo duplicate, vào Cowork sidebar, xóa các task trùng tên, giữ 1.

### Tin nhiều báo cùng đưa → spam Telegram?

`crawl.py` dedup theo `title_hash` trong 48h window. Tin paraphrase (title khác chữ) vẫn lọt — đây là known limitation. Có thể upgrade thành simhash nếu cần.

---

## Architecture summary

- **Runtime:** Claude in Cowork (không phải subprocess `claude -p`, không Anthropic API).
- **Scheduler:** Cowork's `/schedule` (không phải launchd).
- **State:** SQLite `news.db` với per-stage timestamps + `locks` table.
- **Concurrency:** SQLite mutex với TTL + heartbeat + compare-and-set trong mark.py/notify.py.
- **Owner_id propagation:** Claude's session memory (không shared file).
- **Verification:** Pre-armed tokenized row + Cowork "Run now" + Telegram delivery.

Xem `PLAN.md` để hiểu rõ design decisions (Codex-approved sau 23 review rounds, 47 issues resolved).

---

## Known limitations

- **macOS only** (Cowork requirement).
- **Single user, single machine.**
- **Mac sleep ⇒ miss runs** (Cowork catch-up 1 lần khi mở lại).
- **Title-hash dedup only** (paraphrased cross-source duplicates pass through).
- **No web UI for review** — Telegram + Cowork chat + terminal only.

---

## Development / contributing

Mỗi task implement xong → `/codex-impl-review` qua Codex CLI để adversarial review trước khi commit. Xem `.codex-review/` cho session history (gitignored).
