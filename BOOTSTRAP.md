# BOOTSTRAP.md — One-time onboarding for VinFast News Pipeline

**Cách dùng:** Copy TOÀN BỘ nội dung file này (từ heading `## SYSTEM` bên dưới đến hết) và paste vào một Cowork chat mới trong Claude Desktop. Claude sẽ tự động thực hiện setup end-to-end.

**Yêu cầu:** macOS 13+, Claude Desktop với Pro/Max plan, Cowork đã grant folder access cho project này, allow-all bash + allow-all network.

**Fallback:** Nếu Cowork không chạy được (sudo, allow-all bị block), mở Terminal và chạy:

```bash
cd /path/to/this/folder
bash install.sh
.venv/bin/python setup_helper.py
```

---

## SYSTEM

Bạn là agent setup cho VinFast News Pipeline. Working directory = thư mục project này (đã được Cowork grant access). Bạn sẽ thực hiện 6 bước theo thứ tự. **Không tự sáng tạo thêm bước.**

### Step 1 — OS + arch check

```bash
echo "OS: $(uname -s)"
echo "Version: $(sw_vers -productVersion 2>/dev/null)"
echo "Arch: $(uname -m)"
```

Kiểm tra:
- OS phải là `Darwin`. Nếu không → in: "Pipeline chỉ hỗ trợ macOS 13+. Bạn đang dùng [OS]. Setup dừng tại đây." Stop.
- Version major phải ≥ 13. Nếu không → tương tự, stop.
- Ghi nhận arch (`arm64` hoặc `x86_64`) để biết BREW_PREFIX (Step 2 tự xử lý).

### Step 2 — Run install.sh

```bash
bash install.sh
```

Đọc output:
- Nếu thấy `[ok]` cho mọi step và exit code 0 → success, qua Step 3.
- Nếu exit code 78 (sudo required for Xcode CLT) → in: "Cần Xcode Command Line Tools một lần. Mở Terminal, chạy `xcode-select --install`, sau đó quay lại đây paste BOOTSTRAP.md lại." Stop.
- Nếu lỗi khác → in error rồi stop, hướng dẫn user check output.

### Step 3 — Telegram credentials (deterministic nonce flow)

Đọc `.env` xem đã có credentials chưa:

```bash
test -f .env && grep -E '^TELEGRAM_(BOT_TOKEN|CHAT_ID)=.+' .env
```

- Nếu cả 2 đã có VÀ validate được (curl getMe + sendMessage test) → skip to Step 4.
- Nếu chưa hoặc invalid → tiến hành nonce flow:

#### 3.1 — Hướng dẫn user tạo bot

Hỏi user: "Bạn đã có Telegram bot chưa? [y/N]"

- Nếu **N** → hướng dẫn:
  ```
  1. Mở https://t.me/BotFather trong Telegram.
  2. Gõ /newbot
  3. Đặt tên bot (vd "VinFast News")
  4. Đặt username kết thúc bằng "bot" (vd "vinfast_news_bot")
  5. BotFather sẽ gửi cho bạn một TOKEN dạng: 123456789:ABCdef-GhIjKlMnOpQrStUvWxYz
  6. Copy token đó.
  ```

#### 3.2 — Nhận và validate token

Hỏi user paste token. Sau khi user paste:

```bash
TOKEN="<paste-của-user>"
curl -s "https://api.telegram.org/bot${TOKEN}/getMe"
```

- Nếu response chứa `"ok":true` → token valid, lưu vào biến.
- Nếu `"ok":false` hoặc HTTP 401 → tell user "Token sai. Kiểm tra lại với @BotFather". Hỏi paste lại. Max 3 lần.

#### 3.3 — Deterministic chat_id detection

```bash
# Step 3.3a: baseline_update_id
BASELINE=$(curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates?timeout=0&limit=100" | python3 -c 'import json,sys; d=json.load(sys.stdin); ids=[u["update_id"] for u in d.get("result",[])]; print(max(ids) if ids else 0)')

# Step 3.3b: generate nonce
NONCE=$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(6))')
echo "NONCE: $NONCE"
```

Hiển thị NONCE prominently cho user. Hướng dẫn user:

```
1. Mở Telegram, tìm bot bạn vừa tạo.
2. Bấm /start (lần đầu).
3. Sau đó gửi chính xác chuỗi sau vào bot: [NONCE]
```

#### 3.3c — Poll cho nonce (deadline 120s)

```bash
# Pseudo-code, implement in bash + python helper
DEADLINE=$(($(date +%s) + 120))
CHAT_ID=""
CHAT_NAME=""

while [ $(date +%s) -lt $DEADLINE ]; do
  RESULT=$(curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates?offset=$((BASELINE+1))&timeout=10&limit=100")
  # Parse with python, find update with message.text.strip() == NONCE
  PAIR=$(echo "$RESULT" | python3 -c "
import json, sys
data = json.load(sys.stdin)
NONCE = '$NONCE'
for u in data.get('result', []):
    msg = u.get('message', {})
    if msg.get('text', '').strip() == NONCE:
        chat = msg['chat']
        name = chat.get('first_name') or chat.get('title') or 'unknown'
        print(f\"{chat['id']}|{name}\")
        break
")
  if [ -n "$PAIR" ]; then
    CHAT_ID="${PAIR%|*}"
    CHAT_NAME="${PAIR#*|}"
    break
  fi
  sleep 2
done

if [ -z "$CHAT_ID" ]; then
  echo "Timeout 120s — không nhận được nonce. Bạn đã gửi nonce vào đúng bot chưa?"
  # Hỏi user retry với nonce mới
fi
```

#### 3.3d — Confirm with user

Hỏi: "Phát hiện chat: **${CHAT_NAME}** (id=${CHAT_ID}). Xác nhận đây là chat của bạn? [y/N]"

- On **N** → hỏi lý do (gửi nonce sang bot khác?). Restart Step 3.3 với nonce mới.
- On **y** → tiếp Step 3.4.

#### 3.4 — Test send

```bash
curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  -d "chat_id=${CHAT_ID}" \
  -d "text=Setup OK — VinFast pipeline ready."
```

Hỏi user: "Bạn vừa nhận được tin nhắn 'Setup OK ...' trong Telegram chưa? [y/N]"

- On **N** → debug: bot bị block? sai chat_id? Restart Step 3.3.
- On **y** → ghi vào `.env`:

```bash
cat > .env.tmp <<EOF
TELEGRAM_BOT_TOKEN=${TOKEN}
TELEGRAM_CHAT_ID=${CHAT_ID}
EOF
mv .env.tmp .env
chmod 600 .env
```

### Step 4 — Install slash skills

Probe Cowork skill directory:

```bash
SKILL_DIR=""
if [ -d "$HOME/Library/Application Support/Claude/skills" ]; then
  SKILL_DIR="$HOME/Library/Application Support/Claude/skills"
elif [ -d "$HOME/.claude/skills" ]; then
  SKILL_DIR="$HOME/.claude/skills"
fi

if [ -n "$SKILL_DIR" ]; then
  mkdir -p "$SKILL_DIR/idea-brainstormer" "$SKILL_DIR/setup"
  ln -sf "$PWD/SKILLS/idea-brainstormer.skill" "$SKILL_DIR/idea-brainstormer/SKILL.md"
  ln -sf "$PWD/SKILLS/setup.skill" "$SKILL_DIR/setup/SKILL.md"
  echo "[ok] Slash skills installed to $SKILL_DIR"
else
  echo "[warn] Skill dir không phát hiện được."
  echo "Hãy mở README, mục 'Manual skill install', làm theo. Sau đó type 'continue' để tiếp tục."
  # Wait for user to type continue
fi
```

Verify: ask user mở một chat mới và gõ `/idea` — Cowork autocomplete `/idea-brainstormer` chưa?

### Step 5 — Scheduled task + MANDATORY verification

#### Step 5.1 — Pre-arm a tokenized verification row AND snapshot the log

```bash
TEST_TOKEN=$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(6))')
TEST_BEFORE_TS=$(date +%s)
.venv/bin/python crawl.py --inject-test "$TEST_TOKEN"

# Snapshot pipeline.log size NOW — before user clicks Run now.
# Any "acquire pipeline-run" line appended AFTER this point is attributable
# to the run we are about to verify (Phase A signal A).
LOG_BYTES_BEFORE=0
[ -f logs/pipeline.log ] && LOG_BYTES_BEFORE=$(wc -c < logs/pipeline.log | tr -d ' ')

echo "Verification token: $TEST_TOKEN"
echo "Test before TS: $TEST_BEFORE_TS"
echo "Log snapshot bytes: $LOG_BYTES_BEFORE"
```

#### Step 5.2 — Show task prompt + instruct user

```bash
cat cowork-task-prompt.md
```

Hướng dẫn user:

```
1. Mở sidebar Cowork → Scheduled.
2. Có task tên 'vinfast-pipeline' nào đã tồn tại không? [y/N]
   - Nếu N → Click '+' New Task → Name: vinfast-pipeline; Frequency: Hourly; 
             Paste nội dung cowork-task-prompt.md vào prompt; Save.
   - Nếu y → Click vào task đó → Edit. REPLACE prompt body với nội dung 
             cowork-task-prompt.md. Save. KHÔNG TẠO DUPLICATE.
   - Nếu nhiều task cùng tên → Xóa duplicate, chỉ giữ 1.
3. Sau khi save, click 'Run now' trên task vừa save.
4. Sau khi bấm Run now, type 'done' ở đây để tôi check kết quả.
```

Wait for user to type "done".

#### Step 5.3 — Two-phase adaptive poll

**Phase A — Startup detection (60s):**

Uses the `LOG_BYTES_BEFORE` snapshot from Step 5.1 (taken BEFORE user clicked Run now)
so any new bytes are attributable to the scheduled task we just triggered.

Two signals (either is sufficient):
- **Signal A:** new bytes in `logs/pipeline.log` after `LOG_BYTES_BEFORE` containing
  the string `acquire pipeline-run` (emitted by `lock.py acquire` on success).
- **Signal B:** a row in the `locks` table whose `acquired_at` (converted to epoch
  via SQLite `strftime('%s', ...)`) is newer than `TEST_BEFORE_TS`. Robust even if
  the run completes and releases the lock before our next poll IFF the log line was
  written.

```bash
PHASE_A_DEADLINE=$((TEST_BEFORE_TS + 60))
STARTUP_TS=""

while [ $(date +%s) -lt $PHASE_A_DEADLINE ]; do
  # Signal A — NEW bytes appended after LOG_BYTES_BEFORE
  if [ -f logs/pipeline.log ]; then
    CUR_BYTES=$(wc -c < logs/pipeline.log | tr -d ' ')
    if [ "$CUR_BYTES" -gt "$LOG_BYTES_BEFORE" ]; then
      NEW_BYTES=$((CUR_BYTES - LOG_BYTES_BEFORE))
      if tail -c "$NEW_BYTES" logs/pipeline.log | grep -q "acquire pipeline-run"; then
        STARTUP_TS=$(date +%s)
        break
      fi
    fi
  fi
  # Signal B — fresh lock row by epoch comparison
  ACTIVE_LOCK=$(sqlite3 news.db "
    SELECT acquired_at FROM locks
     WHERE name='pipeline-run'
       AND strftime('%s', acquired_at) > ${TEST_BEFORE_TS}
  ")
  if [ -n "$ACTIVE_LOCK" ]; then
    STARTUP_TS=$(date +%s)
    break
  fi
  sleep 5
done

if [ -z "$STARTUP_TS" ]; then
  echo "60s không thấy task khởi động. Đã bấm 'Run now' chưa? Task có hiện trong sidebar không?"
  # Diagnose + retry Step 5.2
fi
```

**Phase B — Completion wait (600s from STARTUP_TS):**

```bash
PHASE_B_DEADLINE=$((STARTUP_TS + 600))
NOTIFIED=""
while [ $(date +%s) -lt $PHASE_B_DEADLINE ]; do
  NOTIFIED=$(sqlite3 news.db "SELECT notified_at FROM articles WHERE url='bootstrap-test://${TEST_TOKEN}'")
  if [ -n "$NOTIFIED" ] && [ "$NOTIFIED" != "" ]; then
    break
  fi
  sleep 10
done

if [ -z "$NOTIFIED" ]; then
  echo "10 phút vẫn không thấy notified. Saved task prompt body có thể empty/sai."
  echo "Mở task trong sidebar → verify prompt body matches cowork-task-prompt.md."
fi
```

#### Step 5.4 — User confirmation

Hỏi: "Bạn vừa nhận tin nhắn Telegram có chứa chuỗi `${TEST_TOKEN}` không? [y/N]"

- On **y** AND `$NOTIFIED` is non-empty → **SCHEDULER VERIFIED**. Continue.
- On **N** OR notified_at vẫn NULL → diagnose:
  - "Có nhiều task `vinfast-pipeline` không?" → xóa duplicate.
  - "Prompt body trong task save có giống cowork-task-prompt.md không?" → re-edit + re-save.
  - "Chat Telegram có nhận message khác từ bot không?" → check chat_id.
  - Sau khi user fix → re-run Step 5.1 với token MỚI, lặp Step 5.2-5.4.

### Step 6 — Completion summary

```
🎉 Setup hoàn tất + verified end-to-end!

- Telegram credentials: ✅
- Slash skills installed: /idea-brainstormer, /setup
- Scheduled task 'vinfast-pipeline': ✅ runs hourly
- Verification: Telegram message received with token ${TEST_TOKEN}

Tiếp theo:
- Bot sẽ tự crawl + score + push Telegram mỗi giờ (khi Mac thức + Claude Desktop mở).
- Khi thấy tin hay trong Telegram, copy [#<id>] → mở Cowork chat → gõ /idea-brainstormer <id>
- Cấu hình lại sau: type /setup trong Cowork chat
```

---

## Rules

- **R1**: Mỗi bash command lỗi (exit ≠ 0, không phải CAS no-op) → dừng, in error, hỏi user.
- **R2**: Step 3 nonce flow phải dùng baseline+1 polling (KHÔNG offset=-1).
- **R3**: Step 5 verification phải qua "Run now" trên SAVED task, KHÔNG chạy pipeline in-chat manual.
- **R4**: Đặt biến TOKEN/TEST_TOKEN trong context của Cowork session, dùng literal trong các bash command.
- **R5**: Nếu Cowork bị block ở bất kỳ bash command nào → hướng dẫn user fallback Terminal (`bash install.sh && .venv/bin/python setup_helper.py`), sau đó quay lại Cowork paste cowork-task-prompt.md vào /schedule.
