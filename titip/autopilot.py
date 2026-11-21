"""Autopilot: reads new Gmail, answers what is safe to answer, keeps the rest for you.

One thread does all the work, one mail at a time:

    new mail -> machine sender? leave it -> ask the model (titip.brain) -> rules -> send, keep or leave

Every step is written to a journal the Autopilot page shows live. What it may never do is fixed in
code, not in a prompt: it answers only the sender of the mail it just read, sends at most
MAX_AUTO_PER_DAY answers per mailbox and day, answers a thread at most once per THREAD_QUIET_S, and
never forwards, deletes, opens a link or opens an attachment. Mail that needs a decision waits in
"Waiting for you" with a draft; Send there sends your wording.

State lives in <home>/autopilot.json (settings, journal, waiting mail). Mail it leaves or keeps for
you is marked unread again in Gmail, so it still looks new; only mail it answered stays read. With
the setting "leftRead" (command left, read=True) the mail it leaves alone stays read as well: then
only what waits for you, or is looked at again, is unread.

A mail it could not judge (the model did not answer, or the sender's address was not on the page) is
read again at the next check, MAX_TRIES times in all, before it is kept for you with that reason.
"""

from __future__ import annotations

import datetime as dt
import json
import queue
import re
import secrets
import threading
import time
from pathlib import Path

from . import brain

INTERVAL_S = 10 * 60
MAX_PER_CHECK = 3  # mails read per mailbox and check; the rest wait for the next one
MAX_AUTO_PER_DAY = 8
THREAD_QUIET_S = 12 * 3600
MAX_EVENTS = 150
MAX_SEEN = 2000
MAX_TRIES = 3  # looks at one mail that could not be judged, before it is kept for the owner
MAX_TRIED = 200  # mails remembered as "being tried again"; the oldest are forgotten first
MODES = ("auto", "review")  # review: every answer waits for you
# Your own notes to yourself and no-reply senders are never opened: there is nobody to answer.
UNREAD_QUERY = "in:inbox is:unread category:primary newer_than:2d -from:me -from:noreply -from:no-reply"


class SourceError(Exception):
    """The mailbox could not be read or the answer could not be sent. The message is shown as is."""


def thread_key(address: str, subject: str) -> str:
    subject = re.sub(r"^\s*((re|fwd?|aw|sv|bls|balas)\s*:\s*)+", "", subject or "", flags=re.I)
    return f"{(address or '').lower()}|{re.sub(r'\s+', ' ', subject).strip().lower()}"


def find_query(address: str, subject: str) -> str:
    words = re.sub(r'["(){}]', " ", thread_key("", subject).split("|", 1)[1])
    return f'in:anywhere from:({address}) subject:("{words}") newer_than:30d'


class Autopilot:
    def __init__(self, home: Path, source=None, ask=None, clock=time.time, interval: int = INTERVAL_S):
        self.home = Path(home)
        self.path = self.home / "autopilot.json"
        self.profile_path = self.home / "autopilot_profile.md"
        self.source = source  # None: no mailbox on this PC (the page says so)
        self.ask = ask
        self.clock = clock
        self.interval = interval
        self.on_change = None  # called with nothing after every change; the remote link listens
        self.busy = ""
        self._lock = threading.RLock()
        self._commands: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state = self._load()

    # ------------------------------------------------------------ state
    def _load(self) -> dict:
        state = {
            "on": False, "mode": "auto", "accounts": [0], "seen": {}, "held": [], "events": [],
            "sent": {}, "answered": {}, "lastCheck": 0, "mailboxes": {}, "started": {}, "tries": {},
            "leftRead": False,
        }
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                state.update({key: saved[key] for key in state if key in saved})
        except (OSError, ValueError):
            pass
        if state["mode"] not in MODES:
            state["mode"] = "auto"
        from .gmail import row_key  # here, because gmail imports this module

        for name in ("seen", "tries"):  # keys saved before 0.10.2 kept the time of a mail with an attachment
            state[name] = {row_key(key): value for key, value in state[name].items()}
        return state

    def _save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(self.path)

    def _changed(self) -> None:
        with self._lock:
            self._save()
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass  # the phone link is optional; mail handling goes on

    def profile(self) -> str:
        try:
            text = self.profile_path.read_text(encoding="utf-8").strip()
            return text or brain.DEFAULT_PROFILE
        except OSError:
            return brain.DEFAULT_PROFILE

    def _today(self) -> str:
        return dt.datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d")

    def _note(self, kind: str, text: str, mailbox: str = "", who: str = "", subject: str = "") -> None:
        with self._lock:
            self.state["events"].append({
                "id": secrets.token_hex(4), "at": int(self.clock()), "kind": kind, "text": text,
                "mailbox": mailbox, "who": who, "subject": subject,
            })
            del self.state["events"][:-MAX_EVENTS]
        self._changed()

    def _set_busy(self, text: str) -> None:
        self.busy = text
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def snapshot(self) -> dict:
        with self._lock:
            state = self.state
            return {
                "available": self.source is not None,
                "on": state["on"],
                "mode": state["mode"],
                "leftRead": bool(state["leftRead"]),
                "busy": self.busy,
                "lastCheck": state["lastCheck"],
                "nextCheck": state["lastCheck"] + self.interval if state["on"] else 0,
                "mailboxes": [state["mailboxes"].get(str(index), f"Gmail account {index + 1}") for index in state["accounts"]],
                "sentToday": sum(count for key, count in state["sent"].items() if key.startswith(self._today())),
                "dailyLimit": MAX_AUTO_PER_DAY,
                "held": [{key: item[key] for key in item if key != "row"} for item in state["held"]],
                "events": list(reversed(state["events"])),
                "now": int(self.clock()),
            }

    # ------------------------------------------------------------ commands (any thread)
    def command(self, name: str, **details) -> dict:
        """switch(on), mode(mode), left(read), check, send(id, text), drop(id). Slow ones run on the worker thread."""
        if name == "switch":
            with self._lock:
                self.state["on"] = bool(details.get("on"))
            self._note("switch", "Autopilot is on." if self.state["on"] else "Autopilot is paused. Nothing is read or sent.")
            if self.state["on"]:
                self._commands.put(("check", {}))
        elif name == "mode" and details.get("mode") in MODES:
            with self._lock:
                self.state["mode"] = details["mode"]
            self._note("switch", "Answers go out on their own." if details["mode"] == "auto" else "Every answer waits for you.")
        elif name == "left" and isinstance(details.get("read"), bool):
            with self._lock:
                self.state["leftRead"] = details["read"]
            self._note("switch", "Mail that needs no answer stays read." if details["read"]
                       else "Mail that needs no answer is marked unread again.")
        elif name == "drop":
            item = self._take(str(details.get("id", "")))
            if item:
                self._note("dropped", "You chose not to answer.", item["mailbox"], item["sender"], item["subject"])
        elif name == "send":
            with self._lock:
                item = next((held for held in self.state["held"] if held["id"] == details.get("id")), None)
                if item is None:
                    raise ValueError("That mail is no longer waiting.")
                text = brain.clean_reply(details.get("text") or item["draft"])
                if not text:
                    raise ValueError("Write an answer first.")
                item["sending"] = True
            self._changed()
            self._commands.put(("send", {"id": item["id"], "text": text}))
        elif name == "check":
            self._commands.put(("check", {}))
        else:
            raise ValueError("Unknown request.")
        return self.snapshot()

    def _take(self, item_id: str) -> dict | None:
        with self._lock:
            for index, item in enumerate(self.state["held"]):
                if item["id"] == item_id:
                    return self.state["held"].pop(index)
        return None

    # ------------------------------------------------------------ the worker thread
    def start(self) -> None:
        if self._thread is None and self.source is not None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="titip-autopilot")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                name, details = self._commands.get(timeout=2)
            except queue.Empty:
                name, details = "", {}
            try:
                if name == "send":
                    self.send_held(details["id"], details["text"])
                elif name == "check" or (self.state["on"] and self.clock() - self.state["lastCheck"] >= self.interval):
                    self.check()
            except Exception as error:  # one bad mail or a closed browser must not end the thread
                self._note("error", f"Stopped this round: {type(error).__name__}.")
            finally:
                self._set_busy("")

    def check(self) -> None:
        """One round: every mailbox, at most MAX_PER_CHECK unread mails each."""
        if not self.state["on"] or self.source is None:
            return
        with self._lock:
            self.state["lastCheck"] = int(self.clock())
        for index in list(self.state["accounts"]):
            self._check_mailbox(index)
        self._changed()

    def _check_mailbox(self, index: int) -> None:
        name = self.state["mailboxes"].get(str(index), f"Gmail account {index + 1}")
        self._set_busy(f"Looking for new mail in {name}")
        try:
            mailbox = self.source.open(index, UNREAD_QUERY)
            with self._lock:
                self.state["mailboxes"][str(index)] = mailbox
            rows = [row for row in self.source.unread() if row.key not in self.state["seen"]]
            if str(index) not in self.state["started"]:
                # The first look at a mailbox: what was already unread stays yours. Only mail that
                # arrives from now on is opened, so switching Autopilot on never answers a backlog.
                with self._lock:
                    self.state["started"][str(index)] = int(self.clock())
                    for row in rows:
                        self.state["seen"][row.key] = int(self.clock())
                waiting = f"{len(rows)} unread mails were" if len(rows) != 1 else "1 unread mail was"
                self._note("left", f"Watching {mailbox} from now on. {waiting} already there and left for you, unopened.", mailbox)
                return
            for position in range(min(len(rows), MAX_PER_CHECK)):
                if position:  # the list was left to read a mail: open it again and find the row by its text
                    self.source.open(index, UNREAD_QUERY)
                    current = {row.key: row for row in self.source.unread()}
                    row = current.get(rows[position].key)
                    if row is None:
                        continue
                else:
                    row = rows[0]
                self._handle(index, mailbox, row)
        except SourceError as error:
            self._note("error", str(error), name)
        finally:
            try:
                self.source.close()
            except Exception:
                pass

    def _handle(self, index: int, mailbox: str, row) -> None:
        self._set_busy(f"Reading a mail in {mailbox}")
        mail = self.source.read(row, mailbox)
        with self._lock:
            self.state["seen"][row.key] = int(self.clock())
            for key in sorted(self.state["seen"], key=self.state["seen"].get)[:-MAX_SEEN]:
                del self.state["seen"][key]
        if mail.address.lower() == mailbox.lower():
            self._leave()
            return self._note("left", "Your own message, nothing to answer.", mailbox, mail.sender, mail.subject)
        self._set_busy(f"Thinking about a mail from {mail.sender or mail.address}")
        decision = brain.decide(mail, self.profile(), self.ask)
        if self._try_again(row.key, decision):
            self._keep_unread()
            tries = self.state["tries"][row.key]
            return self._note("retry", f"{decision.reason} Trying again at the next check (try {tries} of {MAX_TRIES}).",
                              mailbox, mail.sender, mail.subject)
        if decision.action == brain.IGNORE:
            self._leave()
            return self._note("left", f"No answer needed: {decision.reason}.", mailbox, mail.sender, mail.subject)
        if decision.action == brain.REPLY:
            blocked = self._blocked(index, mail)
            if not blocked:
                self._set_busy(f"Answering {mail.sender or mail.address}")
                try:
                    self.source.reply(mail, decision.reply)
                except SourceError as error:
                    blocked = f"Sending failed: {error}"
                else:
                    with self._lock:
                        day = f"{self._today()}|{index}"
                        self.state["sent"] = {key: count for key, count in self.state["sent"].items() if key[:10] == self._today()}
                        self.state["sent"][day] = self.state["sent"].get(day, 0) + 1
                        self.state["answered"][thread_key(mail.address, mail.subject)] = int(self.clock())
                    return self._note("sent", decision.reply, mailbox, mail.sender, mail.subject)
            decision = brain.Decision(brain.HOLD, blocked, decision.summary, decision.reply)
        self._keep_unread()
        with self._lock:
            self.state["held"].append({
                "id": secrets.token_hex(6), "at": int(self.clock()), "account": index, "mailbox": mailbox,
                "sender": mail.sender, "address": mail.address, "subject": mail.subject,
                "summary": decision.summary, "reason": decision.reason, "draft": decision.reply,
            })
        self._note("held", decision.reason, mailbox, mail.sender, mail.subject)

    def _try_again(self, key: str, decision) -> bool:
        """True when this mail was not judged and gets another look at the next check. It is then
        forgotten as seen, so the next check opens it again; after MAX_TRIES looks it is kept instead."""
        with self._lock:
            tries = self.state["tries"]
            if not decision.retry:
                tries.pop(key, None)
                return False
            count = tries.pop(key, 0) + 1
            if count >= MAX_TRIES:
                return False
            tries[key] = count  # put back last, so the oldest entries are the first to go
            for old in list(tries)[:-MAX_TRIED]:
                del tries[old]
            self.state["seen"].pop(key, None)
            return True

    def _leave(self) -> None:
        """A mail that needs no answer: unread again, unless you chose that it stays read ("leftRead")."""
        if not self.state["leftRead"]:
            self._keep_unread()

    def _keep_unread(self) -> None:
        """What is left for you should still look new in Gmail. Best effort: the mail is not opened twice either way."""
        keep = getattr(self.source, "keep_unread", None)
        if keep is None:
            return
        try:
            keep()
        except Exception:
            pass

    def _blocked(self, index: int, mail) -> str:
        """Why an answer the rules allow still waits for the owner; '' when it may go."""
        if self.state["mode"] == "review":
            return "You asked to see every answer first."
        if self.state["sent"].get(f"{self._today()}|{index}", 0) >= MAX_AUTO_PER_DAY:
            return f"Today's limit of {MAX_AUTO_PER_DAY} automatic answers is used up."
        last = self.state["answered"].get(thread_key(mail.address, mail.subject), 0)
        if self.clock() - last < THREAD_QUIET_S:
            return "This conversation already got an automatic answer today."
        return ""

    def send_held(self, item_id: str, text: str) -> None:
        """Send the owner's wording to the sender of a waiting mail."""
        with self._lock:
            item = next((held for held in self.state["held"] if held["id"] == item_id), None)
        if item is None or self.source is None:
            return
        self._set_busy(f"Sending your answer to {item['sender'] or item['address']}")
        try:
            self.source.open(item["account"], find_query(item["address"], item["subject"]))
            rows = self.source.unread(any_state=True)
            if not rows:
                raise SourceError("Could not find that mail in Gmail any more.")
            mail = self.source.read(rows[0], item["mailbox"])
            if mail.address.lower() == item["mailbox"].lower():
                raise SourceError("You already answered this conversation in Gmail. Nothing was sent.")
            if mail.address.lower() != item["address"].lower():
                raise SourceError("The mail found in Gmail is from someone else. Nothing was sent.")
            self.source.reply(mail, text)
        except SourceError as error:
            with self._lock:
                item.pop("sending", None)
                item["error"] = str(error)
            self._note("error", f"Not sent. {error}", item["mailbox"], item["sender"], item["subject"])
            return
        finally:
            try:
                self.source.close()
            except Exception:
                pass
        self._take(item_id)
        with self._lock:
            self.state["answered"][thread_key(item["address"], item["subject"])] = int(self.clock())
        self._note("sent", text, item["mailbox"], item["sender"], item["subject"])


# ------------------------------------------------------------ invented mail, for --demo and the tests

class DemoRow:
    def __init__(self, key: str, text: str):
        self.key, self.text = key, text


DEMO_MAIL = (
    ("Field Notes Weekly", "newsletter@fieldnotes.example", "Issue 48: paper sizes",
     "This week: why A4 won, and three notebooks worth their price. Unsubscribe any time."),
    ("Tomas Lind", "tomas@lind.example", "Can I use your template?",
     "Hi, I found your label template. Is it free to use for a school project? Thanks, Tomas"),
    ("Harbor Print Co.", "billing@harborprint.example", "Invoice 2291 is due",
     "Please transfer the payment for invoice 2291 to our bank account this week."),
)


class DemoGmail:
    """Three invented mails that arrive one per look, so every path of the page can be tried."""

    mailbox = "nadia@mail.example"

    def __init__(self):
        self.arrived = 0
        self.sent: list[tuple[str, str]] = []

    def open(self, account: int, query: str) -> str:
        if query == UNREAD_QUERY:
            self.arrived = min(self.arrived + 1, len(DEMO_MAIL) + 1)
            self.rows = [DemoRow(f"demo-{n}", f"unread, {DEMO_MAIL[n][0]} , {DEMO_MAIL[n][2]}") for n in range(max(0, self.arrived - 1))]
        else:
            self.rows = [DemoRow(f"demo-{n}", mail[2]) for n, mail in enumerate(DEMO_MAIL) if mail[1] in query]
        return self.mailbox

    def unread(self, any_state: bool = False) -> list:
        return list(self.rows)

    def read(self, row, mailbox: str) -> brain.Mail:
        sender, address, subject, text = DEMO_MAIL[int(row.key.split("-")[1])]
        return brain.Mail(mailbox, sender, address, subject, text)

    def reply(self, mail: brain.Mail, text: str) -> None:
        self.sent.append((mail.address, text))

    def keep_unread(self) -> bool:
        return True

    def close(self) -> None:
        pass


def demo_ask(prompt: str) -> str:
    """Stands in for the model in --demo: a fixed answer per invented mail."""
    if "Invoice 2291" in prompt:
        return json.dumps({"action": "reply", "reason": "a short answer helps", "summary": "They ask you to pay invoice 2291.",
                           "reply": "Hello,\n\nThank you for the reminder. I will check the invoice and come back to you.\n\nNadia"})
    if "template" in prompt:
        return json.dumps({"action": "reply", "reason": "a simple question about a public project",
                           "summary": "Tomas asks if the label template is free for a school project.",
                           "reply": "Hi Tomas,\n\nYes, it is free to use. Good luck with the project.\n\nNadia"})
    return json.dumps({"action": "ignore", "reason": "a newsletter", "summary": "", "reply": ""})
