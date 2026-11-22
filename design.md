# Titip design

One direction, given by the person who uses it: **Outlook's layout in Cairn's hand.** Titip is laid out
the way Outlook's phone app is, because that is where the mail lives and where the hands already know
their way. It is drawn the way Cairn is, a field notebook and not a dashboard, because Titip sits next
to Cairn on the same phone and the two should read as one hand.

Assumed: a single direction, no four-direction bracket. The app has one job and one user.

## What comes from where

| From Outlook (the layout) | From Cairn (the hand) |
| --- | --- |
| A coloured bar with the folder's name, search, refresh | Warm paper and ink instead of white and black |
| A round initial for each sender | Hairlines instead of boxes, radius 6 |
| Mail under Today, Yesterday, Last 7 days | Three typefaces: a serif for names of things, a plain face for text, a mono for small facts |
| Folders at the thumb, a New mail button above them | One signal colour, trail orange, and nothing else that shouts |
| Unread marked on the left edge, in bold | Counts and dates set small, in mono, in capitals where they are labels |
| Recipients as labels, names suggested under To and Cc | The eight soft colours of Cairn's note types for the initials |
| Every folder under the folder's name; a calendar by day | The hours of a meeting in mono, where a mail has its initials |

No Microsoft mark, name or icon is used anywhere.

## Palette

| Token | Light | Dark | Used for |
| --- | --- | --- | --- |
| ground | `#f4f1ea` | `#151412` | day labels, the search strip, the pages of words (PC page, Autopilot) |
| paper | `#fffdf8` | `#1c1b18` | the list and the letter |
| card | `#fffdf8` | `#201f1c` | buttons, fields |
| ink | `#1b1a17` | `#efebe3` | text |
| ink-2 | `#4a463e` | `#c8c2b6` | notes on the ground, small text on the open row |
| muted | `#6c665d` | `#968f83` | previews, times, labels |
| line / line-strong | `#e1dbcf` / `#cbc3b3` | `#2e2c28` / `#403d37` | hairlines / the edge of a field |
| head | `#2f5b8c` | `#201f1c` | the bar at the top |
| accent, link | `#2f5b8c` | `#86aee0` | the one filled button per screen, buttons made of words |
| signal | `#d9481f` (as text `#a8340f`) | `#f0764d` | unread edge and time, the tab you are on, the field you type in, the focus ring, the drafts count |
| stamp | `#b3261e` | `#f2867d` | errors, [Draft] |
| good | `#2e7566` | `#6fc2ad` | on, answered |
| pick | `#dfe7f1` | `#1d2836` | the open row, a recipient's label |

The blue is Cairn's "task" blue, standing where Outlook's blue stands. Orange is the only signal: if
something is orange, it is new, or it is where you are.

Initials: accent, idea, decision, journal, session, task, feedback, note, each as Cairn's soft ground
with its own ink. Four of the inks are a step darker than Cairn's so 15 px initials reach 4.5:1.

Every text pair passes WCAG AA in both themes. Lowest: muted on the open row 4.56:1 light, 4.65:1 dark
(the open row uses ink-2 for its small text, so this pair is not shown), muted on ground 5.04:1,
initials 4.62:1.

## Type

| Face | Used for | Sizes |
| --- | --- | --- |
| Instrument Serif 400 | the folder's name in the bar, a letter's subject, page headings, On / Paused | 22 to 36 px |
| Geist 400 to 700 | everything you read or press | 13 to 16 px, body 16, line height 1.45 to 1.6 |
| Geist Mono 400 to 600 | day labels, times, counts, field names, the version line | 12 px, labels in capitals, letter-spacing .06em |

The serif has one weight; it is never made bold. Subjects in the list stay in Geist: a display serif
at 15 px is hard to read down a long list. The three files ship with Titip (see
`THIRD_PARTY_NOTICES.md`); no page asks another server for a font. Until they arrive the text shows in
the system face.

## Space and shape

- Spacing steps 4, 8, 12, 16, 24, 32. Radius 6 on everything that is not a round initial. Tap targets
  48 px, which is what Android asks for and more than the 44 an iPhone does (a recipient's label is
  32 px to look at and 48 to tap: the rest is a border nobody sees). A list to pick from is drawn
  by the page, with its own small arrow, so that it is 48 px high in every browser. A name in a line of text that can be pressed keeps the line one
  line high and is 48 px to the finger.
- What a change costs is said on the sheet before it is saved, in the line under the bar, not in
  a question afterwards: new hours for a series say that single changed times are lost.
- The days of the week are seven equal buttons, Monday first, two letters each, each a finger
  wide: one line where they fit, two where they do not. The ones picked are pressed like an
  answer to an invitation. The day the entry starts on is pressed and
  cannot be let go.
- A category's colour is a 12 px dot before its name; one without a colour has a ring. Eight
  colours, picked to be told apart on both grounds, never the only sign: the name is next to it.
- Two boxes of text on one sheet each have their name over them, small and in mono like a field's
  label, with a line between them. One box alone needs none: the sheet's title says what it is.
- A shadow only under what floats: the New mail button, the notice, the list of names, and the
  composer on a wide screen.

## Signature

The orange edge. A 3 px line of trail orange on the left of every unread mail, the same line on the
left of the envelope in the icon, and a short one over the tab you are on. Nowhere else as decoration.

## Voice

Plain words, said once. Say where mail leaves from, who it goes to and what happened: "Sent to Mateo Silva",
"Not sent. ... Your text is still here.", "Saved to Drafts". No exclamation marks, no jargon.

## States

- Unread: orange edge, bold sender and subject, orange time. Read mail steps back to regular weight.
- The open row (wide screens) is washed blue; its small text steps up to ink-2.
- Notices sit above the New mail button, never on it.
- Send waits 3 seconds behind Undo. A failed send reopens the message with the text, the files and the reason.
  Delete and an answer to an invitation wait the same 3 seconds. Delete is a word in stamp red at
  the top of the letter, never an icon alone.
- The folders used most stay at the thumb: Inbox, Sent, Drafts, Calendar, Autopilot. The folder's
  name in the bar opens the list of all of them; the one you are in has the orange edge.
- An invitation nobody answered yet is new: orange edge, bold, the word Invited in orange. Answered,
  it steps back. The answer you gave is the pressed one of three plain buttons.
- A file that goes along is a label with a paper clip, its size in mono, and a cross: a tap takes it out.
- A mail in the Outbox has not left: one line says so over every list, with the way there.
- More sits next to Delete, as a word. What it lists is done at once (flag, unread, categories); what
  takes the mail away (move) waits behind Undo like Delete. A flag is the small orange flag in the
  list, importance a red exclamation mark: the two marks Outlook has, in the two colours Titip has.
- A list to pick from (folders to move to, categories to tick) rises from the bottom over a dimmed page.
- A calendar entry is written on the same sheet as a mail, with the same labels for people.
- Formatting is four small keys over the text (B, I, U, List) that write marks; the text field stays
  a text field. No toolbar of colours and fonts.
- What changes the mailbox itself (a folder made, renamed, taken out; rules) sits under the list of
  folders, as words in link blue. A name is typed into the same sheet a list is picked from.
- The calendar says whose it is in one line over the days, with the way to another one.
- Tasks, notes and contacts are lists like the mail: a task and a note without initials, the name
  and one line under it; a contact with its initials. A task that is overdue has the orange edge,
  one that is done is struck through. They open in the letter, and are written on the same sheet
  as a mail. The button at the thumb says what it makes: New task, New note, New contact.
- What is switched (automatic replies, reminders on this phone, open or done tasks) is one line
  over the list: the fact on the left, the way to change it on the right, as words in link blue.
- A row pushed aside shows what will happen in a word behind it, on red for Delete and on the
  bar's blue for read or unread: dim until it is far enough to count, then full. Never an icon alone.
- What narrows a list (All, Unread, Flagged) is three plain buttons under the search box, the one
  in use pressed like an answer to an invitation.
- A person's card rises from the bottom like every list to pick from: the name, one line of facts,
  and what can be done (write, call) as rows.
- Several mails picked at once are washed blue with a blue edge; what can be done with them is one
  line over the list, as words. In Deleted Items the word is "Delete for good", never just Delete.
- A notice on the phone is the sender as its title and the subject as its text, as a mail app
  shows it; a reminder says Reminder, then what and when. No sound of Titip's own, no badge count.
- What cannot be undone asks once more, in a sheet whose only row says what will happen.
- Send later is a button under the text like High importance; pressed, it shows a day and a time
  over the buttons, and the button at the top says Send later.
- PC out of reach: the list from memory if there is one, and one sentence that names the cause (see
  "When the phone cannot reach your PC" in the README). Otherwise "Can't show your mail", the reason,
  and Try again.
- Autopilot: one lamp and one word, On or Paused, then one sentence of facts (which mailbox, last look,
  answers today). The primary button is Switch on while paused; while on, the only primary button is
  Send on a waiting mail. Each line of *What it did* starts with what happened: Answered (green),
  Kept for you (red), Left alone, Not answered, Problem, You changed. An answer that went out is
  shown in full, as a quote.
- Autopilot through the relay says so, says when the PC last wrote, and shows a switch you pressed at
  once while the PC picks it up.
- Mail through the relay looks and reads like mail on your own network. The differences are said in
  words only when they matter.
- The PC page says each switch as a fact first, then the detail: "Outlook mail from anywhere: on. A
  phone last asked at 14:02." and "Titip 0.8.0 (d1c68d0). Up to date. Last looked at 14:05."
  Switching mail from anywhere on asks once; switching it off does not.

## Banned

Gradients as decoration, purple, violet and magenta, glass, emoji, cards inside cards, centered hero
text, a second signal colour, pills, bold serif, icons without a label next to a destructive action.
