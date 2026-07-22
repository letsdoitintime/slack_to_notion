# 2026-07-22 — Tier A: pytest toolchain

## What moved

| package | installed | manifest floor | | |
|---|---|---|---|---|
| | before → after | before | after | |
| pytest | 9.0.3 → 9.1.1 | `>=8.0.0` | `>=9.0,<10` | |
| pytest-asyncio | 1.3.0 → 1.4.0 | `>=0.23.0` | `>=1.3,<2` | |

Test-only packages, no auth / wire / data surface. Tier A.

## Gate: suite green on old and new

| pytest / pytest-asyncio | result |
|---|---|
| 9.0.3 / 1.3.0 (old) | 217 passed, 4 skipped |
| 9.1.1 / 1.4.0 (new) | 217 passed, 4 skipped |

Same counts, same skips. `pip check` clean on both.

## The actual finding: the declared floors did not work

The bump itself was uneventful. Installing at the *declared floors* was not — the manifest
was claiming support for a combination that cannot run the suite at all:

```
pytest==8.0.0 + pytest-asyncio==0.23.0

INTERNALERROR> File ".../pytest_asyncio/plugin.py", line 610, in pytest_collectstart
INTERNALERROR>   collector.obj.__pytest_asyncio_scoped_event_loop = scoped_event_loop
INTERNALERROR> AttributeError: 'Package' object has no attribute 'obj'

1 warning in 0.01s
```

Zero tests collected. `pytest-asyncio` 0.23 also predates the
`asyncio_default_fixture_loop_scope` option that `pytest.ini` sets, so even past the
crash it would have scoped async fixtures differently and only warned about it.

Floors are now set to versions actually exercised, and verified rather than assumed:

| pytest / pytest-asyncio | result |
|---|---|
| 8.0.0 / 0.23.0 (old declared floor) | **INTERNALERROR, 0 collected** |
| 9.0 / 1.3 (new declared floor) | 217 passed, 4 skipped |

Both bounds are capped at the next major: pytest-asyncio changes async-fixture scoping
between majors, and that fails silently rather than loudly.

## Side finding — `aiohttp` is load-bearing and undeclared upstream

Building the floor venv without `aiohttp` failed at import:

```
slack_bolt/app/async_app.py:8: ModuleNotFoundError: No module named 'aiohttp'
```

`pip show slack_bolt` → `Requires: slack_sdk`. slack-bolt does **not** declare aiohttp,
so the explicit `aiohttp>=3.9.0` line in `requirements.txt` is the only thing that makes
`AsyncApp` importable — even though nothing in `src/` imports aiohttp directly
(the Ollama client uses stdlib `urllib`). It looks like a prunable unused dependency and
is not one. Commented on the line in the tier-B batch, which owns that pin.

## Rollback

Revert the commit and reinstall from `requirements.txt`. Test-only packages — no runtime
or deploy impact.
