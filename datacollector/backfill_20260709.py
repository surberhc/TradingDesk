"""
backfill_20260709.py — one-shot ThetaData EOD backfill for 2026-07-09.

ThetaEodDaily's scheduled 5:30pm run on 2026-07-09 aborted instantly because the
local ThetaData Terminal was down at that moment (Terminal up-check failed fast,
so nothing was pulled — status.json recorded roots=50, ok=0, fail=0, message
"ThetaData Terminal not reachable — nothing collected"). The watchdog did not
recover the Terminal until ~20:53 that evening, hours after the 5:30pm collection
window closed, so the day was never retried same-night. The next scheduled
eod_daily.py run only targets the current trading day (plus its own short
self-heal look-back window), so it will not go back and fill 7/9 on its own —
this script is the sole writer for that day. Reuses the existing fetch/write/join
logic verbatim:
  download.pull_day  (greeks (join) open_interest)
  storage.have_day / write_day (atomic)  / rebuild_catalog

Resumable: have_day() keys off file presence, so a re-run continues where it
left off if the Terminal stops serving partway.
"""

from __future__ import annotations

import sys

import config
import storage
import download
import thetadata_client as td

DAY = "20260709"


def main() -> int:
    if not td.connected():
        print(f"Theta Terminal not reachable at {config.THETA_BASE_URL}. Aborting.",
              flush=True)
        return 1

    roots = config.all_roots()
    filled: list[tuple[str, int]] = []
    already: list[str] = []
    empty: list[str] = []
    errored: list[tuple[str, str]] = []

    print(f"Backfilling {DAY} for {len(roots)} roots\n", flush=True)

    for i, root in enumerate(roots, 1):
        prefix = f"[{i:>2}/{len(roots)}] {root:<6}"
        try:
            if storage.have_day(root, DAY):
                already.append(root)
                print(f"{prefix} already present — skip", flush=True)
                continue
            df = download.pull_day(root, DAY)
            n = storage.write_day(root, DAY, df)
            if n == 0:
                empty.append(root)
                print(f"{prefix} EMPTY (0 rows) — wrote marker", flush=True)
            else:
                filled.append((root, n))
                print(f"{prefix} FILLED {n:,} rows", flush=True)
        except Exception as e:  # noqa: BLE001 — one bad root must not abort the rest
            errored.append((root, f"{type(e).__name__}: {e}"))
            print(f"{prefix} ERROR  {type(e).__name__}: {e}", flush=True)

    print("\nRebuilding catalog...", flush=True)
    storage.rebuild_catalog()
    print(f"Catalog rebuilt: {config.CATALOG_DB}", flush=True)

    print("\n" + "=" * 60, flush=True)
    print(f"SUMMARY for {DAY}", flush=True)
    print("=" * 60, flush=True)
    print(f"roots processed : {len(roots)}", flush=True)
    print(f"FILLED          : {len(filled)}", flush=True)
    for r, n in filled:
        print(f"    {r:<6} {n:,} rows", flush=True)
    print(f"already present : {len(already)}  {already}", flush=True)
    print(f"EMPTY           : {len(empty)}  {empty}", flush=True)
    print(f"ERRORED         : {len(errored)}", flush=True)
    for r, msg in errored:
        print(f"    {r:<6} {msg}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
