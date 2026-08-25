"""
ledger.py — append-only audit trail for the paperbot. The record of every run.

HANDOFF §4 (Ledger/Logger): every intent, order, verdict and reconciliation goes to a
durable log so each run is auditable after the fact. Stored OFF Drive (config.STATE_DIR)
so Drive sync can never corrupt a file the engine is writing.

Two artifacts, both under config.STATE_DIR:
  * runs.jsonl   — one JSON object per line per run (machine-readable, full detail)
  * paperbot.log — one human-readable line per run (quick scan / tail)

Append-only: we never rewrite history, only add to it.

READING IT BACK (v0.37.0)
-------------------------
An audit trail nobody can traverse is not an audit trail. :func:`iter_runs` / :func:`find_run`
are the minimal READ side, and they are the second half of the join an examiner actually
walks: every orderRef this desk puts on the wire ENDS in ``:{run_id}``
(order_router._run_stamp via safe_execute._deploy_ref), so given an IBKR order you read its
run_id off the ref, :func:`find_run` returns the run record that produced it, and that record
carries the account's model label, the target weights and — where the model is an
Andrew-authored custom allocation — the published allocation version_number / version_id.
Strictly read-only: these never rewrite, truncate or reorder the file.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Iterator, Optional

import config

RUNS_JSONL = os.path.join(config.STATE_DIR, "runs.jsonl")
LOG_TXT = os.path.join(config.STATE_DIR, "paperbot.log")


def record_run(record: dict) -> str:
    """Append one run record. Returns the JSONL path. Stamps a local timestamp."""
    os.makedirs(config.STATE_DIR, exist_ok=True)
    stamped = {"ts": datetime.now().isoformat(timespec="seconds"), **record}

    with open(RUNS_JSONL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(stamped, default=str) + "\n")

    line = (f"{stamped['ts']}  mode={stamped.get('mode')}  acct={stamped.get('account')}  "
            f"nav={stamped.get('nav')}  intents={stamped.get('n_intents')}  "
            f"approved={stamped.get('n_approved')}  transmitted={stamped.get('n_transmitted')}  "
            f"halted={stamped.get('halted')}")
    with open(LOG_TXT, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return RUNS_JSONL


# ========================================================================================
# READ SIDE — minimal, read-only, append-only-safe.
# ========================================================================================
def iter_runs() -> Iterator[dict]:
    """Yield every run record in the order it was appended (oldest first).

    A missing file yields nothing (a desk that has never run has no history — that is not an
    error). A malformed/half-written trailing line is SKIPPED rather than raised: this file is
    appended to by live processes, and a reader must never be able to break, or block, a run
    that is mid-write. Opens read-only; writes nothing."""
    if not os.path.exists(RUNS_JSONL):
        return
    with open(RUNS_JSONL, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue            # torn/partial line from a concurrent append — skip it
            if isinstance(rec, dict):
                yield rec


def find_run(run_id: str) -> Optional[dict]:
    """The run record whose ``run_id`` matches — the join key an orderRef carries.

    Returns the LAST (most recent) match, because the ledger is append-only: if a record for
    the same run were ever appended twice, the later one is the one that saw the whole run.
    None if there is no such record. Read-only."""
    wanted = str(run_id)
    found: Optional[dict] = None
    for rec in iter_runs():
        if str(rec.get("run_id") or "") == wanted:
            found = rec
    return found
