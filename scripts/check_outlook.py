"""Check what Titip 0.9.0 to 0.15.0 added against the real Outlook on this PC.

    python scripts/check_outlook.py            reads only: nothing is sent, saved, moved or deleted
    python scripts/check_outlook.py --write    also makes calendar entries (one that repeats, one over two days, one on two
                                               days of the week, one on a weekday of the month), a folder, a draft
                                               with a file, a task, a note, a contact and a category with a colour, reads them
                                               back and deletes them again (all but the calendar entries and the category end
                                               in Deleted Items).
                                               Where no automatic reply text is set and the replies are off, it writes
                                               one, reads it back and takes it out again
    python scripts/check_outlook.py --away     also switches the automatic replies (out of office) to the other
                                               state and back at once, and writes the text they have again. While they are on, Outlook answers whoever
                                               writes to you: that is why this is asked for by its own word.

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
    switch_away = "--away" in sys.argv[1:]
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

    def narrowed():
        unread = box._do(box._list, "inbox", 20, 0, "", False, "unread", timeout=180)["mail"]
        flagged = box._do(box._list, "inbox", 20, 0, "", False, "flagged", timeout=180)["mail"]
        if not all(row["unread"] for row in unread):
            raise MailError("a mail that is read came back among the unread ones")
        if not all(row["flagged"] for row in flagged):
            raise MailError("a mail without a flag came back among the flagged ones")
        return "%d unread and %d flagged among the newest, and nothing else in either list" % (len(unread), len(flagged))

    attempt("the inbox narrowed to what is unread, and to what is flagged", narrowed)

    def one_person():
        if not newest:
            return "no mail to take a sender from"
        address = box._do(box._get, newest["id"], timeout=120)["fromAddress"]
        if not address:
            return "the newest mail names no address"
        card = box._do(box._person, address, timeout=120)
        if not card["found"]:
            raise MailError("Outlook does not find the sender of the newest mail by the address")
        return "the sender of the newest mail is found: more than the address %s, job %s, department %s, office %s, %d numbers" % (
            card["known"], bool(card["job"]), bool(card["department"]), bool(card["office"]), len(card["phones"]))

    attempt("what Outlook knows of one person", one_person)

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

    def pages():
        said = []
        for kind in ("tasks", "notes", "contacts"):
            page = box._do(box._things, kind, "", False, timeout=300)
            said.append("%d %s%s" % (len(page["things"]), "open tasks" if kind == "tasks" else kind, " and more" if page["more"] else ""))
            if page["things"]:
                full = box._do(box._thing, kind, page["things"][0]["id"], timeout=120)
                if full["id"] != page["things"][0]["id"] or "body" not in full:
                    raise MailError(f"the first of the {kind} did not open in full")
        done = box._do(box._things, "tasks", "", True, timeout=300)
        return ", ".join(said) + "; %d tasks done; the first of each page opens" % len(done["things"])

    attempt("tasks, notes and contacts", pages)

    def contact_search():
        every = box._do(box._things, "contacts", "", False, timeout=300)["things"]
        named = [one for one in every if len(one["title"]) >= 3 and one["title"] != "(no name)"]
        if not named:
            return "no contact with a name to look for"
        found = box._do(box._things, "contacts", named[0]["title"][:3], False, timeout=300)["things"]
        if named[0]["id"] not in [one["id"] for one in found]:
            raise MailError("a contact was not found by the first letters of its name")
        return "the first contact is found by the first three letters of its name, among %d that match" % len(found)

    attempt("a search in the contacts", contact_search)

    def leaves_later():
        then = (dt.datetime.now() + dt.timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)

        def make():
            mail = box._app.CreateItem(0)
            try:
                shift = box._set_time(mail, "DeferredDeliveryTime", then)
                got = mail.DeferredDeliveryTime
                return shift, dt.datetime(got.year, got.month, got.day, got.hour, got.minute)
            finally:
                mail.Close(OL_DISCARD)

        shift, got = box._do(make, timeout=120)
        if got != then:
            raise MailError("the time read back is not the time set")
        return "a new mail takes tomorrow 08:00 as the time it is to leave (Outlook shifted it by %s); it was thrown away unsent" % (shift or "nothing")

    attempt("a mail that leaves later", leaves_later)

    def another_time():
        start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3)).replace(hour=2, minute=30, second=0, microsecond=0, tzinfo=None)

        def make():
            mail = box._app.CreateItem(0)
            try:
                return OutlookMailbox._propose(mail, start, start + dt.timedelta(minutes=45))
            finally:
                mail.Close(OL_DISCARD)

        if not box._do(make, timeout=120):
            return ("this Outlook does not take the fields of a proposed time (or reads them back as another moment): "
                    "an answer that suggests another time says it in words only")
        return "an unsent mail takes the fields Outlook's own Propose New Time writes, and reads them back as set; it was thrown away"

    attempt("another time suggested the way Outlook writes it", another_time)

    def windows():
        def look():
            explorers = box._app.Explorers
            writing = 0
            for index in range(1, explorers.Count + 1):
                try:
                    writing += explorers.Item(index).ActiveInlineResponse is not None
                except Exception:
                    return explorers.Count, None
            return explorers.Count, writing

        count, writing = box._do(look, timeout=120)
        if writing is None:
            return "%d Outlook windows; this Outlook does not say what is written in its reading pane" % count
        return "%d Outlook windows, %d of them with a reply or a draft being written in the reading pane" % (count, writing)

    attempt("what Outlook's windows hold in the reading pane (a draft written there is taken out before Titip sends it)", windows)

    def what_is_new():
        first = box._do(box._news_now, timeout=180)
        again = box._do(box._news_now, timeout=180)
        state = box._news_state
        if first or again:
            raise MailError("the first two looks already called something new")
        return "the Inbox and Outlook's reminders are looked at for what is new: %d mails and %d reminders that are showing are known, nothing announced" % (
            len(state["mails"]), len(state["reminders"]))

    attempt("what a phone would be told while Titip is closed", what_is_new)

    def reply_text():
        state = box._do(box._away_now, timeout=120)
        if state["text"] is None:
            return "this mailbox keeps no note for it: the phone offers the switch only"
        if state["outside"] is None:
            return "the note Outlook keeps for it is read: %d characters; none is kept for people outside" % len(state["text"])
        return "the notes Outlook keeps for it are read: %d characters for colleagues, %d for people outside" % (
            len(state["text"]), len(state["outside"]))

    attempt("what the automatic reply says", reply_text)

    away = attempt("the automatic replies",
                   lambda: {True: "on", False: "off", None: "this mailbox does not say"}[box._do(box._away, timeout=120)])
    if switch_away and away in ("on", "off"):
        def switch():
            was = away == "on"
            there = box.arrange({"what": "away", "on": not was})["away"]
            back = box.arrange({"what": "away", "on": was})["away"]
            if there is was or back is not was:
                raise MailError("the switch did not go there and back")
            return "switched %s and back to %s" % ("off" if was else "on", away)

        attempt("the switch for the automatic replies", switch)

        def same_text_again():
            text = box._do(box._away_now, timeout=120)["text"]
            if not text:
                return "no text is set, so none was written"
            box.arrange({"what": "awayText", "text": text})  # the text it has, written again: nothing changes for anybody
            return "the text it has was written again and read back the same"

        attempt("what the automatic reply says can be written", same_text_again)

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

        def entry_as_0_12_makes_it():
            name = "Titip check, two days (deletes itself)"
            begins = (dt.datetime.now() + dt.timedelta(days=3)).replace(hour=3, minute=0, second=0, microsecond=0)
            utc = lambda moment: moment.astimezone().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
            box.plan({"what": "save", "subject": name, "start": utc(begins), "end": utc(begins + dt.timedelta(days=1, hours=2)),
                      "busy": "free", "private": True, "repeat": {"every": "week", "interval": 2, "times": 3}})
            box._calendars.clear()
            made = [event for event in box._do(box._calendar, 3, 2, timeout=300)["events"] if event["subject"] == name]
            try:
                if len(made) != 1:
                    raise MailError(f"{len(made)} entries found on its first days after making one")
                first = made[0]

                def pattern():
                    series = box._item(first["id"]).GetRecurrencePattern()
                    return int(series.RecurrenceType), int(series.Interval), int(series.Occurrences)

                how = box._do(pattern, timeout=120)
                right = (first["busy"], first["private"], first["start"], first["end"], how) == (
                    "free", True, utc(begins), utc(begins + dt.timedelta(days=1, hours=2)), (1, 2, 3))
            finally:
                if made:
                    box.plan({"what": "remove", "id": made[0]["id"], "scope": "series"})
            if not right:
                raise MailError(f"shows as {first['busy']}, private {first['private']}, repeats {how}: not as it was made")
            return "over two days with hours, shown as free, private, every second week three times; read back and deleted"

        attempt("a calendar entry over two days, private, every second week", entry_as_0_12_makes_it)

        def entry_with_a_pattern(name, repeat, days_read, pattern_wanted, takes_place_on):
            """Make an entry that repeats `repeat`, read back the pattern Outlook keeps and the days it shows, delete it."""
            begins = (dt.datetime.now() + dt.timedelta(days=4)).replace(hour=3, minute=0, second=0, microsecond=0)
            utc = lambda moment: moment.astimezone().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
            how = repeat(begins)
            box.plan({"what": "save", "subject": name, "start": utc(begins), "end": utc(begins + dt.timedelta(minutes=30)), "repeat": how})
            box._calendars.clear()
            made = [event for event in box._do(box._calendar, 4, days_read, timeout=300)["events"] if event["subject"] == name]
            try:
                if not made:
                    raise MailError("the entry is not on the day it was made for")

                def pattern():
                    series = box._item(made[0]["id"]).GetRecurrencePattern()
                    read = []
                    for field in ("RecurrenceType", "DayOfWeekMask", "Instance", "Occurrences"):
                        try:
                            read.append(int(getattr(series, field)))
                        except Exception:
                            read.append(0)  # a field this kind of pattern does not have
                    return tuple(read)

                kept = box._do(pattern, timeout=120)
                days = sorted({(dt.date.fromisoformat(event["day"]) - begins.date()).days for event in made})
            finally:
                if made:
                    box.plan({"what": "remove", "id": made[0]["id"], "scope": "series"})
            wanted = pattern_wanted(begins)
            if kept != wanted or days != takes_place_on:
                raise MailError(f"Outlook keeps the pattern {kept} (wanted {wanted}) and shows it {days} days after its first day (wanted {takes_place_on})")
            return kept

        def on_two_days_of_the_week():
            own = lambda begins: (begins.weekday() + 1) % 7  # noqa: E731  the day it starts on, Sunday 0
            kept = entry_with_a_pattern(
                "Titip check, two days of the week (deletes itself)",
                lambda begins: {"every": "week", "days": [own(begins), (own(begins) + 2) % 7], "day": own(begins), "times": 4},
                10, lambda begins: (1, (1 << own(begins)) | (1 << (own(begins) + 2) % 7), 0, 4), [0, 2, 7, 9])
            return "made for two days of the week four times (Outlook's mask for the days: %d); it takes place on those four days; deleted" % kept[1]

        attempt("a calendar entry on two days of the week", on_two_days_of_the_week)

        def on_a_weekday_of_the_month():
            import calendar as months

            def nth(begins):
                return 5 if begins.day + 7 > months.monthrange(begins.year, begins.month)[1] else (begins.day - 1) // 7 + 1

            kept = entry_with_a_pattern(
                "Titip check, a weekday of the month (deletes itself)",
                lambda begins: {"every": "monthNth", "last": True, "times": 2},
                1, lambda begins: (3, 1 << (begins.weekday() + 1) % 7, nth(begins), 2), [0])
            return "made for the %s such weekday of each month, twice; Outlook keeps that pattern and the first time is on the day asked for; deleted" % (
                {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "last"}[kept[2]])

        attempt("a calendar entry on a weekday of the month", on_a_weekday_of_the_month)

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

        def deleted_for_good():
            subject = "Titip check for good (deletes itself) %s" % dt.datetime.now().strftime("%H%M%S")
            box.send({"mode": "new", "action": "draft", "to": [], "subject": subject, "body": "Made by scripts/check_outlook.py.\n[picture: dot.png]",
                      "files": [{"name": "dot.png", "data": bytes.fromhex(
                          "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360606060000000050001a5f645400000000049454e44ae426082")}]})
            drafts = [row for row in box._do(box._list, "drafts", 20, 0, "", timeout=120)["mail"] if row["subject"] == subject]
            if len(drafts) != 1:
                raise MailError(f"{len(drafts)} drafts found after saving one")
            shown = "cid:" in (box._do(box._get, drafts[0]["id"], timeout=120).get("html") or "")
            try:
                box.act({"what": "erase", "id": drafts[0]["id"]})
                raise RuntimeError("a draft that was not deleted before could be deleted for good")
            except MailError:
                pass
            box.act({"what": "delete", "id": drafts[0]["id"]})
            binned = [row for row in box._do(box._list, "deleted", 30, 0, "", timeout=120)["mail"] if row["subject"] == subject]
            if len(binned) != 1:
                raise MailError(f"{len(binned)} found in Deleted Items after deleting one")
            box.act({"what": "erase", "id": binned[0]["id"]})
            left = [row for row in box._do(box._list, "deleted", 30, 0, "", timeout=120)["mail"] if row["subject"] == subject]
            if left:
                raise MailError("it is still in Deleted Items after it was deleted for good")
            return "a draft with a picture in its text (shown inside the text: %s), refused for good while in Drafts, deleted, then deleted for good" % shown

        attempt("deleted for good, only out of Deleted Items", deleted_for_good)

        stamp = dt.datetime.now().strftime("%H%M%S")

        def one_of(kind, name, done=False):
            found = [one for one in box._do(box._things, kind, name, done, timeout=300)["things"] if one["title"] == name]
            if len(found) != 1:
                raise MailError(f"{len(found)} found after making one")
            return found[0]

        def task():
            name = "Titip check task %s (deletes itself)" % stamp
            due = (dt.date.today() + dt.timedelta(days=3)).isoformat()
            start = (dt.date.today() + dt.timedelta(days=1)).isoformat()
            remind = (dt.datetime.now() + dt.timedelta(days=1)).replace(hour=8, minute=30, second=0, microsecond=0)
            reminds = remind.astimezone().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            box.keep({"kind": "tasks", "what": "save", "title": name, "due": due, "start": start, "remind": reminds, "important": True,
                      "body": "Made by scripts/check_outlook.py."})
            made = one_of("tasks", name)
            try:
                full = box._do(box._thing, "tasks", made["id"], timeout=120)
                right = made["due"] == due and made["important"] and not made["done"] and (full["start"], full["remind"]) == (start, reminds)
                box.keep({"kind": "tasks", "what": "save", "id": made["id"], "title": name, "due": due, "start": "", "remind": "", "important": True,
                          "body": "Made by scripts/check_outlook.py."})
                bare = box._do(box._thing, "tasks", made["id"], timeout=120)
                right = right and (bare["start"], bare["due"], bare["remind"]) == ("", due, "")
                box.keep({"kind": "tasks", "what": "done", "id": made["id"]})
                ticked = one_of("tasks", name, done=True)["done"]
                box.keep({"kind": "tasks", "what": "undone", "id": made["id"]})
                again = not one_of("tasks", name)["done"]
            finally:
                box.keep({"kind": "tasks", "what": "remove", "id": made["id"]})
            if not (right and ticked and again):
                raise MailError(f"due, starting and reminding as asked for, and both taken off again {right}, ticked off {ticked}, open again {again}")
            return ("made due in three days, starting tomorrow, with a reminder; read back as made; start and reminder taken off again; "
                    "ticked off, opened again, moved to Deleted Items")

        attempt("a task", task)

        def note():
            name = "Titip check note %s (deletes itself)" % stamp
            box.keep({"kind": "notes", "what": "save", "body": name + "\nMade by scripts/check_outlook.py."})
            made = one_of("notes", name)
            try:
                text = box._do(box._thing, "notes", made["id"], timeout=120)["body"]
            finally:
                box.keep({"kind": "notes", "what": "remove", "id": made["id"]})
            if text.replace("\r\n", "\n").strip() != name + "\nMade by scripts/check_outlook.py.":
                raise MailError("the note does not hold the text it was made with")
            return "made, read back with its text, moved to Deleted Items"

        attempt("a note", note)

        def contact():
            name = "Titip Check %s" % stamp
            box.keep({"kind": "contacts", "what": "save", "title": name, "company": "Deletes itself", "email": "titip-check@example.com", "mobile": "+00 000 0100"})
            made = one_of("contacts", name)
            try:
                full = box._do(box._thing, "contacts", made["id"], timeout=120)
                box.keep({"kind": "contacts", "what": "save", "id": made["id"], "title": name, "company": "Deletes itself", "job": "Changed",
                          "email": "titip-check@example.com", "mobile": "+00 000 0100"})
                job = box._do(box._thing, "contacts", made["id"], timeout=120)["job"]
            finally:
                box.keep({"kind": "contacts", "what": "remove", "id": made["id"]})
            # Outlook writes a number its own way ("+00 000 0100" comes back as "+00 (000) 0100"): the digits count
            number = "".join(sign for sign in full["mobile"] if sign.isdigit() or sign == "+") == "+000000100"
            if full["emails"] != ["titip-check@example.com"] or not number or job != "Changed":
                raise MailError(f"the contact holds {len(full['emails'])} addresses, its number as given {number}, changed {job == 'Changed'}")
            return "made, found by its name, read back with address and number, changed, moved to Deleted Items"

        attempt("a contact", contact)

        def category_with_a_colour():
            name = "Titip check %s" % stamp

            def take_out():
                box._ns.Categories.Remove(name)

            box.arrange({"what": "category", "name": name, "colour": "teal"})
            try:
                box._folder_list = None
                colours = box._do(box._folders_now, timeout=300).get("colours", {})
            finally:
                box._do(take_out, timeout=120)
                box._folder_list = None
            if colours.get(name) != "teal":
                raise MailError(f"made teal, Outlook says {colours.get(name)!r}")
            still = name in box._do(box._folders_now, timeout=300)["categories"]
            if still:
                raise MailError("the category of the check is still in Outlook's list: take it out there")
            return "made teal, read back as teal among %d categories with a colour to read, taken out of Outlook's list again" % len(colours)

        attempt("a category with a colour", category_with_a_colour)

        def reply_text_written():
            before = box._do(box._away_now, timeout=120)
            if before["text"] is None:
                return "this mailbox keeps no note for it: nothing to write"
            if before["on"] is not False:
                return "the automatic replies are on (or do not say): the text people get now was left alone"
            if before["text"] or before["outside"]:
                return "a text is set: it was left alone, so that nothing of it is lost"
            both = before["outside"] is not None

            def put_back():
                for outside in ((False, True) if both else (False,)):
                    note = box._away_note(outside)
                    note.Body = ""
                    note.Save()

            try:
                order = {"what": "awayText", "text": "Titip check (taken out again at once)"}
                if both:
                    order["outside"] = "Titip check, outside (taken out again at once)"
                box.arrange(order)
                kept = box._do(box._away_now, timeout=120)
                if kept["text"] != order["text"] or (both and kept["outside"] != order["outside"]):
                    raise MailError("the text read back is not the text written")
            finally:
                box._do(put_back, timeout=120)
            after = box._do(box._away_now, timeout=120)
            if after["text"] or after["outside"]:
                raise MailError("the text of the check is still there: take it out in Outlook, File, Automatic Replies")
            return "no text was set and the replies are off: a text was written for colleagues%s, read back, and taken out again" % (
                " and one for people outside" if both else "")

        attempt("what the automatic reply says, written", reply_text_written)

    print()
    print("FAILED: " + "; ".join(problems) if problems else "PASS")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
