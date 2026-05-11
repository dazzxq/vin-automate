#!/usr/bin/env bash
#
# vin-automate v2 — Telegram bot token + chat_id discovery (PLAN-v2 Task 8a).
#
# Deterministic nonce flow (no offset=-1 races):
#   1. User pastes bot token.
#   2. Validate via getMe.
#   3. Capture baseline_update_id from a fresh getUpdates.
#   4. Print a unique hex nonce; ask user to send it to the bot in the
#      target chat.
#   5. Poll getUpdates?offset=baseline+1 until a message matching the nonce
#      arrives. Extract chat.id.
#   6. Send a confirmation message to that chat_id and verify it returns ok.
#   7. Persist TG_TOKEN + TG_CHAT_ID into ./deploy.env (creating the file if
#      it doesn't yet exist).
#
# Run BEFORE `bash scripts/deploy.sh` to populate the two TG_* fields.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPLOY_ENV="$REPO_ROOT/deploy.env"

bold()   { printf '\033[1m%s\033[0m\n' "$*"; }
info()   { printf '  %s\n' "$*"; }
fatal()  { printf '  \033[1;31mfatal\033[0m %s\n' "$*" >&2; exit 1; }
ok()     { printf '  \033[1;32mok\033[0m %s\n' "$*"; }

command -v curl >/dev/null    || fatal "curl required"
command -v python3 >/dev/null || fatal "python3 required (for JSON parsing)"

bold "Telegram bot token + chat_id discovery"

# Step 1 — token prompt.
read -r -p "Paste your Telegram bot token (from @BotFather): " TOKEN
TOKEN="${TOKEN// /}"
[ -n "$TOKEN" ] || fatal "empty token"

# Bot token must NEVER appear in argv (per Codex impl-review).
# tg_get / tg_post pass the URL via curl -K - (config-from-stdin).
tg_get() {
  # $1 = telegram method (e.g. getMe, getUpdates), $2... = query string params
  local method="$1"; shift
  local query=""
  if [ "$#" -gt 0 ]; then
    query="?$(IFS='&'; echo "$*")"
  fi
  printf 'url = "https://api.telegram.org/bot%s/%s%s"\n' "$TOKEN" "$method" "$query" \
    | curl -fsS --max-time 12 -K -
}
tg_post() {
  # $1 = method, $2... = -d key=value pairs (each as a single arg)
  local method="$1"; shift
  local config=""
  config="$(printf 'url = "https://api.telegram.org/bot%s/%s"\n' "$TOKEN" "$method")"
  printf '%s\n' "$config" | curl -fsS --max-time 10 -X POST -K - "$@"
}

# Step 2 — validate getMe.
GETME="$(tg_get getMe || true)"
BOT_USERNAME="$(printf '%s' "$GETME" | python3 -c 'import json,sys
try:
  d=json.load(sys.stdin)
  print(d.get("result",{}).get("username","") if d.get("ok") else "")
except Exception:
  print("")
')"
if [ -z "$BOT_USERNAME" ]; then
  fatal "getMe failed — token invalid or network unreachable"
fi
ok "bot validated: @${BOT_USERNAME}"

# Step 3 — baseline.
BASELINE_JSON="$(tg_get getUpdates 'timeout=0' 'limit=100')"
BASELINE="$(printf '%s' "$BASELINE_JSON" | python3 -c 'import json,sys
d=json.load(sys.stdin); rs=d.get("result",[])
print(max((u.get("update_id",0) for u in rs), default=0))')"
info "baseline_update_id = ${BASELINE}"

# Step 4 — nonce prompt.
NONCE="$(openssl rand -hex 6)"
echo
bold "NONCE: ${NONCE}"
echo
info "In Telegram, open @${BOT_USERNAME}, press /start (first time only),"
info "then send EXACTLY this single line to the bot or to a group/channel containing the bot:"
info ""
info "    ${NONCE}"
info ""
info "Waiting up to 120s for the message to arrive..."

# Step 5 — poll with advancing offset. A busy chat can push the nonce off
# the first 100-message page; we MUST track max(update_id) and advance the
# cursor on each unmatched batch so the nonce is never skipped.
CHAT_ID=""
CHAT_NAME=""
OFFSET=$((BASELINE + 1))
DEADLINE=$(( $(date +%s) + 120 ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  UPDATES="$(tg_get getUpdates "offset=${OFFSET}" 'timeout=10' 'limit=100' 2>/dev/null || true)"
  if [ -z "$UPDATES" ]; then sleep 2; continue; fi

  # Returns "chat_id|chat_name|next_offset". next_offset is max(update_id)+1
  # from this batch (or the current offset if the batch was empty).
  PARSE="$(printf '%s' "$UPDATES" | NONCE="$NONCE" OFFSET="$OFFSET" python3 -c '
import json, os, sys
target  = os.environ["NONCE"].strip()
current = int(os.environ["OFFSET"])
try:
  d = json.load(sys.stdin)
except Exception:
  print(f"||{current}")
  sys.exit(0)
if not d.get("ok"):
  print(f"||{current}")
  sys.exit(0)
items = d.get("result", [])
max_id = current - 1
for u in items:
  uid = u.get("update_id", 0)
  if uid > max_id:
    max_id = uid
  msg  = u.get("message") or u.get("channel_post") or {}
  text = (msg.get("text") or "").strip()
  if text == target:
    chat = msg.get("chat", {})
    cid  = chat.get("id")
    name = chat.get("title") or chat.get("first_name") or chat.get("username") or "unknown"
    print(f"{cid}|{name}|{max_id + 1}")
    sys.exit(0)
print(f"||{max_id + 1}")
')"

  CHAT_ID="$(printf '%s' "$PARSE" | cut -d'|' -f1)"
  CHAT_NAME="$(printf '%s' "$PARSE" | cut -d'|' -f2)"
  NEXT_OFFSET="$(printf '%s' "$PARSE" | cut -d'|' -f3)"

  if [ -n "$CHAT_ID" ]; then
    break
  fi
  # Advance cursor so we don't re-fetch already-seen pages in the next loop.
  if [ -n "$NEXT_OFFSET" ] && [ "$NEXT_OFFSET" -gt "$OFFSET" ]; then
    OFFSET="$NEXT_OFFSET"
  fi
  sleep 2
done

[ -n "$CHAT_ID" ] || fatal "timed out waiting for nonce ${NONCE} — make sure you sent it to @${BOT_USERNAME}"
ok "detected chat: ${CHAT_NAME} (id=${CHAT_ID})"

# Step 6 — confirmation send. CHAT_ID is non-sensitive but tg_post keeps the
# URL (containing the bot token) out of argv via curl -K -.
CONFIRM="$(tg_post sendMessage \
  --data-urlencode "chat_id=${CHAT_ID}" \
  --data-urlencode "text=Setup OK — vin-automate v2 verified chat_id=${CHAT_ID}" \
  2>/dev/null || true)"
case "$CONFIRM" in
  *'"ok":true'*) ok "confirmation message delivered" ;;
  *) fatal "confirmation send failed — chat_id may be wrong, or bot blocked. Resp: $CONFIRM" ;;
esac

# Step 7 — persist into deploy.env (create if absent).
if [ ! -f "$DEPLOY_ENV" ]; then
  info "deploy.env not found — creating skeleton (you still need to set API_TOKEN/DB_PASS etc.)"
  cat > "$DEPLOY_ENV" <<EOF
# vin-automate v2 deploy env (chmod 600 — contains secrets).
API_TOKEN=
DB_PASS=
VPS_IP=14.225.29.159
VPS_SSH_ALIAS=vps-root
FQDN=tlinh.duyet.vn
SUBDOMAIN=tlinh
CF_ZONE_ID=d738588b6a6169bdd8ccde1921f3a62a
CF_EMAIL=the@duyet.dev
TG_TOKEN=
TG_CHAT_ID=
EOF
  chmod 600 "$DEPLOY_ENV"
fi

# Update / insert TG_TOKEN + TG_CHAT_ID. We rewrite the file rather than
# using sed -i because BSD sed (Mac) and GNU sed differ on `-i`.
#
# argv carries DEPLOY_ENV path + CHAT_ID (neither sensitive).
# stdin carries the bot TOKEN — only sensitive value — so it never appears
# in /proc/$pid/cmdline. Using `python3 -c` (not `python3 -`) keeps stdin
# free for the payload; `python3 -` would consume stdin for the program.
printf '%s' "$TOKEN" | python3 -c '
import sys
path     = sys.argv[1]
chat_id  = sys.argv[2]
token    = sys.stdin.read()
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()
out, seen_t, seen_c = [], False, False
for ln in lines:
    if ln.startswith("TG_TOKEN="):
        out.append(f"TG_TOKEN={token}\n"); seen_t = True
    elif ln.startswith("TG_CHAT_ID="):
        out.append(f"TG_CHAT_ID={chat_id}\n"); seen_c = True
    else:
        out.append(ln)
if not seen_t: out.append(f"TG_TOKEN={token}\n")
if not seen_c: out.append(f"TG_CHAT_ID={chat_id}\n")
with open(path, "w", encoding="utf-8") as f:
    f.writelines(out)
' "$DEPLOY_ENV" "$CHAT_ID"
chmod 600 "$DEPLOY_ENV"

ok "deploy.env updated (TG_TOKEN + TG_CHAT_ID set)"
echo
bold "Next: run 'bash scripts/deploy.sh' from $REPO_ROOT to start Phase A."
