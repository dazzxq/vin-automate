# vin-automate v2 — VinFast News Pipeline

[![macOS 13+](https://img.shields.io/badge/macOS-13%2B-blue)](#requirements)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org)
[![PHP 8.5+](https://img.shields.io/badge/PHP-8.5%2B-blue)](https://www.php.net)
[![MariaDB 10.11+](https://img.shields.io/badge/MariaDB-10.11%2B-blue)](https://mariadb.org)

> 3-tier daily pipeline that crawls VinFast news from Google News + curated VN
> sources, lets Claude in Cowork score 1–5 and brainstorm 5 ideas per article,
> and pushes high-score articles to Telegram. **v2 architecture** decouples
> crawling (Mac launchd), state (VPS PHP + MariaDB at `tlinh.duyet.vn`), and
> AI scoring (Cowork via Claude Pro/Max plan).

---

## Why v2 exists

v1 stored state in SQLite on a folder mounted into the Cowork sandbox. That
turned out to be unworkable: the Cowork bash sandbox is a Linux container
whose mounted folder doesn't support POSIX file locking, so SQLite writes
failed with `disk I/O error`. v2 puts state on a VPS, exposed via HTTPS API.
Mac and Cowork each talk to it.

---

## Table of contents

- [Architecture](#architecture)
- [Requirements](#requirements)
- [Quickstart](#quickstart)
- [Daily usage](#daily-usage)
- [File layout](#file-layout)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Code-review trail](#code-review-trail)
- [License](#license)

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│ Mac (launchd, daily 07:00 local)                                       │
│   crawl.py → resolves canonical URLs → POST /api/articles              │
│   extract.py → GET stage=new → trafilatura/Jina → PATCH /extract       │
│                          │                                             │
│                          ▼                                             │
│ VPS (tlinh.duyet.vn — PHP 8.5 + MariaDB 10.11 + nginx + wildcard cert) │
│   PHP routes under vps/src/ — bearer auth, hash_equals, rate limits    │
│   2-phase notify claim with claim-heartbeat (PLAN-v2 §4)               │
│                          ▲                                             │
│                          │                                             │
│ Cowork (Claude Desktop, scheduled daily task)                          │
│   GET /api/articles?stage=extracted → score → PATCH /score             │
│   GET ?stage=scored&min_score=3 → POST /api/notify/{id}                │
│   /vf-brainstorm slash skill on-demand                             │
│                          │                                             │
│                          ▼                                             │
│   Telegram POST via VPS PHP (not Mac/Cowork)                           │
└────────────────────────────────────────────────────────────────────────┘
```

State: VPS owns DB. Mac and Cowork are stateless clients. All inter-tier
communication is HTTPS with a single shared bearer token.

---

## Requirements

- **Mac client:** macOS 13+, Python 3.11+, ~50MB disk for venv + deps.
  Optional — only needed if you want Mac-side crawling.
- **VPS:** Ubuntu 24.04 (we use `14.225.29.159`), nginx, PHP 8.5+ (with
  intl extension for `Normalizer`), MariaDB 10.11+, certbot timer.
  Wildcard cert for `*.duyet.vn` must be pre-installed at
  `/etc/letsencrypt/live/duyet.vn/`. (Deploy script verifies — does NOT
  issue.)
- **Cowork:** Claude Desktop Pro/Max plan, Linux sandbox bash tool.
- **Telegram:** A bot from `@BotFather`, plus a chat where the bot is
  added (private chat or group).
- **Cloudflare:** Global API key for the zone hosting `duyet.vn`, stored in
  macOS Keychain at service=`cloudflare-global-api-key` account=`<your-CF-email>`.

---

## Quickstart

Deployment is split into **Phase A** (automated infra) and **Phase B**
(manual Cowork UI clicks). See [PLAN-v2.md §10](PLAN-v2.md) for the canonical
runbook.

### Phase A — Infrastructure deploy (automated)

```bash
# 1. Discover Telegram bot token + chat_id via deterministic nonce flow.
#    Writes TG_TOKEN + TG_CHAT_ID into deploy.env.
bash scripts/telegram-nonce-helper.sh

# 2. Fill in remaining deploy.env values (API_TOKEN, DB_PASS auto-generated;
#    constants like VPS_IP, FQDN, CF_ZONE_ID, CF_EMAIL already set). See
#    PLAN-v2.md §10 Pre-step.
nano deploy.env   # or your editor
chmod 600 deploy.env

# 3. Run Phase A: DNS + VPS folders + rsync code + DB + .env +
#    nginx (with wildcard cert verify) + health check + Mac client.
bash scripts/deploy.sh
```

`deploy.sh` is **idempotent**: re-running it short-circuits every step
that's already in place. After success, `https://tlinh.duyet.vn/api/health`
returns 200 and the launchd job is registered.

### Phase B — Cowork verify (one-time manual)

1. **Write `SKILLS/.env`** so the scheduled task and the brainstormer skill
   can authenticate. This file is the ONLY auth source for the Cowork side
   (per PLAN-v2 §5.3 mandate — no hardcoded tokens). Re-source deploy.env
   first if you're in a new terminal:
   ```bash
   set -a && . deploy.env && set +a
   cat > SKILLS/.env <<EOF
   API_BASE_URL=https://${FQDN}
   API_TOKEN=${API_TOKEN}
   EOF
   chmod 600 SKILLS/.env
   ```
2. In Claude Desktop, create a new Cowork project pointing at this folder.
3. Grant **bash + network "Allow all"**.
4. Open Cowork chat → `/schedule` → paste the entire contents of
   `cowork-task-prompt.md`. Set frequency = **Daily**. Name =
   `vinfast-pipeline`. Save.
5. Seed a verifiable bootstrap row via the localhost-only admin endpoint
   (uses an SSH tunnel from Mac so the nginx `allow 127.0.0.1` guard
   passes — full command in PLAN-v2 §10 Step 12).
6. In the Cowork UI, click **Run now** on `vinfast-pipeline`. Within 10
   minutes you should receive a Telegram message containing your
   bootstrap token. That confirms the entire pipeline works.
7. Remove secrets from your Mac. macOS doesn't ship `shred`; use either
   `rm -fP deploy.env` (BSD `rm`'s overwrite-before-unlink — best effort
   on APFS; SSDs may retain the data in unallocated blocks but this is
   the same as `shred` on a journaled FS), or `brew install coreutils &&
   gshred -u deploy.env` for the GNU equivalent.

---

## Daily usage

- **Automatic:** launchd kicks `main.py` at 07:00 local every day. It
  crawls new candidates and extracts content. Cowork's saved daily task
  scores them and triggers Telegram notifications.
- **On-demand brainstorm:** open a Cowork chat and type
  `/vf-brainstorm 42` to generate 5 GenK-style ideas for article #42.
  The skill PATCHes them back to the VPS so they persist.
- **Inspect state:** `curl -H "Authorization: Bearer $API_TOKEN" https://tlinh.duyet.vn/api/articles?stage=scored | jq`.

---

## File layout

```
vin-automate/
├── api_client.py             # httpx wrapper with bearer auth + retry
├── crawl.py                  # RSS fetch + canonical resolve + POST
├── extract.py                # trafilatura + Jina fallback + PATCH
├── main.py                   # daily orchestrator (launchd entry)
├── config.py                 # Mac-side topics + tunables (no DB)
├── install.sh                # Mac venv + deps (idempotent, slim)
├── requirements.txt          # pinned Python deps
├── com.tlinh.crawl.plist     # launchd daily job
│
├── vps/                      # PHP backend (deployed to tlinh.duyet.vn)
│   ├── public/index.php
│   ├── src/
│   │   ├── bootstrap.php     # env loader + autoloader + boot invariants
│   │   ├── Db.php Auth.php Router.php Json.php Hashing.php Telegram.php
│   │   └── routes/
│   │       ├── articles.php  # POST/GET/PATCH-extract
│   │       ├── score.php brainstorm.php fail.php
│   │       ├── lock.php      # acquire/heartbeat/release
│   │       ├── notify.php    # 2-phase claim w/ heartbeat
│   │       └── admin.php     # localhost-only inject-test
│   └── schema.sql            # CREATE TABLE IF NOT EXISTS
│
├── scripts/
│   ├── deploy.sh             # Phase A orchestrator
│   ├── dns_setup.sh          # CF A record upsert + dedup
│   └── telegram-nonce-helper.sh
│
├── SKILLS/
│   └── vf-brainstorm.skill        # Cowork slash skill (HTTP-based)
│
├── cowork-task-prompt.md     # paste into Cowork /schedule UI
├── scoring-rubric.md         # editorial 1–5 rubric (unchanged from v1)
├── brainstorm-guidelines.md  # GenK editorial style guide
│
├── PLAN-v2.md                # authoritative spec (Codex-approved, 9 rounds)
├── E2E-VALIDATION-v2.md      # acceptance scenarios for §13 matrix
└── README.md                 # this file
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `deploy.sh` exits at Step 6 with "wildcard cert missing" | `*.duyet.vn` cert not installed | SSH to VPS as root: `certbot certonly --manual --preferred-challenges dns -d "*.duyet.vn" -d duyet.vn` |
| `/api/health` returns 503 `{"db":"disconnected"}` | MariaDB down or grants missing | SSH to VPS: `mariadb -u tlinh -p tlinh_news -e 'SELECT 1'` |
| Cowork bash exits 1 with `[error] SKILLS/.env missing` | Phase B Step 10 skipped | Create `SKILLS/.env` per PLAN-v2 §10 Step 10 |
| Telegram message never arrives after Run now | Notify 2-phase Telegram retry exhausted | `journalctl -u php8.5-fpm \| grep 'notify\\.'` on VPS for `retry_after_exceeded_cap` |
| launchd kickstart silent | venv missing or import error | `tail -20 logs/crawl.err` |
| `crawl.py` reports "api unreachable" | DNS not propagated yet | wait 5 min, or `dig +short @1.1.1.1 tlinh.duyet.vn` |

---

## Known limitations

- Mac-only client. Linux Mac alternative deferred.
- Single user, single topic. Multi-user requires per-user API tokens + per-user DB rows.
- Mac sleep → cron misses run (no catch-up). Acceptable for daily cadence.
- Cowork sandbox bash is Linux; can't auto-deploy from Cowork. Deploy is one-time manual via Mac Terminal.
- Telegram bot is per-chat. Multi-channel routing deferred.
- No web UI; Telegram + Cowork chat + Terminal queries only.

---

## Code-review trail

Every commit went through [Codex](https://github.com/openai/codex) adversarial
review until APPROVE before merging:

| Component | Plan review | Impl review |
|---|---|---|
| PLAN-v2 (architecture spec) | 9 rounds, 22 issues | n/a |
| Task 1 schema.sql | n/a | 2 rounds, 1 issue |
| Tasks 2+7 backend skeleton + Hashing | n/a | 2 rounds, 3 issues |
| Tasks 3-6 PHP route handlers + Telegram | n/a | 4 rounds, 6 issues |
| Tasks 8-9 deploy/DNS/telegram scripts | n/a | 5 rounds, 11 issues |
| Tasks 10-12 Mac client (api_client + crawl + extract) | n/a | 3 rounds, 5 issues |
| Tasks 13-14 Cowork prompt + vf-brainstorm skill (formerly idea-brainstormer) | n/a | 6 rounds, 15 issues |
| Tasks 15-16 launchd plist + main.py + install.sh | n/a | 3 rounds, 4 issues |

Total: **34 review rounds, 67 issues resolved** before any commit landed.

---

## Credits

- Architecture: collaborative design between user + Claude Opus 4.7 (1M ctx)
- Adversarial review: Codex CLI (OpenAI), 34 rounds across plan + impl reviews
- Editorial voice: GenK.vn

---

## License

MIT.
