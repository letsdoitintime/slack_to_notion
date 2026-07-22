# 2026-07-22 — Due-date parser: stop inventing dates

## The report, and what it actually turned out to be

Reported: a bare two-digit year parses into an absurd far-future date (`2079-07-22`),
which then gets written as the Due Date on a real Notion task with no error.

The report was right that dates are being invented, and right about the example — but
two-digit years are one symptom of a much broader problem. `search_dates` scans free
text for *anything* it can coerce into a date, and this bot's traffic is payment-ops
chat: full of amounts, method IDs, error codes, masked card numbers, version ranges and
standards references. Measured over the real corpus, **459 of 15 072 messages produced a
due date, and the overwhelming majority were nonsense**:

| what was in the message | what became the due date |
|---|---|
| `сумма 4300 TRY` (an amount) | `4300-07-22` |
| `method ID to 1664` | `1664-07-22` |
| `$79.89 Manually credited` | `2079-07-22` ← the reported case |
| `ISO 3166-2` (a standard) | `3166-02-22` |
| `9800` | `9800-07-22` |
| `We are testing the withdrawal…` | next Wednesday (`We` → Wednesday) |
| `this may require minor changes` | May (`may` the modal verb) |
| `набор из 2-3 значениц` (a range) | a March date |
| `Pay IN: 6,5 %` (a fee) | June 5 |
| `"timestamp": "06:42:32"` (a time) | tomorrow |

85 of them landed outside 2026–2028 entirely, ranging from `1001-07-22` to `9800-07-22`.

**A sanity bound alone would not have fixed this.** The single largest class — any English
message beginning "We …" becoming next Wednesday — lands comfortably inside any sane
window, as does a case ID of `2025` becoming `2025-07-22`. The bound is necessary but not
sufficient; the root cause is that a match was never checked for being date-shaped.

## The fix

`parse_due_date` now requires a match to earn its date, then bounds the result:

1. **A month name only counts with a day number attached** — `June 13`, `by 10 July`,
   `May 29`. On its own, "may" is the modal verb far more often than the month, and
   "June Invoice" is a document, not a deadline.
2. **A numeric date must be complete — year, month and day, with a four-digit year.**
   `2026-05-22` yes; `10-4` / `2-3` / `1/2` no. There is no structural way to tell
   `dd-mm` from a range or a count, and on real traffic the bare form produced far more
   noise than signal. This does cost the occasional genuine `24/07`; that is the right
   trade here.

   Requiring the *day* as well is what stops the same trick sneaking back in near the
   current year, which review caught: `SDK 2027-1` and `ISO 2027-2` are a version and a
   standard, but dateparser fills the missing day from today and the result lands inside
   the horizon check. `ISO 3166-2` is only caught by the window because 3166 is absurd —
   2027 is not. This class produced **no change on the corpus** (34 before and after,
   0 differences): it was latent, not present, which is precisely why it needs a test
   rather than a corpus number.
3. **Relative expressions are matched explicitly** — `today`, `tomorrow`, `next week`,
   `2 weeks`, `48 hours`, and any bare weekday — whole-word, which is what stops `We`
   matching Wednesday.

   Weekdays are accepted **bare**, without requiring a `next`/`on`/`by` cue, because
   `search_dates` *strips the cue*: "next Friday", "due Friday" and "deadline Monday" all
   come back with a matched substring of just `Friday`. An earlier revision required the
   cue inside the match and so rejected all of them — including "next Friday", which this
   module's own docstring advertises as supported. Review caught it. A bare weekday is
   safe in a way a bare month name is not: it resolves within a week, so the sanity window
   keeps it plausible, and `we` still cannot match because it is not a weekday
   abbreviation (`wed` is the shortest that is). No corpus change (34 → 34, 0 rows differ).
4. **The result must fall within `[today, today + 2 years]`.** Past is not a due date;
   beyond the horizon is a misparsed year.
5. **Scanning continues past a rejected match** rather than giving up on the first one,
   so "ID 1234, please finish by next Friday" still works.

### The subtle one

`search_dates` resolves each match **relative to the previous match in the same text**.
Rejecting a match does not undo its effect on the ones after it — in

> it may require code change and up to **2 weeks** of development

"2 weeks" was computed off the bogus `may` match and came back as **2027-06-05** instead
of two weeks out. The accepted match is now re-parsed on its own, which anchors it to
now. Same story for `on Monday`, which was landing a week late.

## Proof over the real corpus

`scripts/dateparser_parity.py` gained an `--entry` flag. The existing capture path calls
`search_dates` directly — correct for comparing dateparser *versions*, but it bypasses
our filtering entirely, so it is blind to a change like this one. `--entry parse_due_date`
captures the real entry point.

```
captured parse_due_date on dateparser 1.4.1: 15072 texts, 459 with a date, 0 raised   [before]
captured parse_due_date on dateparser 1.4.1: 15072 texts,  34 with a date, 0 raised   [after]
PARITY BROKEN — 437 of 15072 texts differ
```

| | before | after |
|---|---|---|
| messages yielding a due date | 459 | 34 |
| dates outside 2026–2028 | 85 | **0** |
| dates newly *gained* | — | **0** |
| exceptions raised | 0 | 0 |

Zero gained matters: the change only ever removes or corrects, so nothing that previously
had no due date suddenly acquired one.

All 34 survivors were read individually. Every one is a genuine date expression —
`today`, `tomorrow`, `on Monday`, `2 weeks`, `48 hours`, `by 1 August`, `by July 1st`,
`Jun 12th`, `Tuesday, June 23`, `by 10 July`. Some month-and-day mentions still resolve a
year out (`May 29` in July 2026 → 2027-05-29) because `PREFER_DATES_FROM: future` is
doing what it is told; that is inherent ambiguity in the message, not a parsing defect,
and it is in range and plausible rather than absurd.

## Tests

`tests/test_due_date_parser.py`: 6 → 32. The rejection cases are **real strings from the
production corpus**, each annotated with the date it used to produce. There is also a
positive class asserting the expressions the feature exists for still work, so the
filtering cannot be tightened into uselessness later.

Checked for teeth: the new tests were run against the *old* parser — **14 fail**. They
guard the fix rather than merely describing it.

Full suite: 219 passed, 4 skipped.

## Rollback

Revert the commit and restart. Due dates already written to Notion are unaffected; this
only changes what future reactions extract.
