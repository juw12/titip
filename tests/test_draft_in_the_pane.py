"""0.11.1: a draft that Outlook has open for writing in its reading pane.

A reply started in the reading pane, or a draft clicked in the list, is written right there, and while
it is Outlook refuses Send, Delete and Move on it from anywhere else: "This method can't be used with
an inline response mail item." Titip makes Outlook's window look at something else first, the way a
person would, and then does what the phone asked.
"""
import unittest

from tests.test_more_of_outlook import ID, App, Mail, outlook_with
from titip import mailbox
from titip.mailbox import MailError, OutlookMailbox, clean_act_request, clean_send_request

ENTRY = ID.partition(".")[2]
REFUSAL = "This method can't be used with an inline response mail item."


class Refused(Exception):
    """What pywin32 raises when Outlook says no."""

    hresult = -2147352567

    def __init__(self, text=REFUSAL):
        super().__init__(text)
        self.excepinfo = (0, "Microsoft Outlook", text, None, 0, -2147467259)


class Draft(Mail):
    """A draft that knows the window it is written in, and refuses what Outlook refuses while it is."""

    EntryID = ENTRY
    MessageClass = "IPM.Note"

    def __init__(self, body="As it was saved."):
        super().__init__()
        self.Body, self.window, self.typed = body, None, None

    def _held(self):
        return self.window is not None and self.window.inline is self

    def Send(self):
        if self._held():
            raise Refused()
        super().Send()

    def Delete(self):
        if self._held():
            raise Refused()
        self.calls.append("delete")

    def Move(self, folder):
        if self._held():
            raise Refused()
        self.calls.append("move")

    def Display(self):
        self.calls.append("display")
        if self.window.lets_go_by == "window":
            self.window.let_go()


class Folder:
    def __init__(self, name):
        self.EntryID = name


class Window:
    """One Outlook window. `lets_go_by`: what makes it stop writing the draft in its pane."""

    def __init__(self, draft, lets_go_by="selection", folder="DRAFTS"):
        self.inline, self.lets_go_by, self.did = draft, lets_go_by, []
        self.__dict__["folder"] = Folder(folder)
        if draft is not None:
            draft.window = self

    def let_go(self):
        if self.inline is not None and self.inline.typed is not None:
            self.inline.Body = self.inline.typed  # leaving the pane, Outlook saves what was typed there
        self.inline = None

    ActiveInlineResponse = property(lambda self: self.inline)

    def ClearSelection(self):
        self.did.append("nothing selected")
        if self.lets_go_by == "selection":
            self.let_go()

    @property
    def CurrentFolder(self):
        return self.__dict__["folder"]

    @CurrentFolder.setter
    def CurrentFolder(self, folder):
        self.did.append("shows " + folder.EntryID)
        self.__dict__["folder"] = folder
        if self.lets_go_by == "folder":
            self.let_go()


class Windows(list):
    Count = property(len)

    def Item(self, index):
        return self[index - 1]


class Namespace:
    def GetDefaultFolder(self, number):
        return Folder({mailbox.OL_SENT: "SENT", mailbox.OL_INBOX: "INBOX"}[number])

    def CompareEntryIDs(self, one, other):
        return one.upper() == other.upper()


def outlook_showing(draft, *windows):
    box = outlook_with(draft)
    box._app.Explorers = Windows(windows)
    box._ns = Namespace()
    return box


def order(**more):
    return clean_send_request({"mode": "draft", "id": ID, "to": "a@example.com", "subject": "Quote", "body": "As it was saved.", **more})


class InThePane(unittest.TestCase):
    def setUp(self):
        saved = mailbox.PANE_WAIT
        mailbox.PANE_WAIT = 0
        self.addCleanup(setattr, mailbox, "PANE_WAIT", saved)

    def test_as_it_was_the_draft_is_refused_and_the_phone_got_outlooks_own_words(self):
        draft = Draft()
        Window(draft)
        box = outlook_with(draft)  # no windows are looked at: what 0.11.0 did
        with self.assertRaises(Refused):
            box._send(order())

    def test_the_window_is_made_to_select_nothing_and_then_the_draft_leaves(self):
        draft = Draft()
        window = Window(draft)
        self.assertEqual(outlook_showing(draft, window)._send(order()), {"ok": True, "sent": True})
        self.assertEqual((draft.calls, window.did), (["send"], ["nothing selected"]), "the gentlest way was enough: the window still shows its folder")

    def test_where_that_is_not_enough_the_window_shows_another_folder_and_is_put_back(self):
        draft = Draft()
        window = Window(draft, lets_go_by="folder")
        outlook_showing(draft, window)._send(order())
        self.assertEqual(draft.calls, ["send"])
        self.assertEqual(window.did, ["nothing selected", "shows SENT", "shows DRAFTS"])

    def test_a_window_that_shows_sent_goes_to_the_inbox_instead(self):
        draft = Draft()
        window = Window(draft, lets_go_by="folder", folder="SENT")
        outlook_showing(draft, window)._send(order())
        self.assertEqual(window.did, ["nothing selected", "shows INBOX", "shows SENT"])

    def test_last_the_draft_is_opened_in_a_window_of_its_own(self):
        draft = Draft()
        window = Window(draft, lets_go_by="window")
        outlook_showing(draft, window)._send(order())
        self.assertEqual(draft.calls, ["display", "send"])
        self.assertEqual(window.did[-1], "shows DRAFTS", "and the folder it showed is shown again")

    def test_a_window_that_cannot_select_nothing_is_not_a_reason_to_give_up(self):
        draft = Draft()
        window = Window(draft, lets_go_by="folder")
        window.ClearSelection = lambda: 1 / 0  # a view that has no such thing
        outlook_showing(draft, window)._send(order())
        self.assertEqual(draft.calls, ["send"])

    def test_when_nothing_makes_outlook_let_go_the_phone_is_told_what_to_do_and_nothing_is_sent(self):
        draft = Draft()
        window = Window(draft, lets_go_by="a click on the PC")
        with self.assertRaises(MailError) as told:
            outlook_showing(draft, window)._send(order())
        self.assertEqual(str(told.exception), mailbox.IN_THE_PANE)
        self.assertIn("click another mail", str(told.exception))
        self.assertEqual((draft.calls, window.did[-1], draft.To), (["display"], "shows DRAFTS", ""), "the draft was not touched")

    def test_another_draft_in_the_pane_is_left_alone(self):
        draft, other = Draft(), Draft()
        other.EntryID = "FEDCBA9876543210"
        window, empty = Window(other), Window(None)
        outlook_showing(draft, empty, window)._send(order())
        self.assertEqual((draft.calls, window.did, empty.did, window.inline), (["send"], [], [], other))

    def test_two_windows_that_both_write_it_are_both_made_to_let_go(self):
        draft = Draft()
        first, second = Window(draft), Window(None, lets_go_by="folder")
        second.inline = draft
        draft._held = lambda: first.inline is draft or second.inline is draft
        outlook_showing(draft, first, second)._send(order())
        self.assertEqual((draft.calls, first.did, second.did[:2]), (["send"], ["nothing selected"], ["nothing selected", "shows SENT"]))

    def test_what_was_typed_in_the_pane_since_the_phone_read_the_draft_is_what_leaves(self):
        draft = Draft()
        draft.typed = "As it was saved. And one more line, typed on the PC."
        outlook_showing(draft, Window(draft))._send(order())
        self.assertEqual((draft.calls, draft.Body), (["send"], "As it was saved. And one more line, typed on the PC."))
        self.assertIn("signature", draft.HTMLBody, "text the phone did not change is not put over what Outlook saved on leaving the pane")

    def test_text_changed_on_the_phone_is_what_leaves(self):
        draft = Draft()
        draft.typed = "As it was saved. And one more line, typed on the PC."
        outlook_showing(draft, Window(draft))._send(order(body="Changed on the phone."))
        self.assertIn("Changed on the phone.", draft.HTMLBody)
        self.assertNotIn("signature", draft.HTMLBody)

    def test_a_draft_saved_from_the_phone_and_a_new_mail_do_not_look_at_the_windows_for_nothing(self):
        draft = Draft()
        window = Window(draft)
        box = outlook_showing(draft, window)
        self.assertEqual(box._send(order(action="draft")), {"ok": True, "saved": True})
        self.assertEqual(window.did, ["nothing selected"], "a draft in the pane is taken out before it is changed, too")
        other = Window(Draft())
        box = outlook_showing(None, other)
        box._send(clean_send_request({"mode": "new", "to": "a@example.com", "body": "x"}))
        self.assertEqual(other.did, [])

    def test_a_draft_in_the_pane_is_deleted_and_moved_the_same_way(self):
        for what, did in (("delete", "delete"), ("move", "move")):
            draft = Draft()
            window = Window(draft, lets_go_by="folder")
            box = outlook_showing(draft, window)
            box._in_the_bin = lambda item: False
            box._folder = lambda folder_id: Folder("ARCHIVE")
            box._act(clean_act_request({"what": what, "id": ID, "to": "f.0123456789.ABCDEF0123456789AB"}))
            self.assertEqual((draft.calls, window.did), ([did], ["nothing selected", "shows SENT", "shows DRAFTS"]), what)

    def test_a_flag_does_not_take_anything_out_of_the_pane(self):
        draft = Draft()
        window = Window(draft)
        draft.MarkAsTask = lambda kind: draft.calls.append("flag")
        outlook_showing(draft, window)._act(clean_act_request({"what": "flag", "id": ID}))
        self.assertEqual((draft.calls, window.did), (["flag", "save"], []))

    def test_an_outlook_without_a_window_or_without_such_a_pane_sends_as_before(self):
        draft = Draft()
        box = outlook_with(draft)
        box._ns = Namespace()
        self.assertEqual(box._send(order()), {"ok": True, "sent": True})  # the stand-in App has no Explorers at all
        old = type("Old", (), {})()  # an Outlook from before 2013: a window, but no ActiveInlineResponse
        self.assertEqual(outlook_showing(Draft(), old)._send(order()), {"ok": True, "sent": True})

    def test_a_busy_outlook_is_busy_not_a_window_without_a_pane(self):
        busy = type("Busy", (Exception,), {"hresult": mailbox.RPC_BUSY[0]})

        class Stuck:
            @property
            def ActiveInlineResponse(self):
                raise busy()

        with self.assertRaises(busy):
            outlook_showing(Draft(), Stuck())._send(order())

    def test_putting_the_window_back_never_turns_a_mail_that_left_into_an_error(self):
        draft = Draft()
        window = Window(draft, lets_go_by="folder")
        real = Window.CurrentFolder.fset

        def once(self, folder):
            if folder.EntryID == "DRAFTS":
                raise Refused("The folder cannot be shown.")
            real(self, folder)

        Stubborn = type("Stubborn", (Window,), {"CurrentFolder": property(Window.CurrentFolder.fget, once)})
        window.__class__ = Stubborn
        self.assertEqual(outlook_showing(draft, window)._send(order()), {"ok": True, "sent": True})

    def test_outlooks_own_words_reach_the_phone_as_what_to_do(self):
        box = OutlookMailbox(read_ahead=False)
        box._connect = lambda: None

        def step():
            raise Refused()

        with self.assertRaises(MailError) as told:
            box._attempt(step, (), Refused, pause=self.fail)
        self.assertEqual(str(told.exception), mailbox.IN_THE_PANE)
        with self.assertRaises(MailError) as other:
            box._attempt(lambda: (_ for _ in ()).throw(Refused("The attachment is too large.")), (), Refused, pause=self.fail)
        self.assertEqual(str(other.exception), "The attachment is too large.")


if __name__ == "__main__":
    unittest.main()
