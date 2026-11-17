"""Start Titip:  python -m titip            (serve your Outlook mail to paired phones)
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
from .server import DESK_PORT, PHONE_PORT, Titip


def say(text: str) -> None:
    if sys.stdout is not None:  # pythonw has no console
        print(text, flush=True)


def already_running(desk_port: int) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never through a proxy
    try:
        with opener.open(f"http://127.0.0.1:{desk_port}/desk/status", timeout=3) as reply:
            return "version" in json.load(reply)
    except (OSError, ValueError):
        return False


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
    args = parser.parse_args(argv)

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
    else:
        from .mailbox import OutlookMailbox

        mailbox = OutlookMailbox()

    app = Titip(mailbox, home, phone_port=args.phone_port, desk_port=args.desk_port, https=not args.http)
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
        while not app.quit.wait(1):  # "Stop Titip" on the PC page sets it; the short wait keeps Ctrl+C working
            pass
        time.sleep(0.3)  # let the answer to that click reach the browser
    except KeyboardInterrupt:
        pass
    app.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
