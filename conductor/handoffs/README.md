# HANDOFFS — drop folder (Drive-synced, all devices)

This is the single place open desktop sessions drop their handoff files so that when
Andrew leaves the office and switches to **dispatch (phone)**, the Conductor can find
every session's state in one spot and condense it.

## How to use (for any session being asked to hand off)
When Andrew says *"summarize a handoff and drop it in the handoff folder,"* the session:
1. Writes a self-contained handoff (state only — what's done, what's open, what's owed,
   where things live; **never** task orders for the next chat).
2. Saves it HERE using this exact name pattern, with date AND time so multiples sort cleanly:

   `HANDOFF_<lane-or-topic>_<YYYY-MM-DD_HHMM>.md`   (time = 24h, local Central)

   Examples:
   - `HANDOFF_paperbot_2026-06-26_1545.md`
   - `HANDOFF_backtester-ma200_2026-06-26_1602.md`
   - `HANDOFF_datacollector_2026-06-26_1710.md`

## How the Conductor (phone/dispatch) picks up
- Read everything in this folder newest-first; the timestamp in the name is the sort key.
- Two or three handoffs from a sitting are normal — read them, then condense into one
  picture for Andrew.
- After condensing, the live coordination still runs through `conductor/STATUS.md` and
  `conductor/DECISIONS.md` as before. This folder is the **inbox of raw session handoffs**;
  STATUS/DECISIONS are the **working state**.

## Housekeeping
- Don't edit a dropped handoff after the fact — drop a new dated one instead (append-only
  in spirit; the timestamp tells you which is current).
- Old handoffs can be swept into an `_archive/` subfolder once their state is folded into
  STATUS.md, so the top level only shows the current sitting.
