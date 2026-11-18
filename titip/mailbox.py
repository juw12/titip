"""Mailboxes Titip can read and send from.

OutlookMailbox drives the classic Outlook desktop app on this PC through COM, so it uses the account
Outlook is already signed in to: no password, token or mail server setting is stored by Titip.
DemoMailbox serves the invented mail in web/demo.json (tests, screenshots, the public demo).

Both expose the same six calls: state, list, get, attachment, send, people.
"""

from __future__ import annotations

import collections
import datetime as dt
import hashlib
import html
import json
import queue
import itertools
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

WEB = Path(__file__).parent / "web"
FOLDERS = ("inbox", "sent", "drafts")
MAX_LIST = 500  # how far back the phone can page in one folder
MAX_BODY = 200_000
MAX_HTML = 1_500_000
STATE_SECONDS = 300  # counting every folder of every data file takes Outlook seconds, and its own window waits meanwhile
READ_SECONDS = 20  # how long the phone waits for Outlook before it is told "busy"
SEND_SECONDS = 120
REFRESH_SECONDS = 300  # the PC page's own look at Outlook runs in the background and may wait this long

# Copies kept in memory only (never on disk, gone when Titip stops), so a tap on the phone does not
# have to wait for Outlook's one busy thread: the first page of each folder and the messages on it.
ASK_SECONDS = 120  # a read stays in Outlook's queue this long, also after the phone stopped waiting
QUICK_SECONDS = 3  # with a copy in hand, wait only this long for a newer answer
LIST_FRESH_SECONDS = 10  # a first page this new is shown without asking Outlook again
KEEP_MESSAGES = 240
KEEP_CHARACTERS = 40_000_000
KEEP_ROWS = 3000  # listed messages remembered, to find one again after it was moved
MAX_ASKING = 12  # reads waiting for Outlook at one time
PAGE = 40  # the phone's page size, until it asks for another
WARM_SECONDS = 600  # keep the copies up to date this long after the phone last asked for anything
WARM_EVERY = 30
WARM_LONG_SECONDS = 8 * 3600  # then go on slowly this long, so new mail is there when the phone is picked up again
WARM_LONG_EVERY = 300
WARM_PAUSE = 0.2  # between two questions, so Outlook's own window gets its turn
WARM_MESSAGES = {"inbox": 40, "sent": 20, "drafts": 10}  # newest messages read ahead in each folder
WANTED = 100  # rows of older pages and searches the phone was shown: read ahead as well
FIND_AGAIN_ROWS = 100

# Who the composer on the phone suggests while a name is typed: the people in the mail that was read
# ahead, in memory only like the copies above.
PEOPLE_KEEP = 3000
PEOPLE_SHOWN = 400
PEOPLE_RECIPIENTS = 40  # of one message; a mail to a whole department does not fill the list
PEOPLE_SENT = 100  # newest sent messages looked at only for who they went to
PEOPLE_PER_ROUND = 10
PEOPLE_PAUSE = 1.0

PROP = "http://schemas.microsoft.com/mapi/proptag/"
P_DELIVERED = PROP + "0x0E060040"  # PR_MESSAGE_DELIVERY_TIME (UTC)
P_SUBMITTED = PROP + "0x00390040"  # PR_CLIENT_SUBMIT_TIME (UTC)
P_MODIFIED = PROP + "0x30080040"  # PR_LAST_MODIFICATION_TIME (UTC)
P_HAS_ATTACH = PROP + "0x0E1B000B"
P_DISPLAY_TO = PROP + "0x0E04001F"
P_PREVIEW = PROP + "0x1000001F"  # PR_BODY: a table returns only its first 255 characters
P_SMTP = PROP + "0x39FE001F"
P_SENDER_SMTP = PROP + "0x5D01001F"
P_ATTACH_HIDDEN = PROP + "0x7FFE000B"

OL_INBOX, OL_SENT, OL_DRAFTS = 6, 5, 16
OL_DISCARD = 1
RPC_BUSY = (-2147418111, -2147417846)  # RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER


class MailError(Exception):
    """A problem worth showing on the phone as-is (bad recipient, mail not found, Outlook closed)."""


class OutlookBusy(MailError):
    """Outlook did not get to our call in time. `started` tells whether it had begun working on it."""

    def __init__(self, started: bool = False):
        super().__init__("Outlook on your PC is busy right now. Try again in a moment.")
        self.started = started


def text_to_html(text: str) -> str:
    """What the phone typed, as the HTML block that goes above the signature or the quoted thread."""
    lines = html.escape(text.replace("\r\n", "\n").strip("\n")).split("\n")
    body = "<br>".join(lines)
    return f'<div style="font-family:Calibri,Arial,sans-serif;font-size:11pt">{body}</div><br>'


def insert_into_html(existing: str, block: str) -> str:
    """Put `block` right after <body ...> so signature and quoted mail stay below it."""
    match = re.search(r"<body[^>]*>", existing or "", flags=re.IGNORECASE)
    if not match:
        return block + (existing or "")
    return existing[: match.end()] + block + existing[match.end():]


def split_addresses(value) -> list[str]:
    """What was typed in To or Cc, one entry each. Of "Rina Wijaya <rina@example.com>" only the address is kept."""
    if isinstance(value, str):
        value = re.findall(r"[^;,\n<]*<[^<>]*>|[^;,\n]+", value)
    found = []
    for part in value or []:
        part = str(part or "").strip()
        named = re.search(r"<([^<>]*)>\s*$", part)
        if named:
            part = named.group(1).strip()
        if part:
            found.append(part)
    return found


def outlook_running() -> bool:
    """Is Outlook open on this PC right now? Asked of Windows, not of Outlook: asking Outlook would start it."""
    if sys.platform != "win32":
        return False
    try:
        listed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq OUTLOOK.EXE", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15, creationflags=0x08000000,  # no console window
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "outlook.exe" in listed.lower()


def clean_send_request(payload: dict) -> dict:
    """Validate what the phone posted; raises MailError with a message for the composer."""
    mode = payload.get("mode", "new")
    action = payload.get("action", "send")
    if mode not in ("new", "reply", "replyAll", "forward", "draft"):
        raise MailError("Unknown kind of message.")
    if action not in ("send", "draft"):
        raise MailError("Unknown action.")
    request = {
        "mode": mode,
        "action": action,
        "id": str(payload.get("id") or ""),
        "to": split_addresses(payload.get("to")),
        "cc": split_addresses(payload.get("cc")),
        "subject": str(payload.get("subject") or "").strip()[:255],
        "body": str(payload.get("body") or "")[:MAX_BODY],
    }
    if mode != "new" and not request["id"]:
        raise MailError("The original message is missing.")
    if action == "send":
        if not request["to"] and mode not in ("reply", "replyAll"):
            raise MailError("Add at least one recipient.")
        if not request["body"].strip() and mode != "forward":
            raise MailError("Write a message first.")
    return request


# ============================================================ Outlook (COM)

class _Job:
    __slots__ = ("fn", "args", "done", "state")

    def __init__(self, fn, args):
        self.fn, self.args = fn, args
        self.done: queue.Queue = queue.Queue(maxsize=1)
        self.state = "waiting"  # -> "running" -> finished, or -> "dropped" when the caller gave up first


class _ComThread:
    """Outlook's COM objects must stay on one thread; every call is queued to this one.

    Outlook answers one caller at a time. While it is busy (a big move, a dialog, another add-in) calls
    wait here. A caller that gives up drops its job, so a busy spell does not leave a pile of stale
    work behind, and above all a send that was given up on is never carried out later.
    """

    def __init__(self):
        self._jobs: queue.PriorityQueue = queue.PriorityQueue()  # (0 for a send, in order of arrival, job)
        self._arrival = itertools.count()
        self._lock = threading.Lock()
        self._running_since: float | None = None
        threading.Thread(target=self._run, daemon=True, name="titip-outlook").start()

    def _run(self):
        import pythoncom

        pythoncom.CoInitialize()
        while True:
            job = self._jobs.get()[2]
            with self._lock:
                if job.state == "dropped":
                    continue
                job.state = "running"
                self._running_since = time.monotonic()
            try:
                job.done.put((True, job.fn(*job.args)))
            except BaseException as error:  # handed back to the caller's thread
                job.done.put((False, error))
            finally:
                self._running_since = None

    def stuck_for(self) -> float:
        """Seconds Outlook has been working on (or ignoring) the call it has now; 0 when idle."""
        since = self._running_since
        return time.monotonic() - since if since is not None else 0.0

    def call(self, fn, *args, timeout: float = READ_SECONDS, first: bool = False):
        """`first` puts the call ahead of everything that waits: a send does not queue behind reading ahead."""
        if self.stuck_for() >= timeout:  # Outlook has been silent longer than this caller would wait anyway
            raise OutlookBusy(False)
        job = _Job(fn, args)
        self._jobs.put((0 if first else 1, next(self._arrival), job))
        try:
            ok, value = job.done.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                started = job.state == "running"
                job.state = "dropped"
            raise OutlookBusy(started)
        if ok:
            return value
        raise value


class _Ask:
    """One question to Outlook that any number of callers may be waiting for."""

    __slots__ = ("done", "value", "error")

    def __init__(self):
        self.done = threading.Event()
        self.value = None
        self.error: Exception | None = None


def _utc_iso(value) -> str:
    """A COM date read through a MAPI property tag is UTC; pywin32 hands it over as a datetime."""
    try:
        if value.year < 1980 or value.year > 4000:  # Outlook's "no date" is 4501-01-01
            return ""
        return dt.datetime(value.year, value.month, value.day, value.hour, value.minute, value.second).isoformat() + "Z"
    except (AttributeError, ValueError):
        return ""


class OutlookMailbox:
    demo = False

    def __init__(self, read_ahead: bool = True):
        self._read_ahead = read_ahead
        self._thread: _ComThread | None = None
        self._app = None
        self._ns = None
        self._stores: dict[str, str] = {}  # short key -> StoreID
        self._folders: dict[str, list] = {}
        self._state_cache: tuple[float, dict] | None = None
        self._who: tuple[str, str] | None = None  # (address, name): asked once per connection to Outlook
        self._state_problem: MailError | None = None
        self._refresh: threading.Thread | None = None
        self._refresh_lock = threading.Lock()
        # copies in memory (see the constants at the top); every change happens under _lock
        self._lock = threading.Lock()
        self._asking: dict[tuple, _Ask] = {}
        self._lists: dict[tuple, tuple[float, str, dict]] = {}  # (folder, limit) -> (monotonic, UTC time, page)
        self._messages: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self._kept_characters = 0
        self._rows: collections.OrderedDict[str, tuple] = collections.OrderedDict()  # id -> (folder, time, subject, from)
        self._moved: dict[str, str] = {}  # id the phone still holds -> id the message has now
        self._page = PAGE
        self._phone_at = 0.0
        self._warmer: threading.Thread | None = None
        self._wanted: collections.OrderedDict[str, None] = collections.OrderedDict()  # ids shown further down
        self._nudge = threading.Event()  # wakes the reading ahead before its next round is due
        self._people: dict[str, list] = {}  # address in lower case -> [name, address, weight]
        self._people_counted: collections.OrderedDict[str, None] = collections.OrderedDict()  # message ids
        self._people_rows: list[str] | None = None  # sent messages still to look at for who they went to

    # every public method hops to the COM thread and maps COM failures to MailError
    def _do(self, fn, *args, timeout: float = READ_SECONDS, first: bool = False):
        import pythoncom

        def guarded():
            try:
                self._connect()
                return fn(*args)
            except pythoncom.com_error as error:
                if error.hresult in RPC_BUSY:  # Outlook is showing a dialog or serving another program
                    raise OutlookBusy()
                self._app = None  # Outlook was closed or restarted: reconnect on the next call
                self._folders.clear()
                detail = ""
                if error.excepinfo and error.excepinfo[2]:
                    detail = str(error.excepinfo[2]).strip()
                raise MailError(detail or "Outlook on your PC did not answer. Open Outlook and try again.")

        if self._thread is None:
            self._thread = _ComThread()
        return self._thread.call(guarded, timeout=timeout, first=first)

    def _connect(self):
        if self._app is not None:
            return
        import win32com.client

        self._app = win32com.client.Dispatch("Outlook.Application")
        self._ns = self._app.GetNamespace("MAPI")
        self._stores.clear()
        self._folders.clear()
        self._who = None

    def _store_key(self, store_id: str) -> str:
        key = hashlib.sha1(store_id.encode("ascii", "replace")).hexdigest()[:10]
        self._stores[key] = store_id
        return key

    def _mail_folders(self, name: str) -> list:
        """The default folder plus the same-named folder of every other store (archive .pst files).

        Many people (and mail rules) move mail out of the live Inbox into a data file; reading only
        the default Inbox would show an empty phone.
        """
        if name in self._folders:
            return self._folders[name]
        default_id = {"inbox": OL_INBOX, "sent": OL_SENT, "drafts": OL_DRAFTS}[name]
        default = self._ns.GetDefaultFolder(default_id)
        found = [default]
        if name != "drafts":
            wanted = default.Name.lower()
            default_store = default.StoreID
            for store in self._ns.Stores:
                try:
                    if store.StoreID == default_store:
                        continue
                    for folder in store.GetRootFolder().Folders:
                        if folder.Name.lower() == wanted:
                            found.append(folder)
                            break
                except Exception:  # a store that is offline or locked: skip it, keep the rest
                    continue
        order = {"inbox": "[ReceivedTime]", "sent": "[SentOn]", "drafts": "[LastModificationTime]"}[name]

        def newest(folder) -> str:
            try:
                table = folder.GetTable()
                table.Sort(order, True)
                table.Columns.RemoveAll()
                table.Columns.Add(order.strip("[]"))
                return _utc_iso(table.GetArray(1)[0][0]) if table.GetRowCount() else ""
            except Exception:
                return ""

        found[1:] = sorted(found[1:], key=newest, reverse=True)  # newest archive first: searches can stop early
        self._folders[name] = found
        return found

    # -------------------------------------------------- one question per thing, and the copies it leaves
    def _shared(self, key: tuple, fn, *args, wait: float):
        """Ask Outlook once for `key`, however many callers want it, and wait up to `wait` seconds.

        The question stays in Outlook's queue for ASK_SECONDS even when every caller has given up, and
        its answer is kept as a copy. So while Outlook is busy the phone's next try does not start
        again at the back of the queue: it gets the answer the moment Outlook gives it.
        Reads only. A send never comes through here.
        """
        with self._lock:
            ask = self._asking.get(key)
            if ask is None:
                if len(self._asking) >= MAX_ASKING:
                    raise OutlookBusy()
                ask = self._asking[key] = _Ask()
                threading.Thread(target=self._answer, args=(key, ask, fn, args), daemon=True, name="titip-ask").start()
        if not ask.done.wait(wait):
            raise OutlookBusy()
        if ask.error is not None:
            raise ask.error
        return ask.value

    def _answer(self, key: tuple, ask: _Ask, fn, args: tuple) -> None:
        try:
            ask.value = self._do(fn, *args, timeout=ASK_SECONDS)
            self._keep(key, ask.value)
        except MailError as error:
            ask.error = error
        except Exception:
            ask.error = MailError("Outlook could not be reached on this PC.")
        finally:
            with self._lock:
                self._asking.pop(key, None)
            ask.done.set()

    def _keep(self, key: tuple, value: dict) -> None:
        with self._lock:
            if key[0] == "list":
                stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                self._lists[key[1:]] = (time.monotonic(), stamp, value)
                return
            self._drop_message(key[1])
            self._drop_message(value["id"])
            size = len(value.get("html") or "") + len(value.get("body") or "")
            if size > KEEP_CHARACTERS // 4:
                return  # one huge message is not worth pushing all the others out
            self._messages[value["id"]] = value
            self._kept_characters += size
            while len(self._messages) > KEEP_MESSAGES or self._kept_characters > KEEP_CHARACTERS:
                self._drop_message(next(iter(self._messages)))

    def _drop_message(self, mail_id: str) -> None:  # call with _lock held
        old = self._messages.pop(mail_id, None)
        if old is not None:
            self._kept_characters -= len(old.get("html") or "") + len(old.get("body") or "")

    def _kept_message(self, mail_id: str) -> dict | None:
        with self._lock:
            kept = self._messages.get(self._moved.get(mail_id, mail_id))
            if kept is not None:
                self._messages.move_to_end(kept["id"])
            return kept

    def _phone_asked(self) -> None:
        """A phone is in use: keep the copies up to date for the next few minutes."""
        now = time.monotonic()
        back = now - self._phone_at >= WARM_SECONDS
        self._phone_at = now
        if not self._read_ahead:
            return
        self._start_warmer()
        if back:
            self._nudge.set()  # the slow rounds are minutes apart: look now

    def _start_warmer(self) -> None:
        with self._lock:
            if self._warmer is None or not self._warmer.is_alive():
                self._warmer = threading.Thread(target=self._warm, daemon=True, name="titip-warm")
                self._warmer.start()

    def read_ahead_now(self, running=outlook_running) -> bool:
        """Right after Titip starts: read ahead as if a phone had been put down a while ago.

        The copies live in memory, so a restart (an update, Windows starting) empties them, and the
        phone's first look would wait for Outlook. Only when Outlook is already open: Titip never
        starts it for a phone nobody holds. False when nothing was started.
        """
        if not self._read_ahead or self._phone_at or not running():
            return False
        self._phone_at = time.monotonic() - WARM_SECONDS  # the slow rounds, which end when Outlook is closed
        self._start_warmer()
        return True

    def _want(self, rows: list) -> None:
        """The phone was shown these rows (an older page, a search): read them ahead as well."""
        if not self._read_ahead:
            return
        with self._lock:
            for row in rows:
                self._wanted[row["id"]] = None
                self._wanted.move_to_end(row["id"])
            while len(self._wanted) > WANTED:
                self._wanted.popitem(last=False)
        self._nudge.set()

    def _warm(self) -> None:
        """Read ahead of the phone, so that a tap opens a message from memory instead of waiting for Outlook.

        Starts once a phone asks for something, or with Titip when Outlook is already open (so Titip
        never starts Outlook by itself). While
        the phone is in use a round runs every WARM_EVERY seconds. After WARM_SECONDS without a phone
        the rounds go on WARM_LONG_EVERY seconds apart for WARM_LONG_SECONDS, so mail that arrives in
        the meantime is already there when the phone is picked up again. Those slow rounds end the
        first time Outlook turns out to be closed: it is not opened again for a phone nobody holds.
        """
        draft_read_at: dict[str, str] = {}  # draft id -> its "last changed" time when it was read
        while True:
            self._nudge.clear()
            away = time.monotonic() - self._phone_at
            if away >= WARM_LONG_SECONDS:
                return
            try:
                self._warm_round(draft_read_at)
            except OutlookBusy:
                pass  # try again on the next round
            except MailError:
                if away >= WARM_SECONDS:
                    return
            self._nudge.wait(WARM_EVERY if away < WARM_SECONDS else WARM_LONG_EVERY)

    def _warm_round(self, draft_read_at: dict[str, str]) -> None:
        """The first page of each folder with its newest messages, then what the phone was shown further down.

        One small question at a time with a pause in between. A received or sent message is read
        once; a draft again when it changed.
        """
        for folder in FOLDERS:
            page = self._shared(("list", folder, self._page), self._list, folder, self._page, 0, "", wait=ASK_SECONDS)
            for row in page["mail"][: WARM_MESSAGES[folder]]:
                kept = self._kept_message(row["id"])
                if kept is not None and (folder != "drafts" or draft_read_at.get(row["id"]) == row["time"]):
                    continue
                if self._read_one(row["id"]):
                    draft_read_at[row["id"]] = row["time"]
            time.sleep(WARM_PAUSE)
        while True:
            with self._lock:
                mail_id = next(reversed(self._wanted), None)  # what the phone was shown last comes first
            if mail_id is None:
                break
            if self._kept_message(mail_id) is None:
                self._read_one(mail_id)
            with self._lock:
                self._wanted.pop(mail_id, None)
        self._learn_people()

    def _learn_people(self) -> None:
        """A few more sent messages each round, looked at only for who they went to.

        The people you write to are the ones the composer should suggest, and the newest
        WARM_MESSAGES of Sent do not hold many. One message a second, PEOPLE_PER_ROUND a round,
        PEOPLE_SENT in all, once for as long as Titip runs.
        """
        if self._people_rows is None:
            page = self._do(self._list, "sent", PEOPLE_SENT, 0, "", timeout=ASK_SECONDS)
            self._people_rows = [row["id"] for row in page["mail"][WARM_MESSAGES["sent"]:]]
        for _ in range(PEOPLE_PER_ROUND):
            if not self._people_rows:
                return
            time.sleep(PEOPLE_PAUSE)
            mail_id = self._people_rows.pop(0)  # the newest first
            try:
                self._do(self._note_people, mail_id, timeout=ASK_SECONDS)
            except OutlookBusy:
                self._people_rows.insert(0, mail_id)
                raise
            except MailError:
                pass  # deleted in the meantime

    def _read_one(self, mail_id: str) -> bool:
        """Ask Outlook for one message so its copy is kept. False when it cannot be read; a busy Outlook raises."""
        time.sleep(WARM_PAUSE)
        try:
            self._shared(("item", mail_id), self._get, mail_id, wait=ASK_SECONDS)
        except OutlookBusy:
            raise
        except MailError:
            return False  # deleted in the meantime, or a kind of item that cannot be read
        return True

    # -------------------------------------------------- state
    def _fresh_state(self) -> dict | None:
        cached = self._state_cache
        if cached and time.monotonic() - cached[0] < STATE_SECONDS:
            return cached[1]
        return None

    def state(self, timeout: float = READ_SECONDS) -> dict:
        """Account and folder counts. Kept for minutes: the phone and the PC page both poll it, and each
        count is a question Outlook answers on the thread that also draws its window. A send or a saved
        draft drops the copy, so the counts after it are new."""
        state = self._fresh_state()
        if state is None:
            state = self._do(self._state, timeout=timeout)
            self._state_cache = (time.monotonic(), state)
        return state

    def _refresh_state(self) -> None:
        try:
            self.state(timeout=REFRESH_SECONDS)
            self._state_problem = None
        except MailError as error:
            self._state_problem = error
        except Exception:
            self._state_problem = MailError("Outlook could not be reached on this PC.")

    def quick_state(self, wait: float = 1.0) -> dict:
        """For the page on the PC: answer within about a second, whatever Outlook is doing.

        Outlook is asked in the background. Until it answers, the page gets the last known state
        marked busy, with how long Outlook has been silent.
        """
        state = self._fresh_state()
        if state is not None:
            return state
        with self._refresh_lock:
            if self._refresh is None or not self._refresh.is_alive():
                self._refresh = threading.Thread(target=self._refresh_state, daemon=True, name="titip-state")
                self._refresh.start()
            refresh = self._refresh
        refresh.join(0.05 if self._thread and self._thread.stuck_for() > 2 else wait)
        state = self._fresh_state()
        if state is not None:
            return state
        problem = OutlookBusy() if refresh.is_alive() else self._state_problem
        if problem is not None and not isinstance(problem, OutlookBusy):
            return {"ok": False, "error": str(problem)}
        silent = int(self._thread.stuck_for()) if self._thread else 0
        if self._state_cache:
            return {**self._state_cache[1], "busy": True, "busyFor": silent}
        return {"ok": False, "busy": True, "busyFor": silent, "error": str(OutlookBusy())}

    def _account(self) -> tuple[str, str]:
        """(address, name) of the account Outlook sends from."""
        who = self._who
        if who is None:
            account = ""
            try:
                account = self._ns.Accounts.Item(1).SmtpAddress
            except Exception:
                pass
            who = (account, self._ns.CurrentUser.Name)
            if account:  # an address that could not be read is asked for again next time
                self._who = who
        return who

    def _state(self) -> dict:
        who = self._account()
        return {
            "ok": True,
            "demo": False,
            "account": who[0],
            "name": who[1],
            "counts": {name: sum(f.Items.Count for f in self._mail_folders(name)) for name in FOLDERS},
        }

    # -------------------------------------------------- list
    def list(self, folder: str, limit: int = 40, offset: int = 0, query: str = "") -> dict:
        """One page of a folder. The first page comes from the copy in memory when Outlook is slow.

        A copy older than LIST_FRESH_SECONDS is only a fallback: Outlook is asked first, and when it
        does not answer within QUICK_SECONDS the copy is returned marked `saved`, with its time.
        Searches and older pages always wait for Outlook.
        """
        if folder not in FOLDERS:
            raise MailError("Unknown folder.")
        limit = max(1, min(int(limit), 100))
        self._phone_asked()
        if int(offset) > 0 or (query or "").strip():
            page = self._do(self._list, folder, limit, offset, query)
            self._want(page["mail"])
            return page
        self._page = limit
        kept = self._lists.get((folder, limit))
        if kept and time.monotonic() - kept[0] < LIST_FRESH_SECONDS:
            return kept[2]
        try:
            return self._shared(
                ("list", folder, limit), self._list, folder, limit, 0, "",
                wait=QUICK_SECONDS if kept else READ_SECONDS,
            )
        except OutlookBusy:
            if not kept:
                raise
            return {**kept[2], "saved": True, "savedAt": kept[1]}

    def _list(self, folder: str, limit: int, offset: int, query: str) -> dict:
        if folder not in FOLDERS:
            raise MailError("Unknown folder.")
        limit = max(1, min(int(limit), 100))
        offset = max(0, min(int(offset), MAX_LIST))
        want = offset + limit
        when = {"inbox": P_DELIVERED, "sent": P_SUBMITTED, "drafts": P_MODIFIED}[folder]  # read as UTC
        # Table.Sort rejects property tags ("The parameter is incorrect"), so sort by the built-in name
        order = {"inbox": "[ReceivedTime]", "sent": "[SentOn]", "drafts": "[LastModificationTime]"}[folder]
        table_filter = ""
        words = re.sub(r"[%_\[\]'\"]", " ", query or "").split()
        if words:
            text = " ".join(words)[:80]
            who = "urn:schemas:httpmail:displayto" if folder != "inbox" else "urn:schemas:httpmail:fromname"
            table_filter = (
                f'@SQL=("urn:schemas:httpmail:subject" like \'%{text}%\' OR "{who}" like \'%{text}%\')'
            )
        rows = []
        for mail_folder in self._mail_folders(folder):
            store_key = self._store_key(mail_folder.StoreID)
            table = mail_folder.GetTable(table_filter, 0) if table_filter else mail_folder.GetTable()
            columns = table.Columns
            columns.RemoveAll()
            names = ["EntryID", "Subject", "SenderName", "MessageClass", "UnRead", when, P_HAS_ATTACH, P_DISPLAY_TO]
            for column in names:
                columns.Add(column)
            has_preview = True
            try:
                columns.Add(P_PREVIEW)
            except Exception:
                has_preview = False
            table.Sort(order, True)
            if table.GetRowCount() == 0:
                continue
            for row in table.GetArray(want) or ():
                rows.append({
                    "id": f"{store_key}.{row[0]}",
                    "subject": row[1] or "(no subject)",
                    "from": row[2] or "",
                    "kind": "meeting" if str(row[3] or "").startswith("IPM.Schedule") else "mail",
                    "unread": bool(row[4]),
                    "time": _utc_iso(row[5]),
                    "attachments": bool(row[6]),
                    "to": row[7] or "",
                    "preview": " ".join(str(row[8] or "").split())[:180] if has_preview else "",
                })
            if table_filter and len(rows) >= want:
                break  # a search scans every message of a data file: stop once the page is full
        rows.sort(key=lambda mail: mail["time"], reverse=True)
        page = rows[offset:want]
        for mail in page:  # remembered so the message can be found again after it was moved
            self._rows[mail["id"]] = (folder, mail["time"], mail["subject"], mail["from"])
            self._rows.move_to_end(mail["id"])
        while len(self._rows) > KEEP_ROWS:
            self._rows.popitem(last=False)
        return {"folder": folder, "mail": page, "more": len(rows) > want and want < MAX_LIST}

    # -------------------------------------------------- one message
    def _item(self, mail_id: str):
        mail_id = self._moved.get(mail_id, mail_id)
        store_key, _, entry_id = mail_id.partition(".")
        if not re.fullmatch(r"[0-9A-Fa-f]{16,}", entry_id):
            raise MailError("That message link is not valid.")
        if store_key not in self._stores:
            for name in FOLDERS:  # after a restart the phone still holds old ids: rebuild the store map
                for folder in self._mail_folders(name):
                    self._store_key(folder.StoreID)
        store_id = self._stores.get(store_key)
        if not store_id:
            raise MailError("That message is in a mailbox that is no longer open in Outlook.")
        try:
            return self._ns.GetItemFromID(entry_id, store_id)
        except Exception:
            now_id = self._find_again(mail_id)
            if now_id is None:
                raise MailError("That message was moved or deleted. Refresh the list.")
            return self._item(now_id)

    def _find_again(self, mail_id: str) -> str | None:
        """The id a moved message has now, or None.

        A rule or an archiving tool moves mail into another data file seconds after it arrives or is
        sent, and that changes its id while the phone still shows the old one. The same message is
        looked up among the newest ones of that folder by time, subject and sender. Only an exact,
        single match counts.
        """
        known = self._rows.get(mail_id)
        if known is None or not known[1]:
            return None
        folder = known[0]
        self._rows.pop(mail_id)  # whatever comes out, this id is gone: never search for it twice
        try:
            rows = self._list(folder, FIND_AGAIN_ROWS, 0, "")["mail"]
        except Exception:
            return None
        same = [row["id"] for row in rows if (folder, row["time"], row["subject"], row["from"]) == known and row["id"] != mail_id]
        if len(same) != 1:
            return None
        self._moved[mail_id] = same[0]
        if len(self._moved) > KEEP_ROWS:
            self._moved.pop(next(iter(self._moved)))
        return same[0]

    def get(self, mail_id: str) -> dict:
        """One message. A received or sent message that was read before comes from memory at once.

        A draft can change, so Outlook is asked first; when it does not answer within QUICK_SECONDS
        the copy is returned marked `saved`.
        """
        self._phone_asked()
        kept = self._kept_message(mail_id)
        if kept is not None and not kept["draft"]:
            return kept
        try:
            return self._shared(("item", mail_id), self._get, mail_id, wait=QUICK_SECONDS if kept else READ_SECONDS)
        except OutlookBusy:
            if kept is None:
                raise
            return {**kept, "saved": True}

    def _get(self, mail_id: str) -> dict:
        item = self._item(mail_id)
        mail_id = self._moved.get(mail_id, mail_id)
        access = item.PropertyAccessor

        def prop(tag, default=""):
            try:
                return access.GetProperty(tag)
            except Exception:
                return default

        sender_address = self._sender_address(item, access)
        self._count_people(mail_id, item, getattr(item, "SenderName", "") or "", sender_address)
        attachments = []
        try:
            for index in range(1, item.Attachments.Count + 1):
                attachment = item.Attachments.Item(index)
                hidden = False
                try:
                    hidden = bool(attachment.PropertyAccessor.GetProperty(P_ATTACH_HIDDEN))
                except Exception:
                    pass
                if not hidden:
                    attachments.append({"index": index, "name": attachment.FileName, "size": attachment.Size})
        except Exception:
            pass
        sent = _utc_iso(prop(P_SUBMITTED, None))
        return {
            "id": mail_id,
            "subject": getattr(item, "Subject", "") or "(no subject)",
            "from": getattr(item, "SenderName", "") or "",
            "fromAddress": sender_address,
            "to": getattr(item, "To", "") or "",
            "cc": getattr(item, "CC", "") or "",
            "time": _utc_iso(prop(P_DELIVERED, None)) or sent or _utc_iso(prop(P_MODIFIED, None)),
            "draft": not getattr(item, "Sent", True),
            "body": (getattr(item, "Body", "") or "")[:MAX_BODY],
            "html": (getattr(item, "HTMLBody", "") or "")[:MAX_HTML],
            "attachments": attachments,
        }

    # -------------------------------------------------- people, for the composer's suggestions
    @staticmethod
    def _sender_address(item, access) -> str:
        """The sender's internet address. Exchange gives an internal name for a colleague, so ask the
        message for the SMTP one it carries, and only then the address book (which can fail)."""
        try:
            address = access.GetProperty(P_SENDER_SMTP) or ""
            if "@" in address:
                return address
        except Exception:
            pass
        try:
            address = item.SenderEmailAddress or ""
            if "@" not in address:
                address = item.Sender.GetExchangeUser().PrimarySmtpAddress
            return address or ""
        except Exception:
            return ""

    def _note_people(self, mail_id: str) -> None:
        """One message read only for who wrote it and who it went to."""
        with self._lock:
            if self._moved.get(mail_id, mail_id) in self._people_counted:
                return
        item = self._item(mail_id)
        mail_id = self._moved.get(mail_id, mail_id)
        sender = self._sender_address(item, item.PropertyAccessor)
        self._count_people(mail_id, item, getattr(item, "SenderName", "") or "", sender)

    def _count_people(self, mail_id: str, item, sender_name: str, sender_address: str) -> None:
        """Remember the people on this message. Each message counts once; never fails the read it is part of."""
        with self._lock:
            if mail_id in self._people_counted:
                return
            self._people_counted[mail_id] = None
            while len(self._people_counted) > KEEP_ROWS:
                self._people_counted.popitem(last=False)
        found = []
        try:
            recipients = item.Recipients
            for index in range(1, min(recipients.Count, PEOPLE_RECIPIENTS) + 1):
                recipient = recipients.Item(index)
                try:
                    address = recipient.PropertyAccessor.GetProperty(P_SMTP)
                except Exception:
                    address = getattr(recipient, "Address", "")
                found.append((getattr(recipient, "Name", "") or "", address or ""))
            mine = self._account()[0].lower()
        except Exception:
            return
        # someone you wrote to is who you will write to again; someone who wrote to you comes next
        from_me = bool(mine) and sender_address.lower() == mine
        self.remember_people([(sender_name, sender_address, 2)] + [(name, address, 3 if from_me else 1) for name, address in found])

    def remember_people(self, entries) -> None:
        """(name, address, weight) for each: add to what the composer suggests."""
        with self._lock:
            for name, address, weight in entries:
                address = str(address or "").strip()
                if not re.fullmatch(r"[^@\s<>;,]+@[^@\s<>;,]+", address):
                    continue
                name = " ".join(str(name or "").strip("'\" ").split())
                if "@" in name:  # Outlook shows the address where it knows no name
                    name = ""
                known = self._people.get(address.lower())
                if known is None:
                    if len(self._people) >= PEOPLE_KEEP:
                        continue
                    self._people[address.lower()] = [name, address, weight]
                else:
                    known[0] = known[0] or name
                    known[2] += weight

    def people(self) -> dict:
        """Names and addresses to suggest in To and Cc, most written-to first. From memory at once:
        Outlook is not asked, so the list grows as mail is read ahead."""
        self._phone_asked()
        mine = (self._who or ("", ""))[0].lower()
        with self._lock:
            known = [person for address, person in self._people.items() if address != mine]
        known.sort(key=lambda person: (-person[2], (person[0] or person[1]).lower()))
        return {"people": [{"name": name, "address": address} for name, address, _ in known[:PEOPLE_SHOWN]]}

    def attachment(self, mail_id: str, index: int, folder: Path) -> tuple[str, Path]:
        """Save one attachment into `folder`; returns (file name, path)."""
        return self._do(self._attachment, mail_id, index, folder)

    def _attachment(self, mail_id: str, index: int, folder: Path) -> tuple[str, Path]:
        item = self._item(mail_id)
        if not 1 <= index <= item.Attachments.Count:
            raise MailError("That attachment is not there any more.")
        attachment = item.Attachments.Item(index)
        name = attachment.FileName or f"attachment-{index}"
        path = Path(folder) / f"{index}{Path(name).suffix[:12]}"
        attachment.SaveAsFile(str(path))
        return name, path

    # -------------------------------------------------- send or save a draft
    def send(self, payload: dict) -> dict:
        request = clean_send_request(payload)
        self._state_cache = None
        try:
            return self._do(self._send, request, timeout=SEND_SECONDS, first=True)
        except OutlookBusy as error:
            if error.started:  # Outlook is in the middle of it: we cannot know yet whether it went out
                raise MailError(
                    "Outlook is taking a long time with this message. "
                    "Look in Sent and Drafts before you send it again, so it does not go twice."
                )
            raise MailError("Outlook on your PC is busy, so nothing was sent. Your text is kept here. Try again in a moment.")
        finally:
            with self._lock:  # Sent and Drafts have changed (or may have): no old copy of them is shown
                self._lists.clear()
                if request["mode"] == "draft":
                    self._drop_message(self._moved.get(request["id"], request["id"]))

    def _send(self, request: dict) -> dict:
        mode = request["mode"]
        created = True
        if mode == "new":
            mail = self._app.CreateItem(0)
        else:
            source = self._item(request["id"])
            if mode == "draft":
                if getattr(source, "Sent", True):
                    raise MailError("That message is not a draft.")
                mail, created = source, False
            else:
                try:
                    mail = {"reply": source.Reply, "replyAll": source.ReplyAll, "forward": source.Forward}[mode]()
                except AttributeError:
                    raise MailError("This kind of item cannot be answered from Titip. Use Outlook for it.")
        try:
            if mode in ("reply", "replyAll"):  # Outlook already addressed it: only add to that
                for kind, addresses in ((1, request["to"]), (2, request["cc"])):
                    for address in addresses:
                        mail.Recipients.Add(address).Type = kind
            else:
                mail.To = "; ".join(request["to"])
                mail.CC = "; ".join(request["cc"])
            if request["subject"]:
                mail.Subject = request["subject"]
            block = text_to_html(request["body"])
            if mode == "draft":
                # Sent as it was opened: keep Outlook's formatting, signature and pictures untouched.
                if " ".join(request["body"].split()) != " ".join((mail.Body or "").split()):
                    mail.HTMLBody = f"<html><body>{block}</body></html>"
            else:
                # No GetInspector here to pull in a signature: it would leave a hidden editor window
                # inside Outlook for every mail. Whatever Outlook put in the body stays below the text.
                mail.HTMLBody = insert_into_html(mail.HTMLBody, block)
            if mail.Recipients.Count and not mail.Recipients.ResolveAll():
                unknown = [r.Name for r in mail.Recipients if not r.Resolved]
                raise MailError("Outlook does not know: " + ", ".join(unknown) + ". Use a full e-mail address.")
            if request["action"] == "draft":
                mail.Save()
                return {"ok": True, "saved": True}
            if not mail.Recipients.Count:
                raise MailError("Add at least one recipient.")
            mail.Send()
            return {"ok": True, "sent": True}
        except Exception:
            if created:
                try:
                    mail.Close(OL_DISCARD)
                except Exception:
                    pass
            raise


# ============================================================ demo (invented mail)

class DemoMailbox:
    """The mail in web/demo.json, held in memory. Sending moves a message into Sent, nothing leaves."""

    demo = True

    def __init__(self):
        data = json.loads((WEB / "demo.json").read_text(encoding="utf-8"))
        self.me = data["me"]
        self.people_known = data.get("people", [])
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        self.mail: list[dict] = []
        for index, mail in enumerate(data["mail"]):
            when = now - dt.timedelta(minutes=mail.pop("minutesAgo"))
            self.mail.append({
                "id": f"demo.{index:016X}",
                "time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cc": "",
                "attachments": [],
                "unread": False,
                **mail,
            })
        self.lock = threading.Lock()

    def state(self) -> dict:
        counts = {name: sum(1 for mail in self.mail if mail["folder"] == name) for name in FOLDERS}
        return {"ok": True, "demo": True, "account": self.me["address"], "name": self.me["name"], "counts": counts}

    quick_state = state

    def list(self, folder: str, limit: int = 40, offset: int = 0, query: str = "") -> dict:
        if folder not in FOLDERS:
            raise MailError("Unknown folder.")
        needle = (query or "").lower().strip()
        rows = [
            mail for mail in self.mail
            if mail["folder"] == folder
            and (not needle or needle in (mail["subject"] + " " + mail["from"] + " " + mail["to"]).lower())
        ]
        rows.sort(key=lambda mail: mail["time"], reverse=True)
        page = rows[offset: offset + limit]
        return {
            "folder": folder,
            "more": len(rows) > offset + limit,
            "mail": [{
                "id": mail["id"],
                "subject": mail["subject"],
                "from": mail["from"],
                "kind": "mail",
                "unread": mail["unread"],
                "time": mail["time"],
                "attachments": bool(mail["attachments"]),
                "to": mail["to"],
                "preview": " ".join(mail["body"].split())[:180],
            } for mail in page],
        }

    def _find(self, mail_id: str) -> dict:
        for mail in self.mail:
            if mail["id"] == mail_id:
                return mail
        raise MailError("That message was moved or deleted. Refresh the list.")

    def get(self, mail_id: str) -> dict:
        mail = self._find(mail_id)
        return {
            "id": mail["id"],
            "subject": mail["subject"],
            "from": mail["from"],
            "fromAddress": mail.get("fromAddress", ""),
            "to": mail["to"],
            "cc": mail["cc"],
            "time": mail["time"],
            "draft": mail["folder"] == "drafts",
            "body": mail["body"],
            "html": mail.get("html", ""),
            "attachments": [
                {"index": index + 1, "name": a["name"], "size": a["size"]}
                for index, a in enumerate(mail["attachments"])
            ],
        }

    def people(self) -> dict:
        """Everyone in the invented mail, for the composer's suggestions."""
        return {"people": [dict(person) for person in self.people_known]}

    def attachment(self, mail_id: str, index: int, folder: Path) -> tuple[str, Path]:
        mail = self._find(mail_id)
        if not 1 <= index <= len(mail["attachments"]):
            raise MailError("That attachment is not there any more.")
        name = mail["attachments"][index - 1]["name"]
        path = Path(folder) / f"{index}.txt"
        path.write_text("Demo attachment: " + name, encoding="utf-8")
        return name, path

    def send(self, payload: dict) -> dict:
        request = clean_send_request(payload)
        with self.lock:
            source = self._find(request["id"]) if request["id"] else None
            if request["mode"] == "draft" and source["folder"] != "drafts":
                raise MailError("That message is not a draft.")
            to = request["to"]
            subject = request["subject"]
            body = request["body"]
            if request["mode"] in ("reply", "replyAll"):
                to = to or [source["from"]]
                subject = subject or "RE: " + source["subject"]
                body += f"\n\nFrom: {source['from']}\nSubject: {source['subject']}\n\n{source['body']}"
            if request["mode"] == "forward":
                subject = subject or "FW: " + source["subject"]
                body += f"\n\nFrom: {source['from']}\nSubject: {source['subject']}\n\n{source['body']}"
            for address in to:
                if "@" not in address and " " not in address and len(address) < 3:
                    raise MailError(f"Outlook does not know: {address}. Use a full e-mail address.")
            if request["mode"] == "draft":
                self.mail.remove(source)
            folder = "drafts" if request["action"] == "draft" else "sent"
            self.mail.append({
                "id": f"demo.{len(self.mail) + 100:016X}{len(body):04X}",
                "folder": folder,
                "from": self.me["name"],
                "fromAddress": self.me["address"],
                "to": "; ".join(to),
                "cc": "; ".join(request["cc"]),
                "subject": subject or "(no subject)",
                "body": body,
                "time": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "attachments": [],
                "unread": False,
            })
        return {"ok": True, "saved": True} if folder == "drafts" else {"ok": True, "sent": True}
