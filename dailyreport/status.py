"""
status.py — tiny per-job status artifacts that the end-of-day report aggregates.

Each daily job writes ONE small JSON here when it runs; the EOD reporter reads them
all and renders a section per job. This keeps the pipeline decoupled and robust: a
job that crashed (or never ran) simply has a stale/missing status, which the report
surfaces as "❌ didn't run" — it can't take the whole report down.

Status values: "ok" | "partial" | "fail" | "stale".
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

STATUS_DIR = Path(r"C:\TradingDesk-Local\state\dailyreport\status")


def write(job: str, status: str, metrics: dict | None = None,
          message: str = "", day: str | None = None) -> Path:
    """Write one job's status JSON atomically. Returns the path."""
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "job": job,
        "date": day or datetime.now().strftime("%Y%m%d"),
        "status": status,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "metrics": metrics or {},
        "message": message,
    }
    p = STATUS_DIR / f"{job}.json"
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, p)
    return p


def read(job: str) -> dict | None:
    """Read one job's status JSON, or None if missing/corrupt."""
    p = STATUS_DIR / f"{job}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None
