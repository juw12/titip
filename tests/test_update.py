"""Self-update against real git repositories in a temporary folder: nothing here touches the network."""
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from titip import update
from titip.update import Updater, relaunch, run_command

GIT = ["git", "-c", "user.name=Titip Test", "-c", "user.email=test@titip.example", "-c", "commit.gpgsign=false",
       "-c", "core.autocrlf=false", "-c", "protocol.file.allow=always"]
FILES = {
    "titip/__init__.py": '__version__ = "1"\n',
    "titip/server.py": "PORT = 1\n",
    "titip/__main__.py": "from . import server\n",
    "titip/update.py": "EVERY = 900\n",
}


def git(folder: Path, *args: str) -> str:
    done = subprocess.run([*GIT, *args], cwd=folder, capture_output=True, text=True)
    if done.returncode:
        raise AssertionError(f"git {' '.join(args)}: {done.stderr}")
    return done.stdout.strip()


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class Checkout(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.folder.cleanup)
        base = Path(self.folder.name)
        self.home, self.origin, self.desk, self.copy = base / "home", base / "origin.git", base / "desk", base / "copy"
        self.home.mkdir()
        git(base, "init", "--quiet", "--bare", "-b", "main", str(self.origin))
        git(base, "clone", "--quiet", str(self.origin), str(self.desk))
        git(self.desk, "checkout", "--quiet", "-b", "main")
        self.publish(FILES, "first version")
        git(base, "clone", "--quiet", str(self.origin), str(self.copy))  # the copy that runs on the PC
        self.now = 1_800_000_000.0

    def publish(self, files: dict, message: str) -> str:
        """A new version on the published main branch. Returns its commit."""
        for name, text in files.items():
            path = self.desk / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        git(self.desk, "add", "-A")
        git(self.desk, "commit", "--quiet", "-m", message)
        git(self.desk, "push", "--quiet", "origin", "main")
        return git(self.desk, "rev-parse", "HEAD")

    def updater(self, root=None, **more) -> Updater:
        return Updater(self.home, root or self.copy, clock=lambda: self.now, **more)

    def head(self) -> str:
        return git(self.copy, "rev-parse", "HEAD")

    def test_nothing_new_means_up_to_date(self):
        updater = self.updater()
        self.assertEqual(updater.status()["state"], "waiting")
        self.assertEqual(updater.check(), "current")
        status = updater.status()
        self.assertEqual((status["available"], status["on"], status["checked"]), (True, True, int(self.now)))
        self.assertEqual(status["commit"], self.head()[:len(status["commit"])])
        self.assertFalse((self.home / "update-check").exists())

    def test_a_new_version_is_checked_put_in_place_and_asks_for_a_restart(self):
        updater, before = self.updater(), self.head()
        new = self.publish({"titip/server.py": "PORT = 2\n"}, "second version")
        self.assertEqual(updater.check(), "ready")
        self.assertEqual(self.head(), new)
        self.assertEqual((self.copy / "titip" / "server.py").read_text(encoding="utf-8"), "PORT = 2\n")
        self.assertEqual(updater.pending, new)
        saved = json.loads((self.home / "update.json").read_text(encoding="utf-8"))
        self.assertEqual((saved["previous"], saved["merged"]), (before, new))
        self.assertFalse((self.home / "update-check").exists())  # the spare folder is gone again
        self.assertEqual(updater.check(), "ready")  # still waiting for its restart: no second round

    def test_a_version_that_does_not_start_is_never_put_in_place(self):
        updater, before = self.updater(), self.head()
        self.publish({"titip/server.py": "def broken(:\n"}, "does not even import")
        self.assertEqual(updater.check(), "failed")
        self.assertEqual(self.head(), before)
        self.assertEqual(updater.pending, "")
        self.assertIn("keeps running", updater.status()["note"])
        fixed = self.publish({"titip/server.py": "PORT = 3\n"}, "fixed")
        self.assertEqual(updater.check(), "ready")
        self.assertEqual(self.head(), fixed)

    def test_a_copy_with_its_own_changes_is_left_alone(self):
        self.publish({"titip/server.py": "PORT = 2\n"}, "second version")
        before = self.head()
        (self.copy / "notes.txt").write_text("mine", encoding="utf-8")  # a file git does not track is no obstacle
        (self.copy / "titip" / "server.py").write_text("PORT = 99\n", encoding="utf-8")
        updater = self.updater()
        self.assertEqual(updater.check(), "held")
        self.assertEqual(self.head(), before)
        self.assertEqual((self.copy / "titip" / "server.py").read_text(encoding="utf-8"), "PORT = 99\n")
        git(self.copy, "checkout", "--", "titip/server.py")
        self.assertEqual(updater.check(), "ready")
        self.assertEqual((self.copy / "notes.txt").read_text(encoding="utf-8"), "mine")

    def test_another_branch_or_commits_of_its_own_are_left_alone(self):
        self.publish({"titip/server.py": "PORT = 2\n"}, "second version")
        git(self.copy, "checkout", "--quiet", "-b", "experiment")
        self.assertEqual(self.updater().check(), "held")
        git(self.copy, "checkout", "--quiet", "main")
        (self.copy / "titip" / "server.py").write_text("PORT = 7\n", encoding="utf-8")
        git(self.copy, "commit", "--quiet", "-am", "a local commit")
        mine = self.head()
        self.assertEqual(self.updater().check(), "held")
        self.assertEqual(self.head(), mine)

    def test_switched_off_it_does_not_even_look(self):
        updater, before = self.updater(), self.head()
        updater.switch(False)
        self.publish({"titip/server.py": "PORT = 2\n"}, "second version")
        self.assertEqual(updater.check(), "off")
        self.assertEqual(self.head(), before)
        self.assertEqual(git(self.copy, "rev-parse", "origin/main"), before)  # not even fetched
        self.assertFalse(self.updater().status()["on"])  # the switch survives a restart
        updater.switch(True)
        self.assertEqual(updater.check(), "ready")

    def test_an_update_that_did_not_start_is_undone_and_not_tried_again(self):
        updater, before = self.updater(), self.head()
        bad = self.publish({"titip/server.py": "PORT = 2\n"}, "imports, but would not start")
        self.assertEqual(updater.check(), "ready")
        self.assertTrue(updater.undo())
        self.assertEqual(self.head(), before)
        self.assertEqual((self.copy / "titip" / "server.py").read_text(encoding="utf-8"), "PORT = 1\n")
        self.assertFalse(updater.undo())  # nothing left to undo
        again = self.updater()  # the old version, started again
        self.assertEqual(again.status()["state"], "skipped")
        self.assertEqual(again.check(), "skipped")
        self.assertEqual(self.head(), before)
        newer = self.publish({"titip/server.py": "PORT = 3\n"}, "a newer one")
        self.assertNotEqual(newer, bad)
        self.assertEqual(again.check(), "ready")
        self.assertEqual(self.head(), newer)

    def test_undo_touches_nothing_it_did_not_leave_behind(self):
        updater = self.updater()
        self.assertFalse(updater.undo())  # there was no update
        new = self.publish({"titip/server.py": "PORT = 2\n"}, "second version")
        self.assertEqual(updater.check(), "ready")
        (self.copy / "titip" / "server.py").write_text("PORT = 50\n", encoding="utf-8")  # someone edited meanwhile
        self.assertFalse(updater.undo())
        self.assertEqual(self.head(), new)
        self.assertEqual((self.copy / "titip" / "server.py").read_text(encoding="utf-8"), "PORT = 50\n")

    def test_a_folder_that_is_not_its_own_checkout_cannot_update(self):
        for root in (None, self.home, self.copy / "titip"):  # no folder, no git, a folder inside a checkout
            updater = Updater(self.home, root)
            self.assertEqual((updater.status()["available"], updater.check()), (False, "unavailable"), root)
            updater.start()
            self.assertIsNone(updater._thread)

    def test_no_connection_is_an_error_and_the_proxy_is_the_second_try(self):
        calls = []

        def offline(args, cwd, timeout=120):
            calls.append(args)
            return (1, "") if "fetch" in args and "http.proxy=http://proxy.example:3128" not in args else run_command(args, cwd, timeout)

        updater = self.updater(run=offline)
        self.assertEqual(updater.check(), "error")
        self.assertIn("not reachable", updater.status()["note"])
        (self.home / "update.json").write_text(json.dumps({"proxy": "http://proxy.example:3128"}), encoding="utf-8")
        calls.clear()
        updater = self.updater(run=offline)
        self.assertEqual(updater.check(), "current")
        fetches = [args for args in calls if "fetch" in args]
        self.assertEqual(len(fetches), 2)
        self.assertNotIn("-c", fetches[0])  # first without the proxy
        self.assertEqual(fetches[1][1:3], ["-c", "http.proxy=http://proxy.example:3128"])

    def test_it_restarts_only_when_nothing_is_in_progress(self):
        self.publish({"titip/server.py": "PORT = 2\n"}, "second version")
        updater = self.updater(first=0.01)
        busy, asked, restarted = threading.Event(), threading.Event(), threading.Event()
        busy.set()

        def is_busy():
            asked.set()
            return busy.is_set()

        updater.busy, updater.restart = is_busy, restarted.set
        real, update.BUSY_WAIT_S = update.BUSY_WAIT_S, 0.05
        self.addCleanup(setattr, update, "BUSY_WAIT_S", real)
        updater.start()
        self.addCleanup(updater.stop)
        self.assertTrue(asked.wait(60))
        self.assertFalse(restarted.wait(0.3))  # Autopilot is in the middle of something
        self.assertEqual(updater.status()["state"], "ready")
        busy.clear()
        self.assertTrue(restarted.wait(10))


class Child:
    def __init__(self, ended=None):
        self.ended, self.killed = ended, False

    def poll(self):
        return self.ended

    def kill(self):
        self.killed = True


class Relaunch(unittest.TestCase):
    def run_it(self, alive, child, undo=lambda: True, wait_s=5):
        spawned = []

        def spawn(command, cwd):
            spawned.append(command)
            return child

        outcome = relaunch(["python", "Titip.pyw", "--quiet"], Path("."), alive, undo, spawn=spawn, wait_s=wait_s, pause=lambda _: None)
        return outcome, spawned

    def test_the_new_version_answers(self):
        answers = iter([False, False, True])
        child = Child()
        outcome, spawned = self.run_it(lambda: next(answers), child)
        self.assertEqual((outcome, len(spawned), child.killed), ("new", 1, False))

    def test_a_version_that_dies_is_undone_and_the_old_one_started(self):
        undone = []
        child = Child(ended=1)
        outcome, spawned = self.run_it(lambda: False, child, undo=lambda: undone.append(1) or True)
        self.assertEqual((outcome, len(spawned), undone), ("old", 2, [1]))

    def test_a_version_that_hangs_is_stopped_first(self):
        child = Child()
        outcome, spawned = self.run_it(lambda: False, child, wait_s=3)
        self.assertEqual((outcome, child.killed, len(spawned)), ("old", True, 2))

    def test_when_going_back_is_not_possible_nothing_is_started_blindly(self):
        outcome, spawned = self.run_it(lambda: False, Child(ended=1), undo=lambda: False)
        self.assertEqual((outcome, len(spawned)), ("down", 1))


class Command(unittest.TestCase):
    def test_the_restart_command_is_quiet_and_matches_how_titip_was_started(self):
        from titip import __main__ as start

        real = sys.argv
        self.addCleanup(setattr, sys, "argv", real)
        launcher = Path(start.ROOT) / "Titip.pyw"
        sys.argv = [str(launcher), "--quiet"]
        self.assertEqual(start.restart_command(["--quiet"]), [sys.executable, str(launcher.resolve()), "--quiet"])
        sys.argv = [str(Path(start.__file__)), "--http"]
        self.assertEqual(start.restart_command(["--http"]), [sys.executable, "-m", "titip", "--http", "--quiet"])
        self.assertEqual(start.restart_command(["--quiet", "--desk-port", "5"]),
                         [sys.executable, "-m", "titip", "--desk-port", "5", "--quiet"])


if __name__ == "__main__":
    unittest.main()
