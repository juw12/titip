"""Mailboxes Titip can read and send from.

OutlookMailbox drives the classic Outlook desktop app on this PC through COM, so it uses the account
Outlook is already signed in to: no password, token or mail server setting is stored by Titip.
DemoMailbox serves the invented mail in web/demo.json (tests, screenshots, the public demo).

Both expose the same calls: state, list, get, attachment, send, people, lookup, calendar, event, act.
"""

from __future__ import annotations

import collections
import datetime as dt
import hashlib
import html
import json
import os
import queue
import itertools
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

WEB = Path(__file__).parent / "web"
FOLDERS = ("inbox", "sent", "drafts")  # read ahead and counted
MORE_FOLDERS = ("outbox", "deleted", "junk")  # asked for only when the phone opens them
ALL_FOLDERS = FOLDERS + MORE_FOLDERS
TO_FOLDERS = ("sent", "drafts", "outbox")  # listed by who the mail goes to, not who it is from
# A folder of your own is named by where Outlook keeps it: "f.<data file>.<Outlook's id of the folder>".
FOLDER_ID = re.compile(r"f\.[0-9a-f]{10}\.[0-9A-Fa-f]{16,}")
FOLDERS_SHOWN = 300  # folders listed, of every data file together
FOLDERS_DEEP = 5  # folders inside folders, this far down
FOLDERS_FRESH_SECONDS = 3600  # walking every data file keeps Outlook's own window waiting: seldom
CATEGORIES_SHOWN = 100
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

# Outlook's own address book (the company directory), asked while a name is typed. It is read
# from the copy Outlook keeps on this PC, a few names per question, and nothing of it is stored.
LOOKUP_ROWS = 8
LOOKUP_SECONDS = 8  # typing does not wait longer than this for Outlook
LOOKUP_SHORTEST, LOOKUP_LONGEST = 2, 60  # characters of the typed name that are looked up

# Files attached to a mail written on the phone. They arrive in pieces (see titip/uploads.py) and
# rest in memory; they touch the disk only for the moment Outlook reads them into the mail.
MAX_FILES = 20
MAX_FILES_BYTES = 15_000_000  # of one mail; mail servers refuse much more than this anyway
MAX_FILE_NAME = 120
RECIPIENTS_READ = 300  # people of one message that are read, for Reply all

# The calendar: asked for when the phone opens it, never read ahead.
CALENDAR_DAYS = 14
CALENDAR_LOOK = 400  # entries looked at for one page; a series that repeats daily has no end
CALENDAR_FRESH_SECONDS = 600  # reading it can take Outlook a minute or more, and its own window waits meanwhile
CALENDAR_AGAIN_SECONDS = 30  # the phone's Refresh asks anew, but not more often than this
CALENDAR_KEPT = 12
CALENDAR_LONGEST_DAYS = 31  # an entry made on the phone
RESPONSES = {0: "", 1: "organizer", 2: "tentative", 3: "accepted", 4: "declined", 5: "none"}
ANSWERS = {"accept": 3, "tentative": 2, "decline": 4}  # olMeetingAccepted, olMeetingTentative, olMeetingDeclined
BUSY = {0: "free", 1: "tentative", 2: "busy", 3: "away", 4: "elsewhere"}

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
P_FLAG = PROP + "0x10900003"  # PR_FLAG_STATUS: 2 when the message is flagged
BODY_TEXT = "urn:schemas:httpmail:textdescription"

OL_INBOX, OL_SENT, OL_DRAFTS = 6, 5, 16
OL_OUTBOX, OL_DELETED, OL_JUNK, OL_CALENDAR = 4, 3, 23, 9
OL_FOLDER = {"inbox": OL_INBOX, "sent": OL_SENT, "drafts": OL_DRAFTS, "outbox": OL_OUTBOX, "deleted": OL_DELETED, "junk": OL_JUNK}
# what a folder is sorted by (a built-in name: Table.Sort rejects property tags)
ORDER = {
    "inbox": "[ReceivedTime]", "sent": "[SentOn]", "drafts": "[LastModificationTime]",
    "outbox": "[LastModificationTime]", "deleted": "[ReceivedTime]", "junk": "[ReceivedTime]",
}
OL_DISCARD = 1
RPC_BUSY = (-2147418111, -2147417846)  # RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER
PANE_WAIT, PANE_PAUSE = 2.0, 0.2  # how long Outlook is given to let go of a draft it shows in the reading pane
IN_THE_PANE = (
    "This draft is open in Outlook's reading pane on your PC, and while it is written there Outlook lets "
    "nothing else send, delete or move it. On the PC, click another mail, then try again."
)


class MailError(Exception):
    """A problem worth showing on the phone as-is (bad recipient, mail not found, Outlook closed)."""


class OutlookBusy(MailError):
    """Outlook did not get to our call in time. `started` tells whether it had begun working on it.
    `doing` and `seconds`: what Outlook was kept by instead, and for how long, when Titip knows."""

    def __init__(self, started: bool = False, doing: str = "", seconds: float = 0.0):
        super().__init__("Outlook on your PC is busy right now. Try again in a moment.")
        self.started = started
        self.doing = doing
        self.seconds = seconds

    def why(self) -> str:
        """" Outlook has been reading the calendar for 95 seconds." or nothing: added to what the phone is told."""
        if not self.doing or self.seconds < 5:
            return ""
        return f" Outlook has been {self.doing} for {int(self.seconds)} seconds."


# What Outlook is asked to do, in words, by the name of the step that asks it.
DOING = {
    "_list": "listing a folder", "_get": "opening a message", "_state": "counting the folders", "_calendar": "reading the calendar",
    "_calendars_now": "looking for other calendars", "_folders_now": "listing your folders", "_rules_now": "reading the rules",
    "_event": "opening a calendar entry", "_invite_of": "looking up a meeting", "_lookup": "searching the address book",
    "_note_people": "reading who a mail went to", "_attachment": "saving an attachment", "_send": "sending a mail",
    "_act": "changing a message", "_plan": "changing the calendar", "_arrange": "changing folders or rules",
    "_things": "reading tasks, notes or contacts", "_thing": "opening a task, a note or a contact", "_keep_thing": "saving a task, a note or a contact",
}
RPC_TRIES = 4  # a call Outlook turns away (a window of its own is in the way) is made again this often
RPC_PAUSE = 1.5
SLOW_SECONDS = 900  # how long the answer to a slow question (the calendar, the folders) is waited for and still kept
SLOW = ("calendar", "folders", "calendars", "rules", "invite", "things")
ACCESSOR_RECIPIENTS = 60  # of one message: the people whose internet address Outlook is asked for one by one


def marked(line: str) -> str:
    """One line (already made safe for HTML) with its marks turned into formatting:
    **bold**, *italic*, __underlined__. A mark needs text right inside it, so 2 * 3 * 4 stays as typed."""
    line = re.sub(r"\[([^\[\]]+)\]\((https?://[^\s()<>]+)\)", r'<a href="\2">\1</a>', line)  # [words](https://...): a link
    line = re.sub(r"==([^\s=](?:.*?[^\s=])?)==", r'<span style="background:#fff2a8">\1</span>', line)  # ==words==: marked yellow
    line = re.sub(r"\*\*([^\s*](?:.*?[^\s*])?)\*\*", r"<b>\1</b>", line)
    line = re.sub(r"(?<![\w_])__([^\s_](?:.*?[^\s_])?)__(?![\w_])", r"<u>\1</u>", line)
    return re.sub(r"(?<![\w*])\*([^\s*](?:.*?[^\s*])?)\*(?![\w*])", r"<i>\1</i>", line)


def text_to_html(text: str, marks: bool = False) -> str:
    """What the phone typed, as the HTML block that goes above the signature or the quoted thread.

    With `marks` (the buttons over the text were used on the phone) the marks become formatting, and
    lines that start with "- " or "1. " become lists. Without, every sign stays as it was typed.
    """
    lines = html.escape(text.replace("\r\n", "\n").strip("\n")).split("\n")
    if not marks:
        body = "<br>".join(lines)
    else:
        parts, open_list = [], ""
        for number, line in enumerate(lines):
            bullet = re.match(r"\s*[-\u2022]\s+(.*)", line) or re.match(r"\s*\*\s+(.*)", line)
            counted = re.match(r"\s*\d{1,3}[.)]\s+(.*)", line)
            kind = "ul" if bullet else "ol" if counted else ""
            title = None if kind else re.match(r"#\s+(\S.*)", line)  # "# words": a line that stands out as a title
            if kind != open_list:
                parts.append((f"</{open_list}>" if open_list else "") + (f'<{kind} style="margin-top:0;margin-bottom:0">' if kind else ""))
                open_list = kind
            if kind:
                parts.append(f"<li>{marked((bullet or counted).group(1))}</li>")
            elif title:
                parts.append(f'<b style="font-size:14pt">{marked(title.group(1))}</b>' + ("<br>" if number < len(lines) - 1 else ""))
            else:
                parts.append(marked(line) + ("<br>" if number < len(lines) - 1 else ""))
        body = "".join(parts) + (f"</{open_list}>" if open_list else "")
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


def file_name(name) -> str:
    """A name a file can have on this PC for the moment Outlook reads it: no folders, no reserved characters."""
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", str(name or "").replace("\\", "/").rsplit("/", 1)[-1])
    name = name.strip(" .")
    if len(name) > MAX_FILE_NAME:  # keep the ending: Outlook and the reader tell the kind of file by it
        stem, dot, ending = name.rpartition(".")
        ending = dot + ending if dot and len(ending) <= 12 else ""
        name = name[: MAX_FILE_NAME - len(ending)].rstrip(" .") + ending
    if not name or re.fullmatch(r"(con|prn|aux|nul|com\d|lpt\d)(\..*)?", name, flags=re.IGNORECASE):
        name = "_" + name if name else "file"
    return name


def clean_files(value) -> list[dict]:
    """The files that go along, as [{"name", "data"}]. The server puts them in; a phone cannot name a path."""
    files = []
    for entry in value or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("data"), (bytes, bytearray)):
            raise MailError("The attached files did not reach your PC. Send it again.")
        files.append({"name": file_name(entry.get("name")), "data": bytes(entry["data"])})
    if len(files) > MAX_FILES:
        raise MailError(f"A mail from the phone can carry {MAX_FILES} files at most.")
    if sum(len(file["data"]) for file in files) > MAX_FILES_BYTES:
        raise MailError(f"The attached files are larger than {MAX_FILES_BYTES // 1_000_000} MB together.")
    return files


def clean_send_request(payload: dict) -> dict:
    """Validate what the phone posted; raises MailError with a message for the composer."""
    mode = payload.get("mode", "new")
    action = payload.get("action", "send")
    if mode not in ("new", "reply", "replyAll", "forward", "draft"):
        raise MailError("Unknown kind of message.")
    if action not in ("send", "draft"):
        raise MailError("Unknown action.")
    drop = []
    for entry in payload.get("drop") or []:  # attachments of the original that do not go along
        if not isinstance(entry, dict):
            raise MailError("Bad request.")
        try:
            drop.append((int(entry.get("index")), str(entry.get("name") or "")))
        except (TypeError, ValueError):
            raise MailError("Bad request.")
    request = {
        "mode": mode,
        "action": action,
        "id": str(payload.get("id") or ""),
        "to": split_addresses(payload.get("to")),
        "cc": split_addresses(payload.get("cc")),
        "bcc": split_addresses(payload.get("bcc")),
        # "exact": the phone showed who an answer goes to and what it hands over is the whole list.
        # A page from before 0.9.0 does not say so: Outlook's own recipients stay and are only added to.
        "exact": payload.get("recipients") == "exact",
        # "kept": nobody was added or taken out on the phone. Outlook's own recipients then stay as
        # they are, by the same steps as before 0.9.0, and the lists are not gone through at all.
        "kept": payload.get("kept") is True,
        "importance": payload.get("importance") if payload.get("importance") in ("high", "low") else "",
        "receipt": payload.get("receipt") is True,  # ask the people it goes to for a read receipt
        "marks": payload.get("format") == "marks",  # **bold**, *italic*, __underlined__ and lists become formatting
        "later": None,  # the moment it is to leave, on this PC's clock; until then it waits in the Outbox
        "subject": str(payload.get("subject") or "").strip()[:255],
        "body": str(payload.get("body") or "")[:MAX_BODY],
        "files": clean_files(payload.get("files")),
        "drop": sorted(set(drop), reverse=True) if mode in ("forward", "draft") else [],
    }
    if mode != "new" and not request["id"]:
        raise MailError("The original message is missing.")
    if payload.get("later") and action == "send":
        try:
            request["later"] = local_time(payload.get("later"))
        except (TypeError, ValueError):
            raise MailError("That day or time could not be read.")
        ahead = request["later"] - dt.datetime.now()
        if ahead < dt.timedelta(minutes=1):
            raise MailError("The time to send it has passed already. Pick a later one, or send it now.")
        if ahead > dt.timedelta(days=LATER_MOST_DAYS):
            raise MailError(f"A mail can wait {LATER_MOST_DAYS} days at most.")
    if action == "send":
        anyone = request["to"] or request["cc"] or request["bcc"]
        if not anyone and (request["exact"] or mode not in ("reply", "replyAll")):
            raise MailError("Add at least one recipient.")
        if not request["body"].strip() and mode != "forward":
            raise MailError("Write a message first.")
    return request


ACTS = ("delete", "respond", "read", "unread", "flag", "unflag", "move", "categorize", "erase")
ARRANGES = ("folder", "rename", "unfolder", "category", "rule", "ruleFrom", "away", "awayText", "empty")
NEWS_ROWS = 15  # the newest mails of the Inbox that are looked at for what is new
NEWS_REMINDERS = 300  # of Outlook's reminders, how many are asked whether they are showing
NEWS_KEPT = 400  # mails and reminders remembered as told
EMPTY_MOST = 300  # mails deleted for good in one go when a folder is emptied; the phone asks again for the rest
AWAY_TEXT_MOST = 4000
OOF_TEMPLATE = "IPM.Note.Rules.OofTemplate.Microsoft"  # the hidden note in the Inbox that holds the automatic reply for colleagues
OOF_OUTSIDE = "IPM.Note.Rules.ExternalOofTemplate.Microsoft"  # and the one for people outside the organization
# Another time proposed for a meeting, as Outlook itself writes it into its answer (MS-OXOCAL: the answer's
# PidLidAppointmentProposedStartWhole, ...EndWhole, ...Duration and PidLidAppointmentCounterProposal).
APPOINTMENT = "http://schemas.microsoft.com/mapi/id/{00062002-0000-0000-C000-000000000046}/"
P_PROPOSED_START, P_PROPOSED_END = APPOINTMENT + "82500040", APPOINTMENT + "82510040"
P_PROPOSED_MINUTES, P_PROPOSED = APPOINTMENT + "82560003", APPOINTMENT + "8257000B"
P_CONTENT_ID = PROP + "0x3712001F"  # PR_ATTACH_CONTENT_ID: what a picture inside the text is called by
PICTURES = (".png", ".jpg", ".jpeg", ".gif")
NAME_MOST = 80  # letters of a folder's or a category's name
RULES_SHOWN = 100


def clean_arrange_request(payload: dict) -> dict:
    """Something done to how the mailbox is ordered: a folder made, renamed or taken out, a category
    made, a rule switched on or off, a rule made for one sender. Checked before Outlook is asked."""
    what = payload.get("what")
    if what not in ARRANGES:
        raise MailError("Unknown action.")
    request = {
        "what": what, "id": str(payload.get("id") or ""), "in": str(payload.get("in") or ""), "to": str(payload.get("to") or ""),
        "name": " ".join(str(payload.get("name") or "").split())[:NAME_MOST], "on": payload.get("on") is True,
    }
    if what == "empty":
        if payload.get("folder") not in ("deleted", "junk"):
            raise MailError("Only Deleted Items and Junk are emptied from the phone.")
        return {**request, "folder": payload["folder"]}
    if what == "awayText":
        text = str(payload.get("text") or "").replace("\r\n", "\n").strip()
        if not text:
            raise MailError("Write what people are to be told first.")
        outside = payload.get("outside")  # None: the text for people outside stays as it is
        return {**request, "text": text[:AWAY_TEXT_MOST],
                "outside": None if outside is None else str(outside).replace("\r\n", "\n").strip()[:AWAY_TEXT_MOST]}
    if what in ("folder", "rename", "category") and (not request["name"] or re.search(r'[\\/;,]', request["name"])):
        raise MailError("Give it a name, without \\ / ; or a comma.")
    if what == "folder" and request["in"] not in ("", "inbox") and not FOLDER_ID.fullmatch(request["in"]):
        raise MailError("Unknown folder.")
    if what in ("rename", "unfolder") and not FOLDER_ID.fullmatch(request["id"]):
        raise MailError("Unknown folder.")
    if what == "rule" and not request["name"]:
        raise MailError("The rule is missing.")
    if what == "away":
        return {**request, "on": payload.get("on") is True}
    if what == "ruleFrom" and (not request["id"] or not (request["to"] == "junk" or FOLDER_ID.fullmatch(request["to"]))):
        raise MailError("Say which mail and which folder.")
    return request


def clean_act_request(payload: dict) -> dict:
    """Something done to one message (delete, move, flag, mark, label) or an answer to an invitation:
    checked before Outlook is asked for anything."""
    what = payload.get("what")
    request = {
        "what": what, "id": str(payload.get("id") or ""), "answer": str(payload.get("answer") or ""),
        "to": str(payload.get("to") or ""), "categories": [],
        "note": str(payload.get("note") or "").strip()[:NOTE_MOST],  # a few words for the organizer, with the answer
        "occurrence": None,  # of a meeting that repeats: the one time that is answered, by when it starts
        "propose": None,  # another time suggested with the answer: (start, end), both UTC
    }
    if what == "respond" and isinstance(payload.get("propose"), dict):
        try:
            start, end = (dt.datetime.strptime(str(payload["propose"].get(name))[:16], "%Y-%m-%dT%H:%M") for name in ("start", "end"))
        except (TypeError, ValueError):
            raise MailError("That day or time could not be read.")
        if not dt.timedelta(0) < end - start <= dt.timedelta(days=CALENDAR_LONGEST_DAYS):
            raise MailError("The time you suggest ends before it starts.")
        request["propose"] = (start, end)
    if what == "respond" and payload.get("occurrence"):
        try:
            request["occurrence"] = local_time(payload.get("occurrence"))
        except (TypeError, ValueError):
            raise MailError("That day or time could not be read.")
    if what not in ACTS:
        raise MailError("Unknown action.")
    if not request["id"]:
        raise MailError("The message is missing.")
    if what == "respond" and request["answer"] not in ANSWERS:
        raise MailError("Unknown answer.")
    if what == "move" and not (request["to"] in ("inbox", "junk") or FOLDER_ID.fullmatch(request["to"])):
        raise MailError("Unknown folder.")
    if what == "categorize":
        names = payload.get("categories")
        if not isinstance(names, list) or len(names) > 25:
            raise MailError("Bad request.")
        for name in names:
            name = " ".join(str(name or "").split())[:100]
            if name and not re.search(r"[;,]", name) and name not in request["categories"]:
                request["categories"].append(name)
    return request


LATER_MOST_DAYS = 60  # how long a mail may wait in the Outbox to be sent
P_AWAY = PROP + "0x661D000B"  # PR_OOF_STATE: are automatic replies (out of office) switched on for this mailbox?
OL_CONTACTS, OL_NOTES, OL_TASKS = 10, 12, 13
# The pages besides mail and calendar: Outlook's tasks, notes and contacts. For each: its folder,
# the kind of thing Outlook makes for it, and what such a thing's class starts with.
THINGS = {"tasks": (OL_TASKS, 3, "IPM.Task"), "notes": (OL_NOTES, 5, "IPM.StickyNote"), "contacts": (OL_CONTACTS, 2, "IPM.Contact")}
THINGS_SHOWN = 300
THINGS_LOOK = 3000  # contacts looked through for a search, when Outlook cannot search them itself
NO_DAY = dt.datetime(4501, 1, 1, 12)  # Outlook's "no date" (noon, so that a time shifted on the way in stays on that day)


def clean_keep_request(payload: dict) -> dict:
    """A task, a note or a contact to save, tick off or take out: checked before Outlook is asked for anything."""
    kind, what = payload.get("kind"), payload.get("what")
    if kind not in THINGS or what not in ("save", "remove", "done", "undone") or (what in ("done", "undone") and kind != "tasks"):
        raise MailError("Unknown action.")
    request = {"kind": kind, "what": what, "id": str(payload.get("id") or "")}
    if what != "save":
        if not request["id"]:
            raise MailError("It is missing.")
        return request
    line = lambda name, most=255: " ".join(str(payload.get(name) or "").split())[:most]  # noqa: E731
    request.update(title=line("title"), body=str(payload.get("body") or "")[:MAX_BODY], important=payload.get("important") is True,
                   due=None, email=line("email"), mobile=line("mobile", 60), phone=line("phone", 60), company=line("company"), job=line("job"))
    if kind == "notes":
        if not request["body"].strip():
            raise MailError("Write the note first.")
        return request
    if not request["title"]:
        raise MailError("Give it a name first." if kind == "contacts" else "Say what is to be done first.")
    if kind == "tasks" and payload.get("due"):
        try:
            request["due"] = dt.datetime.strptime(str(payload.get("due")), "%Y-%m-%d").date()
        except ValueError:
            raise MailError("That day could not be read.")
    if request["email"] and not re.fullmatch(r"[^@\s<>;,]+@[^@\s<>;,]+", request["email"]):
        raise MailError("That e-mail address is not complete.")
    return request


REPEATS = {"day": 0, "week": 1, "month": 2, "year": 5, "weekday": 1}  # Outlook's numbers for how an entry repeats
WEEKDAYS = 62  # Monday to Friday, as Outlook counts the days of the week (Sunday is 1, Saturday 64)
REPEAT_INTERVAL_MOST = 99  # every so many days, weeks or months
NOTE_MOST = 2000  # characters of a note that goes along with an answer to an invitation
SHOWS = {  # what a list can be narrowed to, as Outlook is asked for it
    "unread": '"urn:schemas:httpmail:read" = 0',
    "flagged": '"http://schemas.microsoft.com/mapi/proptag/0x10900003" = 2',
}
REPEAT_MOST = 500  # times an entry made on the phone takes place
REMINDER_MOST = 28 * 24 * 60  # minutes before


def clean_plan_request(payload: dict) -> dict:
    """A calendar entry to make, change or take out. Times arrive as UTC and are put on this PC's clock.

    Of an entry that repeats, `scope` says what is meant: "one" time it takes place (named by when
    that one starts, `occurrence`) or the whole "series".
    """
    what = payload.get("what")
    request = {"what": what, "id": str(payload.get("id") or ""), "scope": "", "occurrence": None}
    if payload.get("scope") in ("one", "series"):
        request["scope"] = payload["scope"]
        if request["scope"] == "one":
            try:
                request["occurrence"] = local_time(payload.get("occurrence"))
            except (TypeError, ValueError):
                raise MailError("That day or time could not be read.")
    if what == "remove":
        if not request["id"]:
            raise MailError("The entry is missing.")
        return request
    if what != "save":
        raise MailError("Unknown action.")
    request["withExact"] = payload.get("withExact") is True  # the people listed are everyone who is invited, nobody else
    reminder = payload.get("reminder")
    request["reminder"] = None  # as Outlook sets it, or as it is
    if reminder == "none":
        request["reminder"] = -1
    elif reminder is not None:
        try:
            request["reminder"] = max(0, min(int(reminder), REMINDER_MOST))
        except (TypeError, ValueError):
            raise MailError("Bad request.")
    request["repeat"] = None
    repeat = payload.get("repeat")
    if isinstance(repeat, dict) and repeat.get("every") in REPEATS and not request["id"]:
        try:
            times = int(repeat.get("times") or 0)
        except (TypeError, ValueError):
            raise MailError("Bad request.")
        try:
            interval = int(repeat.get("interval") or 1)
        except (TypeError, ValueError):
            raise MailError("Bad request.")
        if repeat["every"] in ("year", "weekday"):
            interval = 1  # every year, every working day: nothing in between
        request["repeat"] = {
            "every": repeat["every"], "times": max(0, min(times, REPEAT_MOST)),  # 0: it goes on
            "interval": max(1, min(interval, REPEAT_INTERVAL_MOST)),
        }
    request["subject"] = " ".join(str(payload.get("subject") or "").split())[:255]
    request["location"] = " ".join(str(payload.get("location") or "").split())[:255]
    request["body"] = str(payload.get("body") or "")[:MAX_BODY]
    request["with"] = split_addresses(payload.get("with"))[:PEOPLE_RECIPIENTS]
    request["optional"] = split_addresses(payload.get("optional"))[:PEOPLE_RECIPIENTS]  # invited, but need not come
    request["optionalExact"] = payload.get("optionalExact") is True  # a page from before 0.12.0 does not list them: they stay
    request["busy"] = payload.get("busy") if payload.get("busy") in BUSY.values() else None  # None: as it is
    request["private"] = payload.get("private") if isinstance(payload.get("private"), bool) else None
    request["allDay"] = payload.get("allDay") is True
    if not request["subject"]:
        raise MailError("Give it a title first.")
    try:
        if request["allDay"]:
            first = dt.datetime.strptime(str(payload.get("day")), "%Y-%m-%d")
            request["start"] = first
            request["end"] = first + dt.timedelta(days=max(1, min(int(payload.get("days", 1)), CALENDAR_LONGEST_DAYS)))
        else:
            request["start"], request["end"] = (local_time(payload.get(name)) for name in ("start", "end"))
    except (TypeError, ValueError):
        raise MailError("That day or time could not be read.")
    if request["end"] < request["start"]:
        raise MailError("It ends before it starts.")
    if request["end"] - request["start"] > dt.timedelta(days=CALENDAR_LONGEST_DAYS):
        raise MailError(f"An entry made on the phone can be {CALENDAR_LONGEST_DAYS} days long at most.")
    return request


def local_time(utc) -> dt.datetime:
    """"2026-11-21T07:00:00Z" as the same moment on this PC's clock, without a time zone, the way Outlook takes it."""
    moment = dt.datetime.strptime(str(utc)[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=dt.timezone.utc)
    return moment.astimezone().replace(tzinfo=None)


def split_categories(text) -> list[str]:
    """Outlook keeps a message's categories as one text, parted by the list sign of this PC (a comma or a semicolon)."""
    return [name for name in (part.strip() for part in re.split(r"[;,]", str(text or ""))) if name]


def list_sign() -> str:
    """The sign this PC puts between the things of a list, which is what Outlook wants between categories."""
    if sys.platform == "win32":
        try:
            import ctypes

            written = ctypes.create_unicode_buffer(8)
            if ctypes.windll.kernel32.GetLocaleInfoW(0x0400, 0x000C, written, 8) and written.value.strip() in (",", ";"):
                return written.value.strip()
        except (AttributeError, OSError, ValueError):
            pass
    return ","


def _registry_text(value) -> str:
    if isinstance(value, bytes):  # Outlook writes these names as UTF-16
        value = value.decode("utf-16-le", "ignore")
    return str(value or "").split("\x00", 1)[0].strip()


def signature_setting() -> tuple[str, str, str]:
    """(folder name, signature for a new mail, signature for an answer) as Outlook is set on this PC.

    Read from Windows' registry, where Outlook keeps them for each account; the first account that
    names one counts. Empty where the registry does not say.
    """
    if sys.platform != "win32":
        return "", "", ""
    import winreg

    folder = new = reply = ""
    for version in ("16.0", "15.0", "14.0"):
        office = rf"Software\Microsoft\Office\{version}"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, office + r"\Common\General") as key:
                folder = folder or _registry_text(winreg.QueryValueEx(key, "Signatures")[0])
        except OSError:
            pass
        try:
            profiles = winreg.OpenKey(winreg.HKEY_CURRENT_USER, office + r"\Outlook\Profiles")
        except OSError:
            continue
        with profiles:
            for number in range(winreg.QueryInfoKey(profiles)[0]):
                accounts = winreg.EnumKey(profiles, number) + r"\9375CFF0413111d3B88A00104B2A6676"
                try:
                    with winreg.OpenKey(profiles, accounts) as key:
                        for index in range(winreg.QueryInfoKey(key)[0]):
                            with winreg.OpenKey(key, winreg.EnumKey(key, index)) as account:
                                for name in ("New Signature", "Reply-Forward Signature"):
                                    try:
                                        text = _registry_text(winreg.QueryValueEx(account, name)[0])
                                    except OSError:
                                        continue
                                    if name.startswith("New"):
                                        new = new or text
                                    else:
                                        reply = reply or text
                except OSError:
                    continue
        if new or reply:
            break
    return folder, new, reply


def read_signatures(folder: Path | None = None, setting=signature_setting) -> dict:
    """{"new", "reply"}: the text of the signature Outlook puts under a new mail and under an answer.

    Outlook keeps every signature as files in the user's own folder, one of them plain text. That
    text is what the phone shows under a mail being written, where it can be changed or taken out.
    Formatting and pictures of a signature do not come along. Empty when there is none.
    """
    try:
        named, new, reply = setting()
        folder = folder or Path(os.environ.get("APPDATA", "")) / "Microsoft" / (named or "Signatures")
        texts = {}
        for path in sorted(Path(folder).glob("*.txt"))[:50]:
            raw = path.read_bytes()[:40_000]
            if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                text = raw.decode("utf-16", "ignore")
            else:
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = raw.decode("mbcs" if sys.platform == "win32" else "latin-1", "ignore")
            texts[path.stem] = text.replace("\r\n", "\n").strip()[:4000]
        if not new and not reply and len(texts) == 1:  # one signature and no setting found: it is the one for a new mail
            return {"new": next(iter(texts.values())), "reply": ""}
        return {"new": texts.get(new, ""), "reply": texts.get(reply, "")}
    except Exception:  # a signature is a convenience: without one the mail is written as before
        return {"new": "", "reply": ""}


def as_person(name, address) -> dict:
    """{name, address} for the phone. The address only when it is an internet one; a colleague Outlook
    knows only by an internal name keeps the name, and Outlook finds him again by it."""
    address = str(address or "").strip()
    if not re.fullmatch(r"[^@\s<>;,]+@[^@\s<>;,]+", address):
        address = ""
    name = " ".join(str(name or "").strip("'\" ").split())
    return {"name": name or address, "address": address}


def answer_plans(sender: dict, to: list, cc: list, reply_to: list, mine) -> dict:
    """Who Reply and Reply all go to, the way Outlook addresses them, for the phone to show and change.

    Reply: whoever the mail asks to be answered (Reply-To), or else the sender. Reply all: the same,
    and everyone else it went to, without yourself. An answer to a mail of your own goes to the
    people it went to.
    """
    mine = {str(address).lower() for address in mine if address}

    def key(one: dict) -> str:
        return (one["address"] or one["name"]).lower()

    def others(people: list, taken: set) -> list:
        kept = []
        for one in people:
            if not key(one) or key(one) in taken or one["address"].lower() in mine:
                continue
            taken.add(key(one))
            kept.append(one)
        return kept

    from_me = bool(sender["address"]) and sender["address"].lower() in mine
    first = list(reply_to) or ([] if from_me else [sender])
    if from_me and not first:
        first = others(to, set()) or [sender]
    taken: set = set()
    reply = []
    for one in first:  # yourself stays here: an answer to your own note goes to you
        if key(one) and key(one) not in taken:
            taken.add(key(one))
            reply.append(one)
    everyone = set(taken)
    all_to = reply + others(to, everyone)
    return {"reply": {"to": reply, "cc": []}, "replyAll": {"to": all_to, "cc": others(cc, everyone)}}


def today() -> dt.datetime:
    """Midnight that began today, on this PC's clock."""
    return dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


def short_date(moment: dt.datetime) -> str:
    """A date the way this PC writes dates, which is how Outlook reads one in a filter."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class SystemTime(ctypes.Structure):
                _fields_ = [(name, wintypes.WORD) for name in (
                    "year", "month", "weekday", "day", "hour", "minute", "second", "milliseconds")]

            stamp = SystemTime(moment.year, moment.month, 0, moment.day, 12, 0, 0, 0)
            written = ctypes.create_unicode_buffer(80)
            # the signed-in user's own format, short date
            if ctypes.windll.kernel32.GetDateFormatW(0x0400, 0x0001, ctypes.byref(stamp), None, written, 80):
                return written.value
        except (AttributeError, OSError, ValueError):
            pass
    return moment.strftime("%m/%d/%Y")


# The ways a date is written for Outlook's calendar filter, tried in this order: as this PC writes
# dates (what Outlook's documentation asks for), year first, month first. See OutlookMailbox._calendar.
DATE_WAYS = (short_date, lambda moment: moment.strftime("%Y-%m-%d"), lambda moment: moment.strftime("%m/%d/%Y"))


# ============================================================ Outlook (COM)

class _Job:
    __slots__ = ("fn", "args", "done", "state", "what", "counted_out")

    def __init__(self, fn, args):
        self.fn, self.args = fn, args
        self.what = ""
        self.counted_out = False  # a send is counted as waiting until Outlook takes it, or its caller gives up
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
        self._running_what = ""  # in words, for the message a send gets when it could not wait any longer
        self._running_first = False
        self._first_waiting = 0  # sends (and the like) that wait for the call Outlook has now
        threading.Thread(target=self._run, daemon=True, name="titip-outlook").start()

    def _run(self):
        import pythoncom

        pythoncom.CoInitialize()
        while True:
            first, _, job = self._jobs.get()
            with self._lock:
                if first == 0 and not job.counted_out:
                    job.counted_out = True
                    self._first_waiting -= 1
                if job.state == "dropped":
                    continue
                job.state = "running"
                self._running_since = time.monotonic()
                self._running_what = job.what
                self._running_first = first == 0
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

    def must_give_way(self) -> bool:
        """Is a send waiting behind the reading that runs now? Long readings ask between two steps, and stop."""
        return self._first_waiting > 0 and not self._running_first

    def call(self, fn, *args, timeout: float = READ_SECONDS, first: bool = False, what: str = ""):
        """`first` puts the call ahead of everything that waits: a send does not queue behind reading ahead."""
        stuck = self.stuck_for()
        if stuck >= timeout:  # Outlook has been silent longer than this caller would wait anyway
            raise OutlookBusy(False, self._running_what, stuck)
        job = _Job(fn, args)
        job.what = what
        if first:
            with self._lock:
                self._first_waiting += 1
        self._jobs.put((0 if first else 1, next(self._arrival), job))
        try:
            ok, value = job.done.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                started = job.state == "running"
                job.state = "dropped"
                if first and not job.counted_out:
                    job.counted_out = True
                    self._first_waiting -= 1
            raise OutlookBusy(started, "" if started else self._running_what, self.stuck_for())
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
        self._mine: set[str] | None = None  # every address Outlook sends from, in lower case
        self._calendars: dict[tuple, tuple[float, dict]] = {}  # (offset, days) -> (monotonic, page)
        self._date_way: int | None = None  # which of DATE_WAYS Outlook on this PC has understood
        self._folder_list: tuple[float, dict] | None = None  # (monotonic, the folders and the categories)
        self._answers: dict[tuple, tuple[float, object]] = {}  # other slow answers kept: key -> (monotonic, answer)
        self._signatures: dict | None = None
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
            return self._attempt(fn, args, pythoncom.com_error)

        if self._thread is None:
            self._thread = _ComThread()
        return self._thread.call(guarded, timeout=timeout, first=first, what=DOING.get(getattr(fn, "__name__", ""), ""))

    def _attempt(self, fn, args: tuple, com_error, pause=time.sleep):
        """Run one step against Outlook. A call Outlook turns away is made again a few times.

        Outlook turns a call away (RPC_E_CALL_REJECTED) while a window of its own is in the way or it
        serves another program. A call turned away was not carried out, so the whole step can be
        made again without anything happening twice: a send that got as far as making the mail
        throws that mail away before it starts over (see _send).
        """
        for attempt in range(RPC_TRIES):
            try:
                self._connect()
                return fn(*args)
            except com_error as error:
                if error.hresult in RPC_BUSY:
                    if attempt + 1 < RPC_TRIES:
                        pause(RPC_PAUSE)
                        continue
                    raise OutlookBusy(False, "busy with a window of its own, or with another program", RPC_TRIES * RPC_PAUSE)
                self._app = None  # Outlook was closed or restarted: reconnect on the next call
                self._folders.clear()
                detail = ""
                if error.excepinfo and error.excepinfo[2]:
                    detail = str(error.excepinfo[2]).strip()
                if "inline response" in detail.lower():  # Outlook's words for it; said the way the phone can act on
                    raise MailError(IN_THE_PANE)
                raise MailError(detail or "Outlook on your PC did not answer. Open Outlook and try again.")

    def _give_way(self) -> None:
        """Called between two steps of a long reading: when a send waits, the reading stops here.
        The phone asks for it again by itself; the send does not wait out the whole reading."""
        if self._thread is not None and self._thread.must_give_way():
            raise OutlookBusy()

    def _connect(self):
        if self._app is not None:
            return
        import win32com.client

        self._app = win32com.client.Dispatch("Outlook.Application")
        self._ns = self._app.GetNamespace("MAPI")
        self._stores.clear()
        self._folders.clear()
        self._who = None
        self._mine = None

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
        if name not in OL_FOLDER:
            return [self._folder(name)]  # a folder of your own: only itself
        default = self._ns.GetDefaultFolder(OL_FOLDER[name])
        found = [default]
        if name in ("inbox", "sent"):
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
        order = ORDER[name]

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
            # A slow question (the calendar of a large mailbox takes minutes) is waited for long after the
            # phone stopped waiting, so that its answer is there for the phone's next try. Asking again
            # each time the phone gave up would keep Outlook at it without end, and never have an answer.
            ask.value = self._do(fn, *args, timeout=SLOW_SECONDS if key[0] in SLOW else ASK_SECONDS)
            self._keep(key, ask.value)
        except MailError as error:
            ask.error = error
        except Exception:
            ask.error = MailError("Outlook could not be reached on this PC.")
        finally:
            with self._lock:
                self._asking.pop(key, None)
            ask.done.set()

    def _keep(self, key: tuple, value) -> None:
        with self._lock:
            if key[0] == "list":
                stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                self._lists[key[1:]] = (time.monotonic(), stamp, value)
                return
            if key[0] == "calendar":
                if len(self._calendars) >= CALENDAR_KEPT:
                    self._calendars.clear()
                self._calendars[key[1:]] = (time.monotonic(), value)
                return
            if key[0] == "folders":
                self._folder_list = (time.monotonic(), value)
                return
            if key[0] != "item":  # the other calendars, the rules, what an invitation is for
                if key[0] == "narrowed":  # searches come one per word typed: the ones nobody can be waiting for any more go first
                    self._forget_narrowed(time.monotonic() - LIST_FRESH_SECONDS)
                if len(self._answers) >= 60:
                    self._answers.clear()
                self._answers[key] = (time.monotonic(), value)
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
        counts = {name: sum(f.Items.Count for f in self._mail_folders(name)) for name in FOLDERS}
        try:  # one small folder: a mail that waits there has not left yet, and the phone says so
            counts["outbox"] = self._ns.GetDefaultFolder(OL_OUTBOX).Items.Count
        except Exception:
            counts["outbox"] = 0
        return {
            "ok": True,
            "demo": False,
            "account": who[0],
            "name": who[1],
            "counts": counts,
            "away": self._away(),
        }

    def _away_note(self, outside: bool = False):
        """The hidden note in the Inbox that holds what colleagues are told automatically, or the one for
        people outside the organization (2: found by its class)."""
        return self._ns.GetDefaultFolder(OL_INBOX).GetStorage(OOF_OUTSIDE if outside else OOF_TEMPLATE, 2)

    def away(self) -> dict:
        """Are automatic replies on, and what do they say? Asked when the phone opens that sheet."""
        self._phone_asked()
        return self._do(self._away_now)

    def _away_now(self) -> dict:
        try:
            text = str(self._away_note().Body or "").replace("\r\n", "\n").strip()
        except Exception as error:
            if getattr(error, "hresult", None) in RPC_BUSY:
                raise
            text = None  # this mailbox keeps no such note (not an Exchange one)
        outside = None
        if text is not None:
            try:
                outside = str(self._away_note(True).Body or "").replace("\r\n", "\n").strip()
            except Exception as error:
                if getattr(error, "hresult", None) in RPC_BUSY:
                    raise
        return {"on": self._away(), "text": text, "outside": outside}

    def _away(self) -> bool | None:
        """Are automatic replies (out of office) on? None when this mailbox does not say (not an Exchange one)."""
        try:
            return bool(self._ns.DefaultStore.PropertyAccessor.GetProperty(P_AWAY))
        except Exception:
            return None

    # -------------------------------------------------- list
    def list(self, folder: str, limit: int = 40, offset: int = 0, query: str = "", deep: bool = False, show: str = "") -> dict:
        """One page of a folder. The first page comes from the copy in memory when Outlook is slow.

        A copy older than LIST_FRESH_SECONDS is only a fallback: Outlook is asked first, and when it
        does not answer within QUICK_SECONDS the copy is returned marked `saved`, with its time.
        Searches and older pages always wait for Outlook. `deep`: a search also looks in the text of the mail.
        `show`: only what is "unread" or "flagged"; asked of Outlook like a search, and not kept as the folder's list.
        """
        if folder not in ALL_FOLDERS and not FOLDER_ID.fullmatch(folder):
            raise MailError("Unknown folder.")
        if show and show not in SHOWS:
            raise MailError("Unknown filter.")
        limit = max(1, min(int(limit), 100))
        self._phone_asked()
        if int(offset) > 0 or (query or "").strip() or show:
            # A search letter by letter, an older page or a narrowed list can take Outlook longer than the
            # phone waits. It is asked once however often the phone asks meanwhile, and its answer is kept
            # a moment for the try that comes after it arrived: what 0.10.3 did for the calendar.
            key = ("narrowed", folder, limit, int(offset), " ".join(str(query or "").split()).lower(), bool(deep), show)
            kept = self._answers.get(key)
            if kept and time.monotonic() - kept[0] < LIST_FRESH_SECONDS:
                page = kept[1]
            else:
                page = self._shared(key, self._list, folder, limit, offset, query,
                                    *((bool(deep), show) if show else (True,) if deep else ()), wait=READ_SECONDS)
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

    @staticmethod
    def _text_filter(mail_folder, text: str, who: str) -> str:
        """A search that also looks in the text of the mail. Where Outlook keeps an index of the data
        file it is asked by word, which is quick; elsewhere letter by letter, which reads every mail."""
        indexed = False
        try:
            indexed = bool(mail_folder.Store.IsInstantSearchEnabled)
        except Exception:
            pass
        how, around = ("ci_phrasematch", "") if indexed else ("like", "%")
        return "@SQL=(" + " OR ".join(
            f'"{field}" {how} \'{around}{text}{around}\'' for field in ("urn:schemas:httpmail:subject", who, BODY_TEXT)
        ) + ")"

    def _list(self, folder: str, limit: int, offset: int, query: str, deep: bool = False, show: str = "") -> dict:
        if folder not in ALL_FOLDERS and not FOLDER_ID.fullmatch(folder):
            raise MailError("Unknown folder.")
        limit = max(1, min(int(limit), 100))
        offset = max(0, min(int(offset), MAX_LIST))
        want = offset + limit
        when = {"sent": P_SUBMITTED, "drafts": P_MODIFIED, "outbox": P_MODIFIED}.get(folder, P_DELIVERED)  # read as UTC
        order = ORDER.get(folder, "[ReceivedTime]")
        late = folder == "deleted" or folder not in ALL_FOLDERS  # may hold what was never delivered: a draft, a sent mail
        table_filter = ""
        words = re.sub(r"[%_\[\]'\"]", " ", query or "").split()
        if words:
            text = " ".join(words)[:80]
            who = "urn:schemas:httpmail:displayto" if folder in TO_FOLDERS else "urn:schemas:httpmail:fromname"
            table_filter = (
                f'@SQL=("urn:schemas:httpmail:subject" like \'%{text}%\' OR "{who}" like \'%{text}%\')'
            )
        only = f" AND ({SHOWS[show]})" if show in SHOWS and words else f"@SQL=({SHOWS[show]})" if show in SHOWS else ""
        if words:
            table_filter += only
        elif only:
            table_filter = only
        rows = []
        for mail_folder in self._mail_folders(folder):
            self._give_way()  # between two data files
            store_key = self._store_key(mail_folder.StoreID)
            table = None
            if deep and words:
                try:
                    table = mail_folder.GetTable(self._text_filter(mail_folder, text, who) + only, 0)
                except Exception as error:
                    if getattr(error, "hresult", None) in RPC_BUSY:
                        raise
                    table = None  # this data file cannot be searched by text: subject and people, as without it
            if table is None:
                table = mail_folder.GetTable(table_filter, 0) if table_filter else mail_folder.GetTable()
            columns = table.Columns
            columns.RemoveAll()
            names = ["EntryID", "Subject", "SenderName", "MessageClass", "UnRead", when, P_HAS_ATTACH, P_DISPLAY_TO]
            if late:  # placed by when it last changed
                names.append(P_MODIFIED)
            for column in names:
                columns.Add(column)
            # what a folder may not be able to tell: left out there, and the rest is listed all the same
            at = {}
            for name, column in (("preview", P_PREVIEW), ("flag", P_FLAG), ("importance", "Importance")):
                try:
                    columns.Add(column)
                    at[name] = len(names) + len(at)
                except Exception:
                    pass
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
                    "time": _utc_iso(row[5]) or (_utc_iso(row[8]) if late else ""),
                    "attachments": bool(row[6]),
                    "to": row[7] or "",
                    "preview": " ".join(str(row[at["preview"]] or "").split())[:180] if "preview" in at else "",
                    "flagged": "flag" in at and row[at["flag"]] == 2,
                    "important": "importance" in at and row[at["importance"]] == 2,
                })
            if words and len(rows) >= want:
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
        store_id = self._stores.get(store_key) or self._store_id(store_key)
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
        except OutlookBusy:
            self._rows[mail_id] = known  # not looked for yet: it may be looked for again
            raise
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
        sender_name = getattr(item, "SenderName", "") or ""
        went_to, cut = [], False
        try:
            went_to, cut = self._recipients(item.Recipients, RECIPIENTS_READ)
        except Exception:  # a kind of item without recipients, or one Outlook cannot list them of
            pass
        self._count_people(mail_id, item, sender_name, sender_address, went_to[:PEOPLE_RECIPIENTS])
        people = {kind: [as_person(name, address) for name, address, type_ in went_to if type_ == number]
                  for kind, number in (("to", 1), ("cc", 2), ("bcc", 3))}
        reply_to = []
        try:
            reply_to = [as_person(name, address) for name, address, _ in self._recipients(item.ReplyRecipients, 20)[0]]
        except Exception:
            pass
        message_class = str(getattr(item, "MessageClass", "") or "")
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
            "from": sender_name,
            "fromAddress": sender_address,
            "to": getattr(item, "To", "") or "",
            "cc": getattr(item, "CC", "") or "",
            "bcc": getattr(item, "BCC", "") or "",
            "people": people,
            "peopleCut": cut,
            "answer": answer_plans(as_person(sender_name, sender_address), people["to"], people["cc"], reply_to, self._my_addresses()),
            "kind": "meeting" if message_class.startswith("IPM.Schedule") else "mail",
            # what an invitation is for is asked when it is opened (invite()), not for every mail read ahead
            "invite": None,
            "inviteAsk": message_class.startswith("IPM.Schedule.Meeting.Request"),
            "flagged": getattr(item, "FlagStatus", 0) == 2,
            "important": getattr(item, "Importance", 1) == 2,
            "categories": split_categories(getattr(item, "Categories", "")),
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

    @staticmethod
    def _recipients(recipients, limit: int) -> tuple[list[tuple[str, str, int]], bool]:
        """(name, address, kind) of the people a message went to, and whether there are more than `limit`.

        Kind is Outlook's: 1 To, 2 Cc, 3 Bcc. The address is the internet one when the message
        carries it; for a colleague it may be Outlook's internal name instead.
        """
        count = recipients.Count
        found = []
        for index in range(1, min(count, limit) + 1):
            recipient = recipients.Item(index)
            try:
                if index > ACCESSOR_RECIPIENTS:  # of a mail to very many, the rest by what the recipient says itself
                    raise LookupError
                address = recipient.PropertyAccessor.GetProperty(P_SMTP)
            except Exception:
                address = getattr(recipient, "Address", "")
            found.append((getattr(recipient, "Name", "") or "", address or "", int(getattr(recipient, "Type", 1) or 1)))
        return found, count > limit

    def _my_addresses(self) -> set[str]:
        """Every address Outlook on this PC sends from: none of them is somebody else to answer."""
        if self._mine is not None:
            return self._mine
        found = set()
        try:
            for account in self._ns.Accounts:
                address = str(account.SmtpAddress or "").strip().lower()
                if address:
                    found.add(address)
        except Exception:
            pass
        try:
            first = self._account()[0].lower()
            if first:
                found.add(first)
        except Exception:
            pass
        if found:  # asked again next time when Outlook could not say
            self._mine = found
        return found

    def _note_people(self, mail_id: str) -> None:
        """One message read only for who wrote it and who it went to."""
        with self._lock:
            if self._moved.get(mail_id, mail_id) in self._people_counted:
                return
        item = self._item(mail_id)
        mail_id = self._moved.get(mail_id, mail_id)
        sender = self._sender_address(item, item.PropertyAccessor)
        self._count_people(mail_id, item, getattr(item, "SenderName", "") or "", sender)

    def _count_people(self, mail_id: str, item, sender_name: str, sender_address: str, went_to=None) -> None:
        """Remember the people on this message. Each message counts once; never fails the read it is part of.

        `went_to`: the recipients when the caller has read them already, as _recipients gives them.
        """
        with self._lock:
            if mail_id in self._people_counted:
                return
            self._people_counted[mail_id] = None
            while len(self._people_counted) > KEEP_ROWS:
                self._people_counted.popitem(last=False)
        try:
            if went_to is None:
                went_to = self._recipients(item.Recipients, PEOPLE_RECIPIENTS)[0]
            found = [(name, address) for name, address, _ in went_to]
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

    def lookup(self, query: str) -> dict:
        """People in Outlook's address book whose name starts with what was typed, each with the address.

        Asked while a name is typed, so it goes ahead of the reading ahead and never makes the
        phone wait or try again: a busy Outlook is an empty answer marked `busy`.
        """
        needle = " ".join(str(query or "").split())[:LOOKUP_LONGEST]
        if len(needle) < LOOKUP_SHORTEST:
            return {"people": [], "book": True}
        self._phone_asked()
        try:
            return {"people": self._do(self._lookup, needle, timeout=LOOKUP_SECONDS, first=True), "book": True}
        except OutlookBusy:
            return {"people": [], "book": True, "busy": True}

    def _lookup(self, needle: str) -> list[dict]:
        book = self._ns.GetGlobalAddressList()
        if book is None:  # an account without a company directory
            return []
        entries = book.AddressEntries
        found: list[dict] = []
        seen = set()

        def take(entry, name: str) -> None:
            try:
                address = entry.PropertyAccessor.GetProperty(P_SMTP) or ""
            except Exception:
                address = getattr(entry, "Address", "") or ""
            address = str(address).strip()
            if re.fullmatch(r"[^@\s<>;,]+@[^@\s<>;,]+", address) and address.lower() not in seen:
                seen.add(address.lower())
                found.append({"name": " ".join(name.split()) or address, "address": address})

        # The book is sorted by name and Outlook can only jump to the first name at or after a
        # text. So: jump to what was typed, then to just past each name found, a few times.
        seek, last = needle, None
        for _ in range(LOOKUP_ROWS):
            try:
                entry = entries.Item(seek)
                name = str(entry.Name or "")
            except Exception:  # past the last name
                break
            if not name.lower().startswith(needle.lower()) or name == last:
                break
            take(entry, name)
            seek, last = name + "\x01", name
        if not found and "@" not in needle:
            # not the start of a name: a short name (alias) that Outlook can match to one person
            try:
                recipient = self._ns.CreateRecipient(needle)
                if recipient.Resolve():
                    take(recipient.AddressEntry, str(recipient.Name or ""))
            except Exception:
                pass
        return found

    # -------------------------------------------------- what a phone that is put away is told
    def news(self) -> list[dict]:
        """Mail that came into the Inbox since the last look, and the reminders Outlook is showing:
        for the phones that asked to be told while Titip is closed (titip/push.py). Never starts
        Outlook, and a busy Outlook is simply asked again at the next look."""
        if not outlook_running():
            return []
        try:
            return self._do(self._news_now, timeout=READ_SECONDS)
        except OutlookBusy:
            return []

    def _news_now(self) -> list[dict]:
        state = self.__dict__.setdefault("_news_state", {
            "since": None, "first": True, "mails": collections.OrderedDict(), "reminders": collections.OrderedDict()})
        found = []
        rows = self._list("inbox", NEWS_ROWS, 0, "")["mail"]
        if state["since"] is not None:  # at the first look nothing is news: only what comes after it
            for row in reversed(rows):  # the oldest first, the way they came
                if row["unread"] and row["time"] > state["since"] and row["id"] not in state["mails"]:
                    found.append({"kind": "mail", "title": row["from"] or "New mail", "body": row["subject"],
                                  "tag": "mail." + row["id"][-32:], "url": "#/inbox/" + row["id"]})
        for row in rows:
            state["mails"][row["id"]] = None
        state["since"] = max([state["since"] or "", *(row["time"] for row in rows)])
        # Outlook's own reminders, the ones its reminder window is showing now: an appointment, a task, a flagged mail
        # From here on nothing is raised: the mail found above is remembered as told already, so it must be handed
        # back. An Outlook that turns busy in the middle shows its reminders at the next look instead.
        try:
            reminders = self._app.Reminders
            count = min(int(reminders.Count), NEWS_REMINDERS)
        except Exception:
            count = 0
        for index in range(1, count + 1):
            try:
                one = reminders.Item(index)
                if not bool(one.IsVisible):
                    continue
                caption = " ".join(str(one.Caption or "").split())
                due = one.NextReminderDate
                key = (caption, f"{due.year}-{due.month}-{due.day} {due.hour}:{due.minute}")
            except Exception as error:
                if getattr(error, "hresult", None) in RPC_BUSY:
                    return found  # before the first look is called done: what is showing now is still not news next time
                continue
            if key in state["reminders"]:
                continue
            state["reminders"][key] = None
            if state["first"]:
                continue  # it was showing already when the first phone asked to be told: not news
            when = ""
            try:
                start = one.Item.Start
                when = f", {start.hour:02d}:{start.minute:02d}"
            except Exception:
                pass  # a task or a flagged mail has no start: the caption says it all
            found.append({"kind": "reminder", "title": "Reminder", "body": caption + when,
                          "tag": "remind." + hashlib.sha1(repr(key).encode("utf-8")).hexdigest()[:16], "url": "#/calendar"})
        state["first"] = False
        for kept in (state["mails"], state["reminders"]):
            while len(kept) > NEWS_KEPT:
                kept.popitem(last=False)
        return found

    def person(self, who: str) -> dict:
        """What Outlook knows about one person, by name or address: the company directory first, then
        your contacts. Asked when a name is tapped, so it goes first and never makes the phone wait long."""
        who = " ".join(str(who or "").split())[:254]
        if len(who) < LOOKUP_SHORTEST:
            raise MailError("Nobody was named.")
        self._phone_asked()
        try:
            return self._do(self._person, who, timeout=LOOKUP_SECONDS, first=True)
        except OutlookBusy:
            return {"found": False, "busy": True}

    def _person(self, who: str) -> dict:
        recipient = self._ns.CreateRecipient(who)
        if not recipient.Resolve():  # nobody, or more than one, by that name
            return {"found": False}
        entry = recipient.AddressEntry
        try:
            address = entry.PropertyAccessor.GetProperty(P_SMTP) or ""
        except Exception as error:
            if getattr(error, "hresult", None) in RPC_BUSY:
                raise
            address = getattr(entry, "Address", "") or ""
        card = {"found": True, "known": False, "name": " ".join(str(recipient.Name or "").split()),
                "address": str(address) if "@" in str(address) else (who if "@" in who else ""),
                "job": "", "department": "", "company": "", "office": "", "phones": []}
        source = None
        for ask in ("GetExchangeUser", "GetContact"):  # a colleague in the directory, or somebody in your contacts
            try:
                source = getattr(entry, ask)()
            except Exception as error:
                if getattr(error, "hresult", None) in RPC_BUSY:
                    raise
                source = None
            if source is not None:
                break
        if source is None:
            return card
        text = lambda name: " ".join(str(getattr(source, name, "") or "").split())  # noqa: E731
        smtp = text("PrimarySmtpAddress") or text("Email1Address")
        if "@" in smtp:
            card["address"] = smtp
        card.update(name=text("Name") or text("FullName") or card["name"], job=text("JobTitle"), department=text("Department"),
                    company=text("CompanyName"), office=text("OfficeLocation"))
        for label, field in (("Mobile", "MobileTelephoneNumber"), ("Work", "BusinessTelephoneNumber")):
            if text(field):
                card["phones"].append({"label": label, "number": text(field)})
        card["known"] = bool(card["job"] or card["department"] or card["company"] or card["office"] or card["phones"])
        return card

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
            raise MailError("Outlook on your PC is busy, so nothing was sent." + error.why() + " Your text is kept here. Try again in a moment.")
        finally:
            with self._lock:  # Sent and Drafts have changed (or may have): no old copy of them is shown
                self._lists.clear()
                self._forget_narrowed()
                if request["mode"] == "draft":
                    self._drop_message(self._moved.get(request["id"], request["id"]))

    # -------------------------------------------------- a draft Outlook has open in its reading pane
    # A reply started in the reading pane, and a draft clicked in the list, is written right there
    # ("inline response"). While it is, Outlook refuses Send, Delete and Move on it from anywhere
    # else: "This method can't be used with an inline response mail item." The object model has no
    # call that sends it all the same. What works is what a person does: look at something else
    # first. So Titip takes the draft out of the pane, does what was asked, and puts the window back.
    def _in_the_pane(self, entry_id: str, explorer) -> bool:
        """Is this the draft that `explorer` (one Outlook window) has open for writing in its reading pane?"""
        try:
            inline = explorer.ActiveInlineResponse
            other = "" if inline is None else str(getattr(inline, "EntryID", "") or "")
            if not other:  # nothing is written there, or something that was never saved: not this draft
                return False
            return other.upper() == entry_id.upper() or bool(self._ns.CompareEntryIDs(entry_id, other))
        except Exception as error:
            if getattr(error, "hresult", None) in RPC_BUSY:
                raise
            return False  # an Outlook from before 2013 has no such pane

    def _held_in_the_pane(self, mail_id: str) -> list:
        """The Outlook windows that have this message open for writing in their reading pane."""
        entry_id = self._moved.get(mail_id, mail_id).partition(".")[2]
        try:
            explorers = self._app.Explorers
            windows = [explorers.Item(index) for index in range(1, explorers.Count + 1)]
        except Exception as error:
            if getattr(error, "hresult", None) in RPC_BUSY:
                raise
            return []
        return [explorer for explorer in windows if entry_id and self._in_the_pane(entry_id, explorer)]

    def _out_of_the_pane(self, mail_id: str, held: list, pause=time.sleep) -> list:
        """Make every window in `held` let go of the draft. Three ways, the gentlest first: select
        nothing, show another folder, open the draft in a window of its own. After each, Outlook is
        asked whether the draft is still in the pane. What comes back: (window, folder it showed)
        for each window that was made to show another folder, to be put back afterwards.
        """
        entry_id = self._moved.get(mail_id, mail_id).partition(".")[2]
        back = []

        def nothing_selected(explorer):
            explorer.ClearSelection()

        def another_folder(explorer):
            was = explorer.CurrentFolder
            sent = self._ns.GetDefaultFolder(OL_SENT)  # nothing in Sent is written in the pane
            if self._ns.CompareEntryIDs(was.EntryID, sent.EntryID):
                sent = self._ns.GetDefaultFolder(OL_INBOX)
            back.append((explorer, was))
            explorer.CurrentFolder = sent

        def a_window_of_its_own(explorer):
            explorer.ActiveInlineResponse.Display()

        try:
            for explorer in held:
                for way in (nothing_selected, another_folder, a_window_of_its_own):
                    try:
                        way(explorer)
                    except Exception as error:
                        if getattr(error, "hresult", None) in RPC_BUSY:
                            raise
                        continue  # this Outlook, or this view, cannot do that: the next way
                    waited = 0.0
                    while self._in_the_pane(entry_id, explorer) and waited < PANE_WAIT:
                        pause(PANE_PAUSE)
                        waited += PANE_PAUSE
                    if not self._in_the_pane(entry_id, explorer):
                        break
                else:
                    raise MailError(IN_THE_PANE)
        except Exception:
            self._put_back(back)  # nothing is done to the draft: the windows show what they showed
            raise
        return back

    @staticmethod
    def _put_back(back: list) -> None:
        """Show again the folder a window showed. Never an error: what was asked for is done by now."""
        for explorer, folder in back:
            try:
                explorer.CurrentFolder = folder
            except Exception:
                pass

    def _send(self, request: dict) -> dict:
        held = self._held_in_the_pane(request["id"]) if request["mode"] == "draft" else []
        if not held:
            return self._send_it(request)
        # What the phone was shown is the draft as it was saved last. Leaving the pane makes Outlook
        # save what was typed there since: text the phone did not change must not undo that.
        try:
            shown = self._item(request["id"]).Body or ""
        except Exception as error:
            if isinstance(error, MailError) or getattr(error, "hresult", None) in RPC_BUSY:
                raise
            shown = None
        back = self._out_of_the_pane(request["id"], held)
        try:
            return self._send_it(request, shown)
        finally:
            self._put_back(back)

    def _send_it(self, request: dict, shown: str | None = None) -> dict:
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
        folder = None  # where the attached files rest for the moment Outlook reads them
        try:
            if request["exact"] and request["kept"] and mode in ("reply", "replyAll"):
                pass  # nobody was added or taken out on the phone: it goes to whom Outlook addressed it
            elif request["exact"] and mode in ("reply", "replyAll", "forward"):
                self._address(mail, request)  # the phone showed who it goes to: exactly those
            elif mode in ("reply", "replyAll"):  # a page from before 0.9.0: Outlook addressed it, only add to that
                for kind, addresses in ((1, request["to"]), (2, request["cc"]), (3, request["bcc"])):
                    for address in addresses:
                        mail.Recipients.Add(address).Type = kind
            else:
                mail.To = "; ".join(request["to"])
                mail.CC = "; ".join(request["cc"])
                if request["bcc"] or mode == "draft":
                    mail.BCC = "; ".join(request["bcc"])
            if request["subject"]:
                mail.Subject = request["subject"]
            if request["importance"]:
                mail.Importance = {"high": 2, "low": 0}[request["importance"]]
            if request["receipt"]:
                mail.ReadReceiptRequested = True
            block = text_to_html(request["body"], request["marks"])
            if mode == "draft":
                # Sent as it was opened: keep Outlook's formatting, signature and pictures untouched.
                typed = " ".join(request["body"].split())
                if typed != " ".join((mail.Body or "").split()) and (shown is None or typed != " ".join(shown.split())):
                    mail.HTMLBody = f"<html><body>{block}</body></html>"
            else:
                # No GetInspector here to pull in a signature: it would leave a hidden editor window
                # inside Outlook for every mail. Whatever Outlook put in the body stays below the text.
                mail.HTMLBody = insert_into_html(mail.HTMLBody, block)
            for index, name in request["drop"]:  # highest number first, so the others keep theirs
                self._take_out(mail, index, name)
            if request["files"]:
                folder = Path(tempfile.mkdtemp(prefix="titip-"))
                for number, file in enumerate(request["files"]):
                    place = folder / str(number)  # a folder each: two files may have the same name
                    place.mkdir()
                    (place / file["name"]).write_bytes(file["data"])
                    added = mail.Attachments.Add(str(place / file["name"]))  # Outlook copies it into the mail
                    # "[picture: name]" on a line of the text: the picture is shown there, not only attached
                    spot = html.escape(f"[picture: {file['name']}]")
                    if file["name"].lower().endswith(PICTURES) and spot in (mail.HTMLBody or ""):
                        called = f"titip{number}.{os.urandom(4).hex()}@titip"
                        try:
                            added.PropertyAccessor.SetProperty(P_CONTENT_ID, called)
                        except Exception as error:
                            if getattr(error, "hresult", None) in RPC_BUSY:
                                raise
                            continue  # it goes along as a file, and the line stays as typed
                        mail.HTMLBody = mail.HTMLBody.replace(
                            spot, f'<img src="cid:{called}" alt="{html.escape(file["name"])}" style="max-width:100%">', 1)
            if mail.Recipients.Count and not mail.Recipients.ResolveAll():
                unknown = [r.Name for r in mail.Recipients if not r.Resolved]
                raise MailError("Outlook does not know: " + ", ".join(unknown) + ". Use a full e-mail address.")
            if request["action"] == "draft":
                mail.Save()
                return {"ok": True, "saved": True}
            if not mail.Recipients.Count:
                raise MailError("Add at least one recipient.")
            if request["later"] is not None:  # it goes to the Outbox now and leaves by itself then, while Outlook is open
                self._set_time(mail, "DeferredDeliveryTime", request["later"])
                mail.Send()
                return {"ok": True, "sent": True, "later": True}
            mail.Send()
            return {"ok": True, "sent": True}
        except Exception:
            if created:
                try:
                    mail.Close(OL_DISCARD)
                except Exception:
                    pass
            raise
        finally:
            if folder is not None:
                shutil.rmtree(folder, ignore_errors=True)

    def _address(self, mail, request: dict) -> None:
        """Make the recipients of an answer exactly the ones the phone showed.

        Outlook has addressed the answer already. Whoever is on both lists stays as Outlook put him
        (so a colleague it knows by an internal name is not looked up again), the others are taken
        out, and who is new is added.
        """
        wanted = [(kind, entry) for kind, entries in ((1, request["to"]), (2, request["cc"]), (3, request["bcc"])) for entry in entries]
        recipients = mail.Recipients
        for index in range(recipients.Count, 0, -1):  # from the end: taking one out renumbers those after it
            recipient = recipients.Item(index)
            names = {str(getattr(recipient, "Name", "") or "").lower(), str(getattr(recipient, "Address", "") or "").lower()}
            try:
                names.add(str(recipient.PropertyAccessor.GetProperty(P_SMTP) or "").lower())
            except Exception:
                pass
            kind = int(getattr(recipient, "Type", 1) or 1)
            same = next((entry for entry in wanted if entry[0] == kind and entry[1].lower() in names), None)
            if same is None:
                recipients.Remove(index)
            else:
                wanted.remove(same)
        for kind, entry in wanted:
            recipients.Add(entry).Type = kind

    @staticmethod
    def _take_out(mail, index: int, name: str) -> None:
        """Remove one attachment of the original from a forward or a draft, and only the one the phone named."""
        attachments = mail.Attachments
        if not 1 <= index <= attachments.Count or (attachments.Item(index).FileName or "") != name:
            raise MailError(f"\u201c{name}\u201d could not be taken out of this message, so nothing was done. Open it again and try once more.")
        attachments.Remove(index)

    # -------------------------------------------------- the calendar
    @staticmethod
    def _event_row(appointment) -> dict:
        """What the phone lists of one appointment or meeting. Times are UTC; `day` is the date on this PC."""
        def whole(value) -> dt.datetime:  # pywin32 hands local times over marked as UTC: only the numbers count
            return dt.datetime(value.year, value.month, value.day, value.hour, value.minute, value.second)

        begins, ends = whole(appointment.Start), whole(appointment.End)
        all_day = bool(appointment.AllDayEvent)
        last = ends - dt.timedelta(minutes=1) if all_day and ends > begins else ends  # an all-day entry ends at midnight
        status = int(getattr(appointment, "MeetingStatus", 0) or 0)
        return {
            "subject": getattr(appointment, "Subject", "") or "(no subject)",
            "start": _utc_iso(appointment.StartUTC),
            "end": _utc_iso(appointment.EndUTC),
            "day": begins.date().isoformat(),
            "lastDay": last.date().isoformat(),
            "allDay": all_day,
            "location": str(getattr(appointment, "Location", "") or ""),
            "organizer": str(getattr(appointment, "Organizer", "") or ""),
            "meeting": status != 0,
            "canceled": status in (5, 7),
            "invited": status == 3,  # somebody else's meeting: it can be accepted or declined
            "mine": status in (0, 1),  # your own entry: it can be changed or taken out
            "response": RESPONSES.get(int(getattr(appointment, "ResponseStatus", 0) or 0), ""),
            "recurring": bool(getattr(appointment, "IsRecurring", False)),
            "busy": BUSY.get(int(getattr(appointment, "BusyStatus", 2) or 0), "busy"),
            "private": int(getattr(appointment, "Sensitivity", 0) or 0) == 2,  # olPrivate: others see only that the time is taken
            # minutes before it starts that Outlook reminds you, or None when it does not
            "reminder": int(getattr(appointment, "ReminderMinutesBeforeStart", 0) or 0) if getattr(appointment, "ReminderSet", False) else None,
        }

    def calendar(self, offset: int = 0, days: int = CALENDAR_DAYS, which: str = "", fresh: bool = False) -> dict:
        """The appointments and meetings of `days` days, starting `offset` days from today.

        Asked for when the phone opens the calendar and kept for ten minutes (`fresh`: the phone's
        Refresh, which asks anew). Outlook is asked once, however long it takes and however often
        the phone asks meanwhile, and the answer is kept even when nobody waited for it. When
        Outlook is busy the page from before is returned marked `saved`. `which`: another calendar
        than your own, as calendars() names it.
        """
        offset = max(0, min(int(offset), 730))
        days = max(1, min(int(days), 62))
        if which and not FOLDER_ID.fullmatch(which):
            raise MailError("Unknown calendar.")
        self._phone_asked()
        place = (offset, days, which) if which else (offset, days)
        kept = self._calendars.get(place)
        if kept and time.monotonic() - kept[0] < (CALENDAR_AGAIN_SECONDS if fresh else CALENDAR_FRESH_SECONDS):
            return kept[1]
        try:
            return self._shared(("calendar", *place), self._calendar, *place, wait=QUICK_SECONDS if kept else READ_SECONDS)
        except OutlookBusy:
            if not kept:
                raise
            return {**kept[1], "saved": True}

    def _asked(self, key: tuple, fn, *args, fresh: float):
        """A slow answer that is kept: given from memory while it is new enough, asked for once otherwise,
        and when Outlook is busy the one from before is given."""
        kept = self._answers.get(key)
        if kept and time.monotonic() - kept[0] < fresh:
            return kept[1]
        try:
            return self._shared(key, fn, *args, wait=QUICK_SECONDS if kept else READ_SECONDS)
        except OutlookBusy:
            if not kept:
                raise
            return kept[1]

    def calendars(self) -> dict:
        """The calendars Outlook shows besides your own: more of yours, and those other people share with you."""
        self._phone_asked()
        return {"calendars": self._asked(("calendars",), self._calendars_now, fresh=FOLDERS_FRESH_SECONDS)}

    def invite(self, mail_id: str) -> dict:
        """What an invitation is for: when, where, what you answered. Asked when the mail is opened on the
        phone, not while reading ahead: finding the meeting can take Outlook a while."""
        self._phone_asked()
        return {"invite": self._asked(("invite", mail_id), self._invite_of, mail_id, fresh=CALENDAR_FRESH_SECONDS)}

    def _invite_of(self, mail_id: str) -> dict | None:
        item = self._item(mail_id)
        if not str(getattr(item, "MessageClass", "") or "").startswith("IPM.Schedule.Meeting.Request"):
            return None
        try:  # False: looking at it does not put it in the calendar
            return self._event_row(item.GetAssociatedAppointment(False))
        except Exception as error:
            if getattr(error, "hresult", None) in RPC_BUSY:
                raise
            return None

    def _calendars_now(self) -> list[dict]:
        default = self._ns.GetDefaultFolder(OL_CALENDAR)
        found: list[dict] = []
        seen = {default.EntryID}

        def take(folder, name: str, group: str) -> None:
            entry_id = folder.EntryID
            if entry_id in seen or folder.DefaultItemType != 1 or len(found) >= 60:
                return
            seen.add(entry_id)
            found.append({"id": f"f.{self._store_key(folder.StoreID)}.{entry_id}", "name": name or str(folder.Name), "group": group})

        try:  # what Outlook lists at the side of its own calendar, the shared ones among them
            explorer = self._app.ActiveExplorer() or self._app.Explorers.Item(1)
            module = explorer.NavigationPane.Modules.GetNavigationModule(1)  # olModuleCalendar
            for group in module.NavigationGroups:
                for shown in group.NavigationFolders:
                    self._give_way()  # reaching somebody else's mailbox can take long: a send does not wait for all of them
                    try:
                        take(shown.Folder, str(shown.DisplayName or ""), str(group.Name or ""))
                    except Exception:
                        continue  # one that cannot be reached right now: the others are still listed
        except Exception as error:
            if isinstance(error, OutlookBusy) or getattr(error, "hresult", None) in RPC_BUSY:
                raise
        try:  # calendars kept inside your own
            for child in default.Folders:
                try:
                    take(child, "", "")
                except Exception:
                    continue
        except Exception:
            pass
        return found

    def _calendar(self, offset: int, days: int, which: str = "") -> dict:
        """The entries of these days. Outlook is asked with the dates written one way, and when what
        it gives back does not belong to those days (it read the dates another way), the next way is
        tried. The way that worked is remembered for as long as Titip runs."""
        first = today() + dt.timedelta(days=offset)
        after = first + dt.timedelta(days=days)
        folder = self._folder(which) if which else self._ns.GetDefaultFolder(OL_CALENDAR)
        if which and getattr(folder, "DefaultItemType", 1) != 1:
            raise MailError("That is not a calendar.")
        store_key = self._store_key(folder.StoreID)
        ways = list(range(len(DATE_WAYS)))
        if self._date_way is not None:
            ways.remove(self._date_way)
            ways.insert(0, self._date_way)
        asked, empty, doubted = set(), None, None
        for way in ways:
            write = DATE_WAYS[way]
            # a day more on each side: every entry is checked here against the real days
            text = f"[Start] <= '{write(after + dt.timedelta(days=1))}' AND [End] >= '{write(first - dt.timedelta(days=1))}'"
            if text in asked:
                continue  # this PC writes dates this way anyway
            asked.add(text)
            try:
                events, more, looked, astray = self._calendar_read(folder, store_key, first, after, text)
            except Exception as error:
                if isinstance(error, MailError) or getattr(error, "hresult", None) in RPC_BUSY:
                    raise
                continue  # Outlook did not take dates written this way
            if which:  # somebody else's calendar, or a second one: to read, not to answer or change from here
                for event in events:
                    event["mine"] = event["invited"] = False
                    event["other"] = True
            page = {"from": first.date().isoformat(), "days": days, "offset": offset, "events": events, "more": more}
            if looked and astray * 2 > looked:
                # most of what came back is from other days: the dates were probably read another way
                if events and (doubted is None or len(events) > len(doubted["events"])):
                    doubted = page
                continue
            if events or way == self._date_way:
                self._date_way = way
                return page
            empty = empty or page  # nothing these days, or dates misread into days with nothing: ask the next way too
        if doubted is not None:
            return doubted  # no way of asking was clean; what did belong to these days is still shown
        if empty is None:
            raise MailError("Outlook did not understand the days Titip asked its calendar for. The calendar is still in Outlook itself.")
        return empty

    def _calendar_read(self, folder, store_key: str, first: dt.datetime, after: dt.datetime, text: str):
        """(entries of the days, whether there were more than were looked at, how many were looked at,
        how many of those were far from the days asked for)."""
        items = folder.Items
        items.Sort("[Start]")
        items.IncludeRecurrences = True  # each time a repeating meeting takes place is an entry of its own
        found = items.Restrict(text)
        events, more, looked, astray = [], False, 0, 0
        near = dt.timedelta(days=3)
        for item in found:  # never its Count: with repeating entries it has no end
            self._give_way()
            if looked >= CALENDAR_LOOK:
                more = True
                break
            looked += 1
            try:
                row = self._event_row(item)
                begins = dt.datetime(item.Start.year, item.Start.month, item.Start.day, item.Start.hour, item.Start.minute)
                ends = dt.datetime(item.End.year, item.End.month, item.End.day, item.End.hour, item.End.minute)
                entry_id = item.EntryID
            except Exception:  # not an appointment, or one Outlook cannot read
                continue
            if begins > after + near or ends < first - near:
                astray += 1
            if begins >= after:
                if astray:
                    continue  # the order cannot be trusted either: look at the rest
                break  # sorted by start: nothing later belongs to these days
            if ends <= first and not (ends == begins and begins >= first):
                continue
            row["id"] = f"{store_key}.{entry_id}"
            events.append(row)
        return events, more, looked, astray

    def event(self, event_id: str) -> dict:
        """One appointment in full: its text and who is invited. Of a repeating one, the series."""
        self._phone_asked()
        return self._do(self._event, event_id)

    def _event(self, event_id: str) -> dict:
        item = self._item(event_id)
        if not str(getattr(item, "MessageClass", "") or "").startswith("IPM.Appointment"):
            raise MailError("That is not a calendar entry.")
        invited, optional = [], []  # who is invited and who need not come, for the phone to list and change: not yourself, not a room
        try:
            mine = self._my_addresses()
            organizer = str(getattr(item, "Organizer", "") or "").lower()
            for name, address, kind in self._recipients(item.Recipients, RECIPIENTS_READ)[0]:
                if kind in (1, 2) and address.lower() not in mine and name.lower() != organizer:
                    (invited if kind == 1 else optional).append(as_person(name, address))
        except Exception:
            pass
        return {
            **self._event_row(item),
            "id": event_id,
            "required": str(getattr(item, "RequiredAttendees", "") or ""),
            "optional": str(getattr(item, "OptionalAttendees", "") or ""),
            "invitedPeople": invited,
            "optionalPeople": optional,
            "body": (getattr(item, "Body", "") or "")[:MAX_BODY],
        }

    # -------------------------------------------------- make, change or take out a calendar entry
    def plan(self, payload: dict) -> dict:
        request = clean_plan_request(payload)
        try:
            return self._do(self._plan, request, timeout=SEND_SECONDS, first=True)
        except OutlookBusy as error:
            if error.started:
                raise MailError("Outlook is taking a long time with this. Look in its calendar before you try it again, so it is not there twice.")
            raise MailError("Outlook on your PC is busy, so the calendar was not changed." + error.why() + " Try again in a moment.")
        finally:
            with self._lock:
                self._calendars.clear()

    @staticmethod
    def _set_time(item, name: str, wanted: dt.datetime, shift: dt.timedelta = dt.timedelta(0)) -> dt.timedelta:
        """Set Start or End and read it back. Outlook wants the time of this PC's clock; should what
        comes back be shifted (a time zone applied on the way in), it is set again by that much less.

        What comes back is by how much Outlook shifted it. End is set with the shift Start showed:
        set plainly it would land before the Start just put right, and an entry that ends before it
        starts Outlook refuses outright ("The object does not support this method").
        """
        def read() -> dt.datetime:
            value = getattr(item, name)
            return dt.datetime(value.year, value.month, value.day, value.hour, value.minute)

        setattr(item, name, wanted - shift)
        got = read()
        if got != wanted:
            shift += got - wanted
            setattr(item, name, wanted - shift)
            if read() != wanted:
                raise MailError("Outlook did not take the time of this entry. Make it in Outlook itself.")
        return shift

    def _own_entry(self, event_id: str, scope: str = "", occurrence: dt.datetime | None = None):
        """An entry of your own, to change or take out. Of one that repeats: the series, or the one time named."""
        item = self._item(event_id)
        if not str(getattr(item, "MessageClass", "") or "").startswith("IPM.Appointment"):
            raise MailError("That is not a calendar entry.")
        if int(getattr(item, "MeetingStatus", 0) or 0) not in (0, 1):
            raise MailError("This is somebody else's meeting. It can be answered, not changed.")
        try:
            elsewhere = not self._ns.CompareEntryIDs(item.Parent.EntryID, self._ns.GetDefaultFolder(OL_CALENDAR).EntryID)
        except Exception:
            elsewhere = False  # Outlook does not say where it is kept: then it is taken for your own
        if elsewhere:
            raise MailError("That entry is in another calendar than your own. It is changed there, in Outlook.")
        if bool(getattr(item, "IsRecurring", False)):
            if scope == "one" and occurrence is not None:
                return self._one_time(item, occurrence), True
            if scope != "series":
                raise MailError("This entry repeats. Say whether this one time or the whole series is meant.")
        return item, False

    @staticmethod
    def _one_time(series, when: dt.datetime):
        """The one time a repeating entry takes place that starts at `when` (this PC's clock).

        Outlook is asked for it by its start. Should Outlook take that time as written on another
        clock, it is asked again shifted by this PC's distance from UTC, either way; what comes back
        must start at `when`, or it is not the one.
        """
        pattern = series.GetRecurrencePattern()
        away = dt.datetime.now().astimezone().utcoffset() or dt.timedelta(0)
        for asked in (when, when - away, when + away):
            try:
                found = pattern.GetOccurrence(asked)
                starts = found.Start
                if dt.datetime(starts.year, starts.month, starts.day, starts.hour, starts.minute) == when:
                    return found
            except Exception as error:
                if getattr(error, "hresult", None) in RPC_BUSY:
                    raise
        raise MailError("Outlook cannot find that one time of the series. Refresh the calendar and try again.")

    def _invite(self, item, wanted: list[str], as_kind: int = 1) -> bool:
        """Make the people invited exactly `wanted`: the required ones (`as_kind` 1) or the optional ones (2).
        Yourself, the others and a room stay. True when somebody was added or taken out."""
        mine = self._my_addresses()
        organizer = str(getattr(item, "Organizer", "") or "").lower()
        left = list(wanted)
        changed = False
        recipients = item.Recipients
        for index in range(recipients.Count, 0, -1):
            recipient = recipients.Item(index)
            kind = getattr(recipient, "Type", 1)
            names = {str(getattr(recipient, "Name", "") or "").lower(), str(getattr(recipient, "Address", "") or "").lower()}
            try:
                names.add(str(recipient.PropertyAccessor.GetProperty(P_SMTP) or "").lower())
            except Exception:
                pass
            if kind != as_kind or names & mine or (organizer and organizer in names):
                continue  # the organizer, a guest of the other kind, a room: not what this list holds
            same = next((entry for entry in left if entry.lower() in names), None)
            if same is None:
                recipients.Remove(index)
                changed = True
            else:
                left.remove(same)
        for entry in left:
            recipients.Add(entry).Type = as_kind
            changed = True
        return changed

    def _plan(self, request: dict) -> dict:
        if request["what"] == "remove":
            item, _ = self._own_entry(request["id"], request["scope"], request["occurrence"])
            if int(getattr(item, "MeetingStatus", 0) or 0) == 1:  # your meeting, with people invited: tell them
                item.MeetingStatus = 5  # olMeetingCanceled
                item.Send()
                try:
                    item.Delete()
                except Exception:
                    pass  # Outlook took it out by itself when the cancellation left
                return {"ok": True, "removed": True, "told": True}
            item.Delete()
            return {"ok": True, "removed": True, "told": False}
        created = not request["id"]
        one_time = False
        if created:
            item = self._app.CreateItem(1)
        else:
            item, one_time = self._own_entry(request["id"], request["scope"], request["occurrence"])
        series = not created and not one_time and bool(getattr(item, "IsRecurring", False))
        try:
            item.Subject = request["subject"]
            item.Location = request["location"]
            if not series:  # when a series takes place is changed in Outlook: here only what it is called, where, and its text
                item.AllDayEvent = request["allDay"]
                shift = self._set_time(item, "Start", request["start"])
                self._set_time(item, "End", request["end"], shift)
            if created or " ".join(request["body"].split()) != " ".join((getattr(item, "Body", "") or "").split()):
                item.Body = request["body"]  # untouched, the text keeps the formatting it has in Outlook
            if request["reminder"] is not None:
                item.ReminderSet = request["reminder"] >= 0
                if request["reminder"] >= 0:
                    item.ReminderMinutesBeforeStart = request["reminder"]
            if request["busy"] is not None:
                item.BusyStatus = next(number for number, name in BUSY.items() if name == request["busy"])
            if request["private"] is not None and not one_time:  # Outlook keeps this for the whole series
                item.Sensitivity = 2 if request["private"] else 0
            if created and request["repeat"]:
                pattern = item.GetRecurrencePattern()  # takes the day and the hours from what was just set
                pattern.RecurrenceType = REPEATS[request["repeat"]["every"]]
                if request["repeat"]["every"] == "weekday":
                    pattern.DayOfWeekMask = WEEKDAYS
                if request["repeat"].get("interval", 1) > 1:
                    pattern.Interval = request["repeat"]["interval"]
                if request["repeat"]["times"]:
                    pattern.Occurrences = request["repeat"]["times"]
                else:
                    pattern.NoEndDate = True
            if created and (request["with"] or request["optional"]):
                item.MeetingStatus = 1  # olMeeting: an entry with people is a meeting, and they are invited
                for kind, addresses in ((1, request["with"]), (2, request["optional"])):
                    for address in addresses:
                        item.Recipients.Add(address).Type = kind
            elif not created and not one_time and (request["withExact"] or request["optionalExact"]):
                changed = request["withExact"] and self._invite(item, request["with"])
                if request["optionalExact"]:
                    changed = self._invite(item, request["optional"], 2) or changed
                if changed and (request["with"] or request["optional"]):
                    item.MeetingStatus = 1
            if item.Recipients.Count and not item.Recipients.ResolveAll():
                unknown = [r.Name for r in item.Recipients if not r.Resolved]
                raise MailError("Outlook does not know: " + ", ".join(unknown) + ". Use a full e-mail address.")
            if int(getattr(item, "MeetingStatus", 0) or 0) == 1:
                item.Send()  # the invitation, or the news that it changed
                return {"ok": True, "saved": True, "told": True}
            item.Save()
            return {"ok": True, "saved": True, "told": False}
        except Exception:
            if created:
                try:
                    item.Close(OL_DISCARD)
                except Exception:
                    pass
            raise

    # -------------------------------------------------- your own folders, and the categories
    def _store_id(self, store_key: str) -> str | None:
        """The data file a short key stands for; every data file open in Outlook is looked at once."""
        if store_key not in self._stores:
            for store in self._ns.Stores:
                try:
                    self._store_key(store.StoreID)
                except Exception:
                    continue
        return self._stores.get(store_key)

    def _folder(self, folder_id: str):
        if not FOLDER_ID.fullmatch(folder_id):
            raise MailError("Unknown folder.")
        _, store_key, entry_id = folder_id.split(".")
        store_id = self._store_id(store_key)
        try:
            if not store_id:
                raise LookupError(store_key)
            return self._ns.GetFolderFromID(entry_id, store_id)
        except Exception as error:
            if getattr(error, "hresult", None) in RPC_BUSY:
                raise
            raise MailError("That folder is not there any more. Open the list of folders again.")

    def folders(self) -> dict:
        """Your own mail folders (every data file, folders inside folders) and Outlook's categories.

        Walking them takes Outlook a while, so the answer is kept for an hour; when Outlook is
        busy the one from before is given. The numbers of unread mail in it are that old too.
        """
        self._phone_asked()
        kept = self._folder_list
        if kept and time.monotonic() - kept[0] < FOLDERS_FRESH_SECONDS:
            return kept[1]
        try:
            return self._shared(("folders",), self._folders_now, wait=QUICK_SECONDS if kept else READ_SECONDS)
        except OutlookBusy:
            if not kept:
                raise
            return kept[1]

    def _folders_now(self) -> dict:
        usual = set()  # the folders the phone shows under their own names already
        for number in OL_FOLDER.values():
            try:
                usual.add(self._ns.GetDefaultFolder(number).EntryID)
            except Exception:
                pass
        default_store = self._ns.GetDefaultFolder(OL_INBOX).StoreID
        found: list[dict] = []

        def walk(folder, store_key: str, store_name: str, path: str, depth: int) -> None:
            for child in folder.Folders:
                self._give_way()
                if len(found) >= FOLDERS_SHOWN:
                    return
                try:
                    name, entry_id, kind = str(child.Name), child.EntryID, child.DefaultItemType
                except Exception:
                    continue
                if kind != 0:  # calendar, contacts, tasks, notes: not mail
                    continue
                if entry_id not in usual:
                    unread = 0
                    if not store_name:  # of your own mailbox only: one more question for each folder of an old data file adds up
                        try:
                            unread = int(child.UnReadItemCount)
                        except Exception:
                            pass
                    found.append({"id": f"f.{store_key}.{entry_id}", "name": name, "in": path, "file": store_name, "unread": unread})
                if depth < FOLDERS_DEEP:
                    try:
                        walk(child, store_key, store_name, f"{path} / {name}" if path else name, depth + 1)
                    except OutlookBusy:
                        raise  # a send is waiting: the walk stops and is asked for again
                    except Exception:
                        pass

        stores = sorted(self._ns.Stores, key=lambda store: store.StoreID != default_store)  # your mailbox first
        for store in stores:
            try:
                walk(store.GetRootFolder(), self._store_key(store.StoreID), "" if store.StoreID == default_store else str(store.DisplayName), "", 1)
            except OutlookBusy:
                raise
            except Exception:  # a data file that is offline or locked: the others are still listed
                continue
        categories = []
        try:
            for category in self._ns.Categories:
                if len(categories) >= CATEGORIES_SHOWN:
                    break
                categories.append(str(category.Name))
        except Exception:
            pass
        return {"folders": found, "categories": categories, "cut": len(found) >= FOLDERS_SHOWN}

    def rules(self) -> dict:
        """Outlook's rules for your mailbox, in the order they run, each on or off."""
        self._phone_asked()
        return {"rules": self._asked(("rules",), self._rules_now, fresh=LIST_FRESH_SECONDS)}

    def _rules_now(self) -> list[dict]:
        found = []
        for rule in self._ns.DefaultStore.GetRules():
            if len(found) >= RULES_SHOWN:
                break
            try:
                found.append({"name": str(rule.Name), "on": bool(rule.Enabled), "order": int(rule.ExecutionOrder), "sent": int(rule.RuleType) == 1})
            except Exception:
                continue
        return found

    def arrange(self, payload: dict) -> dict:
        request = clean_arrange_request(payload)
        try:
            return self._do(self._arrange, request, timeout=SEND_SECONDS, first=True)
        except OutlookBusy as error:
            if error.started:
                raise MailError("Outlook is taking a long time with this. Look in Outlook before you try it again.")
            raise MailError("Outlook on your PC is busy, so nothing was changed." + error.why() + " Try again in a moment.")
        finally:
            self._folder_list = None  # the folders or the categories have changed (or may have)

    def _is_usual(self, folder) -> bool:
        """One of the folders Outlook itself made (Inbox, Sent, Drafts, ...): never renamed or taken out from the phone."""
        for number in (*OL_FOLDER.values(), OL_CALENDAR, 10, 11, 12, 13):  # and contacts, journal, notes, tasks
            try:
                if self._ns.CompareEntryIDs(folder.EntryID, self._ns.GetDefaultFolder(number).EntryID):
                    return True
            except Exception:
                continue
        return False

    def _arrange(self, request: dict) -> dict:
        what = request["what"]
        if what == "folder":
            if request["in"] == "inbox":
                parent = self._ns.GetDefaultFolder(OL_INBOX)
            elif request["in"]:
                parent = self._folder(request["in"])
            else:
                parent = self._ns.GetDefaultFolder(OL_INBOX).Parent  # next to the Inbox
            for child in parent.Folders:
                if str(child.Name).lower() == request["name"].lower():
                    raise MailError(f"There is a folder \u201c{child.Name}\u201d there already.")
            made = parent.Folders.Add(request["name"])
            return {"ok": True, "id": f"f.{self._store_key(made.StoreID)}.{made.EntryID}", "name": str(made.Name)}
        if what in ("rename", "unfolder"):
            folder = self._folder(request["id"])
            if self._is_usual(folder):
                raise MailError("That is one of Outlook's own folders. It stays as it is.")
            if what == "rename":
                folder.Name = request["name"]
                return {"ok": True, "name": request["name"]}
            if folder.Items.Count or folder.Folders.Count:
                raise MailError("That folder is not empty. Only an empty folder is taken out from the phone.")
            folder.Delete()
            return {"ok": True, "removed": True}
        if what == "category":
            for category in self._ns.Categories:
                if str(category.Name).lower() == request["name"].lower():
                    raise MailError(f"There is a category \u201c{category.Name}\u201d already.")
            self._ns.Categories.Add(request["name"])
            return {"ok": True, "name": request["name"]}
        if what == "empty":
            folder = self._ns.GetDefaultFolder(OL_FOLDER[request["folder"]])
            gone = 0
            if request["folder"] == "deleted":  # folders that were deleted lie in here too
                subfolders = folder.Folders
                for index in range(subfolders.Count, 0, -1):
                    subfolders.Item(index).Delete()
                    gone += 1
            items = folder.Items
            for index in range(items.Count, 0, -1):  # from the end: what is left keeps its place
                if gone >= EMPTY_MOST:
                    break
                self._give_way()
                items.Item(index).Delete()  # out of Deleted Items: for good. Out of Junk: to Deleted Items.
                gone += 1
            return {"ok": True, "emptied": gone, "left": int(folder.Items.Count)}
        if what == "awayText":
            note = self._away_note()
            note.Body = request["text"]
            note.Save()
            if " ".join(str(self._away_note().Body or "").split()) != " ".join(request["text"].split()):
                raise MailError("Outlook did not keep the text. Set it in Outlook itself.")
            if request.get("outside") is not None:
                other = self._away_note(True)
                if " ".join(str(other.Body or "").split()) != " ".join(request["outside"].split()):
                    other.Body = request["outside"]
                    other.Save()
                    if " ".join(str(self._away_note(True).Body or "").split()) != " ".join(request["outside"].split()):
                        raise MailError("Outlook kept the text for colleagues, but not the one for people outside. Set that one in Outlook itself.")
            return {"ok": True, "text": request["text"], **({"outside": request["outside"]} if request.get("outside") is not None else {})}
        if what == "away":
            # The switch only. The text of the reply, and whom it goes to, is what is set in Outlook.
            try:
                self._ns.DefaultStore.PropertyAccessor.SetProperty(P_AWAY, request["on"])
            except Exception as error:
                if getattr(error, "hresult", None) in RPC_BUSY:
                    raise
                raise MailError("Outlook did not let Titip switch the automatic replies. Switch them in Outlook itself.")
            if self._away() is not request["on"]:
                raise MailError("Outlook took the switch but the automatic replies did not change. Switch them in Outlook itself.")
            self._state_cache = None
            return {"ok": True, "away": request["on"]}
        rules = self._ns.DefaultStore.GetRules()
        if what == "rule":
            for rule in rules:
                if str(rule.Name) == request["name"]:
                    rule.Enabled = request["on"]
                    rules.Save()
                    return {"ok": True, "on": request["on"]}
            raise MailError("That rule is not there any more.")
        # a rule for one sender: mail from whoever wrote this message goes to that folder from now on
        item = self._item(request["id"])
        address = self._sender_address(item, item.PropertyAccessor)
        who = str(getattr(item, "SenderName", "") or "") or address
        if not address and not who:
            raise MailError("Outlook does not say who wrote that message.")
        target = self._ns.GetDefaultFolder(OL_JUNK) if request["to"] == "junk" else self._folder(request["to"])
        name = f"{who} \u2192 {target.Name}"[:200]
        rule = rules.Create(name, 0)  # olRuleReceive
        sender = rule.Conditions.From
        sender.Recipients.Add(address or who)
        if not sender.Recipients.ResolveAll():
            raise MailError("Outlook does not know that sender well enough to make a rule for them.")
        sender.Enabled = True
        move = rule.Actions.MoveToFolder
        move.Folder = target
        move.Enabled = True
        rules.Save()
        return {"ok": True, "name": name}

    # -------------------------------------------------- tasks, notes and contacts
    def things(self, kind: str, query: str = "", done: bool = False) -> dict:
        """Outlook's tasks (open ones, or with `done` the finished), notes or contacts, as a list. `query` narrows it."""
        if kind not in THINGS:
            raise MailError("Unknown page.")
        self._phone_asked()
        query = " ".join(str(query or "").split())[:80]
        return self._asked(("things", kind, query.lower(), bool(done)), self._things, kind, query, bool(done), fresh=LIST_FRESH_SECONDS)

    @staticmethod
    def _day(value) -> str:
        """A date of Outlook's as "2026-11-23", or "" for its "no date"."""
        try:
            return "" if value.year < 1980 or value.year > 4000 else f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
        except AttributeError:
            return ""

    @staticmethod
    def _moment(value) -> str:
        """A time of this PC's clock as Outlook hands it over (marked UTC though it is not), as real UTC."""
        try:
            local = dt.datetime(value.year, value.month, value.day, value.hour, value.minute, value.second)
            return local.astimezone().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if 1980 <= value.year <= 4000 else ""
        except (AttributeError, ValueError, OSError):
            return ""

    def _thing_row(self, kind: str, item, store_key: str) -> dict:
        row = {"id": f"{store_key}.{item.EntryID}", "kind": kind}
        if kind == "tasks":
            row.update(title=str(getattr(item, "Subject", "") or "(no subject)"), due=self._day(getattr(item, "DueDate", None)),
                       done=bool(getattr(item, "Complete", False)), important=getattr(item, "Importance", 1) == 2)
        elif kind == "notes":
            text = str(getattr(item, "Body", "") or "")
            first, _, rest = text.strip().partition("\n")
            row.update(title=first.strip()[:120] or "(empty note)", preview=" ".join(rest.split())[:180],
                       time=self._moment(getattr(item, "LastModificationTime", None)))
        else:
            email = str(getattr(item, "Email1Address", "") or "")
            name = str(getattr(item, "FullName", "") or "") or str(getattr(item, "FileAs", "") or "") or str(getattr(item, "Subject", "") or "")
            row.update(title=name or str(getattr(item, "CompanyName", "") or "") or email or "(no name)",
                       company=str(getattr(item, "CompanyName", "") or ""), email=email if "@" in email else "",
                       phone=str(getattr(item, "MobileTelephoneNumber", "") or getattr(item, "BusinessTelephoneNumber", "") or ""))
        return row

    def _things(self, kind: str, query: str, done: bool) -> dict:
        folder = self._ns.GetDefaultFolder(THINGS[kind][0])
        store_key = self._store_key(folder.StoreID)
        items = folder.Items
        needle = query.lower()
        searched = False
        if kind == "tasks":  # narrowed first, sorted after: what Restrict hands back is not promised to keep an order
            items = items.Restrict("[Complete] = True" if done else "[Complete] = False")
            items.Sort("[DueDate]")
        elif kind == "notes":
            items.Sort("[LastModificationTime]", True)
        else:
            if needle:
                text = re.sub(r"[%_\[\]'\"]", " ", needle).strip()
                try:  # Outlook searches name, company and address itself; where it cannot, they are looked through here
                    items = items.Restrict("@SQL=" + " OR ".join(
                        f'"urn:schemas:contacts:{field}" like \'%{text}%\'' for field in ("cn", "o", "email1")))
                    searched = True
                except Exception as error:
                    if getattr(error, "hresult", None) in RPC_BUSY:
                        raise
            items.Sort("[FullName]")
        found, looked, more = [], 0, False
        for item in items:
            self._give_way()
            looked += 1
            if len(found) >= THINGS_SHOWN or looked > THINGS_LOOK:
                more = True
                break
            try:
                if not str(getattr(item, "MessageClass", "") or "").startswith(THINGS[kind][2]):
                    continue  # a list of contacts, say: not one person
                row = self._thing_row(kind, item, store_key)
            except Exception as error:
                if isinstance(error, OutlookBusy) or getattr(error, "hresult", None) in RPC_BUSY:
                    raise
                continue
            if needle and not searched and needle not in " ".join(str(value) for value in row.values()).lower():
                continue
            found.append(row)
        return {"kind": kind, "things": found, "more": more}

    def thing(self, kind: str, thing_id: str) -> dict:
        """One task, note or contact in full."""
        if kind not in THINGS:
            raise MailError("Unknown page.")
        self._phone_asked()
        return self._do(self._thing, kind, thing_id)

    def _of_kind(self, kind: str, thing_id: str):
        try:
            item = self._item(thing_id)
        except MailError as error:
            if "moved or deleted" in str(error):  # said of a mail; this is a task, a note or a contact
                raise MailError("It is not there any more. Refresh the list.")
            raise
        if not str(getattr(item, "MessageClass", "") or "").startswith(THINGS[kind][2]):
            raise MailError("That is not what it was when the list was read. Refresh the list.")
        return item

    def _thing(self, kind: str, thing_id: str) -> dict:
        item = self._of_kind(kind, thing_id)
        row = {**self._thing_row(kind, item, thing_id.partition(".")[0]), "id": thing_id, "body": (getattr(item, "Body", "") or "")[:MAX_BODY]}
        if kind == "contacts":
            emails = [str(getattr(item, f"Email{number}Address", "") or "") for number in (1, 2, 3)]
            phones = [(label, str(getattr(item, name, "") or "")) for label, name in (
                ("Mobile", "MobileTelephoneNumber"), ("Work", "BusinessTelephoneNumber"), ("Home", "HomeTelephoneNumber"))]
            row.update(emails=[address for address in emails if "@" in address], job=str(getattr(item, "JobTitle", "") or ""),
                       phones=[{"label": label, "number": number} for label, number in phones if number],
                       mobile=phones[0][1], work=phones[1][1], address=" ".join(str(getattr(item, "BusinessAddress", "") or "").split()))
        return row

    def keep(self, payload: dict) -> dict:
        request = clean_keep_request(payload)
        try:
            return self._do(self._keep_thing, request, timeout=SEND_SECONDS, first=True)
        except OutlookBusy as error:
            if error.started:
                raise MailError("Outlook is taking a long time with this. Look in Outlook before you try it again, so it is not there twice.")
            raise MailError("Outlook on your PC is busy, so nothing was changed." + error.why() + " Try again in a moment.")
        finally:
            with self._lock:
                for key in [key for key in self._answers if key[0] == "things" and key[1] == request["kind"]]:
                    del self._answers[key]

    @staticmethod
    def _set_day(item, name: str, day) -> None:
        """Set a date-only field (a task's due date) and read it back. Outlook may take the time as written
        on another clock: noon stays on its day either way, and should it not, the day next to it is tried."""
        if day is None:
            setattr(item, name, NO_DAY)
            return
        for shift in (0, 1, -1):
            setattr(item, name, dt.datetime(day.year, day.month, day.day, 12) + dt.timedelta(days=shift))
            got = getattr(item, name)
            if (got.year, got.month, got.day) == (day.year, day.month, day.day):
                return
        raise MailError("Outlook did not take that day. Set it in Outlook itself.")

    def _keep_thing(self, request: dict) -> dict:
        kind, what = request["kind"], request["what"]
        if what == "remove":
            self._of_kind(kind, request["id"]).Delete()  # to Deleted Items
            return {"ok": True, "removed": True}
        if what in ("done", "undone"):
            item = self._of_kind(kind, request["id"])
            if what == "done":
                item.MarkComplete()
            else:
                item.Complete = False
                item.Save()
            return {"ok": True, "done": what == "done"}
        created = not request["id"]
        item = self._app.CreateItem(THINGS[kind][1]) if created else self._of_kind(kind, request["id"])
        try:
            if kind == "notes":
                item.Body = request["body"]
            else:
                if created or " ".join(request["body"].split()) != " ".join((getattr(item, "Body", "") or "").split()):
                    item.Body = request["body"]  # untouched, it keeps the formatting it has in Outlook
                if kind == "tasks":
                    item.Subject = request["title"]
                    item.Importance = 2 if request["important"] else 1
                    self._set_day(item, "DueDate", request["due"])
                else:
                    item.FullName = request["title"]
                    item.CompanyName = request["company"]
                    item.JobTitle = request["job"]
                    if created or request["email"] != (str(getattr(item, "Email1Address", "") or "") if "@" in str(getattr(item, "Email1Address", "") or "") else ""):
                        item.Email1Address = request["email"]  # one Outlook knows by an internal name stays untouched
                    item.MobileTelephoneNumber = request["mobile"]
                    item.BusinessTelephoneNumber = request["phone"]
            item.Save()
            return {"ok": True, "saved": True}
        except Exception:
            if created:
                try:
                    item.Close(OL_DISCARD)
                except Exception:
                    pass
            raise

    def signature(self) -> dict:
        """The text Outlook signs a new mail and an answer with. Read once, from files: Outlook is not asked."""
        if self._signatures is None:
            self._signatures = read_signatures()
        return self._signatures

    # -------------------------------------------------- something done to one message, or an answer to an invitation
    def act(self, payload: dict) -> dict:
        request = clean_act_request(payload)
        mail_id = self._moved.get(request["id"], request["id"])
        if request["what"] == "read":
            # Opening a mail on the phone marks it read in Outlook too. It happens behind every tap, so it
            # waits its turn like a read, a busy Outlook is simply not asked, and no list is thrown away.
            result = self._do(self._act, request)
            with self._lock:
                self._forget_narrowed()
                for _, _, page in self._lists.values():
                    for row in page["mail"]:
                        if row["id"] == mail_id:
                            row["unread"] = False
            return result
        self._state_cache = None
        try:
            return self._do(self._act, request, timeout=SEND_SECONDS, first=True)
        except OutlookBusy as error:
            if error.started:  # Outlook is in the middle of it: we cannot know yet whether it happened
                raise MailError("Outlook is taking a long time with this. Look in Outlook before you try it again.")
            raise MailError("Outlook on your PC is busy, so nothing was changed." + error.why() + " Try again in a moment.")
        finally:
            with self._lock:  # the folders and the calendar have changed (or may have)
                self._lists.clear()
                self._forget_narrowed()
                self._calendars.clear()
                self._drop_message(mail_id)

    def _forget_narrowed(self, before: float | None = None) -> None:
        """A search or a narrowed list that was kept a moment is not what the folder holds any more.
        `before`: only the ones kept before that moment. Called with the lock held."""
        for key in [key for key, kept in self._answers.items() if key[0] == "narrowed" and (before is None or kept[0] < before)]:
            del self._answers[key]

    def _act(self, request: dict) -> dict:
        held = self._held_in_the_pane(request["id"]) if request["what"] in ("delete", "move") else []
        if not held:
            return self._act_on(request)
        back = self._out_of_the_pane(request["id"], held)  # a draft written in the reading pane is not deleted or moved while it is there
        try:
            return self._act_on(request)
        finally:
            self._put_back(back)

    def _act_on(self, request: dict) -> dict:
        item = self._item(request["id"])
        message_class = str(getattr(item, "MessageClass", "") or "")
        what = request["what"]
        if what in ("delete", "move") and message_class.startswith("IPM.Appointment"):
            raise MailError("A calendar entry cannot be moved or deleted this way. Open it in the calendar.")
        if what == "delete":
            if self._in_the_bin(item):
                raise MailError("That message is in Deleted Items already. Deleting it again would remove it for good: that is Delete for good.")
            item.Delete()  # to Deleted Items; from the Outbox this also stops the mail from leaving
            return {"ok": True, "deleted": True}
        if what == "erase":  # for good, and only what was deleted before: nothing leaves a folder without a way back
            if not self._in_the_bin(item):
                raise MailError("Only what is in Deleted Items is deleted for good. Delete it first.")
            item.Delete()
            return {"ok": True, "erased": True}
        if what == "move":
            target = self._folder(request["to"]) if request["to"] not in OL_FOLDER else self._ns.GetDefaultFolder(OL_FOLDER[request["to"]])
            item.Move(target)
            return {"ok": True, "moved": True}
        if what in ("read", "unread"):
            if what == "read":
                if not bool(getattr(item, "UnRead", False)):
                    return {"ok": True, "unread": False}
                if bool(getattr(item, "ReadReceiptRequested", False)):
                    # Outlook may ask on the PC whether to send the receipt, and wait for an answer nobody is there to give
                    return {"ok": True, "unread": True, "left": "receipt"}
            item.UnRead = what == "unread"
            item.Save()
            return {"ok": True, "unread": what == "unread"}
        if what in ("flag", "unflag"):
            try:  # the flag Outlook itself sets, which also puts the mail on the to-do list
                item.MarkAsTask(4) if what == "flag" else item.ClearTaskFlag()  # 4: olMarkNoDate
            except Exception as error:
                if getattr(error, "hresult", None) in RPC_BUSY:
                    raise
                item.FlagStatus = 2 if what == "flag" else 0  # an account that keeps no to-do list (IMAP)
            item.Save()
            return {"ok": True, "flagged": what == "flag"}
        if what == "categorize":
            item.Categories = (list_sign() + " ").join(request["categories"])
            item.Save()
            return {"ok": True, "categories": request["categories"]}
        if message_class.startswith("IPM.Schedule.Meeting.Request"):
            appointment = item.GetAssociatedAppointment(True)  # answering it puts it in the calendar, as in Outlook
        elif message_class.startswith("IPM.Appointment"):
            appointment = item
        else:
            raise MailError("That is not an invitation.")
        if appointment is None:
            raise MailError("Outlook cannot find the meeting this invitation is for.")
        if int(getattr(appointment, "MeetingStatus", 0) or 0) in (0, 1):
            raise MailError("This is your own entry: there is nobody to answer.")
        if request["occurrence"] is not None and bool(getattr(appointment, "IsRecurring", False)):
            appointment = self._one_time(appointment, request["occurrence"])  # this one time only: the rest of the series stays unanswered
        answer = appointment.Respond(ANSWERS[request["answer"]], True)  # True: no window opens on the PC
        if answer is None:  # the organizer asked for no answers: Outlook only notes yours
            return {"ok": True, "responded": request["answer"], "told": False}
        if request["note"]:
            answer.Body = request["note"]  # what the organizer reads in the answer
        proposed = bool(request["propose"]) and self._propose(answer, *request["propose"])
        answer.Send()
        return {"ok": True, "responded": request["answer"], "told": True, **({"proposed": proposed} if request["propose"] else {})}

    @staticmethod
    def _propose(answer, start: dt.datetime, end: dt.datetime) -> bool:
        """Write another time into an answer the way Outlook's own "Propose New Time" does, so the organizer's
        Outlook can show it as a proposal and take it over with one press. `start` and `end` are UTC.

        The object model has no call for it; the four fields are set one by one, each read back. A field
        Outlook does not take, or reads back as another moment, takes the whole proposal out again: the
        answer then leaves with the time in words only, and never with a time the words do not say.
        """
        accessor = answer.PropertyAccessor

        def put(name: str, wanted: dt.datetime) -> None:
            def read() -> dt.datetime:
                value = accessor.GetProperty(name)
                return dt.datetime(value.year, value.month, value.day, value.hour, value.minute)

            accessor.SetProperty(name, wanted.replace(tzinfo=dt.timezone.utc))
            got = read()
            if got != wanted:  # taken as if written on another clock: set again by that much less
                accessor.SetProperty(name, (wanted - (got - wanted)).replace(tzinfo=dt.timezone.utc))
                if read() != wanted:
                    raise ValueError(name)

        try:
            put(P_PROPOSED_START, start)
            put(P_PROPOSED_END, end)
            accessor.SetProperty(P_PROPOSED_MINUTES, int((end - start).total_seconds() // 60))
            accessor.SetProperty(P_PROPOSED, True)
            if not bool(accessor.GetProperty(P_PROPOSED)):
                raise ValueError(P_PROPOSED)
            return True
        except Exception as error:
            if getattr(error, "hresult", None) in RPC_BUSY:
                raise
            for name in (P_PROPOSED, P_PROPOSED_MINUTES, P_PROPOSED_END, P_PROPOSED_START):
                try:
                    accessor.DeleteProperty(name)
                except Exception:
                    pass
            return False

    def _in_the_bin(self, item) -> bool:
        try:
            parent = item.Parent
            deleted = parent.Store.GetDefaultFolder(OL_DELETED)
            return bool(self._ns.CompareEntryIDs(parent.EntryID, deleted.EntryID))
        except Exception:
            return False


# ============================================================ demo (invented mail)

class DemoMailbox:
    """The mail and the calendar in web/demo.json, held in memory. Sending moves a message into Sent, nothing leaves."""

    demo = True

    def __init__(self):
        data = json.loads((WEB / "demo.json").read_text(encoding="utf-8"))
        self.me = data["me"]
        self.people_known = data.get("people", [])
        self.book = data.get("book", [])  # the invented company directory
        self.own_folders = data.get("folders", [])
        self.categories = data.get("categories", [])
        self.signatures = data.get("signature", {"new": "", "reply": ""})
        self.addresses = {one["name"]: one["address"] for one in self.people_known + self.book + [self.me]}
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        today = dt.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        def built(event: dict, identity: str) -> dict:
            hour, minute = (int(part) for part in event.get("at", "00:00").split(":"))
            begins = today + dt.timedelta(days=event["inDays"], hours=hour, minutes=minute)
            all_day = bool(event.get("allDay"))
            ends = begins + (dt.timedelta(days=event.get("days", 1)) if all_day else dt.timedelta(minutes=event.get("minutes", 60)))
            last = ends - dt.timedelta(minutes=1) if all_day else ends
            organizer = event.get("organizer", self.me["name"])
            return {
                "id": identity,
                "subject": event["subject"],
                "start": begins.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": ends.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "day": begins.date().isoformat(),
                "lastDay": last.date().isoformat(),
                "allDay": all_day,
                "location": event.get("location", ""),
                "organizer": organizer,
                "meeting": bool(event.get("required")),
                "canceled": False,
                "invited": organizer != self.me["name"],
                "mine": organizer == self.me["name"],
                "response": event.get("response", "organizer" if event.get("required") else ""),
                "recurring": bool(event.get("recurring")),
                "busy": event.get("busy", "busy"),
                "private": bool(event.get("private")),
                "reminder": event.get("reminder", None if all_day else 15),
                "required": event.get("required", ""),
                "optional": event.get("optional", ""),
                "body": event.get("body", ""),
            }

        self.events: list[dict] = [built(event, f"demo.{index + 0xE000:016X}") for index, event in enumerate(data.get("events", []))]
        self.other_calendars = {}  # calendars other people share: read, not answered or changed
        for number, calendar in enumerate(data.get("calendars", [])):
            entries = [built(event, f"demo.{number * 256 + index + 0xC000:016X}") for index, event in enumerate(calendar["events"])]
            for entry in entries:
                entry.update(mine=False, invited=False, other=True)
            self.other_calendars[calendar["id"]] = {"name": calendar["name"], "group": calendar.get("group", ""), "events": entries}
        self.rule_list = [dict(rule) for rule in data.get("rules", [])]
        self.away_on = False
        self.away_text = "I am out of the office and will answer when I am back."
        self.away_outside = ""
        self.pages = {"tasks": [], "notes": [], "contacts": []}  # the invented tasks, notes and contacts
        for index, task in enumerate(data.get("tasks", [])):
            due = (dt.date.today() + dt.timedelta(days=task["dueInDays"])).isoformat() if "dueInDays" in task else ""
            self.pages["tasks"].append({"id": f"demo.{index + 0x7A00:016X}", "kind": "tasks", "title": task["title"], "due": due,
                                        "done": bool(task.get("done")), "important": bool(task.get("important")), "body": task.get("body", "")})
        for index, note in enumerate(data.get("notes", [])):
            when = now - dt.timedelta(minutes=note.get("minutesAgo", 0))
            self.pages["notes"].append({"id": f"demo.{index + 0x7B00:016X}", "kind": "notes", "body": note["body"], "time": when.strftime("%Y-%m-%dT%H:%M:%SZ")})
        for index, contact in enumerate(data.get("contacts", [])):
            self.pages["contacts"].append({
                "id": f"demo.{index + 0x7C00:016X}", "kind": "contacts", "title": contact["name"], "company": contact.get("company", ""),
                "job": contact.get("job", ""), "email": contact.get("email", ""), "mobile": contact.get("mobile", ""), "work": contact.get("phone", ""),
                "address": contact.get("address", ""), "body": contact.get("body", ""),
            })
        self.mail: list[dict] = []
        for index, mail in enumerate(data["mail"]):
            when = now - dt.timedelta(minutes=mail.pop("minutesAgo"))
            self.mail.append({
                "id": f"demo.{index:016X}",
                "time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cc": "",
                "bcc": "",
                "attachments": [],
                "unread": False,
                "flagged": False,
                "important": False,
                "categories": [],
                **mail,
            })
        self.lock = threading.Lock()

    def state(self) -> dict:
        counts = {name: sum(1 for mail in self.mail if mail["folder"] == name) for name in (*FOLDERS, "outbox")}
        return {"ok": True, "demo": True, "account": self.me["address"], "name": self.me["name"], "counts": counts, "away": self.away_on}

    quick_state = state

    def list(self, folder: str, limit: int = 40, offset: int = 0, query: str = "", deep: bool = False, show: str = "") -> dict:
        if folder not in ALL_FOLDERS and not FOLDER_ID.fullmatch(folder):
            raise MailError("Unknown folder.")
        if show and show not in SHOWS:
            raise MailError("Unknown filter.")
        needle = (query or "").lower().strip()
        rows = [
            mail for mail in self.mail
            if mail["folder"] == folder and (not show or mail["unread" if show == "unread" else "flagged"])
            and (not needle or needle in (mail["subject"] + " " + mail["from"] + " " + mail["to"] + (" " + mail["body"] if deep else "")).lower())
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
                "kind": "meeting" if "invite" in mail else "mail",
                "unread": mail["unread"],
                "time": mail["time"],
                "attachments": bool(mail["attachments"]),
                "to": mail["to"],
                "preview": " ".join(mail["body"].split())[:180],
                "flagged": mail["flagged"],
                "important": mail["important"],
            } for mail in page],
        }

    def _find(self, mail_id: str) -> dict:
        for mail in self.mail:
            if mail["id"] == mail_id:
                return mail
        raise MailError("That message was moved or deleted. Refresh the list.")

    def _people(self, names: str) -> list[dict]:
        """"Priya Nair; a@mail.example" as people: a name with its invented address, an address with its name."""
        names_of = {address.lower(): name for name, address in self.addresses.items()}
        found = []
        for entry in filter(None, (part.strip() for part in names.split(";"))):
            if "@" in entry:
                found.append(as_person(names_of.get(entry.lower(), entry), entry))
            else:
                found.append(as_person(entry, self.addresses.get(entry, "")))
        return found

    def _event_summary(self, event: dict) -> dict:
        return {name: value for name, value in event.items() if name not in ("required", "optional", "body")}

    def get(self, mail_id: str) -> dict:
        mail = self._find(mail_id)
        people = {kind: self._people(mail[kind]) for kind in ("to", "cc", "bcc")}
        sender = as_person(mail["from"], mail.get("fromAddress", ""))
        invite = self._event_summary(self.events[mail["invite"]]) if "invite" in mail else None
        return {
            "id": mail["id"],
            "subject": mail["subject"],
            "from": mail["from"],
            "fromAddress": mail.get("fromAddress", ""),
            "to": mail["to"],
            "cc": mail["cc"],
            "bcc": mail["bcc"],
            "people": people,
            "peopleCut": False,
            "answer": answer_plans(sender, people["to"], people["cc"], [], {self.me["address"]}),
            "kind": "meeting" if invite else "mail",
            "invite": invite,
            "inviteAsk": False,  # it is here already
            "flagged": mail["flagged"],
            "important": mail["important"],
            "categories": list(mail["categories"]),
            "time": mail["time"],
            "draft": mail["folder"] in ("drafts", "outbox"),
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

    def away(self) -> dict:
        return {"on": self.away_on, "text": self.away_text, "outside": self.away_outside}

    def news(self) -> list[dict]:
        """Nothing arrives in an invented mailbox by itself; a test puts what is new into `arrived`."""
        with self.lock:
            found, self.__dict__["arrived"] = list(self.__dict__.get("arrived", [])), []
        return found

    def person(self, who: str) -> dict:
        """One person of the invented directory or the invented contacts, by name or address."""
        who = " ".join(str(who or "").split())[:254].lower()
        if len(who) < LOOKUP_SHORTEST:
            raise MailError("Nobody was named.")
        with self.lock:
            for one in self.pages["contacts"]:
                if who in (one["title"].lower(), one["email"].lower()):
                    phones = [{"label": label, "number": number} for label, number in (("Mobile", one["mobile"]), ("Work", one["work"])) if number]
                    return {"found": True, "known": True, "name": one["title"], "address": one["email"], "job": one["job"],
                            "department": "", "company": one["company"], "office": "", "phones": phones}
        for one in self.book + self.people_known:
            if who in (one["name"].lower(), one["address"].lower()):
                return {"found": True, "known": False, "name": one["name"], "address": one["address"], "job": "", "department": "",
                        "company": "", "office": "", "phones": []}
        return {"found": "@" in who, "known": False, "name": who, "address": who if "@" in who else "", "job": "", "department": "",
                "company": "", "office": "", "phones": []} if "@" in who else {"found": False}

    def lookup(self, query: str) -> dict:
        """The invented directory, matched the way Outlook's is: by the start of the name."""
        needle = " ".join(str(query or "").split())[:LOOKUP_LONGEST].lower()
        if len(needle) < LOOKUP_SHORTEST:
            return {"people": [], "book": True}
        found = [dict(person) for person in self.book if person["name"].lower().startswith(needle)]
        return {"people": found[:LOOKUP_ROWS], "book": True}

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
            attachments = []
            if request["mode"] in ("reply", "replyAll"):
                if not request["exact"]:
                    to = to or [source["from"]]
                subject = subject or "RE: " + source["subject"]
                body += f"\n\nFrom: {source['from']}\nSubject: {source['subject']}\n\n{source['body']}"
            if request["mode"] == "forward":
                subject = subject or "FW: " + source["subject"]
                body += f"\n\nFrom: {source['from']}\nSubject: {source['subject']}\n\n{source['body']}"
            if request["mode"] in ("forward", "draft"):  # the files of the original go along, but for the ones taken out
                attachments = [dict(file) for file in source["attachments"]]
                for index, name in request["drop"]:
                    if not 1 <= index <= len(attachments) or attachments[index - 1]["name"] != name:
                        raise MailError(f"\u201c{name}\u201d could not be taken out of this message, so nothing was done. Open it again and try once more.")
                    del attachments[index - 1]
            attachments += [{"name": file["name"], "size": len(file["data"])} for file in request["files"]]
            for address in to + request["cc"] + request["bcc"]:
                if "@" not in address and " " not in address and len(address) < 3:
                    raise MailError(f"Outlook does not know: {address}. Use a full e-mail address.")
            if request["mode"] == "draft":
                self.mail.remove(source)
            folder = "drafts" if request["action"] == "draft" else "outbox" if request["later"] else "sent"
            self.mail.append({
                "id": f"demo.{len(self.mail) + 100:016X}{len(body):04X}",
                "folder": folder,
                "from": self.me["name"],
                "fromAddress": self.me["address"],
                "to": "; ".join(to),
                "cc": "; ".join(request["cc"]),
                "bcc": "; ".join(request["bcc"]),
                "subject": subject or "(no subject)",
                "body": body,
                "time": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "attachments": attachments,
                "unread": False,
                "flagged": False,
                "important": request["importance"] == "high",
                "receipt": request["receipt"],
                "html": text_to_html(body, True) if request["marks"] else "",
                "categories": [],
            })
        if folder == "outbox":
            return {"ok": True, "sent": True, "later": True}
        return {"ok": True, "saved": True} if folder == "drafts" else {"ok": True, "sent": True}

    @staticmethod
    def _page_row(one: dict) -> dict:
        if one["kind"] == "notes":
            first, _, rest = one["body"].strip().partition("\n")
            return {"id": one["id"], "kind": "notes", "title": first.strip()[:120] or "(empty note)", "preview": " ".join(rest.split())[:180], "time": one["time"]}
        if one["kind"] == "tasks":
            return {name: one[name] for name in ("id", "kind", "title", "due", "done", "important")}
        return {"id": one["id"], "kind": "contacts", "title": one["title"], "company": one["company"], "email": one["email"], "phone": one["mobile"] or one["work"]}

    def things(self, kind: str, query: str = "", done: bool = False) -> dict:
        if kind not in THINGS:
            raise MailError("Unknown page.")
        needle = " ".join(str(query or "").split()).lower()
        with self.lock:
            rows = [self._page_row(one) for one in self.pages[kind] if kind != "tasks" or one["done"] == bool(done)]
        rows = [row for row in rows if not needle or needle in " ".join(str(value) for value in row.values()).lower()]
        if kind == "tasks":
            rows.sort(key=lambda row: row["due"] or "9999")
        elif kind == "notes":
            rows.sort(key=lambda row: row["time"], reverse=True)
        else:
            rows.sort(key=lambda row: row["title"].lower())
        return {"kind": kind, "things": rows, "more": False}

    def _page_one(self, kind: str, thing_id: str) -> dict:
        for one in self.pages.get(kind, []):
            if one["id"] == thing_id:
                return one
        raise MailError("That is not what it was when the list was read. Refresh the list.")

    def thing(self, kind: str, thing_id: str) -> dict:
        if kind not in THINGS:
            raise MailError("Unknown page.")
        with self.lock:
            one = self._page_one(kind, thing_id)
            full = {**self._page_row(one), "body": one["body"]}
            if kind == "contacts":
                phones = [("Mobile", one["mobile"]), ("Work", one["work"])]
                full.update(emails=[one["email"]] if one["email"] else [], job=one["job"], mobile=one["mobile"], work=one["work"], address=one["address"],
                            phones=[{"label": label, "number": number} for label, number in phones if number])
            return full

    def keep(self, payload: dict) -> dict:
        request = clean_keep_request(payload)
        kind, what = request["kind"], request["what"]
        with self.lock:
            if what == "remove":
                self.pages[kind].remove(self._page_one(kind, request["id"]))
                return {"ok": True, "removed": True}
            if what in ("done", "undone"):
                self._page_one(kind, request["id"])["done"] = what == "done"
                return {"ok": True, "done": what == "done"}
            if request["id"]:
                one = self._page_one(kind, request["id"])
            else:
                one = {"id": f"demo.{len(self.pages[kind]) + 0x7D00 + list(THINGS).index(kind) * 256:016X}", "kind": kind, "done": False, "address": ""}
                self.pages[kind].append(one)
            one["body"] = request["body"]
            if kind == "notes":
                one["time"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            elif kind == "tasks":
                one.update(title=request["title"], due=request["due"].isoformat() if request["due"] else "", important=request["important"])
            else:
                one.update(title=request["title"], company=request["company"], job=request["job"], email=request["email"],
                           mobile=request["mobile"], work=request["phone"])
            return {"ok": True, "saved": True}

    def folders(self) -> dict:
        with self.lock:
            found = [
                {**folder, "file": "", "unread": sum(1 for mail in self.mail if mail["folder"] == folder["id"] and mail["unread"])}
                for folder in self.own_folders
            ]
        return {"folders": found, "categories": list(self.categories), "cut": False}

    def signature(self) -> dict:
        return dict(self.signatures)

    def plan(self, payload: dict) -> dict:
        request = clean_plan_request(payload)
        utc = lambda moment: moment.astimezone().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        with self.lock:
            rows = [one for one in self.events if one["id"] == request["id"]] if request["id"] else []
            if request["id"] and not rows:
                raise MailError("That calendar entry was moved or deleted. Refresh the calendar.")
            if rows and not rows[0]["mine"]:
                raise MailError("This is somebody else's meeting. It can be answered, not changed.")
            if rows and rows[0]["recurring"]:
                if request["scope"] == "one":
                    rows = [one for one in rows if one["start"] == utc(request["occurrence"])]
                    if not rows:
                        raise MailError("Outlook cannot find that one time of the series. Refresh the calendar and try again.")
                elif request["scope"] != "series":
                    raise MailError("This entry repeats. Say whether this one time or the whole series is meant.")
            if request["what"] == "remove":
                for one in rows:
                    self.events.remove(one)
                return {"ok": True, "removed": True, "told": rows[0]["meeting"]}
            for address in request["with"] + request["optional"]:
                if "@" not in address and " " not in address and len(address) < 3:
                    raise MailError(f"Outlook does not know: {address}. Use a full e-mail address.")
            last = request["end"] - dt.timedelta(minutes=1) if request["allDay"] else request["end"]
            fresh = {"subject": request["subject"], "location": request["location"], "body": request["body"]}
            if request["busy"] is not None:
                fresh["busy"] = request["busy"]
            if request["private"] is not None and request["scope"] != "one":
                fresh["private"] = request["private"]
            when = {
                "start": utc(request["start"]), "end": utc(request["end"]), "day": request["start"].date().isoformat(),
                "lastDay": last.date().isoformat(), "allDay": request["allDay"],
            }
            if request["reminder"] is not None:
                fresh["reminder"] = request["reminder"] if request["reminder"] >= 0 else None
            if not rows:
                repeat = request["repeat"]
                guests = request["with"] + request["optional"]
                first = {
                    "id": f"demo.{len(self.events) + 0xE100:016X}", "organizer": self.me["name"], "meeting": bool(guests),
                    "canceled": False, "invited": False, "mine": True, "response": "organizer" if guests else "",
                    "recurring": bool(repeat), "busy": "busy", "private": False, "required": "; ".join(request["with"]),
                    "optional": "; ".join(request["optional"]), "reminder": 15, **fresh, **when,
                }
                self.events.append(first)
                step = {"day": 1, "week": 7, "month": 30, "year": 365, "weekday": 1}.get(repeat["every"], 0) * repeat["interval"] if repeat else 0
                for number in range(1, min(repeat["times"] or 12, 12) if repeat else 0):  # a year of them at most, in the demo
                    shift = dt.timedelta(days=step * number)
                    self.events.append({
                        **first, "start": utc(request["start"] + shift), "end": utc(request["end"] + shift),
                        "day": (request["start"] + shift).date().isoformat(), "lastDay": (last + shift).date().isoformat(),
                    })
                return {"ok": True, "saved": True, "told": first["meeting"]}
            series = rows[0]["recurring"] and request["scope"] == "series"
            for one in rows:
                one.update(fresh)
                if not series:  # when a series takes place is not changed from the phone
                    one.update(when)
                if request["scope"] != "one" and (request["withExact"] or request["optionalExact"]):
                    if request["withExact"]:
                        one["required"] = "; ".join(request["with"])
                    if request["optionalExact"]:
                        one["optional"] = "; ".join(request["optional"])
                    one["meeting"] = bool(one["required"] or one.get("optional"))
                    one["response"] = "organizer" if one["meeting"] else ""
            return {"ok": True, "saved": True, "told": rows[0]["meeting"]}

    def calendars(self) -> dict:
        return {"calendars": [{"id": identity, "name": one["name"], "group": one["group"]} for identity, one in self.other_calendars.items()]}

    def rules(self) -> dict:
        with self.lock:
            return {"rules": [{"name": rule["name"], "on": bool(rule["on"]), "order": order + 1, "sent": False} for order, rule in enumerate(self.rule_list)]}

    def arrange(self, payload: dict) -> dict:
        request = clean_arrange_request(payload)
        what = request["what"]
        with self.lock:
            own = {folder["id"]: folder for folder in self.own_folders}
            if what == "folder":
                if request["in"] and request["in"] != "inbox" and request["in"] not in own:
                    raise MailError("That folder is not there any more. Open the list of folders again.")
                if any(folder["name"].lower() == request["name"].lower() for folder in self.own_folders):
                    raise MailError(f"There is a folder \u201c{request['name']}\u201d there already.")
                inside = "Inbox" if request["in"] == "inbox" else own[request["in"]]["name"] if request["in"] else ""
                made = {"id": f"f.de30000000.{len(self.own_folders) + 0xB0:016X}", "name": request["name"], "in": inside}
                self.own_folders.append(made)
                return {"ok": True, "id": made["id"], "name": made["name"]}
            if what in ("rename", "unfolder"):
                if request["id"] not in own:
                    raise MailError("That folder is not there any more. Open the list of folders again.")
                if what == "rename":
                    own[request["id"]]["name"] = request["name"]
                    return {"ok": True, "name": request["name"]}
                if any(mail["folder"] == request["id"] for mail in self.mail):
                    raise MailError("That folder is not empty. Only an empty folder is taken out from the phone.")
                self.own_folders.remove(own[request["id"]])
                return {"ok": True, "removed": True}
            if what == "category":
                if any(name.lower() == request["name"].lower() for name in self.categories):
                    raise MailError(f"There is a category \u201c{request['name']}\u201d already.")
                self.categories.append(request["name"])
                return {"ok": True, "name": request["name"]}
            if what == "away":
                self.away_on = request["on"]
                return {"ok": True, "away": self.away_on}
            if what == "awayText":
                self.away_text = request["text"]
                if request["outside"] is not None:
                    self.away_outside = request["outside"]
                return {"ok": True, "text": self.away_text, **({"outside": request["outside"]} if request["outside"] is not None else {})}
            if what == "empty":
                inside = [mail for mail in self.mail if mail["folder"] == request["folder"]]
                for mail in inside[:EMPTY_MOST]:
                    if request["folder"] == "deleted":
                        self.mail.remove(mail)
                    else:
                        mail["folder"] = "deleted"
                return {"ok": True, "emptied": min(len(inside), EMPTY_MOST), "left": max(0, len(inside) - EMPTY_MOST)}
            if what == "rule":
                for rule in self.rule_list:
                    if rule["name"] == request["name"]:
                        rule["on"] = request["on"]
                        return {"ok": True, "on": request["on"]}
                raise MailError("That rule is not there any more.")
            mail = self._find(request["id"])
            target = "Junk" if request["to"] == "junk" else own[request["to"]]["name"] if request["to"] in own else None
            if target is None:
                raise MailError("That folder is not there any more. Open the list of folders again.")
            name = f"{mail['from']} \u2192 {target}"
            self.rule_list.append({"name": name, "on": True})
            return {"ok": True, "name": name}

    def invite(self, mail_id: str) -> dict:
        mail = self._find(mail_id)
        return {"invite": self._event_summary(self.events[mail["invite"]]) if "invite" in mail else None}

    def calendar(self, offset: int = 0, days: int = CALENDAR_DAYS, which: str = "", fresh: bool = False) -> dict:
        offset = max(0, min(int(offset), 730))
        days = max(1, min(int(days), 62))
        if which and which not in self.other_calendars:
            raise MailError("Unknown calendar.")
        first = dt.date.today() + dt.timedelta(days=offset)
        after = first + dt.timedelta(days=days)
        with self.lock:
            events = [
                self._event_summary(event) for event in (self.other_calendars[which]["events"] if which else self.events)
                if event["day"] < after.isoformat() and event["lastDay"] >= first.isoformat()
            ]
        events.sort(key=lambda event: event["start"])
        return {"from": first.isoformat(), "days": days, "offset": offset, "events": events, "more": False}

    def event(self, event_id: str) -> dict:
        shared = [event for calendar in self.other_calendars.values() for event in calendar["events"]]
        for event in self.events + shared:
            if event["id"] == event_id:
                others = lambda text: [one for one in self._people(text) if one["address"] != self.me["address"] and one["name"] != self.me["name"]]  # noqa: E731
                return {**event, "invitedPeople": others(event["required"]), "optionalPeople": others(event.get("optional", ""))}
        raise MailError("That calendar entry was moved or deleted. Refresh the calendar.")

    def act(self, payload: dict) -> dict:
        request = clean_act_request(payload)
        with self.lock:
            what = request["what"]
            if what == "delete":
                mail = self._find(request["id"])
                if mail["folder"] == "deleted":
                    raise MailError("That message is in Deleted Items already. Deleting it again would remove it for good: that is Delete for good.")
                mail["folder"] = "deleted"
                return {"ok": True, "deleted": True}
            if what == "erase":
                mail = self._find(request["id"])
                if mail["folder"] != "deleted":
                    raise MailError("Only what is in Deleted Items is deleted for good. Delete it first.")
                self.mail.remove(mail)
                return {"ok": True, "erased": True}
            if what == "move":
                if request["to"] not in ("inbox", "junk") and request["to"] not in [folder["id"] for folder in self.own_folders]:
                    raise MailError("That folder is not there any more. Open the list of folders again.")
                self._find(request["id"])["folder"] = request["to"]
                return {"ok": True, "moved": True}
            if what in ("read", "unread"):
                self._find(request["id"])["unread"] = what == "unread"
                return {"ok": True, "unread": what == "unread"}
            if what in ("flag", "unflag"):
                self._find(request["id"])["flagged"] = what == "flag"
                return {"ok": True, "flagged": what == "flag"}
            if what == "categorize":
                self._find(request["id"])["categories"] = list(request["categories"])
                return {"ok": True, "categories": request["categories"]}
            event = next((one for one in self.events if one["id"] == request["id"]), None)
            if event is None:
                mail = self._find(request["id"])
                if "invite" not in mail:
                    raise MailError("That is not an invitation.")
                event = self.events[mail["invite"]]
            if not event["invited"]:
                raise MailError("This is your own entry: there is nobody to answer.")
            event["response"] = {"accept": "accepted", "tentative": "tentative", "decline": "declined"}[request["answer"]]
        return {"ok": True, "responded": request["answer"], "told": True}
