"""start.cmd's window: the relay it runs, restarted when its settings change or it exits, and left alone when
another relay holds the ports."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from companion import launcher, reloader


class FakeProc:
    def __init__(self, argv, **kw):
        self.argv, self.returncode = argv, None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 1

    def wait(self, timeout=None):
        return self.returncode


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = Path(tmp.name) / "launcher.local.json"
        self.started = []

        def popen(argv, **kw):
            self.started.append(FakeProc(argv))
            return self.started[-1]
        for p in (mock.patch.object(launcher, "SETTINGS", self.settings),
                  mock.patch.object(reloader.subprocess, "Popen", popen),
                  mock.patch.object(reloader.subprocess, "run"),
                  mock.patch.object(reloader, "tap_open", lambda: self.tap)):
            p.start()
            self.addCleanup(p.stop)
        self.tap = False
        self.clock = 1000.0
        p = mock.patch.object(reloader.time, "monotonic", lambda: self.clock)
        p.start()
        self.addCleanup(p.stop)

    def test_the_tint_setting_restarts_the_relay_with_its_flag(self):
        r = reloader.Relay()
        r.start(take_over=True)
        self.assertNotIn("--tint-store", self.started[-1].argv)
        r.tick()
        self.assertEqual(len(self.started), 1)
        self.settings.write_text(json.dumps({"tint": True}))
        r.tick()
        self.assertEqual(len(self.started), 2)
        self.assertEqual(self.started[0].returncode, 1)          # the old one was stopped
        self.assertIn("--tint-store", self.started[-1].argv)

    def test_a_relay_that_exits_comes_back_unless_another_holds_the_ports(self):
        r = reloader.Relay()
        r.start()
        self.started[-1].returncode = 1
        r.tick()                                     # noticed; waits before trying again
        self.assertEqual(len(self.started), 1)
        self.clock += 10                             # (a quick exit: the wait doubles)
        self.tap = True                              # relay.cmd's relay took over meanwhile
        r.tick()
        self.assertTrue(r.outside)
        self.assertEqual(len(self.started), 1)
        self.clock += 10
        self.tap = False                             # ...and it's gone
        r.tick()
        self.assertEqual(len(self.started), 2)
        self.assertFalse(r.outside)


if __name__ == "__main__":
    unittest.main()
