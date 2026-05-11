#!/usr/bin/env bash
#
# vin-automate v2 — Cloudflare DNS automation (PLAN-v2 §8.2).
#
# Idempotent upsert of the tlinh.duyet.vn A record. First run creates the
# record (proxied=false, TTL 120); subsequent runs PATCH if a non-matching
# value exists, otherwise no-op. Per Codex ISSUE-6, refuses to enable
# proxied=true unless CF SSL mode is "strict" or "full" — but this script
# itself never sets proxied=true; that's a manual op (§8.4).
#
# CF Global API key is pulled from macOS Keychain at runtime (NOT stored
# in deploy.env). The keychain entry name is fixed by PLAN-v2 §10 §8.1.

set -euo pipefail

if [ -n "${REPO_ROOT:-}" ]; then
  : # already exported by deploy.sh
else
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
  if [ -f "$REPO_ROOT/deploy.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$REPO_ROOT/deploy.env"
    set +a
  fi
fi

: "${CF_ZONE_ID:?CF_ZONE_ID missing (deploy.env)}"
: "${CF_EMAIL:?CF_EMAIL missing (deploy.env)}"
: "${FQDN:?FQDN missing (deploy.env)}"
: "${SUBDOMAIN:?SUBDOMAIN missing (deploy.env)}"
: "${VPS_IP:?VPS_IP missing (deploy.env)}"

# Pull CF Global API key from macOS Keychain.
if ! command -v security >/dev/null 2>&1; then
  echo "[fatal] 'security' (macOS keychain CLI) not available. dns_setup.sh runs from Mac." >&2
  exit 1
fi
CF_KEY="$(security find-generic-password -s cloudflare-global-api-key -a "${CF_EMAIL}" -w 2>/dev/null || true)"
if [ -z "$CF_KEY" ]; then
  echo "[fatal] CF Global API key not found in keychain (service=cloudflare-global-api-key account=${CF_EMAIL})." >&2
  echo "        Add it once via: security add-generic-password -s cloudflare-global-api-key -a ${CF_EMAIL} -w '<KEY>'" >&2
  exit 1
fi

cf() {
  # Pass CF credentials via curl's -K config (stdin) so X-Auth-Key never
  # appears in argv / /proc/$pid/cmdline. The caller supplies any other
  # curl flags in $@; only the request body, URL, and method are in argv.
  printf 'header = "X-Auth-Email: %s"\nheader = "X-Auth-Key: %s"\n' \
    "$CF_EMAIL" "$CF_KEY" \
    | curl -fsS -K - "$@"
}

# Look up ALL A records for this FQDN — handle duplicates per Codex
# impl-review. We converge the zone to exactly one canonical record
# (content=$VPS_IP, proxied=false) and delete the rest.
LOOKUP_URL="https://api.cloudflare.com/client/v4/zones/${CF_ZONE_ID}/dns_records?type=A&name=${FQDN}"
LOOKUP_JSON="$(cf "$LOOKUP_URL")"

# Parse all matching records into a tab-separated table: id\tcontent\tproxied.
ALL_RECORDS="$(printf '%s' "$LOOKUP_JSON" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for r in d.get("result", []):
    print("\t".join([
        r.get("id", ""),
        r.get("content", ""),
        str(bool(r.get("proxied", False))).lower(),
    ]))
')"

PAYLOAD="$(python3 -c "import json; print(json.dumps({'type':'A','name':'${SUBDOMAIN}','content':'${VPS_IP}','ttl':120,'proxied':False}))")"

if [ -z "$ALL_RECORDS" ]; then
  echo "  [create] ${FQDN} -> ${VPS_IP}"
  cf -X POST \
    "https://api.cloudflare.com/client/v4/zones/${CF_ZONE_ID}/dns_records" \
    -H 'Content-Type: application/json' -d "$PAYLOAD" >/dev/null
else
  # Pick a canonical record to keep (prefer one that already has content=$VPS_IP).
  CANONICAL_ID=""
  CANONICAL_IP=""
  CANONICAL_PROXIED=""
  while IFS=$'\t' read -r rid rip rprx; do
    if [ "$rip" = "$VPS_IP" ] && [ -z "$CANONICAL_ID" ]; then
      CANONICAL_ID="$rid"; CANONICAL_IP="$rip"; CANONICAL_PROXIED="$rprx"
    fi
  done <<< "$ALL_RECORDS"
  if [ -z "$CANONICAL_ID" ]; then
    # No exact match — take the first record and PATCH it to the canonical value.
    FIRST="$(printf '%s' "$ALL_RECORDS" | head -1)"
    CANONICAL_ID="$(printf '%s' "$FIRST" | cut -f1)"
    CANONICAL_IP="$(printf '%s' "$FIRST"  | cut -f2)"
    CANONICAL_PROXIED="$(printf '%s' "$FIRST" | cut -f3)"
  fi

  if [ "$CANONICAL_IP" = "$VPS_IP" ] && [ "$CANONICAL_PROXIED" = "false" ]; then
    echo "  [noop] ${FQDN} already -> ${VPS_IP} (proxied=false), id=${CANONICAL_ID}"
  else
    echo "  [patch] ${FQDN}: ${CANONICAL_IP}(proxied=${CANONICAL_PROXIED}) -> ${VPS_IP}(proxied=false)"
    cf -X PATCH \
      "https://api.cloudflare.com/client/v4/zones/${CF_ZONE_ID}/dns_records/${CANONICAL_ID}" \
      -H 'Content-Type: application/json' -d "$PAYLOAD" >/dev/null
  fi

  # Delete every OTHER record matching this FQDN so the zone has exactly one.
  while IFS=$'\t' read -r rid rip rprx; do
    if [ -n "$rid" ] && [ "$rid" != "$CANONICAL_ID" ]; then
      echo "  [delete-dup] removing duplicate A record id=${rid} (was ${rip}, proxied=${rprx})"
      cf -X DELETE \
        "https://api.cloudflare.com/client/v4/zones/${CF_ZONE_ID}/dns_records/${rid}" >/dev/null
    fi
  done <<< "$ALL_RECORDS"
fi

# Propagation wait. Cloudflare itself returns the new value immediately on
# 1.1.1.1; the user's resolver may lag briefly but that's outside our scope.
for i in 1 2 3 4 5 6; do
  RESOLVED="$(dig +short "@1.1.1.1" "${FQDN}" A | head -1 || true)"
  if [ "$RESOLVED" = "$VPS_IP" ]; then
    echo "  [ok] 1.1.1.1 -> ${RESOLVED}"
    exit 0
  fi
  echo "  [wait] 1.1.1.1 returns '${RESOLVED:-<none>}' (expected ${VPS_IP}); sleeping 5s..."
  sleep 5
done
echo "[warn] DNS still propagating; continuing — deploy.sh's health check will catch any real failure" >&2
