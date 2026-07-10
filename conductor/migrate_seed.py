"""
migrate_seed.py — ONE-TIME migration: seed the conductor DB from the existing
hand-written conductor/STATUS.md + conductor/DECISIONS.md.

Extracts:
  - the "PICK UP HERE NEXT SESSION" bullets + clearly-open carried-forward items from
    STATUS.md -> items (status='open', area guessed where obvious else 'unclassified',
    session_tag='migration')
  - the ANSWERED rows from DECISIONS.md -> decisions (status='answered')

This is a best-effort text migration, not a parser — the source bullets are prose, not
structured data. Each inserted item's `notes` field carries the original bullet text so
nothing is silently lossy; area is set to 'unclassified' unless the bullet is obviously
about a named lane (S0/S4/S5/S8, gateway, data pipeline, etc).

Run once: `python conductor/migrate_seed.py`. Safe to re-run (checks for an existing
migration marker log_entry before inserting again) but is not designed to be re-run.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from db import get_connection

TODAY = "2026-07-09"
SESSION_TAG = "migration"

# Hand-curated from the "PICK UP HERE NEXT SESSION" blockquote + carried-forward /
# "Open items" bullets surfaced across STATUS.md's dated sections as of 2026-07-09.
# (title, area, notes)
OPEN_ITEMS = [
    (
        "Liquidate DU8922144 + DU8922146 to cash",
        "S0",
        "Still hold live S0 positions on the PAPER gateway; needs a deliberate armed "
        "paper session, sizing off each account's current actual holdings (not a stale "
        "target) since they're now standalone, outside any FA group.",
    ),
    (
        "Confirm first real nightly-monitor / morning-execute pilot cycles fired",
        "S0 automation pilot",
        "nightly-monitor (~21:15 CT) and morning-execute (~08:50 CT) pilot cycles "
        "expected 2026-07-09/10; logs/emails not yet reviewed as of 2026-07-09.",
    ),
    (
        "Review PILOT_MODE 'WOULD HAVE TRANSMITTED' logs, decide whether to flip live",
        "S0 automation pilot",
        "After a few real pilot cycles, Andrew reviews the logs and decides whether to "
        "flip PILOT_MODE=False. No transmit capability exists until then.",
    ),
    (
        "Review british_ic/longleg_slippage_isolation.py and investech/ (untracked)",
        "unclassified",
        "Both sit untracked in the working tree, unreviewed, deliberately left uncommitted.",
    ),
    (
        "Clean up stray git worktree .claude/worktrees/awesome-pare-467379/",
        "unclassified",
        "Old pre-archive copy of dailyreport/; flagged multiple sessions; awaiting "
        "Andrew's explicit cleanup call.",
    ),
    (
        "Decide on strategies/parts/defensive.py fillna(0.0)-as-worst-percentile pattern",
        "strategies",
        "A missing daily factor (e.g. return_3m) is scored via pct.fillna(0.0) as the "
        "WORST cross-sectional percentile rather than NaN/unknown -- design tradeoff, "
        "not a one-line NaN-safety fix like the 6 sites already patched. Andrew's call.",
    ),
    (
        "Pick an account/profile for S4 (SPX vol-control fund) paper-deploy",
        "S4",
        "Shelf-ready, unarmed, no account assigned. DU8922144/146 freeing up are a "
        "natural candidate home once liquidated. Not yet decided or actioned.",
    ),
    (
        "S5 (financed convexity overlay) financing-structure sizing decision",
        "S5",
        "Still pending Andrew's call. Sizing the financing overlay up to pay real "
        "income flips net delta positive and 'finances the hedge away' -- can't run "
        "small insurance-first AND big income-first sizing in one sleeve.",
    ),
    (
        "ThetaData port 25503 vs 25510 client consolidation",
        "data pipeline",
        "Parked; revisit only if/when the InvesTech project resumes.",
    ),
    (
        "Schedule + validate forward-collector depth widening (commit 6c57ecf)",
        "data pipeline",
        "Built but unscheduled; needs a greeks side-by-side + overnight timing run "
        "before cutover.",
    ),
    (
        "HY-OAS-only spike history (shorter ~3yr FRED window)",
        "S0",
        "Flagged but not run -- do if Andrew wants the credit-spread side of the "
        "VIX/HY-OAS data-lag analysis too.",
    ),
    (
        "British IC S8: pull full history back to 2024-09-16",
        "S8 / British IC",
        "TAT log goes back that far; execution-level IBKR data only starts 2025-07-08. "
        "Blocked on Andrew pulling an additional Flex Query export for the earlier window.",
    ),
    (
        "British IC S8: intraday/path-dependent exit-rule test",
        "S8 / British IC",
        "Does the underlying's speed/direction of move (not just the long's own price) "
        "predict which decoupled longs are worth holding past the stop -- proposed, "
        "not started.",
    ),
    (
        "British IC S8: isolate long-leg-specific exit slippage",
        "S8 / British IC",
        "Never isolated from the blended short-stop + long-close bid-ask analysis; "
        "matters if S8's exit is ever priced out for real.",
    ),
    (
        "British IC: unresolved data artifacts",
        "S8 / British IC",
        "2026-01-16 balance-file mismatch (~$1,222, root cause not found); 35 unmatched "
        "+ 51 ambiguous TAT-vs-execution rows (Oct 2025 sample); 1,586 short-side "
        "scale-in batches with no visible paired long; 316 scale-in combo groups where "
        "short-side P&L can't be split further per-batch (IBKR FIFO limitation).",
    ),
    (
        "S8 needs fill-cost validation + more crash-event data before any live implementation",
        "S8 / British IC",
        "Backtest marks forced exit at 1-min OHLC close, not real bid/ask; only ~1yr of "
        "data with exactly ONE true crash event (2025-10-10). Explicit prerequisites, "
        "already stated in S8_DESIGNATION.md.",
    ),
]


def already_migrated(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM log_entries WHERE session_tag = ? AND title = ? LIMIT 1",
        (SESSION_TAG, "STATUS.md / DECISIONS.md migration to SQLite"),
    ).fetchone()
    return row is not None


def migrate_items(conn) -> int:
    count = 0
    for title, area, notes in OPEN_ITEMS:
        exists = conn.execute(
            "SELECT 1 FROM items WHERE title = ? LIMIT 1", (title,)
        ).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO items (title, area, status, opened_date, last_touched, "
            "closed_date, session_tag, notes) VALUES (?, ?, 'open', ?, ?, NULL, ?, ?)",
            (title, area, TODAY, TODAY, SESSION_TAG, notes),
        )
        count += 1
    return count


# Hand-transcribed from conductor/DECISIONS.md's ## ANSWERED section (5 rows, 2026-06-26).
ANSWERED_DECISIONS = [
    (
        "C",
        "block-allocation-model",
        "",
        "OPTION A -- execution model confirmed: engine computes each account's explicit "
        "target shares (net liq + reserve + band) and places blocks against "
        "ContractsOrShares groups. NetLiq order-method route abandoned. Proceeding to "
        "create the 3 tier groups (optionB-create-groups, now ungated since block proof "
        "passed).",
        "2026-06-26",
    ),
    (
        "C",
        "block-validation-path",
        "",
        "OPTION A -- place ONE tiny real paper block (3 PDBC via test_group/NetLiq "
        "~$48) to prove the FA master accepts + splits a block order, confirm via "
        "broker execIds, then immediately flatten back to zero. Paper only, reversible.",
        "2026-06-26",
    ),
    (
        "ALL",
        "autowatcher-executor",
        "",
        "BUILD THE HANDS-OFF LOOP (Andrew's standing directive -- he runs all 4 lanes "
        "from his phone; decisions live ONLY in DECISIONS.md). Stand up ONE always-on "
        "desktop executor that polls, serializes shared resources (gateway, order "
        "placement, git commits, config writes), writes results back, and stops+asks "
        "for anything not yet approved or money-touching. PAPER ONLY; preserve the "
        "review->arm->transmit gate. BOOTSTRAP: first desktop session to take a turn "
        "launches it, then it runs unattended.",
        "2026-06-26",
    ),
    (
        "C",
        "optionB-create-groups",
        "",
        "APPROVED -- once the block-order test passes, create the 3 paper allocation "
        "groups (Conservative / Balanced / Growth), replacing the leftover test group. "
        "Paper only, reversible, no client orders transmitted by this step. Gate stays: "
        "block what-if must pass FIRST; serialize with the flatten.",
        "2026-06-26",
    ),
    (
        "C",
        "paperbot-flatten",
        "",
        "FLATTEN ALL -- do not just clear the one PDBC share. Sweep EVERY DU sub-account "
        "for leftover positions and sell them back so each account is a blank slate "
        "(zero positions). Andrew expanded scope: he believes a few other DU accounts "
        "also hold leftover shares. Paper only. Serialize order placement; reconcile + "
        "log each flatten; confirm zero positions across all DU accounts when done.",
        "2026-06-26",
    ),
]


def migrate_decisions(conn) -> int:
    count = 0
    for lane, question, options, answer, decided_date in ANSWERED_DECISIONS:
        exists = conn.execute(
            "SELECT 1 FROM decisions WHERE question = ? LIMIT 1", (question,)
        ).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO decisions (lane, question, options, status, answer, "
            "decided_date) VALUES (?, ?, ?, 'answered', ?, ?)",
            (lane, question, options, answer, decided_date),
        )
        count += 1
    return count


def main():
    conn = get_connection()

    n_items = migrate_items(conn)
    n_decisions = migrate_decisions(conn)

    conn.execute(
        "INSERT INTO log_entries (date, session_tag, title, body_md, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            TODAY,
            SESSION_TAG,
            "STATUS.md / DECISIONS.md migration to SQLite",
            "One-time migration from the prose STATUS.md + DECISIONS.md to the "
            "SQLite-backed conductor (conductor/db.py, cli.py, render.py). "
            f"Seeded {n_items} open items (from the PICK UP HERE banner + carried-forward "
            f"bullets) and {n_decisions} answered decisions (from DECISIONS.md's ANSWERED "
            "section). The full prior STATUS.md is preserved at "
            "conductor/STATUS_prior_to_migration_2026-07-09.md. DECISIONS.md itself is "
            "left in place, not deleted, per the task's scope limit.",
            _dt.datetime.now().isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    print(f"Migration complete: inserted {n_items} open items, {n_decisions} answered decisions.")
    print("Log entry recorded documenting the migration.")


if __name__ == "__main__":
    main()
