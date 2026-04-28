"""Entry point: pathos command."""
import argparse
import os
import shlex
import shutil
import subprocess
import sys

from . import __version__
from .config import LOGS_DIR, SUPERVISED_DIR, ensure_dirs, load_config
from .session import find_jsonl
from .supervisor import log_entry, poll_loop
from .updater import check_and_update


def tmux_session_exists(name: str) -> bool:
    return subprocess.run(
        ["tmux", "has-session", "-t", name],
        capture_output=True,
    ).returncode == 0


def pick_session_name(base: str) -> str:
    """Auto-increment session name if base is taken: pathos-agent, pathos-agent-2, ..."""
    if not tmux_session_exists(base):
        return base
    n = 2
    while tmux_session_exists(f"{base}-{n}"):
        n += 1
    return f"{base}-{n}"


def main():
    if not os.getenv("_PATHOS_UPDATING"):
        if check_and_update():
            os.environ["_PATHOS_UPDATING"] = "1"
            pathos_bin = shutil.which("pathos")
            if pathos_bin:
                os.execv(pathos_bin, ["pathos"] + sys.argv[1:])

    config = load_config()
    parser = argparse.ArgumentParser(
        prog="pathos",
        description="External supervisor for Claude Code agents",
    )
    parser.add_argument("--session", default="pathos-agent",
                        help="tmux session name (default: pathos-agent)")
    parser.add_argument("--poll", type=int, default=None,
                        help=f"Poll interval in seconds (default: {config['poll_interval']})")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args, claude_args = parser.parse_known_args()

    debug = os.getenv("PATHOS_DEBUG") == "1"
    poll_sec = args.poll or (config["debug_poll_interval"] if debug else config["poll_interval"])

    ensure_dirs()

    for marker in SUPERVISED_DIR.iterdir():
        if not tmux_session_exists(marker.name):
            marker.unlink(missing_ok=True)

    session = pick_session_name(args.session)

    claude_cmd = ["claude"] + claude_args
    if debug:
        subprocess.run(["tmux", "new-session", "-d", "-s", session], check=True)
        subprocess.run(["tmux", "send-keys", "-t", session,
                        shlex.join(claude_cmd), "Enter"], check=True)
    else:
        subprocess.run(["tmux", "new-session", "-d", "-s", session] + claude_cmd,
                       check=True)

    (SUPERVISED_DIR / session).touch()

    jsonl = find_jsonl(session)
    if not jsonl:
        (SUPERVISED_DIR / session).unlink(missing_ok=True)
        print("Could not find agent JSONL — is claude running?", file=sys.stderr)
        sys.exit(1)

    log_path = LOGS_DIR / f"{jsonl.stem}.supervisor.log"
    session_link = LOGS_DIR / f"{session}.log"
    session_link.unlink(missing_ok=True)
    session_link.symlink_to(log_path)
    log_entry(log_path, {"event": "started", "session": session, "jsonl": str(jsonl)}, session)

    pid = os.fork()
    if pid == 0:
        sys.stdin.close()
        try:
            poll_loop(session, jsonl, log_path, poll_sec)
        except Exception:
            pass
        sys.exit(0)
    else:
        subprocess.run(["tmux", "attach-session", "-t", session])
        (SUPERVISED_DIR / session).unlink(missing_ok=True)
        print(f"\nTo resume this supervised session:\n  pathos --resume {jsonl.stem}\n")


if __name__ == "__main__":
    main()
