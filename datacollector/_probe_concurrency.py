r"""
_probe_concurrency.py — SHORT scaling probe for the SPX-root 1m collector.

Throwaway measurement harness (NOT the production supervisor). Launches N
concurrent collector instances on SMALL disjoint slices INSIDE the real target
window (2025-05-01..2026-06-18), so the days it pulls count toward the real job.
Measures aggregate days/hour (wall-clock) at each concurrency level and watches
for HTTP 429 / errors / slowdown to find the KNEE.

ASCII-only output (cp1252 console). Spawns each instance with the BASE
interpreter + venv site-packages on PYTHONPATH (same trick as the supervisor) so
each instance is exactly ONE process, not a stub+worker pair.

Run:  python _probe_concurrency.py --level N --slices "s1e1,s2e2,..." --tag LVL
  where each slice is START:END (dashed). Polls until every day in every slice is
  done (both files) or --timeout is hit, then prints days/hour.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import config
import collect_spx_1m as collector

PY = getattr(sys, "_base_executable", None) or sys.executable
HERE = config.CODE_ROOT
COLLECTOR = HERE / "collect_spx_1m.py"
PROBE_LOGS = config.DATA_ROOT / "probe_logs"


def _venv_site() -> str:
    for p in sys.path:
        if p.endswith("site-packages") and "venv" in p.lower():
            return p
    cand = os.path.join(sys.prefix, "Lib", "site-packages")
    return cand if os.path.isdir(cand) else ""


def _child_env() -> dict:
    env = os.environ.copy()
    site = _venv_site()
    if site:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = site + (os.pathsep + existing if existing else "")
    return env


def trading_days(start: str, end: str) -> list[dt.date]:
    s = dt.date.fromisoformat(start)
    e = dt.date.fromisoformat(end)
    out = []
    d = s
    while d <= e:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def days_done(days: list[dt.date]) -> int:
    return sum(1 for d in days if collector.day_done(d))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, help="label for this level, e.g. L4")
    ap.add_argument("--slices", required=True,
                    help="comma list of START:END dashed date slices")
    ap.add_argument("--timeout", type=int, default=1800,
                    help="max seconds to wait for the level to finish")
    args = ap.parse_args()

    PROBE_LOGS.mkdir(parents=True, exist_ok=True)
    slices = [s.split(":") for s in args.slices.split(",")]
    all_days: list[dt.date] = []
    for s, e in slices:
        all_days.extend(trading_days(s, e))
    # de-dup safety
    all_days = sorted(set(all_days))
    target = len(all_days)
    start_done = days_done(all_days)

    print(f"[{args.tag}] launching {len(slices)} instance(s); "
          f"{target} target trading-days ({start_done} already done)",
          flush=True)

    procs = []
    t0 = time.time()
    for i, (s, e) in enumerate(slices):
        prog = PROBE_LOGS / f"probe_{args.tag}_shard{i}.json"
        log = PROBE_LOGS / f"probe_{args.tag}_shard{i}.log"
        cmd = [PY, "-u", str(COLLECTOR), "--start", s, "--end", e,
               "--progress", str(prog), "--log", str(log)]
        p = subprocess.Popen(cmd, cwd=str(HERE), env=_child_env())
        procs.append(p)
        print(f"  shard{i} pid={p.pid} {s}..{e}", flush=True)

    # Poll until all target days done or timeout.
    last_done = start_done
    while True:
        time.sleep(15)
        elapsed = time.time() - t0
        dn = days_done(all_days)
        alive = sum(1 for p in procs if p.poll() is None)
        new = dn - start_done
        rate = (new / elapsed * 3600) if elapsed > 0 else 0.0
        print(f"  [{args.tag}] t={elapsed:6.0f}s done={dn}/{target} "
              f"(+{new} new) alive={alive} rate={rate:.1f} days/hr", flush=True)
        if dn >= target:
            break
        if elapsed > args.timeout:
            print(f"  [{args.tag}] TIMEOUT at {elapsed:.0f}s", flush=True)
            break
        if alive == 0 and dn < target:
            # all instances exited but not all days done -> they hit legit-empty
            # or errored; give one more count then stop.
            print(f"  [{args.tag}] all instances exited early", flush=True)
            break

    # Ensure instances are done; they exit on their own when their slice is done.
    for p in procs:
        try:
            p.wait(timeout=30)
        except Exception:
            p.terminate()

    elapsed = time.time() - t0
    new = days_done(all_days) - start_done
    rate = (new / elapsed * 3600) if elapsed > 0 else 0.0

    # Scan probe shard logs for 429 / error signatures.
    errs = 0
    http429 = 0
    for i in range(len(slices)):
        log = PROBE_LOGS / f"probe_{args.tag}_shard{i}.log"
        if log.exists():
            txt = log.read_text(errors="ignore")
            http429 += txt.count("429")
            errs += txt.count("ERROR")
    print(f"[{args.tag}] RESULT: {new} days in {elapsed:.0f}s = "
          f"{rate:.1f} days/hr | instances={len(slices)} | "
          f"HTTP429={http429} ERRORs={errs}", flush=True)

    result = {
        "tag": args.tag, "instances": len(slices), "new_days": new,
        "elapsed_s": round(elapsed, 1), "days_per_hr": round(rate, 1),
        "http429": http429, "errors": errs,
    }
    (PROBE_LOGS / f"probe_{args.tag}_result.json").write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
