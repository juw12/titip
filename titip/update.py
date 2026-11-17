"""Titip keeps itself up to date.

A copy that was installed with git (the usual way: clone, then start) looks at its origin every
quarter of an hour. When the published main branch has moved on, it

    1. exports that version into a spare folder and checks that it starts (imports cleanly, with the
       Python packages this PC has),
    2. fast-forwards the checkout to it (never a reset, never a forced move),
    3. waits until nothing is in progress (Autopilot idle, no mail request under way) and restarts.

If the new version then fails to come up, the old process puts the checkout back to the commit it
ran before, starts that again and remembers the bad version, so it is not tried a second time.

A copy that is not on main, or has changes of its own, is left alone and says so on the PC page.

Settings live in <home>/update.json: {"on": true, "proxy": ""}. "proxy" (for example
http://localhost:3128) is only used when GitHub cannot be reached without it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

CHECK_EVERY_S = 15 * 60
FIRST_CHECK_S = 60
BUSY_WAIT_S = 5
START_WAIT_S = 90
SMOKE = "import titip.server, titip.__main__, titip.update"
REFSPEC = "+refs/heads/main:refs/remotes/origin/main"

NOTES = {
    "unavailable": "This copy was not installed with git, so it cannot update itself.",
    "off": "Updates are switched off. This copy stays as it is.",
    "waiting": "Looks for a new version a minute after it starts, then every 15 minutes.",
    "current": "Up to date.",
    "held": "This copy is not a clean copy of the published version (another branch, or changes of its own), so it is left alone.",
    "error": "Could not look for a new version. GitHub was not reachable from this PC.",
    "failed": "A new version is out, but it did not pass its start-up check on this PC. The current one keeps running.",
    "skipped": "The newest version did not start on this PC, so Titip went back to this one. It tries again when a newer one is out.",
    "ready": "A new version is in place. Titip restarts as soon as nothing is in progress.",
}


def run_command(args: list[str], cwd: Path, timeout: float = 120) -> tuple[int, str]:
    """(exit code, output) of a program, without a console window and without ever asking for a password."""
    try:
        done = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return done.returncode, (done.stdout or "").strip()


def spawn_detached(command: list[str], cwd: Path):
    """Start a program that outlives this one."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(
        command, cwd=cwd, creationflags=flags, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def relaunch(command: list[str], cwd: Path, alive, undo, spawn=spawn_detached, wait_s: int = START_WAIT_S, pause=time.sleep) -> str:
    """Start the new version and watch it come up. "new" when it did; otherwise go back: "old", or "down"."""
    child = spawn(command, cwd)
    for _ in range(wait_s):
        if alive():
            return "new"
        if child.poll() is not None:  # it ended without ever answering
            break
        pause(1)
    if alive():
        return "new"
    try:
        child.kill()
    except OSError:
        pass
    if undo():
        spawn(command, cwd)
        return "old"
    return "down"


class Updater:
    def __init__(
        self, home: Path, root: Path | None, run=run_command, clock=time.time, python: str = sys.executable,
        every: float = CHECK_EVERY_S, first: float = FIRST_CHECK_S,
    ):
        self.path = Path(home) / "update.json"
        self.scratch = Path(home) / "update-check"
        self.root = Path(root) if root else None
        self.run = run
        self.clock = clock
        self.python = python
        self.every, self.first = every, first
        self.busy = lambda: False  # set by the app: is something in progress that a restart would cut short?
        self.restart = lambda: None  # set by the app: stop and start the new version
        self.checked = 0.0
        self.pending = ""  # the commit that is in place and waits for a restart
        self.settings = self._load()
        self.available = self._is_checkout()
        self.commit = self._git("rev-parse", "--short", "HEAD")[1] if self.available else ""
        self.state = "unavailable" if not self.available else "waiting" if self.settings["on"] else "off"
        if self.available and self.settings["skip"] and self.settings["on"]:
            self.state = "skipped"
        self._poke = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ settings
    def _load(self) -> dict:
        settings = {"on": True, "proxy": "", "skip": "", "previous": "", "merged": ""}
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            settings["on"] = saved.get("on") is not False
            settings.update({name: str(saved[name]) for name in ("proxy", "skip", "previous", "merged") if saved.get(name)})
        except (OSError, ValueError, AttributeError):
            pass
        return settings

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.settings, indent=1), encoding="utf-8")

    def status(self) -> dict:
        return {
            "available": self.available,
            "on": self.settings["on"],
            "state": self.state,
            "note": NOTES[self.state],
            "checked": int(self.checked),
            "commit": self.commit,
        }

    def switch(self, on: bool) -> None:
        self.settings["on"] = bool(on)
        self._save()
        if self.available and not self.pending:
            self.state = "waiting" if on else "off"
        if on:
            self.check_soon()

    def check_soon(self) -> None:
        self._poke.set()

    # ------------------------------------------------------------ git
    def _is_checkout(self) -> bool:
        """True when Titip's folder is itself the top of a git checkout (not a folder inside someone else's)."""
        if self.root is None:
            return False
        code, top = self._git("rev-parse", "--show-toplevel")
        try:
            return code == 0 and bool(top) and Path(top).resolve() == self.root.resolve()
        except OSError:
            return False

    def _git(self, *args: str, proxy: str = "") -> tuple[int, str]:
        extra = ["-c", f"http.proxy={proxy}"] if proxy else []
        return self.run(["git", *extra, *args], self.root)

    def _clean(self) -> bool:
        code, changed = self._git("status", "--porcelain", "--untracked-files=no")
        return code == 0 and not changed

    def _fetch(self) -> bool:
        if self._git("fetch", "--quiet", "origin", REFSPEC)[0] == 0:
            return True
        proxy = self.settings["proxy"]
        return bool(proxy) and self._git("fetch", "--quiet", "origin", REFSPEC, proxy=proxy)[0] == 0

    def _starts(self, commit: str) -> bool:
        """Export that version into a spare folder and see whether it imports with this PC's Python."""
        shutil.rmtree(self.scratch, ignore_errors=True)
        try:
            self.scratch.mkdir(parents=True)
            archive = self.scratch / "version.zip"
            if self._git("archive", "--format=zip", "-o", str(archive), commit)[0] != 0:
                return False
            with zipfile.ZipFile(archive) as packed:
                packed.extractall(self.scratch / "version")
            return self.run([self.python, "-c", SMOKE], self.scratch / "version", 180)[0] == 0
        except (OSError, zipfile.BadZipFile):
            return False
        finally:
            shutil.rmtree(self.scratch, ignore_errors=True)

    # ------------------------------------------------------------ one look
    def check(self) -> str:
        """Look for a new version and put it in place. Returns the new state; "ready" means: restart."""
        self.state = self._check()
        return self.state

    def _check(self) -> str:
        if not self.available:
            return "unavailable"
        if not self.settings["on"]:
            return "off"
        if self.pending:
            return "ready"
        self.checked = self.clock()
        if self._git("rev-parse", "--abbrev-ref", "HEAD")[1] != "main" or not self._clean():
            return "held"
        if not self._fetch():
            return "error"
        (code_here, here), (code_new, new) = self._git("rev-parse", "HEAD"), self._git("rev-parse", "origin/main")
        if code_here or code_new or not here or not new:
            return "error"
        if here == new:
            return "current"
        if self._git("merge-base", "--is-ancestor", here, new)[0] != 0:
            return "held"  # this copy has commits the published one does not
        if new == self.settings["skip"]:
            return "skipped"
        if not self._starts(new):
            return "failed"
        self.settings.update(previous=here, merged=new)
        self._save()
        if self._git("merge", "--ff-only", new)[0] != 0:
            return "error"
        self.pending = new
        return "ready"

    def undo(self) -> bool:
        """The version just put in place did not start: put the one before back and leave that update alone."""
        previous, merged = self.settings["previous"], self.settings["merged"]
        if not (self.available and previous and merged):
            return False
        if self._git("rev-parse", "HEAD")[1] != merged or not self._clean():
            return False  # not what we left behind: touch nothing
        if self._git("reset", "--keep", previous)[0] != 0:
            return False
        self.settings.update(skip=merged, previous="", merged="")
        self._save()
        self.pending = ""
        return True

    # ------------------------------------------------------------ the thread
    def start(self) -> None:
        if self._thread is None and self.available:
            self._thread = threading.Thread(target=self._run, daemon=True, name="titip-update")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._poke.set()

    def _run(self) -> None:
        wait = self.first
        while True:
            self._poke.wait(wait)
            self._poke.clear()
            if self._stop.is_set():
                return
            try:
                state = self.check()
            except Exception:  # a broken git, a full disk: keep running what runs
                state = self.state = "error"
            if state == "ready":
                if not self.busy():
                    self.restart()
                    return
                wait = BUSY_WAIT_S
            else:
                wait = self.every
