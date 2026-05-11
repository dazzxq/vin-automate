#!/usr/bin/env bash
#
# vin-automate v2 — Phase A infrastructure deploy.
#
# Executes Steps 1–9 of PLAN-v2 §10 against the VPS at $VPS_SSH_ALIAS:
#   1. CF DNS upsert (delegates to scripts/dns_setup.sh)
#   2. Create VPS target directories
#   3. rsync code + scp schema
#   4. DB user + schema apply
#   5. Write VPS shared/.env
#   6. nginx vhost + wildcard-cert preflight (NO certbot run)
#   7. Health check against the live endpoint
#   8. Mac client .env (no install — Tasks 16/17 cover install + README)
#   9. launchd plist (Tasks 15+16 ship the plist; deploy registers it here)
#
# Phase B (Cowork verify + inject-test + Run now) is documented in
# PLAN-v2 §10 Steps 10–14 and is NOT performed by this script. Per Codex
# ISSUE-18, deploy.env is NOT shredded here — it must survive into Phase B.
#
# Idempotent: every step short-circuits if the desired state is already in
# place. Run twice in a row to verify (the second run should exit 0 with
# every step reporting "ok" / "already done").

set -euo pipefail

# Locate the repo root regardless of where this script is invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

DEPLOY_ENV="$REPO_ROOT/deploy.env"

step()  { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok()    { printf '  \033[1;32mok\033[0m   %s\n' "$*"; }
warn()  { printf '  \033[1;33mwarn\033[0m %s\n' "$*"; }
err()   { printf '  \033[1;31mfail\033[0m %s\n' "$*" >&2; exit 1; }

# --- Pre-step: load deploy.env --------------------------------------------
if [ ! -f "$DEPLOY_ENV" ]; then
  err "deploy.env not found at $DEPLOY_ENV. See PLAN-v2.md §10 Pre-step to generate it."
fi
# Refuse to source a world-readable secrets file.
PERMS="$(stat -f '%Lp' "$DEPLOY_ENV" 2>/dev/null || stat -c '%a' "$DEPLOY_ENV" 2>/dev/null || echo "")"
if [ "$PERMS" != "600" ]; then
  warn "deploy.env perms are '$PERMS' (expected 600). chmod-ing to 600."
  chmod 600 "$DEPLOY_ENV"
fi
set -a
# shellcheck disable=SC1090
. "$DEPLOY_ENV"
set +a

: "${API_TOKEN:?API_TOKEN missing from deploy.env}"
: "${DB_PASS:?DB_PASS missing from deploy.env}"
: "${VPS_IP:?VPS_IP missing from deploy.env}"
: "${VPS_SSH_ALIAS:?VPS_SSH_ALIAS missing from deploy.env}"
: "${FQDN:?FQDN missing from deploy.env}"
: "${SUBDOMAIN:?SUBDOMAIN missing from deploy.env}"
: "${CF_ZONE_ID:?CF_ZONE_ID missing from deploy.env}"
: "${CF_EMAIL:?CF_EMAIL missing from deploy.env}"
: "${TG_TOKEN:?TG_TOKEN missing from deploy.env — run scripts/telegram-nonce-helper.sh first}"
: "${TG_CHAT_ID:?TG_CHAT_ID missing from deploy.env — run scripts/telegram-nonce-helper.sh first}"

VPS_BASE="/var/www/tlinh/${FQDN}"
RELEASE_DIR="${VPS_BASE}/releases/v1"
SHARED_DIR="${VPS_BASE}/shared"

# --- Step 1: DNS upsert ---------------------------------------------------
step "Step 1 — DNS upsert (Cloudflare A record)"
"$SCRIPT_DIR/dns_setup.sh"
ok "DNS resolved (script handles its own retry + propagation wait)"

# --- Step 2: VPS target directories ---------------------------------------
step "Step 2 — VPS target directories"
ssh "$VPS_SSH_ALIAS" "RELEASE_DIR='${RELEASE_DIR}' SHARED_DIR='${SHARED_DIR}' VPS_BASE='${VPS_BASE}' bash -s" <<'REMOTE'
set -euo pipefail
mkdir -p "${RELEASE_DIR}/public" "${RELEASE_DIR}/src/routes" "${SHARED_DIR}"
chown -R www-data:www-data "${VPS_BASE}"
REMOTE
ok "target dirs present"

# --- Step 3: rsync code + scp schema --------------------------------------
step "Step 3 — rsync code + schema"
rsync -avz --delete \
  --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store' \
  "$REPO_ROOT/vps/" "${VPS_SSH_ALIAS}:${RELEASE_DIR}/"

# Write a VERSION file the backend can serve from /api/health.
GIT_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
ssh "$VPS_SSH_ALIAS" "RELEASE_DIR='${RELEASE_DIR}' SHA='${GIT_SHA}' bash -s" <<'REMOTE'
set -euo pipefail
printf 'v2-%s\n' "${SHA}" > "${RELEASE_DIR}/src/VERSION"
chown www-data:www-data "${RELEASE_DIR}/src/VERSION"
REMOTE

scp -p "$REPO_ROOT/vps/schema.sql" "${VPS_SSH_ALIAS}:/tmp/schema.sql" >/dev/null
ok "code synced (sha=${GIT_SHA}), schema uploaded to /tmp/schema.sql"

# --- Step 4: DB user + schema --------------------------------------------
# Secrets travel via ssh STDIN as prefixed `KEY=<quoted value>` assignments
# (per Codex impl-review): the ssh argv only contains `bash -s`, so the
# secret never appears in /proc/$pid/cmdline or in journald audit logs.
step "Step 4 — DB user + schema"
{
  printf 'DB_PASS=%q\n' "$DB_PASS"
  cat <<'REMOTE'
set -euo pipefail
# Root grants — uses socket auth, no password in argv.
mariadb -u root <<SQL
CREATE DATABASE IF NOT EXISTS tlinh_news CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'tlinh'@'localhost' IDENTIFIED BY '$DB_PASS';
ALTER USER 'tlinh'@'localhost' IDENTIFIED BY '$DB_PASS';
GRANT SELECT, INSERT, UPDATE, DELETE ON tlinh_news.* TO 'tlinh'@'localhost';
FLUSH PRIVILEGES;
SQL

# Schema import — keep DB_PASS out of mariadb argv by routing it through
# a 600-mode option file. Trap on EXIT so the file is shredded even if the
# import fails part-way.
CRED_FILE="$(mktemp /tmp/.tlinh-mariadb.XXXXXX)"
chmod 600 "$CRED_FILE"
trap 'shred -u "$CRED_FILE" 2>/dev/null || rm -f "$CRED_FILE"' EXIT
cat > "$CRED_FILE" <<CRED
[client]
user=tlinh
password=$DB_PASS
CRED
mariadb --defaults-extra-file="$CRED_FILE" tlinh_news < /tmp/schema.sql
rm -f /tmp/schema.sql
REMOTE
} | ssh "$VPS_SSH_ALIAS" 'bash -s'
ok "DB + schema in place"

# --- Step 5: VPS shared/.env ----------------------------------------------
# Same stdin-prefix pattern: secrets stay out of argv.
step "Step 5 — Write VPS shared/.env"
{
  printf 'SHARED_DIR=%q\n'   "$SHARED_DIR"
  printf 'VPS_BASE=%q\n'     "$VPS_BASE"
  printf 'FQDN=%q\n'         "$FQDN"
  printf 'RELEASE_DIR=%q\n'  "$RELEASE_DIR"
  printf 'API_TOKEN=%q\n'    "$API_TOKEN"
  printf 'DB_PASS=%q\n'      "$DB_PASS"
  printf 'TG_TOKEN=%q\n'     "$TG_TOKEN"
  printf 'TG_CHAT_ID=%q\n'   "$TG_CHAT_ID"
  cat <<'REMOTE'
set -euo pipefail
ENV_PATH="$SHARED_DIR/.env"
umask 077
cat > "$ENV_PATH" <<EOF
DB_HOST=127.0.0.1
DB_NAME=tlinh_news
DB_USER=tlinh
DB_PASS=$DB_PASS
API_TOKEN=$API_TOKEN
TELEGRAM_BOT_TOKEN=$TG_TOKEN
TELEGRAM_CHAT_ID=$TG_CHAT_ID
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
chmod 600 "$ENV_PATH"
chown www-data:www-data "$ENV_PATH"

# Atomic symlink swap (releases/v1 -> current).
ln -sfn "$RELEASE_DIR" "$VPS_BASE/current"
chown -R www-data:www-data "$VPS_BASE"
REMOTE
} | ssh "$VPS_SSH_ALIAS" 'bash -s'
ok "shared/.env (mode 600), current symlink -> v1"

# --- Step 6: nginx vhost + cert preflight ---------------------------------
step "Step 6 — nginx vhost + wildcard-cert preflight (no certbot run)"
ssh "$VPS_SSH_ALIAS" "FQDN='${FQDN}' VPS_BASE='${VPS_BASE}' bash -s" <<'REMOTE'
set -euo pipefail
VHOST=/etc/nginx/sites-available/"${FQDN}"
TMPF=$(mktemp)

cat > "${TMPF}" <<NGINX
# Rate-limit zones (per Codex ISSUE-10)
limit_req_zone \$http_authorization zone=tlinh_main:10m   rate=100r/m;
limit_req_zone \$http_authorization zone=tlinh_lock:10m   rate=600r/m;
limit_req_zone \$http_authorization zone=tlinh_hourly:10m rate=1000r/h;
limit_req_status 429;
map \$status \$retry_after_header { 429 "60"; default ""; }

# HTTP → HTTPS redirect (cert covers all of *.duyet.vn; no acme-challenge needed)
server {
    listen 80;
    server_name ${FQDN};
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl http2;
    server_name ${FQDN};
    root ${VPS_BASE}/current/public;
    index index.php;
    client_max_body_size 5M;

    # Pre-installed wildcard cert (verified by the preflight below).
    ssl_certificate     /etc/letsencrypt/live/duyet.vn/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/duyet.vn/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # Security headers (PLAN-v2 §5.4 + ISSUE-28)
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options        "DENY" always;
    add_header Referrer-Policy        "strict-origin-when-cross-origin" always;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    add_header Retry-After \$retry_after_header always;

    # Health — never rate-limited (per ISSUE-10).
    location = /api/health {
        try_files \$uri /index.php?\$query_string;
    }

    # Admin endpoint — localhost only (per ISSUE-9).
    location = /api/admin/inject-test {
        allow 127.0.0.1;
        allow ::1;
        deny all;
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
        # Notify can take ~241s worst case (Phase 2 budget). Lift fastcgi
        # timeout so php-fpm has time to complete the 2-phase claim/send.
        fastcgi_read_timeout 300s;
        fastcgi_send_timeout 300s;
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
        # Same timeout extension for direct .php hits.
        fastcgi_read_timeout 300s;
        fastcgi_send_timeout 300s;
    }
}
NGINX

# Wildcard cert preflight (per Codex ISSUE-25): all 5 properties.
CERT=/etc/letsencrypt/live/duyet.vn/fullchain.pem
KEY=/etc/letsencrypt/live/duyet.vn/privkey.pem
[ -f "$CERT" ] || { echo "[fatal] wildcard cert missing at $CERT — recovery: SSH to VPS as root and re-run the existing certbot DNS-01 flow for *.duyet.vn (e.g. 'certbot certonly --manual --preferred-challenges dns -d \"*.duyet.vn\" -d duyet.vn'). This deploy script does NOT issue certs; cert lifecycle is owned outside v2." >&2; rm -f "${TMPF}"; exit 1; }
[ -f "$KEY" ]  || { echo "[fatal] private key missing at $KEY" >&2; rm -f "${TMPF}"; exit 1; }
[ -r "$KEY" ]  || { echo "[fatal] private key not readable (check perms / sudo)" >&2; rm -f "${TMPF}"; exit 1; }
openssl x509 -in "$CERT" -noout -ext subjectAltName | grep -q 'DNS:\*\.duyet\.vn' \
  || { echo "[fatal] cert SAN does not include *.duyet.vn" >&2; rm -f "${TMPF}"; exit 1; }
openssl x509 -in "$CERT" -noout -checkend 86400 \
  || { echo "[fatal] cert expires within 24h — renew before deploying" >&2; rm -f "${TMPF}"; exit 1; }
CERT_FP=$(openssl x509 -in "$CERT" -noout -pubkey | openssl pkey -pubin -outform der | sha256sum | cut -d' ' -f1)
KEY_FP=$(openssl pkey -in "$KEY" -pubout -outform der | sha256sum | cut -d' ' -f1)
[ "$CERT_FP" = "$KEY_FP" ] || { echo "[fatal] fullchain.pem and privkey.pem do not match" >&2; rm -f "${TMPF}"; exit 1; }

# Only swap the vhost if it changed.
if ! cmp -s "${TMPF}" "${VHOST}" 2>/dev/null; then
  mv "${TMPF}" "${VHOST}"
  ln -sfn "${VHOST}" /etc/nginx/sites-enabled/"${FQDN}"
  nginx -t
  systemctl reload nginx
  echo "[ok] vhost updated + nginx reloaded"
else
  rm -f "${TMPF}"
  echo "[ok] vhost unchanged (idempotent no-op)"
fi
REMOTE
ok "vhost in place, wildcard cert verified"

# --- Step 7: Health check -------------------------------------------------
step "Step 7 — Health check"
HEALTH_URL="https://${FQDN}/api/health"
for i in 1 2 3 4 5; do
  if curl -fsSL --max-time 10 "$HEALTH_URL" > /tmp/.deploy_health.json 2>/dev/null; then
    BODY="$(cat /tmp/.deploy_health.json)"
    echo "  $BODY"
    case "$BODY" in
      *'"status":"ok"'*) ok "health 200"; break ;;
      *) err "unexpected health body: $BODY" ;;
    esac
  fi
  warn "attempt $i/5 failed (still propagating?); sleeping 5s"
  sleep 5
  [ $i -eq 5 ] && err "health endpoint unreachable after 5 attempts"
done
rm -f /tmp/.deploy_health.json

# --- Step 8: Mac client (install + .env + smoke test) -------------------
# Per PLAN-v2 §10 Step 8: install deps, write .env, run smoke test.
#
# Step 8 + 9 are deferred-but-required: if install.sh / api_client.py /
# com.tlinh.crawl.plist are not yet shipped (e.g. infra is being deployed
# before Tasks 10/15/16 land), each step soft-skips with an explicit
# "DEFERRED" message. When the file IS present, behavior is strict: any
# failure inside install / smoke / launchctl is fatal. Re-running deploy.sh
# after Tasks 10/15/16 land will pick up the now-present files and exercise
# the full sequence.
step "Step 8 — Mac client install + smoke test"
# Mac-side .env first (install.sh + api_client read API_TOKEN from here).
MAC_ENV="$REPO_ROOT/.env"
umask 077
cat > "$MAC_ENV" <<EOF
API_BASE_URL=https://${FQDN}
API_TOKEN=${API_TOKEN}
EOF
chmod 600 "$MAC_ENV"
ok ".env written"

if [ ! -f "$REPO_ROOT/install.sh" ] || [ ! -f "$REPO_ROOT/api_client.py" ]; then
  warn "DEFERRED: install.sh and/or api_client.py not yet present in repo."
  warn "          Complete Tasks 10 (api_client.py) and 16 (install.sh), then re-run deploy.sh."
  warn "          (This is expected during incremental rollout — infra portion still succeeded.)"
  STEP8_DEFERRED=1
else
  # install.sh is idempotent per PLAN-v2 §11 Task 16: re-run is a no-op.
  ( cd "$REPO_ROOT" && bash install.sh ) \
    || err "install.sh failed"
  # Smoke test via the freshly-installed api_client.
  ( cd "$REPO_ROOT" && .venv/bin/python -c 'from api_client import ApiClient; r=ApiClient().health(); print(r); assert r.get("status")=="ok", r' ) \
    || err "api_client smoke test failed — backend may be unreachable or auth broken"
  ok "install + smoke test passed"
  STEP8_DEFERRED=0
fi

# --- Step 9: launchd plist + immediate kickstart + log verification -----
# Per PLAN-v2 §10 Step 9: bootstrap the agent, kickstart it once for an
# immediate trigger, then confirm via logs/crawl.out that the job actually
# executed (registration alone is not proof of a working scheduler).
step "Step 9 — launchd plist (Mac daily crawl)"
PLIST_SRC="$REPO_ROOT/com.tlinh.crawl.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.tlinh.crawl.plist"

# Step 9 depends on Step 8's install (the launchd job invokes the .venv
# python and the api_client). If Step 8 was deferred, Step 9 must defer
# too — kicking the plist would fail at the first python import.
if [ "${STEP8_DEFERRED:-0}" = "1" ]; then
  warn "DEFERRED: Step 8 was deferred (Tasks 10/16 not landed). Skipping plist setup so the job isn't kicked against an empty venv."
  STEP9_DEFERRED=1
  STEP9_DEFER_REASON="step8_dependency"
elif [ ! -f "$PLIST_SRC" ]; then
  warn "DEFERRED: $PLIST_SRC missing. Complete Task 15 then re-run deploy.sh."
  STEP9_DEFERRED=1
  STEP9_DEFER_REASON="plist_missing"
else
  mkdir -p "$REPO_ROOT/logs"
  : > "$REPO_ROOT/logs/crawl.out"  # truncate so post-kickstart tail is fresh
  cp "$PLIST_SRC" "$PLIST_DST"
  # bootout (idempotent) — safe even if the job isn't currently registered.
  launchctl bootout "gui/$(id -u)/com.tlinh.crawl" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
  launchctl print "gui/$(id -u)/com.tlinh.crawl" >/dev/null 2>&1 \
    || err "launchd job did not register after bootstrap — inspect '$PLIST_DST'"
  # Immediate trigger per the runbook.
  launchctl kickstart -k "gui/$(id -u)/com.tlinh.crawl" \
    || err "launchctl kickstart failed"

  # Wait up to 30s for the kicked run to produce output, then verify it.
  for i in 1 2 3 4 5 6; do
    if [ -s "$REPO_ROOT/logs/crawl.out" ]; then
      ok "launchd job registered + kicked; logs/crawl.out produced output"
      tail -5 "$REPO_ROOT/logs/crawl.out" | sed 's/^/    | /'
      STEP9_DEFERRED=0
      break
    fi
    sleep 5
    if [ $i -eq 6 ]; then
      err "launchd kickstart produced no output in logs/crawl.out within 30s — check plist StandardOutPath / job exit code via 'launchctl print gui/\$(id -u)/com.tlinh.crawl'"
    fi
  done
fi

# --- Phase A done ---------------------------------------------------------
step "Phase A complete"
if [ "${STEP8_DEFERRED:-0}" = "1" ] || [ "${STEP9_DEFERRED:-0}" = "1" ]; then
  cat <<NOTE
  Infra portion succeeded, but Mac-side wiring is DEFERRED:
NOTE
  [ "${STEP8_DEFERRED:-0}" = "1" ] && echo "    - Step 8 (install + smoke): waiting on Tasks 10 + 16."
  # Step 9's reason depends on WHY it deferred — Step-8 dependency vs plist missing.
  if [ "${STEP9_DEFERRED:-0}" = "1" ]; then
    case "${STEP9_DEFER_REASON:-plist_missing}" in
      step8_dependency)
        echo "    - Step 9 (launchd plist):    waiting on Tasks 10 + 16 (transitively via Step 8)." ;;
      plist_missing|*)
        echo "    - Step 9 (launchd plist):    waiting on Task 15." ;;
    esac
  fi
  cat <<NOTE
  Re-run 'bash scripts/deploy.sh' once those tasks land — the deferred
  steps will then exercise their full strict path.
NOTE
fi
cat <<NOTE

  Next steps (Phase B — manual, see PLAN-v2 §10 Steps 10–14):
    1. Open Claude Desktop, create the Cowork project pointing at this folder.
    2. Paste cowork-task-prompt.md content into /schedule UI (Daily, name 'vinfast-pipeline').
    3. Seed bootstrap test row via SSH-tunneled curl (Step 12 in plan).
    4. Click "Run now" in the Cowork UI; verify Telegram receives the test message
       containing your bootstrap token within 10 min.
    5. Shred deploy.env: shred -u deploy.env

  Phase A does NOT shred deploy.env (per Codex ISSUE-18) — it must survive
  into Phase B so manual commands can re-source it with 'set -a; . deploy.env; set +a'.
NOTE
