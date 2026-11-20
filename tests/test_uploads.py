"""Files on their way from the phone into a mail: pieces in any order, in memory, bounded, taken once."""
import base64
import unittest

from titip import uploads
from titip.mailbox import MailError
from titip.uploads import FilesGone, Uploads

ONE, TWO = "upload-number-one", "upload-number-two"


def piece(identity, index, data, last=False, name="plan.pdf"):
    return {"upload": identity, "index": index, "last": last, "name": name, "data": base64.b64encode(data).decode("ascii")}


class Pieces(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.store = Uploads(clock=lambda: self.now)

    def test_pieces_in_any_order_make_the_file_and_it_is_handed_over_once(self):
        self.assertEqual(self.store.put(piece(ONE, 2, b"cc", last=True)), {"ok": True, "pieces": 1, "whole": False})
        self.store.put(piece(ONE, 0, b"aa"))
        self.assertTrue(self.store.put(piece(ONE, 1, b"bb"))["whole"])
        self.assertEqual(self.store.take([ONE]), [{"name": "plan.pdf", "data": b"aabbcc"}])
        with self.assertRaises(FilesGone):
            self.store.take([ONE])

    def test_the_same_piece_twice_counts_once(self):
        for _ in range(3):  # the relay, or a phone that woke up, hands it over again
            self.store.put(piece(ONE, 0, b"abc", last=True))
        self.assertEqual(self.store.take([ONE])[0]["data"], b"abc")

    def test_an_empty_file_is_a_file(self):
        self.store.put(piece(ONE, 0, b"", last=True, name="empty.txt"))
        self.assertEqual(self.store.take([ONE]), [{"name": "empty.txt", "data": b""}])

    def test_a_mail_that_names_a_file_with_a_piece_missing_is_not_sent_without_it(self):
        self.store.put(piece(ONE, 0, b"aa"))
        self.store.put(piece(ONE, 2, b"cc", last=True))
        self.store.put(piece(TWO, 0, b"whole", last=True))
        with self.assertRaises(FilesGone) as caught:
            self.store.take([TWO, ONE])
        self.assertIn("nothing was sent", str(caught.exception))
        self.assertIsInstance(caught.exception, MailError)
        with self.assertRaises(FilesGone):
            self.store.take(["never-uploaded-at-all"])

    def test_no_files_is_fine_and_a_mail_cannot_name_too_many(self):
        self.assertEqual(self.store.take(None), [])
        self.assertEqual(self.store.take([]), [])
        for bad in ("one-id-as-text", [ONE] * 21):
            with self.assertRaises(MailError):
                self.store.take(bad)

    def test_the_name_cannot_leave_its_folder(self):
        self.store.put(piece(ONE, 0, b"x", last=True, name="..\\..\\Windows\\win.ini"))
        self.assertEqual(self.store.take([ONE])[0]["name"], "win.ini")

    def test_what_is_not_a_piece_is_refused(self):
        for bad in (
            {**piece(ONE, 0, b"x"), "upload": "short"},
            {**piece(ONE, 0, b"x"), "upload": "../../../etc/passwd"},
            {**piece(ONE, 0, b"x"), "index": -1},
            {**piece(ONE, 0, b"x"), "index": uploads.MAX_PIECES},
            {**piece(ONE, 0, b"x"), "index": "first"},
            {**piece(ONE, 0, b"x"), "data": "not base64 !!"},
            {"upload": ONE},
        ):
            with self.assertRaises(MailError, msg=bad):
                self.store.put(bad)

    def test_a_file_and_all_that_waits_are_bounded(self):
        saved = uploads.MAX_PIECE, uploads.MAX_FILES_BYTES, uploads.MAX_WAITING_BYTES, uploads.MAX_WAITING
        self.addCleanup(lambda: [setattr(uploads, name, value) for name, value in zip(
            ("MAX_PIECE", "MAX_FILES_BYTES", "MAX_WAITING_BYTES", "MAX_WAITING"), saved)])
        uploads.MAX_PIECE, uploads.MAX_FILES_BYTES, uploads.MAX_WAITING_BYTES, uploads.MAX_WAITING = 4, 6, 9, 3
        with self.assertRaises(MailError):
            self.store.put(piece(ONE, 0, b"12345"))  # one piece too large
        self.store.put(piece(ONE, 0, b"1234"))
        with self.assertRaises(MailError):
            self.store.put(piece(ONE, 1, b"123"))  # the file too large: all of it is dropped
        self.assertFalse(self.store.active())
        self.store.put(piece(ONE, 0, b"1234"))
        self.store.put(piece(TWO, 0, b"1234"))
        with self.assertRaises(MailError) as caught:
            self.store.put(piece("upload-number-three", 0, b"12"))  # too much waiting altogether
        self.assertIn("Too many files", str(caught.exception))
        self.store.put(piece("upload-number-four", 0, b"1"))
        with self.assertRaises(MailError):
            self.store.put(piece("upload-number-five", 0, b""))  # too many files

    def test_a_file_no_mail_came_for_is_forgotten(self):
        self.store.put(piece(ONE, 0, b"x", last=True))
        self.assertTrue(self.store.active())
        self.now += uploads.ACTIVE_SECONDS + 1
        self.assertFalse(self.store.active(), "it no longer holds back a restart")
        self.now += uploads.KEEP_SECONDS
        with self.assertRaises(FilesGone):
            self.store.take([ONE])


if __name__ == "__main__":
    unittest.main()
