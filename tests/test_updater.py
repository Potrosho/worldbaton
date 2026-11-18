import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worldbaton import updater


class Updater(unittest.TestCase):
    def test_parse_and_compare(self):
        self.assertGreater(updater.parse("v0.10.0"), updater.parse("0.9.3"))
        self.assertEqual(updater.parse("garbage"), ())

    def test_latest_picks_newer_release_only(self):
        rel = {"tag_name": "v9.0.0", "assets": [{"name": "WorldBaton.exe", "browser_download_url": "https://x/exe"}]}
        with mock.patch("worldbaton.updater.urllib.request.urlopen") as op, mock.patch("json.load", return_value=rel):
            op.return_value.__enter__.return_value = None
            self.assertEqual(updater.latest(), ("v9.0.0", "https://x/exe"))
            rel["tag_name"] = "v0.0.1"
            self.assertIsNone(updater.latest())

    def test_install_swaps_and_keeps_old(self):
        exe = Path(tempfile.mkdtemp()) / "WorldBaton.exe"
        exe.write_bytes(b"MZ old")
        updater.install(exe, b"MZ new")
        self.assertEqual(exe.read_bytes(), b"MZ new")
        self.assertEqual(exe.with_name("WorldBaton.exe.old").read_bytes(), b"MZ old")
        with self.assertRaises(ValueError):
            updater.install(exe, b"<html>rate limited</html>")
        self.assertEqual(exe.read_bytes(), b"MZ new", "a bad download must not replace the app")


if __name__ == "__main__":
    unittest.main()
