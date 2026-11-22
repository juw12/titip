"""0.13.0, against a stand-in Outlook: deleted for good, a folder emptied, one time of a meeting that repeats
answered, what the automatic reply says, a link, a title and a marked passage in the text, a picture inside it."""
import json
import unittest
from pathlib import Path

from tests import test_last_of_outlook, test_server
from tests.test_more_of_outlook import ID, Attachment, Attachments, Mail, Message, at, outlook_with
from titip import mailbox, remote
from titip.mailbox import (
    DemoMailbox, MailError, OutlookMailbox, clean_act_request, clean_arrange_request, clean_send_request, text_to_html,
)


class MoreInTheText(unittest.TestCase):
    def test_a_link_a_title_and_a_marked_passage(self):
        made = text_to_html("# Plan for Friday\nSee [the drawing](https://example.com/d?a=1&b=2) and ==bring both units==.", True)
        self.assertIn('<b style="font-size:14pt">Plan for Friday</b><br>', made)
        self.assertIn('<a href="https://example.com/d?a=1&amp;b=2">the drawing</a>', made)
        self.assertIn('<span style="background:#fff2a8">bring both units</span>', made)

    def test_only_a_web_address_becomes_a_link_and_nothing_typed_becomes_markup(self):
        for unsafe in ("[x](javascript:alert(1))", "[x](data:text/html,hi)", "[x](https://a.example/\"onmouseover=\"y)", "[<b>x</b>](ftp://a)"):
            made = text_to_html(unsafe, True)
            self.assertNotIn("<a ", made.replace('<a href="https://a.example/&quot;onmouseover=&quot;y">', ""), unsafe)
            self.assertNotIn("<b>x", made)
        self.assertIn('href="https://a.example/&quot;onmouseover=&quot;y"', text_to_html('[x](https://a.example/"onmouseover="y)', True),
                      "a quote in the address stays inside the address")

    def test_signs_that_are_not_marks_stay_and_without_the_keys_nothing_is_formatted(self):
        self.assertIn(">a == b == c<", text_to_html("a == b == c", True))
        self.assertIn(">#hashtag and C# stay<", text_to_html("#hashtag and C# stay", True))
        plain = text_to_html("# Title\n[x](https://example.com) ==y==", False)
        self.assertNotIn("<a ", plain)
        self.assertIn("# Title<br>[x](https://example.com) ==y==", plain)


class Picture(Attachment):
    def __init__(self, name, data=b"", refuses=False):
        super().__init__(name, data)
        self.called, outer = None, self

        class Accessor:
            def SetProperty(self, name, value):
                if refuses:
                    raise RuntimeError("this store does not take it")
                outer.called = (name, value)

        self.PropertyAccessor = Accessor()


class Holding(Attachments):
    refuses = False

    def Add(self, path):
        self.append(Picture(Path(path).name, Path(path).read_bytes(), self.refuses))
        return self[-1]


class APictureInTheText(unittest.TestCase):
    def send(self, body, files, refuses=False):
        box = outlook_with()
        holding = Holding()
        holding.refuses = refuses

        def create(kind):
            mail = Mail()
            mail.Attachments = holding
            box._app.created.append(mail)
            return mail

        box._app.CreateItem = create
        box._send(clean_send_request({"mode": "new", "to": "a@example.com", "body": body, "files": files}))
        return box._app.created[0]

    def test_the_line_that_names_it_is_where_it_shows(self):
        mail = self.send("Look at the cam:\n[picture: cam & hinge.jpg]\nAnd the sheet is attached.",
                         [{"name": "cam & hinge.jpg", "data": b"JPG"}, {"name": "sheet.pdf", "data": b"PDF"}])
        picture, sheet = mail.Attachments
        name, called = picture.called
        self.assertEqual(name, mailbox.P_CONTENT_ID)
        self.assertIn(f'<img src="cid:{called}" alt="cam &amp; hinge.jpg" style="max-width:100%">', mail.HTMLBody)
        self.assertNotIn("[picture:", mail.HTMLBody)
        self.assertIsNone(sheet.called)
        self.assertEqual(mail.calls, ["send"])

    def test_a_file_that_is_no_picture_or_has_no_line_goes_along_as_a_file(self):
        mail = self.send("[picture: sheet.pdf]\nThe photo is attached.", [{"name": "sheet.pdf", "data": b"PDF"}, {"name": "cam.jpg", "data": b"JPG"}])
        self.assertEqual([one.called for one in mail.Attachments], [None, None])
        self.assertIn("[picture: sheet.pdf]", mail.HTMLBody)

    def test_two_pictures_of_the_same_name_each_get_their_own_line(self):
        mail = self.send("[picture: image.jpg]\ntext\n[picture: image.jpg]", [{"name": "image.jpg", "data": b"1"}, {"name": "image.jpg", "data": b"2"}])
        first, second = (one.called[1] for one in mail.Attachments)
        self.assertNotEqual(first, second)
        self.assertLess(mail.HTMLBody.index(first), mail.HTMLBody.index(second))

    def test_a_store_that_does_not_take_it_leaves_the_line_and_the_file(self):
        mail = self.send("[picture: cam.jpg]", [{"name": "cam.jpg", "data": b"JPG"}], refuses=True)
        self.assertIn("[picture: cam.jpg]", mail.HTMLBody)
        self.assertEqual((len(mail.Attachments), mail.calls), (1, ["send"]))


class Binned(Message):
    def __init__(self):
        super().__init__()
        self.calls = []

    def Delete(self):
        self.calls.append("delete")


class ForGood(unittest.TestCase):
    def box(self, item, in_the_bin):
        box = OutlookMailbox(read_ahead=False)
        box._item = lambda mail_id: item
        box._in_the_bin = lambda found: in_the_bin
        box._app = type("App", (), {})()
        return box

    def test_only_what_was_deleted_before_is_deleted_for_good(self):
        item = Binned()
        self.assertEqual(self.box(item, True)._act(clean_act_request({"what": "erase", "id": ID})), {"ok": True, "erased": True})
        self.assertEqual(item.calls, ["delete"])
        kept = Binned()
        with self.assertRaises(MailError) as refused:
            self.box(kept, False)._act(clean_act_request({"what": "erase", "id": ID}))
        self.assertIn("Delete it first", str(refused.exception))
        self.assertEqual(kept.calls, [])
        with self.assertRaises(MailError) as twice:
            self.box(kept, True)._act(clean_act_request({"what": "delete", "id": ID}))
        self.assertIn("Delete for good", str(twice.exception))
        self.assertEqual(kept.calls, [], "Delete never removes for good by accident")


class Things:
    """A folder's mails or folders: counted, and taken by their place, as Outlook hands them out."""

    def __init__(self, count, log, word):
        self.left, self.log, self.word = count, log, word

    Count = property(lambda self: self.left)

    def Item(self, index):
        outer = self

        class One:
            def Delete(self):
                assert index == outer.left, "from the end, so what is left keeps its place"
                outer.left -= 1
                outer.log.append(outer.word)

        return One()


class Emptied(unittest.TestCase):
    def box(self, mails, folders=0):
        log = []
        folder = type("Folder", (), {"Items": Things(mails, log, "mail"), "Folders": Things(folders, log, "folder")})()
        box = OutlookMailbox(read_ahead=False)
        asked = []
        box._ns = type("NS", (), {"GetDefaultFolder": lambda ns, number: asked.append(number) or folder})()
        return box, log, asked

    def test_deleted_items_lose_their_folders_and_mails_for_good_a_few_hundred_at_a_time(self):
        box, log, asked = self.box(mailbox.EMPTY_MOST + 40, folders=2)
        result = box._arrange(clean_arrange_request({"what": "empty", "folder": "deleted"}))
        self.assertEqual((result, asked), ({"ok": True, "emptied": mailbox.EMPTY_MOST, "left": 42}, [mailbox.OL_DELETED]))
        self.assertEqual((log[:2], log.count("mail")), (["folder", "folder"], mailbox.EMPTY_MOST - 2))
        self.assertEqual(box._arrange(clean_arrange_request({"what": "empty", "folder": "deleted"})), {"ok": True, "emptied": 42, "left": 0})

    def test_junk_is_moved_out_and_no_other_folder_is_ever_emptied(self):
        box, log, asked = self.box(3, folders=5)
        self.assertEqual(box._arrange(clean_arrange_request({"what": "empty", "folder": "junk"})), {"ok": True, "emptied": 3, "left": 0})
        self.assertEqual((log, asked), (["mail"] * 3, [mailbox.OL_JUNK]))
        for other in ("inbox", "sent", "drafts", "f.0123456789.ABCDEF0123456789AB", "", None):
            with self.assertRaises(MailError):
                clean_arrange_request({"what": "empty", "folder": other})


class OneTimeOfASeries(unittest.TestCase):
    def test_the_answer_goes_for_that_time_and_the_series_is_not_answered(self):
        series = test_last_of_outlook.series_of(at(23, 9, 30), at(24, 9, 30), meeting=3)
        box = OutlookMailbox(read_ahead=False)
        box._item = lambda event_id: series
        box._app = type("App", (), {})()
        when = at(24, 9, 30).astimezone().astimezone(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        box._act(clean_act_request({"what": "respond", "id": ID, "answer": "decline", "occurrence": when, "note": "Not this week."}))
        self.assertEqual((series.answered, series.times[0].answered, series.times[1].answered), ([], [], [(4, True)]))
        self.assertEqual((series.times[1].reply.Body, series.times[1].reply.calls), ("Not this week.", ["send"]))
        box._act(clean_act_request({"what": "respond", "id": ID, "answer": "accept"}))
        self.assertEqual(series.answered, [(3, True)], "without a time named, the whole series is answered, as before")
        with self.assertRaises(MailError):
            clean_act_request({"what": "respond", "id": ID, "answer": "accept", "occurrence": "next week"})


class Storage:
    def __init__(self, text="", keeps=True):
        self.Body, self.keeps, self.saved = text, keeps, None

    def Save(self):
        self.saved = self.Body


class WhatTheAutomaticReplySays(unittest.TestCase):
    def box(self, text="", keeps=True, has_one=True):
        box = OutlookMailbox(read_ahead=False)
        kept = Storage(text, keeps)
        asked = []

        class Inbox:
            def GetStorage(self, name, how):
                asked.append((name, how))
                if not has_one:
                    raise RuntimeError("this store has no hidden notes")
                fresh = Storage(kept.saved if kept.saved is not None and kept.keeps else text)
                fresh.Save = lambda: setattr(kept, "saved", fresh.Body)
                return fresh

        box._ns = type("NS", (), {"GetDefaultFolder": lambda ns, number: Inbox(),
                                   "DefaultStore": type("S", (), {"PropertyAccessor": type("A", (), {"GetProperty": lambda a, n: True})()})()})()
        return box, kept, asked

    def test_it_is_read_from_the_hidden_note_in_the_inbox(self):
        box, _, asked = self.box("Back on Monday.\r\nFor the lab call Jonas.\r\n")
        self.assertEqual(box._away_now(), {"on": True, "text": "Back on Monday.\nFor the lab call Jonas.", "outside": "Back on Monday.\nFor the lab call Jonas."})
        self.assertEqual(asked, [("IPM.Note.Rules.OofTemplate.Microsoft", 2), ("IPM.Note.Rules.ExternalOofTemplate.Microsoft", 2)])
        box, _, _ = self.box(has_one=False)
        self.assertEqual(box._away_now(), {"on": True, "text": None, "outside": None}, "a mailbox without one: the phone does not offer to change it")

    def test_it_is_written_and_read_back(self):
        box, kept, _ = self.box("Old text.")
        result = box._arrange(clean_arrange_request({"what": "awayText", "text": "  Back on 1 December.\r\nFor the lab call Jonas.  "}))
        self.assertEqual((result, kept.saved), ({"ok": True, "text": "Back on 1 December.\nFor the lab call Jonas."}, "Back on 1 December.\nFor the lab call Jonas."))

    def test_a_text_outlook_does_not_keep_is_not_reported_as_changed_and_an_empty_one_is_refused(self):
        box, _, _ = self.box("Old text.", keeps=False)
        with self.assertRaises(MailError) as refused:
            box._arrange(clean_arrange_request({"what": "awayText", "text": "New text."}))
        self.assertIn("did not keep the text", str(refused.exception))
        with self.assertRaises(MailError):
            clean_arrange_request({"what": "awayText", "text": "  \n "})
        self.assertEqual(len(clean_arrange_request({"what": "awayText", "text": "x" * 9000})["text"]), mailbox.AWAY_TEXT_MOST)


class Demo(unittest.TestCase):
    def test_the_same_in_the_invented_mailbox(self):
        box = DemoMailbox()
        first = box.list("inbox")["mail"][0]["id"]
        with self.assertRaises(MailError):
            box.act({"what": "erase", "id": first})
        box.act({"what": "delete", "id": first})
        before = len(box.list("deleted", 100)["mail"])
        self.assertEqual(box.act({"what": "erase", "id": first}), {"ok": True, "erased": True})
        self.assertEqual(len(box.list("deleted", 100)["mail"]), before - 1)
        with self.assertRaises(MailError):
            box.get(first)
        second = box.list("inbox")["mail"][0]["id"]
        box.act({"what": "move", "id": second, "to": "junk"})
        self.assertEqual(box.arrange({"what": "empty", "folder": "junk"}), {"ok": True, "emptied": 1, "left": 0})
        self.assertEqual(box.list("deleted", 100)["mail"][0]["id"], second, "out of Junk it lands in Deleted Items")
        emptied = box.arrange({"what": "empty", "folder": "deleted"})
        self.assertEqual((emptied["left"], box.list("deleted")["mail"]), (0, []))
        self.assertIn("out of the office", box.away()["text"])
        box.arrange({"what": "awayText", "text": "Back on Monday."})
        self.assertEqual(box.away(), {"on": False, "text": "Back on Monday.", "outside": ""})


class OverTheWire(test_server.ServerCase):
    def setUp(self):
        self.token = self.pair()[0]

    def get(self, path, cookie=True):
        reply, data = self.phone("GET", path, cookie=self.token if cookie else None)
        return reply.status, json.loads(data) if reply.status == 200 else None

    def post(self, path, body, cookie=True):
        reply, data = self.phone("POST", path, body, test_server.X, self.token if cookie else None)
        return reply.status, json.loads(data)

    def test_the_reply_text_for_good_and_empty_for_a_paired_phone_only(self):
        status, state = self.get("/api/away")
        self.assertEqual((status, state["on"]), (200, False))
        self.assertEqual(self.app.mail_call("away", {}), (200, state))  # what the relay is answered with
        self.assertEqual(self.post("/api/arrange", {"what": "awayText", "text": "Back on Monday."}), (200, {"ok": True, "text": "Back on Monday."}))
        self.assertEqual(self.get("/api/away")[1]["text"], "Back on Monday.")
        self.assertEqual(self.post("/api/arrange", {"what": "awayText", "text": " "})[0], 400)
        mail = self.get("/api/mail?folder=inbox")[1]["mail"][-1]["id"]
        status, answer = self.post("/api/act", {"what": "erase", "id": mail})
        self.assertEqual(status, 400)
        self.assertIn("Delete it first", answer["error"])
        self.assertEqual(self.post("/api/act", {"what": "delete", "id": mail})[0], 200)
        self.assertEqual(self.post("/api/act", {"what": "erase", "id": mail}), (200, {"ok": True, "erased": True}))
        self.assertEqual(self.post("/api/arrange", {"what": "empty", "folder": "inbox"})[0], 400)
        self.assertEqual(self.post("/api/arrange", {"what": "empty", "folder": "junk"})[1]["left"], 0)
        self.assertEqual(self.get("/api/away", cookie=False)[0], 401)
        self.assertEqual(self.post("/api/act", {"what": "erase", "id": mail}, cookie=False)[0], 401)
        self.assertEqual(self.post("/api/arrange", {"what": "empty", "folder": "deleted"}, cookie=False)[0], 401)
        self.assertIn("away", remote.ASK_ROUTES)
        self.assertNotIn("away", remote.ONCE_ROUTES)


if __name__ == "__main__":
    unittest.main()
