"""Start Titip:  python -m titip            (serve your Outlook mail to paired phones, run Autopilot for Gmail,
                                            keep itself up to date: see titip/update.py)
                 python -m titip --demo     (invented mail, safe to show around)
                 python -m titip unpair     (cut every paired phone off)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__
from .autopilot import Autopilot, DemoGmail, demo_ask
from .server import DESK_PORT, PHONE_PORT, Titip
from .update import Updater, relaunch

ROOT = Path(__file__).resolve().parent.parent


def say(text: str) -> None:
    if sys.stdout is not None:  # pythonw has no console
        print(text, flush=True)


NORMAL_PRIORITY, LOWERED_PRIORITIES = 0x20, (0x40, 0x4000)  # Windows: normal; idle and below normal


def stay_responsive() -> bool:
    """Go back to normal priority if something lowered it. True when it had to.

    Some PCs push background programs down. On a busy PC that leaves a phone waiting many
    seconds for every page, and minutes for Outlook. Titip never asks for more than normal.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.GetPriorityClass.argtypes = [ctypes.c_void_p]
        kernel.GetPriorityClass.restype = ctypes.c_ulong
        kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        process = kernel.GetCurrentProcess()
        if kernel.GetPriorityClass(process) in LOWERED_PRIORITIES:
            return bool(kernel.SetPriorityClass(process, NORMAL_PRIORITY))
    except (AttributeError, OSError):
        pass
    return False


def already_running(desk_port: int) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never through a proxy
    try:
        with opener.open(f"http://127.0.0.1:{desk_port}/desk/status", timeout=3) as reply:
            return "version" in json.load(reply)
    except (OSError, ValueError):
        return False


def restart_command(argv: list[str]) -> list[str]:
    """The command that starts this program again, without opening the pairing page."""
    args = [arg for arg in argv if arg != "--quiet"] + ["--quiet"]
    launcher = Path(sys.argv[0])
    if launcher.name == "__main__.py" or not launcher.is_file():  # started as: python -m titip
        return [sys.executable, "-m", "titip", *args]
    return [sys.executable, str(launcher.resolve()), *args]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="titip", description="Your Outlook inbox on your phone, served by your own PC.")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "unpair"])
    parser.add_argument("--demo", action="store_true", help="invented mail instead of Outlook")
    parser.add_argument("--http", action="store_true", help="plain HTTP to the phone (not recommended)")
    parser.add_argument("--quiet", action="store_true", help="do not open the pairing page (used at Windows start-up)")
    parser.add_argument("--home", type=Path, help="where paired devices and the certificate live (default ~/.titip)")
    parser.add_argument("--phone-port", type=int, default=PHONE_PORT)
    parser.add_argument("--desk-port", type=int, default=DESK_PORT)
    parser.add_argument("--version", action="version", version=__version__)
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)
    stay_responsive()  # first: a start at lowered priority on a full PC otherwise crawls through all that follows

    home = args.home or Path.home() / (".titip-demo" if args.demo else ".titip")
    home.mkdir(parents=True, exist_ok=True)

    if args.command == "unpair":
        from .pairing import DeviceStore

        DeviceStore(home / "devices.json").clear()
        say("Every phone is unpaired.")
        return 0

    desk_url = f"http://127.0.0.1:{args.desk_port}/"
    if already_running(args.desk_port):
        say(f"Titip is already running: {desk_url}")
        if not args.quiet:
            webbrowser.open(desk_url)
        return 0

    if args.demo:
        from .mailbox import DemoMailbox

        mailbox = DemoMailbox()
        autopilot = Autopilot(home, DemoGmail(), demo_ask, interval=60)
        updater = Updater(home, root=None)  # the demo never changes its own files
    else:
        from . import gmail
        from .mailbox import OutlookMailbox

        mailbox = OutlookMailbox()
        autopilot = Autopilot(home, gmail.ChromeGmail() if gmail.available() else None)
        updater = Updater(home, root=ROOT)

    stay_responsive()
    app = Titip(
        mailbox, home, phone_port=args.phone_port, desk_port=args.desk_port, https=not args.http, autopilot=autopilot,
        updater=updater,
    )
    try:
        app.start()
    except OSError:
        (home / "titip.log").write_text(traceback.format_exc(), encoding="utf-8")
        say(f"Could not start: port {args.phone_port} or {args.desk_port} is in use. Details: {home / 'titip.log'}")
        return 1
    say(f"Titip {__version__} is running. Pair a phone here: {app.desk_url}")
    for address in app.addresses():
        say(f"  phone address: {address}")
    if not args.quiet:
        webbrowser.open(app.desk_url)
    try:
        seconds = 0
        while not app.quit.wait(1):  # "Stop Titip" on the PC page sets it; the short wait keeps Ctrl+C working
            seconds += 1
            if seconds % 2 == 0:  # often: at lowered priority on a full PC, Titip may not run again for a minute
                stay_responsive()
        time.sleep(0.3)  # let the answer to that click reach the browser
    except KeyboardInterrupt:
        pass
    app.stop()
    if app.restarting:  # a new version is in place: start it, and go back if it does not come up
        outcome = relaunch(restart_command(argv), ROOT, lambda: already_running(args.desk_port), updater.undo)
        with (home / "update.log").open("a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {__version__} -> {updater.pending[:7] or 'previous'}: {outcome}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
