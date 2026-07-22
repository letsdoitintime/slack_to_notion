#!/usr/bin/env python3
"""Capture -> diff parity harness for due-date parsing across dateparser versions.

Why this exists: ``parse_due_date`` fails *silently*. A dateparser change that
shifts a parse does not raise -- it writes a different due date onto a real Notion
task, and the test suite (which only asserts a handful of synthetic phrases) stays
green. The only way to see it is to run the real entry point over real message text
on both versions and diff the output cell by cell.

Usage — snapshot the corpus, capture under EACH venv, then diff:

    python3 -c "import sqlite3; s=sqlite3.connect('file:slack_to_notion.db?mode=ro',uri=True); \
d=sqlite3.connect('/tmp/corpus.db'); s.backup(d)"
    <venv-old>/bin/python scripts/dateparser_parity.py capture --db /tmp/corpus.db --out old.json
    <venv-new>/bin/python scripts/dateparser_parity.py capture --db /tmp/corpus.db --out new.json
    python3 scripts/dateparser_parity.py diff old.json new.json

Snapshot first, always. The bot writes to that database continuously; capturing the
two versions straight from the live file means the second run can see messages the
first never did. That happened on the first run of this harness -- the corpus grew by
one row between captures. The diff refuses to compare mismatched corpora rather than
reporting a bogus difference, but a snapshot avoids the wasted pass.

Two design points that decide whether the harness is worth anything:

* **The reference date is frozen.** Production settings use
  ``PREFER_DATES_FROM: future``, so "tomorrow" and "next Friday" resolve against
  *now*. Capturing two versions minutes apart would diff on wall-clock drift and
  bury a real regression in noise. ``RELATIVE_BASE`` pins it; everything else is
  the production settings dict, unmodified, through the production call path.

* **Sentinels stay distinct.** "no date found", "a date was found", and "the
  parser raised" are three different outcomes and are tagged as three different
  shapes. Canonicalizing them together (all -> null) would coerce exactly the
  regression this harness is meant to catch.

The corpus is real message text read from the local bot database. It is never
written into the capture file -- only a hash of it -- so captures and diffs can be
shared without leaking message content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.due_date_parser import _DATEPARSER_SETTINGS  # noqa: E402

# Frozen so relative expressions are reproducible across captures. Any fixed
# instant works; this one is arbitrary but must not change between the two runs.
RELATIVE_BASE = datetime(2026, 7, 22, 12, 0, 0)

DEFAULT_DB = Path(__file__).resolve().parent.parent / "slack_to_notion.db"


def load_corpus(db_path: Path) -> list[str]:
    """Return real Slack message texts from the local bot database."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT message_text FROM slack_messages "
            "WHERE message_text IS NOT NULL AND message_text != ''"
        ).fetchall()
    finally:
        con.close()
    # Deduplicate but keep order stable, so the two captures line up positionally.
    seen: set[str] = set()
    corpus: list[str] = []
    for (text,) in rows:
        if text not in seen:
            seen.add(text)
            corpus.append(text)
    return corpus


def parse_one(text: str) -> dict:
    """Run the raw dateparser call over *text* and tag the outcome.

    This is the entry point for comparing dateparser *versions*: it pins library
    behaviour without our own filtering on top, so a library change shows up
    undiluted.

    Returns one of three distinguishable shapes:
        {"found": "YYYY-MM-DD"}   a date was parsed
        {"found": None}           parsed cleanly, no date in the text
        {"raised": "TypeError: ..."}  the parser raised -- part of the contract
    """
    from dateparser.search import search_dates

    settings = dict(_DATEPARSER_SETTINGS, RELATIVE_BASE=RELATIVE_BASE)
    try:
        results = search_dates(text, languages=["en"], settings=settings)
    except Exception as exc:                      # exceptions are results
        return {"raised": f"{type(exc).__name__}: {exc}"}
    if not results:
        return {"found": None}
    _matched, dt = results[0]
    return {"found": dt.strftime("%Y-%m-%d")}


def parse_one_entrypoint(text: str) -> dict:
    """Run the real `parse_due_date` over *text* and tag the outcome the same way.

    This is the entry point for comparing *our own changes* to the parsing rules.
    `parse_one` above deliberately bypasses them, so it cannot see a filtering
    change at all -- capture with `--entry parse_due_date` when the diff under
    test is in `src/utils/due_date_parser.py` rather than in the library.

    Note this path resolves relative expressions against the real "now" rather
    than RELATIVE_BASE, since that is what production does. Capture both sides on
    the same day.
    """
    from src.utils.due_date_parser import parse_due_date

    try:
        return {"found": parse_due_date(text)}
    except Exception as exc:                      # exceptions are results
        return {"raised": f"{type(exc).__name__}: {exc}"}


ENTRY_POINTS = {"search_dates": parse_one, "parse_due_date": parse_one_entrypoint}


def capture(db_path: Path, out_path: Path, entry: str) -> None:
    import dateparser

    parse = ENTRY_POINTS[entry]
    corpus = load_corpus(db_path)
    rows = [
        {
            "i": i,
            # Hash, not text: captures stay shareable without leaking messages.
            "key": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            "result": parse(text),
        }
        for i, text in enumerate(corpus)
    ]
    out_path.write_text(
        json.dumps(
            {
                "dateparser_version": dateparser.__version__,
                "entry": entry,
                "relative_base": RELATIVE_BASE.isoformat(),
                "settings": {k: str(v) for k, v in _DATEPARSER_SETTINGS.items()},
                "rows": rows,
            },
            indent=1,
        )
    )
    found = sum(1 for r in rows if r["result"].get("found"))
    raised = sum(1 for r in rows if "raised" in r["result"])
    print(
        f"captured {entry} on dateparser {dateparser.__version__}: "
        f"{len(rows)} texts, {found} with a date, {raised} raised -> {out_path}"
    )


def diff(old_path: Path, new_path: Path) -> int:
    old = json.loads(old_path.read_text())
    new = json.loads(new_path.read_text())

    if old["relative_base"] != new["relative_base"]:
        print("REFUSING TO DIFF: captures used different RELATIVE_BASE values.")
        return 2
    if old["settings"] != new["settings"]:
        print("REFUSING TO DIFF: captures used different parser settings.")
        return 2
    if old.get("entry") != new.get("entry"):
        print("REFUSING TO DIFF: captures used different entry points.")
        return 2

    print(f"old: dateparser {old['dateparser_version']}  ({len(old['rows'])} texts)")
    print(f"new: dateparser {new['dateparser_version']}  ({len(new['rows'])} texts)")

    by_key_old = {r["key"]: r["result"] for r in old["rows"]}
    by_key_new = {r["key"]: r["result"] for r in new["rows"]}

    if by_key_old.keys() != by_key_new.keys():
        print("REFUSING TO DIFF: corpora differ; re-capture both from the same DB.")
        return 2

    mismatches = [
        (key, by_key_old[key], by_key_new[key])
        for key in by_key_old
        if by_key_old[key] != by_key_new[key]
    ]
    if not mismatches:
        print(f"PARITY OK — {len(by_key_old)} texts, 0 differences.")
        return 0

    print(f"PARITY BROKEN — {len(mismatches)} of {len(by_key_old)} texts differ:\n")
    for key, o, n in mismatches[:50]:
        print(f"  {key}  old={o}  new={n}")
    if len(mismatches) > 50:
        print(f"  … and {len(mismatches) - 50} more")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    cap = sub.add_parser("capture", help="run the parser over the corpus")
    cap.add_argument("--db", type=Path, default=DEFAULT_DB)
    cap.add_argument("--out", type=Path, required=True)
    cap.add_argument(
        "--entry",
        choices=sorted(ENTRY_POINTS),
        default="search_dates",
        help="search_dates = compare dateparser VERSIONS (default); "
             "parse_due_date = compare changes to our own parsing rules",
    )

    dif = sub.add_parser("diff", help="compare two captures")
    dif.add_argument("old", type=Path)
    dif.add_argument("new", type=Path)

    args = ap.parse_args()
    if args.cmd == "capture":
        capture(args.db, args.out, args.entry)
        return 0
    return diff(args.old, args.new)


if __name__ == "__main__":
    raise SystemExit(main())
