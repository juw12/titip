"""What 0.11.0 brought from Outlook, against a fake Outlook: the tasks, notes and contacts pages,
a mail that leaves later, and the switch for automatic replies."""
import datetime as dt
import json
import unittest

from tests.test_more_of_outlook import ID, App, Mail, outlook_with
from tests import test_server
from titip import mailbox, remote
from titip.mailbox import DemoMailbox, MailError, OutlookBusy, OutlookMailbox, clean_arrange_request, clean_keep_request, clean_send_request

TASK, NOTE, CONTACT = "IPM.Task", "IPM.StickyNote", "IPM.Contact"


class Thing:
    """A task, a note or a contact as Outlook holds it."""

    def __init__(self, entry="", kind=TASK, **fields):
        self.EntryID, self.MessageClass, self.calls = entry, kind, []
        self.__dict__.update(fields)

    def Save(self):
        self.calls.append("save")

    def Delete(self):
        self.calls.append("delete")

    def Close(self, how):
        self.calls.append(f"close{how}")

    def MarkComplete(self):
        self.Complete = True
        self.calls.append("complete")


class Items(list):
    """A folder's items. Outlook sorts and narrows them itself; `no_search`: it cannot search this folder."""

    def __init__(self, things=(), asked=None, no_search=False):
        super().__init__(things)
        self.asked, self.no_search = [] if asked is None else asked, no_search

    def Sort(self, by, descending=False):
        self.asked.append(("sort", by, descending))

    def Restrict(self, text):
        self.asked.append(("restrict", text))
        if text.startswith("@SQL="):
            if self.no_search:
                raise RuntimeError("Cannot parse condition.")
            needle = text.split("'%")[1].split("%'")[0]
            return Items([one for one in self if needle in f"{one.FullName} {one.CompanyName} {one.Email1Address}".lower()], self.asked)
        return Items([one for one in self if one.Complete is ("True" in text)], self.asked)


class Namespace:
    def __init__(self, items):
        self.items, self.folders = items, []

    def GetDefaultFolder(self, number):
        self.folders.append(number)
        return type("Folder", (), {"StoreID": "STORE", "Items": self.items})()


def day(year, month, date):
    return dt.datetime(year, month, date)


def box_with(*things, no_search=False):
    box = OutlookMailbox(read_ahead=False)
    box._ns = Namespace(Items(things, no_search=no_search))
    return box


PEOPLE = (
    Thing("C1", CONTACT, FullName="Ada Okafor", CompanyName="Okafor Supplies", Email1Address="orders@okafor-supplies.example",
          MobileTelephoneNumber="+00 000 0142", BusinessTelephoneNumber="+00 000 0140", JobTitle="Sales", Body="Quotes in two days."),
    Thing("C2", "IPM.DistList", FullName="", CompanyName="", Email1Address="", Subject="Hardware team"),
    Thing("C3", CONTACT, FullName="Priya Nair", CompanyName="Lumenworks", Email1Address="/o=Lumenworks/cn=priya", BusinessTelephoneNumber="+00 000 0108"),
)


class TheLists(unittest.TestCase):
    def test_open_tasks_come_by_the_day_they_are_due_and_the_done_ones_only_when_asked_for(self):
        box = box_with(
            Thing("T1", Subject="Send the drawing", DueDate=day(2026, 11, 23), Complete=False, Importance=2),
            Thing("T2", Subject="", DueDate=day(4501, 1, 1), Complete=False, Importance=1),
            Thing("T3", Subject="Return the rig", DueDate=day(2026, 11, 18), Complete=True, Importance=1),
        )
        found = box._things("tasks", "", False)
        key = box._store_key("STORE")
        self.assertEqual(found, {"kind": "tasks", "more": False, "things": [
            {"id": f"{key}.T1", "kind": "tasks", "title": "Send the drawing", "due": "2026-11-23", "start": "", "done": False, "important": True},
            {"id": f"{key}.T2", "kind": "tasks", "title": "(no subject)", "due": "", "start": "", "done": False, "important": False},
        ]})
        self.assertEqual(box._ns.items.asked, [("restrict", "[Complete] = False"), ("sort", "[DueDate]", False)])
        self.assertEqual(box._ns.folders, [mailbox.OL_TASKS])
        done = box._things("tasks", "", True)["things"]
        self.assertEqual([(row["title"], row["done"], row["due"]) for row in done], [("Return the rig", True, "2026-11-18")])

    def test_a_note_is_its_first_line_and_the_rest_under_it_newest_first(self):
        changed = dt.datetime(2026, 11, 20, 9, 30)
        box = box_with(
            Thing("N1", NOTE, Body="Hinge torque window\r\n0.40 N·m over 10,000 cycles.\r\nDrawing rev C.", LastModificationTime=changed),
            Thing("N2", NOTE, Body="", LastModificationTime=changed),
        )
        first, empty = box._things("notes", "", False)["things"]
        self.assertEqual((first["title"], first["preview"]), ("Hinge torque window", "0.40 N·m over 10,000 cycles. Drawing rev C."))
        self.assertEqual(first["time"], changed.astimezone().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(empty["title"], "(empty note)")
        self.assertEqual(box._ns.items.asked, [("sort", "[LastModificationTime]", True)])
        self.assertEqual([row["title"] for row in box._things("notes", "TORQUE", False)["things"]], ["Hinge torque window"])

    def test_contacts_are_people_only_and_an_address_that_is_none_is_not_shown_as_one(self):
        rows = box_with(*PEOPLE)._things("contacts", "", False)["things"]
        self.assertEqual([(row["title"], row["company"], row["email"], row["phone"]) for row in rows], [
            ("Ada Okafor", "Okafor Supplies", "orders@okafor-supplies.example", "+00 000 0142"),
            ("Priya Nair", "Lumenworks", "", "+00 000 0108"),
        ], "a list of people is left out; an internal name is not an address to write to")

    def test_outlook_looks_for_a_contact_itself_and_where_it_cannot_they_are_looked_through(self):
        box = box_with(*PEOPLE)
        self.assertEqual([row["title"] for row in box._things("contacts", "okafor's [x]", False)["things"]], [])
        asked = box._ns.items.asked[0][1]
        self.assertEqual(box._ns.items.asked[1], ("sort", "[FullName]", False), "what Outlook found is put in order, not the folder before it looked")
        self.assertTrue(asked.startswith('@SQL="urn:schemas:contacts:cn" like \'%okafor s  x%\' OR "urn:schemas:contacts:o" like '), asked)
        self.assertEqual([row["title"] for row in box._things("contacts", "lumen", False)["things"]], ["Priya Nair"])
        slow = box_with(*PEOPLE, no_search=True)
        self.assertEqual([row["title"] for row in slow._things("contacts", "supplies", False)["things"]], ["Ada Okafor"])

    def test_a_busy_outlook_is_not_taken_for_a_folder_it_cannot_search(self):
        busy = type("Busy", (Exception,), {"hresult": next(iter(mailbox.RPC_BUSY))})

        class Refusing(Items):
            def Restrict(self, text):
                raise busy()

        box = box_with()
        box._ns.items = Refusing(PEOPLE)
        with self.assertRaises(busy):
            box._things("contacts", "ada", False)

    def test_a_long_list_is_cut_and_says_so_and_a_thing_outlook_cannot_read_is_left_out(self):
        class Broken:
            EntryID = "X"

            @property
            def MessageClass(self):
                raise RuntimeError("damaged")

        many = [Thing(f"N{number}", NOTE, Body=f"Note {number}", LastModificationTime=day(2026, 11, 20)) for number in range(mailbox.THINGS_SHOWN + 5)]
        found = box_with(Broken(), *many)._things("notes", "", False)
        self.assertEqual((len(found["things"]), found["more"], found["things"][0]["title"]), (mailbox.THINGS_SHOWN, True, "Note 0"))

    def test_the_list_is_asked_for_once_and_kept_like_every_slow_answer(self):
        box = box_with(Thing("T1", Subject="Send the drawing", DueDate=day(2026, 11, 23), Complete=False, Importance=1))
        asked = []

        def do(fn, *args, timeout=20, first=False):
            asked.append((fn.__name__, args, timeout))
            return fn(*args)

        box._do = do
        self.assertEqual(box.things("tasks", "  Send   the ")["things"][0]["title"], "Send the drawing")
        box.things("tasks", "send the")
        self.assertEqual(asked, [("_things", ("tasks", "Send the", False), mailbox.SLOW_SECONDS)])
        self.assertIn("things", mailbox.SLOW)
        self.assertIn("_things", mailbox.DOING)
        with self.assertRaises(MailError):
            box.things("calendar")
        with self.assertRaises(MailError):
            box.thing("mail", ID)


class OneInFull(unittest.TestCase):
    def test_a_contact_with_every_address_and_number_it_has(self):
        box = outlook_with(Thing("C1", CONTACT, FullName="Ada Okafor", CompanyName="Okafor Supplies", JobTitle="Sales", Body="Quotes in two days.",
                                 Email1Address="orders@okafor-supplies.example", Email2Address="/o=internal", Email3Address="ada@example.com",
                                 MobileTelephoneNumber="+00 000 0142", HomeTelephoneNumber="+00 000 0199", BusinessAddress="1 Mill Road\r\nSpringfield"))
        full = box._thing("contacts", ID)
        self.assertEqual((full["id"], full["title"], full["job"], full["body"]), (ID, "Ada Okafor", "Sales", "Quotes in two days."))
        self.assertEqual(full["emails"], ["orders@okafor-supplies.example", "ada@example.com"])
        self.assertEqual(full["phones"], [{"label": "Mobile", "number": "+00 000 0142"}, {"label": "Home", "number": "+00 000 0199"}])
        self.assertEqual((full["mobile"], full["work"], full["address"]), ("+00 000 0142", "", "1 Mill Road Springfield"))

    def test_what_is_not_of_that_page_is_not_opened_changed_or_taken_out(self):
        letter = Thing("M1", "IPM.Note", Subject="Hinge sample B")
        box = outlook_with(letter)
        for attempt in (lambda: box._thing("tasks", ID), lambda: box._keep_thing(clean_keep_request({"kind": "notes", "what": "remove", "id": ID})),
                        lambda: box._keep_thing(clean_keep_request({"kind": "tasks", "what": "done", "id": ID}))):
            with self.assertRaises(MailError) as refused:
                attempt()
            self.assertIn("Refresh the list", str(refused.exception))
        self.assertEqual(letter.calls, [], "a mail is never deleted through the tasks or the notes page")

    def test_one_that_is_gone_is_said_to_be_gone_in_words_that_fit(self):
        box = outlook_with()

        def gone(thing_id):
            raise MailError("That message was moved or deleted. Refresh the list.")

        box._item = gone
        with self.assertRaises(MailError) as refused:
            box._thing("notes", ID)
        self.assertEqual(str(refused.exception), "It is not there any more. Refresh the list.")


class Made(App):
    def __init__(self, make=Thing):
        super().__init__()
        self.kinds, self.make = [], make

    def CreateItem(self, kind):
        self.kinds.append(kind)
        self.created.append(self.make())
        return self.created[-1]


class Keeping(unittest.TestCase):
    def box(self, existing=None, make=Thing):
        box = outlook_with(existing)
        box._app = Made(make)
        return box

    def keep(self, box, **payload):
        return box._keep_thing(clean_keep_request(payload))

    def test_a_task_is_made_with_its_day_and_how_much_it_matters(self):
        box = self.box()
        self.assertEqual(self.keep(box, kind="tasks", what="save", title="  Book the   drop tower ", due="2026-11-24", important=True, body="Corner drops."),
                         {"ok": True, "saved": True})
        task = box._app.created[0]
        self.assertEqual(box._app.kinds, [3])
        self.assertEqual((task.Subject, task.Importance, task.Body, task.calls), ("Book the drop tower", 2, "Corner drops.", ["save"]))
        self.assertEqual(task.DueDate, dt.datetime(2026, 11, 24, 12), "noon: an Outlook that shifts the time still lands on that day")
        self.keep(box, kind="tasks", what="save", title="Order hinge samples")
        self.assertEqual((hasattr(box._app.created[1], "DueDate"), box._app.created[1].Importance), (False, 1),
                         "no day: none is set on a task that has none (a real Outlook takes only its own no date)")

    def test_a_day_outlook_shifts_over_midnight_is_set_again_until_it_is_the_day_asked_for(self):
        class Shifted(Thing):
            def __setattr__(self, name, value):
                self.__dict__[name] = value - dt.timedelta(hours=13) if name == "DueDate" else value

        box = self.box(make=Shifted)
        self.keep(box, kind="tasks", what="save", title="Book the drop tower", due="2026-11-24")
        self.assertEqual(box._app.created[0].DueDate, dt.datetime(2026, 11, 24, 23))

        class Stuck(Thing):
            def __setattr__(self, name, value):
                self.__dict__[name] = dt.datetime(2020, 1, 1) if name == "DueDate" else value

        box = self.box(make=Stuck)
        with self.assertRaises(MailError) as refused:
            self.keep(box, kind="tasks", what="save", title="Book the drop tower", due="2026-11-24")
        self.assertIn("did not take that day", str(refused.exception))
        self.assertEqual(box._app.created[0].calls, [f"close{mailbox.OL_DISCARD}"], "what could not be made right is not left behind half made")

    def test_a_task_is_ticked_off_put_back_changed_and_taken_out(self):
        task = Thing("T1", Subject="Send the drawing", Body="With the  Shore A note.\r\n", DueDate=day(2026, 11, 23), Complete=False, Importance=1)
        box = self.box(task)
        self.assertEqual(self.keep(box, kind="tasks", what="done", id=ID), {"ok": True, "done": True})
        self.assertEqual((task.Complete, task.calls), (True, ["complete"]))
        self.assertEqual(self.keep(box, kind="tasks", what="undone", id=ID), {"ok": True, "done": False})
        self.assertEqual((task.Complete, task.calls), (False, ["complete", "save"]))
        self.keep(box, kind="tasks", what="save", id=ID, title="Send the drawing rev C", body="With the Shore A note.", due="2026-11-25")
        self.assertEqual((task.Subject, task.DueDate.day, box._app.kinds), ("Send the drawing rev C", 25, []))
        self.assertEqual(task.Body, "With the  Shore A note.\r\n", "text nobody touched keeps the formatting it has in Outlook")
        self.assertEqual(self.keep(box, kind="tasks", what="remove", id=ID), {"ok": True, "removed": True})
        self.assertEqual(task.calls[-1], "delete")

    def test_a_note_and_a_contact(self):
        box = self.box()
        self.keep(box, kind="notes", what="save", body="Lab 2 door code changes on the first.", title="not used")
        self.keep(box, kind="contacts", what="save", title="Marek Novak", company="Okafor Supplies", job="Application engineer",
                  email="marek.novak@okafor-supplies.example", mobile="+00 000 0141", phone="+00 000 0140", body="Knows the gasket.")
        note, person = box._app.created
        self.assertEqual(box._app.kinds, [5, 2])
        self.assertEqual((note.Body, note.calls, hasattr(note, "Subject")), ("Lab 2 door code changes on the first.", ["save"], False))
        self.assertEqual((person.FullName, person.CompanyName, person.JobTitle, person.Email1Address, person.MobileTelephoneNumber, person.BusinessTelephoneNumber, person.Body),
                         ("Marek Novak", "Okafor Supplies", "Application engineer", "marek.novak@okafor-supplies.example", "+00 000 0141", "+00 000 0140", "Knows the gasket."))

    def test_a_colleague_outlook_knows_by_an_internal_name_keeps_it_when_the_phone_shows_no_address(self):
        colleague = Thing("C3", CONTACT, FullName="Priya Nair", CompanyName="Lumenworks", JobTitle="", Email1Address="/o=Lumenworks/cn=priya", Body="")
        self.keep(self.box(colleague), kind="contacts", what="save", id=ID, title="Priya Nair", company="Lumenworks", job="Design lead", email="")
        self.assertEqual((colleague.JobTitle, colleague.Email1Address), ("Design lead", "/o=Lumenworks/cn=priya"))
        self.keep(self.box(colleague), kind="contacts", what="save", id=ID, title="Priya Nair", email="priya.nair@lumenworks.example")
        self.assertEqual(colleague.Email1Address, "priya.nair@lumenworks.example")

    def test_what_cannot_be_kept_is_refused_before_outlook_is_asked(self):
        for bad, why in (
            ({"kind": "mail", "what": "save", "title": "x"}, "Unknown action."),
            ({"kind": "notes", "what": "done", "id": ID}, "Unknown action."),
            ({"kind": "tasks", "what": "burn", "id": ID}, "Unknown action."),
            ({"kind": "tasks", "what": "remove"}, "It is missing."),
            ({"kind": "tasks", "what": "save", "title": "   "}, "Say what is to be done first."),
            ({"kind": "tasks", "what": "save", "title": "x", "due": "next week"}, "That day could not be read."),
            ({"kind": "notes", "what": "save", "body": " \n "}, "Write the note first."),
            ({"kind": "contacts", "what": "save", "title": ""}, "Give it a name first."),
            ({"kind": "contacts", "what": "save", "title": "Ada", "email": "ada at example"}, "That e-mail address is not complete."),
        ):
            with self.assertRaises(MailError) as refused:
                clean_keep_request(bad)
            self.assertEqual(str(refused.exception), why, bad)
        clean = clean_keep_request({"kind": "contacts", "what": "save", "title": "A" * 900, "mobile": "1" * 900})
        self.assertEqual((len(clean["title"]), len(clean["mobile"])), (255, 60))

    def test_a_change_goes_before_the_reading_and_the_lists_of_that_page_are_read_anew(self):
        box = self.box()
        asked = []

        def do(fn, *args, timeout=20, first=False):
            asked.append((fn.__name__, timeout, first))
            return {"ok": True, "saved": True}

        box._do = do
        box._answers[("things", "notes", "", False)] = (0, {})
        box._answers[("things", "tasks", "", False)] = (0, {})
        box.keep({"kind": "notes", "what": "save", "body": "x"})
        self.assertEqual(asked, [("_keep_thing", mailbox.SEND_SECONDS, True)])
        self.assertEqual(list(box._answers), [("things", "tasks", "", False)])

    def test_a_busy_outlook_says_whether_anything_was_changed(self):
        box = self.box()

        def busy(started):
            def do(fn, *args, timeout=20, first=False):
                raise OutlookBusy(started)
            return do

        box._do = busy(False)
        with self.assertRaises(MailError) as refused:
            box.keep({"kind": "notes", "what": "save", "body": "x"})
        self.assertIn("nothing was changed", str(refused.exception))
        box._do = busy(True)
        with self.assertRaises(MailError) as unsure:
            box.keep({"kind": "notes", "what": "save", "body": "x"})
        self.assertIn("Look in Outlook before you try it again", str(unsure.exception))


def utc(moment):
    return moment.astimezone().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LeavesLater(unittest.TestCase):
    def setUp(self):
        self.then = (dt.datetime.now() + dt.timedelta(hours=20)).replace(second=0, microsecond=0)

    def order(self, **more):
        return {"mode": "new", "to": "a@example.com", "body": "Tomorrow's numbers.", "later": utc(self.then), **more}

    def test_the_mail_is_handed_to_outlook_now_with_the_time_it_is_to_leave(self):
        box = outlook_with()
        self.assertEqual(box._send(clean_send_request(self.order())), {"ok": True, "sent": True, "later": True})
        mail = box._app.created[0]
        self.assertEqual((mail.DeferredDeliveryTime, mail.calls), (self.then, ["send"]))
        box._send(clean_send_request({**self.order(), "later": ""}))
        self.assertFalse(hasattr(box._app.created[1], "DeferredDeliveryTime"))

    def test_an_outlook_that_shifts_the_time_is_given_it_again_until_it_is_right(self):
        class Shifting(Mail):
            def __setattr__(self, name, value):
                super().__setattr__(name, value + dt.timedelta(hours=8) if name == "DeferredDeliveryTime" else value)

        box = outlook_with()
        box._app.CreateItem = lambda kind: box._app.created.append(Shifting()) or box._app.created[-1]
        box._send(clean_send_request(self.order()))
        self.assertEqual((box._app.created[0].DeferredDeliveryTime, box._app.created[0].calls), (self.then, ["send"]))

    def test_a_time_outlook_does_not_take_stops_the_mail_instead_of_sending_it_now(self):
        class Deaf(Mail):
            def __setattr__(self, name, value):
                super().__setattr__(name, dt.datetime(2020, 1, 1) if name == "DeferredDeliveryTime" else value)

        box = outlook_with()
        box._app.CreateItem = lambda kind: box._app.created.append(Deaf()) or box._app.created[-1]
        with self.assertRaises(MailError):
            box._send(clean_send_request(self.order()))
        self.assertNotIn("send", box._app.created[0].calls)

    def test_a_time_that_has_passed_or_is_too_far_off_is_refused_and_a_draft_has_none(self):
        for when, why in ((dt.datetime.now() - dt.timedelta(minutes=5), "passed already"),
                          (dt.datetime.now() + dt.timedelta(days=mailbox.LATER_MOST_DAYS + 1), "60 days at most"), ("tomorrow", "could not be read")):
            with self.assertRaises(MailError) as refused:
                clean_send_request(self.order(later=utc(when) if isinstance(when, dt.datetime) else when))
            self.assertIn(why, str(refused.exception))
        self.assertIsNone(clean_send_request(self.order(action="draft"))["later"])


class Store:
    """A mailbox that says whether its automatic replies are on; `takes`: does the switch do anything?"""

    def __init__(self, on=False, takes=True, exchange=True):
        self.on, self.takes, self.exchange, self.set = on, takes, exchange, []
        self.PropertyAccessor = self

    def GetProperty(self, name):
        if not self.exchange:
            raise RuntimeError("The property is unknown or cannot be found.")
        return self.on

    def SetProperty(self, name, value):
        if not self.exchange:
            raise RuntimeError("not permitted")
        self.set.append((name, value))
        if self.takes:
            self.on = value


class AutomaticReplies(unittest.TestCase):
    def box(self, **how):
        box = OutlookMailbox(read_ahead=False)
        box._ns = type("NS", (), {"DefaultStore": Store(**how)})()
        return box

    def test_the_state_says_whether_they_are_on_or_that_this_mailbox_does_not_say(self):
        self.assertIs(self.box(on=True)._away(), True)
        self.assertIs(self.box()._away(), False)
        self.assertIsNone(self.box(exchange=False)._away())

    def test_the_switch_and_what_comes_back_is_what_outlook_says_afterwards(self):
        box = self.box()
        box._state_cache = "kept"
        self.assertEqual(box._arrange(clean_arrange_request({"what": "away", "on": True})), {"ok": True, "away": True})
        self.assertEqual((box._ns.DefaultStore.set, box._state_cache), ([(mailbox.P_AWAY, True)], None))
        self.assertEqual(box._arrange(clean_arrange_request({"what": "away", "on": "yes"})), {"ok": True, "away": False}, "anything but true switches them off")

    def test_a_switch_outlook_refuses_or_ignores_is_not_reported_as_done(self):
        with self.assertRaises(MailError) as refused:
            self.box(exchange=False)._arrange(clean_arrange_request({"what": "away", "on": True}))
        self.assertIn("did not let Titip switch", str(refused.exception))
        with self.assertRaises(MailError) as ignored:
            self.box(takes=False)._arrange(clean_arrange_request({"what": "away", "on": True}))
        self.assertIn("did not change", str(ignored.exception))


class Demo(unittest.TestCase):
    def test_the_three_pages_and_what_is_done_on_them(self):
        box = DemoMailbox()
        tasks = box.things("tasks")["things"]
        self.assertEqual([task["due"] for task in tasks if task["due"]], sorted(task["due"] for task in tasks if task["due"]))
        self.assertTrue(all(not task["done"] for task in tasks) and box.things("tasks", done=True)["things"][0]["done"])
        self.assertEqual(box.keep({"kind": "tasks", "what": "done", "id": tasks[0]["id"]}), {"ok": True, "done": True})
        self.assertEqual(len(box.things("tasks")["things"]), len(tasks) - 1)
        box.keep({"kind": "tasks", "what": "save", "title": "Check the tool quote", "due": "2026-12-01"})
        made = next(task for task in box.things("tasks")["things"] if task["title"] == "Check the tool quote")
        self.assertEqual((made["due"], box.thing("tasks", made["id"])["body"]), ("2026-12-01", ""))
        box.keep({"kind": "notes", "what": "save", "body": "Ask about the lead time\nfor 100 pieces"})
        self.assertEqual(box.things("notes")["things"][0]["title"], "Ask about the lead time", "the newest note is on top")
        people = box.things("contacts", "okafor")["things"]
        self.assertEqual([person["title"] for person in people], ["Ada Okafor", "Marek Novak"])
        full = box.thing("contacts", people[0]["id"])
        self.assertEqual((full["emails"], full["phones"][0]["label"]), (["orders@okafor-supplies.example"], "Mobile"))
        box.keep({"kind": "contacts", "what": "remove", "id": people[0]["id"]})
        with self.assertRaises(MailError):
            box.thing("contacts", people[0]["id"])
        with self.assertRaises(MailError):
            box.thing("notes", people[1]["id"])

    def test_a_mail_that_leaves_later_waits_in_the_outbox_and_the_replies_switch(self):
        box = DemoMailbox()
        later = utc(dt.datetime.now() + dt.timedelta(hours=3))
        self.assertEqual(box.send({"mode": "new", "to": "a@example.com", "subject": "Later", "body": "x", "later": later}), {"ok": True, "sent": True, "later": True})
        self.assertIn("Later", [mail["subject"] for mail in box.list("outbox")["mail"]])
        self.assertIs(box.state()["away"], False)
        self.assertEqual(box.arrange({"what": "away", "on": True}), {"ok": True, "away": True})
        self.assertIs(box.state()["away"], True)


class OverTheWire(test_server.ServerCase):
    def setUp(self):
        self.token = self.pair()[0]

    def get(self, path):
        reply, data = self.phone("GET", path, cookie=self.token)
        return reply, json.loads(data)

    def post(self, path, body, cookie=True):
        reply, data = self.phone("POST", path, body, test_server.X, self.token if cookie else None)
        return reply.status, json.loads(data)

    def test_the_pages_are_read_and_changed_by_a_paired_phone_only(self):
        reply, tasks = self.get("/api/things?kind=tasks")
        self.assertEqual((reply.status, tasks["kind"], tasks["things"][0]["kind"]), (200, "tasks", "tasks"))
        self.assertEqual(self.get("/api/things?kind=tasks&done=1")[1]["things"][0]["done"], True)
        self.assertEqual(self.app.mail_call("things", {"kind": "tasks"}), (200, tasks))  # what the relay is answered with
        self.assertEqual(self.post("/api/keep", {"kind": "notes", "what": "save", "body": "Made over the wire\nsecond line"}), (200, {"ok": True, "saved": True}))
        note = self.get("/api/things?kind=notes&q=wire")[1]["things"][0]
        self.assertEqual(self.get(f"/api/thing?kind=notes&id={note['id']}")[1]["body"], "Made over the wire\nsecond line")
        self.assertEqual(self.post("/api/keep", {"kind": "notes", "what": "remove", "id": note["id"]}), (200, {"ok": True, "removed": True}))
        status, answer = self.post("/api/keep", {"kind": "contacts", "what": "save", "title": ""})
        self.assertEqual((status, answer["error"]), (400, "Give it a name first."))
        self.assertEqual(self.get("/api/things?kind=mail")[0].status, 502)
        for path in ("/api/things?kind=tasks", f"/api/thing?kind=notes&id={note['id']}"):
            self.assertEqual(self.phone("GET", path)[0].status, 401, path)
        self.assertEqual(self.post("/api/keep", {"kind": "notes", "what": "save", "body": "Stranger"}, cookie=False)[0], 401)
        self.assertEqual(self.phone("POST", "/api/keep", {"kind": "notes", "what": "save", "body": "Forged"}, cookie=self.token)[0].status, 403)

    def test_a_mail_sent_for_later_and_the_automatic_replies(self):
        later = utc(dt.datetime.now() + dt.timedelta(days=1))
        order = {"mode": "new", "to": "a@example.com", "subject": "For tomorrow", "body": "x", "later": later}
        self.assertEqual(self.post("/api/send", order), (200, {"ok": True, "sent": True, "later": True}))
        self.assertIn("For tomorrow", [mail["subject"] for mail in self.get("/api/mail?folder=outbox")[1]["mail"]])
        self.assertNotIn("For tomorrow", [mail["subject"] for mail in self.get("/api/mail?folder=sent")[1]["mail"]])
        status, answer = self.post("/api/send", {**order, "later": utc(dt.datetime.now() - dt.timedelta(hours=1))})
        self.assertEqual(status, 400)
        self.assertIn("passed already", answer["error"])
        self.assertEqual(self.post("/api/arrange", {"what": "away", "on": True}), (200, {"ok": True, "away": True}))
        self.assertIs(self.get("/api/state")[1]["away"], True)
        self.assertEqual(self.post("/api/arrange", {"what": "away", "on": False}), (200, {"ok": True, "away": False}))

    def test_the_relay_passes_the_pages_and_never_repeats_a_change(self):
        for route in ("things", "thing", "keep"):
            self.assertIn(route, remote.ASK_ROUTES)
        self.assertIn("keep", remote.ONCE_ROUTES)
        self.assertNotIn("things", remote.ONCE_ROUTES)


if __name__ == "__main__":
    unittest.main()
