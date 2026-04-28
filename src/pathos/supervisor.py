"""Two-stage supervisor: Haiku triage → Sonnet validation → tmux injection."""
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import GLOBAL_LOG, load_config, load_prompt
from .context import append_summary, extract_transcript, get_context, init_summary, maybe_compress
from .session import find_jsonl, inject_tmux, session_alive, setup_tmux_keys, wait_for_idle

INJECTION_TEMPLATE = (
    "Hey, I was just reviewing your latest changes and saw this issue:\n"
    "\n"
    "{reason}\n"
    "\n"
    "Can you please stop your current process, carefully review what you did, "
    "figure out if you have a way to address and fix this issue reliably then "
    "do it and continue your work. If not stop and let's align on this."
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


def parse_validate_output(stdout: str) -> tuple[bool, str]:
    """Parse VERDICT/REASON format from validator output."""
    confirmed = False
    reason = ""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("VERDICT:"):
            verdict = line.split(":", 1)[1].strip()
            confirmed = verdict.upper().startswith("CRITICAL")
        elif line.startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
    return confirmed, reason


def validate(jsonl: Path, since: int, lines: int, session_id: str,
             triage_summary: str, triage_reason: str, config: dict) -> tuple[bool, str]:
    """Stage 2: deep validation. Returns (confirmed, reason)."""
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
        return False, f"validate error: {err}"

    return parse_validate_output(stdout)


def play_alert():
    """Play alert sound on macOS, silent no-op elsewhere."""
    sound = "/System/Library/Sounds/Sosumi.aiff"
    if Path(sound).exists():
        subprocess.Popen(["afplay", sound])


def poll_loop(tmux_session: str, jsonl: Path, log_path: Path, poll_sec: int):
    config = load_config()
    setup_tmux_keys(tmux_session)
    since = sum(1 for _ in open(jsonl)) if jsonl.exists() else 0
    last_critical_at = -1
    session_id = jsonl.stem
    agent_name = tmux_session

    init_summary(session_id, jsonl)

    while True:
        time.sleep(poll_sec)
        if not session_alive(tmux_session):
            log_entry(log_path, {"event": "stopped", "reason": "tmux session gone"}, agent_name)
            return
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

            if not jsonl.exists():
                continue
            n = sum(1 for _ in open(jsonl))
            if n <= since:
                continue

            summary, flagged, reason = triage(jsonl, since, session_id, config)
            log_entry(log_path, {
                "stage": "triage", "flagged": flagged,
                "lines": n, "summary": summary, "reason": reason,
            }, agent_name)

            if summary:
                append_summary(session_id, summary)
                maybe_compress(session_id)

            if not flagged:
                since = n
                continue

            confirmed, val_reason = validate(
                jsonl, since, n, session_id, summary, reason, config,
            )
            since = n
            log_entry(log_path, {
                "stage": "validate", "confirmed": confirmed,
                "lines": n, "reason": val_reason,
            }, agent_name)

            if not confirmed:
                short_reason = reason[:100] if len(reason) > 100 else reason
                append_summary(session_id, f"DISMISSED: {short_reason}")
                continue

            last_critical_at = n

            try:
                text = INJECTION_TEMPLATE.format(reason=val_reason)
                if not wait_for_idle(tmux_session, timeout_sec=120):
                    log_entry(log_path, {"event": "inject_skipped", "reason": "agent not idle"}, agent_name)
                inject_tmux(tmux_session, text)
                play_alert()
                log_entry(log_path, {"event": "injected", "reason": val_reason}, agent_name)
            except subprocess.CalledProcessError as e:
                log_entry(log_path, {"error": f"inject failed: {e}"}, agent_name)

        except Exception as e:
            log_entry(log_path, {"error": str(e)}, agent_name)
