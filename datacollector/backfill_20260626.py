"""
backfill_20260626.py — one-shot ThetaData EOD backfill for 2026-06-26.

Last night's IBKR forward run failed 30/43 roots for 6/26, so that date is
missing for those roots. config.GRAB_END="20260625" means the historical
downloader/supervisor never writes 6/26 — this script is the sole writer for
that day. Reuses the existing fetch/write/join logic verbatim:
  download.pull_day  (greeks ⨝ open_interest)
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

DAY = "20260626"


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
