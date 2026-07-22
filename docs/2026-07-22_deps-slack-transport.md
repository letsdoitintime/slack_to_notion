# 2026-07-22 — Tier B: Slack transport stack

## What moved

| package | installed | manifest |
|---|---|---|
| slack-bolt | 1.28.0 → 1.30.0 | `>=1.18.0` → `>=1.28,<2` |
| slack-sdk | 3.41.0 → 3.43.0 | `>=3.27.0` → `>=3.41,<4` |
| aiohttp | 3.13.5 → 3.14.2 | `>=3.9.0` → `>=3.13,<4` |

Bumped together and capped together. `slack-bolt` requires `slack_sdk` with **no upper
bound**, so bumping bolt alone can swap the sdk major underneath it, and `aiohttp` is
bolt's async transport. One PR, one venv, one proof.

`slack-bolt` was reported as tier A by the scan (a minor). Escalated to B on contact: it
is the event framework, and the reminders feature is built on an assumption about its
listener dispatch.

## Why the suite could not have caught a regression here

`tests/test_reminders.py` drives the `reaction_added` handler through a `_FakeApp` that
collects functions in a list. It proves the handler *body* works and nothing at all about
**Bolt's own dispatch** — yet the whole reminders design rests on one claim about that
dispatch, which until now lived only in a docstring:

> Regression: reminders were a second `@app.event('reaction_added')` listener, which Bolt
> never reached (it stops after the first).

Never executed against real Bolt. Same shape as a `TestClient(app)` built without a `with`
block: the mechanism the design depends on was never run.

Likewise `aiohttp` is never imported by `src/`, so no test touched it, and the
`SlackClient` error paths all route through `SlackApiError.response.get(...)` — a change
in what `response` *is* turns the `already_reacted` idempotency guard into a silent
failure rather than a loud one.

## The cross-version proof

New `tests/test_slack_transport_contract.py`, **26 tests, run under both versions**:

| stack | result |
|---|---|
| bolt 1.28.0 / sdk 3.41.0 / aiohttp 3.13.5 (old) | 26 passed |
| bolt 1.30.0 / sdk 3.43.0 / aiohttp 3.14.2 (new) | 26 passed |

Passing on both is the point — a test that only passed on the new stack would snapshot
the present instead of guarding the next upgrade.

**Bolt dispatch** — a real `AsyncApp` (offline: `authorize` is stubbed so the genuine
middleware chain and listener matcher still run without a network call):

- Only the **first** matching `reaction_added` listener runs. This is *why* reminders and
  emoji processing are merged into one handler; if a future bolt runs both, this fails
  loudly and the merge can be undone.
- That single listener really receives the event, with the payload intact.

**Slack Web API wire** — every endpoint the bot calls, captured at slack_sdk's send point
so nothing touches the network: `reactions.add`, `chat.postMessage`,
`conversations.history`, `reactions.get`, `users.info`, `conversations.info`,
`conversations.members`, `conversations.replies`, `auth.test`. URL, method, auth header,
body encoding and parameter names are all asserted — the Python surface can be identical
across versions while these move.

**aiohttp exception hierarchy** — the base classes bolt's socket-mode reconnect logic
catches. A reparented subclass turns a handled disconnect into a listener loop that dies
quietly, which for this bot means it goes deaf without crashing.

## Three things the proof surfaced

- **Bolt runs async listeners detached from dispatch.** `async_dispatch` returns 200
  *before* the listener body has run. Two consequences: any test asserting straight after
  dispatch silently sees nothing (the first draft of these tests did), and a listener that
  raises cannot surface in the dispatch result — which is why
  `src/slack/event_handler.py` wraps its own body in try/except rather than relying on
  Bolt to report failures. Now pinned by its own test.
- **`chat.postMessage` is JSON-encoded while every other endpoint is form-encoded.** Not
  cosmetic: `reply_broadcast` travels as a real JSON boolean there, where a form body
  would send the string `"1"`. If slack_sdk changes which calls get JSON, the broadcast
  and threading flags are what change meaning.
- **`aiohttp` is load-bearing and undeclared upstream.** `pip show slack_bolt` →
  `Requires: slack_sdk`. Bolt's async app imports aiohttp but does not declare it, so the
  explicit line in `requirements.txt` is the only thing making `AsyncApp` importable —
  while looking exactly like a prunable unused dependency, since nothing in `src/` imports
  it. Now commented `NOT unused — do not prune`.

## Result

Full suite **243 passed, 4 skipped** on both old and new stacks. `pip check` clean on both.

## Rollback

```bash
.venv/bin/pip install aiohttp==3.13.5 slack-sdk==3.41.0 slack-bolt==1.28.0
```

Then revert the pin commit and restart the service. No stored artifacts and no wire
contract change — the tests above assert the requests are byte-identical across versions.
