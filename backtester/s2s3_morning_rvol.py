r"""
s2s3_morning_rvol.py — pre-registered test of the ONE surviving S2/S3 signal:
morning realized vol (09:30-11:00) predicts the afternoon realized range (11:00-16:00).

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the warehouse.

WHAT THIS IS
------------
The prior S2/S3 intraday-condor study (`s2s3_intraday_condor.py`, report
`output/s2s3_intraday_condor_20260701.md`) REFUTED the morning-GAP "wait & measure"
gate at the placebo. That study's measurement leg surfaced ONE relationship that was
materially stronger and stable out-of-sample:

    morning realized vol (am_rvol_pct, 09:31-11:00)  ->  post-entry PM range (pm_range_pct,
    14:00-16:00),  Spearman rho +0.586 overall (+0.577 train / +0.563 test).

This module pre-registers a SINGLE test of whether that predictiveness converts into
AVOIDED LOSSES on the SAME losing fixed-0.15-delta 0DTE iron-condor control, at honest
fills, versus a MATCHED RANDOM PLACEBO -- exactly the discipline that killed the gap gate.

THE SIGNAL (pre-declared, NOT tuned)
------------------------------------
  * Fit a simple relationship pm_range_pct ~ am_rvol_pct on the TRAIN HALF ONLY
    (ordinary least squares -- the plainest "simple relationship"; monotone in am_rvol,
    so the flag is robust to the functional form).
  * Predict PM range for EVERY day from that train-fit line.
  * "Flagged day" = predicted PM range in the TOP THIRD, where the cutoff is the 67th
    percentile (top 33%) of the TRAIN-half predictions ONLY, then applied UNSEEN to all
    days. ONE cutoff, pre-declared, no threshold search.

Everything -- control, honest fills, day sampling, train/test split (2024-06-30), the
pos/neg-gamma x VIX-tercile sub-buckets, and the 3000-draw random-placebo machinery -- is
INHERITED from `s2s3_intraday_condor` / `s6_control` / `s6_matrix`, so this is apples-to-
apples with the refuted gap gate. Nothing is re-derived.

THREE PRE-REGISTERED ARMS, all driven by the SAME flag, each conditioning the SAME control:
  * Arm A -- GATE:     sit out flagged days entirely (P&L -> 0 on flagged days).
  * Arm B -- DOWNSIZE: trade flagged days at 0.5x size (pre-declared fraction, not swept).
  * Arm C -- WIDEN:    trade flagged days at a wider condor (0.10 delta instead of 0.15;
                       pre-declared, not swept) -- requires re-simulating flagged days at
                       0.10 delta through the control's OWN engine (no re-implementation).

PASS BAR (each arm must INDEPENDENTLY clear ALL FOUR):
  1. Beats the control's net P&L.
  2. Beats its MATCHED random placebo in >= 98% of 3000 draws (raised from 95% as a
     multiple-comparisons guard for testing three arms).
  3. Holds in BOTH halves AND every pos/neg-gamma & VIX-tercile sub-bucket (no bucket
     where the delta-vs-control flips negative).
  4. The improvement comes from AVOIDED LOSSES on flagged days, not merely trading
     fewer/smaller -- decomposed and shown explicitly.

ANTI-CURVE-FIT / NO LOOK-AHEAD (rule #1, load-bearing):
  * am_rvol_pct is measured from 09:31-11:00 bars, all <= the 14:00 entry -> causal.
  * The signal relationship AND the flag cutoff are fit on the TRAIN half ONLY and applied
    unseen to the TEST half. No parameter is swept; no alternative is tried and picked.
  * The control / fills / exit scan are the control's own causal engine, unchanged.
  * Pinned by tests/test_s2s3_morning_rvol.py + the standing causality guard.
"""

from __future__ import annotations

import argparse
import datetime as _dt
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

import s5_intraday_data as s5
import s6_recon as recon
import s6_control as ctrl
import s6_matrix as mx
import s2s3_intraday_condor as s23

# --------------------------------------------------------------------------- #
# Pre-registered, FROZEN constants for this test. None of these are swept.
# --------------------------------------------------------------------------- #
TRAIN_END = s23.TRAIN_END                 # 2024-06-30, same OOS split as everything upstream
FLAG_TOP_FRACTION = 1.0 / 3.0             # "TOP THIRD" -> cutoff at the 67th pct of TRAIN preds
DOWNSIZE_FRACTION = 0.5                   # Arm B: 0.5x size on flagged days (pre-declared)
WIDEN_TARGET_DELTA = 0.10                 # Arm C: 0.10-delta condor on flagged days (pre-declared)

N_PLACEBO_DRAWS = 3000                    # matched-placebo draws (inherited count)
PLACEBO_PASS_FRAC = 0.98                  # arm must beat placebo in >= 98% of draws
PLACEBO_SEED = 7                          # same seed convention as the gap-gate placebo

# The prior study's per-day CSV (control P&L + morning observables + regimes + halves).
DAYS_CSV = s23.OUTPUT_DIR / "s2s3_intraday_condor_days.csv"

OUTPUT_DIR = s23.OUTPUT_DIR               # output/s2s3_research
# Off-Drive cache for the Arm-C 0.10-delta re-simulation (warehouse-adjacent local disk).
# ONE cache keyed by day, covering EVERY traded day: it serves BOTH the arm (flagged days)
# AND the arm's random-placebo denominator (a widened mark on random non-flagged days too),
# so no day is ever re-simulated twice.
LOCAL_CACHE_DIR = Path(r"C:\TradingDesk-Local\state\s2s3_morning_rvol")
WIDEN_CACHE_CSV = LOCAL_CACHE_DIR / "arm_c_widen_ALL.csv"

_GAMMA_BUCKETS = ("positive", "negative", "neutral")
_VIX_BUCKETS = ("contango", "backwardation")


# --------------------------------------------------------------------------- #
# Load the inherited per-day control table (or rebuild it if absent).
# --------------------------------------------------------------------------- #
def load_days(rebuild: bool = False, verbose: bool = True) -> pd.DataFrame:
    """Load the prior study's per-day table (control P&L + am_rvol + pm_range + regimes).

    We reuse the EXACT control outcomes already computed and checkpointed by
    s2s3_intraday_condor.run_history -- no re-simulation of the control, so the yardstick
    is byte-identical to the refuted-gap-gate run.
    """
    if not rebuild and DAYS_CSV.is_file():
        df = pd.read_csv(DAYS_CSV)
    else:
        if verbose:
            print("days CSV absent -> running the inherited control history", flush=True)
        df = s23.run_history(verbose=verbose, save=True)
        df = pd.read_csv(DAYS_CSV)
    df["traded"] = df["traded"].astype(str).str.lower().isin(["true", "1"])
    df["day"] = pd.to_datetime(df["day"]).dt.date
    df = df.sort_values("day").reset_index(drop=True)
    return df


# --------------------------------------------------------------------------- #
# The signal: train-fit OLS (am_rvol -> pm_range) + top-third flag (train cutoff).
# --------------------------------------------------------------------------- #
def _fit_train_line(train: pd.DataFrame) -> tuple[float, float]:
    """OLS slope/intercept of pm_range_pct ~ am_rvol_pct on the TRAIN half only.

    The plainest 'simple relationship'. Monotone increasing (slope > 0 empirically), so the
    top-third of the PREDICTION equals the top-third of am_rvol -- the flag is therefore
    robust to the exact functional form, and no form was searched.
    """
    s = train.dropna(subset=["am_rvol_pct", "pm_range_pct"])
    x = s["am_rvol_pct"].to_numpy(dtype=float)
    y = s["pm_range_pct"].to_numpy(dtype=float)
    if len(x) < 3 or np.std(x) == 0:
        return float("nan"), float("nan")
    slope, intercept = np.polyfit(x, y, 1)
    return float(slope), float(intercept)


def add_flag(df: pd.DataFrame, verbose: bool = True) -> pd.DataFrame:
    """Add pred_pm_range + flagged (top-third by TRAIN cutoff, applied unseen).

    Look-ahead guard: BOTH the OLS line AND the top-third cutoff are computed on the TRAIN
    half only (days <= TRAIN_END). The TEST half never influences either. am_rvol_pct is a
    <=14:00 observable, so the flag is knowable at entry.
    """
    df = df.copy()
    train = df[df["day"] <= TRAIN_END]
    slope, intercept = _fit_train_line(train)
    if not np.isfinite(slope):
        raise RuntimeError("could not fit the train-half am_rvol->pm_range line (no data)")

    df["pred_pm_range"] = slope * df["am_rvol_pct"] + intercept
    # Top-third cutoff from the TRAIN-half predictions ONLY (unseen application to test).
    train_pred = df.loc[df["day"] <= TRAIN_END, "pred_pm_range"].dropna()
    cutoff = float(np.quantile(train_pred, 1.0 - FLAG_TOP_FRACTION))  # 67th pct = top third
    df["flag_cutoff"] = cutoff
    # Days without a prediction (missing am_rvol) are NOT flagged (default = trade as control).
    df["flagged"] = np.where(df["pred_pm_range"].notna(),
                             df["pred_pm_range"] > cutoff, False)
    if verbose:
        n_flag = int(df["flagged"].sum())
        n_flag_tr = int(df[(df["day"] <= TRAIN_END)]["flagged"].sum())
        n_flag_te = int(df[(df["day"] > TRAIN_END)]["flagged"].sum())
        print(f"signal fit (train only): pm_range = {slope:.4f}*am_rvol + {intercept:.4f}; "
              f"top-third cutoff (train 67th pct) = {cutoff:.4f}", flush=True)
        print(f"flagged days: {n_flag} total  ({n_flag_tr} train / {n_flag_te} test)", flush=True)
    return df


# --------------------------------------------------------------------------- #
# Arm C re-simulation: the 0.10-delta condor on flagged TRADED days (control engine).
# --------------------------------------------------------------------------- #
def _run_day_widened(d: _dt.date, clf, target_delta: float,
                     day_data: s5.DayData | None = None) -> float:
    """Re-run the control's OWN iron-condor engine at a DIFFERENT target delta for one day.

    `clf` is unused (kept for signature compatibility; the P&L needs no classifier).
    Mirrors s23.run_day's control leg EXACTLY (same snapshot, same fills, same causal exit
    scan) but builds the condor at `target_delta` instead of 0.15. Returns pnl_dollars, or
    NaN if the day is not tradeable at this delta (skip). No re-implementation: it calls
    ctrl._build_iron_condor / ctrl._scan_exit unchanged.
    """
    try:
        dd = day_data if day_data is not None else s5.load_day(d)
        chain = s5.zero_dte_chain(d, day_data=dd)
        nbbo = chain.nbbo
    except Exception:
        return float("nan")
    if nbbo.empty:
        return float("nan")

    entry_minute = pd.Timestamp(_dt.datetime.combine(d, s23.ENTRY_TIME))
    settle_minute = pd.Timestamp(_dt.datetime.combine(d, s23.SETTLEMENT_TIME))
    if entry_minute not in set(nbbo["minute"].unique()):
        return float("nan")
    entry_snap = ctrl._snap_at(nbbo, entry_minute)
    sr = recon.recover_forward_spot(entry_snap, entry_minute, d)
    if sr is None:
        return float("nan")
    entry_spot = float(sr.spot)
    delta_tbl = recon.per_strike_delta(entry_snap, entry_minute, d, entry_spot)
    build = ctrl._build_iron_condor(entry_snap, delta_tbl, target_delta)
    if build is None:
        return float("nan")
    credit = build["entry_credit"]
    if not np.isfinite(credit) or credit < s23.MIN_ENTRY_CREDIT:
        return float("nan")
    _reason, _m, exit_debit = ctrl._scan_exit(
        nbbo, build["legs"], credit, entry_minute, settle_minute)
    if not np.isfinite(exit_debit):
        return float("nan")
    return (credit - exit_debit) * s23.CONTRACT_MULTIPLIER * s23.N_CONTRACTS


def _load_widen_cache() -> dict:
    """Read the off-Drive 0.10-delta cache (day -> raw widened P&L). Empty if absent."""
    if not WIDEN_CACHE_CSV.is_file():
        return {}
    c = pd.read_csv(WIDEN_CACHE_CSV)
    c["day"] = pd.to_datetime(c["day"]).dt.date
    return dict(zip(c["day"], c["widen_pnl_raw"]))


def populate_widen_cache(days: list[_dt.date], workers: int = 4,
                         verbose: bool = True) -> dict:
    """Re-simulate the 0.10-delta condor for `days` and cache off-Drive, sharded across
    processes. The warehouse day-load (~5s/day) is the bottleneck and is pure per-day
    parquet I/O, so it parallelizes cleanly; the memory note caps a single terminal at ~4
    sustained shards. Idempotent + resumable: already-cached days are skipped, so a killed
    run only loses in-flight days. Returns the merged cache.
    """
    cache = _load_widen_cache()
    to_run = [d for d in days if d not in cache]
    if not to_run:
        if verbose:
            print(f"widen cache: all {len(days)} days present off-Drive (no re-sim).", flush=True)
        return cache
    LOCAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if verbose:
        print(f"widen cache: re-simulating {len(to_run)} days at {WIDEN_TARGET_DELTA:.2f} "
              f"delta across {workers} workers...", flush=True)

    import concurrent.futures as cf
    results: dict = {}
    # Each worker re-imports the module state; _run_day_widened is a top-level fn so it pickles.
    with cf.ProcessPoolExecutor(max_workers=workers) as ex:
        fut = {ex.submit(_widen_one, d, WIDEN_TARGET_DELTA): d for d in to_run}
        done = 0
        for f in cf.as_completed(fut):
            d = fut[f]
            try:
                results[d] = f.result()
            except Exception as e:
                results[d] = float("nan")
                if verbose:
                    print(f"  {d} FAILED {type(e).__name__}", flush=True)
            done += 1
            if verbose and (done % 50 == 0 or done == len(to_run)):
                print(f"  [{done}/{len(to_run)}] widened", flush=True)
    cache.update(results)
    pd.DataFrame([{"day": d, "widen_pnl_raw": cache[d]} for d in sorted(cache)]
                 ).to_csv(WIDEN_CACHE_CSV, index=False)
    if verbose:
        print(f"widen cache: saved {len(cache)} days -> {WIDEN_CACHE_CSV}", flush=True)
    return cache


def _widen_one(d: _dt.date, target_delta: float) -> float:
    """Process-pool entry point (module-level so it pickles). One day, own classifier-free
    re-sim (the classifier is not needed for the P&L)."""
    return _run_day_widened(d, None, target_delta)


def attach_widen(df: pd.DataFrame, verbose: bool = True) -> pd.DataFrame:
    """Attach BOTH widen columns from the single off-Drive cache (all traded days):
      widen_pnl     -> control P&L everywhere, 0.10-delta on FLAGGED traded days (the arm).
      widen_pnl_all -> 0.10-delta on EVERY traded day (the arm's random-placebo denominator).
    Fallback where a wider condor is untradeable: the control P&L (the arm cannot manufacture
    an avoided loss it could not actually place). Assumes the cache is already populated.
    """
    df = df.copy()
    cache = _load_widen_cache()
    traded = df["traded"].to_numpy(dtype=bool)
    flagged = df["flagged"].to_numpy(dtype=bool)
    base = df["pnl_dollars"].to_numpy(dtype=float)
    days = df["day"].tolist()

    widen = base.copy()          # arm: flagged-only override
    widen_all = base.copy()      # placebo denominator: every traded day
    for i in range(len(df)):
        if not traded[i]:
            continue
        raw = cache.get(days[i], float("nan"))
        mark = raw if np.isfinite(raw) else base[i]
        widen_all[i] = mark
        if flagged[i]:
            widen[i] = mark
    df["widen_pnl"] = widen
    df["widen_pnl_all"] = widen_all
    if verbose:
        miss = sum(1 for i in range(len(df)) if traded[i] and days[i] not in cache)
        print(f"widen attached: {miss} traded days missing from cache "
              f"(fell back to control P&L).", flush=True)
    return df


# --------------------------------------------------------------------------- #
# Arm P&L definitions (all conditioning the SAME control, driven by the SAME flag).
# --------------------------------------------------------------------------- #
def arm_pnl(df: pd.DataFrame, arm: str) -> np.ndarray:
    """Per-day arm P&L over the TRADED days (index-aligned to df[df.traded]).

    control:  pnl_dollars everywhere.
    A GATE:   0 on flagged days, control P&L elsewhere.
    B DOWNSIZE: 0.5x control P&L on flagged days, control P&L elsewhere.
    C WIDEN:  widen_pnl (0.10-delta on flagged days), control P&L elsewhere.
    """
    t = df[df["traded"]].copy()
    base = t["pnl_dollars"].to_numpy(dtype=float)
    flag = t["flagged"].to_numpy(dtype=bool)
    if arm == "control":
        return base
    if arm == "A":
        out = base.copy(); out[flag] = 0.0; return out
    if arm == "B":
        out = base.copy(); out[flag] = base[flag] * DOWNSIZE_FRACTION; return out
    if arm == "C":
        return t["widen_pnl"].to_numpy(dtype=float)
    raise ValueError(f"unknown arm {arm!r}")


# --------------------------------------------------------------------------- #
# Matched random placebo per arm (same methodology as the gap-gate placebo).
# --------------------------------------------------------------------------- #
def matched_placebo(df: pd.DataFrame, arm: str,
                    n_draws: int = N_PLACEBO_DRAWS, seed: int = PLACEBO_SEED) -> dict:
    """Compare the arm's gain-vs-control against a MATCHED random placebo.

    The null for each arm is: apply the SAME transformation to a RANDOM set of the same
    number of days (drawn from ALL traded days, not the flagged set). If the arm does not
    beat that, its 'gain' is the apply-the-transform-to-any-days-on-a-losing-book artifact,
    not signal.

      A GATE     -> random SIT-OUT of the same day-count (P&L -> 0).
      B DOWNSIZE -> random 0.5x-DOWNSIZE of the same day-count.
      C WIDEN    -> random WIDEN of the same day-count: swap the 0.10-delta P&L in on a
                    random set of traded days. This needs a 0.10-delta mark on days OUTSIDE
                    the flagged set, supplied as `widen_pnl_all` (every traded day re-
                    simulated at 0.10 delta; see _ensure_widen_all). Without that column the
                    draw would be degenerate (only flagged days differ from control), so the
                    placebo denominator is the honest generalization of the arm.
    """
    t = df[df["traded"]].copy().reset_index(drop=True)
    base = t["pnl_dollars"].to_numpy(dtype=float)
    flag = t["flagged"].to_numpy(dtype=bool)
    n_flag = int(flag.sum())
    full_total = float(np.nansum(base))

    arm_total = float(np.nansum(arm_pnl(df, arm)))
    arm_gain = arm_total - full_total

    rng = np.random.default_rng(seed)
    rand_gains = np.empty(n_draws)
    n_all = len(base)

    if arm == "C":
        widen = t["widen_pnl"].to_numpy(dtype=float)  # differs from base only on flagged days
        # Per-day widen delta, DEFINED for every traded day via on-demand re-sim (see run()).
        wdelta_col = "widen_pnl_all"
        if wdelta_col in t.columns:
            widen_all = t[wdelta_col].to_numpy(dtype=float)
        else:
            widen_all = widen  # fallback (flagged-only); placebo then draws from flagged pool
        for k in range(n_draws):
            idx = rng.choice(n_all, size=min(n_flag, n_all), replace=False)
            trial = base.copy()
            repl = widen_all[idx]
            good = np.isfinite(repl)
            sel = idx[good]
            trial[sel] = repl[good]
            rand_gains[k] = float(np.nansum(trial)) - full_total
    else:
        for k in range(n_draws):
            idx = rng.choice(n_all, size=min(n_flag, n_all), replace=False)
            trial = base.copy()
            if arm == "A":
                trial[idx] = 0.0
            elif arm == "B":
                trial[idx] = base[idx] * DOWNSIZE_FRACTION
            rand_gains[k] = float(np.nansum(trial)) - full_total

    frac_arm_beats_random = float(np.mean(arm_gain > rand_gains))
    frac_random_beats_arm = float(np.mean(rand_gains >= arm_gain))
    return {
        "arm": arm,
        "n_flag": n_flag,
        "arm_total": round(arm_total, 2),
        "control_total": round(full_total, 2),
        "arm_gain": round(arm_gain, 2),
        "rand_gain_mean": round(float(rand_gains.mean()), 2),
        "rand_gain_sd": round(float(rand_gains.std()), 2),
        "rand_gain_p95": round(float(np.percentile(rand_gains, 95)), 2),
        "rand_gain_p98": round(float(np.percentile(rand_gains, 98)), 2),
        "frac_arm_beats_random": round(frac_arm_beats_random, 4),
        "frac_random_beats_arm": round(frac_random_beats_arm, 4),
        "beats_placebo_98": frac_arm_beats_random >= PLACEBO_PASS_FRAC,
    }


# --------------------------------------------------------------------------- #
# Both-halves + sub-bucket delta-vs-control tables (plateau check).
# --------------------------------------------------------------------------- #
def bucket_table(df: pd.DataFrame, arm: str) -> pd.DataFrame:
    """delta(arm - control) total P&L overall + both halves + every gamma x VIX bucket."""
    t = df[df["traded"]].copy().reset_index(drop=True)
    base = t["pnl_dollars"].to_numpy(dtype=float)
    arm_p = arm_pnl(df, arm)

    def block(name, mask):
        m = mask.to_numpy(dtype=bool) if isinstance(mask, pd.Series) else mask
        c = float(np.nansum(base[m]))
        a = float(np.nansum(arm_p[m]))
        return {"bucket": name, "n": int(m.sum()),
                "control_$": round(c, 2), "arm_$": round(a, 2),
                "delta_$": round(a - c, 2)}

    rows = [block("ALL", np.ones(len(t), dtype=bool))]
    for half in ("train", "test"):
        rows.append(block(f"half={half}", t["half"] == half))
    for g in _GAMMA_BUCKETS:
        for v in _VIX_BUCKETS:
            sub = (t["gamma_regime"] == g) & (t["vix_regime"] == v)
            if int(sub.sum()) == 0:
                continue
            rows.append(block(f"{g[:3]}/{v[:4]}", sub))
    return pd.DataFrame(rows)


def plateau_ok(bt: pd.DataFrame) -> tuple[bool, list[str]]:
    """Plateau holds iff delta > 0 in BOTH halves and NO sub-bucket delta is negative.

    (ALL and the half rows must be positive; buckets must not FLIP negative -- a zero-flag
    bucket delta of exactly 0 is not a flip.)"""
    losers = []
    for _, r in bt.iterrows():
        name = r["bucket"]
        if name == "ALL" or name.startswith("half="):
            if r["delta_$"] <= 0:
                losers.append(name)
        else:
            if r["delta_$"] < 0:
                losers.append(name)
    return (len(losers) == 0), losers


# --------------------------------------------------------------------------- #
# Avoided-losses decomposition (bar item #4).
# --------------------------------------------------------------------------- #
def avoided_losses_decomp(df: pd.DataFrame, arm: str) -> dict:
    """Decompose the arm's gain into (i) losses avoided on flagged days vs (ii) profits
    forgone on flagged days. The improvement must come from AVOIDED LOSSES, not merely
    trading fewer/smaller.

    On flagged traded days, per-day change = arm_pnl - control_pnl. We split those changes:
      losses_avoided  = sum of POSITIVE changes on days whose CONTROL P&L was NEGATIVE
                        (i.e. we cut a real loss).
      profits_forgone = sum of NEGATIVE changes on days whose CONTROL P&L was POSITIVE
                        (i.e. we gave up a real winner).
    net_gain = total change on flagged days (should equal arm_gain). If |losses_avoided| does
    not dominate |profits_forgone|, the arm is just trimming exposure, not avoiding losses.
    """
    t = df[df["traded"]].copy().reset_index(drop=True)
    base = t["pnl_dollars"].to_numpy(dtype=float)
    arm_p = arm_pnl(df, arm)
    flag = t["flagged"].to_numpy(dtype=bool)

    change = arm_p - base
    fl_change = change[flag]
    fl_base = base[flag]

    net_gain = float(np.nansum(change))                    # == arm_gain by construction
    # Change on flagged LOSING control days (control pnl < 0): positive change = loss cut.
    losing = fl_base < 0
    winning = fl_base > 0
    change_on_losers = float(np.nansum(fl_change[losing]))     # + = losses avoided
    change_on_winners = float(np.nansum(fl_change[winning]))   # - = profits forgone
    control_loss_on_flagged = float(np.nansum(fl_base[fl_base < 0]))
    control_win_on_flagged = float(np.nansum(fl_base[fl_base > 0]))

    # Fraction of the net gain explained by cutting losing days (vs cutting winners).
    denom = abs(change_on_losers) + abs(change_on_winners)
    frac_from_avoided_losses = (change_on_losers / denom) if denom > 0 else float("nan")
    return {
        "arm": arm,
        "n_flagged_traded": int(flag.sum()),
        "n_flagged_losers": int(losing.sum()),
        "n_flagged_winners": int(winning.sum()),
        "control_loss_$_on_flagged": round(control_loss_on_flagged, 2),
        "control_win_$_on_flagged": round(control_win_on_flagged, 2),
        "change_on_losing_days_$": round(change_on_losers, 2),     # + = avoided loss
        "change_on_winning_days_$": round(change_on_winners, 2),   # - = forgone profit
        "net_gain_$": round(net_gain, 2),
        "frac_of_gross_change_from_avoided_losses": (
            round(frac_from_avoided_losses, 4) if np.isfinite(frac_from_avoided_losses)
            else float("nan")),
        "avoided_losses_dominate": (
            change_on_losers > abs(change_on_winners) and change_on_losers > 0),
    }


# --------------------------------------------------------------------------- #
# Per-arm verdict against the four-item bar.
# --------------------------------------------------------------------------- #
def evaluate_arm(df: pd.DataFrame, arm: str) -> dict:
    plac = matched_placebo(df, arm)
    bt = bucket_table(df, arm)
    plat_ok, plat_losers = plateau_ok(bt)
    decomp = avoided_losses_decomp(df, arm)

    bar1 = plac["arm_gain"] > 0                          # beats control net P&L
    bar2 = plac["beats_placebo_98"]                      # >= 98% vs matched random
    bar3 = plat_ok                                       # both halves + all sub-buckets
    bar4 = decomp["avoided_losses_dominate"]             # from avoided losses, not trimming
    passed = bar1 and bar2 and bar3 and bar4
    return {
        "arm": arm, "placebo": plac, "bucket_table": bt, "plateau_losers": plat_losers,
        "decomp": decomp,
        "bar1_beats_control": bar1, "bar2_beats_placebo98": bar2,
        "bar3_plateau": bar3, "bar4_avoided_losses": bar4,
        "PASS": passed,
    }


# --------------------------------------------------------------------------- #
# Top-level pipeline.
# --------------------------------------------------------------------------- #
def run(verbose: bool = True, save: bool = True, rebuild_days: bool = False,
        workers: int = 4) -> dict:
    df = load_days(rebuild=rebuild_days, verbose=verbose)
    df = add_flag(df, verbose=verbose)

    # ONE off-Drive re-simulation pass over EVERY traded day at 0.10 delta (sharded). It
    # serves BOTH the arm (flagged days) AND the arm's random-placebo denominator (a widened
    # mark on random non-flagged days too) -- no day is re-simulated twice.
    populate_widen_cache(list(df[df["traded"]]["day"]), workers=workers, verbose=verbose)
    df = attach_widen(df, verbose=verbose)

    results = {a: evaluate_arm(df, a) for a in ("A", "B", "C")}

    if save:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(OUTPUT_DIR / "s2s3_morning_rvol_days.csv", index=False)
    if verbose:
        _print_report(df, results)
    return {"days": df, "results": results}


def _print_report(df: pd.DataFrame, results: dict) -> None:
    t = df[df["traded"]]
    print("\n===== S2/S3 MORNING-RVOL SIGNAL — THREE PRE-REGISTERED ARMS =====", flush=True)
    print(f"traded days = {len(t)}; flagged (top-third pred PM range) = "
          f"{int(t['flagged'].sum())}; control total P&L = "
          f"${round(float(t['pnl_dollars'].sum()), 2)}", flush=True)
    for a in ("A", "B", "C"):
        r = results[a]
        p = r["placebo"]; dcp = r["decomp"]
        name = {"A": "GATE (sit out)", "B": "DOWNSIZE 0.5x", "C": "WIDEN 0.10d"}[a]
        print(f"\n--- ARM {a}: {name} ---", flush=True)
        print(f"  bar1 beats control  : {r['bar1_beats_control']}  "
              f"(arm ${p['arm_total']} vs control ${p['control_total']}; gain ${p['arm_gain']})",
              flush=True)
        print(f"  bar2 beats placebo98: {r['bar2_beats_placebo98']}  "
              f"(arm beats random in {p['frac_arm_beats_random']*100:.1f}% of {N_PLACEBO_DRAWS} "
              f"draws; need >=98%; random mean gain ${p['rand_gain_mean']})", flush=True)
        print(f"  bar3 plateau        : {r['bar3_plateau']}  "
              f"(losers: {r['plateau_losers'] or 'none'})", flush=True)
        print(f"  bar4 avoided losses : {r['bar4_avoided_losses']}  "
              f"(loss-days change ${dcp['change_on_losing_days_$']} vs win-days change "
              f"${dcp['change_on_winning_days_$']})", flush=True)
        print(f"  ==> {'PASS' if r['PASS'] else 'FAIL'}", flush=True)
        with pd.option_context("display.width", 200, "display.max_columns", 30):
            print(r["bucket_table"].to_string(index=False), flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="S2/S3 morning-rvol signal — 3 pre-registered arms")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--rebuild-days", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    run(verbose=not args.quiet, save=not args.no_save, rebuild_days=args.rebuild_days,
        workers=args.workers)
