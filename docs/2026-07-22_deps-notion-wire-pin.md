# 2026-07-22 — Pin the Notion wire contract

## What moved

| package | before | after | why |
|---|---|---|---|
| notion-client | `>=2.2.1` (unbounded) | `>=3.0.0,<4` | floor permitted a major with a different wire contract |

No installed version changed — `notion-client` was already 3.1.0. This closes a
*rebuild* hazard, not a drift one, so `depkit.py scan` never flagged it: the package
reports as perfectly up to date.

## The problem

`src/notion/client.py` constructed `Client(auth=token)` and inherited whatever
`Notion-Version` header the installed library defaulted to. That default moves with
the major:

```
UNPINNED default header, per notion-client version
  notion-client 2.2.1  ->  2022-06-28
  notion-client 3.1.0  ->  2025-09-03
```

Both of those satisfied `notion-client>=2.2.1`. Two `pip install -r requirements.txt`
runs from the same file could therefore talk **different Notion APIs**.

That header is not cosmetic. Under `2025-09-03` a database is a container of *data
sources*, and the `parent: {"database_id": ...}` payload in `create_page` is accepted
only as a compatibility shim for single-data-source databases. The API version decides
whether task creation works at all — and nothing in the Python surface changes, so a
fully green suite cannot see it.

## What was done

- Pinned `NOTION_API_VERSION = "2025-09-03"` at the single construction point
  (`src/notion/client.py`), passed explicitly to `Client(...)`.
- Bounded the manifest to `>=3.0.0,<4` and commented why in the file.
- Deleted `get_database_schema` — dead code (no callers anywhere), and the method most
  exposed to `2025-09-03`, where a database's `properties` moved to its data sources.
  Deleting it was smaller than proving it. Its now-unused `logging` and
  `APIResponseError` imports went with it.

## What was proven

`tests/test_notion_client.py` asserts the header on a request built through httpx's own
builder — the merged headers actually sent, not an attribute we hope is used.

Run under both ends of the range the old floor permitted:

| notion-client | result |
|---|---|
| 2.2.1 (old manifest floor) | 6 passed |
| 3.1.0 (installed / prod) | 6 passed |

Passing on **both** is the point: after the pin the wire contract is the same on either
version, so the test guards the next upgrade rather than snapshotting the present.

Full suite: 223 passed, 4 skipped (217 before, +6 new).

Live confirmation: `bot.log` shows `POST /v1/pages → 200 OK` throughout 2026-07-22 on
notion-client 3.1.0, i.e. the `database_id` shim is currently working against the real
API for the databases this bot writes to.

## Rollback

Revert the commit and restart the service. No data migration, no stored-artifact change —
this only affects an outbound request header.
