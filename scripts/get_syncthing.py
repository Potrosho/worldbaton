"""Download the official Syncthing build for Windows into vendor/ and check its SHA-256.

Usage: python scripts/get_syncthing.py
"""
import hashlib
import io
import urllib.request
import zipfile
from pathlib import Path

VERSION = "v2.1.7"  # bump deliberately; the checksum file is fetched for the same version
BASE = f"https://github.com/syncthing/syncthing/releases/download/{VERSION}"
ZIP_NAME = f"syncthing-windows-amd64-{VERSION}.zip"
VENDOR = Path(__file__).resolve().parent.parent / "vendor"


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def main() -> None:
    # shortcut: checks the hash list but not its PGP signature; add gpg verification before release builds in CI.
    sums = fetch(f"{BASE}/sha256sum.txt.asc").decode()
    expected = next(line.split()[0] for line in sums.splitlines() if line.endswith(ZIP_NAME))
    data = fetch(f"{BASE}/{ZIP_NAME}")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise SystemExit(f"Checksum mismatch for {ZIP_NAME}: {actual} != {expected}")

    VENDOR.mkdir(exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.endswith("/syncthing.exe") or name.endswith("/LICENSE.txt"):
                (VENDOR / Path(name).name.replace("LICENSE.txt", "SYNCTHING-LICENSE.txt")).write_bytes(z.read(name))
    print(f"Syncthing {VERSION} verified and saved to {VENDOR}")


if __name__ == "__main__":
    main()
