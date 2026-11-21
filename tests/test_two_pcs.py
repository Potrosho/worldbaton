"""End-to-end: two Syncthing instances on this PC act as two friends passing the world back and forth."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from tests.fake_s3 import FakeS3
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
                                      "relaysEnabled": False, "natEnabled": False, "reconnectionIntervalS": 2,
                                      "listenAddresses": [f"tcp://127.0.0.1:{port}"]})
    return f"tcp://127.0.0.1:{port}"


def join(owner_group, joiner, owner_addr, app_dir, servers, player="Sam", password="hunter22", **kw):
    """A friend joins with the group code and password; the owner's WorldBaton lets them in on its own."""
    invite = core.decode_invite(owner_group.invite_code("Alex"))
    g = core.Group.join(joiner, app_dir, invite, servers, password, player, **kw)
    joiner.patch(f"/rest/config/devices/{invite['o']}", {"addresses": [owner_addr]})  # no discovery in tests
    return g


def let_in(owner, groups, joiner):
    """Run the owner's WorldBaton check until the joining PC is in."""
    group = next(g for g in groups if g.world_id == joiner.world_id)
    wait_for(lambda: core.admit(owner, groups, {}) and joiner.st.my_id in group.member_ids(), "the owner lets them in")


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
        addr_a = local_only(a)
        local_only(b)

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
        ga.set_password("hunter22")
        gb = join(ga, b, addr_a, self.tmp / "b-app", self.tmp / "sam-servers", password="hunter2")  # typo
        refused = {}
        wait_for(lambda: core.admit(a, [ga], refused)[1] == ["Sam"], "Alex's WorldBaton turns away a wrong password")
        self.assertNotIn(b.my_id, a.devices())
        gb.set_join_password("hunter22", "Sam")
        self.assertEqual(b.devices()[b.my_id], f"Sam [wb:{gb.info['proof']}]")
        let_in(a, [ga], gb)
        self.assertEqual(a.devices()[b.my_id], "Sam", "the proof isn't part of the name friends see")

        sam_world = gb.world_path
        wait_for(lambda: gb.local_version() == 1 and gb.status()["need_files"] == 0
                 and (sam_world / "world" / "level.dat").read_text() == "day 1", "Sam gets the world")
        self.assertFalse((sam_world / "logs").exists(), "logs are per-PC and must not sync")
        self.assertFalse((sam_world / ".mc_server_state.json").exists(), "SquidServers state must not sync")
        self.assertFalse((sam_world / "run.bat").exists(), "start scripts hold Alex's Java path")
        self.assertFalse((sam_world / "user_jvm_args.txt").exists(), "RAM setting is per-PC")
        wait_for(lambda: gb.read_baton() and gb.read_baton()["last_host"] == a.my_id, "Sam gets the baton")
        gb.joined("Sam")
        self.assertEqual(b.devices()[b.my_id], "Sam")
        self.assertEqual(b.folder(gb.world_id)["label"], "Big P")

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

        # Same, but Sam is away: the group's server.properties can't come back yet. That mustn't stop Alex hosting.
        b.set_folder(gb.world_id, paused=True)
        (server / "server.properties").write_text("motd=squidservers rewrote this")
        a.scan(ga.world_id)
        wait_for(lambda: (ga.reconcile() or True) and a.folder_status(ga.world_id).get("needFiles") == 1,
                 "Alex's edit is undone and the group's copy is needed")
        s = ga.status()
        self.assertTrue(s["can_host"], s["reason"])
        b.set_folder(gb.world_id, paused=False)
        wait_for(lambda: (server / "server.properties").read_text() == "motd=hi", "the group's copy comes back")

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
        addr_a = local_only(a)
        local_only(b)
        server = self.tmp / "alex-server"
        (server / "world").mkdir(parents=True)
        (server / "world" / "level.dat").write_text("day 1")
        ga = core.Group.create(a, self.tmp / "a-app", "Big P", server, "Alex")
        ga.set_password("hunter22")
        gb = join(ga, b, addr_a, self.tmp / "b-app", self.tmp / "sam-servers")
        let_in(a, [ga], gb)
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

    def test_server_started_out_of_turn(self):
        """With WorldBaton open: Alex (last host) and Sam (behind) each start their server without the baton."""
        a, b = self.a, self.b
        addr_a = local_only(a)
        local_only(b)
        server = self.tmp / "alex-server"
        (server / "world").mkdir(parents=True)
        (server / "world" / "level.dat").write_text("day 1")
        ga = core.Group.create(a, self.tmp / "a-app", "Big P", server, "Alex")
        ga.set_password("hunter22")
        gb = join(ga, b, addr_a, self.tmp / "b-app", self.tmp / "sam-servers")
        let_in(a, [ga], gb)
        level_sam = gb.world_path / "world" / "level.dat"
        wait_for(lambda: level_sam.read_text() == "day 1" and gb.read_baton(), "Sam gets the world")

        def server_on(world, on):  # what SquidServers writes; this test's own process stands in for the server
            (world / ".mc_server_state.json").write_text(json.dumps({"status": "online" if on else "offline",
                                                                      "pid": os.getpid() if on else None}))

        # Alex, the last host, starts the server without pressing Host now.
        server_on(server, True)
        ga.reconcile()
        self.assertEqual((a.folder(ga.world_id)["type"], a.folder(ga.world_id)["paused"]), ("receiveonly", True))
        (server / "world" / "level.dat").write_text("half-saved rogue session")
        self.assertFalse(ga.status()["can_host"])
        with self.assertRaisesRegex(ValueError, "Stop the server"):
            ga.start_hosting("Alex", "alex.example")  # commands typed before Host now would be missed
        time.sleep(12)  # longer than Syncthing waits before sending a change
        self.assertEqual(level_sam.read_text(), "day 1", "nothing from a server running out of turn is sent")

        server_on(server, False)  # stopped: undone, then Alex sends again
        wait_for(lambda: (ga.reconcile() or True) and (server / "world" / "level.dat").read_text() == "day 1"
                 and a.folder(ga.world_id)["type"] == "sendreceive" and not a.folder(ga.world_id)["paused"],
                 "the rogue session is undone")
        wait_for(lambda: ga.status()["can_host"], "Alex can host again")

        # Sam starts a server while Alex hosts: the newer world must not land under it.
        server_on(gb.world_path, True)
        gb.reconcile()
        self.assertTrue(b.folder(gb.world_id)["paused"])
        ga.start_hosting("Alex", "alex.example")
        (server / "world" / "level.dat").write_text("day 2")
        ga.stop_hosting("Alex")
        time.sleep(5)
        self.assertEqual(level_sam.read_text(), "day 1")
        server_on(gb.world_path, False)
        wait_for(lambda: (gb.reconcile() or True) and level_sam.read_text() == "day 2", "Sam gets day 2 once stopped")

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
        gb = core.Group.join(b, self.tmp / "b-app", core.decode_invite(ga.invite_code("Alex")), self.tmp / "sam-servers",
                             "hunter22", "Sam")
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

    def test_never_online_together_cloud(self):
        """Alex and Sam are never connected; the world travels only through the group's S3 cloud copy."""
        fake = FakeS3()
        self.addCleanup(fake.stop)
        a, b = self.a, self.b
        server = self.tmp / "alex-server"
        (server / "world").mkdir(parents=True)
        (server / "logs").mkdir()
        (server / "world" / "level.dat").write_text("day 1")
        (server / "logs" / "latest.log").write_text("alex only")

        ga = core.Group.create(a, self.tmp / "a-app", "Big P", server, "Alex")
        ga.info["local_backups"] = 0  # the upload must not depend on this PC keeping its own copies
        ga.set_cloud(fake.settings())
        self.assertEqual(json.loads(fake.objects[f"{ga.world_id}-baton.json"][0])["version"], 1)
        gb = core.Group.join(b, self.tmp / "b-app", core.decode_invite(ga.invite_code("Alex")), self.tmp / "sam-servers",
                             "hunter22", "Sam")
        gb.set_group_setting("cloud", fake.settings())  # normally reaches Sam through Syncthing, like all group settings
        sam_world = gb.world_path
        (sam_world / "old-mod.jar").write_text("stale")
        (sam_world / "logs").mkdir()
        (sam_world / "logs" / "latest.log").write_text("sam only")

        ga.start_hosting("Alex", "alex.example")
        (server / "world" / "level.dat").write_text("day 2")
        self.assertEqual(ga.stop_hosting("Alex"), "")
        self.assertEqual(sorted(k for k in fake.objects if k.endswith(".zip")), [f"{ga.world_id}-v2.zip"])
        self.assertFalse(list((self.tmp / "a-app").rglob("*.zip")), "the upload's zip is cleaned up")
        self.assertIsNone(ga.transfer)

        # Sam comes online later: his WorldBaton looks at the cloud and may host from it.
        gb.cloud.refresh()
        s = gb.status()
        self.assertTrue(s["can_host"], s["reason"])
        self.assertIn("cloud copy", s["reason"])
        gb.start_hosting("Sam", "sam.example")
        self.assertEqual((sam_world / "world" / "level.dat").read_text(), "day 2")
        self.assertEqual(gb.local_version(), 2)
        self.assertFalse((sam_world / "old-mod.jar").exists())
        self.assertEqual((sam_world / "logs" / "latest.log").read_text(), "sam only")

        # Alex sees Sam hosting through the cloud alone.
        ga.cloud.refresh()
        self.assertEqual(ga.read_baton()["holder"], b.my_id)
        self.assertFalse(ga.status()["can_host"])

        # Sam stops; Alex's copy (v2) is behind v3, so Alex downloads v3.
        (sam_world / "world" / "level.dat").write_text("day 3")
        self.assertEqual(gb.stop_hosting("Sam"), "")
        ga.cloud.refresh()
        ga.reconcile()
        self.assertTrue(ga.status()["can_host"])
        ga.start_hosting("Alex", "alex.example")
        self.assertEqual((server / "world" / "level.dat").read_text(), "day 3")
        self.assertEqual((server / "logs" / "latest.log").read_text(), "alex only")
        self.assertEqual([b["where"] for b in ga.backups()], ["cloud", "cloud"], "History lists the cloud copies")
        for v in range(4, 7):  # only the newest few zips are kept
            ga.stop_hosting("Alex")
            ga.start_hosting("Alex", "alex.example")
        self.assertEqual(sorted(k for k in fake.objects if k.endswith(".zip")),
                         [f"{ga.world_id}-v{v}.zip" for v in (4, 5, 6)])

        # The cloud can't be reached: Stop hosting still shares with online friends, and says so.
        ga.set_group_setting("cloud", fake.settings(secret="changed"))
        warning = ga.stop_hosting("Alex")
        self.assertIn("secret key is wrong", warning)
        self.assertEqual(ga.read_baton()["holder"], None)

    def test_two_groups_on_one_pc(self):
        """Alex has two groups; Sam is only in one. Friends, folders and leaving stay per group."""
        a, b = self.a, self.b
        addr_a = local_only(a)
        local_only(b)
        servers = [self.tmp / "alex-modded", self.tmp / "alex-vanilla"]
        for s in servers:
            (s / "world").mkdir(parents=True)
            (s / "world" / "level.dat").write_text(s.name)
        modded = core.Group.create(a, self.tmp / "a-app", "Modded", servers[0], "Alex")
        vanilla = core.Group.create(a, self.tmp / "a-app", "Vanilla", servers[1], "Alex", taken=[servers[0]])
        for clash in (servers[0], servers[0] / "world"):  # the same folder, or one inside it, can't be shared
            with self.assertRaises(ValueError):
                core.Group.create(a, self.tmp / "a-app", "Again", clash, "Alex", taken=[servers[0], servers[1]])

        modded.set_password("modded1")
        vanilla.set_password("vanilla1")
        sam = join(modded, b, addr_a, self.tmp / "b-app", self.tmp / "sam-servers", password="modded1")
        with self.assertRaises(ValueError):  # joining the same group twice
            join(modded, b, addr_a, self.tmp / "b-app", self.tmp / "sam-servers2", password="modded1",
                 joined=[sam.world_id])
        let_in(a, [modded, vanilla], sam)
        wait_for(lambda: sam.local_version() == 1 and (sam.world_path / "world" / "level.dat").exists(),
                 "Sam gets the modded world")
        sam.joined("Sam")

        self.assertEqual([m["name"] for m in modded.status()["members"]], ["Sam"])
        self.assertEqual(vanilla.status()["members"], [], "Sam isn't in the vanilla group")
        self.assertFalse((self.tmp / "sam-servers" / "Vanilla").exists())

        # Alex's PC already knows Sam's now, so this time the proof comes in the folder Sam offers.
        refused = {}
        wrong = join(vanilla, b, addr_a, self.tmp / "b-app", self.tmp / "sam-servers", password="modded1")
        wait_for(lambda: core.admit(a, [modded, vanilla], refused)[1] == ["Sam"], "a wrong password is noticed")
        self.assertEqual(vanilla.status()["members"], [])
        sam_vanilla = wrong
        sam_vanilla.set_join_password("vanilla1", "Sam")  # typed it again, right this time
        let_in(a, [modded, vanilla], sam_vanilla)
        wait_for(lambda: (sam_vanilla.world_path / "world" / "level.dat").exists(), "Sam gets the vanilla world")
        self.assertTrue(a.devices()[b.my_id] == "Sam" and b.devices()[a.my_id] == "Alex")
        sam_vanilla.leave()

        vanilla.leave()
        self.assertFalse(a.has_folder(vanilla.world_id))
        self.assertFalse(a.has_folder(vanilla.baton_id))
        self.assertEqual((servers[1] / "world" / "level.dat").read_text(), "alex-vanilla", "leaving keeps the files")
        self.assertTrue(a.has_folder(modded.world_id))

    def test_remove_a_friend(self):
        """Sam and Kim join; Alex removes Kim. Sam's PC drops Kim too, and Kim can't get back in with the password."""
        c = Syncthing(self.tmp / "c-st")
        c.start()
        self.addCleanup(c.stop)
        addr_a = local_only(self.a)
        local_only(self.b)
        local_only(c)
        server = self.tmp / "alex-server"
        (server / "world").mkdir(parents=True)
        (server / "world" / "level.dat").write_text("day 1")
        ga = core.Group.create(self.a, self.tmp / "a-app", "Big P", server, "Alex")
        ga.set_password("hunter22")
        sam = join(ga, self.b, addr_a, self.tmp / "b-app", self.tmp / "sam-servers")
        kim = join(ga, c, addr_a, self.tmp / "c-app", self.tmp / "kim-servers", player="Kim")
        let_in(self.a, [ga], sam)
        let_in(self.a, [ga], kim)
        wait_for(lambda: c.my_id in sam.member_ids() and self.b.my_id in kim.member_ids(), "Alex introduces them")

        ga.remove_member(c.my_id)
        self.assertNotIn(c.my_id, self.a.devices())
        wait_for(lambda: c.my_id not in sam.member_ids(), "Sam's PC drops Kim")
        wait_for(lambda: c.my_id not in self.b.devices(), "and forgets Kim's PC")
        time.sleep(4)  # Kim's PC keeps knocking with a valid proof
        self.assertEqual(core.admit(self.a, [ga], {}), ([], []))
        self.assertNotIn(c.my_id, ga.member_ids())

    def test_restore_an_earlier_world(self):
        """Alex hosts twice, then brings back the first session's world; it's shared as a new version."""
        a = self.a
        server = self.tmp / "alex-server"
        (server / "world").mkdir(parents=True)
        (server / "world" / "level.dat").write_text("day 1")
        g = core.Group.create(a, self.tmp / "a-app", "Big P", server, "Alex")
        for day in (2, 3):
            g.start_hosting("Alex", "alex.example")
            (server / "world" / "level.dat").write_text(f"day {day}")
            self.assertEqual(g.stop_hosting("Alex"), "")
        self.assertEqual([b["version"] for b in g.backups()], [3, 2])  # this PC keeps the last 2

        warning = g.restore_version("Alex", next(b for b in g.backups() if b["version"] == 2))
        self.assertEqual(warning, "")
        self.assertEqual((server / "world" / "level.dat").read_text(), "day 2")
        self.assertEqual(g.local_version(), 4)
        self.assertIsNone(g.read_baton()["holder"])
        newest = g.history()[0]
        self.assertEqual((newest["version"], newest["restored_from"]), (4, 2))
        self.assertEqual([b["version"] for b in g.backups()], [4, 3])


if __name__ == "__main__":
    unittest.main()
