# Live-trading order resilience — design stub

**STATUS: tracked design item, NOTHING built.** PAPER today; this is a LIVE-trading prerequisite.
Revisit as we approach the live milestone. **S5 / intraday live is BLOCKED on this.**

## Why
When we go live, every open position must be covered by a protective order that **already rests on
IB's servers**, so a local-machine crash, internet outage, or Gateway death cannot leave a position
unprotected. Our bot's "brain" being offline must never expose us — protection must live on IB's side,
not in our process.

## Current posture (verified 2026-06-30, paper)
- Every order we transmit is a plain **DAY limit** (client-session-bound; dies on disconnect) — the
  `order_router.py` ladder (MIDPRICE / Adaptive / REL / marketable, all `tif="DAY"`).
- The ONLY server-resting order today is a **GTC "finish-the-last-shares" remainder** at the tail of a
  rebalance (`order_router.py` `place_laddered`) — fill-completion, **NOT protection**.
- **No stop / trailing stop / OCA / bracket / conditional rests anywhere.** Between rebalances, positions
  sit naked of server-held protection.
- No reconnect / watchdog / dead-man / `reqGlobalCancel` / protect-on-startup logic in the connection
  layer (`connections/ibkr.py` is a plain one-shot connect/disconnect).
- The S5 server-side conditional/OCA machinery (`order_router.py` `build_price_condition` /
  `build_time_condition` / `build_conditional_order` / `apply_oca_group`) is **inert scaffolding** — only
  unit tests call it; nothing wired into the live flow; `transmit` always False. It proves the library can
  express these orders; it does not yet do anything.

## What survives a disconnect (per `docs/IBKR_RESTING_CONDITIONAL_ORDERS.md`)
- **Survive (server-held):** GTC/GTD limits, stops / stop-limits / trailing (IB-simulated, server-held),
  conditional orders (incl. cross-instrument), OCA groups, brackets / OTO / OCO.
- **Do NOT survive:** DAY limits, Adaptive / MIDPRICE (DAY-bound), anything held in our Python process.

## Two load-bearing risks (must resolve before any live reliance)
1. **Offline-trigger UNPROVEN.** IBKR docs say a simulated stop/conditional "remains active" on disconnect
   but are SILENT on whether it actually **fires while we are fully offline**. → The #1 gating prerequisite
   is a deliberate **kill-the-gateway PAPER PROBE**: place a resting stop/conditional, kill the gateway +
   connection, drive the trigger condition, and confirm IB executes it with us fully offline. Until this
   passes, no live position may rely on server-resting protection.
2. **GTC longevity.** GTC self-cancels after ~90 days of no login, plus quarter-boundary / corporate-action
   resets. "Set and forget" protection must be periodically re-logged-in and re-staged.

## Gap list for live
1. A server-resting protective layer actually placed and left working — at minimum a GTC stop / trailing
   stop (or bracket child) on every live position, transmitted and left at IB, re-staged on roll. (None built.)
2. Wire + gate + live-probe the S5 conditional/OCA seam; prove the offline-trigger on paper (risk #1).
3. Confirm the Gateway config does NOT auto-cancel on disconnect and that "Maintain and resubmit orders when
   connection is restored" is ON (unverified on our box).
4. Reconnect + reconcile-on-startup in the connection layer (survive the nightly auto-restart / a dropped
   socket; re-read open orders; re-stage expiring GTCs).
5. GTC longevity management (periodic login + re-stage).
6. FA-block compatibility of conditional / OCA / stop orders is unconfirmed; until probed, protection may
   have to live on direct (lone-account) legs.

## Per-strategy priority
- **S5 + any intraday 0DTE (S2/S3): HIGHEST.** S5's thesis is a permanent tail hedge + auto-cover that must
  fire with our program OUT OF THE LOOP. S5 **cannot go live** until the conditional / GTC / OCA layer is
  wired AND the kill-the-gateway offline-trigger is paper-proven.
- **S0 / S4 (monthly / daily): LOWER.** Slow-cadence, delta-based, no embedded options leg; an outage is a
  missed rebalance, not an uncovered short-gamma blowup. They still want reconnect / reconcile hygiene.

## Hard rule
Do NOT let S5 go live on the strength of the seam code alone — it is untested scaffolding, and the
offline-trigger guarantee is undocumented by IBKR. **The kill-the-gateway paper probe must pass first.**

Cross-refs: `docs/IBKR_RESTING_CONDITIONAL_ORDERS.md`, `docs/IBKR_ORDER_TYPES_RESEARCH.md`,
`paperbot/order_router.py` (inert seam), `docs/S5_SPEC.md`.
