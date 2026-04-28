"""Auto-update from GitHub releases on every startup."""
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from . import __version__
from .config import PATHOS_DIR

REPO = "stefanopochet/pathos"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"


def _gh_token() -> str | None:
    """Get GitHub token from gh CLI (5000 req/hr vs 60 unauthenticated)."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _api_headers() -> dict:
    headers = {"Accept": "application/vnd.github.v3+json"}
    token = _gh_token()
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def check_and_update() -> bool:
    """Check GitHub for a newer release and install it. Returns True if updated."""
    if (PATHOS_DIR / "src").is_symlink():
        return False

    try:
        headers = _api_headers()
        req = Request(API_URL, headers=headers)
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        remote_version = data["tag_name"].lstrip("v")
        remote_parts = tuple(int(x) for x in remote_version.split("."))
        local_parts = tuple(int(x) for x in __version__.split("."))
        if remote_parts <= local_parts:
            return False

        print(f"Updating pathos to v{remote_version}, wait a sec...")

        tarball_url = data["tarball_url"]
        tmpdir = tempfile.mkdtemp()
        try:
            tarball_req = Request(tarball_url, headers=headers)
            with urlopen(tarball_req, timeout=30) as resp:
                tarball_path = os.path.join(tmpdir, "release.tar.gz")
                with open(tarball_path, "wb") as f:
                    f.write(resp.read())

            with tarfile.open(tarball_path) as tar:
                tar.extractall(tmpdir)

            dirs = [e for e in os.listdir(tmpdir) if os.path.isdir(os.path.join(tmpdir, e))]
            if not dirs:
                return False

            src = os.path.join(tmpdir, dirs[0], "src", "pathos")
            dst = str(PATHOS_DIR / "src" / "pathos")

            if not os.path.isdir(src):
                return False

            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
            print(f"Updated to v{remote_version}. Restarting...\n")
            return True
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    except (URLError, OSError, KeyError, json.JSONDecodeError, tarfile.TarError):
        return False
