#!/usr/bin/env python3
"""One-shot repair for authorless bot rows in ``slack_messages``.

Background
----------
Until 2026-07-22 both ``event_handler`` and ``save_message`` dropped
bot-authored messages. Edits slipped through anyway: ``message_changed`` is
handled above the skip, and its wrapper event carries no top-level ``bot_id``
— that sits on the nested ``event["message"]``. The UPDATE found no original
row, the fallback INSERT fired, and the row landed with no author at all.

The information was never lost, only unread: ``raw_event`` still holds the whole
nested message. This lifts ``bot_id``, and the display name from
``bot_profile.name`` → ``username``, into their columns.

Only rows whose ``raw_event`` actually carries a nested ``bot_id`` are touched,
and an existing ``slack_user_name`` is never overwritten.

This is a one-shot: new rows get their author at write time. Safe to re-run —
the WHERE clause matches nothing once applied.

Usage
-----
    python scripts/repair_bot_authorship.py                 # dry run (default)
    python scripts/repair_bot_authorship.py --apply         # write

Back the DB up first; the bot writes to it continuously::

    sqlite3 slack_to_notion.db ".backup 'backups/pre-repair.db'"
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

# Rows carrying a nested bot_id but no slack_bot_id of their own.
_TARGET = """
      slack_bot_id IS NULL
  AND json_extract(raw_event, '$.message.bot_id') IS NOT NULL
"""

_REPAIR = f"""
UPDATE slack_messages
SET slack_bot_id    = json_extract(raw_event, '$.message.bot_id'),
    slack_user_name = COALESCE(
        slack_user_name,
        NULLIF(json_extract(raw_event, '$.message.bot_profile.name'), ''),
        NULLIF(json_extract(raw_event, '$.message.username'), '')
    )
WHERE {_TARGET}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="slack_to_notion.db")
    parser.add_argument(
        "--apply", action="store_true", help="write the change (default: dry run)"
    )
    args = parser.parse_args()

    # The bot is writing to this database while we are. Wait for it rather than
    # failing the repair on a transient lock.
    conn = sqlite3.connect(args.db, timeout=30.0)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        f"""
        SELECT json_extract(raw_event, '$.message.bot_id') AS bot_id,
               COALESCE(
                   NULLIF(json_extract(raw_event, '$.message.bot_profile.name'), ''),
                   NULLIF(json_extract(raw_event, '$.message.username'), '')
               ) AS name,
               COUNT(*) AS n
        FROM slack_messages
        WHERE {_TARGET}
        GROUP BY bot_id, name
        ORDER BY n DESC
        """
    ).fetchall()

    total = sum(r["n"] for r in rows)
    if not total:
        print("Nothing to repair — no authorless rows carry a nested bot_id.")
        return 0

    print(f"{total} authorless row(s) across {len(rows)} bot(s):\n")
    for r in rows:
        print(f"  {r['n']:>5}  {r['bot_id']:<14}  {r['name'] or '(unnamed)'}")

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply.")
        return 0

    with conn:  # commits on success, rolls back on exception
        changed = conn.execute(_REPAIR).rowcount
    print(f"\nRepaired {changed} row(s).")

    left = conn.execute(
        f"SELECT COUNT(*) FROM slack_messages WHERE {_TARGET}"
    ).fetchone()[0]
    print(f"Remaining authorless rows with a nested bot_id: {left}")
    conn.close()
    return 0 if left == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
