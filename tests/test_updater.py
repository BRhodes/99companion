"""Updates from GitHub: what's checked, what an update writes (and never writes), and the state the dashboard shows."""

import io
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from companion import updater

SHA = "0123456789abcdef0123456789abcdef01234567"


def github_zip(files, top="owner-repo-0123456"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(top + "/", "")
        for name, data in files.items():
            z.writestr(f"{top}/{name}", data)
    return buf.getvalue()


def fake_get(files):
    def get(url, timeout=20):
        if "api.github.com" in url:
            return json.dumps({"sha": SHA, "commit": {"message": "Newer things\n\nbody", "committer": {"date": "2026-10-17T10:00:00Z"}}}).encode()
        if "codeload.github.com" in url:
            assert url.endswith(SHA)
            return github_zip(files)
        raise AssertionError(url)
    return get


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_whats_never_written(self):
        for p in ("journals/x.cap", "accounts.local.json", "update.local.json", "companion.sqlite3", "cache/worldmap.png",
                  ".git/HEAD", "spellbook-layouts.json", "VERSION"):
            self.assertTrue(updater.kept(p), p)
        for p in ("companion/tracker.py", "README.md", "relay.cmd", "companion/data/places.json"):
            self.assertFalse(updater.kept(p), p)

    def test_unpack_strips_the_top_folder_and_keeps_your_files(self):
        (self.root / "journals").mkdir()
        (self.root / "journals" / "mine.cap").write_text("mine")
        (self.root / "accounts.local.json").write_text("secret")
        (self.root / "same.txt").write_text("same")
        written = updater.unpack(github_zip({"companion/a.py": "new", "journals/mine.cap": "theirs",
                                             "accounts.local.json": "theirs", "same.txt": "same", "../evil": "x"}), self.root)
        self.assertEqual(written, ["companion/a.py"])
        self.assertEqual((self.root / "companion" / "a.py").read_text(), "new")
        self.assertEqual((self.root / "journals" / "mine.cap").read_text(), "mine")
        self.assertEqual((self.root / "accounts.local.json").read_text(), "secret")
        self.assertFalse((self.root.parent / "evil").exists())

    def test_latest(self):
        got = updater.latest("owner/repo", "main", fake_get({}))
        self.assertEqual((got["sha"], got["message"]), (SHA, "Newer things"))

    def test_a_download_updates_by_copying_and_says_when_the_relay_changed(self):
        (self.root / "update.local.json").write_text(json.dumps({"repo": "owner/repo"}))
        u = updater.Updater(root=self.root, get=fake_get({"companion/a.py": "new", "companion/relay.py": "relay"}))
        u.check()
        self.assertTrue(u.state()["available"])
        self.assertTrue(u.apply())
        for _ in range(100):
            if not u.info["busy"]:
                break
            time.sleep(0.02)
        st = u.state()
        self.assertEqual((st["current"], st["available"], st["relay"], st["error"]), (SHA, False, True, None))
        self.assertEqual((self.root / "VERSION").read_text(), SHA)

    def test_nothing_without_a_repository(self):
        (self.root / "update.local.json").write_text(json.dumps({"repo": ""}))
        self.addCleanup(setattr, updater, "REPO", updater.REPO)
        updater.REPO = ""
        u = updater.Updater(root=self.root, get=fake_get({}))
        u.check()
        self.assertEqual(u.state()["error"], "No repository set")
        self.assertFalse(u.state()["available"])


if __name__ == "__main__":
    unittest.main()
