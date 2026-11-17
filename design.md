# Titip design

One direction, chosen before the first screen was built: a letter on your desk. Titip hands over mail that
already lives in Outlook, so it should feel like paper and ink, not like a dashboard.

Assumed: a single direction, no four-direction bracket. The app has one job and one user.

## Palette

| Token | Light | Dark | Used for |
| --- | --- | --- | --- |
| paper | `#f4efe6` | `#12171d` | page ground |
| card | `#fffdf8` | `#1a2129` | fields, buttons, the composer |
| ink | `#16202b` | `#eef0f2` | text |
| muted | `#55606c` | `#a6b0bb` | previews, times, labels |
| line | `#d9cfbd` | `#313c47` | hairlines |
| accent (airmail blue) | `#1d4f91` | `#2c69b8`, links `#9cc2f5` | links, the one primary button per screen |
| stamp (airmail red) | `#b3261e` | `#f08a80` | unread dot, errors |
| wash | `#e9e1d2` | `#232c36` | pressed and selected rows |

Every text pair passes WCAG AA in both themes (lowest: muted on wash 4.93:1 light, white on accent 5.50:1 dark).

## Type

- Letters, subjects and headings: Georgia (system serif), 700 for headings.
- Controls, lists and labels: system-ui.
- Floor 13 px, body 16 px, line height 1.5 (1.6 for letter text).

## Space and shape

- Spacing steps 4, 8, 12, 16, 24, 32. Radius 10. Tap targets 48 px (44 minimum).
- One shadow only, on the Write button and the desktop composer.

## Signature

The airmail stripe: red, paper, blue, paper at 135 degrees. 4 px along the top of every screen, 8 px on the
composer and the page on the PC. Nowhere else.

## Voice

Plain words, said once. Say where mail leaves from, who it goes to and what happened: "Sent to Mateo Silva",
"Not sent. ... Your text is still here.", "Saved to Drafts". No exclamation marks, no jargon.

## States

- Unread: red dot, bold sender, subject and time. Read mail steps back to regular weight.
- Notices sit above the Write button, never on it.
- Send waits 5 seconds behind Undo. A failed send reopens the message with the text and the reason.
- PC out of reach: "Can't show your mail", the reason, and Try again.
- Autopilot: one lamp and one word, On or Paused, then one sentence of facts (which mailbox, last look,
  answers today). The primary button is Switch on while paused; while on, the only primary button is
  Send on a waiting mail. Each line of *What it did* starts with what happened: Answered (green),
  Kept for you (red), Left alone, Not answered, Problem, You changed. An answer that went out is
  shown in full, as a quote.
- Autopilot through the relay says so, says when the PC last wrote, and shows a switch you pressed at
  once while the PC picks it up.

## Banned

Gradients as decoration, purple, glass, emoji, cards inside cards, centered hero text, more than one accent
per screen, icons without a label next to a destructive action.
