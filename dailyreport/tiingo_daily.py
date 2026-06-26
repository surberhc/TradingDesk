"""
tiingo_daily.py — daily refresh of the backtester's Tiingo dataset.

Runs the backtester's canonical downloader (`src/download_data.py`) as a subprocess
from the backtester project root — that script pulls every universe ticker's daily
adjusted close (plus Treasury 10y / VIX / HY-credit), rewrites the per-ticker parquet
files, runs quality checks, and rebuilds `data/_manifest.json`. We then read that
manifest and write a small status JSON the EOD report aggregates.

One-shot (fired daily by Task Scheduler after the close). The downloader is itself
resumable/safe to re-run and paces requests for the Tiingo free-tier limit.

Run manually:  <venv python> tiingo_daily.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import status

BT_ROOT = Path(r"C:\Users\andre\My Drive (andrew@surberhc.com)\TradingDesk\backtester")
MANIFEST = BT_ROOT / "data" / "_manifest.json"
LOG = Path(r"C:\TradingDesk-Local\state\dailyreport\tiingo_daily.log")
# Off-Drive secrets — the downloader reads TIINGO_API_KEY (and optional FRED_API_KEY)
# from the environment; a scheduled-task context may not inherit the user env var,
# so we load them here and inject them into the child env. Values are never logged.
SECRET_ENV = Path(r"C:\TradingDesk-Local\secrets\.env")
TODAY = dt.date.today()
TODAY_STR = TODAY.strftime("%Y%m%d")


def _child_env() -> dict:
    """Parent env + API keys loaded from the off-Drive secrets file (never printed)."""
    env = dict(os.environ)
    if SECRET_ENV.exists():
        for line in SECRET_ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, val = line.partition("=")
            name, val = name.strip(), val.strip().strip('"').strip("'")
            if name in ("TIINGO_API_KEY", "FRED_API_KEY") and val and not env.get(name):
                env[name] = val
    return env


def _log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main() -> None:
    _log(f"=== Tiingo refresh {TODAY_STR} start ===")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "src.download_data"],
            cwd=str(BT_ROOT), capture_output=True, text=True, timeout=1800,
            env=_child_env())
        exit_code = proc.returncode
        tail = (proc.stdout or "").strip().splitlines()[-12:]
        for ln in tail:
            _log("  | " + ln)
        if proc.stderr:
            _log("  STDERR: " + proc.stderr.strip()[:300])
    except Exception as e:
        _log(f"downloader failed to launch: {type(e).__name__}: {e}")
        status.write("tiingo", "fail", message=f"downloader launch error: {e}", day=TODAY_STR)
        return

    # Read the manifest the downloader just (re)built for status detail.
    tickers = qc_flagged = critical = 0
    data_end = generated = ""
    fresh = False
    if MANIFEST.exists():
        try:
            mani = json.loads(MANIFEST.read_text())
            generated = mani.get("generated_at", "")
            data_end = mani.get("data_end", "")
            tk = mani.get("tickers", {})
            tickers = len(tk)
            for v in tk.values():
                flags = v.get("qc_flags", [])
                if flags:
                    qc_flagged += 1
                # Critical = real data errors (bad splits / zero or negative prices).
                # "stale run" on cash-like ETFs (SGOV/BIL/…) is benign and ignored.
                if any(("zero" in f.lower() or "split" in f.lower()) for f in flags):
                    critical += 1
            fresh = generated[:10] == TODAY.isoformat()
        except Exception as e:
            _log(f"manifest read error: {e}")

    if exit_code != 0:
        st, msg = "fail", f"downloader exited {exit_code}"
    elif not fresh:
        st, msg = "partial", "ran, but manifest date isn't today (rate limit / no new EOD yet?)"
    elif critical:
        st, msg = "partial", f"refreshed, but {critical} ticker(s) have CRITICAL QC flags — check"
    else:
        note = f" ({qc_flagged} benign QC notes)" if qc_flagged else ""
        st, msg = "ok", f"refreshed {tickers} tickers, data through {data_end}{note}"

    status.write("tiingo", st, day=TODAY_STR,
                 metrics={"tickers": tickers, "qc_flags": qc_flagged, "critical_qc": critical,
                          "data_end": data_end, "generated_at": generated},
                 message=msg)
    _log(f"=== Tiingo refresh {TODAY_STR} done: {st} — {msg} ===")


if __name__ == "__main__":
    main()
