"""Self-update from GitHub Releases: download the newer WorldBaton.exe and swap it in."""
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from . import __version__

REPO = "Potrosho/worldbaton"
ASSET = "WorldBaton.exe"
HEADERS = {"User-Agent": "WorldBaton-updater", "Accept": "application/vnd.github+json"}


def parse(tag: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in tag.strip().lstrip("v").split("."))
    except ValueError:
        return ()


def can_update() -> bool:
    return bool(getattr(sys, "frozen", False))  # only the packaged .exe replaces itself


def latest() -> tuple[str, str] | None:
    """(tag, download url) of a release newer than this one, or None."""
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=10) as r:
        rel = json.load(r)
    url = next((a["browser_download_url"] for a in rel.get("assets", []) if a["name"] == ASSET), None)
    if url and parse(rel["tag_name"]) > parse(__version__):
        return rel["tag_name"], url
    return None


def install(exe: Path, data: bytes) -> None:
    """Replace exe with data. Windows can't overwrite a running exe, but it can rename it."""
    if not data.startswith(b"MZ"):
        raise ValueError("The download isn't a Windows program.")
    new, old = exe.with_name(exe.name + ".new"), exe.with_name(exe.name + ".old")
    new.write_bytes(data)
    old.unlink(missing_ok=True)
    os.replace(exe, old)
    try:
        os.replace(new, exe)
    except OSError:
        os.replace(old, exe)  # put the working version back
        raise


def download_and_install(url: str) -> None:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"]}), timeout=300) as r:
        data = r.read()
    install(Path(sys.executable), data)


def restart() -> None:
    # Without this, a one-file PyInstaller child reuses the parent's temp folder, which is deleted when the parent exits.
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    subprocess.Popen([sys.executable], env=env, close_fds=True)


def clean_old() -> None:
    """Remove the previous version left behind by install()."""
    if can_update():
        try:
            Path(sys.executable).with_name(Path(sys.executable).name + ".old").unlink(missing_ok=True)
        except OSError:
            pass  # old process still closing; removed on a later start
