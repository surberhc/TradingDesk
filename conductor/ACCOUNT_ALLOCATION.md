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
| DU8922144 | UNASSIGNED (freed from S0) | AVAILABLE FOR REASSIGNMENT | pulled from S0's Balanced tier 2026-07-09 to free up for testing another strategy; existing S0 positions being liquidated to cash (not yet done — see Open Items). **2026-07-13:** now the SOLE remaining freed/unassigned account (DU8922146 assigned to S8, see below) — the natural remaining candidate for S4 (conductor item #7 previously floated DU8922144/146 as S4 candidates; narrowed to DU8922144 only as of this date). |
| DU8922145 | S0 (Growth tier) | ACTIVE | now solo in tier (DU8922146 removed 2026-07-09); same DIRECT-routing note as DU8922143 above, but for `tier_growth`. |
| DU8922146 | S8 (British IC + B2) | ASSIGNED (S8) — reserved, but NOT touched by the current runner, see Open Items | pulled from S0's Growth tier 2026-07-09 to free up for testing another strategy. **2026-07-13:** Andrew decided S8 gets this account (over DU8922144) — see `docs/S8_SPEC.md` and `paperbot/s8_config.py` (`ACCOUNT = "DU8922146"`). Still holds residual S0 positions not yet liquidated to cash (see Open Items #1) — must be cleaned via a deliberate, armed PAPER liquidation session before any real S8 pilot activity begins here. **2026-07-13 (later, same day):** Andrew decided `s8_runner.py`'s actual live-cycle IBKR connection does NOT use this (or any) paper account at all — it connects exclusively to the separate live-side read-only data Gateway (`connections.ibkr_live_data`, port 4001) for both its margin-preflight accountSummary read and its 0DTE chain snapshot. This row/assignment stands as-is (DU8922146 remains reserved for S8 for whenever a paper-side or real transmission path is eventually built), but is not queried, filtered on, or otherwise touched by today's runner — `s8_config.ACCOUNT` is informational/provenance-only in the current code (see `paperbot/s8_runner.py`'s module docstring, "CONNECTION TARGET" section, and `paperbot/s8_risk.py`'s docstring). |

## Open items (as of 2026-07-09)

1. **Liquidation not yet done.** DU8922144 and DU8922146 still hold their existing S0
   positions. They are not "clean" for reuse by another strategy until those positions
   are sold to cash via a deliberate, armed PAPER session (review -> arm -> transmit).
   This is a separate next step, not part of the config/docs change that created this
   file.
2. **IBKR FA group membership — DONE (confirmed 2026-07-09):** DU8922144 and
   DU8922146 have been removed from the `tier_balanced`/`tier_growth` FA groups on the
   IBKR GUI side. Code-side ENROLLMENT and live FA group membership should now agree.
3. **TODO — liquidation trade sizing.** When the deliberate armed paper session to
   liquidate DU8922144/DU8922146's residual S0 positions is run (see item 1 above),
   size that session's trades off each account's *current actual holdings*, not off
   any stale target/model allocation — since they're now standalone accounts outside
   any FA group, there's no group-derived target to reconcile against, just "sell
   what's there to cash." Not urgent; flagged for whenever that liquidation session
   happens.

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
- **2026-07-09 (later)** — DU8922144/DU8922146 confirmed removed from the
  `tier_balanced`/`tier_growth` FA groups in the IBKR GUI (Andrew completed this
  manually). Liquidation of their residual S0 positions still pending; when done,
  size off current actual holdings per-account, not a stale target.
- **2026-07-13** — Andrew decided S8 (British IC + B2, see `docs/S8_SPEC.md`) is
  assigned to DU8922146, not DU8922144 (no further reasoning given — decision as made).
  `paperbot/s8_config.py`'s `ACCOUNT` constant updated from `"TBD"` to `"DU8922146"` in
  the same change-set. This leaves DU8922144 as the sole remaining freed/unassigned
  account, narrowing conductor item #7's S4 account question to DU8922144 only. Open
  Items #1's liquidation caveat is unchanged by this decision: DU8922146 still holds
  residual S0 positions that must be liquidated to cash via a deliberate, armed PAPER
  session before any real S8 pilot activity can safely begin there.
- **2026-07-13 (later, same day)** — Andrew decided, after further discussion this
  session, that S8 should not connect to the paper Gateway at all: `paperbot/s8_runner.py`
  now connects exclusively to the separate live-side read-only data Gateway
  (`connections/ibkr_live_data.py`, port 4001, blocked on IBKR's own account approval —
  conductor item #24/#25) for both its margin-preflight accountSummary read and its 0DTE
  chain snapshot. `s8_config.ACCOUNT` (`"DU8922146"`) is therefore now informational/
  reserved-for-later only — it is not passed to, filtered on, or otherwise used by the
  live-cycle connection, since the live-data Gateway only ever authenticates as exactly
  one real personal account with no sub-account concept. DU8922146 remains reserved for
  S8 (row above) for whenever a future paper-side or real transmission path is built;
  today's runner simply does not touch it. `connections/clientids.py` gained
  `paperbot_s8_livedata` (51) for this connection; `paperbot_s8`/`paperbot_s8_exec`
  (49/50) stay registered/reserved unchanged.
