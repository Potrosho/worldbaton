"""End-to-end: two Syncthing instances on this PC act as two friends passing the world back and forth."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from worldbaton import core
from worldbaton.syncthing import Syncthing, bundled_exe, free_port


def wait_for(check, what, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if check():
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise AssertionError(f"Timed out waiting for: {what}")


def local_only(st: Syncthing) -> str:
    port = free_port()
    st.patch("/rest/config/options", {"globalAnnounceEnabled": False, "localAnnounceEnabled": False,
                                      "relaysEnabled": False, "natEnabled": False,
                                      "listenAddresses": [f"tcp://127.0.0.1:{port}"]})
    return f"tcp://127.0.0.1:{port}"


@unittest.skipUnless(bundled_exe().exists(), "run scripts/get_syncthing.py first")
class TwoFriends(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="worldbaton-test-"))
        self.a, self.b = Syncthing(self.tmp / "a-st"), Syncthing(self.tmp / "b-st")
        self.a.start()
        self.b.start()

    def tearDown(self):
        self.a.stop()
        self.b.stop()

    def test_pass_the_world(self):
        a, b = self.a, self.b
        addr_a, addr_b = local_only(a), local_only(b)

        server = self.tmp / "alex-server"
        (server / "world").mkdir(parents=True)
        (server / "logs").mkdir()
        (server / "world" / "level.dat").write_text("day 1")
        (server / "server.properties").write_text("motd=hi")
        (server / "logs" / "latest.log").write_text("alex only")
        (server / ".mc_server_state.json").write_text(json.dumps({"status": "offline", "pid": 1}))
        (server / "run.bat").write_text(r'"C:\Users\alex\java.exe" @user_jvm_args.txt')
        (server / "user_jvm_args.txt").write_text("-Xmx10G")

        ga = core.Group.create(a, self.tmp / "a-app", "Big P", server, "Alex")
        invite = core.decode_invite(ga.invite_code("Alex"))
        gb = core.Group.join(b, self.tmp / "b-app", invite, self.tmp / "sam-servers")
        b.add_device(a.my_id, "Alex", introducer=True, addresses=[addr_a])
        ga.add_member(core.decode_join(core.encode_join(b.my_id, "Sam")))
        a.add_device(b.my_id, "Sam", addresses=[addr_b])

        sam_world = gb.world_path
        wait_for(lambda: gb.local_version() == 1 and gb.status()["need_files"] == 0
                 and (sam_world / "world" / "level.dat").read_text() == "day 1", "Sam gets the world")
        self.assertFalse((sam_world / "logs").exists(), "logs are per-PC and must not sync")
        self.assertFalse((sam_world / ".mc_server_state.json").exists(), "SquidServers state must not sync")
        self.assertFalse((sam_world / "run.bat").exists(), "start scripts hold Alex's Java path")
        self.assertFalse((sam_world / "user_jvm_args.txt").exists(), "RAM setting is per-PC")
        wait_for(lambda: gb.read_baton() and gb.read_baton()["last_host"] == a.my_id, "Sam gets the baton")

        # Sam hosts; Alex must see it and be blocked.
        gb.reconcile()
        gb.start_hosting("Sam", "sam.example:25565")
        wait_for(lambda: ga.read_baton()["holder"] == b.my_id, "Alex sees Sam hosting")
        ga.reconcile()
        self.assertFalse(ga.status()["can_host"])
        self.assertEqual(a.folder(ga.world_id)["type"], "receiveonly")

        # Mid-session changes stay on Sam's PC.
        (sam_world / "world" / "level.dat").write_text("day 2")
        time.sleep(5)
        self.assertEqual((server / "world" / "level.dat").read_text(), "day 1")

        # Sam stops; Alex gets day 2 and may host.
        gb.stop_hosting("Sam")
        wait_for(lambda: ga.local_version() == 2 and (server / "world" / "level.dat").read_text() == "day 2",
                 "Alex gets Sam's session")
        wait_for(lambda: ga.status()["can_host"], "Alex may host again")

        # Alex's stray local edit (not hosting) is thrown away, not spread.
        ga.reconcile()
        (server / "server.properties").write_text("motd=oops")
        a.scan(ga.world_id)
        wait_for(lambda: (ga.reconcile() or True) and (server / "server.properties").read_text() == "motd=hi",
                 "Alex's stray edit is reverted")
        self.assertEqual((sam_world / "server.properties").read_text(), "motd=hi")

        # Sam makes a server in SquidServers and switches WorldBaton to it.
        squid = self.tmp / "sam-squidservers" / "Big P (shared)"
        (squid / "world").mkdir(parents=True)
        (squid / "squidservers").mkdir()
        (squid / "world" / "level.dat").write_text("fresh default world")
        (squid / "run.bat").write_text(r'"C:\Users\sam\java.exe" @user_jvm_args.txt')
        (squid / "user_jvm_args.txt").write_text("-Xmx4G")
        (squid / "squidservers" / "scheduled-tasks.json").write_text("[]")
        gb.move_world(squid)
        self.assertEqual(gb.world_path, squid)
        self.assertEqual((squid / "world" / "level.dat").read_text(), "day 2")
        self.assertEqual((squid / "run.bat").read_text(), r'"C:\Users\sam\java.exe" @user_jvm_args.txt')
        self.assertEqual((squid / "user_jvm_args.txt").read_text(), "-Xmx4G")
        self.assertTrue((squid / "squidservers" / "scheduled-tasks.json").exists())
        self.assertEqual(b.folder(gb.world_id)["path"], str(squid))

        # Syncing carries on into the new folder.
        wait_for(lambda: ga.status()["can_host"], "Alex may host")
        ga.start_hosting("Alex", "alex.example")
        (server / "world" / "level.dat").write_text("day 3")
        ga.stop_hosting("Alex")
        wait_for(lambda: (gb.reconcile() or True) and gb.local_version() == 3
                 and (squid / "world" / "level.dat").read_text() == "day 3", "Sam's new folder gets day 3")
        self.assertEqual((squid / "user_jvm_args.txt").read_text(), "-Xmx4G")

    def test_playing_without_the_baton_is_undone(self):
        """Alex (last host) closes WorldBaton, runs the server anyway, then reopens WorldBaton."""
        a, b = self.a, self.b
        addr_a, addr_b = local_only(a), local_only(b)
        server = self.tmp / "alex-server"
        (server / "world").mkdir(parents=True)
        (server / "world" / "level.dat").write_text("day 1")
        ga = core.Group.create(a, self.tmp / "a-app", "Big P", server, "Alex")
        gb = core.Group.join(b, self.tmp / "b-app", core.decode_invite(ga.invite_code("Alex")), self.tmp / "sam-servers")
        b.add_device(a.my_id, "Alex", introducer=True, addresses=[addr_a])
        ga.add_member(core.decode_join(core.encode_join(b.my_id, "Sam")))
        a.add_device(b.my_id, "Sam", addresses=[addr_b])
        level_sam = gb.world_path / "world" / "level.dat"
        wait_for(lambda: level_sam.read_text() == "day 1", "Sam gets the world")

        ga.park()  # Alex closes WorldBaton
        self.assertEqual(a.folder(ga.world_id)["type"], "receiveonly")
        (server / "world" / "level.dat").write_text("rogue session")
        a.scan(ga.world_id)
        time.sleep(3)
        self.assertEqual(level_sam.read_text(), "day 1", "a parked PC must not send")

        # Alex reopens WorldBaton: the rogue change is undone, then Alex is the sender again.
        wait_for(lambda: (ga.reconcile() or True) and (server / "world" / "level.dat").read_text() == "day 1",
                 "rogue session undone")
        wait_for(lambda: (ga.reconcile() or True) and a.folder(ga.world_id)["type"] == "sendreceive",
                 "Alex can send again")
        self.assertEqual(level_sam.read_text(), "day 1")

    def test_never_online_together(self):
        """Alex and Sam are never connected; the world travels only through the shared backup folder."""
        a, b = self.a, self.b
        backup = self.tmp / "google-drive" / "Big P backup"  # stands in for a shared cloud-drive folder
        server = self.tmp / "alex-server"
        (server / "world").mkdir(parents=True)
        (server / "logs").mkdir()
        (server / "world" / "level.dat").write_text("day 1")
        (server / "logs" / "latest.log").write_text("alex only")

        ga = core.Group.create(a, self.tmp / "a-app", "Big P", server, "Alex")
        ga.set_backup_dir(backup)
        gb = core.Group.join(b, self.tmp / "b-app", core.decode_invite(ga.invite_code("Alex")), self.tmp / "sam-servers")
        gb.set_backup_dir(backup)
        sam_world = gb.world_path
        (sam_world / "old-mod.jar").write_text("stale")  # must be removed by the restore
        (sam_world / "logs").mkdir()
        (sam_world / "logs" / "latest.log").write_text("sam only")  # per-PC, must survive

        # Alex plays a session and stops. Nothing has reached Sam yet, and v1 was never backed up.
        ga.start_hosting("Alex", "alex.example")
        (server / "world" / "level.dat").write_text("day 2")
        self.assertEqual(ga.stop_hosting("Alex"), "")
        self.assertTrue(ga.backup_has(2))

        # Sam comes online later: allowed to host because the backup has version 2.
        s = gb.status()
        self.assertTrue(s["can_host"], s["reason"])
        self.assertIn("backup folder", s["reason"])
        gb.start_hosting("Sam", "sam.example")
        self.assertEqual((sam_world / "world" / "level.dat").read_text(), "day 2")
        self.assertEqual(gb.local_version(), 2)
        self.assertFalse((sam_world / "old-mod.jar").exists())
        self.assertEqual((sam_world / "logs" / "latest.log").read_text(), "sam only")

        # Alex sees Sam hosting through the backup folder alone.
        self.assertEqual(ga.read_baton()["holder"], b.my_id)
        self.assertFalse(ga.status()["can_host"])

        # Sam stops; Alex's copy (v2) is behind v3, so Alex loads it from the backup.
        (sam_world / "world" / "level.dat").write_text("day 3")
        gb.stop_hosting("Sam")
        ga.reconcile()
        self.assertTrue(ga.status()["can_host"])
        ga.start_hosting("Alex", "alex.example")
        self.assertEqual((server / "world" / "level.dat").read_text(), "day 3")
        self.assertEqual((server / "logs" / "latest.log").read_text(), "alex only")
        for v in range(4, 7):  # only the newest few zips are kept
            ga.stop_hosting("Alex")
            ga.start_hosting("Alex", "alex.example")
        self.assertEqual(sorted(p.name for p in backup.glob("*.zip")),
                         [f"{ga.world_id}-v{v}.zip" for v in (4, 5, 6)])


if __name__ == "__main__":
    unittest.main()
