"""Gmail as it is signed in in Chrome on this PC, read and answered through the screen.

No Google API, no password and no stored token. A Gmail search is opened in a new window of your
normal Chrome profile, read through Windows UI Automation (the accessibility tree a screen reader
uses), and closed again. To answer, the page's own Reply and Send buttons are pressed.

What that means in practice:

* The PC must be on and unlocked, and Chrome signed in to the mailbox. A locked screen has no page.
* The window has to stay on the screen. When your Chrome is already open it is kept as a strip a few
  pixels wide at the right edge until it closes. The keyboard is handed back if Chrome takes it.
* Opening a mail marks it as read. Nothing is deleted, moved, forwarded or downloaded, no link in a
  mail is followed, and no cookie or profile file is touched.
* An answer goes to whoever wrote the last message of the thread that was just read, and only when
  Gmail's reply form names that one person.
* Gmail shows only the name of somebody you have written to before. Their address is then read from
  the message's own "Show details", which is opened and closed again. An earlier message of the
  thread never stands in for the last one.

Check what this PC can read:  python -m titip.gmail          (mailboxes and how many unread, no text)
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import brain
from .autopilot import SourceError

CHROME_PATHS = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)
# Chrome stops loading a window that is fully covered. The flag only counts when this launch starts
# Chrome; when Chrome is already open the window is kept as a strip at the screen edge instead.
CHROME_ARGS = ("--profile-directory=Default", "--disable-features=CalculateNativeWinOcclusion")
WINDOW_WAIT_S = 90  # a cold Chrome start on a busy PC can take a minute
ROWS_WAIT_S = 120
MAIL_WAIT_S = 40
SENT_WAIT_S = 30
EDGE_STRIP_PX = 8
MUTEX = "ReachPilotChromeMail"  # shared with ReachPilot: one hidden Gmail window at a time on this PC

# UI Automation control types and patterns
BUTTON, EDIT, LINK, LISTITEM, TEXT, ROW, DOCUMENT = 50000, 50004, 50005, 50007, 50020, 50029, 50030
INVOKE_PATTERN, VALUE_PATTERN, TEXT_PATTERN, EXPAND_PATTERN = 10000, 10002, 10014, 10005

OUR_TITLE = re.compile(r"untitled|gmail|mail\.google\.com|sign in|google accounts", re.I)
SIGN_IN_TITLE = re.compile(r"sign in|google accounts", re.I)
MAILBOX_IN_TITLE = re.compile(r"([\w.+-]+@[\w-]+(?:\.[\w-]+)+) - Gmail")
NO_RESULTS = re.compile(r"no messages matched your search", re.I)
ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
REPLY_BUTTON = re.compile(r"^reply$", re.I)
SEND_BUTTON = re.compile(r"^send(\s*\W*\(.*\)\W*)?$", re.I)
DISCARD_BUTTON = re.compile(r"^discard draft", re.I)
SENT_NOTICE = re.compile(r"^message sent", re.I)
UNREAD_BUTTON = re.compile(r"^mark as unread", re.I)
DETAILS_BUTTON = re.compile(r"^show details$", re.I)
FROM_HEADER = re.compile(r"^from:?$", re.I)
DETAILS_WAIT_S = 10
TIME_OF_DAY = re.compile(r"\b\d{1,2}[:.]\d{2}\b")  # every date cell of an open thread carries one
HEADER_SLACK = 80  # characters a message may put between its sender and its date
VERIFIED = re.compile(r"\bverified sender\b", re.I)  # the tick Gmail puts next to some senders, read out as words
LEAVE_BUTTON = re.compile(r"^leave", re.I)
STAY_BUTTON = re.compile(r"^(cancel|stay)", re.I)
CLOSE_TRIES = 3
CLOSE_WAIT_S = 3
SAVE_WAIT_S = 3  # Gmail saves "unread" and a sent answer in the background; closing earlier asks "Leave site?"
WHEN = re.compile(
    r"^(\d{1,2}[:.]\d{2}(\s?[ap]m)?|[a-z]{3,9}\.? \d{1,2}|\d{1,2} [a-z]{3,9}\.?|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})$", re.I)
TO_LINE = re.compile(r"^to (.+?) show details$", re.I)
REPLY_BOX = "Message Body"


# ------------------------------------------------------------ plain helpers (tested without a browser)

def search_url(account: int, query: str) -> str:
    return f"https://mail.google.com/mail/u/{int(account)}/#search/" + urllib.parse.quote(query, safe="")


def row_key(text: str) -> str:
    """The same mail before and after it was opened, and today and tomorrow.

    Gmail starts unread rows with 'unread' and shows '14:31' today but 'Nov 17' tomorrow, so both are
    left out. A new message in the thread changes the count and the snippet, and so the key.
    """
    parts = [part.strip() for part in re.sub(r"\s+", " ", text or "").split(" , ")]
    kept = [part for part in parts if part and not WHEN.match(part)]
    return re.sub(r"^unread,?\s*", "", " , ".join(kept), flags=re.I)[:300]


def is_unread(text: str) -> bool:
    return bool(re.match(r"\s*unread\b", text or "", re.I))


def results_state(row_count: int, cell_names) -> str:
    """'rows', 'empty' or 'loading'. "No mail" is believed only when Gmail says so itself."""
    if row_count:
        return "rows"
    if any(NO_RESULTS.search(name) for name in cell_names):
        return "empty"
    return "loading"


def edge_place(monitors, strip: int = EDGE_STRIP_PX) -> tuple[int, int]:
    """(x, y) that leaves a strip of a window on the right-most screen; monitors are (left, top, right, bottom)."""
    _, top, right, _ = max(monitors, key=lambda monitor: monitor[2])
    return right - strip, top + 120


def subject_from_title(title: str, mailbox: str) -> str:
    """Chrome's window title for an open mail is '<subject> - <mailbox> - Gmail - Google Chrome'."""
    head, found, _ = (title or "").rpartition(f" - {mailbox} - Gmail")
    return head.strip() if found else ""


def last_sender(cell_names) -> tuple[str, str]:
    """(name, address) of whoever wrote the last message, from a thread's header cells ('Name address')."""
    for name in reversed(list(cell_names)):
        found = ADDRESS.search(name or "")
        if found:
            shown = VERIFIED.sub(" ", re.sub(r"[<>()]", " ", name[:found.start()]))
            return re.sub(r"\s+", " ", shown).strip(), found.group(0)
    return "", ""


def _bare(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def last_header(message_names, cell_names) -> str:
    """The header cell ('Name address', or only 'Name') of the last message of a thread.

    Header cells and date cells come in pairs, one pair per message, and a message's text starts with
    its sender and its date. Gmail leaves the address out for people you have written to, so "the
    last cell with an address" can belong to an earlier message (often your own). A bullet point
    inside a mail is a list item too and starts with neither. Between the two a message can carry
    words the cells do not ('Unsubscribe'), and a cell words the message does not ('Verified sender').
    """
    cells = [name for name in cell_names if name and name.strip()]
    pairs = [(header, when) for header, when in zip(cells, cells[1:]) if TIME_OF_DAY.search(when) and not ADDRESS.search(when)]
    for message in reversed([_bare(name) for name in message_names if name and name.strip()]):
        for header, when in reversed(pairs):
            if message.startswith(_bare(header.split()[0])) and _bare(when) in message[:len(_bare(header)) + HEADER_SLACK]:
                return header
    return ""


def from_details(cells, shown: str) -> tuple[str, str]:
    """(name, address) from the rows 'Show details' adds; cells are (role, name) in page order.

    Only the last 'from:' row counts, and only when it carries the name the message itself shows:
    details of another message are never believed.
    """
    cells = [(role, (name or "").strip()) for role, name in cells]
    starts = [index for index, (role, name) in enumerate(cells) if role == "rowheader" and FROM_HEADER.match(name)]
    if not starts:
        return "", ""
    for role, name in cells[starts[-1] + 1:]:
        if role != "gridcell":
            break
        sender, address = last_sender([name])
        if address:
            same = not shown.strip() or _bare(sender) == _bare(VERIFIED.sub(" ", shown))
            return (sender, address) if same else ("", "")
    return "", ""


def reply_names(group_names) -> list[str]:
    """Who Gmail's reply form says it will write to: 'to Mateo Show details' -> ['mateo']."""
    found = [TO_LINE.match(name or "") for name in group_names]
    return [match.group(1).strip().lower() for match in found if match]


def names_match(shown: str, sender: str, address: str) -> bool:
    """Gmail shows a first name, or the address when the sender gave no name."""
    shown = (shown or "").strip().lower()
    if not shown or "," in shown or " and " in shown:  # more than one recipient is never ours
        return False
    known = f"{sender} {address} {address.split('@')[0]}".lower()
    return shown in known.split() or shown == sender.lower() or shown == address.lower()


def thread_text(message_names, limit: int = brain.MAX_MAIL_CHARS) -> str:
    """The messages of a thread, oldest first, cut from the front so the newest always fits."""
    parts = [re.sub(r"[ \t]+", " ", name).strip() for name in message_names if name and name.strip()]
    return "\n\n-----\n\n".join(parts)[-limit:]


def same_text(written: str, shown: str) -> bool:
    squeeze = lambda text: " ".join((text or "").split())  # noqa: E731
    return bool(squeeze(written)) and squeeze(written) in squeeze(shown)


@dataclass
class Row:
    key: str
    text: str
    opener: object = None


# ------------------------------------------------------------ one Chrome window

class _Window:
    """One Chrome window out of the way, driven through UI Automation only (no mouse, no typing)."""

    def __init__(self):
        import comtypes
        import comtypes.client
        import win32api
        import win32con
        import win32gui
        import win32process

        try:
            comtypes.CoInitialize()  # the autopilot runs on its own thread
        except OSError:
            pass
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient

        self.gui, self.con, self.api = win32gui, win32con, UIAutomationClient
        self.win, self.process = win32api, win32process
        self.automation = comtypes.client.CreateObject(
            UIAutomationClient.CUIAutomation8, interface=UIAutomationClient.IUIAutomation
        )
        self.handle = None
        self.foreground = None  # the window you were last seen in
        self.edge = None
        self.home = None

    def chrome_windows(self) -> dict:
        found = {}

        def visit(handle, _):
            if self.gui.IsWindowVisible(handle) and self.gui.GetClassName(handle) == "Chrome_WidgetWin_1":
                title = self.gui.GetWindowText(handle)
                if title.endswith("Google Chrome"):
                    found[handle] = title

        self.gui.EnumWindows(visit, None)
        return found

    def open(self, url: str) -> None:
        chrome = next((path for path in CHROME_PATHS if path.exists()), None)
        if chrome is None:
            raise SourceError("Google Chrome is not installed in the usual place.")
        before = set(self.chrome_windows())
        self.foreground = self.gui.GetForegroundWindow()
        quiet = subprocess.STARTUPINFO()
        quiet.dwFlags = subprocess.STARTF_USESHOWWINDOW
        quiet.wShowWindow = self.con.SW_SHOWNOACTIVATE
        launched = time.time()
        subprocess.Popen([str(chrome), *CHROME_ARGS, "--new-window", url], startupinfo=quiet)
        deadline = time.monotonic() + WINDOW_WAIT_S
        while time.monotonic() < deadline and self.handle is None:
            time.sleep(0.2)
            for handle, title in self.chrome_windows().items():
                if handle in before or not OUR_TITLE.search(title):
                    continue
                if self.normal_chrome(handle):
                    self.handle = handle
                    break
                before.add(handle)  # a robot's own browser: never moved, never closed
        if self.handle is None:
            raise SourceError("Chrome did not open a Gmail window. Is the PC unlocked?")
        self.choose_place(launched)
        self.park()

    def normal_chrome(self, handle) -> bool:
        """False for a Chrome that a robot runs with a profile of its own (Playwright and the like)."""
        try:
            import psutil

            _, process_id = self.process.GetWindowThreadProcessId(handle)
            arguments = psutil.Process(process_id).cmdline()
            return not any(argument.startswith(("--user-data-dir", "--remote-debugging")) for argument in arguments)
        except Exception:
            return True

    def choose_place(self, launched: float) -> None:
        try:
            import psutil

            _, process_id = self.process.GetWindowThreadProcessId(self.handle)
            if psutil.Process(process_id).create_time() < launched - 1:  # your Chrome was already open
                monitors = [monitor[2] for monitor in self.win.EnumDisplayMonitors()]
                self.home = self.gui.GetWindowRect(self.handle)[:2]
                self.edge = edge_place(monitors)
        except Exception:
            self.edge = None

    def park(self) -> None:
        """Keeps the window out of the way and not minimized, and gives the keyboard back if it took it."""
        stay = self.con.SWP_NOSIZE | self.con.SWP_NOACTIVATE
        try:
            if self.gui.IsIconic(self.handle):  # a minimized page has no accessibility tree
                self.gui.ShowWindow(self.handle, self.con.SW_SHOWNOACTIVATE)
            if self.edge:
                self.gui.SetWindowPos(self.handle, self.con.HWND_TOPMOST, *self.edge, 0, 0, stay)
            else:
                self.gui.SetWindowPos(self.handle, self.con.HWND_BOTTOM, 0, 0, 0, 0, stay | self.con.SWP_NOMOVE)
            front = self.gui.GetForegroundWindow()
            if front != self.handle:
                self.foreground = front or self.foreground
                return
            target = self.foreground if self.usable(self.foreground) else self.window_above()
            if not target:
                return
            ours = self.win.GetCurrentThreadId()
            theirs, _ = self.process.GetWindowThreadProcessId(self.handle)
            self.process.AttachThreadInput(ours, theirs, True)
            try:
                self.gui.SetForegroundWindow(target)
            finally:
                self.process.AttachThreadInput(ours, theirs, False)
        except Exception:
            pass  # placement is courtesy; reading goes on

    def usable(self, handle) -> bool:
        return bool(handle) and handle != self.handle and self.gui.IsWindow(handle) \
            and self.gui.IsWindowVisible(handle) and not self.gui.IsIconic(handle)

    def window_above(self):
        found = []

        def visit(handle, _):
            if not found and self.usable(handle) and self.gui.GetWindowText(handle):
                found.append(handle)

        self.gui.EnumWindows(visit, None)
        return found[0] if found else None

    def title(self) -> str:
        try:
            return self.gui.GetWindowText(self.handle)
        except Exception:
            return ""

    def close(self) -> None:
        """Closes the window. If Gmail is still saving, Chrome asks "Leave site?": stay, wait, close again."""
        handle, self.handle = self.handle, None
        if handle is None:
            return
        stay = self.con.SWP_NOSIZE | self.con.SWP_NOACTIVATE
        for attempt in range(CLOSE_TRIES):
            last = attempt == CLOSE_TRIES - 1
            try:
                if self.edge and self.home:  # Chrome opens its next window where the last one was
                    self.gui.SetWindowPos(handle, self.con.HWND_BOTTOM, *self.home, 0, 0, stay)
                self.gui.PostMessage(handle, self.con.WM_CLOSE, 0, 0)
            except Exception:
                return
            question = self.question_after_close(handle)
            if question is None:
                return
            self.answer(question, leave=last)
            if last:
                return
            try:
                if self.edge:
                    self.gui.SetWindowPos(handle, self.con.HWND_TOPMOST, *self.edge, 0, 0, stay)
                if self.usable(self.foreground):
                    self.gui.SetForegroundWindow(self.foreground)
            except Exception:
                pass
            time.sleep(SAVE_WAIT_S)

    def question_after_close(self, handle):
        """The dialog Chrome put on top of our window instead of closing it, or None once it is gone."""
        deadline = time.monotonic() + CLOSE_WAIT_S
        while time.monotonic() < deadline:
            time.sleep(0.2)
            if not self.gui.IsWindow(handle):
                return None
            owned = self.owned_by(handle)
            if owned:
                return owned[0]
        return None  # a busy PC closes slowly; nothing asked a question

    def owned_by(self, handle) -> list:
        found = []

        def visit(other, _):
            try:
                if self.gui.IsWindowVisible(other) and self.gui.GetWindow(other, self.con.GW_OWNER) == handle:
                    found.append(other)
            except Exception:
                pass

        self.gui.EnumWindows(visit, None)
        return found

    def answer(self, dialog, leave: bool) -> bool:
        wanted = LEAVE_BUTTON if leave else STAY_BUTTON
        try:
            root = self.automation.ElementFromHandle(dialog)
        except Exception:
            return False
        for name, element in self.find(BUTTON, inside=root):
            if wanted.match(name):
                try:
                    element.GetCurrentPattern(INVOKE_PATTERN).QueryInterface(self.api.IUIAutomationInvokePattern).Invoke()
                    return True
                except Exception:
                    return False
        return False

    def page(self):
        root = self.automation.ElementFromHandle(self.handle)
        is_page = self.automation.CreatePropertyCondition(self.api.UIA_ControlTypePropertyId, DOCUMENT)
        return root.FindFirst(self.api.TreeScope_Descendants, is_page)

    def find(self, control_type: int, inside=None) -> list:
        """(name, element) of every element of this type on the page (or inside one element)."""
        try:
            scope = inside if inside is not None else self.page()
            if scope is None:
                return []
            wanted = self.automation.CreatePropertyCondition(self.api.UIA_ControlTypePropertyId, control_type)
            found = scope.FindAll(self.api.TreeScope_Descendants, wanted)
            result = []
            for index in range(found.Length):
                element = found.GetElement(index)
                try:
                    result.append(((element.CurrentName or "").strip(), element))
                except Exception:
                    continue
            return result
        except Exception:  # the page changed under us; the caller asks again
            return []

    def role(self, element) -> str:
        try:
            return element.CurrentAriaRole or ""
        except Exception:
            return ""

    def press(self, element) -> bool:
        try:
            element.GetCurrentPattern(INVOKE_PATTERN).QueryInterface(self.api.IUIAutomationInvokePattern).Invoke()
            return True
        except Exception:
            return False
        finally:
            self.park()  # pressing focuses the page, which brings the window forward

    def expand(self, element, opened: bool = True) -> bool:
        """Opens (or closes) something that unfolds in place, like a message's 'Show details'."""
        try:
            pattern = element.GetCurrentPattern(EXPAND_PATTERN).QueryInterface(self.api.IUIAutomationExpandCollapsePattern)
            pattern.Expand() if opened else pattern.Collapse()
            return True
        except Exception:
            return False
        finally:
            self.park()

    def text_of(self, element) -> str:
        try:
            pattern = element.GetCurrentPattern(TEXT_PATTERN).QueryInterface(self.api.IUIAutomationTextPattern)
            return pattern.DocumentRange.GetText(-1) or ""
        except Exception:
            pass
        try:
            pattern = element.GetCurrentPattern(VALUE_PATTERN).QueryInterface(self.api.IUIAutomationValuePattern)
            return pattern.CurrentValue or ""
        except Exception:
            return ""

    def set_text(self, element, text: str) -> bool:
        try:
            pattern = element.GetCurrentPattern(VALUE_PATTERN).QueryInterface(self.api.IUIAutomationValuePattern)
            pattern.SetValue(text)
            return True
        except Exception:
            return False
        finally:
            self.park()

    def wait(self, ready, seconds: float, pause: float = 0.5):
        deadline = time.monotonic() + seconds
        while True:
            result = ready()
            if result or time.monotonic() >= deadline:
                return result
            self.park()
            time.sleep(pause)

    def rows(self, cells=None) -> list:
        """(text, opener) per mail in the list: the row's text and the link that opens it."""
        result = []
        for name, element in self.find(ROW) if cells is None else cells:
            if self.role(element) != "row" or not name:  # a row's cells have the same control type
                continue
            openers = self.find(LINK, inside=element)
            if openers:
                result.append((name, openers[0][1]))
        return result

    def results(self) -> str:
        """Waits for the list: 'rows', 'empty', or '' when the page never finished loading."""
        deadline = time.monotonic() + ROWS_WAIT_S
        while True:
            cells = self.find(ROW)
            state = results_state(len(self.rows(cells)), [name for name, _ in cells])
            if state != "loading":
                return state
            if time.monotonic() >= deadline:
                return ""
            self.park()
            time.sleep(0.5)


# ------------------------------------------------------------ the mailbox the autopilot talks to

class ChromeGmail:
    """open -> unread -> read -> reply -> close. One window at a time; every call is made by one thread."""

    def __init__(self):
        self.window: _Window | None = None
        self.mailbox = ""
        self._mutex = None

    def open(self, account: int, query: str) -> str:
        """Shows the mails that match the query and returns the mailbox's address."""
        self.close()
        try:
            import win32event

            self._mutex = win32event.CreateMutex(None, False, MUTEX)
            win32event.WaitForSingleObject(self._mutex, 180_000)
            self.window = _Window()
        except ImportError as error:
            raise SourceError("Reading Gmail needs the Python packages pywin32 and comtypes on this PC.") from error
        window = self.window
        window.open(search_url(account, query))

        def mailbox_shown():
            found = MAILBOX_IN_TITLE.search(window.title())
            return found.group(1) if found else ""

        self.mailbox = window.wait(mailbox_shown, WINDOW_WAIT_S)
        if not self.mailbox:
            if SIGN_IN_TITLE.search(window.title()):
                raise SourceError("Chrome is not signed in to this Gmail account.")
            raise SourceError("Gmail did not finish loading in Chrome. Is the PC unlocked?")
        if not window.results():
            raise SourceError("Gmail's list of mail could not be read in Chrome.")
        return self.mailbox

    def unread(self, any_state: bool = False) -> list[Row]:
        if self.window is None:
            return []
        rows = [Row(row_key(text), text, opener) for text, opener in self.window.rows()]
        return rows if any_state else [row for row in rows if is_unread(row.text)]

    def read(self, row: Row, mailbox: str) -> brain.Mail:
        window = self.window
        list_title = window.title()
        if not window.press(row.opener):
            raise SourceError("Could not open the mail in Gmail.")
        if not window.wait(lambda: window.title() != list_title, MAIL_WAIT_S):
            raise SourceError("The mail did not open in Gmail.")
        messages: list[str] = []

        def settled():  # the same text twice in a row: the page has stopped changing
            found = [name for name, _ in window.find(LISTITEM) if name]
            done = bool(found) and found == messages
            messages[:] = found
            return done

        window.wait(settled, MAIL_WAIT_S, pause=1.0)
        subject = subject_from_title(window.title(), mailbox)
        cells = [name for name, element in window.find(ROW) if window.role(element) == "gridcell"]
        header = last_header(messages, cells)
        sender, address = last_sender([header])
        if header and not address:  # somebody you have written to: Gmail shows the name only
            sender, address = self._sender_from_details(header)
        return brain.Mail(mailbox, sender, address, subject, thread_text(messages))

    def _sender_from_details(self, shown: str) -> tuple[str, str]:
        """Who wrote the last message, from its 'Show details'. ('', '') when the page does not say."""
        window = self.window
        buttons = [element for name, element in window.find(BUTTON) if DETAILS_BUTTON.match(name)]
        if not buttons:
            return "", ""
        button = buttons[-1]  # the last message's; from_details checks the name, so a wrong one is not believed
        if not window.expand(button) and not window.press(button):
            return "", ""

        def rows():
            return [(window.role(element), name) for name, element in window.find(ROW)]

        window.wait(lambda: from_details(rows(), shown)[1], DETAILS_WAIT_S)
        found = from_details(rows(), shown)
        window.expand(button, opened=False)
        return found

    def reply(self, mail: brain.Mail, text: str) -> None:
        """Presses Reply on the open mail, writes the text and presses Send. Raises SourceError otherwise."""
        window = self.window
        if window is None or not mail.address:
            raise SourceError("No mail is open to answer.")
        buttons = [element for name, element in window.find(BUTTON) if REPLY_BUTTON.match(name)]
        if not buttons or not window.press(buttons[-1]):
            raise SourceError("Gmail showed no Reply button.")

        def box():
            found = [element for name, element in window.find(EDIT) if name == REPLY_BOX]
            return found[0] if len(found) == 1 else None

        if not window.wait(box, MAIL_WAIT_S):
            raise SourceError("Gmail's reply box did not open.")
        try:
            if not self._addressed_to(mail):
                raise SourceError(f"The reply box is not addressed to {mail.address}. Nothing was sent.")
            if window.text_of(box()).strip():
                raise SourceError("The reply box already holds a draft. Nothing was sent.")
            if not window.set_text(box(), text) or not window.wait(lambda: same_text(text, window.text_of(box())), 10):
                raise SourceError("The answer could not be written into Gmail's reply box.")
            send = [element for name, element in window.find(BUTTON) if SEND_BUTTON.match(name)]
            if len(send) != 1 or not window.press(send[0]):
                raise SourceError("Gmail's Send button could not be pressed.")
        except SourceError:
            self._discard()
            raise

        def sent():
            return any(SENT_NOTICE.match(name) for name, _ in window.find(TEXT)) or (
                box() is None and not any(SEND_BUTTON.match(name) for name, _ in window.find(BUTTON)))

        if not window.wait(sent, SENT_WAIT_S):
            raise SourceError("Gmail did not confirm the answer as sent. Look in Drafts and Sent.")

    def _addressed_to(self, mail: brain.Mail) -> bool:
        """True when the reply form writes to the person who wrote the last message, and only to them.

        Gmail's form shows 'to <first name>'; the newest such line on the page is the form's own.
        Together with read(), which takes the sender from the last message of the thread, an answer
        cannot go to a thread the mail was not read from.
        """
        window = self.window
        groups = [name for control_type in (50025, 50026) for name, _ in window.find(control_type)]
        shown = reply_names(groups)
        return bool(shown) and names_match(shown[-1], mail.sender, mail.address)

    def _discard(self) -> None:
        for name, element in self.window.find(BUTTON):
            if DISCARD_BUTTON.match(name):
                self.window.press(element)
                return

    def keep_unread(self) -> bool:
        """Marks the open mail unread again: what Autopilot leaves for you should still look new in Gmail."""
        window = self.window
        if window is None:
            return False
        opened = window.title()
        buttons = [element for name, element in window.find(BUTTON) if UNREAD_BUTTON.match(name)]
        if not buttons or not window.press(buttons[-1]):
            return False
        back = bool(window.wait(lambda: window.title() != opened, 15))  # Gmail returns to the list
        time.sleep(SAVE_WAIT_S)
        return back

    def close(self) -> None:
        if self.window is not None:
            self.window.close()
            self.window = None
        if self._mutex is not None:
            try:
                import win32event

                win32event.ReleaseMutex(self._mutex)
            except Exception:
                pass
            self._mutex = None


def available() -> bool:
    """Can this PC read Gmail this way at all? (Windows, Chrome, pywin32, comtypes)"""
    if sys.platform != "win32" or not any(path.exists() for path in CHROME_PATHS):
        return False
    try:
        import comtypes  # noqa: F401
        import win32gui  # noqa: F401
    except ImportError:
        return False
    return True


def main() -> int:
    """Says which mailboxes can be read and how many unread mails wait. Prints no mail text."""
    from .autopilot import UNREAD_QUERY

    if not available():
        print("Not ready: this needs Windows, Google Chrome and the Python packages pywin32 and comtypes.")
        return 1
    source = ChromeGmail()
    seen = set()
    for account in range(4):
        try:
            mailbox = source.open(account, UNREAD_QUERY)
            if mailbox in seen:  # Gmail falls back to the first account for a number nobody is signed in to
                break
            seen.add(mailbox)
            print(f"account {account}: {mailbox}, {len(source.unread())} unread in Primary (last 2 days)")
        except SourceError as error:
            print(f"account {account}: {error}")
            break
        finally:
            source.close()
    return 0 if seen else 1


if __name__ == "__main__":
    raise SystemExit(main())
