"""What changed in each version, read from CHANGELOG.md (bundled into the exe).

python -m worldbaton.changelog v0.4.0 notes.md  writes that version's notes for its GitHub release, and fails if
the changelog has no section for it.
"""
import sys
from pathlib import Path

from .updater import parse

# PyInstaller unpacks bundled files under sys._MEIPASS; in a source checkout it's the repo root.
FILE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "CHANGELOG.md"


def read(text: str) -> list[tuple[str, list[str]]]:
    """[(version, [change, ...]), ...] in file order (newest first). Wrapped lines join the change above."""
    sections = []
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip().lstrip("v"), []))
        elif sections and line.startswith("- "):
            sections[-1][1].append(line[2:].strip())
        elif sections and sections[-1][1] and line.startswith("  ") and line.strip():
            sections[-1][1][-1] += " " + line.strip()
    return sections


def load() -> list[tuple[str, list[str]]]:
    try:
        return read(FILE.read_text(encoding="utf-8"))
    except OSError:
        return []


def between(sections, old: str, new: str) -> list[tuple[str, list[str]]]:
    """Versions after old, up to and including new."""
    return [(v, c) for v, c in sections if parse(old) < parse(v) <= parse(new)]


def notes(sections, version: str) -> str:
    return "\n".join(f"- {c}" for v, changes in sections if parse(v) == parse(version) for c in changes)


if __name__ == "__main__":
    tag, out = sys.argv[1], sys.argv[2]
    text = notes(load(), tag)
    if not text:
        sys.exit(f"CHANGELOG.md has no section for {tag}. Add one before releasing.")
    Path(out).write_text(text + "\n", encoding="utf-8")
