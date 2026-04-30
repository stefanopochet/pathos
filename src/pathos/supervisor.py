"""Two-stage supervisor: Haiku triage → Sonnet validation → tmux injection."""
import json
import os
import select
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import GLOBAL_LOG, load_config, load_prompt
from .context import append_summary, extract_transcript, get_context, init_summary, maybe_compress
from .session import find_jsonl, inject_tmux, session_alive, setup_tmux_keys, wait_for_idle

INJECTION_TEMPLATE = (
    "[PATHOS] I spotted an issue while supervising your work. "
    "— {title} "
    "— {reason} "
    "— Please stop, review what happened, and fix it if you can. "
    "Otherwise, pause and let's align."
)


def log_entry(log_path: Path, entry: dict, agent_name: str | None = None):
    entry.setdefault("v", __version__)
    entry.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if agent_name:
        entry.setdefault("agent", agent_name)
    line = json.dumps(entry) + "\n"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(line)
    with open(GLOBAL_LOG, "a") as f:
        f.write(line)


def _projects_dir() -> Path:
    """Get the Claude Code projects dir for the current cwd."""
    cwd = str(Path.cwd())
    return Path.home() / ".claude" / "projects" / cwd.replace("/", "-")


def run_claude(model: str, prompt: str) -> tuple[str | None, str | None]:
    """Run claude -p with a given model. Returns (stdout, error)."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    proj = _projects_dir()
    before = set(proj.glob("*.jsonl")) if proj.exists() else set()

    try:
        proc = subprocess.run(
            ["claude", "-p",
             "--model", model,
             "--no-session-persistence",
             prompt],
            capture_output=True, text=True, timeout=300, env=env,
        )
    except subprocess.TimeoutExpired:
        return None, "timed out after 300s"
    finally:
        if proj.exists():
            for f in set(proj.glob("*.jsonl")) - before:
                f.unlink(missing_ok=True)

    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout or "").strip()[:200]

    return proc.stdout.strip(), None


def parse_triage_output(stdout: str) -> tuple[str, bool, str]:
    """Parse SUMMARY/VERDICT/REASON format from triage output."""
    summary = ""
    flagged = False
    reason = ""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("SUMMARY:"):
            summary = line.split(":", 1)[1].strip()
        elif line.startswith("VERDICT:"):
            verdict = line.split(":", 1)[1].strip()
            flagged = verdict.upper().startswith("FLAG")
        elif line.startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
    return summary, flagged, reason


def triage(jsonl: Path, since: int, session_id: str, config: dict) -> tuple[str, bool, str]:
    """Stage 1: fast scan. Returns (summary, flagged, reason)."""
    context = get_context(session_id, jsonl)
    transcript = extract_transcript(jsonl, since)
    prompt = load_prompt("triage.txt").format(
        jsonl=jsonl, since=since, context=context, transcript=transcript,
    )
    stdout, err = run_claude(config["triage_model"], prompt)

    if err:
        return "", False, f"triage error: {err}"

    return parse_triage_output(stdout)


def parse_validate_output(stdout: str) -> tuple[bool, str, str]:
    """Parse VERDICT/TITLE/REASON format from validator output."""
    confirmed = False
    title = ""
    reason = ""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("VERDICT:"):
            verdict = line.split(":", 1)[1].strip()
            confirmed = verdict.upper().startswith("CRITICAL")
        elif line.startswith("TITLE:"):
            title = line.split(":", 1)[1].strip()
        elif line.startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
    return confirmed, title, reason


def validate(jsonl: Path, since: int, lines: int, session_id: str,
             triage_summary: str, triage_reason: str, config: dict) -> tuple[bool, str, str]:
    """Stage 2: deep validation. Returns (confirmed, title, reason)."""
    context = get_context(session_id, jsonl)
    transcript = extract_transcript(jsonl, since)
    prompt = load_prompt("validate.txt").format(
        jsonl=jsonl, since=since, lines=lines,
        session_id=session_id,
        triage_summary=triage_summary,
        triage_reason=triage_reason,
        context=context,
        transcript=transcript,
    )
    stdout, err = run_claude(config["validate_model"], prompt)

    if err:
        return False, "", f"validate error: {err}"

    return parse_validate_output(stdout)


def play_alert(config: dict):
    """Run the configured alert command."""
    cmd = config.get("alert_command", "")
    if cmd:
        try:
            subprocess.Popen(cmd, shell=True)
        except Exception:
            pass


def _wait_for_write(fd: int, timeout_sec: int) -> bool:
    """Block until fd is written to, or timeout. Returns True if file changed."""
    kq = select.kqueue()
    ev = select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                       flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                       fflags=select.KQ_NOTE_WRITE)
    try:
        events = kq.control([ev], 1, timeout_sec)
        return len(events) > 0
    finally:
        kq.close()


def poll_loop(tmux_session: str, jsonl: Path, log_path: Path, poll_sec: int):
    config = load_config()
    setup_tmux_keys(tmux_session)
    since = sum(1 for _ in open(jsonl)) if jsonl.exists() else 0
    session_id = jsonl.stem
    agent_name = tmux_session

    init_summary(session_id, jsonl)
    log_entry(log_path, {"event": "watching", "mode": "kqueue", "heartbeat_sec": poll_sec}, agent_name)

    cycle = 0
    while True:
        if not session_alive(tmux_session):
            log_entry(log_path, {"event": "stopped", "reason": "tmux session gone"}, agent_name)
            return

        # Block until JSONL is written to (or heartbeat timeout)
        if jsonl.exists():
            size_before = jsonl.stat().st_size
            fd = os.open(str(jsonl), os.O_RDONLY)
            try:
                changed = _wait_for_write(fd, poll_sec)
            finally:
                os.close(fd)
            size_after = jsonl.stat().st_size if jsonl.exists() else 0
            if not changed:
                log_entry(log_path, {"event": "heartbeat", "size": size_before}, agent_name)
                continue
            log_entry(log_path, {"event": "kqueue_wake", "size_before": size_before, "size_after": size_after}, agent_name)
        else:
            log_entry(log_path, {"event": "waiting_for_jsonl", "path": str(jsonl)}, agent_name)
            time.sleep(poll_sec)
            continue

        cycle += 1

        try:
            new_jsonl = find_jsonl(tmux_session, timeout_sec=3)
            if new_jsonl and new_jsonl != jsonl:
                log_entry(log_path, {
                    "event": "session_changed",
                    "old": jsonl.stem, "new": new_jsonl.stem,
                }, agent_name)
                jsonl = new_jsonl
                since = 0
                session_id = jsonl.stem
                init_summary(session_id, jsonl)

            n = sum(1 for _ in open(jsonl))
            if n <= since:
                log_entry(log_path, {"event": "wake_no_new_lines", "lines": n, "since": since}, agent_name)
                continue

            log_entry(log_path, {"event": "wake", "cycle": cycle, "lines": n, "since": since}, agent_name)

            t0 = time.monotonic()
            summary, flagged, reason = triage(jsonl, since, session_id, config)
            triage_sec = round(time.monotonic() - t0, 1)
            log_entry(log_path, {
                "stage": "triage", "flagged": flagged,
                "lines": n, "summary": summary, "reason": reason,
                "duration_sec": triage_sec,
            }, agent_name)

            if summary:
                append_summary(session_id, summary)
                maybe_compress(session_id)

            if not flagged:
                since = n
                continue

            t0 = time.monotonic()
            confirmed, val_title, val_reason = validate(
                jsonl, since, n, session_id, summary, reason, config,
            )
            validate_sec = round(time.monotonic() - t0, 1)
            since = n
            log_entry(log_path, {
                "stage": "validate", "confirmed": confirmed,
                "lines": n, "title": val_title, "reason": val_reason,
                "duration_sec": validate_sec,
            }, agent_name)

            if not confirmed:
                append_summary(session_id, f"DISMISSED: {val_title or reason[:100]}")
                continue

            try:
                text = INJECTION_TEMPLATE.format(title=val_title, reason=val_reason)
                if not wait_for_idle(tmux_session, timeout_sec=120):
                    log_entry(log_path, {"event": "inject_skipped", "reason": "agent not idle"}, agent_name)
                inject_tmux(tmux_session, text)
                play_alert(config)
                log_entry(log_path, {"event": "injected", "title": val_title, "reason": val_reason}, agent_name)
            except subprocess.CalledProcessError as e:
                log_entry(log_path, {"error": f"inject failed: {e}"}, agent_name)

        except Exception as e:
            log_entry(log_path, {"error": str(e)}, agent_name)
