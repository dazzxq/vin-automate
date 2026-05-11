#!/usr/bin/env bash
# install.sh v1 — Bootstrap installer for VinFast News Pipeline
#
# Idempotent. Auto-installs Homebrew + Python 3.11 + venv + Python deps + logs dir.
# Does NOT initialize the DB schema (added in install.sh v2 by Task 3).
#
# Exit codes:
#   0  — success (or already configured)
#   1  — generic error
#   2  — unsupported OS
#   3  — unsupported macOS version (<13)
#   4  — sqlite3 missing
#   5  — Python install failed
#   6  — pip install failed
#   7  — network unreachable
#  78  — sudo required for Xcode Command Line Tools (user must run xcode-select --install in Terminal)
#
# Tagged log lines: [check], [install], [ok], [warn], [error], [info]

set -u
set -o pipefail

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

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

# -----------------------------------------------------------------------------
# Step 1 — OS check
# -----------------------------------------------------------------------------

step_os_check() {
    log_check "OS = $(uname -s); version = $(sw_vers -productVersion 2>/dev/null || echo 'unknown')"

    local kernel
    kernel="$(uname -s)"
    if [[ "$kernel" != "Darwin" ]]; then
        fatal 2 "Only macOS is supported. Detected: $kernel. See README 'Roadmap' for Linux/Windows."
    fi

    local product_version major
    product_version="$(sw_vers -productVersion 2>/dev/null || echo "")"
    if [[ -z "$product_version" ]]; then
        fatal 3 "Could not detect macOS version (sw_vers failed)."
    fi
    major="${product_version%%.*}"
    if ! [[ "$major" =~ ^[0-9]+$ ]] || (( major < 13 )); then
        fatal 3 "macOS 13 (Ventura) or later required. You have $product_version."
    fi
    log_ok "macOS $product_version (>= 13)"
}

# -----------------------------------------------------------------------------
# Step 2 — Architecture + Homebrew prefix
# -----------------------------------------------------------------------------

step_arch() {
    local arch
    arch="$(uname -m)"
    case "$arch" in
        arm64)  BREW_PREFIX="/opt/homebrew" ;;
        x86_64) BREW_PREFIX="/usr/local" ;;
        *)      fatal 2 "Unsupported architecture: $arch (expected arm64 or x86_64)" ;;
    esac
    log_ok "Architecture $arch → BREW_PREFIX=$BREW_PREFIX"
}

# -----------------------------------------------------------------------------
# Step 3 — Xcode Command Line Tools detection
#
# Homebrew requires Xcode CLT. Installing CLT requires sudo/GUI consent which
# cannot be granted from a non-interactive Cowork bash session. Detect early
# and exit with code 78 so the orchestrator (BOOTSTRAP.md / /setup) can route
# the user to Terminal.
# -----------------------------------------------------------------------------

step_xcode_clt() {
    log_check "Xcode Command Line Tools (required by Homebrew)"
    if xcode-select -p >/dev/null 2>&1; then
        log_ok "Xcode CLT installed: $(xcode-select -p)"
        return 0
    fi
    # CLT missing. If brew is already installed somewhere (even outside PATH), we can skip.
    # Check the canonical arch-specific install location, not just PATH (per Codex ISSUE-1).
    if [[ -x "$BREW_PREFIX/bin/brew" ]] || command -v brew >/dev/null 2>&1; then
        log_warn "Xcode CLT not detected but brew is available; continuing."
        return 0
    fi
    log_err "Xcode Command Line Tools are not installed."
    log_err "This step requires Terminal + sudo and cannot be done from Cowork."
    log_err "Please open Terminal and run: xcode-select --install"
    log_err "Then re-run: bash install.sh"
    exit 78
}

# -----------------------------------------------------------------------------
# Step 4 — Homebrew (install if missing, eval shellenv either way)
# -----------------------------------------------------------------------------

step_homebrew() {
    # Authoritative existence check is the canonical arch-specific path,
    # NOT `command -v brew` — brew may be installed but not on the current
    # shell's PATH (Cowork's non-login shell). Per Codex ISSUE-1.
    if [[ -x "$BREW_PREFIX/bin/brew" ]]; then
        log_ok "Homebrew already installed: $BREW_PREFIX/bin/brew"
    else
        log_inst "Homebrew not found — installing (NONINTERACTIVE)"
        # NONINTERACTIVE=1 suppresses the 'Press RETURN to continue' prompt.
        # CI=1 also helps some sub-steps stay non-interactive.
        if ! NONINTERACTIVE=1 CI=1 /bin/bash -c \
            "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"; then
            fatal 1 "Homebrew install script failed. Check network and try again."
        fi
        log_ok "Homebrew installed"
    fi

    # Always eval shellenv so brew is on PATH in the CURRENT shell session.
    # Homebrew's installer adds shellenv to ~/.zprofile but does NOT export into
    # the current process — without this eval, the next 'brew install' call fails.
    if [[ -x "$BREW_PREFIX/bin/brew" ]]; then
        eval "$("$BREW_PREFIX/bin/brew" shellenv)"
        log_ok "brew shellenv evaluated (PATH includes $BREW_PREFIX/bin)"
    else
        fatal 1 "Expected brew at $BREW_PREFIX/bin/brew but not found after install."
    fi

    # Sanity: brew callable in this shell now
    if ! command -v brew >/dev/null 2>&1; then
        fatal 1 "brew still not on PATH after shellenv eval."
    fi
}

# -----------------------------------------------------------------------------
# Step 5 — Python 3.11+
# -----------------------------------------------------------------------------

step_python() {
    # Prefer an explicit python3.11 binary; otherwise check generic python3 >=3.11.
    if command -v python3.11 >/dev/null 2>&1; then
        log_ok "python3.11 already installed: $(command -v python3.11)"
        PYTHON_BIN="$(command -v python3.11)"
        return 0
    fi

    if command -v python3 >/dev/null 2>&1; then
        local ver
        ver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")"
        local maj min
        maj="${ver%%.*}"; min="${ver##*.}"
        if [[ "$maj" == "3" ]] && (( min >= 11 )); then
            log_ok "python3 (>=3.11) found: $(command -v python3) ($ver)"
            PYTHON_BIN="$(command -v python3)"
            return 0
        fi
    fi

    log_inst "Python 3.11 not found — brew install python@3.11"
    if ! brew install python@3.11; then
        fatal 5 "brew install python@3.11 failed."
    fi
    # Re-eval shellenv in case brew linked new binaries
    eval "$("$BREW_PREFIX/bin/brew" shellenv)"

    if command -v python3.11 >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python3.11)"
        log_ok "python3.11 installed: $PYTHON_BIN"
    else
        # Fall back to the brew-managed path explicitly
        local candidate="$BREW_PREFIX/opt/python@3.11/bin/python3.11"
        if [[ -x "$candidate" ]]; then
            PYTHON_BIN="$candidate"
            log_ok "python3.11 installed at $PYTHON_BIN (not on PATH; using absolute path)"
        else
            fatal 5 "python@3.11 brew formula reported success but python3.11 still missing."
        fi
    fi
}

# -----------------------------------------------------------------------------
# Step 6 — sqlite3 CLI (built in to macOS; sanity check only)
# -----------------------------------------------------------------------------

step_sqlite3() {
    if command -v sqlite3 >/dev/null 2>&1; then
        log_ok "sqlite3 available: $(sqlite3 --version | awk '{print $1}')"
    else
        fatal 4 "sqlite3 CLI not found. It should ship with macOS — please check your installation."
    fi
}

# -----------------------------------------------------------------------------
# Step 7 — venv + pip install
# -----------------------------------------------------------------------------

step_venv() {
    local venv_dir="$SCRIPT_DIR/.venv"
    if [[ -d "$venv_dir" && -x "$venv_dir/bin/python" ]]; then
        log_ok ".venv already exists at $venv_dir"
    else
        log_inst "Creating venv at $venv_dir"
        if ! "$PYTHON_BIN" -m venv "$venv_dir"; then
            fatal 5 "Failed to create venv. Try removing $venv_dir and re-running."
        fi
        log_ok "venv created"
    fi

    log_check "Installing/updating Python deps"
    if ! "$venv_dir/bin/pip" install --quiet --upgrade pip; then
        log_warn "pip self-upgrade failed; continuing with bundled pip."
    fi
    if ! "$venv_dir/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"; then
        fatal 6 "pip install -r requirements.txt failed. Check $venv_dir for clues."
    fi
    log_ok "Python deps installed (trafilatura, feedparser, httpx, python-dotenv)"
}

# -----------------------------------------------------------------------------
# Step 8 — logs/ directory
# -----------------------------------------------------------------------------

step_logs() {
    mkdir -p "$SCRIPT_DIR/logs"
    log_ok "logs/ directory ready"
}

# -----------------------------------------------------------------------------
# Step 9 — Network sanity (Telegram + PyPI + GitHub)
# -----------------------------------------------------------------------------

step_network() {
    log_check "Network sanity (Telegram, PyPI, GitHub)"
    local fail=0
    for url in "https://api.telegram.org/" "https://pypi.org/" "https://github.com/"; do
        if curl -sI -m 5 -o /dev/null "$url"; then
            log_ok "reachable: $url"
        else
            log_warn "unreachable: $url (network may be flaky; pipeline may fail at runtime)"
            fail=1
        fi
    done
    if (( fail == 1 )); then
        log_warn "Some endpoints failed reachability check; continuing anyway."
    fi
}

# -----------------------------------------------------------------------------
# Step 10 — Summary
# -----------------------------------------------------------------------------

print_summary() {
    echo
    echo "================================================================"
    echo " VinFast News Pipeline — install.sh v1 complete"
    echo "================================================================"
    echo " Project:       $SCRIPT_DIR"
    echo " Python:        $PYTHON_BIN"
    echo " venv:          $SCRIPT_DIR/.venv"
    echo " Homebrew:      $(command -v brew || echo 'not on PATH')"
    echo " logs/:         $SCRIPT_DIR/logs"
    echo
    echo " Next steps:"
    echo "   1. Open Claude Desktop, add this folder to Cowork trusted folders."
    echo "   2. Paste BOOTSTRAP.md content into a Cowork chat to finish setup."
    echo "      (Or run: $SCRIPT_DIR/.venv/bin/python setup_helper.py for Terminal fallback)"
    echo "================================================================"
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

main() {
    cd "$SCRIPT_DIR"
    log "Starting install.sh v1 (Task 1, idempotent)"
    step_os_check
    step_arch
    step_xcode_clt
    step_homebrew
    step_python
    step_sqlite3
    step_venv
    step_logs
    step_network
    print_summary
    log_ok "Done. install.sh v1 finished without errors."
}

main "$@"
