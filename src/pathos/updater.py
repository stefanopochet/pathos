"""Auto-update from GitHub releases on every startup."""
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from . import __version__
from .config import PATHOS_DIR

REPO = "stefanopochet/pathos"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"


def check_and_update() -> bool:
    """Check GitHub for a newer release and install it. Returns True if updated."""
    if (PATHOS_DIR / "src").is_symlink():
        return False

    try:
        req = Request(API_URL, headers={"Accept": "application/vnd.github.v3+json"})
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        remote_version = data["tag_name"].lstrip("v")
        if remote_version == __version__:
            return False

        print(f"Updating pathos to v{remote_version}, wait a sec...")

        tarball_url = data["tarball_url"]
        tmpdir = tempfile.mkdtemp()
        try:
            with urlopen(tarball_url, timeout=30) as resp:
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
