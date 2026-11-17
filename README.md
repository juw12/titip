# Titip

Read, answer and write your Outlook mail from your phone. Your own PC serves it.

*Titip* is Indonesian for leaving something with someone to pass on. You leave a message with your PC,
and Outlook on that PC sends it exactly as if you had typed it at your desk.

**Try it with invented mail:** <https://juw12.github.io/titip/>

![Titip on a phone: inbox, a message with a table, and the reply screen](docs/shots/phone.png)

## What it does

- Shows Inbox, Sent and Drafts from the classic Outlook desktop app, newest first, including mail that
  rules or archivers moved into `.pst` data files. Unread mail carries a red dot.
- Opens a message with its formatting in a frame that cannot run scripts or load remote pictures,
  or as plain text. Attachments download to the phone.
- Reply, Reply all, Forward and new mail. Outlook adds the earlier thread. (No automatic signature: type it, or keep it in a draft.)
- Every send waits 5 seconds behind an **Undo** button. **Save as draft** puts it in Outlook's Drafts.
  If a send fails, the message comes back with your text and the reason.
- Search by subject or person. Light and dark. Works as a home-screen app.

It stores no password and no mail. It drives the Outlook that is already signed in on your PC.

## What you need

- Windows with classic Outlook (the desktop app, not "new Outlook") open and signed in
- Python 3.10 or newer
- The phone on the same network as the PC, or [Tailscale](https://tailscale.com) on both

## Start

```powershell
git clone https://github.com/juw12/titip
cd titip
pip install -r requirements.txt
python -m titip
```

A page opens on the PC. Press **Show a QR code** and scan it with the phone's camera.

The first time, the phone warns that the connection is not private. That is expected: the PC made its
own certificate, so the link is encrypted but no authority vouches for it. Tap *Advanced*, then
*Proceed* (Safari: *Show Details*, then *visit this website*).

`scripts/install.ps1` adds a Desktop shortcut and starts Titip quietly when you sign in to Windows.
`scripts/install.ps1 -Remove` takes both away.

| Command | What it does |
| --- | --- |
| `python -m titip` | Serve your Outlook mail to paired phones |
| `python -m titip --demo` | Invented mail instead of Outlook, safe to show around |
| `python -m titip unpair` | Cut every paired phone off |
| `python -m titip --http` | No encryption on the phone link. Only for a network you trust. |

## How the phone is trusted

- The QR code holds a random code that works once, for five minutes. It sits in the part of the link
  that browsers never send to a server or write to a log.
- The phone trades it for a random device cookie (HttpOnly, SameSite=Strict, Secure). The PC keeps only
  a hash of it in `~/.titip/devices.json`.
- A phone without that cookie gets one page: "scan the QR code". No mail, no subject, no count.
- **Unpair all phones** on the PC page, or `python -m titip unpair`, cuts every phone off at once.
- The PC page that shows the QR code answers only on `127.0.0.1`.
- Nothing goes through another server. There is no account, no cloud relay, no analytics.

## Before you use it with work mail

Titip opens a door from your network to your mailbox. Read this first.

- Anyone holding a paired, unlocked phone can read and send your mail. Keep a screen lock on the phone.
- Check your employer's rules. Many companies do not allow mail on personal devices or extra services
  on a work PC. That call is yours and theirs, not this tool's.
- Windows Firewall may block the phone. If the phone cannot connect, allow Python on port 47779 for
  your private or domain network. That needs an administrator.
- The PC must be on, awake and running Outlook. Titip is a window onto that Outlook, not a mail server.
  If a tool on the PC lowers the priority of background programs, Titip puts itself back to normal
  (never higher), so the phone still gets an answer while the PC is busy.
- Outlook answers one program at a time. While it is busy with something else, the phone says so and
  keeps asking for up to a minute. Only reading is asked again. A send is never repeated.

## Limits

- Classic Outlook on Windows only.
- No attachments on mail you write from the phone (Forward keeps the original ones).
- Text you type is sent as plain paragraphs. A draft sent unchanged keeps its formatting; a draft you
  edit on the phone goes as plain text.
- Search looks at subject and sender (recipient in Sent), not the message body.
- Calendar invitations can be read, not accepted.
- Opening a message on the phone clears its red dot on that phone only. Outlook still shows it as unread.

## Develop

```powershell
python -m unittest discover -s tests -t .     # unit and server tests, no Outlook needed
python scripts/e2e.py                         # real browser against the demo mailbox (needs playwright)
python scripts/sync_docs.py                   # copy titip/web into docs/ for the public demo
```

The pairing code is adapted from [Cairn](https://github.com/juw12/cairn) by the same author.

MIT License.
