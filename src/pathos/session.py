"""JSONL discovery and tmux helpers for Claude Code sessions."""
import json
import subprocess
import time
from pathlib import Path

SESSIONS_DIR = Path.home() / ".claude" / "sessions"
PROJECTS_DIR = Path.home() / ".claude" / "projects"


def find_jsonl(tmux_session: str, timeout_sec: int = 20) -> Path | None:
    """Get the agent's JSONL path by reading its session state file.
    Checks the pane PID and its children (for shell-wrapped debug mode)."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            pane_pid = subprocess.run(
                ["tmux", "display-message", "-t", tmux_session, "-p", "#{pane_pid}"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            pids = [pane_pid]
            children = subprocess.run(
                ["pgrep", "-P", pane_pid],
                capture_output=True, text=True,
            )
            pids.extend(children.stdout.strip().splitlines())
            for pid in pids:
                state_file = SESSIONS_DIR / f"{pid.strip()}.json"
                if state_file.exists():
                    data = json.loads(state_file.read_text())
                    sid, cwd = data.get("sessionId"), data.get("cwd")
                    if sid and cwd:
                        return PROJECTS_DIR / cwd.replace("/", "-") / f"{sid}.jsonl"
        except Exception:
            pass
        time.sleep(1)
    return None


def session_alive(tmux_session: str) -> bool:
    return subprocess.run(
        ["tmux", "has-session", "-t", tmux_session],
        capture_output=True,
    ).returncode == 0


def setup_tmux_keys(tmux_session: str):
    """Enable extended-keys for Shift+Enter passthrough (tmux 3.2+)."""
    subprocess.run(
        ["tmux", "set-window-option", "-t", tmux_session, "extended-keys", "on"],
        capture_output=True,
    )
    subprocess.run(
        ["tmux", "set-window-option", "-t", tmux_session, "allow-passthrough", "on"],
        capture_output=True,
    )


def wait_for_idle(tmux_session: str, timeout_sec: int = 60) -> bool:
    """Wait until Claude shows its idle prompt before injecting."""
    prompt_char = "❯"
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", tmux_session, "-p"],
                capture_output=True, text=True, check=True,
            )
            for line in result.stdout.strip().splitlines()[-5:]:
                if prompt_char in line:
                    return True
        except subprocess.CalledProcessError:
            pass
        time.sleep(2)
    return False


SHIFT_ENTER = "\x1b[13;2u"


def inject_tmux(tmux_session: str, text: str, retries: int = 3):
    """Send text into a tmux session with retry on transient errors.

    Newlines become Shift+Enter (CSI u) so the message stays as one input.
    """
    lines = text.split("\n")
    for attempt in range(retries):
        try:
            for i, line in enumerate(lines):
                if line:
                    subprocess.run(
                        ["tmux", "send-keys", "-t", tmux_session, "-l", line],
                        capture_output=True, text=True, check=True,
                    )
                if i < len(lines) - 1:
                    subprocess.run(
                        ["tmux", "send-keys", "-t", tmux_session, "-l", SHIFT_ENTER],
                        capture_output=True, text=True, check=True,
                    )
            subprocess.run(
                ["tmux", "send-keys", "-t", tmux_session, "Enter"],
                capture_output=True, text=True, check=True,
            )
            return
        except subprocess.CalledProcessError as e:
            err = (e.stderr or "").lower()
            if "not found" in err or "no such" in err or "can't find" in err:
                raise
            if attempt < retries - 1:
                time.sleep(1 + attempt)
            else:
                raise
