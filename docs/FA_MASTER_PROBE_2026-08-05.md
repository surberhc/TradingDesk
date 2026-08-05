> **Corrected 2026-08-05:** the original verdict overstated this as a refutation/blocker ("REFUTED", "Andrew must become an FA master", "Option 2 not buildable"). That reading was WRONG — FA block/group orders are already PROVEN and FA Groups + CRM-brain is the LOCKED architecture; the 2-account view is deliberate access scoping, not a capability gap. The factual probe results below are unchanged; the corrected interpretation is in the Verdict section.

# Step-0 FA-master probe — live 4003 login `apsv1816` (2026-08-05)

**Question (MASTER_PLAN Priority-1):** Is the 4003 live-trade login an IBKR Financial
Advisor (FA) MASTER exposing the firm's ~329/303 client sub-accounts, or does it only
expose the 2 known accounts? This gates "Option 2" (wire real FA Group orders onto 4003).

**Method:** READ-ONLY connect to 127.0.0.1:4003 via `ib_async` 2.1.0 (clientId 199, one-off,
`readonly=True`). Called `ib.managedAccounts()`, `ib.requestFA(1/2/3)` (GROUPS/PROFILES/
ALIASES), and `ib.accountSummary()`. Placed / modified / cancelled nothing.

## Evidence (live, verbatim)
- `serverVersion()` = 178; connected fine.
- `managedAccounts()` → **count 2**:
  - `U14438624`  — AccountType **TRUST**, NetLiquidation 121,012.13 (the funded, currently-trading S0/S8 account)
  - `U5721712`   — AccountType **INDIVIDUAL**, NetLiquidation 957.10 (retired/test)
- `requestFA(1)` GROUPS → **timeout, None returned**
- `requestFA(2)` PROFILES → **timeout, None returned**
- `requestFA(3)` ALIASES → **timeout, None returned**
  (ib_async logged `requestFAAsync: Timeout` on all three — the server sent no FA payload.)

## Verdict (corrected 2026-08-05)
The raw facts above stand: today the live 4003 login `apsv1816` exposes exactly 2 accounts
(`U14438624` trust + `U5721712` retired individual), no FA groups/profiles/aliases are defined,
and `requestFA(1/2/3)` timed out. What those facts MEAN, correctly:

- **This is NOT a refutation and NOT a capability gap.** FA block/group orders are already
  **PROVEN** on the paper gateway (2026-06-27 real block proof — one master fill split to sub-
  accounts; see [[fa-block-order-allocation]]), and **FA Groups + a CRM-as-brain is the LOCKED
  architecture** for running the book ([[ibkr-sleeve-architecture]]). The mechanism works; the
  question was never whether FA groups are buildable.
- **The 2-account view is Andrew's DELIBERATE access scoping "for right now"** — not evidence
  that `apsv1816` can't be an FA master or that the client book doesn't exist. Nothing here says
  Andrew "must become an FA master"; that framing was wrong and is removed.
- **No live "Growth" group is populated yet — which is exactly what's EXPECTED at this stage.**
  Populating the live `Growth` group with client accounts is a normal point-and-click build step
  Andrew performs WHEN the live FA-group port is built, precisely as
  `FA_Group_Live_Port_BuildPlan_2026-08-04.md` ("IBKR setup (Andrew)") already specifies. The
  probe simply confirms that step hasn't been done yet; it refutes nothing.

## Next step
Build the live FA-group port per the existing plan
(`FA_Group_Live_Port_BuildPlan_2026-08-04.md`): the new `paperbot/live_fa_block_execute.py`
module + tests + read-only group verification. Andrew populates the live `Growth` group (add the
client U-accounts, method = "Contracts or Shares") point-and-click WHEN we build, then arms the
4003 gateway only for the one supervised tiny-block proof. Until that port is live, book-wide
trading continues through the already-proven per-account-direct executor
(`paperbot/batch_rebalance_execute.py`).
