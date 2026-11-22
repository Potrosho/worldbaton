"""The abuse catcher: admin commands, mod/setting changes, and the ways a host might try to hide them."""
import gzip
import json
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from worldbaton import core

NEO = "[20Sep2026 12:09:54.627] [Server thread/INFO] [net.minecraft.server.MinecraftServer/]: "
MODS = r"C:\Servers\Big P\mods"


class FakeSyncthing:
    def __init__(self, my_id="ABCDEFG-HIJ", modified_by=None):
        self.my_id, self.modified_by = my_id, modified_by or {}

    def scan(self, folder_id):
        pass

    def devices(self):
        return {"ABCDEFG-HIJ": "Sam", "ZZZZZZZ-XYZ": "Alex"}

    def get(self, path):
        name = path.rsplit("file=", 1)[1]
        return {"global": {"modifiedBy": self.modified_by.get(name, name[len("activity-"):][:7])}}


def make_group(d: Path, st=None) -> core.Group:
    (d / "world" / "logs").mkdir(parents=True, exist_ok=True)
    g = core.Group(st or FakeSyncthing(), {"world_id": "w", "baton_id": "b", "world_path": str(d / "world")}, d / "app")
    g.baton_file.parent.mkdir(parents=True, exist_ok=True)
    return g


class Lines(unittest.TestCase):
    def test_admin_commands(self):
        self.assertEqual(core.admin_line(NEO + "[Potrosh0: Gave 34 [Diamond] to ThebestWill686]\n"),
                         ("20Sep2026 12:09:54.627", "Potrosh0: Gave 34 [Diamond] to ThebestWill686"))
        self.assertEqual(core.admin_line(NEO + "Made Potrosh0 a server operator")[1], "Console: Made Potrosh0 a server operator")
        self.assertEqual(core.admin_line("[11:57:08] [Server thread/INFO]: [Sam: Set own game mode to Creative Mode]")[1],
                         "Sam: Set own game mode to Creative Mode")
        self.assertEqual(core.admin_line(NEO + "[@: Gave 64 [Diamond] to Sam]")[1], "@: Gave 64 [Diamond] to Sam")  # command block
        self.assertEqual(core.admin_line(NEO + "[Rcon: Made Sam a server operator]")[1], "Rcon: Made Sam a server operator")
        self.assertTrue(core.admin_line(NEO + "Gamerule logAdminCommands is now set to: false")[1].startswith("Console"))
        for ignored in (NEO + "Potrosh0 joined the game", NEO + "<Sam> [Sam: Gave 1 [Diamond] to Sam]",
                        NEO + "[Server] hi", "[20Sep2026 12:09:54.627] [Worker/INFO] [x/]: Gave 1 thing", "garbage"):
            self.assertIsNone(core.admin_line(ignored), ignored)

    def test_mods_loaded_from_elsewhere(self):
        line = '[20Sep2026 10:53:47.187] [main/INFO] [net.neoforged.fml.loading.moddiscovery.ModDiscoverer/SCAN]: ' \
               'Found mod file "{jar}" [locator: {loc}, reader: mod manifest]'
        normal = [("create.jar", "{mods folder locator at " + MODS + "}"), ("inner.jar", "jarinjar"),
                  ("server.jar", "production server provider +net.neoforged:neoforge:21.1.250:server")]
        for jar, loc in normal:
            self.assertIsNone(core.odd_mod_line(line.format(jar=jar, loc=loc), Path(MODS)), jar)
        hit = core.odd_mod_line(line.format(jar="cheat.jar", loc=r"{mods folder locator at C:\Temp\hidden}"), Path(MODS))
        self.assertIn("cheat.jar", hit[1])
        self.assertIsNotNone(core.odd_mod_line(line.format(jar="x.jar", loc="SomeOtherLocator"), Path(MODS)))


class Snapshots(unittest.TestCase):
    def test_changes_and_warnings(self):
        with tempfile.TemporaryDirectory() as d:
            w = Path(d)
            empty = core.server_snapshot(w)
            self.assertEqual(empty["mods"], [])
            (w / "mods").mkdir()
            (w / "mods" / "create.jar").write_text("")
            (w / "mods" / "notes.txt").write_text("")
            (w / "ops.json").write_text(json.dumps([{"name": "Sam"}, {"name": "Sam"}]))
            (w / "server.properties").write_text("#c\nenable-rcon=true\nonline-mode=false\nserver-port=25565\nlevel-name=world\n")
            (w / "world" / "datapacks" / "cheats").mkdir(parents=True)
            nbt = b"\x08\x00\x10logAdminCommands\x00\x05false\x08\x00\x12commandBlockOutput\x00\x04true"
            with gzip.open(w / "world" / "level.dat", "wb") as f:
                f.write(b"\x0a\x00\x00junk" + nbt + b"\x00")
            snap = core.server_snapshot(w)
        self.assertEqual(snap["ops"], ["Sam"])
        self.assertEqual(snap["settings"], {"enable-rcon": "true", "online-mode": "false", "level-name": "world"})  # port isn't watched
        self.assertEqual(snap["rules"], {"logAdminCommands": "false", "commandBlockOutput": "true"})
        self.assertEqual(core.snapshot_changes(empty, snap), [
            "New mod: create.jar", "Now an operator: Sam", "New datapack: world/datapacks/cheats",
            "Server setting enable-rcon: (not set) → true", "Server setting level-name: (not set) → world",
            "Server setting online-mode: (not set) → false",
            "Gamerule commandBlockOutput: (not set) → true", "Gamerule logAdminCommands: (not set) → false"])
        self.assertEqual(core.snapshot_changes(snap, snap), [])
        self.assertEqual(core.snapshot_changes({"mods": []}, snap), ["New mod: create.jar"])  # older snapshots: no noise
        warnings = " ".join(core.watch_warnings(snap))
        self.assertIn("logAdminCommands", warnings)
        self.assertIn("RCON", warnings)
        self.assertIn("online-mode", warnings)


class Hosting(unittest.TestCase):
    def test_only_new_lines_and_restarts(self):
        with tempfile.TemporaryDirectory() as d:
            g = make_group(Path(d))
            log = g.world_path / "logs" / "latest.log"
            log.write_text(NEO + "[Sam: Gave 1 [Diamond] to Sam]\n")
            g._skip_old_log()  # what start_hosting does: earlier lines aren't this session
            with open(log, "a") as f:
                f.write(NEO + "Made Sam a server operator\n" + NEO + "Sam joined the game\n" + NEO + "[Sam: Kil")
            g.record_activity("Sam")
            self.assertEqual([e["text"] for e in g.activity()], ["Console: Made Sam a server operator"])
            with open(log, "a") as f:
                f.write("led Bob]\n")  # the half-written line is picked up once finished
            g._logs = {}  # WorldBaton restarted mid-session: rereads the log without duplicates
            g.record_activity("Sam")
            texts = sorted(e["text"] for e in g.activity())
        self.assertEqual(texts, ["Console: Made Sam a server operator", "Sam: Gave 1 [Diamond] to Sam", "Sam: Killed Bob"])

    def test_edited_log(self):
        with tempfile.TemporaryDirectory() as d:
            g = make_group(Path(d))
            log = g.world_path / "logs" / "latest.log"
            log.write_bytes((NEO + "Sam joined the game\n" + NEO + "[Sam: Gave 64 [Diamond] to Sam]\n").encode())
            g._skip_old_log()
            # The host rewrites the line in place, same length, hoping nobody notices.
            log.write_bytes(log.read_bytes().replace(b"[Sam: Gave 64 [Diamond] to Sam]", b"Sam said hello to everyone!!!!"))
            g.record_activity("Sam")
            self.assertIn("was edited or cut", " ".join(e["text"] for e in g.activity()))
            log.write_bytes(NEO[:20].encode())  # cut
            self.assertIn("was edited or cut", g._new_log_lines("latest.log")[1])
            with open(log, "ab") as f:
                f.write(b"\n" + NEO.encode() + b"Sam left the game\n")  # normal growth afterwards: no warning
            self.assertEqual(g._new_log_lines("latest.log"), ([NEO[:20], NEO + "Sam left the game"], None))

    def test_server_starts_a_new_log(self):
        """The normal flow: Host now, then start the server. Minecraft moves latest.log aside and starts a new one;
        on Windows the new file even keeps the old one's creation time. That's not an edit."""
        with tempfile.TemporaryDirectory() as d:
            g = make_group(Path(d))
            log = g.world_path / "logs" / "latest.log"
            log.write_text((NEO + "Sam joined the game\n") * 50)  # the last session's log
            g._skip_old_log()  # Host now
            time.sleep(0.05)
            log.rename(log.with_name("2026-11-19-1.log"))
            log.write_text(NEO + "Made Sam a server operator\n")
            g.record_activity("Sam")
            texts = [e["text"] for e in g.activity()]
        self.assertEqual(texts, ["Console: Made Sam a server operator"])

    def test_log_rolled_at_midnight(self):
        """The server keeps running past midnight: Minecraft gzips latest.log into logs/<date>-1.log.gz and starts a
        new one. Not a warning, and lines written just before midnight still count. A swapped-in log still warns."""
        with tempfile.TemporaryDirectory() as d, mock.patch.object(core, "server_pid", return_value=4242):
            g = make_group(Path(d))
            log = g.world_path / "logs" / "latest.log"
            log.write_text(NEO + "Sam joined the game\n")
            g._skip_old_log()
            with open(log, "a") as f:
                f.write(NEO + "Made Sam a server operator\n")  # 23:59:58, not read yet
            old = log.read_bytes()
            log.unlink()
            log.write_text(NEO + "[Sam: Gave 1 [Diamond] to Sam]\n")
            self.assertEqual(g._new_log_lines("latest.log"), ([], None))  # not compressed yet: wait
            (log.parent / "2026-11-18-1.log.gz").write_bytes(gzip.compress(old))
            g.record_activity("Sam")
            self.assertEqual(sorted(e["text"] for e in g.activity()),
                             ["Console: Made Sam a server operator", "Sam: Gave 1 [Diamond] to Sam"])
            log.unlink()  # now a log that isn't the one we were reading
            log.write_text(NEO + "Sam left the game\n")
            g._logs["latest.log"]["rolled_at"] = time.time() - 200
            self.assertIn("was replaced", g._new_log_lines("latest.log")[1])

    def test_log_copied_and_emptied_at_midnight(self):
        """When another program (SquidServers' console) has latest.log open, Minecraft can't move it aside at
        midnight: it copies it to the .gz and empties the same file. Same file, now shorter: not an edit."""
        with tempfile.TemporaryDirectory() as d, mock.patch.object(core, "server_pid", return_value=4242):
            g = make_group(Path(d))
            log = g.world_path / "logs" / "latest.log"
            log.write_text((NEO + "Sam joined the game\n") * 20)
            g._skip_old_log()
            with open(log, "a") as f:
                f.write(NEO + "Made Sam a server operator\n")
            (log.parent / "2026-11-18-1.log.gz").write_bytes(gzip.compress(log.read_bytes()))
            with open(log, "r+b") as f:  # emptied in place, then the new day's lines
                f.truncate(0)
                f.write((NEO + "[Sam: Gave 1 [Diamond] to Sam]\n").encode())
            g.record_activity("Sam")
            self.assertEqual(sorted(e["text"] for e in g.activity()),
                             ["Console: Made Sam a server operator", "Sam: Gave 1 [Diamond] to Sam"])
            with open(log, "r+b") as f:  # cut with no matching old log: still caught, after the grace period
                f.truncate(10)
            self.assertEqual(g._new_log_lines("latest.log"), ([], None))
            g._logs["latest.log"]["rolled_at"] = time.time() - 200
            self.assertIn("was edited or cut", g._new_log_lines("latest.log")[1])

    def test_files_edited_while_server_off(self):
        with tempfile.TemporaryDirectory() as d:
            g = make_group(Path(d))
            w = g.world_path
            (w / "world" / "playerdata").mkdir(parents=True)
            uuid = "dba74f26-bc9c-34e4-80c1-d4c0a1737d1e"
            (w / "world" / "playerdata" / f"{uuid}.dat").write_bytes(b"old")
            (w / "usercache.json").write_text(json.dumps([{"name": "Potrosh0", "uuid": uuid}]))
            (w / "server.properties").write_text("server-port=1\n")
            g.watch_offline_edits("Sam")  # server off: remember the files
            (w / "world" / "playerdata" / f"{uuid}.dat").write_bytes(b"64 diamonds")  # NBT editor
            (w / "server.properties").write_text("server-port=2\n")  # SquidServers does this itself: not reported
            (w / "logs" / "latest.log").write_text("x")  # per-PC: not reported
            g.report_offline_edits("Sam")  # what the next start (or Stop hosting) does
            texts = [e["text"] for e in g.activity()]
            self.assertFalse(g._marks_file.exists())
        self.assertEqual(texts, [f"⚠ Files changed while the server was off, not by the game: "
                                 f"world/playerdata/{uuid}.dat (Potrosh0)"])

    def test_level_dat_compared_by_its_settings(self):
        """SquidServers rewrites level.dat before each start: only a changed setting is reported, by name."""
        def level_dat(difficulty, time_of_day):
            def s(text):
                return len(text).to_bytes(2, "big") + text.encode()
            data = (b"\x0a" + s("") + b"\x0a" + s("Data") + b"\x01" + s("Difficulty") + bytes([difficulty]) +
                    b"\x04" + s("Time") + time_of_day.to_bytes(8, "big") +
                    b"\x0a" + s("GameRules") + b"\x08" + s("keepInventory") + s("false") + b"\x00" + b"\x00\x00")
            return gzip.compress(data)

        with tempfile.TemporaryDirectory() as d, mock.patch.object(core, "server_pid", return_value=None):
            g = make_group(Path(d))
            (g.world_path / "world").mkdir()
            level = g.world_path / "world" / "level.dat"
            level.write_bytes(level_dat(2, 1000))
            g.watch_offline_edits("Sam")
            level.write_bytes(level_dat(2, 1001) + b"")  # rewritten, same settings
            g.report_offline_edits("Sam")
            self.assertEqual(g.activity(), [])
            g.watch_offline_edits("Sam")
            level.write_bytes(level_dat(0, 1002))  # peaceful
            g.report_offline_edits("Sam")
            self.assertIn("world/level.dat (Difficulty: 2 → 0)", g.activity()[0]["text"])

    def test_offline_edits_reported_once_when_syncthing_fails(self):
        class Failing(FakeSyncthing):
            def scan(self, folder_id):
                raise OSError("insufficient space on disk")

        with tempfile.TemporaryDirectory() as d:
            g = make_group(Path(d), Failing())
            with mock.patch.object(core, "server_pid", return_value=None):
                g.watch_offline_edits("Sam")
            (g.world_path / "mods").mkdir()
            (g.world_path / "mods" / "x.jar").write_text("new")
            with mock.patch.object(core, "server_pid", return_value=4242):
                g.watch_offline_edits("Sam")
                g.watch_offline_edits("Sam")  # the next refresh: nothing new to report
            self.assertEqual(len(g.activity()), 1)

    def test_closed_while_hosting(self):
        with tempfile.TemporaryDirectory() as d:
            g = make_group(Path(d))
            g.write_baton({"version": 1, "holder": g.st.my_id, "holder_name": "Sam", "heartbeat": time.time() - 600})
            g.heartbeat()
            self.assertIn("wasn't running for 10 min", g.activity()[0]["text"])


class SharedList(unittest.TestCase):
    def test_deleted_entries_stay_visible(self):
        with tempfile.TemporaryDirectory() as d:
            g = make_group(Path(d))
            g.add_activity("Sam", [("1", "Sam: Gave 64 [Diamond] to Sam"), ("2", "Sam: Set own game mode to Creative Mode")])
            self.assertEqual(len(g.activity()), 2)  # this PC has now seen both
            entries = g.read_activity_file(g.activity_file)
            g.activity_file.write_text(json.dumps(entries[1]) + "\n")  # the host deletes the /give
            shown = {e["text"]: e.get("removed", False) for e in g.activity()}
        self.assertEqual(shown, {"Sam: Gave 64 [Diamond] to Sam": True, "Sam: Set own game mode to Creative Mode": False})

    def test_list_changed_by_someone_else(self):
        with tempfile.TemporaryDirectory() as d:
            st = FakeSyncthing(modified_by={"activity-ABCDEFG.jsonl": "ZZZZZZZ"})
            g = make_group(Path(d), st)
            g.add_activity("Sam", [("1", "Sam: Gave 1 [Diamond] to Sam")])
            texts = [e["text"] for e in g.activity()]
        self.assertIn("⚠ Sam's activity list was last changed by Alex (ZZZZZZZ).", texts)


if __name__ == "__main__":
    unittest.main()
