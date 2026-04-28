"""Session context: rolling summary for triage and validation."""
import json
import os
import subprocess
from pathlib import Path

from .config import PATHOS_DIR, load_config

STATE_DIR = PATHOS_DIR / "state"
COMPRESS_THRESHOLD = 3000
COMPRESS_TARGET = 1000


def state_path(session_id: str) -> Path:
    return STATE_DIR / f"{session_id}.summary.txt"


def ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def extract_first_user_message(jsonl: Path) -> str:
    """Parse JSONL to find the first human message."""
    try:
        with open(jsonl) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "human":
                    msg = entry.get("message", {})
                    if isinstance(msg, dict):
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    return block["text"][:500]
                        elif isinstance(content, str):
                            return content[:500]
    except Exception:
        pass
    return ""


def extract_last_user_message(jsonl: Path) -> str:
    """Parse JSONL tail to find the last human message."""
    try:
        with open(jsonl, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            offset = max(0, size - 65536)
            f.seek(offset)
            data = f.read()
        for line in reversed(data.split(b"\n")):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == "human":
                msg = entry.get("message", {})
                if isinstance(msg, dict):
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                return block["text"][:500]
                    elif isinstance(content, str):
                        return content[:500]
    except Exception:
        pass
    return ""


def init_summary(session_id: str, jsonl: Path):
    """Initialize summary file with the first user message."""
    ensure_state_dir()
    path = state_path(session_id)
    if path.exists():
        return
    first_msg = extract_first_user_message(jsonl)
    if first_msg:
        path.write_text(f"GOAL: {first_msg}\n")


def append_summary(session_id: str, summary: str):
    """Append a triage summary line."""
    ensure_state_dir()
    path = state_path(session_id)
    with open(path, "a") as f:
        f.write(summary.strip() + "\n")


def read_summary(session_id: str) -> str:
    """Read the current summary."""
    path = state_path(session_id)
    if not path.exists():
        return ""
    return path.read_text()


def maybe_compress(session_id: str):
    """If summary exceeds threshold, compress with Haiku."""
    path = state_path(session_id)
    if not path.exists():
        return
    content = path.read_text()
    line_count = content.count("\n")
    if line_count < COMPRESS_THRESHOLD:
        return

    config = load_config()
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    prompt = (
        f"Compress the following session summary to approximately {COMPRESS_TARGET} lines. "
        "Keep the GOAL line as-is. Preserve key decisions, errors, and state changes. "
        "Drop routine Ok summaries. Output only the compressed summary, no preamble.\n\n"
        + content
    )
    try:
        proc = subprocess.run(
            ["claude", "-p",
             "--model", config["triage_model"],
             "--no-session-persistence",
             prompt],
            capture_output=True, text=True, timeout=120, env=env,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            path.write_text(proc.stdout.strip() + "\n")
    except Exception:
        pass


def _content_text(content) -> str:
    """Extract text from a message content field (string or block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
    return ""


# Disabled: truncation causes false positives — triage/validate models can't tell
# that tool calls were executed when results are stripped. Until we find a way to
# communicate the truncation reliably, we let the byte cap do all the trimming.
# RESULT_TRUNCATE = 300

def extract_transcript(jsonl: Path, since: int, max_bytes: int = 80000) -> str:
    """Extract transcript from JSONL — full content, trimmed only by byte cap."""
    parts = []
    try:
        with open(jsonl) as f:
            for i, raw in enumerate(f, 1):
                if i <= since:
                    continue
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                etype = entry.get("type", "")

                if etype == "result" or (etype == "" and "message" not in entry):
                    content = entry.get("content", "")
                    text = _content_text(content) if isinstance(content, list) else str(content)
                    if text:
                        parts.append(f"[L{i}] RESULT: {text}")
                    continue

                msg = entry.get("message", {})
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role", etype)
                content = msg.get("content", "")

                if role == "user" or etype == "human":
                    text = _content_text(content)
                    if text:
                        parts.append(f"[L{i}] USER: {text}")

                elif role == "assistant" or etype == "assistant":
                    if isinstance(content, str) and content:
                        parts.append(f"[L{i}] AGENT: {content}")
                    elif isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            btype = block.get("type", "")
                            if btype == "text" and block.get("text", ""):
                                parts.append(f"[L{i}] AGENT: {block['text']}")
                            elif btype == "tool_use":
                                name = block.get("name", "?")
                                inp = str(block.get("input", ""))
                                parts.append(f"[L{i}] TOOL_CALL: {name} — {inp}")
                            elif btype == "tool_result":
                                text = _content_text(block.get("content", ""))
                                if text:
                                    parts.append(f"[L{i}] RESULT: {text}")
    except Exception:
        pass

    transcript = "\n".join(parts)
    if len(transcript.encode()) > max_bytes:
        total = 0
        cutoff = len(parts)
        for j in range(len(parts) - 1, -1, -1):
            total += len(parts[j].encode()) + 1
            if total > max_bytes:
                cutoff = j + 1
                break
        transcript = "(earlier lines omitted)\n" + "\n".join(parts[cutoff:])

    return transcript if transcript else "(no new content)"


def get_context(session_id: str, jsonl: Path) -> str:
    """Build the full context string for triage/validation prompts."""
    summary = read_summary(session_id)
    last_msg = extract_last_user_message(jsonl)

    parts = []
    if summary:
        parts.append(summary.strip())
    if last_msg:
        parts.append(f"LATEST USER MESSAGE: {last_msg}")

    return "\n".join(parts) if parts else "No prior context available."
