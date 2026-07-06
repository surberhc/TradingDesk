r"""
condor_width_sweep.py — ARM 1 of the 0DTE iron-condor reopen (docs\PREREG_condor_reopen_2026-07-06.md):
the STRIKE-WIDTH sweep. Does WIDENING the condor's wings fix the transaction-cost drag that
kills the 5-pt 0DTE iron condor?

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the warehouse.

THE HYPOTHESIS (mechanism-first)
--------------------------------
Every prior 0DTE-condor refutation shares ONE honest finding (output/condor_management_20260703.md):
the condor harvests theta (positive at mid fill) but the **4-leg bid/ask spread on thin 0DTE
premium swamps the edge at realistic (>=50%) fills**. The binding constraint is TRANSACTION COST,
not strategy logic. A 5-pt condor collects a tiny credit and pays 4 bid/ask spreads to open and
(when managed) 4 more to close; the spread is a large FRACTION of that thin credit.

Proposed lever (Andrew's): WIDER wings collect MORE credit per condor while the per-leg bid/ask
spread grows sub-proportionally, so the spread-cost-as-%-of-credit should FALL as width rises. IF
that is real, some plateau of wider widths flips the managed condor from net-loss to net-positive
after honest 4-leg costs at the f50 headline fill. IF not, it dies -- a clean refutation is valid.

WHAT CHANGES vs WHAT IS FROZEN (rule #1 anti-curve-fit)
-------------------------------------------------------
FROZEN, inherited VERBATIM from the control (s6_control): entry 14:00 ET, short strike at
0.15-delta (picked by the control's own _pick_short_by_delta on the control's own per-strike
deltas), settlement 16:00, $0.05 winner brake, 2x-credit stop, $0.30 min-credit no-trade floor,
honest 4-leg bid/ask fills (the control's own _credit_to_open / _spread_debit_to_close math).
ONLY THE WING WIDTH CHANGES: the long put sits WIDTH points BELOW the short put and the long call
WIDTH points ABOVE the short call.

Management is FIXED across all widths at the prior run's best RISK arm: profit-target 25% of credit
OR 2x-credit stop OR settle, whichever binds first (the B_pt25 rule, reused verbatim from
condor_management_experiment's exit scan).

THE GRID (PRE-REGISTERED, NOT swept-to-winner)
----------------------------------------------
Wing widths: 5 (control) / 10 / 20 / 30 / 50-pt. Exactly these five. No cells added, none removed.

HONEST COSTS (the crux)
-----------------------
Entry and the managed exit are HONEST 4-leg bid/ask fills. Widening the wings buys a long option
that is FURTHER OTM (cheaper) so the NET credit RISES with width, but the wing still costs real
premium to buy at the ASK -- booked. We report, per width, the total credit collected AND the
4-leg net spread cost as a % of that credit (mid-credit minus worst-side-credit, over mid-credit),
which is the exact quantity the whole thesis rests on.

FILL BAND: P&L reported across mid / f25 / f50 (HEADLINE) / full, same blended net-combo axis as
the management experiment. f=1.0 reproduces the control's honest worst-side bound.

NO LOOK-AHEAD: entry uses ONLY the 14:00 snapshot; the exit scan walks minutes forward and freezes
each width at the FIRST minute its rule binds; a later minute can never rewrite an earlier exit.
Pinned by tests/test_condor_width_sweep.py + the standing causality guard.

MATCHED PLACEBO: any width that beats the 5-pt control at f50 gets a random-exit placebo matched to
that width's mean holding time (the management experiment's exact path-based placebo).

PASS (stated before results): a POSITIVE PLATEAU across >=3 ADJACENT widths at the f50 headline
fill AND OOS-stable (test half positive) AND beats the matched placebo. A single positive width is
NOT a pass and is reported as such.

CRASH-RESILIENT + RESUMABLE: per-day incremental CSV append + resume-skip; --max-new-days chunk cap
for a fresh-process loop that beats OOM; del + gc.collect() each day. ASCII-only console output.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import gc
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd

import s5_intraday_data as s5
import s6_recon as recon
import s6_control as ctrl
import s6_matrix as mx
import condor_management_experiment as cm

# --------------------------------------------------------------------------- #
# Entry chassis -- inherited VERBATIM from the control (NOT re-tuned).
# --------------------------------------------------------------------------- #
ENTRY_TIME = ctrl.ENTRY_TIME              # 14:00 ET
SETTLEMENT_TIME = ctrl.SETTLEMENT_TIME    # 16:00 ET
TARGET_SHORT_DELTA = ctrl.TARGET_SHORT_DELTA  # 0.15
MIN_ENTRY_CREDIT = ctrl.MIN_ENTRY_CREDIT  # 0.30
WINNER_DEBIT = ctrl.WINNER_DEBIT          # 0.05
STOP_MULTIPLE = ctrl.STOP_MULTIPLE        # 2.0
CONTRACT_MULTIPLIER = ctrl.CONTRACT_MULTIPLIER
N_CONTRACTS = ctrl.N_CONTRACTS

# --------------------------------------------------------------------------- #
# THE ONLY SWEPT AXIS -- pre-registered wing widths (points). Frozen; NOT swept to a winner.
# 5.0 is the control (ctrl.SPREAD_WIDTH) and MUST be present so the sweep contains its baseline.
# --------------------------------------------------------------------------- #
WIDTHS = (5.0, 10.0, 20.0, 30.0, 50.0)
assert ctrl.SPREAD_WIDTH in WIDTHS, "the sweep must contain the 5-pt control width"

# --------------------------------------------------------------------------- #
# FIXED management across all widths = the prior run's best RISK arm (B_pt25):
# profit-target 25% of credit OR 2x-credit stop OR settle, whichever binds first.
# --------------------------------------------------------------------------- #
PROFIT_TARGET_FRAC = 0.25                 # take profit at 25% of entry credit (the B_pt25 rule)

# --------------------------------------------------------------------------- #
# FILL BAND -- the same blended net-combo execution axis as the management experiment.
#   entry credit(f) = (1-f)*net_mid_credit + f*worst_credit
#   close  debit(f) = (1-f)*net_mid_debit  + f*worst_debit
# f=0 -> mid (optimistic); f=1 -> worst-side every leg (the control's honest bound). f=0.5 headline.
# We REUSE the management experiment's own blended-fill functions so the fill math is one code path.
# --------------------------------------------------------------------------- #
FILL_FRACS = cm.FILL_FRACS                # (0.0, 0.25, 0.50, 1.0)
HEADLINE_FILL = cm.HEADLINE_FILL          # 0.50
_FILL_TAG = cm._FILL_TAG                   # {0.0:"mid",0.25:"f25",0.50:"f50",1.0:"full"}

TRAIN_END = mx.TRAIN_END                  # 2024-06-30, same OOS split as everything upstream

OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "condor_width_sweep"
_PARTIAL_CSV = OUTPUT_DIR / "condor_width_sweep_partial.csv"

# Off-Drive cache for the per-day honest debit PATHS (per width, per fill) for the placebo.
LOCAL_CACHE_DIR = Path(r"C:\TradingDesk-Local\state\condor_width_sweep")
PATHS_DIR = LOCAL_CACHE_DIR / "paths"

# Column tag per width, e.g. "w5", "w10". Kept ASCII + filesystem-safe.
def _wtag(w: float) -> str:
    return f"w{int(w)}"

WIDTH_TAGS = tuple(_wtag(w) for w in WIDTHS)


# --------------------------------------------------------------------------- #
# WIDTH-AWARE iron-condor builder. Reuses the control's OWN strike-selection + fill math;
# the ONLY difference from ctrl._build_iron_condor is the long-wing distance (WIDTH, not the
# frozen module constant ctrl.SPREAD_WIDTH). Short strikes are the control's 0.15-delta picks,
# unchanged, so the entry chassis is identical bar the wing.
# --------------------------------------------------------------------------- #
def build_condor_at_width(snap: pd.DataFrame, delta_tbl: pd.DataFrame, width: float) -> dict | None:
    """Iron condor at a given wing WIDTH. Short put/call at TARGET_SHORT_DELTA (control's pick);
    long put WIDTH below the short put, long call WIDTH above the short call. Honest net credit
    via the control's own _credit_to_open (sell shorts at BID, buy wings at ASK). Returns None if
    any leg is unquoted (never invent a fill)."""
    short_put_k = ctrl._pick_short_by_delta(delta_tbl, "PUT", TARGET_SHORT_DELTA)
    short_call_k = ctrl._pick_short_by_delta(delta_tbl, "CALL", TARGET_SHORT_DELTA)
    if short_put_k is None or short_call_k is None:
        return None
    long_put_k = short_put_k - width
    long_call_k = short_call_k + width

    spq = ctrl._leg_quote(snap, short_put_k, "PUT")
    lpq = ctrl._leg_quote(snap, long_put_k, "PUT")
    scq = ctrl._leg_quote(snap, short_call_k, "CALL")
    lcq = ctrl._leg_quote(snap, long_call_k, "CALL")
    if spq is None or lpq is None or scq is None or lcq is None:
        return None

    sp_bid, sp_ask = spq
    lp_bid, lp_ask = lpq
    sc_bid, sc_ask = scq
    lc_bid, lc_ask = lcq
    # Honest net credit: put spread (short bid - long ask) + call spread (short bid - long ask).
    put_credit = ctrl._credit_to_open(sp_bid, lp_ask)
    call_credit = ctrl._credit_to_open(sc_bid, lc_ask)
    credit = put_credit + call_credit

    sd = delta_tbl[(delta_tbl["strike"] == short_put_k) & (delta_tbl["right"] == "PUT")]
    return {
        "short_put_k": short_put_k, "long_put_k": long_put_k,
        "short_call_k": short_call_k, "long_call_k": long_call_k,
        "entry_credit": credit,
        "entry_short_delta": float(sd["delta"].iloc[0]) if not sd.empty else float("nan"),
        # +1 = SHORT leg (sold), -1 = LONG leg (bought) -- the control's sign convention.
        "legs": [(short_put_k, "PUT", +1), (long_put_k, "PUT", -1),
                 (short_call_k, "CALL", +1), (long_call_k, "CALL", -1)],
    }


# --------------------------------------------------------------------------- #
# The B_pt25 managed exit scan at ONE fill fraction. Same causal minute-walk contract as the
# management experiment: winner (<=WINNER_DEBIT) / stop (>=(1+2x)*credit) / take-profit at 25% /
# settle. Marks are the blended net-combo debit at fill_frac (REUSING cm._blended_debit_to_close),
# and the 25% target is measured against entry_credit_f at the SAME fraction. Freezes at the first
# binding minute; a later minute never rewrites it. Returns ({exit_reason, exit_minute, exit_debit,
# hold_min}, path) where path is (offset_min, blended_debit) for the placebo.
# --------------------------------------------------------------------------- #
def scan_pt25_exit_at_fill(
    nbbo: pd.DataFrame,
    legs: list[tuple],
    entry_credit_f: float,
    fill_frac: float,
    entry_minute: pd.Timestamp,
    settle_minute: pd.Timestamp,
    profit_frac: float = PROFIT_TARGET_FRAC,
) -> tuple[dict, np.ndarray]:
    minutes = sorted(m for m in nbbo["minute"].unique()
                     if entry_minute < m <= settle_minute)
    stop_debit = (1.0 + STOP_MULTIPLE) * entry_credit_f
    target_debit = (1.0 - profit_frac) * entry_credit_f   # open P&L >= f*credit <=> debit <= (1-f)*credit
    result = None
    last_debit = float("nan")
    last_minute = entry_minute
    path_rows: list[tuple[float, float]] = []

    def resolve(reason, minute, debit):
        return {"exit_reason": reason, "exit_minute": minute, "exit_debit": float(debit),
                "hold_min": (minute - entry_minute).total_seconds() / 60.0}

    for m in minutes:
        snap = ctrl._snap_at(nbbo, m)
        debit = cm._blended_debit_to_close(snap, legs, fill_frac)
        if debit is None:
            continue  # unquoted minute -> cannot act; never invent a fill.
        last_debit, last_minute = debit, m
        path_rows.append(((m - entry_minute).total_seconds() / 60.0, float(debit)))
        if result is not None:
            continue  # already exited; keep extending the path only for diagnostics? No -> break.
        if debit <= WINNER_DEBIT:
            result = resolve("winner", m, debit)
        elif debit >= stop_debit:
            result = resolve("stop", m, debit)
        elif debit <= target_debit:
            result = resolve("target", m, debit)
        if result is not None:
            break  # frozen at the first binding minute -- no look-ahead past it.

    if result is None:
        result = resolve("settle", last_minute, last_debit)
    path = np.asarray(path_rows, dtype=float) if path_rows else np.empty((0, 2))
    return result, path


# --------------------------------------------------------------------------- #
# Per-day record: one row per day, with per-(width x fill) P&L / exit / hold + the cost-drag
# quantities (mid credit and worst-side credit per width, for the spread-cost-%-of-credit report).
# --------------------------------------------------------------------------- #
@dataclass
class DayRecord:
    day: _dt.date
    traded: bool = False
    skip_reason: str = ""
    entry_spot: float = float("nan")
    gamma_regime: str = "unknown"
    vix_regime: str = "unknown"
    half: str = ""
    # per-width blocks attached dynamically; flattened on write.
    widths: dict = None

    def flat(self) -> dict:
        base = {k: v for k, v in asdict(self).items() if k != "widths"}
        w = self.widths or {}
        for wt in WIDTH_TAGS:
            sub = w.get(wt, {})
            base[f"short_put_k_{wt}"] = sub.get("short_put_k", float("nan"))
            base[f"short_call_k_{wt}"] = sub.get("short_call_k", float("nan"))
            base[f"credit_mid_{wt}"] = sub.get("credit_mid", float("nan"))
            base[f"credit_full_{wt}"] = sub.get("credit_full", float("nan"))
            for frac in FILL_FRACS:
                tag = _FILL_TAG[frac]
                base[f"entry_credit_{wt}_{tag}"] = sub.get(tag, {}).get("entry_credit", float("nan"))
                base[f"pnl_{wt}_{tag}"] = sub.get(tag, {}).get("pnl", float("nan"))
                base[f"exit_{wt}_{tag}"] = sub.get(tag, {}).get("exit_reason", "")
                base[f"holdmin_{wt}_{tag}"] = sub.get(tag, {}).get("hold_min", float("nan"))
        return base


def _flat_fieldnames() -> list[str]:
    base = [k for k in asdict(DayRecord(day=_dt.date(2022, 1, 3))).keys() if k != "widths"]
    for wt in WIDTH_TAGS:
        base += [f"short_put_k_{wt}", f"short_call_k_{wt}",
                 f"credit_mid_{wt}", f"credit_full_{wt}"]
        for frac in FILL_FRACS:
            tag = _FILL_TAG[frac]
            base += [f"entry_credit_{wt}_{tag}", f"pnl_{wt}_{tag}",
                     f"exit_{wt}_{tag}", f"holdmin_{wt}_{tag}"]
    return base


def write_header_ok(fieldnames: list[str]) -> bool:
    """True iff the existing partial CSV header EXACTLY matches the current schema."""
    try:
        with open(_PARTIAL_CSV, newline="") as fh:
            existing = next(csv.reader(fh))
        return existing == fieldnames
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# One day: for each width, build the 0.15-delta condor at 14:00, run the B_pt25 managed exit at
# every fill fraction, book P&L, cache the debit path for the placebo.
# --------------------------------------------------------------------------- #
def run_day(d: _dt.date, clf: mx.DayClassifier,
            day_data: s5.DayData | None = None,
            save_path: bool = True) -> DayRecord:
    rec = DayRecord(day=d, widths={})
    rec.half = "train" if d <= TRAIN_END else "test"
    lab = clf.classify(d)
    rec.gamma_regime, rec.vix_regime = lab["gamma_regime"], lab["vix_regime"]

    try:
        dd = day_data if day_data is not None else s5.load_day(d)
        chain = s5.zero_dte_chain(d, day_data=dd)
        nbbo = chain.nbbo
    except Exception as e:
        rec.skip_reason = f"load error: {type(e).__name__}"
        return rec
    if nbbo.empty:
        rec.skip_reason = "no 0dte chain"
        return rec

    entry_minute = pd.Timestamp(_dt.datetime.combine(d, ENTRY_TIME))
    settle_minute = pd.Timestamp(_dt.datetime.combine(d, SETTLEMENT_TIME))
    if entry_minute not in set(nbbo["minute"].unique()):
        rec.skip_reason = "no 14:00 snapshot"
        return rec

    entry_snap = ctrl._snap_at(nbbo, entry_minute)
    sr = recon.recover_forward_spot(entry_snap, entry_minute, d)
    if sr is None:
        rec.skip_reason = "spot recon failed at entry"
        return rec
    rec.entry_spot = float(sr.spot)
    delta_tbl = recon.per_strike_delta(entry_snap, entry_minute, d, rec.entry_spot)

    any_traded = False
    for w in WIDTHS:
        wt = _wtag(w)
        build = build_condor_at_width(entry_snap, delta_tbl, w)
        if build is None:
            rec.widths[wt] = {"skip_reason": "could not build condor at width"}
            continue
        credit_honest = build["entry_credit"]   # worst-side (full) credit, the control's honest math
        if not np.isfinite(credit_honest) or credit_honest < MIN_ENTRY_CREDIT:
            rec.widths[wt] = {"skip_reason": f"credit {credit_honest:.2f} < {MIN_ENTRY_CREDIT}"}
            continue
        credit_mid = cm._credit_mid(entry_snap, build["legs"])
        block = {
            "short_put_k": build["short_put_k"], "short_call_k": build["short_call_k"],
            "credit_mid": float(credit_mid) if credit_mid is not None else float("nan"),
            "credit_full": float(credit_honest),
        }
        wt_traded = True
        for frac in FILL_FRACS:
            tag = _FILL_TAG[frac]
            credit_f = cm._blended_credit_to_open(entry_snap, build["legs"], frac)
            if credit_f is None or not np.isfinite(credit_f):
                block[tag] = {"entry_credit": float("nan"), "pnl": float("nan"),
                              "exit_reason": "no-entry-quote", "hold_min": float("nan")}
                wt_traded = False
                continue
            exf, path_f = scan_pt25_exit_at_fill(
                nbbo, build["legs"], credit_f, frac, entry_minute, settle_minute)
            if not np.isfinite(exf["exit_debit"]):
                block[tag] = {"entry_credit": float(credit_f), "pnl": float("nan"),
                              "exit_reason": "no-close-quote", "hold_min": float("nan")}
                wt_traded = False
                continue
            pnl = (credit_f - exf["exit_debit"]) * CONTRACT_MULTIPLIER * N_CONTRACTS
            block[tag] = {"entry_credit": float(credit_f), "pnl": float(pnl),
                          "exit_reason": exf["exit_reason"], "hold_min": exf["hold_min"]}
            if save_path and len(path_f):
                fdir = PATHS_DIR / wt / tag
                fdir.mkdir(parents=True, exist_ok=True)
                pd.DataFrame(path_f, columns=["offset_min", "debit"]).to_parquet(
                    fdir / f"{d.strftime('%Y%m%d')}.parquet", index=False)
        rec.widths[wt] = block
        any_traded = any_traded or wt_traded

    rec.traded = any_traded
    if not any_traded and not rec.skip_reason:
        rec.skip_reason = "no width tradeable"
    return rec


# --------------------------------------------------------------------------- #
# Full-history run -- crash-resilient + resumable + heartbeat + chunk cap.
# --------------------------------------------------------------------------- #
def run_history(days: list[_dt.date] | None = None, verbose: bool = True,
                save: bool = True, resume: bool = True,
                max_new_days: int = 0) -> pd.DataFrame:
    if days is None:
        days = s5.available_days()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    clf = mx.DayClassifier()

    done_days: set[str] = set()
    if resume and _PARTIAL_CSV.is_file():
        try:
            prev = pd.read_csv(_PARTIAL_CSV, usecols=["day"])
            done_days = set(prev["day"].astype(str).unique())
        except Exception:
            done_days = set()
    if verbose and done_days:
        print(f"resume: {len(done_days)} days already done; skipping", flush=True)

    n = len(days)
    fieldnames = _flat_fieldnames()
    if _PARTIAL_CSV.is_file() and not write_header_ok(fieldnames):
        raise SystemExit(
            f"{_PARTIAL_CSV} header does not match the current schema. "
            f"Move/delete it for a clean run.")
    write_header = not _PARTIAL_CSV.is_file()

    with open(_PARTIAL_CSV, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, restval="", extrasaction="ignore")
        if write_header:
            writer.writeheader()
        n_crash_skips = 0
        n_new = 0
        hit_chunk_cap = False
        n_remaining = sum(1 for d in days if str(d) not in done_days)
        for i, d in enumerate(days, 1):
            if str(d) in done_days:
                continue
            if max_new_days and n_new >= max_new_days:
                hit_chunk_cap = True
                if verbose:
                    print(f"chunk cap reached: {n_new} new day(s) this run; exiting cleanly "
                          f"(resume next chunk from here).", flush=True)
                break
            dd = None
            try:
                dd = s5.load_day(d)
                rec = run_day(d, clf, day_data=dd)
            except Exception as e:
                n_crash_skips += 1
                rec = DayRecord(day=d, widths={})
                rec.half = "train" if d <= TRAIN_END else "test"
                rec.skip_reason = f"crash-skip: {type(e).__name__}: {str(e)[:80]}"
                if verbose:
                    print(f"[{i}/{n}] {d} CRASH-SKIP {rec.skip_reason}", flush=True)
            writer.writerow(rec.flat())
            fh.flush()
            n_new += 1
            if verbose and (n_new % 10 == 0):
                print(f"HEARTBEAT: {n_new} done this chunk / {n_remaining - n_new} remaining "
                      f"(last {d}, traded={rec.traded}) [crash-skips: {n_crash_skips}]", flush=True)
            del dd, rec
            gc.collect()
        if verbose:
            done_msg = "chunk done" if hit_chunk_cap else "run_history complete"
            print(f"{done_msg}: {n_new} new day(s) processed, "
                  f"{n_crash_skips} crash-skipped day(s).", flush=True)

    df = pd.read_csv(_PARTIAL_CSV)
    df["traded"] = df["traded"].astype(str).str.lower().isin(["true", "1"])
    df["day"] = pd.to_datetime(df["day"]).dt.date
    df = df.sort_values("day").reset_index(drop=True)
    if save:
        df.to_csv(OUTPUT_DIR / "condor_width_sweep_days.csv", index=False)
        if verbose:
            print(f"Saved {OUTPUT_DIR / 'condor_width_sweep_days.csv'}", flush=True)
    return df


# --------------------------------------------------------------------------- #
# Stats helpers (mirror the management experiment's arm_stats_fill).
# --------------------------------------------------------------------------- #
def _ann_sharpe(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 2 or x.std(ddof=1) == 0:
        return float("nan")
    return float(x.mean() / x.std(ddof=1) * np.sqrt(252))


def _ann_sortino(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 2:
        return float("nan")
    downside = x[x < 0]
    if len(downside) < 1:
        return float("nan")
    dd = np.sqrt(np.mean(downside ** 2))
    if dd == 0:
        return float("nan")
    return float(x.mean() / dd * np.sqrt(252))


def width_stats_fill(df: pd.DataFrame, wt: str, tag: str, sub: pd.DataFrame | None = None) -> dict:
    """Per-width stats at ONE fill fraction (column pnl_{wt}_{tag})."""
    t = (sub if sub is not None else df)
    t = t[t["traded"]].copy()
    col = f"pnl_{wt}_{tag}"
    if col not in t.columns:
        return {"width": wt, "fill": tag, "trades": 0, "total_$": 0.0}
    x = t[col].to_numpy(dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n == 0:
        return {"width": wt, "fill": tag, "trades": 0, "total_$": 0.0}
    wins = x[x > 0]
    hcol = f"holdmin_{wt}_{tag}"
    hold = t[hcol].to_numpy(dtype=float) if hcol in t.columns else np.array([])
    hold = hold[np.isfinite(hold)]
    return {
        "width": wt, "fill": tag, "trades": n,
        "total_$": round(float(x.sum()), 2),
        "win_rate": round(len(wins) / n, 4),
        "avg_$": round(float(x.mean()), 2),
        "worst_day_$": round(float(x.min()), 2),
        "p05_$": round(float(np.percentile(x, 5)), 2),
        "std_$": round(float(x.std(ddof=1)), 2),
        "sharpe_ann": round(_ann_sharpe(x), 3),
        "sortino_ann": round(_ann_sortino(x), 3),
        "avg_hold_min": round(float(hold.mean()), 1) if len(hold) else float("nan"),
    }


def credit_cost_table(df: pd.DataFrame) -> pd.DataFrame:
    """Per width: total credit collected (worst-side + mid), and the 4-leg spread cost as % of
    credit = (mid_credit - worst_credit) / mid_credit, averaged per day then reported overall.

    This is the crux quantity: as width rises, does the spread eat a SMALLER fraction of the
    (larger) credit? Uses per-day credit_mid / credit_full captured at entry (no exit).
    """
    t = df[df["traded"]].copy()
    rows = []
    for w in WIDTHS:
        wt = _wtag(w)
        cm_col, cf_col = f"credit_mid_{wt}", f"credit_full_{wt}"
        if cm_col not in t.columns:
            continue
        cmid = t[cm_col].to_numpy(dtype=float)
        cfull = t[cf_col].to_numpy(dtype=float)
        ok = np.isfinite(cmid) & np.isfinite(cfull) & (cmid > 0)
        cmid, cfull = cmid[ok], cfull[ok]
        if len(cmid) == 0:
            continue
        # spread cost (points) = mid credit - worst-side credit (the full 4-leg bid/ask crossed).
        spread_cost = cmid - cfull
        pct = spread_cost / cmid           # fraction of the mid credit the spread eats
        rows.append({
            "width": wt,
            "n_days": len(cmid),
            "tot_credit_mid_$": round(float(cmid.sum() * CONTRACT_MULTIPLIER * N_CONTRACTS), 0),
            "tot_credit_full_$": round(float(cfull.sum() * CONTRACT_MULTIPLIER * N_CONTRACTS), 0),
            "avg_credit_mid_pts": round(float(cmid.mean()), 3),
            "avg_credit_full_pts": round(float(cfull.mean()), 3),
            "avg_spread_cost_pts": round(float(spread_cost.mean()), 3),
            "spread_cost_pct_of_credit": round(float(pct.mean()), 4),
        })
    return pd.DataFrame(rows).set_index("width")


# --------------------------------------------------------------------------- #
# Placebo (reuses the management experiment's exact path-based matched random-exit).
# --------------------------------------------------------------------------- #
def load_paths(days: list[_dt.date], wt: str, tag: str) -> dict:
    base = PATHS_DIR / wt / tag
    out = {}
    for d in days:
        p = base / f"{d.strftime('%Y%m%d')}.parquet"
        if p.is_file():
            try:
                pdf = pd.read_parquet(p)
                out[d] = pdf[["offset_min", "debit"]].to_numpy(dtype=float)
            except Exception:
                pass
    return out


# --------------------------------------------------------------------------- #
# Analysis + dated markdown report.
# --------------------------------------------------------------------------- #
def _md_table(dframe: pd.DataFrame, floatfmt: str = "{:,.0f}") -> str:
    d = dframe.copy()
    cols = list(d.columns)
    head = "| " + " | ".join([str(d.index.name or "")] + [str(c) for c in cols]) + " |"
    sep = "| " + " | ".join(["---"] * (len(cols) + 1)) + " |"
    lines = [head, sep]
    for idx, row in d.iterrows():
        cells = []
        for c in cols:
            v = row[c]
            if isinstance(v, float) and np.isfinite(v):
                cells.append(floatfmt.format(v))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join([str(idx)] + cells) + " |")
    return "\n".join(lines)


def analyze(df: pd.DataFrame, n_placebo: int = 2000, verbose: bool = True) -> dict:
    """Build width x fill x split tables, per-year, per-regime, credit-vs-cost, and the matched
    placebo for any width that beats the 5-pt control at f50."""
    t = df[df["traded"]].copy()
    t["year"] = pd.to_datetime(t["day"]).dt.year
    htag = _FILL_TAG[HEADLINE_FILL]

    # width x fill overall + per half.
    scopes = {"overall": t, "train": t[t["half"] == "train"], "test": t[t["half"] == "test"]}
    band = {}
    for scope, sub in scopes.items():
        rows = []
        for w in WIDTHS:
            wt = _wtag(w)
            for frac in FILL_FRACS:
                rows.append(width_stats_fill(df, wt, _FILL_TAG[frac], sub=sub))
        band[scope] = pd.DataFrame(rows)

    # total-$ pivot: width (rows) x fill tag (cols).
    def total_pivot(sub):
        piv = {}
        for frac in FILL_FRACS:
            tag = _FILL_TAG[frac]
            piv[tag] = {_wtag(w): round(float(sub[f"pnl_{_wtag(w)}_{tag}"].sum()), 0) for w in WIDTHS}
        p = pd.DataFrame(piv).reindex(WIDTH_TAGS)
        p.index.name = "width"
        return p
    total_by_fill = {s: total_pivot(sub) for s, sub in scopes.items()}

    # per-year at headline fill.
    year_tbl = {_wtag(w): t.groupby("year")[f"pnl_{_wtag(w)}_{htag}"].sum().round(0).to_dict()
                for w in WIDTHS}
    # per-regime at headline fill.
    regime_tbl = {}
    for rk in ("gamma_regime", "vix_regime"):
        regime_tbl[rk] = {_wtag(w): t.groupby(rk)[f"pnl_{_wtag(w)}_{htag}"].sum().round(0).to_dict()
                          for w in WIDTHS}

    credit_cost = credit_cost_table(df)

    # placebo at headline fill for any width beating the 5-pt control total.
    ctrl_wt = _wtag(5.0)
    ctrl_total_h = float(t[f"pnl_{ctrl_wt}_{htag}"].sum())
    days = list(t["day"])
    placebos = {}
    for w in WIDTHS:
        wt = _wtag(w)
        if wt == ctrl_wt:
            continue
        arm_total = float(t[f"pnl_{wt}_{htag}"].sum())
        if arm_total <= ctrl_total_h:
            placebos[wt] = {"skipped": "does not beat the 5-pt control at f50"}
            continue
        hpaths = load_paths(days, wt, htag)
        credit_by_day = {}
        for row in t.itertuples():
            c = getattr(row, f"entry_credit_{wt}_{htag}")
            if np.isfinite(c):
                credit_by_day[row.day] = float(c)
        arm_hold = float(t[f"holdmin_{wt}_{htag}"].replace([np.inf, -np.inf], np.nan).dropna().mean())
        if hpaths and credit_by_day:
            placebos[wt] = cm.random_exit_placebo_from_paths(
                hpaths, credit_by_day, arm_hold, arm_total, n_draws=n_placebo)
        else:
            placebos[wt] = {"skipped": "no headline paths cached"}

    if verbose:
        print("\n===== TOTAL $ by WIDTH x FILL (overall) =====", flush=True)
        print(total_by_fill["overall"].to_string(), flush=True)
        print("\n===== CREDIT vs 4-LEG SPREAD COST =====", flush=True)
        print(credit_cost.to_string(), flush=True)
        print("\n===== HEADLINE-FILL (f50) PLACEBO =====", flush=True)
        for wt, p in placebos.items():
            print(f"  {wt}: {p}", flush=True)

    return {"band": band, "total_by_fill": total_by_fill, "year_tbl": year_tbl,
            "regime_tbl": regime_tbl, "credit_cost": credit_cost, "placebos": placebos,
            "headline_tag": htag, "ctrl_total_h": ctrl_total_h}


def write_markdown_report(df: pd.DataFrame, an: dict, out_path: Path) -> Path:
    htag = an["headline_tag"]
    tot = an["total_by_fill"]
    band = an["band"]
    placebos = an["placebos"]
    ctrl_wt = _wtag(5.0)

    traded = df[df["traded"]].copy()
    n_traded = len(traded)
    n_days = len(df)
    n_skip = int((~df["traded"]).sum())
    crash_skips = df["skip_reason"].astype(str).str.startswith("crash-skip").sum()
    dmin, dmax = df["day"].min(), df["day"].max()

    # PASS determination.
    # (1) positive plateau across >=3 ADJACENT widths at f50.
    f50_by_width = {_wtag(w): float(traded[f"pnl_{_wtag(w)}_{htag}"].sum()) for w in WIDTHS}
    pos_flags = [f50_by_width[_wtag(w)] > 0 for w in WIDTHS]
    # longest run of adjacent positive widths.
    best_run = cur = 0
    for f in pos_flags:
        cur = cur + 1 if f else 0
        best_run = max(best_run, cur)
    plateau_ok = best_run >= 3
    # (2) OOS-stable: the plateau widths positive in the TEST half too.
    test = traded[traded["half"] == "test"]
    f50_test = {_wtag(w): float(test[f"pnl_{_wtag(w)}_{htag}"].sum()) for w in WIDTHS}
    # (3) beats placebo.
    passed_placebo = [wt for wt, p in placebos.items()
                      if isinstance(p, dict) and p.get("arm_beats_placebo")]
    # widths that are positive at f50 overall AND OOS AND beat placebo.
    robust_widths = [_wtag(w) for w in WIDTHS
                     if f50_by_width[_wtag(w)] > 0 and f50_test[_wtag(w)] > 0
                     and _wtag(w) in passed_placebo]
    overall_pass = plateau_ok and bool(robust_widths)

    L = []
    L.append("# 0DTE Iron-Condor STRIKE-WIDTH sweep (ARM 1) -- finished-window report\n")
    L.append(f"_Generated {_dt.date.today().isoformat()}. Window {dmin} -> {dmax}, "
             f"{n_days} session-days ({n_traded} traded, {n_skip} no-trade/skip, "
             f"{crash_skips} crash-skipped). PAPER / research only._\n")
    L.append("Pre-registration: `docs/PREREG_condor_reopen_2026-07-06.md`, Arm 1. "
             "Entry chassis frozen from the control (14:00 entry, 0.15-delta shorts, 16:00 "
             "settlement, honest 4-leg fills). Management fixed = profit-target 25% OR 2x stop "
             "OR settle. ONLY the wing width is swept: **5 (control) / 10 / 20 / 30 / 50-pt**.\n")

    L.append("## 1. Credit collected vs 4-leg spread cost (the crux)\n")
    L.append("Does widening collect more credit while the bid/ask spread eats a SMALLER fraction "
             "of it? `spread_cost_pct_of_credit` = (mid credit - worst-side credit) / mid credit, "
             "per day, averaged.\n")
    L.append(_md_table(an["credit_cost"], floatfmt="{:,.4g}") + "\n")

    L.append("## 2. Total P&L ($) by WIDTH x FILL FRACTION -- OVERALL\n")
    L.append("`mid`=0% (optimistic), `f25`=25%, `f50`=50% (**HEADLINE**), `full`=100% worst-side "
             "(the control's honest bound). The fraction propagates through the 25% profit-target "
             "trigger.\n")
    L.append(_md_table(tot["overall"]) + "\n")
    L.append("### TRAIN (2022-01 .. 2024-06)\n")
    L.append(_md_table(tot["train"]) + "\n")
    L.append("### TEST / OOS (2024-07 .. end)\n")
    L.append(_md_table(tot["test"]) + "\n")

    L.append("## 3. Full per-width stats at the HEADLINE f50 fill\n")
    hband = band["overall"][band["overall"]["fill"] == htag].set_index("width")
    keep = [c for c in ("trades", "total_$", "win_rate", "avg_$", "worst_day_$", "p05_$",
                        "std_$", "sharpe_ann", "sortino_ann", "avg_hold_min") if c in hband.columns]
    L.append(_md_table(hband[keep], floatfmt="{:,.3f}") + "\n")

    L.append("## 4. Per-year total P&L per width (headline f50 fill)\n")
    ydf = pd.DataFrame(an["year_tbl"]).T
    ydf.index.name = "width"
    L.append(_md_table(ydf) + "\n")

    L.append("## 5. Per-regime total P&L per width (headline f50 fill)\n")
    for rk, tbl in an["regime_tbl"].items():
        rdf = pd.DataFrame(tbl).T
        rdf.index.name = "width"
        L.append(f"### by {rk}\n")
        L.append(_md_table(rdf) + "\n")

    L.append("## 6. Matched random-exit PLACEBO (headline f50 fill)\n")
    L.append("Run only for widths whose f50 total beats the 5-pt control. `arm_beats_placebo=True` "
             "means the width clears the 5% bar vs a random exit matched to its mean holding time.\n")
    prows = []
    for wt, p in placebos.items():
        if "skipped" in p:
            prows.append({"width": wt, "verdict": p["skipped"]})
        else:
            prows.append({"width": wt, "arm_total_$": p["arm_total_$"],
                          "placebo_p50_$": p["placebo_p50_$"], "placebo_p95_$": p["placebo_p95_$"],
                          "frac_placebo_ge_arm": p["frac_placebo_ge_arm"],
                          "arm_beats_placebo": p["arm_beats_placebo"]})
    if prows:
        pdf = pd.DataFrame(prows).set_index("width")
        L.append(_md_table(pdf, floatfmt="{:,.4g}") + "\n")
    else:
        L.append("_No width beat the 5-pt control at f50 -- no placebo needed._\n")

    L.append("## 7. VERDICT\n")
    L.append(f"**f50 total P&L by width:** " +
             ", ".join(f"{wt}=${f50_by_width[wt]:,.0f}" for wt in WIDTH_TAGS) + ".\n")
    L.append(f"**(a) Positive plateau across >=3 ADJACENT widths at f50?** "
             f"Longest adjacent-positive run = {best_run} width(s) -> "
             f"{'YES' if plateau_ok else 'NO'}.\n")
    L.append(f"**(b) OOS-stable (positive in the TEST half)?** f50 test-half by width: " +
             ", ".join(f"{wt}=${f50_test[wt]:,.0f}" for wt in WIDTH_TAGS) + ".\n")
    L.append(f"**(c) Beats matched placebo?** Widths clearing the 5% bar: "
             f"{passed_placebo if passed_placebo else 'NONE'}.\n")
    L.append(f"**Widths clearing ALL THREE (f50-positive AND OOS-positive AND beat placebo):** "
             f"{robust_widths if robust_widths else 'NONE'}.\n")
    L.append(f"\n> **VERDICT: {'PASS -- robust width plateau' if overall_pass else 'REFUTED'}.** "
             + ("A positive plateau of >=3 adjacent widths clears OOS and the placebo. "
                "ADOPTION still requires Andrew's explicit blessing (a plateau is a map feature, "
                "not a recommendation)."
                if overall_pass else
                "No positive plateau of >=3 adjacent widths that is also OOS-stable and beats the "
                "matched placebo. Widening the wings does not rescue the 0DTE condor: the extra "
                "credit does not outrun the 4-leg cost at a realistic (f50) fill. A single positive "
                "cell, if any, is a mirage and is NOT a pass. Consistent with the cost-bound "
                "finding in the management terrain map.") + "\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(L), encoding="utf-8")
    return out_path


def run(verbose: bool = True, save: bool = True, n_placebo: int = 2000) -> dict:
    df = run_history(verbose=verbose, save=save)
    an = analyze(df, n_placebo=n_placebo, verbose=verbose)
    if save:
        out = write_markdown_report(
            df, an, OUTPUT_DIR.parent / f"condor_width_sweep_{_dt.date.today():%Y%m%d}.md")
        if verbose:
            print(f"\nReport written: {out}", flush=True)
    return {"days": df, "analysis": an}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="0DTE iron-condor STRIKE-WIDTH sweep (ARM 1)")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="run only the first N days (smoke test)")
    ap.add_argument("--history-only", action="store_true", help="run history, skip report")
    ap.add_argument("--resume", action="store_true", help="resume from the partial CSV (default)")
    ap.add_argument("--max-new-days", type=int, default=0,
                    help="process at most N not-yet-done days this run, then exit cleanly "
                         "(fresh-process chunk loop to beat OOM). 0 = no cap.")
    ap.add_argument("--report-only", action="store_true",
                    help="load the finished days CSV and (re)build the dated markdown report")
    args = ap.parse_args()
    if args.report_only:
        _df = pd.read_csv(OUTPUT_DIR / "condor_width_sweep_days.csv")
        _df["traded"] = _df["traded"].astype(str).str.lower().isin(["true", "1"])
        _df["day"] = pd.to_datetime(_df["day"]).dt.date
        _an = analyze(_df, verbose=not args.quiet)
        _out = write_markdown_report(
            _df, _an, OUTPUT_DIR.parent / f"condor_width_sweep_{_dt.date.today():%Y%m%d}.md")
        print(f"Report written: {_out}", flush=True)
    elif args.history_only or args.limit or args.resume or args.max_new_days:
        days = s5.available_days()
        if args.limit:
            days = days[: args.limit]
        run_history(days=days, verbose=not args.quiet, save=not args.no_save,
                    max_new_days=args.max_new_days)
    else:
        run(verbose=not args.quiet, save=not args.no_save)
