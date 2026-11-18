import unittest

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


if __name__ == "__main__":
    unittest.main()
