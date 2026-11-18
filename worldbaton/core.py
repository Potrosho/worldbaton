"""WorldBaton rules: who may host, and how each PC's copy of the world is set up.

Two Syncthing folders per group:
  world  - the server folder. Only the last host sends; everyone else is receive-only.
           Paused on the host while hosting, so friends never get half-saved files.
  baton  - one small baton.json everyone can write: who is hosting, their address, the world version.

Optional backup folder (e.g. a shared Google Drive/OneDrive folder) for groups that are never
online at the same time: each finished session is zipped there, and the baton is mirrored there.
"""
import base64
import ctypes
import fnmatch
import json
import os
import secrets
import shutil
import time
import zipfile
from pathlib import Path

STALE_AFTER = 300      # seconds without a heartbeat before others may take over
HEARTBEAT_EVERY = 30
VERSION_FILE = ".worldbaton-version"

# Per-PC or throwaway files. SquidServers keeps its own state/tunnel data in these too,
# and writes start scripts with this PC's Java path and RAM setting.
IGNORES = [
    "(?d)logs", "(?d)crash-reports", "(?d).cache", "(?d).mixin.out",
    "(?d).mc_server_state.json", "(?d)squidservers", "(?d)world/session.lock",
    "(?d)*.sync-conflict-*",
    "(?d)run.bat", "(?d)run.sh", "(?d)run.bat.bak", "(?d)run.sh.bak", "(?d)start.ps1",
    "(?d)user_jvm_args.txt", "(?d)install_java.ps1", "(?d)install_java.sh", "(?d)installer.log",
]
SKIP = [p.removeprefix("(?d)") for p in IGNORES] + [".stfolder", ".stversions", ".stignore"]
KEEP_BACKUPS = 3


def skipped(rel: str) -> bool:
    """Same files Syncthing ignores, for zipping/restoring. rel is a /-separated path in the server folder."""
    first, name = rel.split("/", 1)[0], rel.rsplit("/", 1)[-1]
    return any(rel == p or first == p or fnmatch.fnmatch(name, p) for p in SKIP)


# --- codes ---------------------------------------------------------------------------
def _enc(prefix: str, data: dict) -> str:
    return prefix + base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")


def _dec(prefix: str, code: str) -> dict:
    code = code.strip()
    if not code.startswith(prefix):
        raise ValueError(f"That isn't a WorldBaton {'invite' if prefix == 'WBI1-' else 'join'} code.")
    raw = code[len(prefix):]
    try:
        return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:
        raise ValueError("The code is damaged. Copy it again in full.")


def encode_invite(owner_id, owner_name, group, world_id, baton_id) -> str:
    return _enc("WBI1-", {"o": owner_id, "on": owner_name, "g": group, "w": world_id, "b": baton_id})


def decode_invite(code: str) -> dict:
    d = _dec("WBI1-", code)
    if not all(isinstance(d.get(k), str) and d[k] for k in ("o", "on", "g", "w", "b")):
        raise ValueError("The invite code is missing information.")
    return d


def encode_join(device_id, name) -> str:
    return _enc("WBJ1-", {"d": device_id, "n": name})


def decode_join(code: str) -> dict:
    d = _dec("WBJ1-", code)
    if not (isinstance(d.get("d"), str) and isinstance(d.get("n"), str)):
        raise ValueError("The join code is missing information.")
    return d


def safe_folder_name(name: str) -> str:
    cleaned = "".join(c for c in name if c not in '<>:"/\\|?*').strip().rstrip(".")
    return cleaned or "WorldBaton world"


# --- rules (pure) --------------------------------------------------------------------
def holder_alive(baton: dict | None, now: float) -> bool:
    return bool(baton and baton.get("holder") and now - baton.get("heartbeat", 0) < STALE_AFTER)


def desired_world_mode(baton: dict | None, my_id: str) -> tuple[str, bool]:
    """(folder type, paused) this PC's world folder should be in."""
    if not baton:
        return "receiveonly", False
    if baton.get("holder") == my_id:
        return "sendreceive", True
    if not baton.get("holder") and baton.get("last_host") == my_id:
        return "sendreceive", False
    return "receiveonly", False


def baton_key(b: dict) -> tuple:
    # Later events sort higher: a release bumps version; hosting/takeover keeps it but sets holder + newer heartbeat.
    return (b.get("version", 0), bool(b.get("holder")), b.get("heartbeat", 0))


def newest_baton(*batons) -> dict | None:
    found = [b for b in batons if b]
    return max(found, key=baton_key) if found else None


def host_check(baton, my_id, local_version, need_files, now, backup_version=0) -> tuple[bool, str]:
    """Can this PC start hosting? Returns (allowed, reason shown to the player)."""
    if not baton:
        return False, "Waiting for the group's data from the owner."
    if baton.get("holder") == my_id:
        return False, "You are already hosting."
    if holder_alive(baton, now):
        return False, f"{baton.get('holder_name', 'Someone')} is hosting right now."
    if local_version < baton.get("version", 0) or need_files:
        if backup_version >= baton.get("version", 0):
            return True, "Your copy is out of date, so WorldBaton will load the newest world from the backup folder first."
        return False, f"Your copy is out of date. Waiting for the latest world from {baton.get('last_host_name', 'the last host')}."
    if baton.get("holder"):
        return True, (f"{baton.get('holder_name', 'The host')} stopped responding. "
                      "Taking over loses anything from their session that wasn't saved.")
    return True, "You have the latest world."


# --- server detection ------------------------------------------------------------------
def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def server_running(world_path: Path) -> bool:
    """True if SquidServers reports this server online and its process exists."""
    try:
        state = json.loads((Path(world_path) / ".mc_server_state.json").read_text())
    except (OSError, ValueError):
        return False
    return state.get("status") == "online" and isinstance(state.get("pid"), int) and pid_alive(state["pid"])


def replace_synced_files(src: Path, dest: Path, move: bool) -> None:
    """Make dest's synced files exactly src's. Per-PC files in dest (see SKIP) are left alone."""
    src, dest = Path(src), Path(dest)
    for p in list(dest.rglob("*")):
        if p.is_file() and not skipped(p.relative_to(dest).as_posix()):
            p.unlink()
    for p in list(src.rglob("*")):
        rel = p.relative_to(src)
        if p.is_file() and not skipped(rel.as_posix()):
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            (shutil.move if move else shutil.copy2)(p, dest / rel)


# --- group (uses Syncthing) ------------------------------------------------------------
class Group:
    def __init__(self, st, info: dict, app_dir: Path):
        self.st, self.info = st, info
        self.world_id, self.baton_id = info["world_id"], info["baton_id"]
        self.world_path = Path(info["world_path"])
        self.app_dir = Path(app_dir)
        self.baton_file = self.app_dir / f"baton-{self.baton_id}" / "baton.json"

    @property
    def backup_dir(self) -> Path | None:
        return Path(self.info["backup_dir"]) if self.info.get("backup_dir") else None

    def set_backup_dir(self, path) -> None:
        self.info["backup_dir"] = str(path) if path else None
        baton = self.read_baton()
        if baton:
            self.write_baton(baton)  # so the backup folder knows the current state right away

    # setup
    @classmethod
    def create(cls, st, app_dir, group_name, world_path, player):
        info = {"name": group_name, "world_id": "wb-world-" + secrets.token_hex(6),
                "baton_id": "wb-baton-" + secrets.token_hex(6), "world_path": str(world_path), "owner": st.my_id}
        g = cls(st, info, app_dir)
        g.baton_file.parent.mkdir(parents=True, exist_ok=True)
        st.add_folder(g.world_id, group_name, world_path, [], "sendreceive", versioning=True)
        st.add_folder(g.baton_id, f"{group_name} (baton)", g.baton_file.parent, [])
        st.set_ignores(g.world_id, IGNORES)
        g._write_version(1)
        g.write_baton({"version": 1, "holder": None, "last_host": st.my_id, "last_host_name": player})
        return g

    @classmethod
    def join(cls, st, app_dir, invite: dict, servers_dir: Path):
        world_path = Path(servers_dir) / safe_folder_name(invite["g"])
        if world_path.exists() and any(world_path.iterdir()):
            raise ValueError(f"{world_path} already exists and isn't empty. Move or rename it first.")
        info = {"name": invite["g"], "world_id": invite["w"], "baton_id": invite["b"],
                "world_path": str(world_path), "owner": invite["o"]}
        g = cls(st, info, app_dir)
        world_path.mkdir(parents=True, exist_ok=True)
        g.baton_file.parent.mkdir(parents=True, exist_ok=True)
        st.add_device(invite["o"], invite["on"], introducer=True)
        st.add_folder(g.world_id, invite["g"], world_path, [invite["o"]], "receiveonly", versioning=True)
        st.add_folder(g.baton_id, f"{invite['g']} (baton)", g.baton_file.parent, [invite["o"]])
        st.set_ignores(g.world_id, IGNORES)
        return g

    def invite_code(self, player) -> str:
        return encode_invite(self.st.my_id, player, self.info["name"], self.world_id, self.baton_id)

    def add_member(self, join: dict) -> None:
        self.st.add_device(join["d"], join["n"])
        self.st.share_folder(self.world_id, join["d"])
        self.st.share_folder(self.baton_id, join["d"])

    # files
    @staticmethod
    def _read_json(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)

    def _mirror(self) -> Path | None:
        return self.backup_dir / f"{self.world_id}-baton.json" if self.backup_dir else None

    def read_baton(self) -> dict | None:
        """The newest of the Syncthing baton and the backup-folder copy."""
        mirror = self._mirror()
        return newest_baton(self._read_json(self.baton_file), mirror and self._read_json(mirror))

    def write_baton(self, baton: dict) -> None:
        self._write_json(self.baton_file, baton)
        self.st.scan(self.baton_id)
        if self._mirror():
            try:
                self.backup_dir.mkdir(parents=True, exist_ok=True)
                self._write_json(self._mirror(), baton)
            except OSError:
                pass  # backup folder unplugged/offline; Syncthing copy still works

    # backups
    def _zip(self, v: int) -> Path:
        return self.backup_dir / f"{self.world_id}-v{v}.zip"

    def backup_has(self, v: int) -> bool:
        """True if the backup folder holds a complete zip of version v (not still uploading)."""
        if not self.backup_dir:
            return False
        try:
            with zipfile.ZipFile(self._zip(v)) as z:
                return z.read(VERSION_FILE).decode().strip() == str(v)
        except (OSError, KeyError, zipfile.BadZipFile):
            return False

    def save_backup(self, v: int) -> None:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        part = self._zip(v).with_suffix(".zip.part")
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            for p in self.world_path.rglob("*"):
                rel = p.relative_to(self.world_path).as_posix()
                if p.is_file() and not skipped(rel):
                    z.write(p, rel)
        os.replace(part, self._zip(v))
        old = sorted(self.backup_dir.glob(f"{self.world_id}-v*.zip"), key=lambda p: int(p.stem.rsplit("-v", 1)[1]))
        for p in old[:-KEEP_BACKUPS]:
            p.unlink(missing_ok=True)

    def restore_backup(self, v: int) -> None:
        """Replace the synced files in the server folder with backup v. Per-PC files are left alone."""
        tmp = self.app_dir / "restore"
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(self._zip(v)) as z:
            z.extractall(tmp)  # extractall drops absolute paths and '..', so a bad zip can't write outside tmp
        if (tmp / VERSION_FILE).read_text().strip() != str(v):
            raise ValueError("The backup doesn't match its version number.")
        replace_synced_files(tmp, self.world_path, move=True)
        shutil.rmtree(tmp, ignore_errors=True)

    def move_world(self, new_path) -> None:
        """Use another server folder (e.g. one the player made in SquidServers) for this group.
        The group's files are copied in; that folder's own per-PC files (start scripts, RAM, logs) stay."""
        new, old = Path(new_path), self.world_path
        if new.resolve() == old.resolve():
            return
        if server_running(old) or server_running(new):
            raise ValueError("Stop the server in SquidServers first.")
        baton = self.read_baton()
        if baton and baton.get("holder") == self.st.my_id:
            raise ValueError("Press Stop hosting first.")
        self.st.set_folder(self.world_id, paused=True)
        copied = False
        try:
            replace_synced_files(old, new, move=False)
            shutil.copytree(old / ".stfolder", new / ".stfolder", dirs_exist_ok=True)  # Syncthing's folder marker
            copied = True
        finally:
            self.st.set_folder(self.world_id, path=str(new if copied else old), paused=False)
        self.st.set_ignores(self.world_id, IGNORES)  # ignore rules live in the folder's .stignore
        self.info["world_path"], self.world_path = str(new), new

    def local_version(self) -> int:
        try:
            return int((self.world_path / VERSION_FILE).read_text().strip())
        except (OSError, ValueError):
            return 0

    def _write_version(self, v: int) -> None:
        (self.world_path / VERSION_FILE).write_text(str(v))

    # every refresh
    def reconcile(self) -> None:
        """Put this PC's world folder in the mode the baton says, and drop stray local edits."""
        want_type, want_paused = desired_world_mode(self.read_baton(), self.st.my_id)
        f = self.st.folder(self.world_id)
        if f["paused"] and not want_paused:
            return  # we were hosting but lost the baton: keep the session until the player picks Discard
        if f["type"] == "receiveonly" and not f["paused"]:
            # Anything changed here without the baton (e.g. a server started while it wasn't our turn)
            # is undone before this PC may send again. Not while that server runs: wait for it to stop.
            if server_running(self.world_path):
                return
            try:
                if self.st.folder_status(self.world_id).get("receiveOnlyChangedFiles", 0):
                    self.st.revert(self.world_id)
                    return  # revert runs in the background; switch modes on a later refresh
            except Exception:
                return  # folder still starting up; next refresh retries
        if (f["type"], f["paused"]) != (want_type, want_paused):
            self.st.set_folder(self.world_id, type=want_type, paused=want_paused)

    def park(self) -> None:
        """On close: stop this PC sending until the next start has read the baton (see reconcile)."""
        f = self.st.folder(self.world_id)
        if not f["paused"] and f["type"] != "receiveonly":
            self.st.set_folder(self.world_id, type="receiveonly")

    def status(self) -> dict:
        now = time.time()
        baton = self.read_baton()
        try:
            fs = self.st.folder_status(self.world_id)
        except Exception:
            fs = {}
        need = fs.get("needFiles", 0)
        can, reason = host_check(baton, self.st.my_id, self.local_version(), need, now, self._backup_version(baton, need))
        names, online = self.st.devices(), self.st.connected()
        members = []
        for dev_id, name in names.items():
            if dev_id == self.st.my_id:
                continue
            try:
                done = self.st.completion(self.world_id, dev_id)
            except Exception:
                done = None
            members.append({"id": dev_id, "name": name, "online": dev_id in online, "synced": done})
        me_hosting = bool(baton and baton.get("holder") == self.st.my_id)
        try:
            lost = self.st.folder(self.world_id)["paused"] and not me_hosting
        except Exception:
            lost = False
        return {"baton": baton, "can_host": can and not lost, "reason": reason, "me_hosting": me_hosting, "lost_baton": lost,
                "holder_alive": holder_alive(baton, now), "need_files": need, "need_bytes": fs.get("needBytes", 0),
                "server_running": server_running(self.world_path), "members": members,
                "conflicts": self.conflicts(), "local_version": self.local_version()}

    def conflicts(self) -> list[str]:
        return [p.name for p in self.baton_file.parent.glob("*.sync-conflict-*")]

    def _backup_version(self, baton, need_files) -> int:
        """Version available in the backup folder, checked only when our own copy is behind."""
        v = (baton or {}).get("version", 0)
        behind = self.local_version() < v or need_files
        return v if behind and self.backup_has(v) else 0

    # hosting
    def start_hosting(self, player, address) -> None:
        baton = self.read_baton()
        need = self.status()["need_files"]
        backup_v = self._backup_version(baton, need)
        ok, reason = host_check(baton, self.st.my_id, self.local_version(), need, time.time(), backup_v)
        if not ok:
            raise ValueError(reason)
        if server_running(self.world_path) and backup_v:
            raise ValueError("Stop the server in SquidServers first, so WorldBaton can load the newest world.")
        # Pause first so nothing from this session leaks out until it is finished.
        self.st.set_folder(self.world_id, type="sendreceive", paused=True)
        if backup_v:
            try:
                self.restore_backup(backup_v)
            except Exception as e:
                self.st.set_folder(self.world_id, type="receiveonly", paused=False)
                raise ValueError(f"Couldn't load the backup: {e}")
        self.write_baton({**baton, "holder": self.st.my_id, "holder_name": player,
                          "address": address.strip(), "heartbeat": time.time(), "since": time.time()})

    def heartbeat(self) -> None:
        baton = self.read_baton()
        if baton and baton.get("holder") == self.st.my_id:
            self.write_baton({**baton, "heartbeat": time.time()})

    def stop_hosting(self, player) -> str:
        """Share the session. Returns a warning for the player, or '' if all went well."""
        if server_running(self.world_path):
            raise ValueError("Stop the server in SquidServers first, then press Stop hosting.")
        baton = self.read_baton()
        if not baton or baton.get("holder") != self.st.my_id:
            raise ValueError("Someone else took over while you were hosting, so your session wasn't shared. "
                             "Use 'Discard my session' to get their world, or ask the group what to keep.")
        v = baton.get("version", 0) + 1
        self._write_version(v)
        warning = ""
        if self.backup_dir:
            try:
                self.save_backup(v)  # before the baton says v exists, so nobody looks for a zip that isn't there
            except OSError as e:
                warning = f"Couldn't save to the backup folder ({e}). Friends can still get the world while you're online."
        self.write_baton({"version": v, "holder": None, "last_host": self.st.my_id, "last_host_name": player})
        self.st.set_folder(self.world_id, type="sendreceive", paused=False)
        self.st.scan(self.world_id)
        return warning

    def discard_session(self) -> None:
        if server_running(self.world_path):
            raise ValueError("Stop the server in SquidServers first.")
        self.st.set_folder(self.world_id, type="receiveonly", paused=False)
        time.sleep(1)
        self.st.revert(self.world_id)
