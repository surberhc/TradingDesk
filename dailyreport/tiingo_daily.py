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

BT_ROOT = Path(__file__).resolve().parents[1] / "backtester"
# The downloader writes the manifest wherever config points — the dataset was moved
# off Drive (C:\TradingDesk-Local\bt_data) because Drive sync corrupts it, so the old
# BT_ROOT/data path is dead. Resolve the SAME manifest the downloader writes, or the
# status will forever read empty and report a false "partial". Fall back to the known
# local path if the config import isn't reachable in the scheduled-task context.
try:
    sys.path.insert(0, str(BT_ROOT.parent))  # TradingDesk root, so `strategies` imports
    from strategies import config as _bt_config
    MANIFEST = Path(_bt_config.MANIFEST_FILE)
except Exception:
    MANIFEST = Path(r"C:\TradingDesk-Local\bt_data\_manifest.json")
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


def manifest_is_fresh(mani: dict, today: dt.date) -> bool:
    """True when the manifest's dataset really is dated today, in LOCAL time.

    `data_end` is the downloader's own local run date, so it is the honest field to
    compare and is preferred. `generated_at` is stamped in UTC: after about 18:00
    Central it has already rolled over to tomorrow's UTC date, so the old comparison of
    its first ten characters against the local date read as stale on every evening run.
    Convert it to local time before falling back to it.
    """
    data_end = (mani.get("data_end") or "").strip()
    if data_end:
        return data_end[:10] == today.isoformat()
    generated = (mani.get("generated_at") or "").strip()
    if not generated:
        return False
    try:
        stamp = dt.datetime.fromisoformat(generated)
    except ValueError:
        return False
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone()  # into this machine's local time
    return stamp.date() == today


def verdict(exit_code: int, fresh: bool, critical_tickers: list[str],
            qc_flagged: int, tickers: int, data_end: str) -> tuple[str, str]:
    """Decide the job's status and the operator-facing message.

    A serious data-quality finding is judged BEFORE freshness. It used to sit in a
    branch after the freshness test, so a manifest that merely looked stale hid it
    entirely and the warning never once reached the operator.
    """
    if exit_code != 0:
        return "fail", f"the downloader exited with code {exit_code}"
    if critical_tickers:
        names = ", ".join(critical_tickers)
        return "partial", (
            f"refreshed {tickers} tickers, but {len(critical_tickers)} of them have a "
            f"serious data-quality problem that needs a human look: {names}. Each one "
            f"shows a single-day price move bigger than the quality limit, which is "
            f"either a real market move or a stock split the data vendor never adjusted "
            f"for. Check before trusting these prices.")
    if not fresh:
        return "partial", (
            "the downloader ran, but the dataset is not dated today — usually a vendor "
            "rate limit, or today's closing prices are not published yet (the dataset "
            f"currently runs through {data_end or 'an unknown date'})")
    note = f" ({qc_flagged} harmless data-quality notes)" if qc_flagged else ""
    return "ok", f"refreshed {tickers} tickers, data through {data_end}{note}"


def main() -> int:
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
        return 1

    # Read the manifest the downloader just (re)built for status detail.
    tickers = qc_flagged = 0
    critical_tickers: list[str] = []
    data_end = generated = ""
    fresh = False
    if MANIFEST.exists():
        try:
            mani = json.loads(MANIFEST.read_text())
            generated = mani.get("generated_at", "")
            data_end = mani.get("data_end", "")
            tk = mani.get("tickers", {})
            tickers = len(tk)
            for sym, v in sorted(tk.items()):
                flags = v.get("qc_flags", [])
                if flags:
                    qc_flagged += 1
                # Critical = real data errors (bad splits / zero or negative prices),
                # matched on the QC's stable leading tag rather than on its prose —
                # the old test substring-matched the word "split", so any flag whose
                # wording happened to mention splits escalated.
                # "[stale]" on cash-like ETFs (SGOV/BIL/…) is benign and ignored.
                if any(str(f).startswith(("[zero]", "[move]")) for f in flags):
                    critical_tickers.append(sym)
            fresh = manifest_is_fresh(mani, TODAY)
        except Exception as e:
            _log(f"manifest read error: {e}")

    st, msg = verdict(exit_code, fresh, critical_tickers, qc_flagged, tickers, data_end)

    status.write("tiingo", st, day=TODAY_STR,
                 metrics={"tickers": tickers, "qc_flags": qc_flagged,
                          "critical_qc": len(critical_tickers),
                          "critical_tickers": critical_tickers,
                          "data_end": data_end, "generated_at": generated},
                 message=msg)
    _log(f"=== Tiingo refresh {TODAY_STR} done: {st} — {msg} ===")
    return 0 if st == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
