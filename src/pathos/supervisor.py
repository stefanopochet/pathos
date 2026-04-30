"""Two-stage supervisor: Haiku triage → Sonnet validation → tmux injection."""
import json
import os
import select
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import GLOBAL_LOG, load_config, load_prompt
from .context import append_summary, extract_transcript, get_context, init_summary, maybe_compress
from .session import find_jsonl, inject_tmux, session_alive, setup_tmux_keys, wait_for_idle

INJECTION_TEMPLATE = (
    "[PATHOS] I spotted an issue while supervising your work.\n\n"
    "— {title}\n\n"
    "— {reason}\n\n"
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


def run_claude(model: str, prompt: str, resume_id: str | None = None,
               session_id: str | None = None) -> tuple[str | None, str | None]:
    """Run claude -p. With resume_id, continues an existing conversation.
    With session_id, starts a new conversation with that ID.
    Returns (stdout, error)."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    proj = _projects_dir()
    before = set(proj.glob("*.jsonl")) if proj.exists() else set()

    cmd = ["claude", "-p", "--model", model]
    if resume_id:
        cmd.extend(["--resume", resume_id])
    elif session_id:
        cmd.extend(["--session-id", session_id])
    else:
        cmd.append("--no-session-persistence")
    cmd.append(prompt)

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, env=env,
        )
    except subprocess.TimeoutExpired:
        return None, "timed out after 300s"
    finally:
        if not resume_id and not session_id and proj.exists():
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


# --- One-shot mode (persistent_sessions: false) ---

def triage_oneshot(jsonl: Path, since: int, session_id: str,
                   config: dict) -> tuple[str, bool, str]:
    """Stage 1: fast scan with fresh session. Returns (summary, flagged, reason)."""
    context = get_context(session_id, jsonl)
    transcript = extract_transcript(jsonl, since)
    prompt = load_prompt("triage.txt").format(
        jsonl=jsonl, since=since, context=context, transcript=transcript,
    )
    stdout, err = run_claude(config["triage_model"], prompt)
    if err:
        return "", False, f"triage error: {err}"
    return parse_triage_output(stdout)


def validate_oneshot(jsonl: Path, since: int, lines: int, session_id: str,
                     triage_summary: str, triage_reason: str,
                     config: dict) -> tuple[bool, str, str]:
    """Stage 2: deep validation with fresh session. Returns (confirmed, title, reason)."""
    context = get_context(session_id, jsonl)
    transcript = extract_transcript(jsonl, since)
    prompt = load_prompt("validate.txt").format(
        jsonl=jsonl, since=since, lines=lines, session_id=session_id,
        triage_summary=triage_summary, triage_reason=triage_reason,
        context=context, transcript=transcript,
    )
    stdout, err = run_claude(config["validate_model"], prompt)
    if err:
        return False, "", f"validate error: {err}"
    return parse_validate_output(stdout)


# --- Persistent mode (persistent_sessions: true) ---
# TODO: session rotation — persistent sessions accumulate context indefinitely.
# Add rotation after N cycles or N lines to prevent context exhaustion.

class PersistentSession:
    """Manages a persistent claude -p conversation for triage or validation."""

    def __init__(self, role: str, model_key: str, init_prompt: str, delta_prompt: str,
                 warmup_prompt: str | None = None):
        self.role = role
        self.model_key = model_key
        self.init_prompt = init_prompt
        self.delta_prompt = delta_prompt
        self.warmup_prompt = warmup_prompt
        self.claude_session_id: str | None = None

    def _new_session_id(self) -> str:
        sid = str(uuid.uuid4())
        self.claude_session_id = sid
        return sid

    def reset(self):
        self.claude_session_id = None

    @property
    def is_warm(self) -> bool:
        return self.claude_session_id is not None

    def warmup(self, config: dict, prompt_vars: dict) -> str | None:
        """Pre-warm the session with context. Returns error or None on success."""
        if self.is_warm or not self.warmup_prompt:
            return None
        model = config[self.model_key]
        sid = self._new_session_id()
        prompt = load_prompt(self.warmup_prompt).format(**prompt_vars)
        _, err = run_claude(model, prompt, session_id=sid)
        if err:
            self.reset()
            return err
        return None

    def call(self, config: dict, prompt_vars: dict) -> tuple[str | None, str | None]:
        model = config[self.model_key]

        if self.claude_session_id is None:
            sid = self._new_session_id()
            prompt = load_prompt(self.init_prompt).format(**prompt_vars)
            stdout, err = run_claude(model, prompt, session_id=sid)
            if err:
                self.reset()
                return None, err
            return stdout, None

        prompt = load_prompt(self.delta_prompt).format(**prompt_vars)
        stdout, err = run_claude(model, prompt, resume_id=self.claude_session_id)
        if err:
            sid = self._new_session_id()
            prompt = load_prompt(self.init_prompt).format(**prompt_vars)
            stdout, err = run_claude(model, prompt, session_id=sid)
            if err:
                self.reset()
                return None, f"fallback init also failed: {err}"
            return stdout, None
        return stdout, None


def triage_persistent(jsonl: Path, since: int, session_id: str, config: dict,
                      ps: PersistentSession) -> tuple[str, bool, str]:
    context = get_context(session_id, jsonl)
    transcript = extract_transcript(jsonl, since)
    prompt_vars = dict(jsonl=jsonl, since=since, context=context, transcript=transcript)
    stdout, err = ps.call(config, prompt_vars)
    if err:
        return "", False, f"triage error: {err}"
    return parse_triage_output(stdout)


def validate_persistent(jsonl: Path, since: int, lines: int, session_id: str,
                        triage_summary: str, triage_reason: str, config: dict,
                        ps: PersistentSession) -> tuple[bool, str, str]:
    context = get_context(session_id, jsonl)
    transcript = extract_transcript(jsonl, since)
    prompt_vars = dict(
        jsonl=jsonl, since=since, lines=lines, session_id=session_id,
        triage_summary=triage_summary, triage_reason=triage_reason,
        context=context, transcript=transcript,
    )
    stdout, err = ps.call(config, prompt_vars)
    if err:
        return False, "", f"validate error: {err}"
    return parse_validate_output(stdout)


# --- Shared ---

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
    persistent = config.get("persistent_sessions", False)

    triage_ps = None
    validate_ps = None
    if persistent:
        triage_ps = PersistentSession(
            "triage", "triage_model", "triage_init.txt", "triage_delta.txt",
        )
        validate_ps = PersistentSession(
            "validate", "validate_model", "validate_init.txt", "validate_delta.txt",
            warmup_prompt="validate_warmup.txt",
        )

    init_summary(session_id, jsonl)
    mode = "kqueue+persistent" if persistent else "kqueue"
    log_entry(log_path, {"event": "watching", "mode": mode, "heartbeat_sec": poll_sec}, agent_name)

    cycle = 0
    while True:
        if not session_alive(tmux_session):
            log_entry(log_path, {"event": "stopped", "reason": "tmux session gone"}, agent_name)
            return

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
                if triage_ps:
                    triage_ps.reset()
                if validate_ps:
                    validate_ps.reset()

            n = sum(1 for _ in open(jsonl))
            if n <= since:
                log_entry(log_path, {"event": "wake_no_new_lines", "lines": n, "since": since}, agent_name)
                continue

            log_entry(log_path, {"event": "wake", "cycle": cycle, "lines": n, "since": since}, agent_name)

            t0 = time.monotonic()
            if persistent:
                summary, flagged, reason = triage_persistent(
                    jsonl, since, session_id, config, triage_ps,
                )
            else:
                summary, flagged, reason = triage_oneshot(
                    jsonl, since, session_id, config,
                )
            triage_sec = round(time.monotonic() - t0, 1)
            log_entry(log_path, {
                "stage": "triage", "flagged": flagged,
                "lines": n, "summary": summary, "reason": reason,
                "duration_sec": triage_sec,
                "persistent": persistent,
            }, agent_name)

            if summary:
                append_summary(session_id, summary)
                maybe_compress(session_id)

            if not flagged:
                since = n
                if validate_ps and not validate_ps.is_warm:
                    context = get_context(session_id, jsonl)
                    transcript = extract_transcript(jsonl, 0)
                    t0_w = time.monotonic()
                    warmup_err = validate_ps.warmup(config, dict(
                        context=context, transcript=transcript,
                        since=0, jsonl=jsonl,
                    ))
                    warmup_sec = round(time.monotonic() - t0_w, 1)
                    log_entry(log_path, {
                        "event": "validate_warmup",
                        "duration_sec": warmup_sec,
                        "error": warmup_err or "",
                    }, agent_name)
                continue

            t0 = time.monotonic()
            if persistent:
                confirmed, val_title, val_reason = validate_persistent(
                    jsonl, since, n, session_id, summary, reason, config, validate_ps,
                )
            else:
                confirmed, val_title, val_reason = validate_oneshot(
                    jsonl, since, n, session_id, summary, reason, config,
                )
            validate_sec = round(time.monotonic() - t0, 1)
            since = n
            log_entry(log_path, {
                "stage": "validate", "confirmed": confirmed,
                "lines": n, "title": val_title, "reason": val_reason,
                "duration_sec": validate_sec,
                "persistent": persistent,
            }, agent_name)

            if not confirmed:
                append_summary(session_id, f"DISMISSED: {val_title or reason[:100]}")
                continue

            try:
                text = INJECTION_TEMPLATE.format(title=val_title, reason=val_reason)
                if not wait_for_idle(tmux_session, timeout_sec=120):
                    log_entry(log_path, {"event": "inject_skipped", "reason": "agent not idle"}, agent_name)
                inject_tmux(tmux_session, text, inject_delay=config.get("inject_delay", 0.1))
                play_alert(config)
                log_entry(log_path, {"event": "injected", "title": val_title, "reason": val_reason}, agent_name)
            except subprocess.CalledProcessError as e:
                log_entry(log_path, {"error": f"inject failed: {e}"}, agent_name)

        except Exception as e:
            log_entry(log_path, {"error": str(e)}, agent_name)
