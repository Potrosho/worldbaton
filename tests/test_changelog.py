import tempfile
import unittest
from pathlib import Path

from worldbaton import __version__, changelog

SAMPLE = """# Changelog
Intro text.

## 0.4.0
- New logo.
- A long change that
  wraps onto a second line.

## v0.3.1
- Trust warning.
"""


class Changelog(unittest.TestCase):
    def test_read_between_and_notes(self):
        sections = changelog.read(SAMPLE)
        self.assertEqual(sections, [("0.4.0", ["New logo.", "A long change that wraps onto a second line."]),
                                    ("0.3.1", ["Trust warning."])])
        self.assertEqual([v for v, _ in changelog.between(sections, "0.3.1", "0.4.0")], ["0.4.0"])
        self.assertEqual([v for v, _ in changelog.between(sections, "0", "0.4.0")], ["0.4.0", "0.3.1"])
        self.assertEqual(changelog.notes(sections, "v0.3.1"), "- Trust warning.")
        self.assertEqual(changelog.notes(sections, "v9.9.9"), "")

    def test_this_version_is_in_the_changelog(self):
        # The release build refuses a version without notes; catch it before tagging.
        self.assertTrue(changelog.notes(changelog.load(), __version__), f"CHANGELOG.md needs a ## {__version__} section")

    def test_release_notes_command(self):
        import subprocess
        import sys
        out = Path(tempfile.mkdtemp()) / "notes.md"
        ok = subprocess.run([sys.executable, "-m", "worldbaton.changelog", f"v{__version__}", str(out)])
        self.assertEqual(ok.returncode, 0)
        self.assertTrue(out.read_text(encoding="utf-8").startswith("- "))
        missing = subprocess.run([sys.executable, "-m", "worldbaton.changelog", "v9.9.9", str(out)], capture_output=True)
        self.assertNotEqual(missing.returncode, 0)


if __name__ == "__main__":
    unittest.main()
