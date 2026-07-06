r"""
condor_base_grid.py -- 0DTE iron-condor BASE-PACKAGE grid runner (ARM 5 of the reopen).

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the warehouse.

WHAT THIS IS
------------
A THIN orchestrator that sweeps the four base-package dials of the 0DTE iron condor and,
for each (entry_time, short_delta, wing_width) BASE PACKAGE, runs the EXISTING managed
minute-walk over all available days ONCE, recording per management arm and per fill fraction:
n, total P&L, avg/trade, win rate, avg win, avg loss, expectancy -- on the FULL sample, on
TRAIN (2022-01..2024-06) and on OOS (2024-07..end).

REUSE, NEVER RE-IMPLEMENT (rule: paperbot/backtester share one code path; here we share the
research engine's one code path too):
  * WING WIDTH + SHORT DELTA + entry credit  : condor_width_sweep.build_condor_at_width
        (the control's own _pick_short_by_delta + _credit_to_open math; the ONLY difference
         from ctrl._build_iron_condor is the parameterized wing distance). It reads the short
         delta from condor_width_sweep.TARGET_SHORT_DELTA, which we set per config.
  * THE 7 MANAGEMENT ARMS (A_hold/B_pt25/50/75/C_t1500/1530/D_combo) at every fill fraction
        (mid/f25/f50/full) : condor_management_experiment._scan_managed_exits_at_fill, the
        exact causal minute-walk that resolves every arm at the first minute its rule binds.
        The full-cross honest arm result is recovered from the "full" fill (f=1.0), which
        reproduces the control byte-for-byte, matching cm._scan_managed_exits' honest path.
  * ENTRY TIME + SETTLEMENT + spot recon + per-strike delta : the control's own snapshot,
        recon.recover_forward_spot, recon.per_strike_delta -- all inherited verbatim.

NO strategy math is copied. The dials are set by assigning module globals BEFORE each base
pass (proven pattern -- credit_by_time.py already showed ctrl.ENTRY_TIME monkeypatching moves
the built condor):
    cm.ENTRY_TIME  = entry            # the managed walk's entry minute
    ws.TARGET_SHORT_DELTA = delta      # the width builder's short-strike delta target
    (wing width is the explicit `width` arg to ws.build_condor_at_width)
cm.TARGET_SHORT_DELTA is also set for parity so any cm-internal path agrees.

GRID (PRE-REGISTERED base package -- NOT swept to a winner)
----------------------------------------------------------
  entry_time  in {09:45, 11:30, 14:00}
  short_delta in {0.10, 0.15, 0.20, 0.30}
  wing_width  in {5, 10, 20} points
  management  = the 7 existing arms (all resolved in ONE minute-walk per base package)
  fill        = mid / f25 / f50 (HEADLINE) / full  (the existing blended net-combo axis)
=> 3 x 4 x 3 = 36 base packages, each covering 7 arms x 4 fills over ~1080 days.

This module is BUILD + MINI-VALIDATION scope. It does NOT launch the full grid on import.

RESUMABLE + CRASH-SAFE
----------------------
One row per (entry, delta, wing, arm, fill) is appended+flushed to a partial CSV. On restart,
any (entry, delta, wing) config already present is SKIPPED (config-level resume). --max-new-configs
caps how many not-yet-done base packages a single process runs, for a fresh-process chunk loop.
ASCII-only console; progress flushes each config.

--mini MODE (fast self-validation)
----------------------------------
Runs ONLY: entry {11:30, 14:00} x delta {0.15, 0.30} x wing {5, 10} x arms {A_hold, D_combo}
over the FIRST 40 available days, to a SEPARATE mini partial CSV. Used to prove all four dials
move the output and to cross-check A_hold against the management experiment's headline.

NO LOOK-AHEAD: entry uses only the entry-minute snapshot; the exit scan (reused verbatim)
freezes each arm at the first binding minute. Inherited from the pinned engine + causality guard.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import gc
from pathlib import Path

import numpy as np
import pandas as pd

import s5_intraday_data as s5
import s6_recon as recon
import s6_control as ctrl
import s6_matrix as mx
import condor_management_experiment as cm
import condor_width_sweep as ws

# --------------------------------------------------------------------------- #
# THE GRID (pre-registered; frozen).
# --------------------------------------------------------------------------- #
ENTRY_TIMES = (_dt.time(9, 45), _dt.time(11, 30), _dt.time(14, 0))
SHORT_DELTAS = (0.10, 0.15, 0.20, 0.30)
WING_WIDTHS = (5.0, 10.0, 20.0)

ARM_NAMES = cm.ARM_NAMES            # the 7 existing arms, resolved in one walk
FILL_FRACS = cm.FILL_FRACS          # (0.0, 0.25, 0.50, 1.0)
HEADLINE_FILL = cm.HEADLINE_FILL    # 0.50
_FILL_TAG = cm._FILL_TAG            # {0.0:"mid",0.25:"f25",0.50:"f50",1.0:"full"}

# Inherited chassis constants (frozen from the control).
SETTLEMENT_TIME = ctrl.SETTLEMENT_TIME
MIN_ENTRY_CREDIT = ctrl.MIN_ENTRY_CREDIT
CONTRACT_MULTIPLIER = ctrl.CONTRACT_MULTIPLIER
N_CONTRACTS = ctrl.N_CONTRACTS

TRAIN_END = mx.TRAIN_END            # 2024-06-30, same OOS split as everything upstream

OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "condor_base_grid"
_PARTIAL_CSV = OUTPUT_DIR / "condor_base_grid_partial.csv"
_MINI_CSV = OUTPUT_DIR / "condor_base_grid_mini.csv"

# --------------------------------------------------------------------------- #
# MINI sub-grid (fast validation).
# --------------------------------------------------------------------------- #
MINI_ENTRIES = (_dt.time(11, 30), _dt.time(14, 0))
MINI_DELTAS = (0.15, 0.30)
MINI_WIDTHS = (5.0, 10.0)
MINI_ARMS = ("A_hold", "D_combo")
MINI_N_DAYS = 40


def _etag(t: _dt.time) -> str:
    return t.strftime("%H%M")


def _dtag(delta: float) -> str:
    return f"d{int(round(delta * 100)):02d}"


def _wtag(width: float) -> str:
    return f"w{int(width)}"


# --------------------------------------------------------------------------- #
# Output row schema: one row per (entry, delta, wing, arm, fill, scope).
# --------------------------------------------------------------------------- #
FIELDNAMES = [
    "entry", "delta", "wing", "arm", "fill", "scope",
    "n", "total_pnl", "avg_pnl", "win_rate", "avg_win", "avg_loss", "expectancy",
    "avg_hold_min", "avg_entry_credit",
]


# --------------------------------------------------------------------------- #
# Per-day, per-arm, per-fill outcome for ONE base package (entry, delta, wing).
# We reuse the SAME primitives the width sweep uses so no strategy math is copied:
#   ws.build_condor_at_width  -> strikes + honest credit at the parameterized delta/width
#   cm._blended_credit_to_open / cm._scan_managed_exits_at_fill -> the managed 7-arm walk
#     at each fill fraction (the exact causal engine, all arms in one pass).
# --------------------------------------------------------------------------- #
def _run_day_base(d: _dt.date, clf: mx.DayClassifier, width: float,
                  day_data: s5.DayData | None = None) -> dict | None:
    """Run one day for a single (entry, delta, wing) base package. `cm.ENTRY_TIME` and
    `ws.TARGET_SHORT_DELTA` MUST already be set by the caller. Returns a dict:
        {"half": str, "fills": {fill_tag: {"entry_credit": float,
                                           arm_name: {"pnl", "hold_min", "exit_reason"}}}}
    or None if the day is not tradeable (no chain / no entry snap / spot recon / credit floor).
    Marks per arm per fill are the blended net-combo P&L, identical to the width sweep's booking.
    """
    try:
        dd = day_data if day_data is not None else s5.load_day(d)
        chain = s5.zero_dte_chain(d, day_data=dd)
        nbbo = chain.nbbo
    except Exception:
        return None
    if nbbo.empty:
        return None

    entry_minute = pd.Timestamp(_dt.datetime.combine(d, cm.ENTRY_TIME))
    settle_minute = pd.Timestamp(_dt.datetime.combine(d, SETTLEMENT_TIME))
    if entry_minute not in set(nbbo["minute"].unique()):
        return None

    entry_snap = ctrl._snap_at(nbbo, entry_minute)
    sr = recon.recover_forward_spot(entry_snap, entry_minute, d)
    if sr is None:
        return None
    spot = float(sr.spot)
    delta_tbl = recon.per_strike_delta(entry_snap, entry_minute, d, spot)

    build = ws.build_condor_at_width(entry_snap, delta_tbl, width)
    if build is None:
        return None
    credit_honest = build["entry_credit"]   # worst-side (full) credit, the control's honest math
    if not np.isfinite(credit_honest) or credit_honest < MIN_ENTRY_CREDIT:
        return None

    out = {"half": "train" if d <= TRAIN_END else "test", "fills": {}}
    for frac in FILL_FRACS:
        tag = _FILL_TAG[frac]
        credit_f = cm._blended_credit_to_open(entry_snap, build["legs"], frac)
        if credit_f is None or not np.isfinite(credit_f):
            continue
        # The exact 7-arm causal minute-walk at this fill fraction (reused verbatim).
        exits_f, _path = cm._scan_managed_exits_at_fill(
            nbbo, build["legs"], credit_f, frac, entry_minute, settle_minute)
        block = {"entry_credit": float(credit_f)}
        ok = True
        for name in ARM_NAMES:
            exf = exits_f[name]
            if not np.isfinite(exf["exit_debit"]):
                ok = False
                break
            pnl = (credit_f - exf["exit_debit"]) * CONTRACT_MULTIPLIER * N_CONTRACTS
            block[name] = {"pnl": float(pnl), "hold_min": float(exf["hold_min"]),
                           "exit_reason": exf["exit_reason"]}
        if ok:
            out["fills"][tag] = block
    if not out["fills"]:
        return None
    return out


# --------------------------------------------------------------------------- #
# Stats over a per-day P&L vector (matches the management experiment's fields + adds
# avg_win / avg_loss / expectancy explicitly as the task asks).
# --------------------------------------------------------------------------- #
def _stats(pnl: np.ndarray, hold: np.ndarray, credit: np.ndarray) -> dict:
    x = pnl[np.isfinite(pnl)]
    n = len(x)
    if n == 0:
        return dict(n=0, total_pnl=0.0, avg_pnl=float("nan"), win_rate=float("nan"),
                    avg_win=float("nan"), avg_loss=float("nan"), expectancy=float("nan"),
                    avg_hold_min=float("nan"), avg_entry_credit=float("nan"))
    wins = x[x > 0]
    losses = x[x <= 0]
    win_rate = len(wins) / n
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    # expectancy per trade = win_rate*avg_win + (1-win_rate)*avg_loss (== avg_pnl by identity,
    # reported explicitly as the task requests so the decomposition is on the row).
    expectancy = win_rate * avg_win + (1.0 - win_rate) * avg_loss
    h = hold[np.isfinite(hold)]
    c = credit[np.isfinite(credit)]
    return dict(
        n=n,
        total_pnl=round(float(x.sum()), 2),
        avg_pnl=round(float(x.mean()), 4),
        win_rate=round(float(win_rate), 4),
        avg_win=round(avg_win, 4),
        avg_loss=round(avg_loss, 4),
        expectancy=round(float(expectancy), 4),
        avg_hold_min=round(float(h.mean()), 2) if len(h) else float("nan"),
        avg_entry_credit=round(float(c.mean()), 4) if len(c) else float("nan"),
    )


# --------------------------------------------------------------------------- #
# One BASE PACKAGE: (entry, delta, wing). Set the dials, walk all days ONCE, and emit
# one row per (arm, fill, scope in {full,train,test}).
# --------------------------------------------------------------------------- #
def run_base_package(entry: _dt.time, delta: float, width: float,
                     days: list[_dt.date], clf: mx.DayClassifier,
                     verbose: bool = True) -> list[dict]:
    # Set the dials. cm.run_day reads cm.ENTRY_TIME / cm.TARGET_SHORT_DELTA as globals;
    # ws.build_condor_at_width reads ws.TARGET_SHORT_DELTA. Set all three for parity.
    cm.ENTRY_TIME = entry
    cm.TARGET_SHORT_DELTA = delta
    ws.TARGET_SHORT_DELTA = delta

    # Collect per-day, per-arm, per-fill P&L / hold / credit.
    # store[tag][arm] -> list of pnl;  hold[tag][arm] -> list;  credit[tag] -> list
    pnl = {tag: {a: [] for a in ARM_NAMES} for tag in _FILL_TAG.values()}
    hold = {tag: {a: [] for a in ARM_NAMES} for tag in _FILL_TAG.values()}
    half = {tag: {a: [] for a in ARM_NAMES} for tag in _FILL_TAG.values()}  # parallel 'train'/'test'
    credit = {tag: [] for tag in _FILL_TAG.values()}
    n_traded = 0
    for d in days:
        try:
            dd = s5.load_day(d)
            res = _run_day_base(d, clf, width, day_data=dd)
        except Exception:
            res = None
        if res is not None:
            n_traded += 1
            for tag, block in res["fills"].items():
                credit[tag].append(block["entry_credit"])
                for a in ARM_NAMES:
                    if a in block:
                        pnl[tag][a].append(block[a]["pnl"])
                        hold[tag][a].append(block[a]["hold_min"])
                        half[tag][a].append(res["half"])
        del dd, res
        gc.collect()

    etag, dtag, wtag = _etag(entry), _dtag(delta), _wtag(width)
    rows = []
    for tag in _FILL_TAG.values():
        for a in ARM_NAMES:
            p = np.asarray(pnl[tag][a], dtype=float)
            hh = np.asarray(hold[tag][a], dtype=float)
            hv = np.asarray(half[tag][a], dtype=object)
            cc = np.asarray(credit[tag], dtype=float)   # credit is per-day (arm-independent)
            for scope in ("full", "train", "test"):
                if scope == "full":
                    m = np.ones(len(p), dtype=bool)
                    cm_mask = np.ones(len(cc), dtype=bool)
                else:
                    m = (hv == scope) if len(hv) else np.zeros(0, dtype=bool)
                    cm_mask = m if len(m) == len(cc) else m
                st = _stats(p[m] if len(m) else p[:0],
                            hh[m] if len(m) else hh[:0],
                            cc[cm_mask] if len(cm_mask) == len(cc) else cc[:0])
                rows.append(dict(entry=etag, delta=delta, wing=int(width), arm=a,
                                 fill=tag, scope=scope, **st))
    if verbose:
        # headline line: A_hold + D_combo full-sample f50 totals.
        def _tot(a):
            return next((r["total_pnl"] for r in rows
                         if r["arm"] == a and r["fill"] == "f50" and r["scope"] == "full"), float("nan"))
        print(f"  [{etag} {dtag} {wtag}] traded={n_traded}  "
              f"f50/full A_hold=${_tot('A_hold'):,.0f}  D_combo=${_tot('D_combo'):,.0f}",
              flush=True)
    return rows


# --------------------------------------------------------------------------- #
# Full-grid orchestration -- resumable + crash-safe + chunk cap.
# --------------------------------------------------------------------------- #
def _done_configs(csv_path: Path) -> set[tuple]:
    if not csv_path.is_file():
        return set()
    try:
        prev = pd.read_csv(csv_path, usecols=["entry", "delta", "wing"])
    except Exception:
        return set()
    return {(str(r.entry), float(r.delta), int(r.wing)) for r in prev.itertuples()}


def run_grid(entries=ENTRY_TIMES, deltas=SHORT_DELTAS, widths=WING_WIDTHS,
             days: list[_dt.date] | None = None, csv_path: Path = _PARTIAL_CSV,
             max_new_configs: int = 0, verbose: bool = True) -> pd.DataFrame:
    if days is None:
        days = s5.available_days()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    clf = mx.DayClassifier()

    done = _done_configs(csv_path)
    if verbose and done:
        print(f"resume: {len(done)} base package(s) already done; skipping", flush=True)

    write_header = not csv_path.is_file()
    configs = [(e, d, w) for e in entries for d in deltas for w in widths]
    n_total = len(configs)
    n_new = 0
    with open(csv_path, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES, restval="", extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for i, (e, d, w) in enumerate(configs, 1):
            key = (_etag(e), float(d), int(w))
            if key in done:
                continue
            if max_new_configs and n_new >= max_new_configs:
                if verbose:
                    print(f"chunk cap reached: {n_new} new config(s) this run; exiting cleanly "
                          f"(resume next chunk).", flush=True)
                break
            if verbose:
                print(f"[config {i}/{n_total}] entry={_etag(e)} delta={d} wing={int(w)} ...",
                      flush=True)
            rows = run_base_package(e, d, w, days, clf, verbose=verbose)
            for r in rows:
                writer.writerow(r)
            fh.flush()
            n_new += 1
        if verbose:
            print(f"run_grid done: {n_new} new base package(s) this run.", flush=True)

    df = pd.read_csv(csv_path)
    return df


# --------------------------------------------------------------------------- #
# MINI mode: tiny sub-grid over the first 40 days, to a separate CSV.
# --------------------------------------------------------------------------- #
def run_mini(verbose: bool = True) -> pd.DataFrame:
    days = s5.available_days()[:MINI_N_DAYS]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    clf = mx.DayClassifier()
    if _MINI_CSV.is_file():
        _MINI_CSV.unlink()   # mini is a fresh self-check each run, not resumed.
    write_header = True
    with open(_MINI_CSV, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES, restval="", extrasaction="ignore")
        writer.writeheader()
        for e in MINI_ENTRIES:
            for d in MINI_DELTAS:
                for w in MINI_WIDTHS:
                    if verbose:
                        print(f"[mini] entry={_etag(e)} delta={d} wing={int(w)} "
                              f"({MINI_N_DAYS} days) ...", flush=True)
                    rows = run_base_package(e, d, w, days, clf, verbose=verbose)
                    # keep only the mini arms.
                    rows = [r for r in rows if r["arm"] in MINI_ARMS]
                    for r in rows:
                        writer.writerow(r)
                    fh.flush()
    df = pd.read_csv(_MINI_CSV)
    if verbose:
        print(f"\nSaved {_MINI_CSV}", flush=True)
    return df


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="0DTE iron-condor BASE-PACKAGE grid runner")
    ap.add_argument("--mini", action="store_true",
                    help="run the tiny validation sub-grid over the first 40 days")
    ap.add_argument("--max-new-configs", type=int, default=0,
                    help="process at most N not-yet-done base packages this run, then exit "
                         "cleanly (fresh-process chunk loop). 0 = no cap.")
    ap.add_argument("--limit-days", type=int, default=0,
                    help="run the grid over only the first N days (smoke test).")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.mini:
        run_mini(verbose=not args.quiet)
    else:
        days = s5.available_days()
        if args.limit_days:
            days = days[: args.limit_days]
        run_grid(days=days, max_new_configs=args.max_new_configs, verbose=not args.quiet)
