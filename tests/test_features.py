"""Notifications, asking to host next, history, your game's mods, checks before hosting, Discord links."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from worldbaton import core, tools
from tests.test_watch import FakeSyncthing, make_group

ME, ALEX = "ME-ID", "ALEX-ID"


def status(**kw):
    base = {"baton": {}, "holder_alive": False, "me_hosting": False, "requests": [], "my_request": False}
    return {**base, **kw}


class Notifications(unittest.TestCase):
    def test_nothing_on_first_look(self):
        self.assertEqual(core.events(None, status(baton={"holder": ALEX}, holder_alive=True), ME), [])

    def test_someone_hosts_then_server_up_then_stops(self):
        idle = status(baton={"holder": None})
        hosting = status(baton={"holder": ALEX, "holder_name": "Alex", "address": "a.gg"}, holder_alive=True)
        up = status(baton={"holder": ALEX, "holder_name": "Alex", "address": "a.gg", "server_up": True}, holder_alive=True)
        self.assertEqual(core.events(idle, hosting, ME), [("Alex is hosting", "They're getting the server ready.")])
        self.assertEqual(core.events(hosting, up, ME), [("Alex's server is up", "Join at a.gg")])
        self.assertEqual(core.events(up, up, ME), [])  # no repeats every refresh
        done = status(baton={"holder": None}, my_request=True)
        self.assertEqual(core.events(up, done, ME), [("Alex stopped hosting", "You can host now.")])

    def test_host_hears_requests_once(self):
        me = status(baton={"holder": ME}, holder_alive=True, me_hosting=True)
        asked = {**me, "requests": ["Sam"]}
        self.assertEqual(core.events(me, asked, ME)[0][0], "Sam wants to host next")
        self.assertEqual(core.events(asked, asked, ME), [])

    def test_my_own_hosting_is_quiet(self):
        mine = status(baton={"holder": ME, "server_up": True}, holder_alive=True, me_hosting=True)
        self.assertEqual(core.events(status(baton={"holder": None}), mine, ME), [])


class Requests(unittest.TestCase):
    def test_ask_cancel_and_expire(self):
        with tempfile.TemporaryDirectory() as d:
            sam = make_group(Path(d))
            alex = core.Group(FakeSyncthing("ZZZZZZZ-XYZ"), sam.info, Path(d) / "app")  # same baton folder
            sam.request_host("Sam")
            self.assertTrue(sam.my_request())
            self.assertEqual(sam.requests(), [])  # your own request isn't "someone asking"
            self.assertEqual(alex.requests(), ["Sam"])
            with mock.patch("time.time", return_value=time.time() + core.REQUEST_FOR + 1):
                self.assertEqual(alex.requests(), [])
            sam.cancel_request()
            self.assertEqual(alex.requests(), [])


class History(unittest.TestCase):
    def test_newest_first_from_every_pc(self):
        with tempfile.TemporaryDirectory() as d:
            sam = make_group(Path(d))
            alex = core.Group(FakeSyncthing("ZZZZZZZ-XYZ"), sam.info, Path(d) / "app")
            sam._add_history({"host": "Sam", "start": 10, "end": 20, "version": 2})
            alex._add_history({"host": "Alex", "start": 30, "end": 40, "version": 3})
            self.assertEqual([e["host"] for e in sam.history()], ["Alex", "Sam"])


class GameMods(unittest.TestCase):
    def test_sync_keeps_own_mods(self):
        with tempfile.TemporaryDirectory() as d:
            server, game = Path(d) / "server", Path(d) / "game"
            server.mkdir()
            game.mkdir()
            (server / "create.jar").write_text("v6")
            (server / "jei.jar").write_text("same")
            (game / "jei.jar").write_text("same")
            (game / "minimap.jar").write_text("mine")  # the player's own client-only mod
            added, removed, managed = tools.sync_game_mods(server, game, [])
            self.assertEqual((added, removed), (["create.jar"], []))
            (server / "create.jar").write_text("v7 longer")
            (server / "jei.jar").unlink()
            added, removed, managed = tools.sync_game_mods(server, game, managed)
            self.assertEqual((added, removed, managed), (["create.jar"], ["jei.jar"], ["create.jar"]))
            self.assertEqual(sorted(p.name for p in game.iterdir()), ["create.jar", "minimap.jar"])
            self.assertEqual((game / "create.jar").read_text(), "v7 longer")


class BeforeHosting(unittest.TestCase):
    def test_loader_version_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            w = Path(d)
            (w / "run.bat").write_text('"C:\\Java\\bin\\java.exe" @user_jvm_args.txt '
                                       "@libraries/net/neoforged/neoforge/21.1.200/win_args.txt nogui %*")
            (w / "libraries" / "net" / "neoforged" / "neoforge" / "21.1.250").mkdir(parents=True)
            self.assertEqual(tools.loader_from_start_script(w), ("net/neoforged/neoforge", "21.1.200"))
            with mock.patch("shutil.disk_usage", return_value=mock.Mock(free=100e9)):
                problems = tools.prehost_problems(w)
            self.assertEqual(len(problems), 1)
            self.assertIn("NeoForge 21.1.200", problems[0])
            self.assertIn("uses NeoForge 21.1.250", problems[0])
            (w / "libraries" / "net" / "neoforged" / "neoforge" / "21.1.200").mkdir()
            with mock.patch("shutil.disk_usage", return_value=mock.Mock(free=1e9)):
                problems = tools.prehost_problems(w)
            self.assertEqual(len(problems), 1)
            self.assertIn("1.0 GB is free", problems[0])

    def test_no_start_script_no_complaint(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("shutil.disk_usage", return_value=mock.Mock(free=100e9)):
            self.assertEqual(tools.prehost_problems(Path(d)), [])


class FindingSquidServers(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        env = {"APPDATA": str(self.tmp / "Roaming"), "LOCALAPPDATA": str(self.tmp / "Local")}
        self.env = mock.patch.dict("os.environ", env)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_not_installed(self):
        with mock.patch.object(tools, "_squid_registered", return_value=False):
            self.assertFalse(tools.squid_installed())
        self.assertEqual(tools.squid_servers_dir(), self.tmp / "Local" / "SquidServers")

    def test_installed_somewhere_else(self):
        with mock.patch.object(tools, "_squid_registered", return_value=True):  # in Windows' installed-apps list
            self.assertTrue(tools.squid_installed())

    def test_servers_in_a_custom_folder(self):
        data = self.tmp / "Roaming" / "squidservers"
        data.mkdir(parents=True)
        (data / "user-preferences.json").write_text('{"defaultServerDirectory": "D:\\\\Games\\\\Servers"}')
        self.assertEqual(tools.squid_servers_dir(), Path("D:/Games/Servers"))
        (data / "servers.json").write_text("[]")  # it has been set up, even with no servers folder yet
        with mock.patch.object(tools, "_squid_registered", return_value=False):
            self.assertTrue(tools.squid_installed())


class ConfigUpgrade(unittest.TestCase):
    def test_one_group_becomes_a_list(self):
        from worldbaton.app import upgrade_config
        old = {"player": "Sam", "address": "sam.gg", "group": {"world_id": "wb-world-1", "name": "Big P"}}
        cfg = upgrade_config(old)
        self.assertEqual(cfg["groups"], [{"world_id": "wb-world-1", "name": "Big P", "address": "sam.gg"}])
        self.assertEqual(cfg["active"], "wb-world-1")
        self.assertNotIn("group", cfg)
        self.assertEqual(upgrade_config({"player": "New"})["groups"], [])
        self.assertEqual(upgrade_config(dict(cfg)), cfg, "upgrading twice changes nothing")


class Discord(unittest.TestCase):
    def test_only_webhook_links(self):
        self.assertTrue(tools.valid_webhook("https://discord.com/api/webhooks/123456/abc-DEF_9"))
        self.assertTrue(tools.valid_webhook("https://canary.discord.com/api/webhooks/1/x"))
        for bad in ("https://evil.example/api/webhooks/1/x", "http://discord.com/api/webhooks/1/x",
                    "https://discord.com.evil.example/api/webhooks/1/x", "discord.com/api/webhooks/1/x", ""):
            self.assertFalse(tools.valid_webhook(bad), bad)
        with self.assertRaises(ValueError):
            tools.post_discord("https://evil.example/x", "hi")


if __name__ == "__main__":
    unittest.main()
