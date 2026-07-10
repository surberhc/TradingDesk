# CLAUDE.md — TradingDesk operating contract

## What this is
An evergreen Interactive Brokers trading system — research, paper execution, data, and
reporting under one roof. There is no "done"; we build and grow it continuously. "Good"
means strategies that are robust and honestly validated (never curve-fit), execution that
is safe and reversible (paper for now), and a clean compliance trail behind every change.

## The two non-negotiables
1. **Never curve-fit.** This is the first rule of the whole project. Strategy and regime
   config knobs are **frozen** — no tuning without Andrew's explicit blessing. Any
   parameter or strategy change must clear out-of-sample and per-episode / per-regime
   checks and must not be fit to a single period. A fragile win is not a win — flag the
   curve-fit risk and stop rather than ship it. Default to the curve-fit-*preventing*
   read of an ambiguous result.
2. **Paper only — for now.** Live trading IS a planned future milestone; we just haven't
   crossed that bridge yet. Until we deliberately decide to, everything runs on the PAPER
   account (DU…141, port 4002) and nothing trades real money. Never call it "live."
   Backstop: the only login available reaches the paper account, so live trading isn't
   reachable by accident. The review → arm → transmit gate stays sacred; nothing transmits
   without a deliberate, gated, armed action.

## The counterweight — judge on net merit
Rule #1 bars curve-fitting the **strategy**; it does not license curve-fitting the **test** to guarantee failure. Every real strategy has weak spots — the call is whether strengths outweigh weaknesses **on balance**, never whether a weakness exists. Don't reflexively hunt for a reason to fail a result, treat a single weak spot as disqualifying, or move the goalposts once something clears the bar; a weakness disqualifies only when it genuinely outweighs the strengths. Reserve the strict robustness gate for its real job — parameters tuned to a period — not for beating down every honest result. Test a strategy in the **role it's actually used** (its real combination/deployment) against a bar that matches how it's meant to work — not a strawman in isolation (a hedge needn't profit every year; a financing overlay needn't win the crash its paired hedge exists to cover). **Lead with the net verdict, then weigh caveats in proportion.** If you're stacking "buts" onto a good result, stop and state the balance.

## Where things live
- **Code** is in Google Drive at the `TradingDesk\` root (synced + backed up).
- **Data, running state, the venv, and secrets** live on local `C:\TradingDesk-Local\`
  and are NEVER synced. Backtester market data lives there too (Drive sync corrupts it).
- Run Python with the local venv: `C:\TradingDesk-Local\venv\Scripts\python.exe`.
- Folders: `strategies\` (shared brain — one file per strategy, imported by both backtester
  and paperbot), `backtester\` (research), `paperbot\` (paper execution), `connections\`
  (shared IBKR/Tiingo access + the collision-proof clientId registry), `datacollector\`
  (options warehouse), `dailyreport\` (EOD regime email), `msr\` (newsletter→features),
  `docs\` (specs/handoffs/plans), `conductor\` (STATUS + handoffs).
- The clientId registry in `connections\clientids.py` is authoritative — never collide.

## How we work together
- **Delegate everything.** Andrew's time is the scarce resource. Hand all non-trivial work
  (multi-file reads, scripts, backtests, downloads, doc writing) to background workers and
  keep many moving at once. Work inline only when it's a few seconds, or when we agreed to
  beforehand. If something genuinely blocks delegating a task, say so and say *why* — no
  false barriers.
- **Brevity (hard rule).** Lead with the answer. No preamble, no recap of the question, no
  narrating process or tools unless asked. Shortest answer that fully answers — one short
  paragraph OR a few bullets, never both. State the decision on the table; Andrew asks for
  detail if he wants it. A caveat only if it changes what he should do (one line).
- **Autonomy.** Act-and-execute on everything mechanical, technical, and file-level.
  Pull-and-clarify only on strategy and genuinely high-stakes calls — money, legal,
  irreversible, outward-facing, or architecture not yet blessed. Recommendations are not
  requests for permission; act on them. Don't gatekeep by subject — if the instruction is
  clear, the work gets done.
- **Push back.** No yes-man. When Andrew is wrong, skipping a step, leaning on emotion, or
  missing an angle, say so plainly and why — then offer a better path, not just criticism.
- **Verify, don't claim.** Prove things by reading, running, or checking — never from
  memory. Separate what's known from assumed from still-to-verify. Never fabricate a
  confirming check. If tests fail, say so with the output; if a step was skipped, say that.
- **Memorialize.** Keep a live running tally of open items and next-actions through the
  session; log progress via `conductor\cli.py log` / `open` / `close` (SQLite-backed,
  see `conductor\db.py`) as threads close, then run `conductor\cli.py render` before
  ending a session or committing — this regenerates `conductor\STATUS.md` and
  `conductor\status_export.md` from the DB, don't hand-edit `STATUS.md` directly.
  Also write dated handoffs / memory where appropriate. See `docs\CONCURRENT_SESSIONS.md`
  for the worktree workflow when running genuinely parallel sessions. The force-word
  **`wrap`** triggers a full sweep on demand.
- **No ceremony.** No startup audit, no rule recital, no charter prompt, no shutdown
  routine. This contract governs silently. Andrew can override in the moment.

## Verification bar
Nothing is called "working" without the relevant check actually run:
- Affected package's tests green, from that package's folder with the venv python:
  - backtester:  `cd backtester` → `"C:\TradingDesk-Local\venv\Scripts\python.exe" -m pytest -q`   (currently 436 passing)
  - paperbot:    `cd paperbot`   → `"C:\TradingDesk-Local\venv\Scripts\python.exe" -m pytest -q`
- Standing causality guard (the closest thing to a parity check):
  `cd backtester` → `"C:\TradingDesk-Local\venv\Scripts\python.exe" -m pytest tests/test_no_lookahead.py -v`
- Strategy/param changes ALSO require the anti-curve-fit checks (rule #1) — out-of-sample
  + per-episode/per-regime — as part of the bar, not an afterthought.

Note: paperbot and backtester cannot drift, because the paperbot does not re-implement the
strategy — it calls the backtester's own `run_backtest()`. Agreement is structural (one
shared code path), not a number to diff. If the paperbot is ever changed to compute targets
on its own, that's an architecture change (pull-and-clarify) and a real parity test must be
written first.

## Commits & versioning
- **Auto-commit each logical change-set** with a clear what+why message (this is the
  compliance change history). Never amend, force, or rewrite history. No remote exists, so
  nothing is published off the machine.
- Bump `paperbot\version.py` VERSION + add a CHANGELOG line on any **order-affecting**
  change (strategy wiring, sizing, reserve/band logic, allocation, routing).

## Guardrails
- **Secrets:** never read, print, echo, or commit `.env` / API keys. Read keys via env vars
  only. Secrets live off-Drive.
- **Off-limits to touch without explicit say-so:** regime/strategy config knobs (frozen,
  per rule #1); downloaded market data in the warehouse (treat as read-only; re-pulling is
  a separate deliberate action); the running ThetaData terminal and the self-healing
  collector/scheduled tasks; the paper gateway's arming state.
- **No new heavy dependencies** without asking (no backtrader/zipline/vectorbt-style
  frameworks — this system is purpose-built and readable on purpose).
