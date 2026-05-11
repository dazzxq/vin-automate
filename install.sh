#!/usr/bin/env bash
# install.sh v2 — slim installer for vin-automate (Mac side)
#
# Idempotent: venv + Python deps + logs/ + Mac-side prerequisites. No SQLite,
# no schema init, no BOOTSTRAP — the VPS is the single source of truth.
# Called by scripts/deploy.sh Step 8; safe to re-run standalone.
#
# Exit codes:
#   0  — success (or already configured)
#   1  — generic error
#   2  — unsupported OS
#   3  — unsupported macOS version (<13)
#   5  — Python install failed
#   6  — pip install failed
#  78  — sudo required for Xcode Command Line Tools (manual step)

set -u
set -o pipefail

readonly SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

log()       { printf '[info]    %s\n' "$*"; }
log_check() { printf '[check]   %s\n' "$*"; }
log_inst()  { printf '[install] %s\n' "$*"; }
log_ok()    { printf '[ok]      %s\n' "$*"; }
log_warn()  { printf '[warn]    %s\n' "$*" >&2; }
log_err()   { printf '[error]   %s\n' "$*" >&2; }

fatal() {
    local rc="$1"; shift
    log_err "$*"
    exit "$rc"
}

# --- Step 1: OS check ---------------------------------------------------
step_os_check() {
    log_check "OS = $(uname -s); version = $(sw_vers -productVersion 2>/dev/null || echo 'unknown')"
    local kernel; kernel="$(uname -s)"
    if [[ "$kernel" != "Darwin" ]]; then
        fatal 2 "Only macOS is supported. Detected: $kernel."
    fi
    local product_version major
    product_version="$(sw_vers -productVersion 2>/dev/null || echo "")"
    [[ -n "$product_version" ]] || fatal 3 "Could not detect macOS version."
    major="${product_version%%.*}"
    if ! [[ "$major" =~ ^[0-9]+$ ]] || (( major < 13 )); then
        fatal 3 "macOS 13 (Ventura) or later required. You have $product_version."
    fi
    log_ok "macOS $product_version (>= 13)"
}

# --- Step 2: arch + Homebrew prefix -------------------------------------
step_arch() {
    local arch; arch="$(uname -m)"
    case "$arch" in
        arm64)  BREW_PREFIX="/opt/homebrew" ;;
        x86_64) BREW_PREFIX="/usr/local" ;;
        *)      fatal 2 "Unsupported architecture: $arch" ;;
    esac
    log_ok "Architecture $arch → BREW_PREFIX=$BREW_PREFIX"
}

# --- Step 3: Xcode Command Line Tools ----------------------------------
step_xcode_clt() {
    log_check "Xcode Command Line Tools"
    if xcode-select -p >/dev/null 2>&1; then
        log_ok "Xcode CLT installed: $(xcode-select -p)"
        return 0
    fi
    if [[ -x "$BREW_PREFIX/bin/brew" ]] || command -v brew >/dev/null 2>&1; then
        log_warn "Xcode CLT not detected but brew is available; continuing."
        return 0
    fi
    log_err "Xcode CLT missing. Run 'xcode-select --install' in Terminal, then re-run."
    exit 78
}

# --- Step 4: Homebrew --------------------------------------------------
step_homebrew() {
    if [[ -x "$BREW_PREFIX/bin/brew" ]]; then
        log_ok "Homebrew already installed: $BREW_PREFIX/bin/brew"
    else
        log_inst "Homebrew not found — installing (NONINTERACTIVE)"
        if ! NONINTERACTIVE=1 CI=1 /bin/bash -c \
            "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"; then
            fatal 1 "Homebrew install script failed."
        fi
        log_ok "Homebrew installed"
    fi
    [[ -x "$BREW_PREFIX/bin/brew" ]] || fatal 1 "Expected brew at $BREW_PREFIX/bin/brew but not found."
    eval "$("$BREW_PREFIX/bin/brew" shellenv)"
    log_ok "brew shellenv evaluated"
    command -v brew >/dev/null 2>&1 || fatal 1 "brew still not on PATH after shellenv eval."
}

# --- Step 5: Python 3.11+ ----------------------------------------------
step_python() {
    if command -v python3.11 >/dev/null 2>&1; then
        log_ok "python3.11: $(command -v python3.11)"
        PYTHON_BIN="$(command -v python3.11)"
        return 0
    fi
    if command -v python3 >/dev/null 2>&1; then
        local ver maj min
        ver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")"
        maj="${ver%%.*}"; min="${ver##*.}"
        if [[ "$maj" == "3" ]] && (( min >= 11 )); then
            log_ok "python3 (>=3.11) found: $(command -v python3) ($ver)"
            PYTHON_BIN="$(command -v python3)"
            return 0
        fi
    fi
    log_inst "Python 3.11 not found — brew install python@3.11"
    brew install python@3.11 || fatal 5 "brew install python@3.11 failed."
    eval "$("$BREW_PREFIX/bin/brew" shellenv)"
    if command -v python3.11 >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python3.11)"
        log_ok "python3.11 installed: $PYTHON_BIN"
    else
        local candidate="$BREW_PREFIX/opt/python@3.11/bin/python3.11"
        [[ -x "$candidate" ]] || fatal 5 "python3.11 still missing after brew install."
        PYTHON_BIN="$candidate"
        log_ok "python3.11 installed at $PYTHON_BIN (absolute path)"
    fi
}

# --- Step 6: venv + deps -----------------------------------------------
step_venv() {
    local venv_dir="$SCRIPT_DIR/.venv"
    local req="$SCRIPT_DIR/requirements.txt"
    local stamp="$venv_dir/.requirements.sha256"
    local fresh_venv=0
    if [[ -d "$venv_dir" && -x "$venv_dir/bin/python" ]]; then
        log_ok ".venv already exists at $venv_dir"
    else
        log_inst "Creating venv at $venv_dir"
        "$PYTHON_BIN" -m venv "$venv_dir" || fatal 5 "Failed to create venv."
        log_ok "venv created"
        fresh_venv=1
    fi

    # Idempotent dep install: hash requirements.txt and compare to the stamp
    # written after a successful install. Skip pip entirely if the stamp
    # matches — second run is then a true no-op (per Task 16 acceptance).
    local current_hash stored_hash
    current_hash="$(shasum -a 256 "$req" | awk '{print $1}')"
    stored_hash=""
    [[ -f "$stamp" ]] && stored_hash="$(cat "$stamp" 2>/dev/null || true)"

    if [[ "$fresh_venv" -eq 0 && "$current_hash" == "$stored_hash" ]]; then
        log_ok "deps unchanged (requirements.txt hash matches stamp) — skipping pip"
        return 0
    fi

    log_check "Installing/updating Python deps"
    # pip self-upgrade only on fresh venv; steady-state reuses whatever pip
    # the venv shipped with.
    if [[ "$fresh_venv" -eq 1 ]]; then
        "$venv_dir/bin/pip" install --quiet --upgrade pip || log_warn "pip self-upgrade failed; continuing."
    fi
    "$venv_dir/bin/pip" install --quiet -r "$req" \
        || fatal 6 "pip install -r requirements.txt failed."
    printf '%s\n' "$current_hash" > "$stamp"
    log_ok "Python deps installed (httpx, feedparser, trafilatura, python-dotenv)"
}

# --- Step 7: logs/ directory -------------------------------------------
step_logs() {
    mkdir -p "$SCRIPT_DIR/logs"
    log_ok "logs/ directory ready"
}

# --- Step 8: Network sanity --------------------------------------------
step_network() {
    log_check "Network sanity (PyPI, GitHub, Telegram, news.google)"
    local fail=0
    for url in "https://pypi.org/" "https://github.com/" "https://api.telegram.org/" "https://news.google.com/"; do
        if curl -sI -m 5 -o /dev/null "$url"; then
            log_ok "reachable: $url"
        else
            log_warn "unreachable: $url"
            fail=1
        fi
    done
    (( fail == 1 )) && log_warn "Some endpoints unreachable; pipeline may fail at runtime."
}

# --- Summary -----------------------------------------------------------
print_summary() {
    cat <<EOF

================================================================
 vin-automate v2 — install.sh complete
================================================================
 Project:       $SCRIPT_DIR
 Python:        $PYTHON_BIN
 venv:          $SCRIPT_DIR/.venv
 logs/:         $SCRIPT_DIR/logs
 Mac .env:      $([[ -f "$SCRIPT_DIR/.env" ]] && echo present || echo "NOT YET (run scripts/deploy.sh)")

 Next:
   1. scripts/deploy.sh   (orchestrates DNS, VPS setup, this script's smoke test)
   2. Cowork setup steps 10–13 of PLAN-v2 §10 (Phase B manual verify).
================================================================
EOF
}

main() {
    cd "$SCRIPT_DIR"
    log "Starting install.sh v2 (idempotent)"
    step_os_check
    step_arch
    step_xcode_clt
    step_homebrew
    step_python
    step_venv
    step_logs
    step_network
    print_summary
    log_ok "Done. install.sh finished without errors."
}

main "$@"
