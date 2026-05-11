#!/usr/bin/env python3
"""Terminal-fallback TUI for non-tech users when Cowork can't run BOOTSTRAP.md.

Implements the SAME deterministic nonce flow + skill install as BOOTSTRAP.md.
Does NOT attempt Cowork UI automation — its job is to get the user from
"Cowork can't onboard me" to "everything except scheduler is configured".

Usage:
    .venv/bin/python setup_helper.py

Prerequisites:
    bash install.sh   (must succeed before running this script)
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx


PROJECT_ROOT = Path(__file__).resolve().parent
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
ENV_PATH = PROJECT_ROOT / ".env"
SKILLS_SRC = PROJECT_ROOT / "SKILLS"
COWORK_TASK_PROMPT = PROJECT_ROOT / "cowork-task-prompt.md"
NONCE_POLL_DEADLINE_SECONDS = 120
TELEGRAM_TOKEN_MAX_ATTEMPTS = 3


def info(msg: str) -> None:
    print(f"\033[36m[info]\033[0m {msg}")


def ok(msg: str) -> None:
    print(f"\033[32m[ok]\033[0m {msg}")


def warn(msg: str) -> None:
    print(f"\033[33m[warn]\033[0m {msg}")


def err(msg: str) -> None:
    print(f"\033[31m[error]\033[0m {msg}")


def ask(prompt: str) -> str:
    try:
        return input(f"\033[1m{prompt}\033[0m ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        err("aborted")
        sys.exit(1)


def ask_yn(prompt: str, default_yes: bool = False) -> bool:
    suffix = " [Y/n]" if default_yes else " [y/N]"
    while True:
        answer = ask(prompt + suffix).lower()
        if not answer:
            return default_yes
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        warn("Hãy trả lời y hoặc n.")


# --- Step 0: precondition --------------------------------------------------

def step_precondition() -> None:
    if not VENV_PYTHON.exists():
        err("Chưa có .venv. Chạy `bash install.sh` trước rồi quay lại đây.")
        sys.exit(1)


# --- Step 1: Telegram credentials -----------------------------------------

def validate_telegram_token(token: str) -> dict | None:
    try:
        resp = httpx.get(
            f"https://api.telegram.org/bot{token}/getMe", timeout=10
        )
    except httpx.HTTPError as e:
        err(f"Network error gọi getMe: {e}")
        return None
    if resp.status_code != 200:
        err(f"Telegram getMe trả về HTTP {resp.status_code}")
        return None
    data = resp.json()
    if not data.get("ok"):
        err(f"Telegram getMe ok=false: {data}")
        return None
    return data["result"]


def collect_telegram_credentials() -> tuple[str, str]:
    info("=== Telegram setup ===")
    print()
    print("Nếu bạn chưa có bot:")
    print("  1. Mở https://t.me/BotFather trên Telegram")
    print("  2. Gõ /newbot, làm theo hướng dẫn, copy TOKEN.")
    print()

    token: str | None = None
    bot_info: dict | None = None
    for attempt in range(1, TELEGRAM_TOKEN_MAX_ATTEMPTS + 1):
        token = ask(f"Paste TELEGRAM_BOT_TOKEN (attempt {attempt}/{TELEGRAM_TOKEN_MAX_ATTEMPTS}):")
        if not token:
            err("Token trống.")
            continue
        bot_info = validate_telegram_token(token)
        if bot_info is not None:
            ok(f"Token valid: bot @{bot_info.get('username')} ({bot_info.get('first_name')})")
            break
        warn("Token sai hoặc bot bị disable. Kiểm tra lại trong @BotFather.")
    if bot_info is None or token is None:
        err("Đã thử 3 lần, vẫn không validate được token. Thoát.")
        sys.exit(1)

    chat_id = detect_chat_id_via_nonce(token, bot_info)

    # Test send
    info("Gửi test message...")
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "Setup OK — VinFast pipeline ready."},
            timeout=10,
        )
    except httpx.HTTPError as e:
        err(f"Lỗi gửi test: {e}")
        sys.exit(1)
    if resp.status_code != 200 or not resp.json().get("ok"):
        err(f"Test send fail: {resp.status_code} {resp.text[:200]}")
        sys.exit(1)
    if not ask_yn("Bạn vừa nhận tin nhắn 'Setup OK — VinFast pipeline ready.' trong Telegram?"):
        err("Test message không đến. Kiểm tra chat_id, bot block, network.")
        sys.exit(1)
    ok("Telegram credentials xác nhận.")
    return token, chat_id


def detect_chat_id_via_nonce(token: str, bot_info: dict) -> str:
    """Deterministic nonce flow per BOOTSTRAP.md Step 3.3.

    Records baseline_update_id, asks user to send a unique nonce, polls
    getUpdates?offset=baseline+1 for all newer updates until the nonce match
    appears or deadline elapses.
    """
    while True:
        # Baseline
        try:
            resp = httpx.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"timeout": 0, "limit": 100},
                timeout=10,
            )
        except httpx.HTTPError as e:
            err(f"Network error getUpdates baseline: {e}")
            sys.exit(1)
        baseline = 0
        if resp.status_code == 200 and resp.json().get("ok"):
            updates = resp.json().get("result", [])
            if updates:
                baseline = max(u["update_id"] for u in updates)
        info(f"Baseline update_id = {baseline}")

        nonce = secrets.token_hex(6)
        print()
        print(f"\033[1mNONCE: {nonce}\033[0m")
        print()
        username = bot_info.get("username")
        print(f"Hành động cần làm trong Telegram:")
        print(f"  1. Mở chat với bot @{username}")
        print(f"  2. Bấm /start (lần đầu)")
        print(f"  3. Gửi CHÍNH XÁC chuỗi sau cho bot: {nonce}")
        print()

        deadline = time.time() + NONCE_POLL_DEADLINE_SECONDS
        chat_id: str | None = None
        chat_name: str = ""
        warned = False
        while time.time() < deadline:
            try:
                resp = httpx.get(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params={"offset": baseline + 1, "timeout": 10, "limit": 100},
                    timeout=15,
                )
            except httpx.HTTPError as e:
                if not warned:
                    warn(f"Transient network err (will retry): {e}")
                    warned = True
                time.sleep(2)
                continue
            if resp.status_code != 200:
                time.sleep(2)
                continue
            data = resp.json()
            if not data.get("ok"):
                time.sleep(2)
                continue
            for update in data.get("result", []):
                msg = update.get("message") or {}
                text = (msg.get("text") or "").strip()
                if text == nonce:
                    chat = msg.get("chat", {})
                    chat_id = str(chat.get("id"))
                    chat_name = chat.get("first_name") or chat.get("title") or "unknown"
                    break
            if chat_id is not None:
                break
            time.sleep(2)

        if chat_id is None:
            warn(f"Timeout {NONCE_POLL_DEADLINE_SECONDS}s. Bạn đã gửi nonce vào đúng bot?")
            if not ask_yn("Thử lại với nonce mới?"):
                err("Aborted.")
                sys.exit(1)
            continue

        ok(f"Phát hiện chat: {chat_name} (id={chat_id})")
        if ask_yn("Đúng chat của bạn?", default_yes=True):
            return chat_id
        warn("Restart with new nonce.")


def write_env_atomic(token: str, chat_id: str) -> None:
    tmp = ENV_PATH.with_suffix(".tmp")
    tmp.write_text(
        f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={chat_id}\n",
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    tmp.replace(ENV_PATH)
    ok(f".env written ({ENV_PATH})")


# --- Step 2: Install slash skills -----------------------------------------

def install_skills() -> None:
    """Probe for an existing Cowork skill directory and symlink our skills.

    Mirrors the BOOTSTRAP.md probe exactly: check `-d` for each candidate
    directory in order and use the first one that ACTUALLY exists. If neither
    exists we refuse to guess (Cowork may be using an entirely different path
    on this machine); the user is routed to README's Manual skill install.
    """
    info("=== Slash skill install ===")
    candidates = [
        Path.home() / "Library" / "Application Support" / "Claude" / "skills",
        Path.home() / ".claude" / "skills",
    ]
    skill_dir: Path | None = None
    for c in candidates:
        if c.is_dir():
            skill_dir = c
            break
    if skill_dir is None:
        warn("Không phát hiện thư mục Cowork skill nào đang tồn tại.")
        warn(f"Đã thử: {[str(c) for c in candidates]}")
        warn("Skill install BỎ QUA — xem README mục 'Manual skill install' để cài tay.")
        return

    for name in ("idea-brainstormer", "setup"):
        src = SKILLS_SRC / f"{name}.skill"
        dst_dir = skill_dir / name
        dst = dst_dir / "SKILL.md"
        if not src.exists():
            warn(f"Source skill missing: {src}")
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        dst.symlink_to(src)
        ok(f"Linked {dst} → {src}")


# --- Step 3: Schema init (idempotent) -------------------------------------

def init_schema() -> None:
    info("=== Schema init ===")
    res = subprocess.run(
        [str(VENV_PYTHON), "-c", "from db import init_db; init_db()"],
        cwd=str(PROJECT_ROOT),
    )
    if res.returncode != 0:
        err("init_db() failed")
        sys.exit(1)
    ok("Schema verified")


# --- Step 4: Show cowork-task-prompt instructions -------------------------

def print_next_steps() -> None:
    print()
    print("=" * 60)
    print(" Terminal setup DONE — except the Cowork scheduled task")
    print("=" * 60)
    print()
    print("Cần làm tiếp trong Claude Desktop / Cowork:")
    print()
    print("1. Mở Cowork sidebar → Scheduled → '+' New Task")
    print("   - Name: vinfast-pipeline")
    print("   - Frequency: Hourly")
    print("2. Paste TOÀN BỘ nội dung sau vào prompt body của task:")
    print()
    print("-" * 60)
    try:
        prompt_body = COWORK_TASK_PROMPT.read_text(encoding="utf-8")
        print(prompt_body)
    except OSError as e:
        warn(f"Không đọc được {COWORK_TASK_PROMPT}: {e}")
        print(f"(Đọc file thủ công: {COWORK_TASK_PROMPT})")
    print("-" * 60)
    print()
    print("3. Save task.")
    print("4. Click 'Run now' để verify task chạy đúng.")
    print()
    print("Để arm scheduler verification (hex token, idempotent per token):")
    # Generate a real hex token so the example is copy-pasteable.
    sample_token = secrets.token_hex(6)
    print(f"     {VENV_PYTHON} crawl.py --inject-test {sample_token}")
    print("     # Sau đó bấm 'Run now' trong Cowork, đợi tin nhắn Telegram")
    print(f"     # chứa chuỗi: {sample_token}")
    print()


def main() -> int:
    print("\033[1mVinFast News Pipeline — Terminal setup helper\033[0m")
    print()
    step_precondition()

    if ENV_PATH.exists():
        if not ask_yn("Đã có .env — refresh credentials?"):
            ok(".env existing kept.")
        else:
            token, chat_id = collect_telegram_credentials()
            write_env_atomic(token, chat_id)
    else:
        token, chat_id = collect_telegram_credentials()
        write_env_atomic(token, chat_id)

    install_skills()
    init_schema()
    print_next_steps()
    ok("setup_helper.py finished.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
