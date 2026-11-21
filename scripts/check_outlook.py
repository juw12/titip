"""Check what Titip 0.9.0 and 0.10.0 added against the real Outlook on this PC.

    python scripts/check_outlook.py            reads only: nothing is sent, saved, moved or deleted
    python scripts/check_outlook.py --write    also makes a calendar entry, one that repeats, a folder and a draft
                                               with a file, reads them back and deletes them again (the draft and
                                               the folder end in Deleted Items)

The unit tests run against a stand-in that does what Microsoft's documentation says Outlook does. This
asks the Outlook that is open here. It prints counts and yes or no, never a subject, a name or a text,
so its output can be passed on. An answer to all is made and thrown away unsent, to see whom Outlook
addresses it to and that people can be taken out of it and put into it.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from titip import __version__, mailbox  # noqa: E402
from titip.mailbox import OL_DISCARD, MailError, OutlookMailbox, clean_send_request  # noqa: E402

problems: list[str] = []


def check(condition: bool, label: str) -> None:
    print(("ok   " if condition else "FAIL ") + label, flush=True)
    if not condition:
        problems.append(label)


def attempt(label: str, step):
    """Run one step; what it returns is the detail printed after the label. A step that raises has failed."""
    try:
        detail = step()
        check(True, f"{label}: {detail}" if detail else label)
        return detail
    except Exception as error:  # every failure is worth a line, and the next step is still tried
        check(False, f"{label}: {type(error).__name__}: {str(error)[:160]}")
        return None


def people_keys(people: list[dict]) -> set[str]:
    return {(one["address"] or one["name"]).lower() for one in people}


def main() -> int:
    if sys.platform != "win32":
        print("This checks the real Outlook, so it runs on the Windows PC that has it.")
        return 2
    write = "--write" in sys.argv[1:]
    box = OutlookMailbox(read_ahead=False)
    print(f"Titip {__version__}, {'reading and writing' if write else 'reading only'}", flush=True)

    state = attempt("Outlook answers, and says how many mails wait in the Outbox",
                    lambda: "outbox %d" % box.state(timeout=180)["counts"]["outbox"])
    if state is None:
        print("\nOutlook is not open or does not answer: nothing else can be checked.")
        return 1

    newest: dict = {}

    def inbox():
        rows = box._do(box._list, "inbox", 10, 0, "", timeout=180)["mail"]
        if not rows:
            raise MailError("the inbox is empty")
        newest.update(rows[0])
        return "%d rows, times %s, flag column %s, importance column %s" % (
            len(rows), all(row["time"] for row in rows), "flagged" in rows[0], "important" in rows[0])

    attempt("the inbox is listed with the new columns", inbox)

    for folder in ("outbox", "deleted", "junk"):
        attempt(f"{folder} is listed", lambda folder=folder: "%d rows" % len(box._do(box._list, folder, 10, 0, "", timeout=120)["mail"]))

    def text_search():
        rows = box._do(box._list, "inbox", 5, 0, "the", True, timeout=180)["mail"]
        return "%d rows for a common word" % len(rows)

    attempt("a search that also looks in the text of mail", text_search)

    message: dict = {}
    if newest:
        def read():
            message.update(box._do(box._get, newest["id"], timeout=120))
            people = message["people"]
            return "to %d, cc %d, flagged %s, categories %d, invitation %s" % (
                len(people["to"]), len(people["cc"]), message["flagged"], len(message["categories"]), bool(message.get("inviteAsk")))

        attempt("the newest mail opens with who it went to", read)

    if message and not message["draft"] and message["kind"] == "mail":
        def answer_all():
            item = box._item(newest["id"])
            answer = item.ReplyAll()
            try:
                found, _ = box._recipients(answer.Recipients, 300)
                outlook = {1: set(), 2: set()}
                for name, address, kind in found:
                    outlook.setdefault(kind, set()).add((address if "@" in address else name).lower())
                plan = message["answer"]["replyAll"]
                same = people_keys(plan["to"]) == outlook[1] and people_keys(plan["cc"]) == outlook[2]
                if not plan["to"]:
                    raise MailError("Titip found nobody to answer on this mail")
                # take everybody out but the first, put one address in, and read it back
                first = plan["to"][0]
                request = clean_send_request({
                    "mode": "replyAll", "id": newest["id"], "recipients": "exact", "body": "x",
                    "to": [first["address"] or first["name"]], "cc": ["titip-check@example.invalid"],
                })
                box._address(answer, request)
                after = [(address if "@" in address else name, kind) for name, address, kind in box._recipients(answer.Recipients, 300)[0]]
                changed = len(after) == 2 and {kind for _, kind in after} == {1, 2} and after[-1][0].lower() == "titip-check@example.invalid"
                if not changed:
                    raise MailError(f"after the change the answer has {len(after)} recipients of kinds {[kind for _, kind in after]}")
                return "Outlook addresses it to %d and %d, Titip's list is %s; taking out and putting in works" % (
                    len(outlook[1]), len(outlook[2]), "the same" if same else "DIFFERENT (still sent as Outlook addressed it when untouched)")
            finally:
                answer.Close(OL_DISCARD)  # never saved, never sent

        attempt("an answer to all, made and thrown away unsent", lambda: box._do(answer_all, timeout=180))

    own: dict = {}

    def folders():
        own.update(box._do(box._folders_now, timeout=300))
        return "%d folders of your own, %d categories%s" % (len(own["folders"]), len(own["categories"]), ", more than are listed" if own["cut"] else "")

    attempt("your own folders and the categories", folders)
    if own.get("folders"):
        attempt("a folder of your own is listed",
                lambda: "%d rows" % len(box._do(box._list, own["folders"][0]["id"], 10, 0, "", timeout=120)["mail"]))

    events: list = []

    def calendar():
        page = box._do(box._calendar, 0, 14, timeout=300)
        events.extend(page["events"])
        first, after = page["from"], (dt.date.fromisoformat(page["from"]) + dt.timedelta(days=14)).isoformat()
        inside = all(event["day"] < after and event["lastDay"] >= first for event in events)
        way = {0: "as this PC writes dates", 1: "year first", 2: "month first", None: "no way was needed or none was clean"}[box._date_way]
        if not inside:
            raise MailError("an entry outside the days asked for came through")
        return "%d entries in the next 14 days, dates asked %s (%s), invitations %d" % (
            len(events), way, mailbox.DATE_WAYS[box._date_way or 0](dt.datetime.now()), sum(1 for event in events if event["invited"]))

    attempt("the calendar of the next two weeks", calendar)
    if events:
        def entry():
            full = box._do(box._event, events[0]["id"], timeout=120)
            return "text %d characters, people %s" % (len(full["body"]), bool(full["required"]))

        attempt("one calendar entry opens in full", entry)

    def other_calendars():
        found = box._do(box._calendars_now, timeout=300)
        if not found:
            return "none besides your own"
        page = box._do(box._calendar, 0, 14, found[0]["id"], timeout=300)
        return "%d besides your own; the first one has %d entries in the next 14 days, none of them yours to change: %s" % (
            len(found), len(page["events"]), not any(event["mine"] or event["invited"] for event in page["events"]))

    attempt("the other calendars Outlook shows", other_calendars)

    def rules():
        found = box._do(box._rules_now, timeout=300)
        return "%d rules, %d of them on" % (len(found), sum(1 for rule in found if rule["on"]))

    attempt("Outlook's rules", rules)

    if events:
        def people():
            own = [event for event in events if event["mine"] and event["meeting"]]
            if not own:
                return "no meeting of your own in the next 14 days to look at"
            full = box._do(box._event, own[0]["id"], timeout=120)
            return "a meeting of your own lists %d people invited, reminder %s" % (len(full["invitedPeople"]), full["reminder"])

        attempt("who is invited to a meeting of your own", people)

    def signature():
        found = box.signature()
        return "new mail %d characters, answers %d characters" % (len(found["new"]), len(found["reply"]))

    attempt("the signature", signature)

    if write:
        def entry_that_repeats():
            begins = (dt.datetime.now() + dt.timedelta(days=2)).replace(hour=3, minute=0, second=0, microsecond=0)
            utc = lambda moment: moment.astimezone().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
            name = "Titip check, repeating (deletes itself)"
            box.plan({"what": "save", "subject": name, "start": utc(begins), "end": utc(begins + dt.timedelta(minutes=30)),
                      "repeat": {"every": "day", "times": 3}, "reminder": 45})
            box._calendars.clear()
            made = [event for event in box._do(box._calendar, 2, 5, timeout=300)["events"] if event["subject"] == name]
            try:
                if len(made) != 3:
                    raise MailError(f"{len(made)} times found of an entry made to take place 3 times")
                if [event["reminder"] for event in made] != [45, 45, 45] or not all(event["recurring"] for event in made):
                    raise MailError("the times it takes place do not have the reminder it was made with")
                box.plan({"what": "save", "id": made[1]["id"], "scope": "one", "occurrence": made[1]["start"], "subject": name + ", changed",
                          "start": made[1]["start"], "end": made[1]["end"]})
                box._calendars.clear()
                after = sorted(event["subject"] for event in box._do(box._calendar, 2, 5, timeout=300)["events"] if event["subject"].startswith(name))
                if after != [name, name, name + ", changed"]:
                    raise MailError("changing one time of it did not leave the two others as they were")
            finally:
                if made:
                    box.plan({"what": "remove", "id": made[0]["id"], "scope": "series"})
            return "made for 3 days with a reminder, one time of it changed, the series deleted"

        attempt("a calendar entry that repeats", entry_that_repeats)

        def folder_and_category():
            renamed = "Titip check renamed (deletes itself)"  # no comma: Titip does not take one in a folder's name
            made = box.arrange({"what": "folder", "name": "Titip check (deletes itself)"})
            try:
                box.arrange({"what": "rename", "id": made["id"], "name": renamed})
                listed = [folder for folder in box._do(box._folders_now, timeout=300)["folders"] if folder["id"] == made["id"]]
            finally:
                box.arrange({"what": "unfolder", "id": made["id"]})  # also when a step before failed: no folder is left behind
            if [folder["name"] for folder in listed] != [renamed]:
                raise MailError("the folder was not listed under its new name")
            return "a folder made next to the Inbox, renamed, listed, taken out (it ends in Deleted Items)"

        attempt("a folder of your own", folder_and_category)

        def entry_for_yourself():
            begins = (dt.datetime.now() + dt.timedelta(days=1)).replace(hour=3, minute=0, second=0, microsecond=0)
            utc = lambda moment: moment.astimezone().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
            box.plan({"what": "save", "subject": "Titip check (deletes itself)", "start": utc(begins), "end": utc(begins + dt.timedelta(minutes=30))})
            box._calendars.clear()
            made = [event for event in box._do(box._calendar, 1, 1, timeout=300)["events"] if event["subject"] == "Titip check (deletes itself)"]
            if len(made) != 1:
                raise MailError(f"{len(made)} entries found after making one")
            right = made[0]["start"] == utc(begins) and made[0]["mine"]
            box.plan({"what": "remove", "id": made[0]["id"]})
            if not right:
                raise MailError("the entry was not at the time it was made for")
            return "made at the right time, read back, deleted"

        attempt("a calendar entry for yourself", entry_for_yourself)

        def draft_with_a_file():
            subject = "Titip check (deletes itself) %s" % dt.datetime.now().strftime("%H%M%S")
            box.send({"mode": "new", "action": "draft", "to": [], "subject": subject, "body": "Made by **scripts/check_outlook.py**.\n- one\n- two",
                      "files": [{"name": "titip-check.txt", "data": b"a file attached by Titip"}], "importance": "high",
                      "format": "marks", "receipt": True})
            rows = [row for row in box._do(box._list, "drafts", 20, 0, "", timeout=120)["mail"] if row["subject"] == subject]
            if len(rows) != 1:
                raise MailError(f"{len(rows)} drafts found after saving one")
            saved = box._do(box._get, rows[0]["id"], timeout=120)
            files = [(file["name"], file["size"] > 0) for file in saved["attachments"]]
            box.act({"what": "flag", "id": rows[0]["id"]})
            flagged = box._do(box._get, rows[0]["id"], timeout=120)["flagged"]
            box.act({"what": "delete", "id": rows[0]["id"]})
            if files != [("titip-check.txt", True)] or not saved["important"]:
                raise MailError(f"the draft holds {files}, important {saved['important']}")
            return "saved with its file and high importance, flagged %s, moved to Deleted Items" % flagged

        attempt("a draft with a file", draft_with_a_file)

    print()
    print("FAILED: " + "; ".join(problems) if problems else "PASS")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
