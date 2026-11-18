"""Where WorldBaton's own files are: bundled ones (inside the exe) and per-user data."""
import os
import sys
from pathlib import Path

# PyInstaller unpacks bundled files under sys._MEIPASS; in a source checkout they live in the repo.
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
ICON = BUNDLE / "assets" / "worldbaton.png"
APP_DIR = Path(os.environ.get("WORLDBATON_HOME") or Path(os.environ.get("APPDATA", Path.home())) / "WorldBaton")
