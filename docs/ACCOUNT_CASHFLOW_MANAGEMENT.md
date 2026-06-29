# Cashflow-Aware Account-Management Layer — Requirements + Design SPEC

**Status:** BACKLOG / SPEC — nothing built or changed. This captures requirements and a proposed design for Andrew to react to.
**Scope:** PAPER account only (DU8922142–146 under FA master DF8922141). No real-money configuration exists in this project.
**Date:** 2026-06-29

---

## Executive summary

Today the paperbot sizes every account to `(NetLiq − distribution_reserve) × (1 − 5% cash_reserve)`, which means a *correctly* invested account silently reads ~5% under its model on every position — the cash buffer is smeared across all holdings as a haircut rather than held as its own line, so the dashboard reports it as "drift." This SPEC proposes a **cashflow-aware account-management layer** that does five things: (1) shrinks the structural cash buffer from 5% to ~1–1.5%; (2) reframes cash as an **explicit allocation bucket** (a real ~1.5% CASH target in the tier model) so risk positions hit their TRUE model weights and the dashboard's "drift" becomes honest; (3) adds a **decumulation fail-safe** that reserves an upcoming withdrawal *before* investing the rest so a client is never left short; (4) adds an **accumulation trigger** that detects fresh deposits and rebalances promptly so new cash gets invested; and (5) wraps all of it in a **per-account monitoring layer** that watches cash-vs-expected, withdrawals-due, deposits-arrived, and drift/band status, then decides per account to rebalance / hold / alert. IB has no native cashflow-aware rebalancing, so we own this. Andrew's architectural point: this monitor is the *same substrate* future algorithmic-trade management will need, so it is worth designing once to serve both account maintenance and algo execution.

---

## Current state (shared context for all 5 items)

The investable-capital formula is implemented in three mirrored places (they must stay in lockstep):

| Location | What it does |
|---|---|
| `paperbot/config.py:50` | `RISK_LIMITS["cash_reserve_pct"] = 0.05` — the structural buffer knob. |
| `paperbot/config.py:98` | `REBALANCE_BAND_PCT = 0.03` — the no-trade band. |
| `paperbot/rebalance_engine.py:48-65` | `compute_investable(net_liq, reserve, cash_reserve_pct)` → `investable = (net_liq − reserve) × (1 − cash_reserve_pct)`, floored at 0. This is the canonical implementation. |
| `paperbot/rebalance_engine.py:94-95` | `plan_account` calls `cashflows.reserve_for(account, net_liq)` then `compute_investable(...)`. |
| `paperbot/recon_report.py:73` | Re-derives the same formula inline (read-only readout). |
| `paperbot/reconcile.py:47-48` | Default `investable = nav × (1 − cash_reserve_pct)` when no override is passed. |
| `paperbot/execution_engine.py:107` | Single-account path: `investable = nav × (1 − cash_reserve_pct)`. |
| `paperbot/cashflows.py` | Owns the per-account `SCHEDULE` (currently empty) + `reserve_for()` = `RESERVE_MONTHS (=1) × monthly distributions`. The "reserve=0 unless a cashflow is scheduled" hook already exists here. |

**The haircut mechanic (the root of the "drift" complaint):** `reconcile.reconcile` (`reconcile.py:55`) sizes `target_shares = int(weight × investable / price)` against the *reduced* investable, but computes `drift_weight = actual_weight − weight` (`reconcile.py:58`) against the *full* model `weight`. So a fully, correctly invested account sits ~`cash_reserve_pct` under its raw model weight by construction on every holding. The engine already works around this for the no-trade band by keying the breach test on **trade size vs NetLiq**, not raw drift (`rebalance_engine.py:114-122`, mirrored at `recon_report.py:88-96`) — but the **dashboard does not**: `dashboard/app.py:668` calls `recon.reconcile(tgt, info.net_liq, positions)` with no `investable` override and renders `drift_weight` directly (`app.py:673`), labelling the account "drift present" (`app.py:679, 684`). That false-positive drift display is the L7 naming bug this SPEC ties into item #2.

---

## Item 1 — Shrink the structural cash reserve from 5% to ~1–1.5%

**(a) Current state.** `config.py:50` — `"cash_reserve_pct": 0.05`. Changing this one knob propagates everywhere through the formula in the table above (engine, recon report, reconcile default, execution engine). The comment frames it as "no leverage + a buffer." Strategy-level cash (the All-Weather model's natural SGOV/T-bill sleeve) is **separate** from this — `cash_reserve_pct` is an *additional* haircut on top of whatever cash the model already targets.

**(b) Requirement (plain English).** The 5% buffer is too fat — it parks a large chunk of NAV out of the market purely as an operating cushion. Shrink it to ~1–1.5%. Keep just enough to absorb fills, commissions, and price slippage between sizing-time and fill-time so a rebalance never tries to spend money the account doesn't have.

**(c) Proposed design.** Drop `cash_reserve_pct` to `0.015` (1.5%) as a first cut. This is purely an operating float — it is **not** the risk-cash sleeve (that's the model's own SGOV/T-bills) and **not** the distribution reserve (item #3). Once item #2 makes cash an explicit modeled bucket, this structural knob can shrink even further (toward ~0.5–1%) because it would then only need to cover *execution* slack, not stand in for cash allocation. Recommend the two be considered together: pick the explicit CASH bucket (#2) first, then set this residual buffer to the smallest value that reliably prevents over-spend.

**How small is safe?** The buffer must cover, per rebalance: (i) commissions (negligible on paper, ~$1/order on real), (ii) the gap between the limit price the engine sizes against and the actual fill (the ladder caps this at `ORDER_CAP_K = 0.003` = 30 bps over the touch — `config.py:134`), and (iii) whole-share flooring slack (`int()` always rounds *down*, so flooring frees cash, it never overspends). The dominant term is (ii). A 1% buffer covers a 30-bps marketable cap with ~3× headroom across a whole book. **1% is defensible; 1.5% is comfortable.** Below ~0.5% the marketable-cap slippage on a large illiquid leg could in principle nip the buffer.

**(d) Open questions / decisions for Andrew.**
- Target value: **1.0% or 1.5%?** (Recommend 1.5% as the standalone change, then revisit toward 1.0% after #2 lands.)
- Should the buffer be a flat pct, or a small **floor in dollars** (e.g. `max(1% × NAV, $X)`) so tiny accounts still keep a minimum cushion? (Not relevant at ~$1.1M paper subs, but matters if account sizes diverge.)
- Confirm: this change touches `config.py:50` only; all 6 call sites read from it, so a single edit propagates. No formula change required for item #1 alone.

---

## Item 2 — Make cash its own explicit category, not a haircut on every position

**(a) Current state.** Cash is a **residual**, not a line. `reconcile.py:55` sizes positions to `weight × investable` where `investable < NAV`; the leftover NAV is just whatever didn't get allocated. There is no `CASH` row in the tier model (`strategy_target.Target.weights` is the strategy's risk weights, summing to ~1.0 — `strategy_target.py:52-53`). The dashboard then measures drift against the full model weight and reports every holding ~`reserve%` light as "drift" (`dashboard/app.py:668-686`). Cash effectively "robs" each bucket by ~1% and the report calls it drift.

**(b) Requirement (plain English).** Reframe cash as an **intentional holding**. The tier model should include an explicit ~1.5% CASH target. Real risk positions then size to their **true model weights** (not a haircut version), and the cash bucket sits **on target** as its own line. "Drift" reporting becomes honest: a correctly invested account shows ~0% drift everywhere including cash, instead of a phantom ~1% under-weight smeared across every holding.

**(c) Proposed design.**

1. **Model schema.** Add an explicit `CASH` (or reuse the model's existing cash-equivalent sleeve symbol, e.g. SGOV) target weight inside each tier model so the weights still sum to 1.0 *with* cash counted. Two implementation choices:
   - **2a — Modeled cash symbol (preferred):** fold the operating cash into the model's existing SGOV/T-bill sleeve weight (the All-Weather model already holds a cash-equivalent). Cash becomes a real, yield-bearing, reconcilable position. Nothing in the sizing math changes except that `investable` becomes `NetLiq − distribution_reserve` (no `× (1 − cash_reserve_pct)` haircut) because the cash target now lives **inside** the weights.
   - **2b — Synthetic CASH line:** add a literal `CASH` pseudo-symbol at ~1.5% weight that the engine sizes to dollars-held (not shares) and reconciles as "uninvested cash on hand." More bookkeeping; lets the operating float stay as literal cash rather than SGOV.

2. **Share-sizing math.** Today: `target_shares = int(weight × (NAV − reserve) × (1 − cash_reserve_pct) / price)`. Proposed: `target_shares = int(weight × (NAV − reserve) / price)`, where the model `weight` set now *includes* the cash bucket (so risk weights are the strategy's true weights and the cash weight absorbs the operating float). Net effect: risk positions get bigger (hit true model weight) and the cash bucket is an on-target line, not a leftover.

3. **Dashboard / L7 naming fix (tie-in).** With explicit cash, `dashboard/app.py:668` can drop the false drift: either pass `investable` so sizing and drift agree, or simply report against the explicit cash-inclusive model so a correctly invested book reads "Aligned." The "drift present" label (`app.py:679,684`) then means *real* drift. This is the honest-reporting payoff the reframe buys.

**(d) Open questions / decisions for Andrew.**
- **2a (fold into SGOV sleeve) vs 2b (synthetic CASH line)?** 2a is cleaner and keeps the buffer yield-bearing; 2b keeps a literal-cash cushion. Recommend **2a**.
- Where does the explicit cash weight live — in the **shared `strategies` brain** (so backtester + paperbot agree, preserving byte-parity) or as a **paperbot-only post-processing layer** on top of the strategy weights? Folding it into the shared brain changes backtest results and risks the byte-parity invariant (memory: `strategy-core-shared-brain`); a paperbot-side cash bucket keeps the strategy untouched but means paper ≠ backtest by a small cash sleeve. **This is the load-bearing decision.** Recommend paperbot-side (operating cash is an execution concern, not a strategy signal).
- Does the explicit cash bucket **replace** `cash_reserve_pct` entirely, or coexist with a tiny residual buffer (item #1)? Recommend: explicit cash bucket carries the intentional cash; a small residual `cash_reserve_pct` (~0.5–1%) stays purely for execution slack.
- Confirm all 6 call sites get updated together (the formula is mirrored — see shared-context table) to avoid the engine and the readout disagreeing.

---

## Item 3 — Withdrawal-account (decumulation) fail-safe

**(a) Current state.** The hook exists and is wired but unused. `cashflows.py` owns `SCHEDULE` (per-account list of `Flow(kind, amount, pct_nav, day, note)`), currently **empty** (`cashflows.py:40-43`). `reserve_for(account, nav)` = `RESERVE_MONTHS (=1) × sum(monthly distributions)` (`cashflows.py:51-56`). The engine already subtracts this reserve **first**, before the structural buffer: `investable = (NetLiq − reserve) × (1 − cash_reserve_pct)` (`rebalance_engine.py:58, 94-95`). So the plumbing to carve out a withdrawal already runs — there is just no schedule data in it yet. "reserve = 0 unless a cashflow is scheduled" is the current behavior by construction (empty `SCHEDULE` → `reserve_for` returns 0).

**(b) Requirement (plain English).** For accounts taking monthly withdrawals/income: a rebalance must **reserve enough cash for the upcoming distribution(s) before investing the rest**, so the engine never invests money the client needs for next month's payment and never has to fire-sell to fund a withdrawal.

**(c) Proposed design.** Build on the existing hook — most of this is data, not code.
- **Where the schedule lives:** `cashflows.SCHEDULE` (already the right home). Populate per withdrawal account with `Flow("distribution", amount=…, day=…)`. Consider promoting this to a per-account **profile** (a small dataclass: tier version + cash flows + buffer-months override) if per-account policy grows; for now the `SCHEDULE` dict is sufficient.
- **How many months to hold:** `RESERVE_MONTHS = 1` is the never-caught-short default for regular monthly income. Spec a **buffer option**: hold 1 month always, optionally 1.5–2 months as a date-proximity ramp (reserve grows as the distribution day approaches) so capital isn't idle all month. Start with a flat 1 month; ramp is a later refinement.
- **Composition with the structural reserve (#1/#2):** the order is already correct and should stay — **distribution reserve comes out first** (`NetLiq − reserve`), *then* the small structural/operating buffer applies to the remainder. The distribution reserve is client-specific and intentional; the operating buffer is execution slack. They are different dollars and must not be conflated. With item #2's explicit cash bucket, the distribution reserve naturally lands in the model's cash sleeve (yield-bearing) rather than idle cash.
- **Engine subtraction:** no new code — `compute_investable` already does `(net_liq − reserve) × …`. The work is (i) fill `SCHEDULE`, (ii) optionally add the ramp/buffer-months knob, (iii) surface the reserve on the dashboard as an explicit "withdrawal reserve" line so it's visibly carved out, not hidden.

**(d) Open questions / decisions for Andrew.**
- **Buffer months:** flat 1 month, or 1 + a date-proximity ramp? (Recommend flat 1 to start.)
- **Schedule source of truth:** keep hand-maintained `cashflows.SCHEDULE`, or eventually drive it from IB's recurring-withdrawal feature if one exists? (Memory `ibkr-model-portfolio-api-limit` flagged a TODO to check whether IBKR supports recurring scheduled withdrawals to pair with the reserve — still open.)
- **Boundary (confirm):** the engine guarantees cash is *available*; the actual outbound ACH/wire stays a human/IB step. The bot reserves, it does not disburse.
- Per-account profile dataclass now, or keep the flat `SCHEDULE` dict until policy complexity demands it?

---

## Item 4 — Deposit-account (accumulation) trigger

**(a) Current state.** No deposit detection exists. `cashflows.py` models contributions (`Flow("contribution", …)`, `monthly_net_flow` at `cashflows.py:59-65`) for *reporting* only — contributions add **nothing** to the reserve (`reserve_for` ignores them, by design: incoming cash needs no reserve). The only thing that triggers a rebalance today is the **drift band**: an account is touched only when some holding's required trade exceeds `REBALANCE_BAND_PCT = 3%` of NAV (`rebalance_engine.py:121-122`). A fresh deposit lands as idle cash and **waits** until drift happens to trip the band — new cash can sit uninvested.

**(b) Requirement (plain English).** For accounts making monthly deposits: **detect when fresh cash arrives and trigger a rebalance promptly** (more frequent rebalances when warranted) so new money gets invested instead of waiting for drift. Guard against over-trading on trivial cash moves.

**(c) Proposed design.**
- **Detection (three options, combinable):**
  1. **Cash-balance vs baseline (preferred, robust):** the monitor (item #5) persists each account's last-known settled cash in `STATE_DIR` (`config.py:59`). On each poll, compare current `TotalCashValue`/`SettledCash` (from `ib.accountSummary`) to the baseline; a jump beyond a threshold = a deposit arrived.
  2. **Known deposit schedule:** `cashflows.SCHEDULE` already carries `contribution` flows with a `day`. Use the schedule to *expect* a deposit on/after `day` and confirm it landed via option 1 (schedule says "expect ~$X around day 15"; cash check confirms).
  3. **IB account-value deltas:** watch `NetLiquidation`/cash deltas across polls; a deposit shows as a cash inflow with no corresponding fill.
  - Recommend **1 + 2 together:** schedule sets the expectation, cash-delta confirms the actual arrival (handles early/late/missed deposits without false triggers).
- **Trigger logic:** when a confirmed deposit ≥ threshold lands, mark the account "rebalance due — fresh cash" and let the monitor invest it on the next cycle (subject to the usual review→arm→transmit gate). This effectively makes rebalance cadence event-driven for accumulation accounts instead of purely drift-driven.
- **Over-trading guards:** require the new cash to exceed **both** an absolute floor (e.g. ≥ $X) **and** a NAV fraction (e.g. ≥ the existing `REBALANCE_BAND_PCT` of NAV, ~3%) before triggering — so dividend drips, interest, and rounding cash never trip a rebalance. Also debounce: at most one deposit-triggered rebalance per account per day.

**(d) Open questions / decisions for Andrew.**
- **Deposit threshold:** reuse `REBALANCE_BAND_PCT` (3% of NAV) as the trip level, or a separate, smaller deposit threshold (deposits are "good" cash you *want* invested, so maybe a lower bar than drift)?
- **Which cash tag** to baseline against — `TotalCashValue`, `SettledCash`, or `AvailableFunds`? (Settled is safest to avoid acting on unsettled inflows.)
- Should a deposit trigger an **immediate** rebalance or just **arm/alert** for the next scheduled monitor cycle? (Recommend alert-then-arm — stays inside the human gate.)
- Distinguish a *deposit* (external cash in) from a *fill-driven* cash change — a sale also raises cash. The "no corresponding fill" check (option 3) disambiguates; confirm that's reliable on the paper feed.

---

## Item 5 — The per-account monitoring layer that ties it together

**(a) Current state.** No monitoring layer exists. The pieces are all **on-demand, manually run, single-shot**: `recon_report.py` (read-only drift readout), `rebalance_run.py` (dry-run runner), `rebalance_execute.py` (armed executor, Monday). The dashboard (`dashboard/app.py`) shows live drift but is a viewer, not a decider. Nothing watches cash-vs-expected, withdrawals-due, or deposits-arrived on a schedule. IB itself has **no native cashflow-aware rebalancing or scheduled-flow handling** (memory `ibkr-model-portfolio-api-limit`: allocation groups split one order; they do not do per-account cashflow rebalancing) — so we own the whole monitoring substrate.

**(b) Requirement (plain English).** Build a **per-account monitoring layer** that, on a schedule, watches each account's: cash balance vs expected, withdrawals due (and whether the reserve covers them), deposits arrived, and drift/band status — then **decides per account: rebalance / hold / alert.** It is the single brain that fires items #3 and #4 and surfaces #1/#2 honestly.

**(c) Proposed design.**
- **One pure decision function per account** that takes (positions, NAV, cash, schedule, baseline) and returns a verdict: `HOLD` / `REBALANCE(reason)` / `ALERT(reason)`. Reasons: `DRIFT_BAND_BREACH`, `DEPOSIT_ARRIVED`, `WITHDRAWAL_DUE_UNRESERVED`, `CASH_BELOW_EXPECTED`. This composes the existing pure pieces: `reconcile.reconcile` (drift), the trade-size band test (`rebalance_engine.plan_account`), `cashflows.reserve_for` (withdrawal coverage), and the new deposit/cash-baseline check (#4). Keep it **pure** like `rebalance_engine` — it reads state, decides, and emits a verdict; it transmits nothing. The existing review→arm→transmit gate stays the only path to an order.
- **What it watches, per account, per cycle:**
  - cash balance vs expected baseline (deposit detection — #4)
  - upcoming withdrawal vs current reserve (is the distribution covered? — #3)
  - drift/band status (existing trade-size-vs-NAV band)
  - flags `ALERT` if cash is unexpectedly low, a withdrawal is due but unreserved, or an untracked position appears.
- **Where it runs:** a **scheduled task**, modeled on the existing EOD report / collector-watchdog pattern (Task Scheduler, CT) referenced throughout STATUS. New clientId from the registry (`connections/clientids.py`; current paperbot lane uses 30/34/35/36/37/38 — pick the next free, e.g. 39). State (baselines, last-decision, last-deposit-seen) persists in `STATE_DIR` (`config.py:59`, off-Drive on local C:). Output = a per-account verdict table to the dashboard + the EOD digest; **PAPER-only**, transmits nothing on its own.
- **Andrew's architectural point — design once, serve twice.** This account-monitor is the **same substrate** future algorithmic-trade management will need: a scheduled per-entity loop that reads state, evaluates rules, and emits gated actions (rebalance now; later: enter/exit/adjust an algo position). Build the monitor with a **generic "watch → decide → gated-act" core** and account-maintenance as the first consumer, so the algo-execution layer plugs into the same loop rather than being a second, competing scheduler. This avoids the "two programs that fight" failure mode flagged in `ibkr-model-portfolio-api-limit` for the cashflow layer — one monitor, many rule-sets.

**(d) Open questions / decisions for Andrew.**
- **Cadence:** EOD once-daily (matches the report), intraday (e.g. every N minutes during RTH for deposit/withdrawal responsiveness), or both (daily full pass + light intraday cash-watch)?
- **Decide vs alert posture:** should the monitor only ever **alert + arm** (human transmits), never auto-rebalance, even on paper? (Recommend yes — preserve the gate; the monitor proposes, the human disposes.)
- **Generality now or later:** build the generic "watch→decide→gated-act" core up front (more design cost, future-proofs algo execution), or build account-maintenance concretely first and generalize when the algo layer is real? (Recommend a thin generic seam now — a `Verdict` dataclass + a `decide(account_state) -> Verdict` contract — without over-engineering the rest.)
- New clientId assignment (next free in the registry) and scheduled-task name/owner.
- Does the monitor own deposit/withdrawal *baselines* state, or does `cashflows.py` grow a state-backed companion? (Recommend the monitor owns runtime state; `cashflows` stays pure policy+schedule.)

---

## Cross-cutting decisions (the 3 that gate the rest)

1. **Where does explicit cash live — shared `strategies` brain (breaks byte-parity) or paperbot-side overlay (keeps it)?** (Item #2. Recommend paperbot-side.)
2. **Final residual buffer value** once cash is explicit — 0.5%, 1%, or 1.5%? (Items #1+#2 together.)
3. **Build the monitor generic (algo-ready) or account-only first?** (Item #5. Recommend a thin generic seam.)

*All work above is PAPER-only and proposal-only. No engine/config code is changed by this document. The review→arm→transmit gate remains the sole path to any order.*
