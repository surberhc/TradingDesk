# S0 Deployment Plan — Trust Account U14438624 → Adaptive All-Weather Core

**Date:** 2026-07-28
**Status:** DRAFT for Andrew's review. NOTHING is armed or transmitted from this doc. No order
goes out until Andrew reviews the exact plan and deliberately arms it (review → arm → transmit).
**Builds on:** the 2026-07-28 tiny-test milestone (conductor #144) that proved the arm→transmit→fill
path live — BOT 1 USFR @ $50.355 on U14438624. That proved the *plumbing*; this deploys the *strategy*.

---

## 1. Objective
Transition the funded trust account **U14438624** from its current ad-hoc holdings into the
**S0 (Adaptive All-Weather Core)** target portfolio — the desk's FIRST real deployment of actual
strategy capital. S0's config is FROZEN (rule #1): we deploy the strategy exactly as it is, no tuning.

## 2. Current state (verified read-only, 2026-07-28)
NetLiquidation ≈ **$117,480**. Holdings:

| Symbol | Position | In S0 universe? | Action |
|---|---|---|---|
| XLU | 50.63 | Yes (sector sleeve) | adjust to S0 weight |
| XLV | 39.90 | Yes (sector sleeve) | adjust to S0 weight |
| XLP | 27.90 | Yes (sector sleeve) | adjust to S0 weight |
| XLE | 81.70 | Yes (sector sleeve) | adjust to S0 weight |
| XLI | 13.38 | Yes (sector sleeve) | adjust to S0 weight |
| BIL | 253.72 | Yes (defensive sleeve) | keep / adjust |
| USFR | 1.00 | Yes (defensive sleeve) | keep (the tiny-test share folds in) |
| GDXJ | 19.22 | **No** (gold miners) | **SELL** |
| GDX | 25.46 | **No** (gold miners) | **SELL** |
| SIL | 24.98 | **No** (silver miners) | **SELL** |
| SILJ | 76.94 | **No** (silver miners) | **SELL** |
| BUCK | 1721.89 | **No** (cash-like, not S0) | **SELL** |

The book is a sector + precious-metals-miner + cash-like mix — **not** the S0 basket.

## 3. Target (S0 — frozen config)
S0 is regime-driven. Today's reading (2026-07-28): **RiskOn, score 78.8, equity band 80–100%**.
S0's universe (strategies/config.py): equity core (SPY/VTI/RSP), 11 sector ETFs, a defensive sleeve
(SGOV/BIL/SHY/VGSH/USFR/TFLO/IEF/TLT), and real assets (GLDM/IAU gold, SCHP/STIP TIPS, PDBC/DBC
commodities). Note S0 holds gold via **GLDM/IAU**, not miners — which is why the miners are sells.
Exact target weights come from the frozen shared brain; we do not set or tune them.

## 4. The transition (the gap, in words)
- **Sell** the non-S0 holdings: GDXJ, GDX, SIL, SILJ, BUCK (raises cash).
- **Adjust** the overlapping holdings to S0 target weight: XLU/XLV/XLP/XLE/XLI, BIL, USFR.
- **Buy** what's missing: SPY/RSP core, the six sectors not yet held, the gold/TIPS/commodity sleeves,
  and the rest of the defensive sleeve to target.
- Exact per-symbol whole-share deltas are produced by the read-only full-plan preview (step 6.2),
  sized by the UNCHANGED rebalance_engine against NetLiq — not hand-written here.

## 5. Decisions Andrew must make BEFORE arming
1. **Deploy amount** — the full ~$117k, or a starting tranche?
2. **Sequencing** — liquidate the non-S0 holdings to cash first and buy the S0 basket second (cleaner,
   two steps, brief cash drag), or one net rebalance (the engine nets sells/buys)?
3. **Tax / trust** — selling the appreciated miners realizes gains in the trust. Confirm that's
   acceptable and coordinated with the trust's tax picture. (This is Andrew's call, flagged not decided.)
4. **Timing** — deploy in one session, or stage over a few days to cut single-day slippage on the
   larger names?

## 6. Mechanism & staged rollout
The transmit path is proven (07-28). Do **NOT** reuse the 1-share tiny-test executor (its caps are
1 share / $150 — wrong tool for a full deployment).
- **6.1 Build an S0-deploy executor** from the existing, blessed rebalance path
  (`rebalance_engine.plan_account` → `order_router` laddered marketable-limit → armed `place()` on 4003),
  pinned to U14438624, S0 target only, **whole-share**, every order behind the HARD price guard and the
  dedup gate, with per-order and per-run bounds sized to a real deployment (not $150). Tests first,
  version bump — order-affecting.
- **6.2 Read-only full-plan preview** — enumerate every sell/buy and the resulting weights vs the S0
  target; Andrew reviews the exact list before anything arms.
- **6.3 Arm & transmit in tranches** — Read-Only API off on 4003, then transmit **sells first** (raise
  cash), then buys; one tranche at a time, laddered limits. Re-check Read-Only API (disarm) when done.
- **6.4 Verify + reconcile** — confirm fills and reconcile positions to the S0 target after each tranche.

## 7. Guards (non-negotiable)
- review → arm → transmit gate is sacred; Andrew arms explicitly, each session.
- S0 config FROZEN (rule #1) — deploy as-is, zero tuning.
- Whole-share; account is PDT-clear (funded margin > $25k).
- Every order behind the HARD price guard + dedup gate.
- Trust/fiduciary: Andrew confirms authority + intent to deploy.

## 8. Open build items (tracked)
- S0-deploy executor (from the rebalance path) + tests + version bump.
- Read-only full-plan preview (exact per-symbol sells/buys) — the immediate next step.

## Next action
Generate the **read-only full-plan preview** — the exact sells/buys to take U14438624 to the S0 target —
for Andrew's review. Nothing transmits; it's a preview.
