"""Configuration loading and standard paths."""
import json
from importlib import resources
from pathlib import Path

PATHOS_DIR = Path.home() / ".pathos"
CONFIG_YML = PATHOS_DIR / "config.yml"
CONFIG_JSON = PATHOS_DIR / "config.json"
PROMPTS_DIR = PATHOS_DIR / "prompts"
LOGS_DIR = PATHOS_DIR / "logs"
GLOBAL_LOG = LOGS_DIR / "all.log"
SUPERVISED_DIR = PATHOS_DIR / "supervised"

DEFAULTS = {
    "triage_model": "claude-haiku-4-5-20251001",
    "validate_model": "claude-opus-4-7",
    "poll_interval": 60,
    "debug_poll_interval": 5,
    "alert_command": "afplay /System/Library/Sounds/Sosumi.aiff",
}


def _parse_yaml(text: str) -> dict:
    """Parse flat YAML: key-value pairs with comments. No external deps needed."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if "  #" in value:
            value = value[: value.index("  #")].strip()
        if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
            result[key] = value[1:-1]
        elif value.lstrip("-").isdigit():
            result[key] = int(value)
        elif value.lower() in ("true", "yes"):
            result[key] = True
        elif value.lower() in ("false", "no"):
            result[key] = False
        elif value.lower() in ("null", "~") or value == "":
            result[key] = None
        else:
            result[key] = value
    return result


def ensure_dirs():
    for d in (PATHOS_DIR, LOGS_DIR, SUPERVISED_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    config = dict(DEFAULTS)
    if CONFIG_YML.exists():
        try:
            user = _parse_yaml(CONFIG_YML.read_text())
            config.update(user)
        except Exception:
            pass
    elif CONFIG_JSON.exists():
        try:
            user = json.loads(CONFIG_JSON.read_text())
            config.update(user)
        except Exception:
            pass
    return config


def load_prompt(name: str) -> str:
    """Load a prompt file. User overrides in ~/.pathos/prompts/ take priority."""
    user_path = PROMPTS_DIR / name
    if user_path.exists():
        return user_path.read_text()
    return resources.files("pathos").joinpath("prompts", name).read_text()
