"""Runs a private Syncthing instance and talks to its REST API."""
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def bundled_exe() -> Path:
    # PyInstaller unpacks data next to sys._MEIPASS; in a source checkout it lives in vendor/.
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "vendor" / ("syncthing.exe" if os.name == "nt" else "syncthing")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Syncthing:
    def __init__(self, home: Path, exe: Path | None = None):
        self.home = Path(home)
        self.exe = exe or bundled_exe()
        self.api_key = secrets.token_urlsafe(24)
        self.port = free_port()
        self.proc: subprocess.Popen | None = None

    # --- process -----------------------------------------------------------------
    def start(self, timeout: float = 60) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, STNODEFAULTFOLDER="1", STNOUPGRADE="1")
        self.proc = subprocess.Popen(
            [str(self.exe), "serve", f"--home={self.home}", "--no-browser", "--no-restart",
             f"--gui-address=127.0.0.1:{self.port}", f"--gui-apikey={self.api_key}",
             f"--log-file={self.home / 'syncthing.log'}"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"Syncthing exited early; see {self.home / 'syncthing.log'}")
            try:
                self.my_id = self.get("/rest/system/status")["myID"]
                return
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.3)
        raise RuntimeError("Syncthing did not start in time")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.post("/rest/system/shutdown")
                self.proc.wait(timeout=15)
            except Exception:
                self.proc.kill()

    # --- http --------------------------------------------------------------------
    def _req(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                     headers={"X-API-Key": self.api_key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
        return json.loads(raw) if raw.strip() else None

    def get(self, path):
        return self._req("GET", path)

    def post(self, path, body=None):
        return self._req("POST", path, body)

    def patch(self, path, body):
        return self._req("PATCH", path, body)

    def put(self, path, body):
        return self._req("PUT", path, body)

    # --- config ------------------------------------------------------------------
    def set_name(self, name: str) -> None:
        self.patch(f"/rest/config/devices/{self.my_id}", {"name": name})

    def add_device(self, device_id: str, name: str, introducer=False, addresses=None) -> None:
        dev = self.get("/rest/config/defaults/device")
        dev.update(deviceID=device_id, name=name, introducer=introducer, addresses=addresses or ["dynamic"])
        self.put(f"/rest/config/devices/{device_id}", dev)

    def has_folder(self, folder_id: str) -> bool:
        return any(f["id"] == folder_id for f in self.get("/rest/config/folders"))

    def add_folder(self, folder_id, label, path, device_ids, ftype="sendreceive", versioning=False) -> None:
        f = self.get("/rest/config/defaults/folder")
        f.update(id=folder_id, label=label, path=str(path), type=ftype,
                 devices=[{"deviceID": d} for d in {self.my_id, *device_ids}])
        if versioning:
            f["versioning"] = {**f.get("versioning", {}), "type": "simple",
                               "params": {"keep": "3", "cleanoutDays": "30"}}
        self.put(f"/rest/config/folders/{folder_id}", f)

    def share_folder(self, folder_id: str, device_id: str) -> None:
        f = self.get(f"/rest/config/folders/{folder_id}")
        if all(d["deviceID"] != device_id for d in f["devices"]):
            f["devices"].append({"deviceID": device_id})
            self.put(f"/rest/config/folders/{folder_id}", f)

    def folder(self, folder_id: str) -> dict:
        return self.get(f"/rest/config/folders/{folder_id}")

    def set_folder(self, folder_id: str, **fields) -> None:
        self.patch(f"/rest/config/folders/{folder_id}", fields)

    def set_ignores(self, folder_id: str, patterns: list[str]) -> None:
        self.post(f"/rest/db/ignores?folder={folder_id}", {"ignore": patterns})

    # --- status ------------------------------------------------------------------
    def folder_status(self, folder_id: str) -> dict:
        return self.get(f"/rest/db/status?folder={folder_id}")

    def completion(self, folder_id: str, device_id: str) -> float:
        return self.get(f"/rest/db/completion?folder={folder_id}&device={device_id}")["completion"]

    def devices(self) -> dict[str, str]:
        return {d["deviceID"]: d["name"] for d in self.get("/rest/config/devices")}

    def connected(self) -> set[str]:
        conns = self.get("/rest/system/connections")["connections"]
        return {d for d, c in conns.items() if c.get("connected")}

    def scan(self, folder_id: str) -> None:
        self.post(f"/rest/db/scan?folder={folder_id}")

    def revert(self, folder_id: str) -> None:
        self.post(f"/rest/db/revert?folder={folder_id}")
