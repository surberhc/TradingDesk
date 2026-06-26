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
- PENDING | C | optionB-create-groups | Once the block-order test passes, may the worker
  create 3 allocation groups on the PAPER gateway — one per risk level (Conservative /
  Balanced / Growth) — so each group's client accounts can be traded together as a block
  (one price, split per account)? It would replace a leftover test group. Paper only,
  reversible, no client orders sent by this step. | YES create them / HOLD

## ANSWERED
- ANSWERED | C | paperbot-flatten | FLATTEN ALL — do not just clear the one PDBC share.
  Sweep EVERY DU sub-account for leftover positions and sell them back so each account is a
  blank slate (zero positions). Andrew expanded scope: he believes a few other DU accounts
  also hold leftover shares. Paper only. Serialize order placement; reconcile + log each
  flatten; confirm zero positions across all DU accounts when done. | 2026-06-26
