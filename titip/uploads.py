"""Files on their way from the phone into a mail.

A phone cannot hand a file over in one request: a question through the relay holds about 100 KB, a
request on your own network 1 MB. So a file comes in pieces, each a request of its own, in any
order, and rests here until the mail it belongs to is sent. Then titip.mailbox gets it as bytes.

In memory only. A file that no mail came for is dropped after KEEP_SECONDS, and all of them are
gone when Titip stops. The phone uploads again for every try to send, so nothing here is ever
needed twice.
"""

from __future__ import annotations

import base64
import binascii
import re
import threading
import time

from .mailbox import MAX_FILES, MAX_FILES_BYTES, MailError, file_name

ID = re.compile(r"[A-Za-z0-9_-]{12,40}")
MAX_PIECE = 700_000  # bytes of one piece
MAX_PIECES = 400
KEEP_SECONDS = 900
ACTIVE_SECONDS = 120  # a piece this new: a mail is on its way, so Titip does not restart for an update
MAX_WAITING_BYTES = 60_000_000  # all files that wait, of every phone together
MAX_WAITING = 80


class FilesGone(MailError):
    """A mail names a file that is not here (any more): Titip restarted, or the upload never finished."""

    def __init__(self):
        super().__init__("The attached files did not reach your PC, so nothing was sent. Send it again.")


class Uploads:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._files: dict[str, dict] = {}  # id -> {"name", "pieces": {number: bytes}, "last", "size", "at"}
        self._lock = threading.Lock()

    def _forget_old(self) -> None:  # call with _lock held
        now = self.clock()
        for identity in [identity for identity, file in self._files.items() if now - file["at"] > KEEP_SECONDS]:
            del self._files[identity]

    def put(self, params: dict) -> dict:
        """One piece: {"upload": id, "index": 0.., "last": true on the final piece, "name", "data": base64}."""
        identity = str(params.get("upload") or "")
        if not ID.fullmatch(identity):
            raise MailError("Bad request.")
        try:
            number = int(params.get("index", -1))
            piece = base64.b64decode(str(params.get("data") or ""), validate=True)
        except (TypeError, ValueError, binascii.Error):
            raise MailError("That piece of the file arrived damaged. Send it again.")
        if not 0 <= number < MAX_PIECES or len(piece) > MAX_PIECE:
            raise MailError("That file is too large to attach from the phone.")
        with self._lock:
            self._forget_old()
            file = self._files.get(identity)
            if file is None:
                if len(self._files) >= MAX_WAITING:
                    raise MailError("Too many files are waiting on your PC. Try again in a few minutes.")
                file = self._files[identity] = {"name": "", "pieces": {}, "last": None, "size": 0, "at": self.clock()}
            grown = file["size"] - len(file["pieces"].get(number, b"")) + len(piece)
            if grown > MAX_FILES_BYTES:
                del self._files[identity]
                raise MailError(f"A file larger than {MAX_FILES_BYTES // 1_000_000} MB cannot be attached from the phone.")
            waiting = sum(other["size"] for other in self._files.values()) - file["size"] + grown
            if waiting > MAX_WAITING_BYTES:
                del self._files[identity]
                raise MailError("Too many files are waiting on your PC. Try again in a few minutes.")
            file["pieces"][number] = piece
            file["size"] = grown
            file["at"] = self.clock()
            if params.get("name"):
                file["name"] = file_name(params.get("name"))
            if params.get("last") is True:
                file["last"] = number
            return {"ok": True, "pieces": len(file["pieces"]), "whole": self._whole(file)}

    @staticmethod
    def _whole(file: dict) -> bool:
        return file["last"] is not None and all(number in file["pieces"] for number in range(file["last"] + 1))

    def take(self, identities) -> list[dict]:
        """The files a mail names, as [{"name", "data"}], and they are forgotten here. FilesGone when one is not whole."""
        if not identities:
            return []
        if not isinstance(identities, list) or len(identities) > MAX_FILES:
            raise MailError(f"A mail from the phone can carry {MAX_FILES} files at most.")
        with self._lock:
            self._forget_old()
            found = [self._files.pop(str(identity), None) for identity in identities]
        if any(file is None or not self._whole(file) for file in found):
            raise FilesGone()
        return [
            {"name": file["name"] or "file", "data": b"".join(file["pieces"][number] for number in range(file["last"] + 1))}
            for file in found
        ]

    def active(self) -> bool:
        """Is a file arriving right now?"""
        now = self.clock()
        with self._lock:
            return any(now - file["at"] < ACTIVE_SECONDS for file in self._files.values())
