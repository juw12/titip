# Titip refine log

Run of 2026-11-17. One artifact: the phone app, its desktop two-pane view and the page on the PC.
Scores are council-judged by three model judges (A, B, C below) reading screenshots of the demo
mailbox. They are opinions of models, not of users. Nobody has judged it on a real phone yet.

## Run queue

- [x] Freeze the rubric before the first edit
- [x] Baseline v0 (commit `15ad79e`)
- [x] Round 1: kept (commit `cb86588`)
- [x] Round 2: not kept
- [x] Round 3: not kept, loop stopped (2 rounds in a row without a kept gain)
- [x] Candidate from rounds 2 and 3 saved on branch `try/day-groups-write-in-bar`
- [ ] Open: try it on a real phone on the same network, and send one real message

## Assumptions

- Assumed: one design direction (a letter on your desk), no four-direction bracket. One job, one user.
- Assumed: opening a message on the phone does not mark it read in Outlook. The phone only clears its own dot.
- Assumed: no automatic signature. An earlier build read the signature from a hidden Outlook editor;
  that was removed because it could keep Outlook busy.
- Assumed: the repository is public, like Cairn, and holds invented mail only.
- Assumed: the frozen keep rule decides, also when the side-by-side vote and the score disagree (round 3).

## Rubric (frozen before any edit)

Artifact: the Titip phone app, its desktop two-pane view and the page on the PC, judged from screenshots
of the demo mailbox (invented mail). Audience: one engineer reading and answering work mail on a phone.

## Hard gates (code-checked, the judges never decide these)

| Gate | Check |
| --- | --- |
| G1 | `python -m unittest discover -s tests -t .` passes |
| G2 | `python scripts/e2e.py` passes every check (pairing, read, reply, undo, draft, search, lockout, stop) |
| G3 | hone app audit A1-A8 passes (no console errors, no overflow, AA contrast, tap targets, 12 px floor, alt, one h1) |
| G4 | hone slop_check: 0 blockers |
| G5 | no sideways overflow at 320, 390, 768, 1024, 1440 px |
| G6 | every text/background token pair passes WCAG AA in light and dark |
| G7 | no real mail, name, address or secret in the repo (tests/test_repo.py) |

## Soft criteria (judged from screenshots)

- C1 Reading mail on a phone (w=0.30): 4 = a list and a message can be read but sender, subject, preview and time compete; 7 = they are clearly ranked, the message header and body are easy to scan and attachments are visible; 10 = unread and attachment state are readable at a glance, nothing is clipped, crowded or covered at 320-390 px, tables inside a mail stay readable.
- C2 Replying and writing (w=0.25): 4 = one can type and send but it is unclear who it goes to or what happens next; 7 = recipient, primary action and cancel are obvious, sending has an undo and saving a draft is visible; 10 = every state (sending, sent, saved, failed, text kept) is told in plain words and nothing can go out by accident.
- C3 Visual identity and craft (w=0.20): 4 = generic template look; 7 = consistent palette, type pair and spacing with one accent used with intent; 10 = distinct enough that a designer would not call it a default AI layout, with the same quality in dark mode and in the desktop two-pane view.
- C4 Trust and plain words (w=0.15): 4 = where mail leaves from and who can read it is not explained; 7 = the demo is labelled, the page on the PC says how pairing works and how to cut phones off, errors are plain; 10 = a non-technical person understands all of that without jargon and without hunting for it.
- C5 States and widths (w=0.10): 4 = works at one width only; 7 = empty, notice and error states exist and the layout holds from 320 to 1440 px; 10 = each state is designed, the desktop view uses its space well and no element covers another anywhere.

## Rules

- Keep a round only if every gate passes, the council score rises by at least 0.3 and the candidate wins the pairwise vote.
- Stop after 5 rounds, or 2 rounds in a row without a kept gain.
- Never report a score as more than what the judges returned; never call the result perfect.

## Baseline v0

Commit `15ad79e`. Gates pass. Council **7.75** (C1 7, C2 8, C3 8, C4 9, C5 7; judges 8.10, 7.55, 7.58).
Judged from 4 images.

## Round 1: kept

Changes: unread marks come from Outlook; sending, sent, saved and failed states say what happened in
plain words; notices sit above the Write button instead of under it; the desktop view gets a reply bar.

- Gates: 41 unit tests, browser test, audit A1-A8, slop check 0 blockers: all pass.
- Council **8.75** (C1 8, C2 9, C3 9, C4 10, C5 8; judges 9.00, 7.65, 8.75). One judge reply had to be
  salvaged from a malformed answer.
- Side by side against v0: 3-0 for round 1.
- Kept: +1.00, vote won.

Caveat: the judged set grew from 4 to 5 images in this round, and one of the new images shows states
that v0 already had. Part of the +1.00 is better evidence, not better design.

## Round 2: not kept

Changes: Write moved into the bar with the tabs (bottom on a phone, top on desktop); day headings
(Today, Yesterday, date) in the list; table headers in a mail stay on one line.

- Gates: all pass.
- Council **8.60** (C1 8, C2 9, C3 9, C4 9, C5 8; judges 9.80, 8.20, 8.35).
- Side by side against round 1: 1 vote for round 2, 2 ties. No win.
- Not kept: score did not rise, vote not won.

## Round 3: not kept

Changes on top of round 2: drawn postmark marks on the empty, no-result and offline states; plainer
hints; a line under the list saying the PC serves the mail; plainer certificate wording on the PC page.

- Gates: all pass (41 unit tests, browser test, audit A1-A8, slop 0 blockers and 0 warnings,
  contrast AA in light and dark, lowest pairs 4.93 and 5.50).
- Council **8.50** (C1 8, C2 9, C3 8, C4 9, C5 9; judges 9.50, 8.50, 8.40).
- Side by side against round 1: 3-0 for round 3 (6 of 6 picks, both orders).
- Not kept: the rule needs +0.3 on the score and a won vote. The vote was won, the score was not.

The two measures disagree. Absolute scores moved by up to 0.8 per judge between runs of near-identical
screens, so a 0.25 drop is inside the noise, and the side-by-side vote is the steadier signal. Two
judges also counted the "no recipient" stop as new, although round 1 has it and only the new screenshot
showed it. The rule was frozen first, so it stands: main stays on round 1. The candidate is on branch
`try/day-groups-write-in-bar` (commit `e6a26d9`), gates green, ready to merge if the owner prefers it.

## Result

v0 **7.75** -> final **8.75** (council-judged, 3 judges, range 7.65 to 9.00). Stopped by the rule:
two rounds in a row without a kept gain.

## What the judges still want (not done)

- A mail table at 320 px gets narrow; give it a sideways scroll or keep values on one line.
- "Add Cc" on the reply screen is a text link and easy to miss.
- The identity is consistent but safe: carry the airmail stripe into more than the top edge.
- Name the recipient in the undo notice ("Sending to ... in 5 s").

## Not verified

- A real phone: pairing by QR, the certificate warning, and whether Windows Firewall lets the phone in.
- A real send. No test ever calls Outlook's Send; the demo mailbox stands in.
- The live Outlook mailbox end to end in a browser. Outlook on the build PC stopped answering during
  the run, so the final check ran against the demo mailbox and the unit tests only.
