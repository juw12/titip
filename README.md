# Titip

Read, answer and write your Outlook mail from your phone. Your own PC serves it.
It can also answer the easy mail in your Gmail and keep the rest for you.

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

## Autopilot for Gmail

Titip can also look after a Gmail inbox on the same PC. It is off until you switch it on.

<img src="docs/shots/autopilot.png" alt="The Autopilot page on a phone: On, the two ways to answer, and an invoice waiting with a draft" width="320">

Every 10 minutes it looks at new mail in the Primary tab of the Gmail that Chrome is signed in to, and
for each mail does one of three things. All three are written down on the **Autopilot** page, live.

- **Answers it.** Only a short answer that promises nothing: thanks, "please send the details in
  writing", a simple question about your public work.
- **Keeps it for you.** Anything about money, a contract, a password or code, personal papers, or an
  answer that would name a date, a price, a link or an address. It waits under *Waiting for you* with
  a draft. You edit it and press Send, or press Don't answer.
- **Leaves it alone.** Newsletters, receipts, machines.

What it can never do is fixed in code, not in a prompt:

- answer anyone but the sender of the mail it just read
- send more than 8 answers a day per mailbox, or two to one conversation within 12 hours
- forward or delete mail, follow a link, open an attachment
- touch the mail that was already unread when you switched it on

Choose **Show me first** and nothing leaves without you. Your own answering rules go in
`autopilot_profile.md` in Titip's folder (`~/.titip`); without it a careful default is used.

How it works: Titip opens a narrow Chrome window at the edge of the screen, reads the mail through
Windows' accessibility interface and closes the window. It stores no Google password and uses no Google API.
The text of a mail is given to [OpenCode](https://opencode.ai) on your PC with every tool switched
off, so the model you run there reads it. If that model is a hosted one, the mail text goes to that host.

### Autopilot from any network

The Autopilot page (not your Outlook mail) can be opened from mobile data. For that the PC sends its
state to a small relay and takes your button presses from it. Both are encrypted on the PC and on the
phone with a key that only travels inside the QR code, after the `#` of the address, which a browser
never sends to a server. The relay holds ciphertext it cannot read.

You run the relay yourself, free, as a Cloudflare Worker:

```powershell
cd worker
npx wrangler kv namespace create RELAY      # put the id it prints into wrangler.toml
npx wrangler deploy                         # prints https://titip-relay.<you>.workers.dev
```

Put that address into `~/.titip/remote.json` as `{"relay": "https://titip-relay.<you>.workers.dev"}`,
start Titip again, and on the PC page press **Show the code** under *Autopilot from anywhere*. Scan it
once. **Make a new code** cuts off every phone that scanned the old one.

## What you need

- Windows with classic Outlook (the desktop app, not "new Outlook") open and signed in
- Python 3.10 or newer
- The phone on the same network as the PC, or [Tailscale](https://tailscale.com) on both
- For Autopilot: Google Chrome signed in to Gmail, and [OpenCode](https://opencode.ai) with a model

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
- Your Outlook mail never goes through another server. There is no account and no analytics.
  The one exception is yours to switch on: *Autopilot from any network* (Gmail only, encrypted, see above).

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

### Autopilot

- Windows and Chrome only. The PC must be on, unlocked and signed in to Gmail in Chrome's first profile.
- English Gmail. It finds Reply, Send and Mark as unread by their names.
- It reads the Primary tab only, at most 3 new mails per look, and nothing older than 2 days.
- A narrow Chrome window appears at the screen edge for a few seconds per look. Now and then it takes
  the keyboard for a moment and gives it back.
- Through the relay, a button you press reaches the PC within about a minute.
- The model can misjudge a mail. The rules above hold whatever it says, and Show me first removes the risk.

## Develop

```powershell
python -m unittest discover -s tests -t .     # unit and server tests, no Outlook needed
python scripts/e2e.py                         # real browser against the demo mailbox (needs playwright)
python scripts/sync_docs.py                   # copy titip/web into docs/ for the public demo
```

The pairing code is adapted from [Cairn](https://github.com/juw12/cairn) by the same author.

MIT License.
