#!/usr/bin/env bash
# Wrapper invoked by com.tlinh.crawl launchd job. Lives in scripts/ so the
# plist's ProgramArguments can reference a stable absolute path without
# needing to inline a `bash -c` string (which then needs careful shell
# quoting when the repo path contains spaces / shell-special characters).
#
# Steps:
#   1. cd into the repo (its parent of scripts/).
#   2. Source .venv/bin/activate so $VIRTUAL_ENV + PATH are set. We MUST
#      source rather than invoke .venv/bin/python directly because launchd
#      resolves symlinks on the Program path, which dereferences the venv
#      shim and loses site-packages.
#   3. exec python main.py.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ ! -f .venv/bin/activate ]; then
  echo "[error] .venv/bin/activate missing — run bash install.sh first" >&2
  exit 1
fi
# shellcheck disable=SC1091
. .venv/bin/activate

exec python main.py
