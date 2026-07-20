# S0 Live-Account Reconciliation / Corp-Action Guard — Fix Spec (FOR REVIEW)

**Status:** proposed, not built. Written 2026-07-20. Author: desk session (main).
**Gate:** prerequisite #3 for arming S0 on a live account (conductor #41). It does **not**
flip `PILOT_MODE`, does **not** touch any strategy/regime/band/sizing knob (rule #1 clean —
this is reconciliation plumbing + a safety gate, not a strategy change), and does **not**
change the review → arm → transmit gate. It **is** order-affecting (it changes which orders the
engine will and won't emit), so it carries a `version.py` bump and needs Andrew's blessing before
any code.

---

## 1. Problem (verified 2026-07-20)

S0 already reconciles to **real broker positions every cycle** — `rebalance_run.py:310` reads
`ib.positions(account)` live (not a cached model state), and `reconcile.reconcile`
(`reconcile.py:39`) classifies each ticker MATCHED / DRIFTED / MISSING / UNTRACKED against the
tier target. That mechanism is sound and stays. The gap is what a **live** account's drift does
to that path — drift sources the PAPER account (DU…141) never exercises: reinvested dividends
(DRIP), corporate actions (spinoffs, in-kind distributions, ticker renames), and a real cash /
money-market sweep balance.

Verified findings (file:line):

1. **`UNTRACKED` conflates two economically opposite cases.**
   `reconcile.py:65` classifies *any* held symbol with model weight 0 as `UNTRACKED`. That single
   bucket contains both:
   - **A legitimate model rotation-out** — a ticker the strategy *does* know (∈ `config.ALL_TICKERS`)
     that this cycle's model weights at 0 (e.g. the regime rotated out of `TLT`). This **should**
     be sold; it is normal rebalancing.
   - **An alien holding** — a symbol the strategy has *never* known (∉ `ALL_TICKERS`): a spun-off
     security, a renamed ticker, a manual position, or a cash-sweep fund. On a live account this
     is real value that must **not** be auto-liquidated by the bot on its own say-so.

   The classifier cannot currently tell them apart, so the safety gate below cannot either.

2. **A whole-share `UNTRACKED` holding auto-liquidates.**
   `band_breached` returns True for *any* `UNTRACKED` line (`rebalance_engine.py:89`), and
   `plan_account` then emits `delta = target_shares - int(actual_shares)` for every line
   (`rebalance_engine.py:146`). For an `UNTRACKED` holding `target_shares == 0`, so the delta is a
   **sell of the entire position**. On paper that is the intended behavior (sweep leftover test
   positions). On live it means the bot would auto-sell a spun-off security, or — on a ticker
   rename — sell the renamed holding (`UNTRACKED`) while separately *buying* the "MISSING" target
   symbol, i.e. churn a rename into a taxable round-trip.

3. **Fractional-only DRIP leftovers perpetually flag.**
   `rebalance_engine.py:146` uses `int(actual_shares)`, truncating fractional shares. A
   fractional-only leftover (e.g. 0.6 sh of a DRIP, model weight 0) is `UNTRACKED` → it breaches
   the band every cycle (finding #2) — but `int(0.6) == 0` → delta 0 → no route is built. The
   nightly monitor then hits its "band breach but no routes (all UNTRACKED-only) — nothing to
   stage" path (`nightly_monitor_run.py:314-318`) and emits that alert **every single night**, a
   standing false page on live.

4. **No paper-vs-live distinction exists to scope any of this.**
   The engine is hard-guarded to paper today — `config.ACCOUNT_SUFFIX = "141"` (`config.py:25`)
   makes it refuse any account not ending in 141. The live single-account S0 test uses a *different*
   account (stood up under prerequisite #4), so there is no existing flag on which to scope a
   "live accounts get the review treatment, paper keeps auto-sweep" rule.

**Ruled out (not a gap):**
- **Stock splits.** NAV-neutral (shares ×N, price ÷N) and reconcile keys on *weight*, so a split
  never falsely trips the band. Already correct — no change.
- **Reading live positions.** Already done every cycle (`rebalance_run.py:310`). This spec adds
  *classification + a gate*, not position-reading.

---

## 2. Invariant to establish

> **On a live account, every cycle each held symbol is reconciled against BOTH the model target
> AND the strategy's known universe (`config.ALL_TICKERS`). The engine auto-trades only symbols
> the strategy knows — including a legitimate rotation-out to 0% — and NEVER auto-liquidates an
> alien / corporate-action / manual holding: it surfaces that holding for human reconciliation and
> transmits nothing against it. A fractional-only leftover neither triggers a rebalance nor
> perpetually alerts. Paper-account behavior is unchanged.**

Fail **closed**: an alien holding we cannot prove is safe to auto-trade is left in place and
alerted, never swept. Legitimate model rotations must still execute — the gate must not freeze real
rebalancing (that would be its own failure).

---

## 3. Design

### 3.A Refine the reconcile classification (universe-aware)

`reconcile.reconcile` gains an optional `universe: set[str] | None` parameter (the strategy's
tradeable symbols; `None` preserves today's behavior verbatim, so paper/backtester callers are
untouched). The single `UNTRACKED` status splits into three precise ones:

| New status   | Condition (held, model weight 0)                     | Meaning                          |
|--------------|------------------------------------------------------|----------------------------------|
| `SWEEP`      | symbol is the cash symbol or ∈ `config.SWEEP_WHITELIST` | money-market sweep held by design → not `ALIEN`, no order, no page |
| `FRACTIONAL` | `int(actual_shares) == 0` and `actual_shares != 0`   | DRIP stub → record, do not action    |
| `ROTATE_OUT` | symbol ∈ `universe`, `int(actual_shares) ≥ 1`        | model dropped it → **sell** (normal) |
| `ALIEN`      | symbol ∉ `universe` (and not sweep / cash / whitelist) | corp-action / manual → **review**    |

Precedence is top-to-bottom (as implemented in `reconcile.classify_untracked`): a whitelisted
symbol is `SWEEP` even if fractional; a sub-1-share stub is `FRACTIONAL` before universe membership
is consulted (you can't trade a sub-share lot either way); otherwise a known symbol is `ROTATE_OUT`
and everything else is `ALIEN`. `MATCHED` / `DRIFTED` / `MISSING` are unchanged. When
`universe is None`, all four collapse back to today's `UNTRACKED` (behavior-preserving default).
`classify_untracked` is the single predicate; `reconcile.alien_holdings(positions, universe,
whitelist)` reuses it so the morning executor's positions-only alien check cannot drift from it.

### 3.B Gate the engine on the new statuses (account-type-independent — Q1 UNIFY)

`band_breached` and `plan_account` (`rebalance_engine.py`) key off the reconcile **status** only —
there is **no** per-account flag (Q1 = UNIFY removed the earlier draft's `auto_clear_untracked`).
The universe-based split does all the work, identically on paper and live:

- `ROTATE_OUT` / `UNTRACKED` — always breach the band and emit the sell delta (a known dropped
  ticker must clear regardless of size). This is the load-bearing "don't freeze real rebalances"
  behavior. (`UNTRACKED` is the `universe=None` equivalent of `ROTATE_OUT`.)
- `ALIEN` — does **not** breach the band by itself and emits **no** delta. It is collected onto
  `AccountPlan.alien_lines` for the per-cycle review alert (§3.E). A cycle whose only off-model line
  is an alien holding stages nothing and is not "dirty" — it is "needs review", never a false "band
  breach, no routes" page.
- `FRACTIONAL` / `SWEEP` — never breach, never emit a delta, never trigger the "band breach, no
  routes" alert. Recorded in the reconciliation readout, not actioned. (`FRACTIONAL` is a
  truncation-seam correctness fix; `SWEEP` is a whitelisted cash/MMF held by design.)

The engine's shared frozensets `_NO_AUTOTRADE_STATUSES = {ALIEN, FRACTIONAL, SWEEP}` and
`_ALWAYS_BREACH_STATUSES = {UNTRACKED, ROTATE_OUT}` are the single definitions, replicated
byte-identically in `recon_report.plan_account` (which imports `AccountPlan` **from** recon_report,
so importing the engine back would be circular).

### 3.C Fix the fractional truncation seam

The `int(actual_shares)` at `rebalance_engine.py:146` and `_trade_weight`
(`rebalance_engine.py:75`) stays for delta *sizing* (we can only trade whole shares), but the
`FRACTIONAL` classification (§3.A) is computed from the raw float so a sub-1-share stub is
recognized and suppressed from the breach path rather than looping through it forever.

### 3.D Live-account scoping — NOT NEEDED (Q1 = UNIFY)

The earlier draft introduced a `LIVE_ACCOUNTS` set and derived a per-account
`auto_clear_untracked` flag. Reviewer decision **Q1 = UNIFY removed both**: behavior is
account-type-independent, so no paper/live distinction is needed at all. The only "scoping" is
whether a `universe` is threaded into `build_plan`/`plan_accounts` — the S0 multi-account path
passes it (via `strategy.universe()`), so paper and live get the identical review treatment; every
other caller leaves it `None` and gets the byte-identical legacy `UNTRACKED` behavior. `config`
gains only `SWEEP_WHITELIST` (default empty), not a `LIVE_ACCOUNTS` set.

### 3.E Surface alien holdings for human reconciliation

The nightly monitor and morning execute emit a distinct **"CORP-ACTION / UNTRACKED REVIEW"** alert
whenever a live account has ≥1 `ALIEN` line, listing per holding: symbol, quantity, and estimated
value, plus the three human actions available — (a) manually sell it via
`rebalance_execute.py --arm-i-understand`, (b) update `ENROLLMENT` / the model if it belongs, or
(c) accept and whitelist it. This alert is informational; it must **not** block the account's
legitimate `ROTATE_OUT` / `DRIFTED` / `MISSING` rebalancing, which proceeds normally in the same
cycle.

---

## 4. Failure modes → behavior (acceptance matrix)

| Scenario (live account unless noted)                                   | Required behavior                                   |
|------------------------------------------------------------------------|-----------------------------------------------------|
| Model rotates a known ticker (∈ universe) to 0%, whole shares held     | `ROTATE_OUT` → sell emitted (rebalances normally)   |
| Spinoff deposits a new alien symbol (∉ universe), 10 shares            | `ALIEN` → **no order**, review alert                |
| Ticker rename: old symbol held (∉ universe), new symbol MISSING        | old = `ALIEN` (no sell), new = `MISSING`; alert, **no churn** |
| DRIP leaves 0.6 fractional shares of a dropped holding                 | `FRACTIONAL` → no breach, no delta, **no nightly false page** |
| Alien holding present **and** a genuine model drift on another sleeve  | drift rebalances; alien is left in place + alerted  |
| Cash / money-market sweep balance shows as a position                  | whitelisted → not `ALIEN`, no alert                 |
| Same alien symbol still held next cycle (unreviewed)                   | alert re-emitted; still no auto-sell (fail-closed)  |
| **Paper** account holds a leftover UNTRACKED test position             | auto-swept as today (unchanged) *(pending open Q1)* |
| Split doubles a held position                                          | `MATCHED`/in-band by weight — no action (unchanged) |
| `universe` unavailable / `None`                                        | collapses to today's `UNTRACKED` (behavior-preserving) |

Each row becomes a test in a new `paperbot/test_live_reconciliation.py` with synthetic
`account_inputs` (the engine is already dict-driven and broker-free per `rebalance_engine.py:161`,
so no fake `ib` is needed for the classification/gate logic).

---

## 5. Scope of change

- `reconcile.py` — add the `universe` parameter and the `ROTATE_OUT` / `ALIEN` / `FRACTIONAL`
  classification; `None` preserves current behavior exactly.
- `rebalance_engine.py` — `band_breached` / `plan_account` / `plan_accounts` consult
  `auto_clear_untracked`; `ROTATE_OUT` sells, `ALIEN` (live) is held + collected, `FRACTIONAL`
  suppressed. Thread the per-account flag through `plan_accounts`.
- `paperbot` config — a new `LIVE_ACCOUNTS` set (default empty) and a canonical way to obtain the
  strategy universe (`config.ALL_TICKERS` via `strategy_target`, or a `strategy.universe()`
  accessor — open question 2).
- `nightly_monitor_run.py` / `morning_execute_run.py` — emit the corp-action review alert; ensure
  an alien-only live cycle is "needs review," not a false "band breach, no routes" page.
- `recon_report.py` — show the new statuses in the standalone read-only reconciliation readout.
- `paperbot/version.py` — bump VERSION + CHANGELOG (order-affecting: which orders the engine emits).
- Tests — the §4 matrix (`test_live_reconciliation.py`).

**Out of scope (explicit):** any strategy / regime / band / sizing knob (rule #1 frozen); flipping
`PILOT_MODE`; the live gateway + clientId + enrollment stand-up (prerequisite #4, separate item);
auto-executing any corp-action decision (a human always decides what happens to an `ALIEN`
holding); DRIP fractional *trading* (we surface the stub, we don't try to trade sub-1-share lots).

---

## 6. Reviewer decisions — RESOLVED (2026-07-20) and BUILT

All four were resolved by the reviewer and are implemented on branch
`thread/s0-live-reconciliation` (paperbot v0.18.0).

1. **Paper behavior — UNIFY.** The alien-holding → review behavior applies on **both** paper and
   live, identically. This *simplified* the design: there is **no** per-account
   `auto_clear_untracked` flag and **no** `LIVE_ACCOUNTS` set (both were in the earlier draft of
   §3.B/§3.D and are now removed). The universe-based split does all the work everywhere and is
   account-type-independent — a symbol the strategy knows that the model dropped is `ROTATE_OUT`
   (sell, on paper and live alike); a symbol it never knew is `ALIEN` (review, on paper and live
   alike). Behavior is still scoped by whether a `universe` is passed at all: `universe=None`
   (backtester and any caller that doesn't pass it) reproduces today's single `UNTRACKED` bucket
   bit-for-bit, so nothing off the S0 multi-account path changes.
2. **Universe source — the `strategy.universe()` accessor.** Added `universe()` on
   `StrategyBase` (which refuses by default — a strategy must self-describe) and on
   `AdaptiveAllWeather` / a module-level `strategies.all_weather.universe()`, returning
   `set(config.ALL_TICKERS)`. Verified a superset of every sleeve's output (parts/sector.py →
   `EQUITY_CORE + SECTORS`; parts/defensive.py + portfolio `_BUCKET_OF` → `DEFENSIVE_ASSETS`;
   parts/real_assets.py → `REAL_ASSETS`; the best-tbill fallback `BENCHMARK_TBILL ∈ TBILLS`), so no
   sleeve can emit a symbol outside it. paperbot reads it via the accessor, not a hardcoded
   duplicate — the durable seam a future added sleeve/ticker updates in one place.
3. **Fractional stub disposition — a quiet readout line.** `FRACTIONAL` stubs (held, model weight 0,
   `int(shares)==0`, `shares!=0`) are suppressed from the breach/page path entirely and appear only
   as a quiet line in `recon_report`'s read-only readout. They never alert or page.
4. **Cash-sweep whitelist — mechanism built, default empty (discover-then-whitelist).**
   `config.SWEEP_WHITELIST` (a `set[str]`, default **empty**) names symbols excluded from `ALIEN`,
   **extending** the existing `reconcile._investable.CASH_SYMBOL` anchor (which is always excluded).
   No sweep symbol is hardcoded — Andrew names the live account's exact sweep ticker(s) once the live
   account is known (ties to prerequisite #4) and adds them here. A whitelisted symbol classifies
   `SWEEP` (never `ALIEN`): no breach, no order, no page.
