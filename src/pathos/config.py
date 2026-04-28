"""Configuration loading and standard paths."""
import json
from importlib import resources
from pathlib import Path

PATHOS_DIR = Path.home() / ".pathos"
CONFIG_PATH = PATHOS_DIR / "config.json"
PROMPTS_DIR = PATHOS_DIR / "prompts"
LOGS_DIR = PATHOS_DIR / "logs"
GLOBAL_LOG = LOGS_DIR / "all.log"
SUPERVISED_DIR = PATHOS_DIR / "supervised"

DEFAULTS = {
    "triage_model": "claude-haiku-4-5-20251001",
    "validate_model": "claude-opus-4-7",
    "poll_interval": 60,
    "debug_poll_interval": 5,
}


def ensure_dirs():
    for d in (PATHOS_DIR, LOGS_DIR, SUPERVISED_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    config = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text())
            config.update(user)
        except (json.JSONDecodeError, OSError):
            pass
    return config


def load_prompt(name: str) -> str:
    """Load a prompt file. User overrides in ~/.pathos/prompts/ take priority."""
    user_path = PROMPTS_DIR / name
    if user_path.exists():
        return user_path.read_text()
    return resources.files("pathos").joinpath("prompts", name).read_text()
