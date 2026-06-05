# 🚀 Codex Orchestration — paste-ready task template

**How to use:** fill in the **TASK BRIEF** below, then paste this whole file into a fresh Claude
Code session in this repo. Claude will (1) create the task doc, (2) delegate implementation to
**Codex**, then (3) review the result. You edit **only** the TASK BRIEF — leave everything under
"INSTRUCTIONS FOR CLAUDE" as-is.

> Loop: **Claude plans → Codex codes → Claude reviews.** Contracts live in `CLAUDE.md` (domain
> facts + conventions) and `AGENTS.md` (Codex's operating rules: verify commands, off-limits paths,
> git policy, approval protocol). Verified against `codex-cli 0.136.0`; defaults
> (`model = gpt-5.5`, `model_reasoning_effort = xhigh`, `sandbox = workspace-write`, this project
> `trusted`) are already set in `~/.codex/config.toml`, so no `-m`/`-c` flag is needed normally.

---

## ✏️ TASK BRIEF — fill this in

```
Slug (kebab-case):    <e.g. add-psp-fee-coverage-badge>

Goal (what & why):    <1–3 sentences: what we're building/fixing and the user value>

Acceptance criteria:
  - <criterion 1>
  - <criterion 2>

Likely affected areas (optional):
  - backend/app/rules/...        # analytics, stats, decline analysis, recommender, tx analytics
  - backend/app/imports/...      # PG + mega importers / normalizers
  - frontend/src/components/...   # (+ rules/, tools/, admin/ subtrees)

Constraints / out of scope (optional):
  - <e.g. no new dependencies; reuse existing component patterns; respect CLAUDE.md domain facts>
```

---

## 🤖 INSTRUCTIONS FOR CLAUDE — do these in order

You are the **planner + reviewer** in the `Claude-plans → Codex-codes → Claude-reviews` loop.
**Do NOT write the feature code yourself — Codex implements it.** Scope it, hand it off cleanly,
review the diff, iterate.

### 1 · Create the task doc
- Get today's date: `date +%F`
- Create `docs/<YYYY-MM-DD>_<slug>.md` with **Goal**, **Affected files** (best guess — refine
  later), and **Approach / decisions** — per the doc-per-change convention in `CLAUDE.md` /
  `.github/copilot-instructions.md`.

### 2 · Plan (briefly)
- Inspect the relevant files yourself first so the plan is grounded in real code.
- Write a short bullet plan into the doc. Capture any constraint Codex must respect (existing
  naming/patterns, no new deps, URL-state persistence for frontend filters, expected tests).
- Cross-check **`CLAUDE.md` → "Verified Mega-analytics domain facts"** — do not ask Codex to
  re-derive them (withdrawals-only denominator, EUR base columns, no auxiliary `tx_type` exclusion).
- ⚠️ If the task touches **DB schema/migrations, auth/security, payments/money, infra/deploy, or a
  broad (>5-module) rewrite**, decide the approach up front and put it in the prompt — per
  `AGENTS.md`, Codex will **pause and ask** on these even when implementation is "approved."

### 3 · Delegate to Codex
Use a **unique slug → unique output file** so parallel runs don't clash. The prompt is piped via
stdin (`-`) so multi-line content needs no quote-escaping:

```bash
codex exec -s workspace-write -o /tmp/codex_<slug>.txt - <<'PROMPT'
Implementation is approved for this bounded task.

<concise task: goal, acceptance criteria, affected files, constraints>
PROMPT
```

- The exact phrase **"Implementation is approved for this bounded task"** waives Codex's
  design-approval step (`AGENTS.md` keys on it) — but the hard-stops above still make Codex pause.
- Reasoning effort defaults to **`xhigh`** (from `~/.codex/config.toml`) — no flag needed. For a
  trivial change, append `-c model_reasoning_effort="medium"`.
- Keep the prompt **minimal** — Codex already reads `AGENTS.md` (layout, verify commands, off-limits
  paths, git policy, "do not commit"). Don't restate the workflow.

### 4 · Review
- Read **`git diff`** *and* **`/tmp/codex_<slug>.txt`** (Codex's own summary).
- Check: correctness vs acceptance criteria · adherence to `CLAUDE.md` domain facts · security ·
  style/consistency with surrounding code · scope creep · any **new dependency** (must be flagged in
  Codex's summary) · **verification actually ran**:
  - backend touched → `.venv/bin/python -m pytest backend/tests/ -v` passed,
  - frontend touched → `cd frontend && npm run build` succeeded.
  - **There is no linter/type-checker** in this repo — don't accept "lint/type-check passed" as
    evidence; it doesn't exist here.
- Codex leaves changes **unstaged** and does **not** commit/push (`AGENTS.md`) — review the working
  tree; don't assume a commit exists.

### 5 · Iterate (if needed)
Write a focused corrective prompt and **resume the same Codex session** (keeps its context):

```bash
codex exec resume --last -o /tmp/codex_<slug>.txt "<corrective instructions>"
```

> `resume` inherits the original session's sandbox — do **not** pass `-s` here (not a valid flag on
> `resume`). Repeat review ↔ iterate until the acceptance criteria are met.

### 6 · Finish
- Update the task doc with what actually changed.
- Move it to **`docs/done/`**: `git mv docs/<YYYY-MM-DD>_<slug>.md docs/done/`
- Add a **Result** section: what shipped/fixed · files changed · verify results · date completed.
- Summarize for the user: what changed, test/build results, and anything still to decide.
  **Do not commit or push unless explicitly asked.**

---

### 🩺 Codex health check (only if Codex seems misconfigured)

```bash
codex exec -s workspace-write -o /dev/null "Say: OK"
```

### 📎 Quick flag reference (codex-cli 0.136.0)
| Flag | Purpose |
|------|---------|
| `-s workspace-write` | Codex may edit files; no destructive shell ops (omit on `resume`) |
| `-o /tmp/codex_<slug>.txt` | Capture Codex's final summary (`--output-last-message`; unique slug per task) |
| `-` (positional) | Read the prompt from stdin — clean for multi-line heredocs |
| `-c model_reasoning_effort="medium"` | Dial reasoning down from the default `xhigh` |
| `-m gpt-5.5` | Override model (default already `gpt-5.5` in `~/.codex/config.toml`) |
