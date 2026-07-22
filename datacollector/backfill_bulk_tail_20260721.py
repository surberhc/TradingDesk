r"""
backfill_bulk_tail_20260721.py — one-shot ThetaData EOD tail-backfill for the
"bulk-only" roots over the 2026-07-04 .. 2026-07-20 gap.

CONTEXT
-------
The nightly EOD job (eod_daily.py) only maintains config.all_roots() — the curated
~50-root universe. The warehouse ALSO holds ~91 extra "bulk-only" roots (single
names / sector & thematic ETFs) that were captured by one-time bulk pulls and are
NOT on the nightly list, so they froze when those bulk pulls stopped (~2026-07-03
for 68 roots, ~2026-07-08 for 22 roots; plus the long-delisted FB root, frozen
2021, which simply returns no data for any recent day and lands as an empty marker).

This script fills the tail gap for those bulk-only roots — every US-equity TRADING
DAY from 2026-07-04 through 2026-07-20 inclusive — WITHOUT touching config.all_roots()
(those 50 are already current via the nightly job; re-pulling them would be wasted
requests). It reuses the exact same fetch/write/join logic as backfill_20260709.py:
  download.pull_day  (greeks ⨝ open_interest, settled/historical path)
  storage.have_day / write_day (atomic) / rebuild_catalog

TARGET ROOTS (computed, never hardcoded)
----------------------------------------
    bulk_only = {on-disk root dirs under raw/options} - set(config.all_roots())
So if the curated universe changes, the target set stays correct automatically.

TRADING CALENDAR (holiday-correct by construction)
--------------------------------------------------
The codebase has no holiday-aware calendar helper (eod_daily uses pd.bdate_range =
weekdays only; the 1-min collectors use a weekday filter + a known-empty JSON). Per
the fallback of "derive from an existing root's present dates," the target trading
days are taken from a MAINTAINED reference root (SPY, kept current by the nightly
job): a day in [2026-07-04, 2026-07-20] is a trading day iff SPY has a file for it.
This yields exactly the real, collectable trading calendar (weekends + market
holidays already absent), so we never waste a request on a closed session.

RESUMABLE / IDEMPOTENT
----------------------
storage.have_day() keys off file presence, so a re-run continues where it left off:
any (root, day) already on disk is skipped. Safe to re-run.

SAFETY
------
Aborts cleanly (exit 1) if the Terminal is not reachable. One bad (root, day) never
aborts the rest. DOES NOT run any ThetaData pull until invoked. Exit 0 on success.

Usage (see the exact command printed by the task report):
    <venv python> backfill_bulk_tail_20260721.py
    <venv python> backfill_bulk_tail_20260721.py --log C:\path\to\bulk_tail.log
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

import config
import storage
import download
import thetadata_client as td

# Inclusive backfill window (US-equity trading days only; see module docstring).
START_DAY = "20260704"
END_DAY = "20260720"

# Maintained reference root whose on-disk dates define the true trading calendar.
REFERENCE_ROOT = "SPY"

# Default log path (scratchpad). Overridable with --log.
DEFAULT_LOG = (r"C:\Users\andre\AppData\Local\Temp\claude"
               r"\C--TradingDesk\bce124f5-270d-4137-ae19-5370ce11178b"
               r"\scratchpad\bf\bulk_tail.log")

_DAY_RE = re.compile(r"^(\d{8})\.parquet$")


def log(msg: str, log_path: Path) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def bulk_only_roots() -> list[str]:
    """On-disk option root dirs MINUS config.all_roots() = the bulk-only roots."""
    cfg = set(config.all_roots())
    disk = sorted(p.name for p in config.RAW_OPTIONS.iterdir() if p.is_dir())
    return [r for r in disk if r not in cfg]


def _present_days(root: str) -> list[str]:
    d = config.RAW_OPTIONS / root
    if not d.is_dir():
        return []
    out = []
    for f in d.iterdir():
        m = _DAY_RE.match(f.name)
        if m:
            out.append(m.group(1))
    return sorted(out)


def trading_days() -> list[str]:
    """The real trading days in [START_DAY, END_DAY], from the reference root's
    present dates (holiday-correct). Falls back to a weekday filter only if the
    reference root has no files in-window (should not happen — SPY is maintained).
    """
    ref = [x for x in _present_days(REFERENCE_ROOT) if START_DAY <= x <= END_DAY]
    if ref:
        return ref
    # Fallback: Mon-Fri only (no holiday awareness). Emitted with a warning.
    import pandas as pd
    rng = pd.bdate_range(pd.to_datetime(START_DAY), pd.to_datetime(END_DAY))
    return [d.strftime("%Y%m%d") for d in rng]


def main() -> int:
    ap = argparse.ArgumentParser(description="Bulk-only roots EOD tail backfill "
                                             "2026-07-04..2026-07-20")
    ap.add_argument("--log", default=DEFAULT_LOG,
                    help="append-only progress log path (default: scratchpad bf dir)")
    args = ap.parse_args()
    log_path = Path(args.log)

    if not td.connected():
        log(f"Theta Terminal not reachable at {config.THETA_BASE_URL}. Aborting.",
            log_path)
        return 1

    roots = bulk_only_roots()
    days = trading_days()

    used_fallback = not [x for x in _present_days(REFERENCE_ROOT)
                         if START_DAY <= x <= END_DAY]
    if used_fallback:
        log(f"WARNING: reference root {REFERENCE_ROOT} had no in-window files — "
            "fell back to a weekday (no-holiday) calendar.", log_path)

    log(f"=== bulk-tail backfill start === {len(roots)} bulk-only roots × "
        f"{len(days)} trading days ({days[0]}..{days[-1]}) = "
        f"{len(roots) * len(days)} (root,day) cells", log_path)
    log(f"trading days: {days}", log_path)
    log(f"roots: {roots}", log_path)

    filled: list[tuple[str, str, int]] = []
    already: int = 0
    empty: list[tuple[str, str]] = []
    errored: list[tuple[str, str, str]] = []

    for i, root in enumerate(roots, 1):
        r_filled = r_already = r_empty = r_err = 0
        for day in days:
            prefix = f"[{i:>2}/{len(roots)}] {root:<6} {day}"
            try:
                if storage.have_day(root, day):
                    already += 1
                    r_already += 1
                    continue
                df = download.pull_day(root, day)      # settled/historical path
                n = storage.write_day(root, day, df)
                if n == 0:
                    empty.append((root, day))
                    r_empty += 1
                    log(f"{prefix} EMPTY (0 rows) — wrote marker", log_path)
                else:
                    filled.append((root, day, n))
                    r_filled += 1
                    log(f"{prefix} FILLED {n:,} rows", log_path)
            except Exception as e:   # noqa: BLE001 — one bad (root,day) never aborts the run
                errored.append((root, day, f"{type(e).__name__}: {e}"))
                r_err += 1
                log(f"{prefix} ERROR  {type(e).__name__}: {e}", log_path)
        log(f"[{i:>2}/{len(roots)}] {root:<6} done — "
            f"filled={r_filled} already={r_already} empty={r_empty} err={r_err}",
            log_path)

    log("Rebuilding catalog...", log_path)
    try:
        storage.rebuild_catalog()
        log(f"Catalog rebuilt: {config.CATALOG_DB}", log_path)
    except Exception as e:   # noqa: BLE001 — DuckDB has been observed to hard-crash here;
        # the parquet writes above are already durable, so a catalog failure is not
        # a data-loss event. Rebuild on demand later if needed.
        log(f"catalog rebuild skipped: {type(e).__name__}: {e}", log_path)

    log("=" * 60, log_path)
    log("SUMMARY (bulk-tail backfill 2026-07-04..2026-07-20)", log_path)
    log("=" * 60, log_path)
    log(f"roots            : {len(roots)}", log_path)
    log(f"trading days     : {len(days)}", log_path)
    log(f"cells FILLED     : {len(filled)}", log_path)
    log(f"cells already    : {already}", log_path)
    log(f"cells EMPTY      : {len(empty)}", log_path)
    log(f"cells ERRORED    : {len(errored)}", log_path)
    for r, d, msg in errored[:40]:
        log(f"    {r:<6} {d} {msg}", log_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
