# ACCOUNT_ALLOCATION.md — paper account -> strategy map

PAPER ONLY (port 4002). This is the single source of truth for which IBKR PAPER
sub-account trades which strategy. As more strategies come online (S4, S8, etc.) and
get assigned to paper accounts for testing, **update this file at the same time** —
before or in the same change-set as any `paperbot/config.py` ENROLLMENT edit. If this
file and `config.py` ever disagree, `config.py` is what actually executes, but this
file should never be allowed to go stale — treat a mismatch as a bug to fix immediately.

## FA structure

- **DF8922141** — FA master (advisor/connection account). Never traded directly.
  `REBALANCE_MASTER = False` in `paperbot/config.py`. All rebalancing happens on the
  client sub-accounts below; the master is used only as the IBKR API connection point.

## Sub-accounts

| Account | Assigned Strategy | Status | Notes |
|---|---|---|---|
| DU8922142 | S0 (Conservative tier) | ACTIVE | solo account, DIRECT order routing |
| DU8922143 | S0 (Balanced tier) | ACTIVE | now solo in tier (DU8922144 removed 2026-07-09); routing is derived automatically at runtime by `rebalance_engine.route_blocks()` from the live account count per tier — a tier with exactly 1 enrolled account routes DIRECT, >=2 routes as an FA block. With DU8922144 gone, DU8922143 now routes DIRECT (same mechanism that already makes Conservative/DU8922142 DIRECT), no code change required for this beyond the ENROLLMENT edit. IBKR's live `tier_balanced` FA group still lists DU8922144 as a member on the gateway side (GUI-only, no API) — must be edited there too, see Open Items. |
| DU8922144 | UNASSIGNED (freed from S0) | AVAILABLE FOR REASSIGNMENT | pulled from S0's Balanced tier 2026-07-09 to free up for testing another strategy; existing S0 positions being liquidated to cash (not yet done — see Open Items) |
| DU8922145 | S0 (Growth tier) | ACTIVE | now solo in tier (DU8922146 removed 2026-07-09); same DIRECT-routing note as DU8922143 above, but for `tier_growth`. |
| DU8922146 | UNASSIGNED (freed from S0) | AVAILABLE FOR REASSIGNMENT | pulled from S0's Growth tier 2026-07-09 to free up for testing another strategy; existing S0 positions being liquidated to cash (not yet done — see Open Items) |

## Open items (as of 2026-07-09)

1. **Liquidation not yet done.** DU8922144 and DU8922146 still hold their existing S0
   positions. They are not "clean" for reuse by another strategy until those positions
   are sold to cash via a deliberate, armed PAPER session (review -> arm -> transmit).
   This is a separate next step, not part of the config/docs change that created this
   file.
2. **IBKR FA group membership (GUI-only) not yet updated.** IBKR's live FA groups
   `tier_balanced` and `tier_growth` still list DU8922144 and DU8922146 as members on
   the actual gateway side. There is no API to edit FA group membership (see memory:
   IBKR model portfolio API limit) — it's a GUI-only, serialized admin step. Until that
   GUI edit happens, `rebalance_execute.py`'s fail-closed live-membership check will
   raise "FAILING CLOSED" the next time a Balanced or Growth rebalance is attempted,
   because the code-side ENROLLMENT (2 accounts) will no longer match the live FA
   group's membership (still 4 accounts total across the two groups). Fix the GUI-side
   group membership before the next Balanced/Growth rebalance run.

## History

- **2026-07-09** — DU8922144 (Balanced) and DU8922146 (Growth) pulled out of S0's
  rotation. Why: S0 didn't need two duplicate accounts per tier to validate the
  strategy, and freeing 2 of the 5 paper accounts creates capacity to paper-test other
  strategies (e.g. S4, S8) going forward without needing new accounts provisioned.
  Balanced and Growth tiers go from 2-account FA blocks to solo DIRECT-routed accounts
  (mirroring how Conservative already worked). Updated in the same change-set:
  `paperbot/config.py` ENROLLMENT, `paperbot/version.py` (VERSION bump + CHANGELOG),
  `paperbot/MONDAY_RUNBOOK.md` (flagged as historical). See CLAUDE.md for the
  paper-only / commit-discipline rules governing this change.
