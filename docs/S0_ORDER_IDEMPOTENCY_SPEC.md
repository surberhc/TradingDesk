# S0 Order Idempotency — Fix Spec (FOR REVIEW)

**Status:** proposed, not built. Written 2026-07-20. Author: desk session (main).
**Gate:** this is a prerequisite for arming S0 on a live account (armed-live). It does **not**
flip `PILOT_MODE`, does **not** touch strategy/regime config (rule #1 clean — plumbing only),
and does **not** change the review → arm → transmit gate.

---

## 1. Problem (verified 2026-07-20)

S0's armed execution path can **double-transmit real orders** on re-entry. It is inert today
only because three independent walls are up (`config.READONLY=True`, `config.DRY_RUN=True`,
`morning_execute_run.PILOT_MODE=True`). This is the same class of latent bug S8 exhibited as
conductor item #36 ("would double-fire orders if ever armed").

Verified findings (file:line):

1. **The `orderRef` idempotency is documented but never enforced.**
   `order_router.py:11` claims a deterministic `orderRef`
   (`paperbot:<account>:<as_of>:<side>:<symbol>`, or `paperbot:<fa_group>:<as_of>:<side>:<symbol>`
   for FA blocks) means "a duplicate can be detected, not double-sent." But `place()`
   (`order_router.py:272`) and `place_laddered()` (`order_router.py:351`) call `ib.placeOrder()`
   **unconditionally** once armed. A package-wide grep confirms **no** call to
   `reqAllOpenOrders` / `reqOpenOrders` / `openOrders` anywhere in the transmit path. IBKR does
   **not** enforce uniqueness on `orderRef` (it is a free-text client tag). The detection code
   does not exist — the claim is aspirational.

2. **Crash / kill / reboot mid-route-loop re-fires the whole list.**
   In `morning_execute_run._do_work` path [4] the staged file is archived in a `finally`
   (`morning_execute_run.py:340`). `finally` covers exceptions but **not** a hard task-kill,
   power loss, or Windows "End Task." If the machine dies after route 1 fills but before the
   loop ends, the staged file survives and the next morning run reprocesses **all** routes,
   re-placing route 1. Because of finding #1, nothing catches it. No per-route "already sent"
   marker is persisted during the loop.

3. **The GTC resting remainder compounds re-entry.**
   `place_laddered` can leave a resting GTC order (`order_router.py:464`). On a re-run per
   finding #2 the still-working GTC is not detected and a fresh ladder stacks on top → double
   exposure on the remainder. The `order_router.py:457` comment claims a reconnect "detects the
   resting order rather than double-sending" — no such code exists.

4. **No durable per-order transmit record.**
   Path [4] emails fills and calls `_write_status(...)` but never calls `ledger.record_run()`.
   There is no local record of *which orderRefs were actually sent today* for a resumed run (or
   a human) to consult. Not a double-fire cause itself, but it is the missing substrate any fix
   needs (same ledger-persistence gap as conductor #26).

**Ruled out (not a risk):** nightly → morning cross-fire. `nightly_monitor_run` connects
`readonly=True` and only writes the staged JSON; it never calls `place()`. Concurrent processes
are already blocked by `gateway_lock` (refuse/skip). Ordinary same-day re-runs are already
no-ops via the date-keyed staged-file archive. Those existing guards stay.

---

## 2. Invariant to establish

> **For a given rebalance cycle (identified by its per-tier `as_of`), each leg's `orderRef` is
> transmitted at most once. A re-entry — retry, crash-resume, manual re-run, or a stacked
> ladder — places nothing the broker already has working or already filled, and never blindly
> auto-retries an uncertain partial.**

Two layers deliver this, broker-truth-first with a local tripwire underneath:

- **A. Pre-transmit dedup against broker truth** (authoritative).
- **B. Transmit journal** (crash-window tripwire + audit substrate).

Both fail **closed**: if we cannot prove a leg is safe to send, we do **not** send it.

---

## 3. Design

### 3.A Pre-transmit dedup gate (broker truth)

A new function in `order_router.py`, called once **per leg** immediately before the first
`placeOrder` for that leg (i.e. at the top of `place()` for the direct/block path and at the top
of `place_laddered` before rung 1):

```
def already_present(ib, order_ref, target_qty) -> LegState
```

It reads live broker state with the **safe read APIs only** (never `whatIfOrder` — known hang,
`order_router.py:495` `what_if()` stays unused in this path):

1. **Open orders** — `ib.reqAllOpenOrders()`; collect the set of `orderRef`s currently working
   (this catches resting GTC remainders and any in-flight order).
2. **Today's executions** — `ib.reqExecutions(ExecutionFilter())` (the same live call
   `account_monitor_run.py:219` already uses read-only); sum filled quantity per `orderRef`.

Returns one of:

| LegState        | Condition                                              | Action                              |
|-----------------|-------------------------------------------------------|-------------------------------------|
| `FRESH`         | ref not open, 0 filled today                          | proceed — place normally            |
| `WORKING`       | ref is in open orders                                 | **SKIP** the leg (already live)     |
| `COMPLETE`      | filled today ≥ target_qty                             | **SKIP** the leg (already done)     |
| `PARTIAL`       | 0 < filled today < target_qty, nothing open           | **SKIP + ALERT** (human decides)    |
| `UNKNOWN`       | either broker read failed / timed out                 | **SKIP + ALERT** (fail closed)      |

Only `FRESH` proceeds to transmit. Everything else transmits nothing for that leg and is
reported. `PARTIAL` is deliberately **not** auto-resumed in v1 — auto-netting a
partially-filled leg across runs is a v2 enhancement; v1's safe default is "never double-send,
surface the partial to a human." This matches the desk's fail-closed / human-in-loop posture.

Scoping note: `orderRef` embeds the cycle `as_of`, so the gate naturally blocks only *this
cycle's* re-entry — a genuinely new rebalance next month carries a different `as_of` and is not
falsely deduped. `reqExecutions`' default filter only returns recent/today's fills; a cross-day
resume is backstopped by layer B (below) and by open-order persistence (GTC rests survive across
days and are still caught by step 1).

### 3.B Transmit journal (crash-window tripwire + audit)

A new leaf module `paperbot/transmit_journal.py` (or an extension of `ledger.py` — see §5),
append-only JSONL under `config.STATE_DIR` (off Drive, per the ledger's own rule), keyed by
`(date, order_ref)`:

- **Before** the first `placeOrder` for a leg: append `{state: "ATTEMPTING", order_ref, as_of,
  symbol, side, target_qty, ts}`.
- **After** the leg settles: append `{state: "SENT", order_ref, filled, remaining, rested_gtc,
  avg_px, ts}`.

On a resumed run, the gate consults the journal **as well as** the broker:

- `order_ref` journaled `SENT` today → skip (defense-in-depth with LegState `COMPLETE`).
- `order_ref` journaled `ATTEMPTING` but **not** `SENT` → the process died *between* placing and
  confirming. Broker state is uncertain → **SKIP + ALERT**, never auto-retry. Layer A's
  broker read confirms what (if anything) actually landed.

Broker truth (A) is authoritative for "is it there?"; the journal (B) is the tripwire that says
"we were mid-transmit — stop and have a human confirm" for the one window A alone can't
distinguish (placed-but-not-yet-recorded).

### 3.C Staged-file handling

Stop relying on archive-*after*-the-loop as the idempotency guard (finding #2). The journal
keyed by `orderRef` makes the staged file **safely replayable**: a resumed run skips already-
`SENT` legs and only sends `FRESH` ones. Keep the existing archive step, but it becomes a
tidy-up, not the safety mechanism. Add a top-level per-cycle "run complete" journal marker so a
fully-reconciled cycle is unambiguous.

### 3.D Correct the false docstrings

Update `order_router.py:11` and the `:457`/`:459` comments so they describe the enforcement that
now actually exists (or, until built, stop asserting a protection that isn't there).

---

## 4. Failure modes → behavior (acceptance matrix)

| Scenario                                                        | Required behavior                          |
|----------------------------------------------------------------|--------------------------------------------|
| Re-run of a fully-completed staged file                        | **0** `placeOrder` calls                   |
| Crash after leg 1 filled, resume same cycle                    | leg 1 skipped (`COMPLETE`), only leg 2 sent|
| Resting GTC remainder exists for a ref, leg re-run             | leg skipped (`WORKING`) — no stacking      |
| Partial fill across runs                                       | whole leg skipped + alert; no double-send  |
| Journal `ATTEMPTING` with no `SENT`                            | alert; no auto-retry                       |
| `reqAllOpenOrders` / `reqExecutions` fails or times out        | fail closed: transmit nothing + alert      |
| Genuinely new cycle next month (new `as_of`)                   | places normally (not falsely deduped)      |
| Concurrent second process                                      | already blocked by `gateway_lock` (unchanged) |

Each row becomes a test in `paperbot/test_order_ladder.py` (or a new
`test_order_idempotency.py`) with a fake `ib` recording every `placeOrder`. This is the parity
test finding #36 implies S8 also needs; write it here for S0 first.

---

## 5. Scope of change

- `order_router.py` — add `already_present()` + `LegState`; call the gate at the top of the
  transmit branch of `place()` and before rung 1 of `place_laddered()`; correct the docstrings.
- `morning_execute_run.py` — in path [4], write journal `ATTEMPTING`/`SENT` around each leg;
  call `ledger.record_run()` with the per-cycle result; keep archive as tidy-up.
- **New:** `paperbot/transmit_journal.py` (small, append-only, mirrors `ledger.py` discipline) —
  or fold into `ledger.py` if the reviewer prefers one sink. `ledger.record_run` already appends
  to `runs.jsonl`; the journal is finer-grained (per leg, with an `ATTEMPTING` pre-state), which
  is why a separate artifact is the cleaner default. Reviewer's call.
- `paperbot/version.py` — bump VERSION + CHANGELOG (order-affecting change: routing/idempotency).
- Tests — the §4 matrix.

**Out of scope (explicit):** flipping `PILOT_MODE`; any strategy/regime/band/sizing knob (rule
#1 frozen); auto-resume of partial fills (v2); the standalone live-account gateway/clientId
stand-up (separate work item); `whatIfOrder` (stays unused — known hang).

## 6. Open questions for the reviewer

1. Separate `transmit_journal.py` vs. extend `ledger.py`? (Default: separate — needs an
   `ATTEMPTING` pre-state the run ledger doesn't model.)
2. `PARTIAL` handling in v1 — confirm "skip + alert, human decides" is acceptable vs. wanting
   auto-remainder now. (Recommend skip+alert; auto-netting is the riskier path.)
3. Should the gate also run in **dry/pilot** mode as a live-fire rehearsal (compute LegState,
   log "would skip/would send", transmit nothing) so we validate the dedup logic against real
   broker reads *before* arming? (Recommend yes — it's zero-transmit and exercises the exact
   read path.)
