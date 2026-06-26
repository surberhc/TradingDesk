"""
ledger.py — append-only audit trail for the paperbot. The record of every run.

HANDOFF §4 (Ledger/Logger): every intent, order, verdict and reconciliation goes to a
durable log so each run is auditable after the fact. Stored OFF Drive (config.STATE_DIR)
so Drive sync can never corrupt a file the engine is writing.

Two artifacts, both under config.STATE_DIR:
  * runs.jsonl   — one JSON object per line per run (machine-readable, full detail)
  * paperbot.log — one human-readable line per run (quick scan / tail)

Append-only: we never rewrite history, only add to it.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

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
