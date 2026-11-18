import json
import tempfile
import unittest
from pathlib import Path

from worldbaton import core

ME, YOU = "ME-ID", "YOU-ID"


class Rules(unittest.TestCase):
    def test_codes_round_trip(self):
        code = core.encode_invite("A", "Alex", "Big P", "wb-world-1", "wb-baton-1")
        self.assertEqual(core.decode_invite(code)["g"], "Big P")
        self.assertEqual(core.decode_join(core.encode_join("B", "Sam"))["n"], "Sam")
        with self.assertRaises(ValueError):
            core.decode_invite(core.encode_join("B", "Sam"))
        with self.assertRaises(ValueError):
            core.decode_invite("WBI1-%%%")

    def test_world_mode(self):
        self.assertEqual(core.desired_world_mode(None, ME), ("receiveonly", False))
        self.assertEqual(core.desired_world_mode({"holder": ME}, ME), ("sendreceive", True))
        self.assertEqual(core.desired_world_mode({"holder": None, "last_host": ME}, ME), ("sendreceive", False))
        self.assertEqual(core.desired_world_mode({"holder": YOU, "last_host": ME}, ME), ("receiveonly", False))

    def test_host_check(self):
        now = 10_000
        free = {"version": 3, "holder": None, "last_host": YOU}
        self.assertTrue(core.host_check(free, ME, 3, 0, now)[0])
        self.assertFalse(core.host_check(free, ME, 2, 0, now)[0], "older copy must not host")
        self.assertFalse(core.host_check(free, ME, 3, 5, now)[0], "still downloading")
        busy = {**free, "holder": YOU, "holder_name": "Sam", "heartbeat": now - 10}
        self.assertFalse(core.host_check(busy, ME, 3, 0, now)[0])
        stale = {**busy, "heartbeat": now - core.STALE_AFTER - 1}
        ok, reason = core.host_check(stale, ME, 3, 0, now)
        self.assertTrue(ok)
        self.assertIn("stopped responding", reason)
        self.assertFalse(core.host_check(None, ME, 0, 0, now)[0])

    def test_host_check_with_backup(self):
        free = {"version": 5, "holder": None}
        self.assertTrue(core.host_check(free, ME, 4, 0, 0, backup_version=5)[0])
        self.assertFalse(core.host_check(free, ME, 4, 0, 0, backup_version=4)[0], "backup older than needed")

    def test_newest_baton(self):
        released = {"version": 5, "holder": None}
        hosting = {"version": 5, "holder": YOU, "heartbeat": 100}
        takeover = {"version": 5, "holder": ME, "heartbeat": 500}
        next_release = {"version": 6, "holder": None}
        self.assertIs(core.newest_baton(released, hosting), hosting)
        self.assertIs(core.newest_baton(hosting, takeover), takeover)
        self.assertIs(core.newest_baton(takeover, next_release, None), next_release)
        self.assertIsNone(core.newest_baton(None, None))

    def test_skipped(self):
        for rel in ("logs/latest.log", ".mc_server_state.json", "squidservers/scheduled-tasks.json",
                    "world/session.lock", "world/region/r.0.0.mca.sync-conflict-x", ".stversions/a"):
            self.assertTrue(core.skipped(rel), rel)
        for rel in ("world/level.dat", "mods/a.jar", "server.properties", core.VERSION_FILE):
            self.assertFalse(core.skipped(rel), rel)

    def test_safe_folder_name(self):
        self.assertEqual(core.safe_folder_name('a<b>:c?.'), "abc")
        self.assertEqual(core.safe_folder_name("..."), "WorldBaton world")


NEO = "[20Sep2026 12:09:54.627] [Server thread/INFO] [net.minecraft.server.MinecraftServer/]: "


class FakeSyncthing:
    my_id = "ABCDEFG-HIJ"

    def scan(self, folder_id):
        pass


class Watching(unittest.TestCase):
    def test_admin_lines(self):
        self.assertEqual(core.admin_line(NEO + "[Potrosh0: Gave 34 [Diamond] to ThebestWill686]\n"),
                         ("20Sep2026 12:09:54.627", "Potrosh0: Gave 34 [Diamond] to ThebestWill686"))
        self.assertEqual(core.admin_line(NEO + "Made Potrosh0 a server operator")[1], "Console: Made Potrosh0 a server operator")
        self.assertEqual(core.admin_line("[11:57:08] [Server thread/INFO]: [Sam: Set own game mode to Creative Mode]")[1],
                         "Sam: Set own game mode to Creative Mode")
        for ignored in (NEO + "Potrosh0 joined the game", NEO + "<Sam> [Sam: Gave 1 [Diamond] to Sam]",
                        NEO + "[Server] hi", "[20Sep2026 12:09:54.627] [Worker/INFO] [x/]: Gave 1 thing", "garbage"):
            self.assertIsNone(core.admin_line(ignored), ignored)

    def test_snapshot_changes(self):
        with tempfile.TemporaryDirectory() as d:
            w = Path(d)
            self.assertEqual(core.server_snapshot(w), {"mods": [], "ops": []})
            (w / "mods").mkdir()
            (w / "mods" / "create.jar").write_text("")
            (w / "mods" / "notes.txt").write_text("")
            (w / "ops.json").write_text(json.dumps([{"name": "Sam", "level": 4}]))
            snap = core.server_snapshot(w)
        self.assertEqual(snap, {"mods": ["create.jar"], "ops": ["Sam"]})
        self.assertEqual(core.snapshot_changes({"mods": ["jei.jar"], "ops": []}, snap),
                         ["New mod: create.jar", "Mod removed: jei.jar", "Now an operator: Sam"])
        self.assertEqual(core.snapshot_changes(snap, snap), [])

    def test_record_activity_only_new_lines(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "world" / "logs").mkdir(parents=True)
            log = d / "world" / "logs" / "latest.log"
            log.write_text(NEO + "[Sam: Gave 1 [Diamond] to Sam]\n")
            g = core.Group(FakeSyncthing(), {"world_id": "w", "baton_id": "b", "world_path": str(d / "world")}, d)
            g.baton_file.parent.mkdir(parents=True)
            g._skip_old_log()  # what start_hosting does: earlier lines aren't this session
            with open(log, "a") as f:
                f.write(NEO + "Made Sam a server operator\n" + NEO + "Sam joined the game\n" + NEO + "[Sam: Kil")
            g.record_activity("Sam")
            self.assertEqual([e["text"] for e in g.activity()], ["Console: Made Sam a server operator"])
            with open(log, "a") as f:
                f.write("led Bob]\n")  # the half-written line is picked up once finished
            g._log_at = None  # WorldBaton restarted mid-session: rereads the log without duplicates
            g.record_activity("Sam")
            texts = sorted(e["text"] for e in g.activity())
        self.assertEqual(texts, ["Console: Made Sam a server operator", "Sam: Gave 1 [Diamond] to Sam", "Sam: Killed Bob"])


if __name__ == "__main__":
    unittest.main()
