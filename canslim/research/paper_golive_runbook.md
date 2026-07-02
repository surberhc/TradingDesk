# CAN SLIM execution engine — PAPER go-live RUNBOOK (documentation only)

> ## ⛔ DO NOT RUN UNATTENDED — REQUIRES ANDREW PRESENT ⛔
> This is a **written plan + go/no-go checklist**, nothing more. Reading or writing this
> file **executes nothing**. Every armed/arming/transmit step below is to be performed
> **only with Andrew physically present**, deliberately, one step at a time. Nothing here
> is scheduled, backgrounded, or auto-armed.
>
> **Nothing has been wired yet.** `canslim/execution_engine.py` is still COMPUTE-ONLY, no
> paperbot code consumes its `DayPlan`, and **no `paperbot/version.py` bump has been made**
> because no order-affecting change exists. The wiring in §3 is the *future* gated step,
> not a change already landed.
>
> **PAPER account only** (DU…141 family, port 4002). Real-money (port 4001) is intentionally
> absent from `connections/clientids.py` and unreachable. We never call this "live."

---

## 0. Status honesty — this is NOT ready to arm today

The **#1 no-go** is strategy validation, per the project's first rule (never curve-fit):

- The **selection / replacement** side of CAN SLIM is *promising but not proven* (memory:
  `canslim-aps-strategy-eval`). The engine's `Pick` source is still pluggable and unproven
  end-to-end.
- The **options-overlay** work is still open (being tested, not validated).
- What *is* proven is the **execution discipline** (the `E3 + timing` config ≈ 2.7× his
  realized book at lower drawdown, on **his** picks) — that is what `execution_engine.py`
  applies forward. Discipline-on-a-validated-pick-stream is the bar; a live paper run on an
  *unproven* pick stream would be arming a strategy we have not cleared out-of-sample.

**Conclusion:** the wiring and arming procedure below can be *rehearsed in DRY-RUN* now, but
**must not be armed for real transmission** until selection + options validation clears the
anti-curve-fit bar (out-of-sample + per-episode/per-regime) with Andrew's explicit blessing.

---

## 1. Prerequisites / GO–NO-GO checklist

Every box must be **GO** before §4's arm step. Any single NO-GO ⇒ stop.

| # | Prerequisite | How to confirm (read-only) | State today |
|---|---|---|---|
| 1 | **Strategy validated (anti-curve-fit)** — selection + options cleared OOS + per-episode/per-regime; Andrew's explicit blessing on file | review `canslim/research/*` verdicts; memory `canslim-aps-strategy-eval`, `strategy-evaluation-playbook` | **NO-GO** (selection promising-not-proven; options still open) |
| 2 | **Protective stop rests SERVER-SIDE** — the `initial_stop` (−7%) must survive a client/gateway disconnect | the kill-the-gateway probe, §2 (run WITH Andrew) | **NOT YET DONE** — mandatory before any arm (memory `live-trading-order-resilience`) |
| 3 | **clientId reserved** — a dedicated, collision-proof id for the CAN SLIM executor | `connections/clientids.py` — next free id after `42`; add `"canslim_paper_exec": 43` (own id, never collides with paperbot on 30 / rebalance-exec on 38 / arm-verify on 39) | **NOT YET RESERVED** (must be added before wiring) |
| 4 | **Paper gateway healthy** | `python paperbot/arming.py verify` (reports READ-ONLY/WRITE-ENABLED + `config.ini` value, transmits nothing); or `connections.ibkr.gateway_running()` | check at run time |
| 5 | **Gateway currently LOCKED (read-only)** — the safe resting state | `arming.py verify` shows `READ-ONLY (locked)`; `paperbot/config.py` committed `READONLY=True`, `DRY_RUN=True` | should be GO (committed defaults are safe) |
| 6 | **Single-process gateway mutex available** — no other desk process (monitor/rebalance/collector) will operate the gateway during the run | `paperbot/gateway_lock.py` — acquire with `on_busy="refuse"` (names the holder) | procedural |
| 7 | **Kill switch + risk guards present** | `paperbot/risk_manager.py` (`max_daily_loss_pct_nav` = −2%, per-position cap, cash reserve) — evaluate per account BEFORE any transmit | exists; wire into the CAN SLIM path (§3) |
| 8 | **Dry-run rehearsal green** | `cd canslim` → `python execution_engine.py` (compute-only day-by-day plan); `pytest tests/test_execution_engine.py -q` | run at rehearsal time |
| 9 | **Andrew present** | — | mandatory for §2 and §4 |

---

## 2. THE KILL-THE-GATEWAY PROBE (server-side resilience test)

**Purpose:** prove that a protective stop, once placed, **rests on IBKR's servers** and
**survives the client/gateway dying** — so a −7% catastrophic stop still protects the
position if our process or the gateway goes down. This is the **#1 prerequisite** for any
live/paper arming of a stop-carrying strategy (memory: `live-trading-order-resilience`).

**Run this WITH Andrew present, on the PAPER account, in a supervised window — not now.**

Procedure (documented; do not execute here):

1. **Confirm safe start.** `python paperbot/arming.py verify` → expect `READ-ONLY (locked)`.
   Note `ibg.xml` mtime as a baseline (arming commit evidence).
2. **Arm** (deliberate, Andrew present): `python paperbot/arming.py arm`. This sets
   `ReadOnlyApi=no`, clean-restarts the gateway (kills the process on port 4002, waits for
   the port to close, settles, relaunches with `java_version=17`), then **self-verifies**
   write-enabled via the zero-transmission probe (`probe_api_readonly`). It raises loudly if
   the toggle did not commit — do not proceed on a raise.
3. **Place ONE tiny resting protective order on a single test symbol**, sized trivially
   (one lot), as a **resting server-side stop** — a plain **GTC** stop/limit that IB holds
   (the router's `build_gtc_limit` proves a GTC-tif LMT rests at IB; a true STP/STP-LMT
   built the same way with `tif="GTC"` is the protective form). Server-side, GTC, so it does
   **not** depend on our client staying connected. Record its `orderRef`.
4. **Verify it is resting at IB.** Reconnect read-only and confirm the open order is present
   (`ib.reqAllOpenOrders()` / `ib.openTrades()`), i.e. it lives on IBKR, not just in our
   process memory.
5. **KILL THE GATEWAY** out from under it — the actual resilience test. Use the proven kill
   primitive `paperbot/arming.stop_gateway()` (kills only the process listening on port
   4002; leaves the ThetaData collector alone) and confirm the port is closed.
6. **Relaunch the gateway** (`connections.ibkr.ensure_gateway()` or `arming.restart_gateway()`)
   and **reconnect read-only.**
7. **CONFIRM THE STOP IS STILL THERE** — the resting protective order re-appears in the open
   orders with the same `orderRef`. **PASS** = the stop survived the kill. **FAIL** = the
   stop vanished ⇒ the strategy is **not** disconnect-safe ⇒ **do not go live** until fixed.
8. **Clean up:** cancel the test order (read-once-then-cancel), then **DISARM**:
   `python paperbot/arming.py disarm` (sets `ReadOnlyApi=yes`, clean restart, self-verifies
   `READ-ONLY (locked)`). Confirm `ibg.xml` mtime advanced across the arm/disarm cycle
   (proof the checkbox actually toggled, per the `paper-arming-and-fills` root-cause fix).

A **PASS on step 7 is a hard gate** for §4. No pass, no arm.

---

## 3. Wiring — feed the `DayPlan` into the router as a PROPOSAL (the gated seam)

This is the future order-affecting change. **It bumps `paperbot/version.py`** (routing
change) and needs a CHANGELOG line **when built** — not before. The single hand-off point
is `# === PAPERBOT SEAM ===` on `DayPlan` (`canslim/execution_engine.py` ≈ L130).

**Mapping `DayPlan` → router intents (proposal, transmit-free by default):**

- `plan.exits` → **close** orders for the named held positions. Each `Action` carries a
  `trigger` (`"stop_7pct"` | `"decisive_50sma_break"`) — log it as the exit reason.
- `plan.entries` → **open** orders sized to `target_dollars` (the engine already applied
  ~12% sizing, 18% cap, 7-name concurrent cap, and the exposure dial), with `initial_stop`
  (the −7% level) placed as a **resting server-side protective stop** alongside the fill.
  `entry_ref` is the pivot (buy-zone reference), for the limit.
- `plan.holds` → no order.

**Router interface it consumes (real signatures in `paperbot/order_router.py`):**

- Single-account laddered execution (the intended path for CAN SLIM single-name equities):
  `order_router.place_laddered(ib, *, symbol, side, total_qty, caps, instrument_class,
  account=…, order_ref=…, armed=False, …)`.
  - `instrument_class` from `classify_instrument(symbol)` → single-name equities resolve to
    `LIQUID_ETF` / `ILLIQUID_ETF` by the spread heuristic (they are `Stock`, not options).
  - `caps` = per-rung worst-case cap dict built from a live quote (`live_quotes.marketable_cap`,
    `config.ORDER_CAP_K`); every cap re-passes the HARD PRICE GUARD (`_check_limit_price`,
    rejects NaN/None/≤0) inside each rung builder.
  - `order_ref` — a deterministic id (mirror the `paperbot:<account>:<as_of>:<side>:<symbol>`
    convention) so a restart re-derives the same id and never double-sends.
  - **GTC-remainder layer** (`config.LADDER_REST_REMAINDER=True`): an unfilled remainder is
    left as a resting GTC LMT at IB — "ladder while connected, rest when gone."
- The **protective stop** for each entry is a **separate resting server-side order** (GTC),
  built the same disconnect-surviving way `build_gtc_limit` proves out (a STP/STP-LMT with
  `tif="GTC"`), priced at `Action.initial_stop`. It must be placed **server-side** so it
  survives the kill probe of §2 — this is the whole reason §2 gates §4.
- **Never** call `ib.whatIfOrder(...)` on this path (known hang; the router avoids it
  everywhere). `what_if` exists only for single-account non-hanging validation and is not
  required here.

**Transmit-free by construction.** `place_laddered` / `place` call `transmit_guard(armed)`,
which **fails CLOSED**: transmission is permitted **only** if `config.DRY_RUN is False` AND
`config.READONLY is False` AND the caller passed `armed=True`. With the committed defaults
(both True) the wiring **builds + logs orders and transmits nothing** — a pure proposal /
dry preview. This is the state to rehearse in.

**Pattern to copy:** `paperbot/rebalance_execute.py` is the existing, proven template for a
transmit-capable executor behind the exact gate — default DRY-RUN identical to the review
runner; transmits only with **all four** of (`READONLY False` + `DRY_RUN False` + `armed=True`
+ an explicit CLI token like `--arm-i-understand`), flipped **in-process only**, never on
disk, never auto-armed. The CAN SLIM executor should mirror this shape (own clientId 43,
pinned to a DU sub-account, `risk_manager.evaluate` per account before any transmit, ledger
each step).

---

## 4. The review → arm → transmit gate (PAPER account)

**Andrew present. One step at a time. Nothing auto-arms.**

- **A. REVIEW (read-only, transmits nothing).** Run the CAN SLIM executor in its **default
  DRY-RUN**: it connects read-only, computes today's `DayPlan`, maps it to router intents,
  runs `risk_manager.evaluate` per account, and **logs** every order it *would* send
  (`transmit_guard` → BLOCKED). Andrew reads the exact action list — exits, entries,
  `target_dollars`, `initial_stop` levels, sizing vs. the exposure dial — and the cash line.
  **This is the decision point.** If anything looks wrong, stop here; nothing was sent.
- **B. ACQUIRE THE GATEWAY MUTEX.** `gateway_lock(purpose="canslim_paper_exec",
  client_id=43, on_busy="refuse")` — wait-then-refuse (naming the holder) so no monitor /
  rebalance / collector operates the gateway during the run. Heartbeat lease keeps a long
  legitimate run from looking wedged.
- **C. ARM (deliberate, verified).** `paperbot/arming.arm()` — `ReadOnlyApi=no`, clean
  restart, **self-verify write-enabled** (raises loudly if the toggle didn't commit). The
  in-process flip of `config.READONLY`/`config.DRY_RUN` to False happens only on the arm
  path, only for this process, gated additionally by the explicit CLI token — the committed
  `config.py` defaults on disk stay `True`.
- **D. TRANSMIT (armed, supervised).** Call the router with `armed=True` on the PAPER
  account (DU sub-account, clientId 43). Entries go through `place_laddered` (laddered →
  GTC-rest remainder); the **protective stop rests server-side (GTC)** for each new position.
  Watch fills with the router's flushed, bounded per-rung progress (supervise-long-ops).
- **E. RECONCILE.** Read back positions/cash (read-only, e.g. `recon_report`) and confirm
  each fill + that every entry has its resting protective stop present at IB.
- **F. DISARM.** `paperbot/arming.disarm()` — `ReadOnlyApi=yes`, clean restart, self-verify
  `READ-ONLY (locked)`. Release the gateway mutex. Back to the safe resting state.

**Backstop:** even a mis-typed `--armed` (without the exact token) stays a dry review; the
executor refuses to flip the flags without the exact token. There is no auto-arm anywhere.

---

## 5. Rollback / abort + monitoring

**Instant abort (any time):**
- **DISARM** the gateway: `python paperbot/arming.py disarm` → verifies read-only lock; the
  API then **physically cannot** transmit. This is the master off-switch.
- **Kill switch:** `risk_manager` halts ALL trading at `max_daily_loss_pct_nav` = −2% on the
  day; per-position and cash-reserve guards run **before** every transmit.
- **Resting stops stay working:** because protective stops rest **server-side (GTC)**, they
  keep protecting open positions even after we disarm/disconnect (that is the §2 guarantee).
- **Cancel a specific order** by its deterministic `orderRef` (read-once-then-cancel).
- **Release the gateway mutex** on any exit (the lock's `__exit__` releases on normal exit
  **and** exception — a crashed/Ctrl-C'd holder never leaves a poisoned lock).

**Monitoring (heartbeat + the liveness rubric — memory `liveness-rubric`):**
- **Heartbeat:** the router prints flushed per-rung progress; the gateway-lock holder
  refreshes its heartbeat every ~30s (a silent holder ≥300s is treated as wedged and
  reclaimable). Never run this path silently.
- **Run it against the 11 death-and-no-restart modes** before trusting any scheduled/repeat
  use: crash, stall, dup, partial output, poison input, dependency-down, supervisor-death,
  reboot, missed-window, logoff, unnoticed-death. (For go-live this is **attended**, so
  "unnoticed death" is covered by Andrew being present — do **not** schedule it unattended.)
- **Ledger every step** (like `rebalance_execute`) — connect, arm, each block, each fill,
  disarm — stamped with `paperbot/version.py` for the compliance trail.

---

## 6. What has and hasn't changed

- **No order-affecting change has been made.** `canslim/execution_engine.py` remains
  COMPUTE-ONLY; nothing imports paperbot from it; the seam is a comment, not a call.
- **No `paperbot/version.py` bump.** A bump + CHANGELOG line is required **when the §3
  wiring is actually built** (routing is order-affecting) — not for this documentation.
- **No clientId consumed yet.** `canslim_paper_exec` (proposed id 43) is *reserved on paper*
  in this plan; add it to `connections/clientids.py` as part of the §3 build.
- **Committed safety posture untouched:** `paperbot/config.py` stays `READONLY=True`,
  `DRY_RUN=True`; the gateway stays read-only-locked. Arming is a deliberate, attended,
  self-verifying, reversible action — never a default.
```
