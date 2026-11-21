"""WorldBaton rules: who may host, and how each PC's copy of the world is set up.

Two Syncthing folders per group:
  world  - the server folder. Only the last host sends; everyone else is receive-only.
           Paused on the host while hosting, so friends never get half-saved files.
  baton  - one small baton.json everyone can write: who is hosting, their address, the world version.
           Also each PC's activity-<id>.jsonl: admin commands seen in the server log while it hosted.

Optional backup folder (e.g. a shared Google Drive/OneDrive folder) for groups that are never
online at the same time: each finished session is zipped there, and the baton is mirrored there.
The cloud copy (S3 storage such as Backblaze B2 or Cloudflare R2, set once for the whole group) does the same.
"""
import base64
import ctypes
import fnmatch
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import threading
import time
import zipfile
from pathlib import Path

from .cloud import S3, CloudError

STALE_AFTER = 300      # seconds without a heartbeat before others may take over
HEARTBEAT_EVERY = 30
VERSION_FILE = ".worldbaton-version"
KEEP_ACTIVITY = 500    # admin-log entries kept per PC
KEEP_LOCAL = 2         # world backups each PC keeps for restoring, unless changed in Settings
REQUEST_FOR = 3 * 3600  # a "can I host next?" request expires after this many seconds

# Server log lines: NeoForge "[20Sep2026 11:57:08.841] [Server thread/INFO] [net.minecraft.server.MinecraftServer/]: msg",
# vanilla "[11:57:08] [Server thread/INFO]: msg".
LOG_LINE = re.compile(r"^\[([^\]]+)\] \[Server thread/INFO\](?: \[[^\]]*\])?: (.*)$")
# logAdminCommands: "[Potrosh0: Gave 34 [Diamond] to X]". The source can also be a command block ("@"), "Rcon", a named block.
SOURCE_COMMAND = re.compile(r"^\[([^\[\]:]{1,40}): (.*)\]$")
# shortcut: console commands have no "who" marker, so only these common admin results are caught; add more as found.
CONSOLE_COMMAND = re.compile(
    r"^(Made \S+ (a|no longer a) server operator|Set \S+'s game mode|Set own game mode|Gave |Teleported |"
    r"Gamerule |Set the (time|weather|difficulty|world spawn|world border)|Set spawn point|Banned |Unbanned |Kicked |"
    r"Killed |Summoned |Applied (effect|enchantment)|Removed \d+ item|Cleared the inventory|Enchanting succeeded|"
    r"Added .* to the whitelist|Removed .* from the whitelist|Whitelist is now|Granted |Revoked |Successfully (filled|cloned)|"
    r"Changed the block|Placed |Executed \d+ command|Set \d+ |Modified )")
# NeoForge debug.log: 'Found mod file "x.jar" [locator: {mods folder locator at C:\...\mods}, reader: mod manifest]'
FOUND_MOD = re.compile(r'^\[([^\]]+)\] .*Found mod file "([^"]+)" \[locator: (.*)$')
# shortcut: NeoForge's own places for mods; other loaders' debug logs aren't checked.
NORMAL_LOCATORS = ("jarinjar", "production server provider", "PathBasedLocator[name=neoforge,", "org.sinytra.connector")
WATCHED_RULES = ("logAdminCommands", "commandBlockOutput")
WATCHED_SETTINGS = ("enable-command-block", "enable-rcon", "op-permission-level", "function-permission-level",
                    "online-mode", "white-list", "enforce-whitelist", "gamemode", "force-gamemode", "difficulty", "pvp",
                    "allow-flight", "spawn-protection", "level-name", "level-seed")
# Written by SquidServers itself before each start; their watched settings are compared instead.
NOT_EDITS = {"server.properties", "eula.txt", VERSION_FILE}

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
    if code.startswith("WBI1-"):
        raise ValueError("That group code is from an older WorldBaton. Ask your friend to open WorldBaton "
                         "(it updates itself) and send you the new group code and password.")
    if not code.startswith(prefix):
        raise ValueError("That isn't a WorldBaton group code.")
    raw = code[len(prefix):]
    try:
        return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:
        raise ValueError("The code is damaged. Copy it again in full.")


def encode_invite(owner_id, owner_name, group, world_id, baton_id) -> str:
    return _enc("WBI2-", {"o": owner_id, "on": owner_name, "g": group, "w": world_id, "b": baton_id})


def decode_invite(code: str) -> dict:
    d = _dec("WBI2-", code)
    if not all(isinstance(d.get(k), str) and d[k] for k in ("o", "on", "g", "w", "b")):
        raise ValueError("The group code is missing information.")
    return d


# --- joining with a password -----------------------------------------------------------
# A joining PC proves it knows the password without sending it: an HMAC of its own Syncthing ID, keyed by the
# password. It travels in the PC's name (seen by the owner's Syncthing as a "pending device") and in the folder
# names it offers (seen as "pending folders" when the owner already knows that PC from another group).
PROOF = re.compile(r"\s*\[wb:([0-9a-f,]*)\]")
MIN_PASSWORD = 6
GUESSES_PER_HOUR = 10  # wrong-password attempts before the owner's WorldBaton stops letting anyone in for an hour


def group_key(password: str, world_id: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), world_id.encode(), 200_000).hex()


def make_proof(key: str, device_id: str) -> str:
    return hmac.new(bytes.fromhex(key), device_id.encode(), "sha256").hexdigest()[:16]


def proofs_in(text: str) -> list[str]:
    m = PROOF.search(text or "")
    return [p for p in m.group(1).split(",") if p] if m else []


def plain_name(name: str) -> str:
    return PROOF.sub("", name or "").strip()


def with_proofs(name: str, proofs) -> str:
    proofs = list(proofs)
    return f"{plain_name(name)} [wb:{','.join(proofs)}]".strip() if proofs else plain_name(name)


def admit(st, groups, refused: dict, now=None) -> tuple[list, list]:
    """On the owner's PC: let in every PC that proved it knows one of this PC's groups' passwords.
    refused remembers wrong attempts ({(device, proofs): time}); too many in an hour and nobody is let in.
    Returns ([(group, name) let in], [names that just tried a wrong password])."""
    now = now or time.time()
    owned = [g for g in groups if g.is_owner() and g.info.get("key")]
    if not owned:
        return [], []
    for k in [k for k, t in refused.items() if now - t > 3600]:
        del refused[k]
    known = st.devices()
    asks = [(dev, d.get("name", ""), d.get("name", ""), None)
            for dev, d in (st.get("/rest/cluster/pending/devices") or {}).items()]
    ours = {fid for g in owned for fid in (g.world_id, g.baton_id)}
    for fid, f in (st.get("/rest/cluster/pending/folders") or {}).items():
        if fid in ours:
            asks += [(dev, known.get(dev, ""), d.get("label", ""), fid) for dev, d in f.get("offeredBy", {}).items()]
    let_in, wrong = [], []
    for dev, name, text, fid in asks:
        if len(refused) >= GUESSES_PER_HOUR:
            break  # paused
        proofs = proofs_in(text)
        if not proofs:
            continue  # not someone joining with WorldBaton
        name = plain_name(name) or "A friend"
        hits = [g for g in owned if fid in (None, g.world_id, g.baton_id) and make_proof(g.info["key"], dev) in proofs]
        for g in hits:
            if dev not in g.info.get("removed", []) and dev not in g.member_ids():
                g.add_member(dev, name)
                let_in.append((g, name))
        attempt = (dev, ",".join(proofs))  # the same attempt shows up once per folder
        if not hits:
            # forget it in Syncthing too, so old attempts don't count again once the hour is up
            st.delete(f"/rest/cluster/pending/devices?device={dev}" if fid is None else
                      f"/rest/cluster/pending/folders?folder={fid}&device={dev}")
            if attempt not in refused:
                refused[attempt] = now
                wrong.append(name)
    return let_in, wrong


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


def host_check(baton, my_id, local_version, need_files, now, backup_version=0,
               backup_where="the backup folder") -> tuple[bool, str]:
    """Can this PC start hosting? Returns (allowed, reason shown to the player)."""
    if not baton:
        return False, "Waiting for the group's data from the owner."
    if baton.get("holder") == my_id:
        return False, "You are already hosting."
    if holder_alive(baton, now):
        return False, f"{baton.get('holder_name', 'Someone')} is hosting right now."
    if local_version < baton.get("version", 0) or need_files:
        if backup_version >= baton.get("version", 0):
            return True, f"Your copy is out of date, so WorldBaton will load the newest world from {backup_where} first."
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


def server_state(world_path: Path) -> dict:
    """SquidServers' .mc_server_state.json: status, pid and ts (ms, when it was written)."""
    try:
        state = json.loads((Path(world_path) / ".mc_server_state.json").read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def server_pid(world_path: Path) -> int | None:
    """The server's process id while that process exists (even while it is still saving on shutdown)."""
    pid = server_state(world_path).get("pid")
    return pid if isinstance(pid, int) and pid_alive(pid) else None


def server_running(world_path: Path) -> bool:
    """True if SquidServers reports this server online and its process exists."""
    return server_state(world_path).get("status") == "online" and server_pid(world_path) is not None


def rolled_log_rest(logs: Path, prev: dict) -> list[str] | None:
    """The unread lines of a log the server moved aside while running, if one of the recently moved logs is the
    file WorldBaton was reading (same bytes where it stopped). None if none is."""
    recent = []
    for p in logs.glob("*.log*"):
        try:
            if p.name not in ("latest.log", "debug.log") and time.time() - p.stat().st_mtime < 1800:
                recent.append((p.stat().st_mtime, p))
        except OSError:
            pass
    start = prev["pos"] - len(prev["tail"])
    for _, p in sorted(recent, reverse=True)[:5]:
        try:
            data = gzip.decompress(p.read_bytes()) if p.suffix == ".gz" else p.read_bytes()
        except (OSError, EOFError, gzip.BadGzipFile):
            continue  # still being written
        if len(data) >= prev["pos"] and data[start:prev["pos"]] == prev["tail"]:
            return data[prev["pos"]:].decode("utf-8", "replace").splitlines()
    return None


def admin_line(line: str) -> tuple[str, str] | None:
    """(log time, what happened) if a server log line is an admin command, else None."""
    m = LOG_LINE.match(line.rstrip("\r\n"))
    if not m:
        return None
    at, msg = m.groups()
    if p := SOURCE_COMMAND.match(msg):
        return at, f"{p[1]}: {p[2]}"
    if CONSOLE_COMMAND.match(msg):
        return at, f"Console: {msg}"
    if "logAdminCommands" in msg or "commandBlockOutput" in msg:
        return at, f"⚠ {msg}"
    return None


def odd_mod_line(line: str, mods_dir: Path) -> tuple[str, str] | None:
    """A debug.log line saying the server loaded a mod from outside the synced mods folder."""
    m = FOUND_MOD.match(line.rstrip("\r\n"))
    if not m:
        return None
    at, jar, locator = m.groups()
    prefix = "{mods folder locator at "
    if locator.startswith(prefix):
        folder = locator[len(prefix):].split("}", 1)[0]
        if os.path.normcase(os.path.normpath(folder)) == os.path.normcase(os.path.normpath(mods_dir)):
            return None
    elif locator.startswith(NORMAL_LOCATORS):
        return None
    return at, f"⚠ The server loaded a mod from outside the mods folder: {jar} ({locator[:100]})"


def read_settings(world_path: Path) -> dict:
    try:
        text = (Path(world_path) / "server.properties").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    pairs = (line.split("=", 1) for line in text.splitlines() if "=" in line and not line.lstrip().startswith("#"))
    return {k.strip(): v.strip() for k, v in pairs}


def read_gamerules(level_dat: Path) -> dict:
    """The watched gamerules in level.dat. They're NBT string tags: 0x08, name length, name, value length, value."""
    # shortcut: finds the tags by bytes instead of parsing NBT; newer Minecraft versions that store gamerules
    # differently just show no gamerules, and the warning about them never appears.
    try:
        with gzip.open(level_dat) as f:
            data = f.read()
    except (OSError, EOFError):
        return {}
    rules = {}
    for name in WATCHED_RULES:
        key = b"\x08" + len(name).to_bytes(2, "big") + name.encode()
        i = data.find(key)
        if i >= 0:
            j = i + len(key)
            n = int.from_bytes(data[j:j + 2], "big")
            rules[name] = data[j + 2:j + 2 + n].decode("utf-8", "replace")
    return rules


def server_snapshot(world_path: Path) -> dict:
    """What players should hear about when it changes: mods, operators, datapacks, risky settings, gamerules."""
    world_path = Path(world_path)
    try:
        ops = sorted({o["name"] for o in json.loads((world_path / "ops.json").read_text())})
    except (OSError, ValueError, KeyError, TypeError):
        ops = []
    settings = read_settings(world_path)
    level = world_path / (settings.get("level-name") or "world")
    packs = sorted({f"{d.parent.name}/datapacks/{p.name}" if d.parent != world_path else p.name
                    for d in (world_path / "datapacks", level / "datapacks") if d.is_dir() for p in d.iterdir()})
    return {"mods": sorted(p.name for p in (world_path / "mods").glob("*.jar")), "ops": ops, "datapacks": packs,
            "settings": {k: settings[k] for k in WATCHED_SETTINGS if k in settings},
            "rules": read_gamerules(level / "level.dat")}


def snapshot_changes(old: dict, new: dict) -> list[str]:
    def added_removed(key, added, removed):
        if key not in old:
            return []
        a, b = set(old[key]), set(new[key])
        return [f"{added}: {x}" for x in sorted(b - a)] + [f"{removed}: {x}" for x in sorted(a - b)]

    def values(key, label):
        if key not in old:
            return []
        a, b = old[key], new[key]
        return [f"{label} {k}: {a.get(k, '(not set)')} → {b.get(k, '(not set)')}"
                for k in sorted(a.keys() | b.keys()) if a.get(k) != b.get(k)]

    return (added_removed("mods", "New mod", "Mod removed") + added_removed("ops", "Now an operator", "No longer an operator") +
            added_removed("datapacks", "New datapack", "Datapack removed") +
            values("settings", "Server setting") + values("rules", "Gamerule"))


def watch_warnings(snapshot: dict) -> list[str]:
    """Settings that blind the activity list, shown to everyone for as long as they're set."""
    rules, settings = snapshot.get("rules", {}), snapshot.get("settings", {})
    out = []
    if rules.get("logAdminCommands") == "false":
        out.append("Admin commands aren't logged in this world (gamerule logAdminCommands is false), "
                   "so Host activity can't see them.")
    if settings.get("enable-command-block") == "true" and rules.get("commandBlockOutput") == "false":
        out.append("Command blocks are on and their commands aren't logged (gamerule commandBlockOutput is false).")
    if settings.get("enable-rcon") == "true":
        out.append("RCON (remote console) is on: someone with its password can run commands from outside the game.")
    if settings.get("online-mode") == "false":
        out.append("Names aren't checked (online-mode is false): anyone can join as someone else, even an operator, "
                   "and their commands are logged under that name.")
    return out


def events(prev: dict | None, cur: dict, me: str) -> list[tuple[str, str]]:
    """Notifications worth showing, from two status() snapshots taken a few seconds apart: (title, message)."""
    if not prev:
        return []  # first look after starting: nothing "happened" yet
    out = []
    was, now = prev.get("baton") or {}, cur.get("baton") or {}
    holder, name = now.get("holder"), now.get("holder_name", "Someone")
    if holder and holder != me and cur.get("holder_alive"):
        if holder != was.get("holder"):
            out.append((f"{name} is hosting", "They're getting the server ready."))
        if now.get("server_up") and not (was.get("server_up") and holder == was.get("holder")):
            out.append((f"{name}'s server is up", f"Join at {now['address']}" if now.get("address") else "Join now."))
    old_holder = was.get("holder")
    if old_holder and old_holder != me and not holder and prev.get("holder_alive"):
        out.append((f"{was.get('holder_name', 'Someone')} stopped hosting",
                    "You can host now." if cur.get("my_request") else "Their session is being shared."))
    if cur.get("me_hosting"):
        for asker in [r for r in cur.get("requests", []) if r not in prev.get("requests", [])]:
            out.append((f"{asker} wants to host next", "Stop the server in SquidServers, then press Stop hosting."))
    return out


def file_marks(world_path: Path) -> dict:
    """Size and modified time of every synced file, to spot files changed outside the game."""
    world_path, out = Path(world_path), {}
    for p in world_path.rglob("*"):
        rel = p.relative_to(world_path).as_posix()
        if rel in NOT_EDITS or skipped(rel):
            continue
        try:
            if p.is_file():
                info = p.stat()
                out[rel] = [info.st_mtime_ns, info.st_size]
        except OSError:
            pass
    return out


def describe_edits(before: dict, after: dict, world_path: Path) -> str | None:
    changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    if not changed:
        return None
    try:
        names = {u["uuid"]: u["name"] for u in json.loads((Path(world_path) / "usercache.json").read_text())}
    except (OSError, ValueError, KeyError, TypeError):
        names = {}
    shown = [f"{c} ({names[Path(c).stem]})" if Path(c).stem in names else c for c in changed[:6]]
    more = f" (+{len(changed) - 6} more)" if len(changed) > 6 else ""
    return f"⚠ Files changed while the server was off, not by the game: {', '.join(shown)}{more}"


def check_not_taken(path: Path, taken) -> None:
    """Refuse a server folder that another group already uses (or that sits inside or around one)."""
    path = Path(path).resolve()
    for other in taken:
        other = Path(other).resolve()
        if path == other or other in path.parents or path in other.parents:
            raise ValueError(f"{other} already belongs to another of your groups. Each group needs its own server folder.")


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


# --- cloud copy (S3) ---------------------------------------------------------------------
class CloudCopy:
    """The group's cloud copy: a zip of each finished session plus a copy of the baton, so friends who are never
    online together still get the newest world. Reads come from a copy refreshed in the background, so the window
    never waits on the internet; uploads and downloads run in WorldBaton's busy work."""
    REFRESH = 300       # seconds between looks at the cloud (a GET and a LIST: well inside free tiers)
    MIRROR_EVERY = 120  # a host's heartbeat is copied this often (alive means < STALE_AFTER old)

    def __init__(self, cfg: dict, world_id: str):
        self.cfg, self.world_id = dict(cfg), world_id
        self.s3 = S3(cfg.get("endpoint", ""), cfg.get("bucket", ""), cfg.get("key_id", ""), cfg.get("secret", ""))
        self.baton, self.zips = None, {}  # zips: version -> {"size", "mtime"}
        self.error, self.checked = None, 0.0
        self._lock = threading.Lock()
        self._mirrored = (None, 0.0)

    def zip_name(self, v: int) -> str:
        return f"{self.world_id}-v{v}.zip"

    @property
    def _baton_name(self) -> str:
        return f"{self.world_id}-baton.json"

    def refresh(self) -> None:
        """Look at the cloud now (network: busy work or a background thread only)."""
        self.checked = time.time()
        try:
            found = {}
            for key, (size, mtime) in self.s3.list(self.world_id + "-v").items():
                m = re.fullmatch(re.escape(self.world_id) + r"-v(\d+)\.zip", key)
                if m:
                    found[int(m.group(1))] = {"size": size, "mtime": mtime}
            self.baton, self.zips, self.error = self._get_baton(), found, None
        except CloudError as e:
            self.error = str(e)
            raise

    def cached_baton(self) -> dict | None:
        if time.time() - self.checked > self.REFRESH:
            self.checked = time.time()  # one look at a time
            threading.Thread(target=self._quiet, args=(self.refresh,), daemon=True).start()
        return self.baton

    def _quiet(self, work) -> None:
        try:
            work()
        except Exception as e:
            self.error = str(e)

    def _get_baton(self) -> dict | None:
        try:
            b = json.loads(self.s3.get(self._baton_name) or b"null")
            return b if isinstance(b, dict) else None
        except ValueError:
            return None

    def write_baton(self, baton: dict, now=False) -> None:
        """Copy the baton up, unless the cloud already has a newer one. now: wait for it (raises CloudError)."""
        fields = {k: baton.get(k) for k in ("version", "holder", "server_up", "address")}
        if not now and fields == self._mirrored[0] and time.time() - self._mirrored[1] < self.MIRROR_EVERY:
            return  # only the heartbeat moved, and it was copied recently
        self._mirrored = (fields, time.time())

        def work():
            with self._lock:  # so an older baton never lands on top of a newer one from this PC
                current = self._get_baton()
                if current and baton_key(current) > baton_key(baton):
                    self.baton = current
                    return
                self.s3.put(self._baton_name, json.dumps(baton).encode())
                self.baton = baton
        if now:
            work()
        else:
            threading.Thread(target=self._quiet, args=(work,), daemon=True).start()

    def upload(self, v: int, path: Path, progress=None) -> None:
        self.s3.put_file(self.zip_name(v), path, progress)
        self.refresh()
        for old in sorted(self.zips)[:-KEEP_BACKUPS]:
            try:
                self.s3.delete(self.zip_name(old))
                self.zips.pop(old, None)
            except CloudError:
                pass  # tried again after the next session

    def download(self, v: int, dest: Path, progress=None) -> None:
        self.s3.get_file(self.zip_name(v), dest, progress)


# --- group (uses Syncthing) ------------------------------------------------------------
class Group:
    def __init__(self, st, info: dict, app_dir: Path):
        self.st, self.info = st, info
        self.world_id, self.baton_id = info["world_id"], info["baton_id"]
        self.world_path = Path(info["world_path"])
        self.app_dir = Path(app_dir)
        self.baton_file = self.app_dir / f"baton-{self.baton_id}" / "baton.json"
        self._logs = {}  # per log file: creation time, bytes read, last bytes read, server pid
        self._by = {}    # activity file -> (mtime, short ID of the PC that last changed it)
        self._cloud = None
        self.transfer = None  # e.g. "Uploading the world 42%", for the window to show

    @property
    def backup_dir(self) -> Path | None:
        return Path(self.info["backup_dir"]) if self.info.get("backup_dir") else None

    @property
    def cloud(self) -> CloudCopy | None:
        """The group's cloud copy, if someone in the group set one up (Settings)."""
        cfg = self.group_settings().get("cloud")
        if not cfg:
            self._cloud = None
        elif not self._cloud or self._cloud.cfg != cfg:
            try:
                self._cloud = CloudCopy(cfg, self.world_id)
            except ValueError:
                self._cloud = None  # unusable settings; Settings says what's wrong when they're saved
        return self._cloud

    def set_cloud(self, cfg: dict | None) -> None:
        """Turn the cloud copy on (S3 settings) or off (None) for the whole group, and tell it the current state."""
        if cfg:
            S3(cfg["endpoint"], cfg["bucket"], cfg["key_id"], cfg["secret"]).check(f"{self.world_id}-check")
        self.set_group_setting("cloud", cfg)
        baton = self.read_baton()
        if cfg and baton:
            self.cloud.write_baton(baton, now=True)

    def _progress(self, what: str):
        def show(fraction):
            self.transfer = f"{what} {fraction:.0%}"
        return show

    def set_backup_dir(self, path) -> None:
        self.info["backup_dir"] = str(path) if path else None
        baton = self.read_baton()
        if baton:
            self.write_baton(baton)  # so the backup folder knows the current state right away

    # setup
    @classmethod
    def create(cls, st, app_dir, group_name, world_path, player, taken=()):
        check_not_taken(world_path, taken)
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
    def join(cls, st, app_dir, invite: dict, servers_dir: Path, password: str, player: str, joined=(), waiting=()):
        """Set up this PC's copy and ask the owner to let it in. waiting: proofs of other groups still being joined."""
        if invite["w"] in joined:
            raise ValueError(f"You're already in {invite['g']}.")
        world_path = Path(servers_dir) / safe_folder_name(invite["g"])
        if world_path.exists() and any(world_path.iterdir()):
            raise ValueError(f"{world_path} already exists and isn't empty. Move or rename it first.")
        info = {"name": invite["g"], "world_id": invite["w"], "baton_id": invite["b"],
                "world_path": str(world_path), "owner": invite["o"], "owner_name": invite["on"]}
        g = cls(st, info, app_dir)
        world_path.mkdir(parents=True, exist_ok=True)
        g.baton_file.parent.mkdir(parents=True, exist_ok=True)
        st.add_folder(g.world_id, invite["g"], world_path, [], "receiveonly", versioning=True)
        st.add_folder(g.baton_id, f"{invite['g']} (baton)", g.baton_file.parent, [])
        st.set_ignores(g.world_id, IGNORES)
        g.set_join_password(password, player, waiting)
        return g

    def set_join_password(self, password: str, player: str, waiting=()) -> None:
        """Put the proof of the password in this PC's name and in the folders it offers the owner, then offer them."""
        self.info["proof"] = make_proof(group_key(password, self.world_id), self.st.my_id)
        tag, owner = with_proofs("", [self.info["proof"]]), self.info["owner"]
        self.st.set_name(with_proofs(player, [*waiting, self.info["proof"]]))  # before the owner is added: first hello
        for folder_id, label in ((self.world_id, self.info["name"]), (self.baton_id, f"{self.info['name']} (baton)")):
            self.st.unshare_folder(folder_id, owner)  # Syncthing only re-offers a renamed folder when its sharing changes
            self.st.set_folder(folder_id, label=f"{label} {tag}")
        if owner not in self.st.devices():
            self.st.add_device(owner, self.info.get("owner_name", "Owner"), introducer=True)
        else:  # known from another group: keep its addresses, but let it introduce this group's friends
            self.st.patch(f"/rest/config/devices/{owner}", {"introducer": True})
        self.st.share_folder(self.world_id, owner)
        self.st.share_folder(self.baton_id, owner)

    def joined(self, player: str, waiting=()) -> None:
        """The owner let this PC in: take the password proof back out of its names."""
        self.info.pop("proof", None)
        self.st.set_name(with_proofs(player, waiting))
        self.st.set_folder(self.world_id, label=self.info["name"])
        self.st.set_folder(self.baton_id, label=f"{self.info['name']} (baton)")

    def is_owner(self) -> bool:
        return self.info.get("owner") == self.st.my_id

    def set_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"Use at least {MIN_PASSWORD} characters.")
        self.info["password"], self.info["key"] = password, group_key(password, self.world_id)

    def invite_code(self, owner_name) -> str:
        return encode_invite(self.info["owner"], owner_name, self.info["name"], self.world_id, self.baton_id)

    def member_ids(self) -> set[str]:
        return {d["deviceID"] for d in self.st.folder(self.world_id)["devices"]} - {self.st.my_id}

    def add_member(self, device_id: str, name: str) -> None:
        if device_id not in self.st.devices():
            self.st.add_device(device_id, name)
        self.st.share_folder(self.world_id, device_id)
        self.st.share_folder(self.baton_id, device_id)

    def remove_member(self, device_id: str) -> None:
        """Owner only: stop sharing this group with a PC. Everyone else drops it too (the owner introduced it),
        and it isn't let back in, even with the password."""
        self.info["removed"] = [*self.info.get("removed", []), device_id]
        for folder_id in (self.world_id, self.baton_id):
            self.st.unshare_folder(folder_id, device_id)
        if not any(device_id == d["deviceID"] for f in self.st.get("/rest/config/folders") for d in f["devices"]):
            self.st.remove_device(device_id)  # in no other group of this PC

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
        """The newest of the Syncthing baton, the backup-folder copy and the cloud copy."""
        mirror, cloud = self._mirror(), self.cloud
        return newest_baton(self._read_json(self.baton_file), mirror and self._read_json(mirror),
                            cloud and cloud.cached_baton())

    def write_baton(self, baton: dict, cloud="background") -> None:
        """cloud: "background", "now" (wait; raises CloudError) or "skip" (the caller copies it up later)."""
        self._write_json(self.baton_file, baton)
        self.st.scan(self.baton_id)
        if self.cloud and cloud != "skip":
            self.cloud.write_baton(baton, now=cloud == "now")
        if self._mirror():
            try:
                self.backup_dir.mkdir(parents=True, exist_ok=True)
                self._write_json(self._mirror(), baton)
            except OSError:
                pass  # backup folder unplugged/offline; Syncthing copy still works

    # backups: the shared backup folder (for groups never online together) and this PC's own copies (for restoring)
    def _zip(self, v: int, folder: Path | None = None) -> Path:
        return (folder or self.backup_dir) / f"{self.world_id}-v{v}.zip"

    @property
    def local_backup_dir(self) -> Path:
        return self.app_dir / "backups"

    @property
    def keep_local(self) -> int:
        """How many world backups this PC keeps for restoring (0 = off)."""
        return int(self.info.get("local_backups", KEEP_LOCAL))

    @staticmethod
    def _zip_version(path: Path) -> int | None:
        """The version a complete backup zip holds, or None if it's missing, broken or still uploading."""
        try:
            with zipfile.ZipFile(path) as z:
                return int(z.read(VERSION_FILE).decode().strip())
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            return None

    def _folder_has(self, v: int) -> bool:
        """True if the backup folder holds a complete zip of version v (not still uploading)."""
        return bool(self.backup_dir) and self._zip_version(self._zip(v)) == v

    def backup_has(self, v: int) -> bool:
        """True if the backup folder or the cloud copy holds version v."""
        return self._folder_has(v) or bool(self.cloud and v in self.cloud.zips)

    def _write_zip(self, folder: Path, v: int, keep: int) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        part = self._zip(v, folder).with_suffix(".zip.part")
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            for p in self.world_path.rglob("*"):
                rel = p.relative_to(self.world_path).as_posix()
                if p.is_file() and not skipped(rel):
                    z.write(p, rel)
        os.replace(part, self._zip(v, folder))
        old = sorted(folder.glob(f"{self.world_id}-v*.zip"), key=lambda p: int(p.stem.rsplit("-v", 1)[1]))
        for p in old[:-keep]:
            p.unlink(missing_ok=True)
        return self._zip(v, folder)

    def save_backup(self, v: int) -> None:
        self._write_zip(self.backup_dir, v, KEEP_BACKUPS)

    def save_local_backup(self, v: int) -> Path:
        return self._write_zip(self.local_backup_dir, v, self.keep_local)

    def backups(self) -> list[dict]:
        """Complete backups this PC can restore from, newest first: version, path, where, size."""
        cloud = self.cloud.zips if self.cloud else {}
        found = {v: {"version": v, "path": None, "where": "cloud", **z} for v, z in cloud.items()}
        for where, folder in (("backup folder", self.backup_dir), ("this PC", self.local_backup_dir)):
            if not folder or not folder.is_dir():
                continue
            for p in folder.glob(f"{self.world_id}-v*.zip"):
                v = self._zip_version(p)
                if v is not None:
                    info = p.stat()
                    found[v] = {"version": v, "path": p, "where": where, "size": info.st_size, "mtime": info.st_mtime}
        return sorted(found.values(), key=lambda b: b["version"], reverse=True)

    def restore_zip(self, path: Path, v: int) -> None:
        """Replace the synced files in the server folder with a backup. Per-PC files are left alone."""
        tmp = self.app_dir / "restore"
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(path) as z:
            z.extractall(tmp)  # extractall drops absolute paths and '..', so a bad zip can't write outside tmp
        if (tmp / VERSION_FILE).read_text().strip() != str(v):
            raise ValueError("The backup doesn't match its version number.")
        replace_synced_files(tmp, self.world_path, move=True)
        shutil.rmtree(tmp, ignore_errors=True)

    def restore_backup(self, v: int) -> None:
        """Load version v from the backup folder, or else download it from the cloud copy."""
        if self._folder_has(v):
            return self.restore_zip(self._zip(v), v)
        tmp = self.app_dir / "download" / f"{self.world_id}-v{v}.zip"
        try:
            self.cloud.download(v, tmp, self._progress("Downloading the world"))
            self.restore_zip(tmp, v)
        finally:
            self.transfer = None
            tmp.unlink(missing_ok=True)

    def restore_version(self, player, backup: dict) -> str:
        """Bring back an earlier world for everyone: take the baton, put the backup in place, share it as a new
        version. Returns stop_hosting's warning ('' if none)."""
        if server_running(self.world_path):
            raise ValueError("Stop the server in SquidServers first.")
        self.start_hosting(player, "")
        try:
            if backup["path"]:
                self.restore_zip(backup["path"], backup["version"])
            else:
                self.restore_backup(backup["version"])
        except Exception as e:
            self.discard_session()
            self.write_baton({**self.read_baton(), "holder": None})
            raise ValueError(f"Couldn't restore that backup: {e}")
        return self.stop_hosting(player, restored_from=backup["version"])

    # requests to host next: one small file per PC in the baton folder, so two requests never clash
    def _request_file(self, device_id: str | None = None) -> Path:
        return self.baton_file.parent / f"request-{(device_id or self.st.my_id)[:7]}.json"

    def request_host(self, player) -> None:
        self._write_json(self._request_file(), {"name": player, "t": time.time()})
        self.st.scan(self.baton_id)

    def cancel_request(self) -> None:
        if self._request_file().exists():
            self._request_file().unlink()
            self.st.scan(self.baton_id)

    def my_request(self) -> bool:
        r = self._read_json(self._request_file())
        return bool(r) and time.time() - r.get("t", 0) < REQUEST_FOR

    def requests(self) -> list[str]:
        """Names of other players asking to host next, oldest request first."""
        mine = self._request_file().name
        found = []
        for p in self.baton_file.parent.glob("request-*.json"):
            r = self._read_json(p)
            if p.name != mine and r and time.time() - r.get("t", 0) < REQUEST_FOR:
                found.append((r.get("t", 0), r.get("name", "Someone")))
        return [name for _, name in sorted(found)]

    # session history: also one file per PC
    @property
    def _history_file(self) -> Path:
        return self.baton_file.parent / f"history-{self.st.my_id[:7]}.jsonl"

    def history(self, limit=100) -> list[dict]:
        entries = [e for p in self.baton_file.parent.glob("history-*.jsonl") for e in self.read_activity_file(p)]
        return sorted(entries, key=lambda e: e.get("end", 0), reverse=True)[:limit]

    def _add_history(self, entry: dict) -> None:
        entries = (self.read_activity_file(self._history_file) + [entry])[-KEEP_ACTIVITY:]
        tmp = self._history_file.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        os.replace(tmp, self._history_file)

    # group-wide settings (e.g. the Discord webhook), editable by anyone in the group
    @property
    def _settings_file(self) -> Path:
        return self.baton_file.parent / "group-settings.json"

    def group_settings(self) -> dict:
        return self._read_json(self._settings_file) or {}

    def set_group_setting(self, key, value) -> None:
        self._write_json(self._settings_file, {**self.group_settings(), key: value})
        self.st.scan(self.baton_id)

    def update_server_up(self) -> str | None:
        """While hosting: note in the baton whether the server is running, so friends can be told it's up.
        Returns "up" or "down" when that just changed."""
        baton = self.read_baton()
        if not baton or baton.get("holder") != self.st.my_id:
            return None
        up = server_running(self.world_path)
        if bool(baton.get("server_up")) == up:
            return None
        self.write_baton({**baton, "server_up": up})
        return "up" if up else "down"

    def move_world(self, new_path, taken=()) -> None:
        """Use another server folder (e.g. one the player made in SquidServers) for this group.
        The group's files are copied in; that folder's own per-PC files (start scripts, RAM, logs) stay."""
        new, old = Path(new_path), self.world_path
        if new.resolve() == old.resolve():
            return
        check_not_taken(new, taken)
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
        if f["paused"] and f["type"] != "receiveonly" and not want_paused:
            return  # we were hosting but lost the baton: keep the session until the player picks Discard
        if not want_paused and server_running(self.world_path):
            # A server running when it isn't this PC's turn: send none of its half-saved world, and don't let the
            # newer world land under it. Syncthing waits ~10 s after a change before sending; we look every 3 s.
            if (f["type"], f["paused"]) != ("receiveonly", True):
                self.st.set_folder(self.world_id, type="receiveonly", paused=True)
            return
        if f["type"] == "receiveonly":
            if f["paused"]:  # that server stopped: what it changed is undone next
                self.st.set_folder(self.world_id, paused=False)
                return
            try:
                fs = self.st.folder_status(self.world_id)
            except Exception:
                return  # folder still starting up; next refresh retries
            if fs.get("receiveOnlyChangedFiles", 0):
                self.st.revert(self.world_id)
                return  # revert runs in the background; switch modes on a later refresh
            if want_type == "sendreceive" and self._need(fs):
                return  # the last host undoing a session: get the real world back from a friend before sending again
        if (f["type"], f["paused"]) != (want_type, want_paused):
            self.st.set_folder(self.world_id, type=want_type, paused=want_paused)

    def _need(self, fs: dict) -> int:
        """Files this PC still needs, not counting server.properties and eula.txt. SquidServers rewrites those by
        itself; undoing that must not stop anyone hosting while the friend who has the group's copy is offline."""
        n = fs.get("needFiles", 0)
        if not 0 < n <= 2:
            return n
        try:
            d = self.st.get(f"/rest/db/need?folder={self.world_id}")
            names = [x["name"] for k in ("progress", "queued", "rest") for x in d.get(k) or []]
        except Exception:
            return n
        return n if len(names) != n else sum(x not in NOT_EDITS - {VERSION_FILE} for x in names)

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
        need = self._need(fs)
        backup_v = self._backup_version(baton, need)
        can, reason = host_check(baton, self.st.my_id, self.local_version(), need, now, backup_v,
                                 "the backup folder" if backup_v and self._folder_has(backup_v) else "the cloud copy")
        names, online = self.st.devices(), self.st.connected()
        try:  # only this group's friends (other groups' friends are devices too)
            shared = {d["deviceID"] for d in self.st.folder(self.world_id).get("devices", [])}
        except Exception:
            shared = set(names)
        members = []
        for dev_id, name in names.items():
            if dev_id == self.st.my_id or dev_id not in shared:
                continue
            try:
                done = self.st.completion(self.world_id, dev_id)
            except Exception:
                done = None
            members.append({"id": dev_id, "name": plain_name(name) or "Friend", "online": dev_id in online, "synced": done})
        me_hosting = bool(baton and baton.get("holder") == self.st.my_id)
        running = server_running(self.world_path)
        try:
            f = self.st.folder(self.world_id)
        except Exception:
            f = {"paused": False, "type": "receiveonly"}
        lost = f["paused"] and f["type"] != "receiveonly" and not me_hosting
        if not me_hosting and not lost:
            if running:
                can, reason = False, ("Stop the server in SquidServers first, then press Host now. It isn't your turn, "
                                      "so syncing is paused while it runs, and what it changes will be undone.")
            elif f["type"] == "receiveonly" and (f["paused"] or fs.get("receiveOnlyChangedFiles", 0) or (
                    desired_world_mode(baton, self.st.my_id)[0] == "sendreceive" and need)):
                can, reason = False, ("Undoing what your server changed while it wasn't your turn" +
                                      (" (needs a friend online)." if need else "…"))
        if self.info.get("proof") and not baton:
            reason = (f"Waiting for {self.info.get('owner_name', 'the owner')} to let you in. Their WorldBaton needs to "
                      "be open (the tray is enough). If nothing happens, check the password with them.")
        return {"baton": baton, "can_host": can and not lost, "reason": reason, "me_hosting": me_hosting, "lost_baton": lost,
                "holder_alive": holder_alive(baton, now), "need_files": need, "need_bytes": fs.get("needBytes", 0),
                "server_running": running, "members": members,
                "conflicts": self.conflicts(), "local_version": self.local_version(),
                "requests": self.requests(), "my_request": self.my_request()}

    def conflicts(self) -> list[str]:
        return [p.name for p in self.baton_file.parent.glob("*.sync-conflict-*")]

    # admin activity: each PC writes only its own file, so the baton folder never gets conflicts
    @property
    def activity_file(self) -> Path:
        return self.baton_file.parent / f"activity-{self.st.my_id[:7]}.jsonl"

    @property
    def _marks_file(self) -> Path:
        return self.app_dir / f"offline-marks-{self.world_id}.json"

    @property
    def _seen_file(self) -> Path:
        return self.app_dir / f"activity-seen-{self.baton_id}.json"

    def _new_log_lines(self, name: str) -> tuple[list[str], str | None]:
        """New complete lines of logs/<name> since the last read, and a warning if it was edited in between."""
        log = self.world_path / "logs" / name
        try:
            info = log.stat()
        except OSError:
            return [], None
        # Which file this is: its ID, which changes when the server starts a new log. Not its creation time:
        # Windows gives a new file the creation time of the one it replaces (tested with a real server).
        born = info.st_ino or getattr(info, "st_birthtime", info.st_ctime)
        pid = server_pid(self.world_path)
        prev = self._logs.get(name)
        pos, warning, rolled = 0, None, None
        with open(log, "rb") as f:
            same = bool(prev) and prev["born"] == born
            if same:
                f.seek(max(0, prev["pos"] - len(prev["tail"])))
            if same and info.st_size >= prev["pos"] and f.read(len(prev["tail"])) == prev["tail"]:
                pos = prev["pos"]  # it only grew
            elif prev:
                running = bool(pid) and prev["pid"] == pid
                if running:
                    # Minecraft starts a new latest.log on the first line after midnight (debug.log at 200 MB) and
                    # keeps the old one as logs/<date>-1.log.gz; when another program has the log open, it copies
                    # and empties it instead. Fine if that file is the one we were reading.
                    rolled = rolled_log_rest(log.parent, prev)
                    if rolled is None and time.time() - prev.setdefault("rolled_at", time.time()) < 120:
                        return [], None  # the old log may still be being compressed: look again next time
                if rolled is None and (same or running):
                    warning = f"⚠ The server log ({name}) was " + ("edited or cut while hosting." if same else
                                                                  "replaced while the server kept running.")
            f.seek(pos)
            data = f.read()
            end = pos + data.rfind(b"\n") + 1  # leave a half-written last line for next time
            f.seek(max(0, end - 64))
            tail = f.read(end - max(0, end - 64))
        self._logs[name] = {"born": born, "pos": end, "tail": tail, "pid": pid}
        return (rolled or []) + data[:end - pos].decode("utf-8", "replace").splitlines(), warning

    def _skip_old_log(self) -> None:
        self._logs = {}
        for name in ("latest.log", "debug.log"):
            self._new_log_lines(name)

    def record_activity(self, player) -> None:
        """Read the server logs for admin commands and signs of tampering (called every few seconds while hosting)."""
        now = time.strftime("%d%b%Y %H:%M:%S")
        lines, warn = self._new_log_lines("latest.log")
        found = [a for a in map(admin_line, lines) if a]
        debug, debug_warn = self._new_log_lines("debug.log")
        found += [a for a in (odd_mod_line(line, self.world_path / "mods") for line in debug) if a]
        found += [(now, w) for w in (warn, debug_warn) if w]
        pid, state = server_pid(self.world_path), server_state(self.world_path)
        log = self.world_path / "logs" / "latest.log"
        if pid and isinstance(state.get("ts"), (int, float)):
            # A running server writes latest.log as it starts. If it hasn't since well before SquidServers marked it
            # started, file logging was turned off (e.g. a custom log setup in this PC's JVM arguments).
            try:
                stale = log.stat().st_mtime < state["ts"] / 1000 - 600
            except OSError:
                stale = True
            if stale:
                found.append((f"no-log-{pid}", "⚠ The server is running but isn't writing its log, "
                                               "so its commands can't be seen."))
        self.add_activity(player, found)

    def add_activity(self, player, found: list[tuple[str, str]]) -> None:
        if not found:
            return
        entries = self.read_activity_file(self.activity_file)
        seen = {(e["at"], e["text"]) for e in entries}  # after a WorldBaton restart the log is read again
        now = time.time()
        entries += [{"t": now, "at": at, "host": player, "text": text} for at, text in found if (at, text) not in seen]
        tmp = self.activity_file.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(e) + "\n" for e in entries[-KEEP_ACTIVITY:]), encoding="utf-8")
        os.replace(tmp, self.activity_file)
        self.st.scan(self.baton_id)

    def watch_offline_edits(self, player) -> None:
        """While hosting: remember the files when the server stops, report what changed by the time it starts again."""
        alive = server_pid(self.world_path) is not None
        if not alive and not self._marks_file.exists():
            self._write_json(self._marks_file, file_marks(self.world_path))
        elif alive and self._marks_file.exists():
            self.report_offline_edits(player)

    def report_offline_edits(self, player) -> None:
        before = self._read_json(self._marks_file)
        if before is not None:
            text = describe_edits(before, file_marks(self.world_path), self.world_path)
            if text:
                self.add_activity(player, [(time.strftime("%d%b%Y %H:%M:%S"), text)])
        self._marks_file.unlink(missing_ok=True)

    @staticmethod
    def read_activity_file(path: Path) -> list[dict]:
        try:
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, ValueError):
            return []

    def _modified_by(self, path: Path) -> str | None:
        """Short ID of the PC that last changed a synced file (Syncthing knows), cached until the file changes."""
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return None
        cached = self._by.get(path.name)
        if not cached or cached[0] != mtime:
            try:
                by = self.st.get(f"/rest/db/file?folder={self.baton_id}&file={path.name}")["global"]["modifiedBy"]
            except Exception:
                return None
            self._by[path.name] = cached = (mtime, by)
        return cached[1]

    def activity(self, limit=50) -> list[dict]:
        """Newest first, from every PC that has hosted. Entries this PC saw before stay, marked "removed", if a list
        loses them; a list last changed by a PC other than its owner gets a warning entry."""
        # shortcut: not mirrored to the backup folder, so groups never online together don't see it; add if asked.
        seen = self._read_json(self._seen_file) or {}
        try:
            names = {i[:7]: n for i, n in self.st.devices().items()}
        except Exception:
            names = {}
        files = {p.name: p for p in self.baton_file.parent.glob("activity-*.jsonl")}
        out, new_seen = [], {}
        for name in sorted(files.keys() | seen.keys()):
            current = self.read_activity_file(files[name]) if name in files else []
            keys = {(e["at"], e["text"]) for e in current}
            oldest = min((e["t"] for e in current), default=None)
            removed = [{**e, "removed": True} for e in seen.get(name, [])
                       if (e["at"], e["text"]) not in keys and (oldest is None or e["t"] >= oldest)]  # older = trimmed
            out += current + removed
            new_seen[name] = (current + [{k: v for k, v in e.items() if k != "removed"} for e in removed])[-KEEP_ACTIVITY:]
            owner = name.removeprefix("activity-").removesuffix(".jsonl")
            by = self._modified_by(files[name]) if name in files else None
            if by and by != owner:
                out.append({"t": files[name].stat().st_mtime, "host": "",
                            "text": f"⚠ {names.get(owner, owner)}'s activity list was last changed by "
                                    f"{names.get(by, 'another PC')} ({by})."})
        if new_seen != seen:
            self._write_json(self._seen_file, new_seen)
        return sorted(out, key=lambda e: e.get("t", 0), reverse=True)[:limit]

    def _backup_version(self, baton, need_files) -> int:
        """Version available in the backup folder, checked only when our own copy is behind."""
        v = (baton or {}).get("version", 0)
        behind = self.local_version() < v or need_files
        return v if behind and self.backup_has(v) else 0

    # hosting
    def start_hosting(self, player, address) -> None:
        if self.cloud:
            try:
                self.cloud.refresh()  # decide on what the cloud has now, not up to five minutes ago
            except CloudError:
                pass  # offline: Syncthing and the backup folder still count
        baton = self.read_baton()
        need = self.status()["need_files"]
        backup_v = self._backup_version(baton, need)
        ok, reason = host_check(baton, self.st.my_id, self.local_version(), need, time.time(), backup_v)
        if not ok:
            raise ValueError(reason)
        if server_running(self.world_path):  # its log so far, and its changes, aren't part of a session
            raise ValueError("Stop the server in SquidServers first, then press Host now. Start it again once "
                             "you're the host.")
        # Pause first so nothing from this session leaks out until it is finished.
        self.st.set_folder(self.world_id, type="sendreceive", paused=True)
        if backup_v:
            try:
                self.restore_backup(backup_v)
            except Exception as e:
                self.st.set_folder(self.world_id, type="receiveonly", paused=False)
                raise ValueError(f"Couldn't load the backup: {e}")
        try:
            self.write_baton({**baton, "holder": self.st.my_id, "holder_name": player, "server_up": False,
                              "address": address.strip(), "heartbeat": time.time(), "since": time.time()}, cloud="now")
        except CloudError:
            pass  # friends who only see the cloud learn it from a later heartbeat
        self._request_file().unlink(missing_ok=True)  # got what was asked for
        self._skip_old_log()  # only report what happens from now on
        self._marks_file.unlink(missing_ok=True)
        self.watch_offline_edits(player)

    def heartbeat(self) -> None:
        baton = self.read_baton()
        if baton and baton.get("holder") == self.st.my_id:
            now = time.time()
            gap = now - baton.get("heartbeat", now)
            if gap > 120:  # WorldBaton was closed (or the PC asleep) mid-session; its logs are read again now
                self.add_activity(baton.get("holder_name", "?"), [(f"gap-{baton.get('heartbeat')}",
                                  f"⚠ WorldBaton wasn't running for {gap / 60:.0f} min while hosting.")])
            self.write_baton({**baton, "heartbeat": now})

    def stop_hosting(self, player, restored_from: int | None = None) -> str:
        """Share the session. Returns a warning for the player, or '' if all went well."""
        if server_running(self.world_path):
            raise ValueError("Stop the server in SquidServers first, then press Stop hosting.")
        baton = self.read_baton()
        if not baton or baton.get("holder") != self.st.my_id:
            raise ValueError("Someone else took over while you were hosting, so your session wasn't shared. "
                             "Use 'Discard my session' to get their world, or ask the group what to keep.")
        try:
            self.record_activity(player)  # the last lines, before the session is shared
            self.report_offline_edits(player)
        except Exception:
            pass  # never block sharing the world over the activity log
        v = baton.get("version", 0) + 1
        self._write_version(v)
        warning = ""
        if self.backup_dir:
            try:
                self.save_backup(v)  # before the baton says v exists, so nobody looks for a zip that isn't there
            except OSError as e:
                warning = f"Couldn't save to the backup folder ({e}). Friends can still get the world while you're online."
        cloud, zipped = self.cloud, None
        if self.keep_local:
            try:
                zipped = self.save_local_backup(v)
            except OSError as e:
                warning = (warning + "\n" if warning else "") + f"Couldn't save this PC's world backup ({e})."
        elif cloud:
            try:
                zipped = self._write_zip(self.app_dir / "upload", v, 1)
            except OSError as e:
                warning = (warning + "\n" if warning else "") + f"Couldn't zip the world for the cloud copy ({e})."
        entry = {"host": player, "start": baton.get("since", time.time()), "end": time.time(), "version": v}
        if restored_from:
            entry["restored_from"] = restored_from
        try:
            self._add_history(entry)
        except OSError:
            pass
        released = {"version": v, "holder": None, "last_host": self.st.my_id, "last_host_name": player}
        self.write_baton(released, cloud="skip")  # the cloud only hears of v once it holds v
        self.st.set_folder(self.world_id, type="sendreceive", paused=False)
        self.st.scan(self.world_id)
        problem = None
        if cloud and zipped:
            try:
                cloud.upload(v, zipped, self._progress("Uploading the world"))
            except CloudError as e:
                problem = e
            finally:
                self.transfer = None
                if not self.keep_local:
                    zipped.unlink(missing_ok=True)
        if cloud:
            try:  # even without the zip: friends who only see the cloud then wait for v instead of hosting an old world
                cloud.write_baton(released, now=True)
            except CloudError as e:
                problem = problem or e
            if problem or not zipped:
                warning = (warning + "\n" if warning else "") + (
                    f"Couldn't upload to the cloud copy: {problem or 'no zip'}\n"
                    "Friends can still get the world from you while you're online.")
        return warning

    def leave(self) -> None:
        """Stop being part of this group on this PC. The server folder and its world stay on disk."""
        baton = self.read_baton()
        if baton and baton.get("holder") == self.st.my_id:
            raise ValueError("You're hosting this group. Press Stop hosting first.")
        self.cancel_request()
        for folder_id in (self.world_id, self.baton_id):
            try:
                self.st.remove_folder(folder_id)
            except Exception:
                pass  # already gone
        shutil.rmtree(self.baton_file.parent, ignore_errors=True)  # this PC's copy of the shared baton files
        for p in (self._marks_file, self._seen_file):
            p.unlink(missing_ok=True)

    def discard_session(self) -> None:
        if server_running(self.world_path):
            raise ValueError("Stop the server in SquidServers first.")
        self.st.set_folder(self.world_id, type="receiveonly", paused=False)
        time.sleep(1)
        self.st.revert(self.world_id)
