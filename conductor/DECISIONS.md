# DECISIONS — inbox / outbox  (append-only)

How this works:
- A **desktop worker** adds an item under **PENDING** when it needs Andrew to decide something.
- The **Conductor** asks Andrew that ONE question, then records his answer under **ANSWERED**
  (date + the decision). The desktop worker watches ANSWERED and proceeds.
- Append only — never rewrite history. One item = one decision.

Format:
  PENDING  | <lane> | <id> | <plain-English question> | <options if any>
  ANSWERED | <lane> | <id> | <Andrew's decision> | <date>

---

## PENDING (waiting on Andrew)
(none)

## CORRECTIONS (conductor, authoritative)
- RESOLVED | C | flatten-monday | OBSOLETE — based on a STALE pre-flatten snapshot. The flatten
  DID execute Fri 16:10 CT in extended hours (outsideRth=True). Confirmed by IBKR's own execution
  feed (reqExecutions), broker execIds on file: DU146 SLD 100 SPY @730.57 (execId 00025b49.6a482609),
  DU143 SLD 100 GOOG @335.70 (00025b44.6a410891), DU142 SLD 1 PDBC @15.81 (00025b49.6a48260f).
  Live now: all 5 DU subs FLAT (0 positions), zero open/working orders. The "flatten-monday" item
  came from a PARKED session reading positions from before 16:10; it cancelled only its OWN unfilled
  RTH orders (already moot). NO Monday action needed — accounts are a confirmed blank slate. | 2026-06-26

## ANSWERED
- ANSWERED | C | block-allocation-model | OPTION A — execution model confirmed: engine computes
  each account's explicit target shares (net liq + reserve + band) and places blocks against
  ContractsOrShares groups. NetLiq order-method route abandoned. Proceeding to create the 3 tier
  groups (optionB-create-groups, now ungated since block proof passed). | 2026-06-26
- ANSWERED | C | block-validation-path | OPTION A — place ONE tiny real paper block (3 PDBC
  via test_group/NetLiq ~$48) to prove the FA master accepts + splits a block order, confirm via
  broker execIds, then immediately flatten back to zero. Paper only, reversible. | 2026-06-26
- ANSWERED | ALL | autowatcher-executor | BUILD THE HANDS-OFF LOOP (Andrew's standing
  directive — he runs all 4 lanes from his phone; decisions live ONLY in this file). Stand
  up ONE always-on desktop executor that: (1) polls conductor/DECISIONS.md; (2) for each
  newly-APPROVED item, runs that lane's action; (3) SERIALIZES all shared resources — the IB
  gateway, order placement, git commits, and any connections/ or config write — so two lanes
  never touch them at once; (4) writes results + next state back to conductor/STATUS.md and,
  when a lane needs Andrew, appends a PENDING here; (5) STOPS and asks (PENDING) for anything
  not yet approved or that hits money / real-money / business-line — never self-approves.
  PAPER ONLY; preserve the existing review->arm->transmit gate for any rebalance that
  transmits. The 4 build-conversations are no longer required for execution — this executor
  is the single runner. BOOTSTRAP: the first desktop session to take a turn launches it
  (e.g. a /loop session or a scheduled poll), then it runs unattended. Report back here when
  it's up. | 2026-06-26
- ANSWERED | C | optionB-create-groups | APPROVED — once the block-order test passes, create
  the 3 paper allocation groups (Conservative / Balanced / Growth), replacing the leftover
  test group. Paper only, reversible, no client orders transmitted by this step. Gate stays:
  block what-if must pass FIRST; serialize with the flatten (flatten completes before any
  block rebalance). | 2026-06-26
- ANSWERED | C | paperbot-flatten | FLATTEN ALL — do not just clear the one PDBC share.
  Sweep EVERY DU sub-account for leftover positions and sell them back so each account is a
  blank slate (zero positions). Andrew expanded scope: he believes a few other DU accounts
  also hold leftover shares. Paper only. Serialize order placement; reconcile + log each
  flatten; confirm zero positions across all DU accounts when done. | 2026-06-26
