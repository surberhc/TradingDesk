# Handoff — IBKR sleeve architecture: FA group block test (2026-07-23)

**Resume:** read `docs/CRM_DESIGN_groups_brain.md` (esp. new **§12**) and `docs/HANDOFF_2026-07-21_CRM_groups.md`, then this file. Architecture **Option A** is LOCKED (FA Account Groups + CRM-as-brain; Model Portfolios dead for automation).

## This session's work
1. **CRM design §12 added** (operational refinements, worked out with Andrew) to `docs/CRM_DESIGN_groups_brain.md`:
   - **12.1 Reconciliation TRIAGED, not binary-freeze** — explain drift from IBKR's own transaction/activity ledger (the broker already labels dividends/interest/corp-actions), EOD via Flex; intraday drift is `UNEXPLAINED_PENDING`, only the unexplained residual latches an account. Per-account freeze, never system-wide.
   - **12.2 Two-tier data cadence** — heavy Flex once daily; balances/margin re-pulled PER-TRANCHE pre-flight (freezes the roster before the order opens).
   - **12.3 Faults LATCH** — one fault pulls the account out, alerts ONCE, sits out the rest of the day; two alert types: `TRADE_SKIPPED_LATCHED` vs `TEMPLATE_NO_LONGER_QUALIFIES` (reassign, not retry).
   - **12.4 Whole-contract sizing floor** — options are atomic; floor = 1 contract × peak concurrent firings × margin/spread; below floor → account SITS OUT entirely (Andrew's call).
   - **12.5 Cadence is CONFIG not code** — floor recomputes when cadence changes; the cadence values stay frozen (rule #1 intact).

2. **Test B (FA group block test) — read-only recon DONE.** Live paper FA groups on port 4002 (master DF8922141):

   | Group | Member | amount | defaultMethod |
   |---|---|---|---|
   | Balanced | DU8922143 | 224 | ContractsOrShares |
   | Conservative | DU8922142 | 1 | ContractsOrShares |
   | Growth | DU8922145 | 194 | ContractsOrShares |

   - Each group has ONE account. `defaultMethod` already ContractsOrShares (no method change needed).
   - **XML tag casing CONFIRMED** (`Group / name / defaultMethod / ListOfAccts varName="list" / Account / acct / amount`) — resolves the MONDAY_RUNBOOK "casing unconfirmed offline" flag. `set_group_contracts_or_shares` tag handling matches.

## BIG operational discovery (load-bearing)
**The IBKR paper account/login is SHARED by multiple people.** Daytime lockouts = multi-user contention on the single paper login (one-login-per-username), NOT merely data-entitlement contention. HARD RULE: during market hours (before ~15:00 CT) do NOT disrupt other users — no killing paper-gateway processes, no login takeover, no relaunch (a relaunch competes for the one login and kicks whoever is on it). Read-only investigation is OK. Own-the-gateway work (relaunch / FA writes / arming) only AFTER 3 PM CT. **Architecture implication (→ conductor #80):** the desk can't reliably own the paper gateway during market hours; real unattended automation likely needs its OWN dedicated IBKR login, not the shared paper account. See memory `paper-account-after-3pm-ct`.

## Gateway ops notes
- IBC config (`C:\IBC\config.ini`): `ReadOnlyApi=yes` (BLOCKS the replaceFA write — MUST flip to `no` + relaunch for the membership write), `ExistingSessionDetectedAction=manual`, TradingMode=paper, port 4002.
- A clean IBC launch (`connections.ibkr_paper.ensure_gateway()`) DID auto-accept the paper-trading API disclaimer (the recurring Error 10141 blocker) — the automated path works when IBC owns the launch (the manual logins are what caused the dueling-session mess).
- clientIds: 33 = paperbot_fa (read), 36 = paperbot_fa_admin (write). Recon script lives in the session scratchpad as `fa_inventory.py`.
- **Live gateway = port 4003 (S8 zero-transmit pilot; daily 08:15 CT restart) — NEVER touch.**

## Incident
**`paperbot\arming.py arm` killed the S8 LIVE-pilot Gateway on port 4003 (2m51s outage, 09:55:17→09:58:08 CDT).**
Step 2 of NEXT ACTION below (flip `ReadOnlyApi=no` + relaunch) went through `arming.py arm`, whose
elevated restart calls `_kill_gateway_processes()` with the paper default `dir_substring=r"C:\IBC"` —
which is a string **prefix** of `C:\IBC-Live-Trade`, so it killed the live pilot too and then reported
success. Zero-transmit wall never breached; recovered by re-running `LiveTradeGatewayOpen_0815CT`.
**Until the fix lands, `arming.py arm` AND `disarm` are unsafe to run while any other Gateway is up.**
Full writeup: `docs/INCIDENT_2026-07-23_arm_restart_killed_live_gateway.md` — conductor **#46** (fix)
and **#47** (orphan `java` pid 29236 cleanup + the unresolved 4002 disappearance).

## NEXT ACTION — resume AFTER 3 PM CT (Andrew authorized adding the member)
1. Confirm the shared paper login is free (other users off).
2. Flip IBC `ReadOnlyApi=no`; relaunch ONE clean paper gateway via `ensure_gateway()`.
3. `backup_fa_groups(ib)` (mandatory — replaceFA is a full-XML overwrite).
4. `set_group_contracts_or_shares(ib, "Balanced", {"DU8922143":1, "DU8922144":1})` → adds DU8922144 (amounts are placeholders; the executor overwrites per-order).
5. `requestFA(1)` read-back → confirm Balanced has 2 members.
6. THEN the load-bearing **Test 0 / §10.4**: does an FA group block fill BOTH accounts at one price with the ContractsOrShares split, AND can an S8 multi-leg combo ride an FA block? (`config.LADDER_FA_BLOCKS=False`; combo × FA-block unproven.) This is the genuine unknown gating Option A execution.

## Also this session
- **Conductor #79:** 4001 live-data gateway access RESOLVED — `databot0001` got TWS access (2026-07-22). Data-poll re-enablement UNBLOCKED, but the live API pull is UNVERIFIED (verify a real round-trip before trusting; the old "smoke test passed" STATUS entries remain fabricated). Memory updated.
- **Conductor #80:** gateway unattended-resilience HARDENING task (fix IBC config, re-enable GatewayWatchdog, root-cause the elevated-launch / shared-login contention, guarantee fail-closed + off-machine alerts). Pick up after the block-fill test.
- Open decisions still pending (Andrew): transport JSON-vs-DB (SQLite recommended), overlay-tier weights (frozen, needs out-of-sample/per-regime validation).

Conductor items **#42** (CRM) and **#43** (gateway test) advanced.
