"""Settings > Game and Accounts: the game folder, accounts (whose passwords never come back out), and the API
that only takes an account's key to launch."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from companion import launcher


class LauncherTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.accounts = self.dir / "accounts.local.json"
        for p in (mock.patch.object(launcher, "ACCOUNTS", self.accounts),
                  mock.patch.object(launcher, "SETTINGS", self.dir / "launcher.local.json")):
            p.start()
            self.addCleanup(p.stop)

    def game(self, *names):
        g = self.dir / "game"
        g.mkdir(exist_ok=True)
        for n in names:
            (g / n).write_bytes(b"x")
        return g

    def test_accounts_never_give_a_password_back(self):
        key = launcher.save_account({"account": "zorn", "password": "hunter2", "background": True})
        self.assertEqual(key, "1")
        self.assertEqual(launcher.accounts(running={"zorn": [42]}),
                         [{"key": "1", "account": "zorn", "password": True, "background": True, "dir": "", "running": True}])
        self.assertNotIn("hunter2", json.dumps(launcher.Launcher().state()))
        self.assertEqual(json.loads(self.accounts.read_text())["1"]["password"], "hunter2")   # (aclaunch reads it)

    def test_a_blank_password_keeps_the_saved_one_and_other_fields_stay(self):
        self.accounts.write_text(json.dumps({"1": {"account": "zorn", "password": "old", "dir": "D:\\AC2"},
                                             "2": {"account": "", "password": ""}}))
        launcher.save_account({"account": "zorn2", "password": "", "background": False}, "1")
        saved = json.loads(self.accounts.read_text())["1"]
        self.assertEqual((saved["account"], saved["password"], saved["dir"], saved["background"]), ("zorn2", "old", "D:\\AC2", False))
        self.assertEqual(launcher.save_account({"account": "alt", "password": "p"}), "2")    # the blank template's slot
        launcher.delete_account("1")
        self.assertEqual([a["key"] for a in launcher.accounts()], ["2"])

    def test_bad_accounts_are_refused(self):
        for values, key in (({"account": ""}, None), ({"account": "a:b"}, None), ({"account": "x" * 65}, None),
                            ({"account": "zorn", "password": 5}, None), ({"account": "zorn"}, "9")):
            with self.assertRaises(ValueError):
                launcher.save_account(values, key)

    def test_the_game_folder(self):
        g = self.game("portal.dat", "cell.dat")
        self.assertEqual(launcher.check_game(g)["ok"], False)                  # no client yet
        self.game(launcher.CLIENT_EXE)
        got = launcher.check_game(g)
        self.assertEqual((got["ok"], got["background"]), (True, False))
        with mock.patch.object(launcher, "registry_dirs", lambda: [str(self.dir / "nowhere"), str(g)]):
            self.assertEqual(launcher.find_game(), str(g))
            self.assertEqual(launcher.game_dir(), str(g))
            launcher.save_settings({"game_dir": "E:\\Picked"})
            self.assertEqual(launcher.game_dir(), "E:\\Picked")                  # a picked folder wins

    def test_whose_client_it_is_comes_from_its_command_line_without_the_password(self):
        self.assertEqual(launcher.account_in(r".\client-windows-3d-accel.exe -h 127.0.0.1 -p 9110 -a Zorn:pa ss -db"), "Zorn")
        self.assertEqual(launcher.account_in('client.exe -a "zorn:pw"'), "zorn")
        self.assertIsNone(launcher.account_in("client.exe -h 127.0.0.1"))
        with mock.patch.object(launcher, "_client_command_lines", lambda: [(7, "c.exe -a Zorn:secret"), (8, "c.exe")]):
            self.assertEqual(launcher.running_accounts(), {"zorn": [7]})
            self.assertNotIn("secret", json.dumps(launcher.running_accounts()))

    def test_launch_takes_only_a_known_key(self):
        import asyncio
        self.assertEqual(asyncio.run(launcher.launch("..\\..\\evil")), (False, "no such account"))


class LauncherRoutesTest(unittest.TestCase):
    """The dashboard's routes for it (the Companion's _launcher_route), without a relay."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for p in (mock.patch.object(launcher, "ACCOUNTS", self.dir / "accounts.local.json"),
                  mock.patch.object(launcher, "SETTINGS", self.dir / "launcher.local.json")):
            p.start()
            self.addCleanup(p.stop)
        from companion.__main__ import Companion
        from companion.store import Store
        self.app = Companion({}, Store(":memory:"))

    def call(self, method, path, body=None):
        status, _, out = self.app.route(method, path, json.dumps(body).encode() if body is not None else b"")
        return status, json.loads(out)

    def test_accounts_round_trip(self):
        status, got = self.call("POST", "/api/accounts", {"account": "zorn", "password": "pw"})
        self.assertEqual((status, got["key"]), (200, "1"))
        self.assertNotIn("pw", json.dumps(self.app.snapshot()["launcher"]))
        self.assertEqual(self.call("PUT", "/api/accounts/1", {"background": True})[0], 200)
        self.assertTrue(launcher.accounts()[0]["background"])
        self.assertEqual(self.call("PUT", "/api/accounts/7", {"account": "x"})[0], 400)
        self.assertEqual(self.call("DELETE", "/api/accounts/1")[0], 200)
        self.assertEqual(launcher.accounts(), [])

    def test_a_folder_without_the_game_is_refused(self):
        status, got = self.call("PUT", "/api/launcher/game", {"dir": str(self.dir)})
        self.assertEqual(status, 400)
        self.assertNotIn("game_dir", launcher.settings())
        for n in ("portal.dat", "cell.dat"):
            (self.dir / n).write_bytes(b"x")
        status, got = self.call("PUT", "/api/launcher/game", {"dir": str(self.dir)})
        self.assertEqual((status, got["restart"]), (200, True))
        self.assertEqual(launcher.settings()["game_dir"], str(self.dir))
        self.assertFalse(self.app.restart)               # (not run from start.cmd's window: nothing restarts it)

    def test_the_tint_setting(self):
        self.assertEqual(self.call("PUT", "/api/launcher/settings", {"tint": True})[1]["tint"], True)
        self.assertEqual(launcher.settings()["tint"], True)


if __name__ == "__main__":
    unittest.main()
