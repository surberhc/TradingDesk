"""
transmit_journal.py — append-only, per-LEG transmit journal (the crash-window tripwire).

Layer B of the S0 order-idempotency design (docs/S0_ORDER_IDEMPOTENCY_SPEC.md §3.B). It
is finer-grained than ledger.py's per-RUN record: it captures a pre-transmit `ATTEMPTING`
state (which the run ledger does not model) so a process that dies BETWEEN placing an
order and confirming it can be detected on the next run.

Discipline mirrors ledger.py exactly:
  * append-only JSONL under config.STATE_DIR (OFF Drive — Drive sync corrupts a file the
    engine is writing) — we never rewrite history, only add to it;
  * one JSON object per line, keyed by (day, order_ref);
  * dependency-light — imports config only, so it is a true leaf (order_router imports it
    without any cycle).

Record shapes (one per line, all carry a local `ts` + `day`):
  * ATTEMPTING     — written BEFORE the first placeOrder for a leg.
      {state, day, order_ref, as_of, symbol, side, target_qty, ts}
  * SENT           — written AFTER the leg settles.
      {state, day, order_ref, filled, remaining, rested_gtc, avg_px, ts}
  * CYCLE_COMPLETE — a per-cycle "run reconciled" marker (unambiguous full-cycle close).
      {state, day, as_of, n_routes, n_sent, n_skipped, ts}

The gate (order_router.already_present) consults state_for():
  * order_ref SENT today       -> defense-in-depth COMPLETE (skip);
  * order_ref ATTEMPTING, no SENT -> placed-but-unconfirmed -> SKIP + ALERT, never retry.
Broker truth (layer A) stays authoritative for "is it actually there?"; this journal is
the tripwire for the one window a broker read alone cannot distinguish.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime

import config

# State constants (also the on-disk `state` field values).
ATTEMPTING = "ATTEMPTING"
SENT = "SENT"
CYCLE_COMPLETE = "CYCLE_COMPLETE"


def _journal_path() -> str:
    """Resolved at call time (not import) so a test that redirects config.STATE_DIR — or
    the off-Drive move — is honored without reimporting this module."""
    return os.path.join(config.STATE_DIR, "transmit_journal.jsonl")


def _day_str(day=None) -> str:
    if day is None:
        return date.today().isoformat()
    if isinstance(day, date):
        return day.isoformat()
    return str(day)


def _append(record: dict) -> str:
    """Append one journaled record. Returns the JSONL path. Stamps a local timestamp."""
    path = _journal_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stamped = {"ts": datetime.now().isoformat(timespec="seconds"), **record}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(stamped, default=str) + "\n")
    return path


def record_attempting(order_ref, *, as_of=None, symbol=None, side=None,
                      target_qty=None, day=None) -> str:
    """Journal that we are ABOUT to place `order_ref` (before the first placeOrder)."""
    return _append({"state": ATTEMPTING, "day": _day_str(day), "order_ref": order_ref,
                    "as_of": as_of, "symbol": symbol, "side": side,
                    "target_qty": target_qty})


def record_sent(order_ref, *, filled=None, remaining=None, rested_gtc=None,
               avg_px=None, day=None) -> str:
    """Journal that `order_ref` has SETTLED (after the leg's placement completes)."""
    return _append({"state": SENT, "day": _day_str(day), "order_ref": order_ref,
                    "filled": filled, "remaining": remaining,
                    "rested_gtc": rested_gtc, "avg_px": avg_px})


def record_cycle_complete(*, as_of=None, n_routes=None, n_sent=None,
                         n_skipped=None, day=None) -> str:
    """A top-level per-cycle marker: a fully-reconciled cycle is unambiguous (spec §3.C)."""
    return _append({"state": CYCLE_COMPLETE, "day": _day_str(day), "as_of": as_of,
                    "n_routes": n_routes, "n_sent": n_sent, "n_skipped": n_skipped})


def _read_records(day=None) -> list[dict]:
    """All journal records for `day` (today by default). Missing file -> empty list. A
    malformed line is skipped rather than crashing a resumed run."""
    path = _journal_path()
    want = _day_str(day)
    out: list[dict] = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("day") == want:
                out.append(rec)
    return out


def state_for(order_ref, day=None):
    """The most-advanced journaled state for `order_ref` on `day`: SENT beats ATTEMPTING.
    Returns "SENT", "ATTEMPTING", or None if the ref was never journaled that day."""
    seen = None
    for rec in _read_records(day):
        if rec.get("order_ref") != order_ref:
            continue
        st = rec.get("state")
        if st == SENT:
            return SENT
        if st == ATTEMPTING:
            seen = ATTEMPTING
    return seen
