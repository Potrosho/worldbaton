"""Helpers that work on this PC only: your own game's mods, checks before hosting, Discord posts, start with Windows."""
import json
import os
import re
import shutil
import sys
import urllib.request
from pathlib import Path

LOW_DISK_GB = 5
# run.bat / run.sh made by SquidServers (and the NeoForge/Forge installers) start the server through
# libraries/<loader>/<version>/win_args.txt, so that path says which loader version this PC's start script expects.
LOADER_PATH = re.compile(r"libraries[/\\](net[/\\]neoforged[/\\]neoforge|net[/\\]minecraftforge[/\\]forge)[/\\]([^/\\\s]+)[/\\]")
LOADER_NAMES = {"neoforge": "NeoForge", "forge": "Forge"}
DISCORD = re.compile(r"^https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


# --- your own Minecraft's mods ------------------------------------------------------------------------
def sync_game_mods(server_mods: Path, game_mods: Path, managed: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Make the game's mods folder have the server's mods. Only touches files WorldBaton put there before
    (managed) or that the server has, so the player's own client-only mods stay.
    Returns (added or updated, removed, new managed list)."""
    server_mods, game_mods = Path(server_mods), Path(game_mods)
    game_mods.mkdir(parents=True, exist_ok=True)
    server = {p.name: p for p in server_mods.glob("*.jar")}
    added, removed = [], []
    for name, src in sorted(server.items()):
        dest = game_mods / name
        if not dest.exists() or dest.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dest)
            added.append(name)
    for name in sorted(set(managed) - set(server)):
        dest = game_mods / name
        if dest.exists():
            dest.unlink()
            removed.append(name)
    return added, removed, sorted(server)


def default_game_mods() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / ".minecraft" / "mods"


# --- checks before hosting (only what SquidServers can't know: it doesn't know about the group) ---------
def loader_from_start_script(world_path: Path) -> tuple[str, str] | None:
    """(loader path under libraries/, version) that this PC's start script runs, if it says."""
    for name in ("run.bat", "run.sh"):
        try:
            m = LOADER_PATH.search((Path(world_path) / name).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if m:
            return m[1].replace("\\", "/"), m[2]
    return None


def prehost_problems(world_path: Path) -> list[str]:
    world_path, out = Path(world_path), []
    try:
        free = shutil.disk_usage(world_path).free / 1e9
        if free < LOW_DISK_GB:
            out.append(f"Only {free:.1f} GB is free on this drive. If it fills up while the server is saving, "
                       "the world can get damaged. Free up some space first.")
    except OSError:
        pass
    found = loader_from_start_script(world_path)
    if found:
        loader, version = found
        if not (world_path / "libraries" / loader / version).is_dir():
            installed = sorted(p.name for p in (world_path / "libraries" / loader).glob("*") if p.is_dir())
            name = LOADER_NAMES.get(loader.rsplit("/", 1)[-1], loader)
            group = f" The group's server uses {name} {', '.join(installed)}." if installed else ""
            out.append(f"Your SquidServers server is set up to start {name} {version}, which isn't in the group's "
                       f"server folder, so it won't start.{group} Create the server again in SquidServers with "
                       "the group's version, then use Change server folder.")
    return out


# --- Discord ------------------------------------------------------------------------------------------
def valid_webhook(url: str) -> bool:
    return bool(DISCORD.match(url.strip()))


def post_discord(url: str, text: str) -> None:
    """Post a message to a Discord channel webhook. Raises on failure."""
    if not valid_webhook(url):
        raise ValueError("That isn't a Discord webhook link.")
    body = json.dumps({"content": text[:1900], "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(url.strip(), data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "WorldBaton"})
    with urllib.request.urlopen(req, timeout=10):
        pass


# --- start with Windows ---------------------------------------------------------------------------------
def can_autostart() -> bool:
    return os.name == "nt" and bool(getattr(sys, "frozen", False))  # only the installed .exe, not a source checkout


def autostart_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "WorldBaton")
            return True
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    """Start WorldBaton hidden in the tray when you sign in to Windows (only your account)."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, "WorldBaton", 0, winreg.REG_SZ, f'"{sys.executable}" --tray')
        else:
            try:
                winreg.DeleteValue(k, "WorldBaton")
            except FileNotFoundError:
                pass
