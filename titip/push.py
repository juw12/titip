"""Notices on the phone while Titip is closed: new mail and Outlook's reminders, as Web Push.

A page that is closed cannot ask the PC anything. What can reach a phone then is its browser's push
service (Apple's for an iPhone, Google's for Android and Chrome, Mozilla's for Firefox). The phone
asks its browser for an address at that service and a key pair, and hands the address and the public
key to this PC. For each notice the PC then

- seals the words for that one phone (RFC 8291, "aes128gcm"): the push service passes sealed bytes
  on and cannot read who wrote, or what about;
- proves that it is the PC the phone signed up with, with a key pair of its own (VAPID, RFC 8292);
- posts the sealed bytes to the address, through the same ways out of the network as the relay.

Nothing here listens: the PC only makes requests. The key pair and the phones' addresses are kept in
Titip's own folder (push-key.pem, push.json) and nowhere else.
"""

from __future__ import annotations

import json
import os
import struct
import threading
import time
import urllib.parse
from pathlib import Path

from . import remote
from .mailbox import MailError

PHONES_MOST = 8  # phones (or browsers) that are told; one more pushes the oldest out
NOTICES_MOST = 4  # notices of one look that are sent each on its own; more become one line
PUSH_EVERY = 90  # seconds between two looks at Outlook while a phone wants to be told
TTL = 4 * 3600  # how long the push service keeps a notice for a phone that is off
SUBJECT = "https://github.com/juw12/titip"  # whom a push service can ask about these requests (RFC 8292 wants one)
# Where a notice may be posted to: the push services of the browsers, and nothing else. The address
# comes from a phone, and the PC must not be made to post into its own network.
SERVICES = ("push.apple.com", "googleapis.com", "push.services.mozilla.com", "notify.windows.com")


class PushGone(Exception):
    """The push service says this phone does not want notices any more (it was reset, or switched them off)."""


def _keys():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return hashes, serialization, ec, decode_dss_signature, AESGCM, HKDF


def public_bytes(key) -> bytes:
    """The public half of a P-256 key the way Web Push writes it: 65 bytes, 0x04 and the two coordinates."""
    _, serialization, _, _, _, _ = _keys()
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def own_key(home: Path):
    """This PC's key pair for push, made once and kept in Titip's folder."""
    _, serialization, ec, _, _, _ = _keys()
    path = Path(home) / "push-key.pem"
    try:
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError):
        key = ec.generate_private_key(ec.SECP256R1())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return key


def proof(key, endpoint: str, now: float | None = None) -> str:
    """The Authorization header that says which PC this is: a short statement (for which service, until
    when, whom to ask), signed with the PC's key. The service checks it against the key the phone signed up with."""
    hashes, _, ec, decode_dss_signature, _, _ = _keys()
    address = urllib.parse.urlsplit(endpoint)
    said = {"aud": f"{address.scheme}://{address.netloc}", "exp": int(now if now is not None else time.time()) + 12 * 3600, "sub": SUBJECT}
    pack = lambda data: remote.b64(json.dumps(data, separators=(",", ":")).encode("ascii"))  # noqa: E731
    signed = pack({"typ": "JWT", "alg": "ES256"}) + "." + pack(said)
    r, s = decode_dss_signature(key.sign(signed.encode("ascii"), ec.ECDSA(hashes.SHA256())))
    token = signed + "." + remote.b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={token}, k={remote.b64(public_bytes(key))}"


def seal(plain: bytes, p256dh: bytes, auth: bytes, sender=None, salt: bytes | None = None) -> bytes:
    """`plain` sealed for the phone whose public key is `p256dh` and whose shared secret is `auth` (RFC 8291).

    A key pair made for this one message meets the phone's public key; from what the two share, the
    phone's secret and a random salt, the key and the nonce for AES-GCM are worked out. What comes
    back is the salt, the record size, the message's public key, and the sealed text: all the phone
    needs, and nothing the push service can open. `sender` and `salt` are given only by the tests."""
    hashes, _, ec, _, AESGCM, HKDF = _keys()
    sender = sender or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    sent_by = public_bytes(sender)
    shared = sender.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh))
    start = HKDF(hashes.SHA256(), 32, auth, b"WebPush: info\x00" + p256dh + sent_by).derive(shared)
    key = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(start)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(start)
    return salt + struct.pack(">I", 4096) + bytes([len(sent_by)]) + sent_by + AESGCM(key).encrypt(nonce, plain + b"\x02", None)


def clean_phone(payload) -> dict:
    """What a browser hands over when it signs up for notices, checked: an address at a push service and two keys."""
    payload = payload if isinstance(payload, dict) else {}
    keys = payload.get("keys") if isinstance(payload.get("keys"), dict) else {}
    endpoint = str(payload.get("endpoint") or "")
    address = urllib.parse.urlsplit(endpoint)
    host = (address.hostname or "").lower()
    if address.scheme != "https" or len(endpoint) > 2000 or not any(host == service or host.endswith("." + service) for service in SERVICES):
        raise MailError("That is not the address of a push service Titip knows.")
    try:
        p256dh, auth = remote.unb64(str(keys.get("p256dh") or "")), remote.unb64(str(keys.get("auth") or ""))
    except (ValueError, TypeError):
        raise MailError("The phone's keys for notices could not be read.")
    if len(p256dh) != 65 or p256dh[0] != 4 or len(auth) != 16:
        raise MailError("The phone's keys for notices could not be read.")
    return {"endpoint": endpoint, "p256dh": remote.b64(p256dh), "auth": remote.b64(auth)}


def as_text(notice: dict) -> bytes:
    """A notice as the phone's page unpacks it. Kept short: a push service takes about 4,000 bytes at most."""
    cut = lambda text, most: " ".join(str(text or "").split())[:most]  # noqa: E731
    return json.dumps({"title": cut(notice.get("title"), 80) or "Titip", "body": cut(notice.get("body"), 240),
                       "tag": cut(notice.get("tag"), 60), "url": str(notice.get("url") or "")[:300]},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class Push:
    """The phones that want to be told, and the telling."""

    def __init__(self, home: Path, mailbox=None, send=remote.http, every: float = PUSH_EVERY):
        self.home, self.mailbox, self.send, self.every = Path(home), mailbox, send, every
        self.path = self.home / "push.json"
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._stopped = False
        self._key = None
        self.last = ""  # what happened the last time something was sent, for the PC page and for "Send a test"
        try:
            self.phones = [clean_phone({"endpoint": one["endpoint"], "keys": one}) for one in json.loads(self.path.read_text(encoding="utf-8"))]
        except (OSError, ValueError, KeyError, TypeError, MailError):
            self.phones = []

    # ------------------------------------------------------------ the phones
    def key(self):
        if self._key is None:
            self._key = own_key(self.home)
        return self._key

    def public(self) -> str:
        """What a browser needs to sign up for notices from this PC and from nobody else."""
        return remote.b64(public_bytes(self.key()))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.phones, indent=1), encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def add(self, payload) -> dict:
        phone = clean_phone(payload)
        with self._lock:
            self.phones = [one for one in self.phones if one["endpoint"] != phone["endpoint"]][-(PHONES_MOST - 1):] + [phone]
            self._save()
        self.start()
        return phone

    def remove(self, endpoint: str) -> bool:
        with self._lock:
            kept = [one for one in self.phones if one["endpoint"] != str(endpoint or "")]
            gone = len(kept) != len(self.phones)
            if gone:
                self.phones = kept
                self._save()
        return gone

    def knows(self, endpoint: str) -> bool:
        with self._lock:
            return any(one["endpoint"] == str(endpoint or "") for one in self.phones)

    # ------------------------------------------------------------ the telling
    def tell_one(self, phone: dict, notice: dict) -> None:
        """One notice to one phone. PushGone when the phone signed off; OSError when it did not get out or was refused."""
        body = seal(as_text(notice), remote.unb64(phone["p256dh"]), remote.unb64(phone["auth"]))
        status, _ = self.send("POST", phone["endpoint"], body, {
            "Authorization": proof(self.key(), phone["endpoint"]), "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream", "TTL": str(TTL), "Urgency": "normal",
        })
        if status in (404, 410):
            raise PushGone()
        if not 200 <= status < 300:
            raise OSError(f"the push service answered {status}")

    def tell(self, notices: list[dict], only: str = "") -> dict:
        """Every notice to every phone (or to the one at `only`). What comes back: how many phones were reached, and why not."""
        with self._lock:
            phones = [one for one in self.phones if not only or one["endpoint"] == only]
        if len(notices) > NOTICES_MOST:  # a morning's worth of mail is one line, not a phone that will not stop buzzing
            mails = [one for one in notices if one.get("kind") == "mail"]
            rest = [one for one in notices if one.get("kind") != "mail"][:NOTICES_MOST - 1]
            if mails:
                rest.append({"title": f"{len(mails)} new mails", "body": ", ".join(dict.fromkeys(one.get("title", "") for one in mails))[:200],
                             "tag": "mail.many", "url": "#/inbox"})
            notices = rest
        reached, why = 0, ""
        for phone in phones:
            try:
                for notice in notices:
                    self.tell_one(phone, notice)
                reached += 1
            except PushGone:
                self.remove(phone["endpoint"])
                why = "The phone has switched notices off."
            except OSError as error:
                why = f"This PC could not reach the phone's push service ({str(error)[:120]})."
            except Exception as error:  # a key that does not fit, a library that is missing: said, never raised into the loop
                why = f"The notice could not be sealed ({type(error).__name__})."
        self.last = why or (f"Told {reached} phone{'s' if reached != 1 else ''} at {time.strftime('%H:%M')}." if phones else "")
        return {"phones": len(phones), "reached": reached, "why": why}

    # ------------------------------------------------------------ looking at Outlook while a phone wants to be told
    def start(self) -> None:
        with self._lock:
            if self._stopped or not self.phones or self.mailbox is None or (self._thread and self._thread.is_alive()):
                return
            self._thread = threading.Thread(target=self._run, daemon=True, name="titip-push")
            self._thread.start()

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()

    def look(self) -> dict | None:
        """One look: what is new in Outlook is told. A mailbox that cannot say (Outlook closed, busy) is asked again next time."""
        try:
            notices = self.mailbox.news()
        except Exception:
            return None
        return self.tell(notices) if notices else None

    def _run(self) -> None:
        while not self._stopped:
            with self._lock:
                wanted = bool(self.phones)
            if not wanted:
                return  # nobody to tell: the next phone that signs up starts it again
            self.look()
            self._wake.wait(self.every)
