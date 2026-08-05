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

## Verdict
- **(a) Is `apsv1816` an FA master? NO.** A real FA master answers `requestFA` with a groups/
  aliases XML payload and lists every managed sub-account in `managedAccounts()`. This login
  answered `requestFA` with a hard timeout (no FA data channel) and reports only 2 accounts,
  both ordinary types (TRUST / INDIVIDUAL), not an advisor umbrella.
- **(b) Accounts exposed: exactly 2** — `U14438624` (trust) + `U5721712` (individual). NOT the
  client book.
- **(c) FA GROUPS defined? NONE.** No groups, profiles, or aliases exist on this login.
- **(d) REFUTES the plan's "all client accounts under one 4003 FA master" claim.** Option 2 (FA
  Group orders on 4003) is **NOT buildable as-is** — there is no FA master and no client
  accounts to group. The per-account-direct executor (`paperbot/batch_rebalance_execute.py`)
  remains the only proven whole-book path.

## What would unblock Option 2
Andrew must convert/link this login to an actual FA master in IBKR Account Management (or log
the 4003 gateway in under the firm's real FA advisor login) so the client accounts appear under
one master; only then do `managedAccounts()` and `requestFA` expose a groupable client book.
Until then, book-wide trading goes through the per-account-direct executor.
