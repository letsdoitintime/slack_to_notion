# 2026-07-22 — Tier C: dateparser

## What moved

| package | installed | manifest |
|---|---|---|
| dateparser | 1.4.0 → 1.4.1 | `>=1.2.0` → `>=1.4,<2` |

## Why a patch bump got the tier-C treatment

`parse_due_date` fails **silently**. A changed parse does not raise — it writes a
different due date onto a real Notion task and the suite stays green, because the suite
asserts a handful of synthetic phrases ("Fix this by 2027-03-15", "due June 20 2027")
that a regression would have to be unlucky to break. The size of the version bump does
not change that failure mode, so the gate is parity over real data, not a green suite.

## The harness

`scripts/dateparser_parity.py` — capture under each version, then diff cell by cell.
Two design points decide whether it is worth anything:

- **The reference date is frozen.** Production settings use `PREFER_DATES_FROM: future`,
  so "tomorrow" and "next Friday" resolve against *now*. Capturing two versions minutes
  apart would diff on wall-clock drift and bury a real regression in noise.
  `RELATIVE_BASE` pins it; every other setting is the production dict, unmodified,
  through the production call path.
- **Sentinels stay distinct.** "no date found" (`{"found": null}`), "a date was found"
  (`{"found": "YYYY-MM-DD"}`) and "the parser raised" (`{"raised": "..."}`) are three
  different outcomes with three different shapes. Collapsing them together would coerce
  away exactly the regression the harness exists to catch.

The corpus is real Slack message text from the bot's own database — feature-level: free
prose in mixed languages, quoted logs and stack traces, pasted URLs and IDs, mrkdwn
entities, and a long tail of text containing no date at all. Only a hash of each text is
written into the capture, never the text, so captures are shareable.

## Result

```
old: dateparser 1.4.0
new: dateparser 1.4.1
PARITY OK — 0 differences.
```

Identical on every message, including the exception column (neither version raised on
any input).

Full suite: 217 passed, 4 skipped on both versions. `pip check` clean.

## The harness was checked for teeth

A parity harness that always reports OK is worthless, so it was run against a
deliberately perturbed baseline — one production setting flipped
(`PREFER_DATES_FROM: future → past`), same version, same frozen corpus:

```
PARITY BROKEN — 289 texts differ:
  old={'found': '2026-07-24'}  new={'found': '2026-07-20'}
  old={'found': '2026-11-22'}  new={'found': '2025-11-22'}
  …
```

So the clean run above is a real result, not a harness that cannot see anything.

## Two things the real-data pass surfaced

- **The corpus is a moving target.** The bot writes to that database continuously; the
  first attempt captured old and new straight from the live file and the corpus grew by
  one row between the two runs. The diff refuses to compare mismatched corpora rather
  than reporting a bogus difference, and the harness now documents snapshotting first.
  A synthetic corpus would never have shown this.
- **A pre-existing quirk, not a regression.** The perturbation run exposed real messages
  where a bare two-digit year parses to an absurd year (`2079-07-22` under
  `PREFER_DATES_FROM: future`). Both 1.4.0 and 1.4.1 agree on it, so it is out of scope
  here, but it is a live wrong-due-date source worth its own look.

## Rollback

Revert the commit, reinstall from `requirements.txt`, restart the service. No stored
artifact changes — due dates already written to Notion are unaffected.
