r"""
condor_neggamma_hedge.py — ARM 2 of the reopened 0DTE condor line:
a NEGATIVE-GAMMA HEDGE OVERLAY on the managed (profit-target-25%) iron condor.

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the warehouse.

THE THESIS (Andrew's, pre-registered in docs\PREREG_condor_reopen_2026-07-06.md, Arm 2)
--------------------------------------------------------------------------------------
Negative-gamma days are the structurally WORST regime bucket in every prior 0DTE run
(output\condor_management_20260703.md: neg-gamma is where the condor bleeds). The claim is
that on those days ONLY -- flagged CAUSALLY by the prior-EOD dealer-gamma sign, knowable
before the 14:00 entry -- adding a cheap DEFINED LONG TAIL at ~0.05-delta CAPS the big
losing days without bleeding the calm ones enough to turn the book into a wash.

WHAT THIS MODULE DOES (adds ONLY the hedge; base chassis is the control's, verbatim)
------------------------------------------------------------------------------------
Base = the managed iron condor, profit-target 25% arm (B_pt25), NO hedge:
  * entry 14:00, 0.15-delta shorts, 5-pt wings; management = take profit at 25% of credit
    OR 2x-credit stop OR settle. This is the control/management engine reused byte-for-byte
    (condor_management_experiment._scan_managed_exits with only the B_pt25 arm read).

Hedge (fires ONLY on prior-EOD negative-gamma days):
  * At 14:00, from the SAME entry snapshot used to build the condor, pick the ~0.05-delta
    long PUT and the ~0.05-delta long CALL (further OTM than the sold 0.15-delta shorts),
    using the control's own recovered per-strike delta table (no new greeks, no look-ahead).
  * HEADLINE hedge = the two-sided "tail package": buy BOTH the 0.05-delta put and call
    (one long option per wing). A condor's tail risk is two-sided; a one-sided tail only
    caps one direction, and WHICH direction blows up is not knowable causally at 14:00. We
    ALSO compute put-only and call-only variants in the same run for completeness, so no
    side is cherry-picked after seeing the result -- the VERDICT judges the pre-registered
    two-sided package.
  * Honest fills: BUY each hedge leg at the 14:00 ASK (cost booked into P&L immediately).
    The hedge is closed at the SAME minute the base condor's pt25 arm exits, SELLING each
    hedge leg at that minute's BID (honest). If the base holds to settlement, the hedge is
    marked at its settlement bid too (0DTE -> ~intrinsic). Cost is fully charged: buying the
    tail can ONLY reduce entry-day P&L; it pays off only if the tail gains more than its ask.

  hedged_pnl(day) = base_pt25_pnl(day) + hedge_pnl(day),  where
  hedge_pnl(day)  = sum_over_hedge_legs( exit_bid - entry_ask ) * mult * n_contracts.

FILL BAND (same pre-registered execution axis as the base experiment)
---------------------------------------------------------------------
Everything is reported across mid / f25 / f50 (HEADLINE) / full-cross, EXACTLY as the base
experiment. The base condor's P&L at fill f is the control's blended-fill result; the hedge
leg is blended the SAME way (buy: (1-f)*mid + f*ask; sell: (1-f)*mid + f*bid). f=1 is the
honest worst-side bound; f=0 is pure mid. The fraction is a real engine parameter, not a
post-hoc multiplier -- it changes the base condor's exit minute AND the hedge's entry cost.

NO LOOK-AHEAD (load-bearing)
----------------------------
  * The hedge fires on the PRIOR-EOD gamma sign (DayClassifier, strictly-causal prior row).
  * The hedge strikes are chosen from the 14:00 snapshot ONLY (same snapshot as the condor).
  * The hedge exit minute is the base condor's own causal exit minute; a later minute can
    never rewrite it (inherited from _scan_managed_exits, which freezes at the firing minute).
Pinned by tests/test_condor_neggamma_hedge.py: (a) hedge fires ONLY on negative-gamma days,
(b) the hedge is CHARGED (buying the tail reduces entry-day P&L), (c) no look-ahead.

MATCHED PLACEBO (decisive, rule #1)
-----------------------------------
The neg-gamma hedge must beat hedging the SAME NUMBER of RANDOM days. We draw many random
day-sets of size = (# neg-gamma hedge days), apply the identical hedge on those days, and
report the fraction of random draws whose hedged book does as well or better than the
neg-gamma-targeted book. If random-day hedging matches it, the gamma targeting added nothing.

CRASH-RESILIENT + RESUMABLE: per-day incremental CSV append + resume-skip, heartbeat prints
flushed each block, chunked --max-new-days loop, gc each day. ASCII-only console output.
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
# Inherited chassis constants (NOT re-tuned -- the control's / management's own).
# --------------------------------------------------------------------------- #
ENTRY_TIME = ctrl.ENTRY_TIME
SETTLEMENT_TIME = ctrl.SETTLEMENT_TIME
TARGET_SHORT_DELTA = ctrl.TARGET_SHORT_DELTA        # 0.15 sold shorts
MIN_ENTRY_CREDIT = ctrl.MIN_ENTRY_CREDIT
CONTRACT_MULTIPLIER = ctrl.CONTRACT_MULTIPLIER
N_CONTRACTS = ctrl.N_CONTRACTS

# --------------------------------------------------------------------------- #
# The HEDGE dial -- pre-registered plain choices (rule #1: NOT swept to a winner).
# --------------------------------------------------------------------------- #
HEDGE_TARGET_DELTA = 0.05        # further-OTM long tail delta (the pre-registered ~0.05)
HEDGE_TRIGGER_REGIME = "negative"  # fire ONLY on prior-EOD negative-gamma days

# The base management arm the hedge overlays (profit-target 25%, per the pre-reg baseline).
BASE_ARM = "B_pt25"

# Fill band -- inherited verbatim from the base experiment so the yardstick matches.
FILL_FRACS = cm.FILL_FRACS                 # (0.0, 0.25, 0.50, 1.0)
HEADLINE_FILL = cm.HEADLINE_FILL           # 0.50
_FILL_TAG = cm._FILL_TAG

# Hedge variants computed every run (headline = 'both'); the VERDICT judges 'both'.
HEDGE_VARIANTS = ("both", "put", "call")
HEADLINE_VARIANT = "both"

TRAIN_END = mx.TRAIN_END                    # 2024-06-30 OOS split, same as everything upstream

OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "condor_neggamma_hedge"
_PARTIAL_CSV = OUTPUT_DIR / "condor_neggamma_hedge_partial.csv"


# --------------------------------------------------------------------------- #
# Per-day record.
# --------------------------------------------------------------------------- #
@dataclass
class HedgeDayRecord:
    day: _dt.date
    traded: bool = False
    skip_reason: str = ""
    gamma_regime: str = "unknown"
    vix_regime: str = "unknown"
    half: str = ""
    hedge_fired: bool = False               # True only on negative-gamma days that traded
    entry_spot: float = float("nan")
    short_put_k: float = float("nan")
    short_call_k: float = float("nan")
    hedge_put_k: float = float("nan")
    hedge_call_k: float = float("nan")
    # Per fill tag: base pt25 pnl, hedge cost/pnl per variant, hedged pnl per variant.
    # Flattened on write to base_pnl_{tag}, hedgecost_{var}_{tag}, hedgepnl_{var}_{tag},
    # hedged_pnl_{var}_{tag}.
    fills: dict = None

    def flat(self) -> dict:
        base = {k: v for k, v in asdict(self).items() if k != "fills"}
        fb = self.fills or {}
        for frac in FILL_FRACS:
            tag = _FILL_TAG[frac]
            blk = fb.get(tag, {})
            base[f"base_pnl_{tag}"] = blk.get("base_pnl", float("nan"))
            for var in HEDGE_VARIANTS:
                base[f"hedgecost_{var}_{tag}"] = blk.get(f"hedgecost_{var}", float("nan"))
                base[f"hedgepnl_{var}_{tag}"] = blk.get(f"hedgepnl_{var}", float("nan"))
                base[f"hedged_pnl_{var}_{tag}"] = blk.get(f"hedged_pnl_{var}", float("nan"))
        return base


def _flat_fieldnames() -> list[str]:
    base = [k for k in asdict(HedgeDayRecord(day=_dt.date(2022, 1, 3))).keys() if k != "fills"]
    for frac in FILL_FRACS:
        tag = _FILL_TAG[frac]
        base.append(f"base_pnl_{tag}")
        for var in HEDGE_VARIANTS:
            base += [f"hedgecost_{var}_{tag}", f"hedgepnl_{var}_{tag}", f"hedged_pnl_{var}_{tag}"]
    return base


def write_header_ok(fieldnames: list[str]) -> bool:
    try:
        with open(_PARTIAL_CSV, newline="") as fh:
            existing = next(csv.reader(fh))
        return existing == fieldnames
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Hedge-leg fill helpers. Honest: BUY a long leg at the ASK, SELL it at the BID.
# Blended at fill fraction f between mid (f=0) and worst-side (f=1), mirroring the
# base experiment's _blended_* helpers so the hedge and the condor share ONE fill axis.
# --------------------------------------------------------------------------- #
def _hedge_buy_price(snap: pd.DataFrame, strike: float, right: str, f: float) -> float | None:
    """Cost to BUY one long hedge leg at fill fraction f. worst (f=1) = pay the ASK;
    mid (f=0) = pay the mid. Returns None if the leg is unquoted (never invent a fill)."""
    q = ctrl._leg_quote(snap, strike, right)
    if q is None:
        return None
    bid, ask = q
    mid = 0.5 * (bid + ask)
    return (1.0 - f) * mid + f * ask


def _hedge_sell_price(snap: pd.DataFrame, strike: float, right: str, f: float) -> float | None:
    """Proceeds to SELL one long hedge leg at fill fraction f. worst (f=1) = receive the BID;
    mid (f=0) = receive the mid. Returns None if the leg is unquoted."""
    q = ctrl._leg_quote(snap, strike, right)
    if q is None:
        return None
    bid, ask = q
    mid = 0.5 * (bid + ask)
    return (1.0 - f) * mid + f * bid


def _pick_hedge_strike(delta_tbl: pd.DataFrame, right: str, short_k: float,
                       target_abs_delta: float) -> float | None:
    """Pick the strike whose |delta| is nearest target_abs_delta, FURTHER OTM than the sold
    short (a long wing beyond the condor). For a PUT that means a LOWER strike than the short
    put; for a CALL a HIGHER strike than the short call. Uses ONLY the entry snapshot's
    recovered delta table -> causal, no look-ahead."""
    side = delta_tbl[(delta_tbl["right"] == right) & delta_tbl["delta"].notna()].copy()
    if side.empty:
        return None
    if right == "PUT":
        side = side[side["strike"] < short_k]        # further OTM = lower strike
    else:
        side = side[side["strike"] > short_k]        # further OTM = higher strike
    if side.empty:
        return None
    side["d_err"] = (side["delta"].abs() - target_abs_delta).abs()
    return float(side.sort_values("d_err").iloc[0]["strike"])


# --------------------------------------------------------------------------- #
# One day.
# --------------------------------------------------------------------------- #
def run_day(d: _dt.date, clf: mx.DayClassifier,
            day_data: s5.DayData | None = None) -> HedgeDayRecord:
    rec = HedgeDayRecord(day=d, fills={})
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
    build = ctrl._build_iron_condor(entry_snap, delta_tbl, TARGET_SHORT_DELTA)
    if build is None:
        rec.skip_reason = "could not build iron condor at entry"
        return rec
    rec.short_put_k = build["short_strike"]
    rec.short_call_k = build["short_strike_2"]
    if not np.isfinite(build["entry_credit"]) or build["entry_credit"] < MIN_ENTRY_CREDIT:
        rec.skip_reason = f"entry credit {build['entry_credit']:.2f} < {MIN_ENTRY_CREDIT}"
        return rec

    # Does the ACTUAL strategy hedge today? ONLY on negative-gamma days (causal prior-EOD).
    rec.hedge_fired = (rec.gamma_regime == HEDGE_TRIGGER_REGIME)

    # We compute the hedge's would-be P&L on EVERY traded day (causally, from the 14:00 snap)
    # so the matched random-day placebo is EXACT: it can re-hedge a random set of days of the
    # same size from the full traded pool without re-reading chains. Only neg-gamma days go
    # into the ACTUAL book (hedge_fired); the stored hedge delta on other days is a pure
    # counterfactual used only by the placebo. This adds no look-ahead (same 14:00 snapshot).
    hedge_put_k = _pick_hedge_strike(delta_tbl, "PUT", build["short_strike"],
                                     HEDGE_TARGET_DELTA)
    hedge_call_k = _pick_hedge_strike(delta_tbl, "CALL", build["short_strike_2"],
                                      HEDGE_TARGET_DELTA)
    rec.hedge_put_k = hedge_put_k if hedge_put_k is not None else float("nan")
    rec.hedge_call_k = hedge_call_k if hedge_call_k is not None else float("nan")

    # ---- Base condor pt25 P&L + exit minute at EACH fill fraction (control engine) ----
    # We run the full managed minute-walk at each fill and read ONLY the B_pt25 arm's exit,
    # so the base is the management experiment's B_pt25 result byte-for-byte.
    any_fill_ok = False
    for frac in FILL_FRACS:
        tag = _FILL_TAG[frac]
        credit_f = cm._blended_credit_to_open(entry_snap, build["legs"], frac)
        if credit_f is None or not np.isfinite(credit_f):
            rec.fills[tag] = {"base_pnl": float("nan")}
            continue
        exits_f, _ = cm._scan_managed_exits_at_fill(
            nbbo, build["legs"], credit_f, frac, entry_minute, settle_minute)
        ex = exits_f[BASE_ARM]
        if not np.isfinite(ex["exit_debit"]):
            rec.fills[tag] = {"base_pnl": float("nan")}
            continue
        base_pnl = (credit_f - ex["exit_debit"]) * CONTRACT_MULTIPLIER * N_CONTRACTS
        blk = {"base_pnl": float(base_pnl)}

        # ---- Hedge would-be P&L at this fill fraction (computed on EVERY day for the
        # exact placebo; the actual book uses it only where hedge_fired). The hedge is
        # bought at 14:00 and sold at the base condor's OWN pt25 exit minute -> causal. ----
        exit_minute = ex["exit_minute"]
        exit_snap = ctrl._snap_at(nbbo, exit_minute) if exit_minute is not None else None
        for var in HEDGE_VARIANTS:
            legs = []
            if var in ("both", "put") and hedge_put_k is not None:
                legs.append((hedge_put_k, "PUT"))
            if var in ("both", "call") and hedge_call_k is not None:
                legs.append((hedge_call_k, "CALL"))
            if not legs or exit_snap is None:
                # Could not place/mark the hedge this fill -> record NaN (never invent a fill).
                blk[f"hedgecost_{var}"] = float("nan")
                blk[f"hedgepnl_{var}"] = float("nan")
                blk[f"hedged_pnl_{var}"] = float("nan")
                continue
            buy_total = 0.0
            sell_total = 0.0
            ok = True
            for k, right in legs:
                bp = _hedge_buy_price(entry_snap, k, right, frac)
                sp = _hedge_sell_price(exit_snap, k, right, frac)
                if bp is None or sp is None:
                    ok = False
                    break
                buy_total += bp
                sell_total += sp
            if not ok:
                blk[f"hedgecost_{var}"] = float("nan")
                blk[f"hedgepnl_{var}"] = float("nan")
                blk[f"hedged_pnl_{var}"] = float("nan")
                continue
            hedge_cost = buy_total * CONTRACT_MULTIPLIER * N_CONTRACTS
            hedge_pnl = (sell_total - buy_total) * CONTRACT_MULTIPLIER * N_CONTRACTS
            blk[f"hedgecost_{var}"] = float(hedge_cost)
            blk[f"hedgepnl_{var}"] = float(hedge_pnl)
            blk[f"hedged_pnl_{var}"] = float(base_pnl + hedge_pnl)
        rec.fills[tag] = blk
        any_fill_ok = True

    if not any_fill_ok:
        rec.skip_reason = "no fill fraction markable"
        return rec
    rec.traded = True
    return rec


# --------------------------------------------------------------------------- #
# Full-history run -- crash-resilient + resumable + heartbeat + chunk cap.
# --------------------------------------------------------------------------- #
def run_history(days: list[_dt.date] | None = None, verbose: bool = True,
                save: bool = True, resume: bool = True, max_new_days: int = 0) -> pd.DataFrame:
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
        for i, d in enumerate(days, 1):
            if str(d) in done_days:
                continue
            if max_new_days and n_new >= max_new_days:
                hit_chunk_cap = True
                if verbose:
                    print(f"chunk cap reached: {n_new} new day(s) this run; exiting cleanly.",
                          flush=True)
                break
            dd = None
            try:
                dd = s5.load_day(d)
                rec = run_day(d, clf, day_data=dd)
            except Exception as e:
                n_crash_skips += 1
                rec = HedgeDayRecord(day=d, fills={})
                rec.half = "train" if d <= TRAIN_END else "test"
                rec.skip_reason = f"crash-skip: {type(e).__name__}: {str(e)[:80]}"
                if verbose:
                    print(f"[{i}/{n}] {d} CRASH-SKIP {rec.skip_reason}", flush=True)
            writer.writerow(rec.flat())
            fh.flush()
            n_new += 1
            if verbose and (i % 25 == 0 or i == n):
                hp = rec.fills.get("f50", {}).get("hedged_pnl_both") if rec.fills else None
                print(f"[{i}/{n}] {d} done (fired={rec.hedge_fired}, hedged_f50={hp}) "
                      f"[crash-skips: {n_crash_skips}]", flush=True)
            del dd, rec
            gc.collect()
        if verbose:
            done_msg = "chunk done" if hit_chunk_cap else "run_history complete"
            print(f"{done_msg}: {n_new} new day(s), {n_crash_skips} crash-skipped.", flush=True)

    df = pd.read_csv(_PARTIAL_CSV)
    df["traded"] = df["traded"].astype(str).str.lower().isin(["true", "1"])
    df["hedge_fired"] = df["hedge_fired"].astype(str).str.lower().isin(["true", "1"])
    df["day"] = pd.to_datetime(df["day"]).dt.date
    df = df.sort_values("day").reset_index(drop=True)
    if save:
        df.to_csv(OUTPUT_DIR / "condor_neggamma_hedge_days.csv", index=False)
        if verbose:
            print(f"Saved {OUTPUT_DIR / 'condor_neggamma_hedge_days.csv'}", flush=True)
    return df


# --------------------------------------------------------------------------- #
# Stats.
# --------------------------------------------------------------------------- #
def _bucket_stats(x: np.ndarray) -> dict:
    x = x[np.isfinite(x)]
    n = len(x)
    if n == 0:
        return {"n": 0, "total_$": 0.0}
    wins = x[x > 0]
    return {
        "n": n,
        "total_$": round(float(x.sum()), 2),
        "avg_$": round(float(x.mean()), 2),
        "win_rate": round(len(wins) / n, 4),
        "worst_day_$": round(float(x.min()), 2),
        "p05_$": round(float(np.percentile(x, 5)), 2),
        "std_$": round(float(x.std(ddof=1)), 2) if n > 1 else float("nan"),
    }


def _hedge_delta_col(df: pd.DataFrame, var: str, tag: str) -> pd.Series:
    """The hedge's OWN P&L per day ($) at (var, tag) = hedged_pnl - base_pnl. Computed on
    every traded day (counterfactual on non-fired days), used by the placebo."""
    return df[f"hedged_pnl_{var}_{tag}"] - df[f"base_pnl_{tag}"]


def book_pnl_col(df: pd.DataFrame, var: str, tag: str, hedge_mask: pd.Series) -> pd.Series:
    """The book's per-day P&L: base condor pt25 P&L everywhere, PLUS the hedge delta only on
    days in `hedge_mask`. For the real strategy hedge_mask = hedge_fired (neg-gamma days)."""
    base = df[f"base_pnl_{tag}"].astype(float)
    delta = _hedge_delta_col(df, var, tag).astype(float).fillna(0.0)
    return base + np.where(hedge_mask.to_numpy(), delta.to_numpy(), 0.0)


# --------------------------------------------------------------------------- #
# Matched random-day placebo (decisive, pre-registered).
# The real strategy hedges the K = (# neg-gamma traded days) days. The placebo hedges a
# RANDOM set of K traded days instead, many draws, using each day's OWN computed hedge delta.
# We compare the TOTAL BOOK (base everywhere + hedge on the chosen K days). If a random K-day
# hedge does as well or better as often as not, the negative-gamma TARGETING added nothing.
# --------------------------------------------------------------------------- #
def random_day_placebo(df: pd.DataFrame, var: str, tag: str,
                       n_draws: int = 2000, seed: int = 7) -> dict:
    t = df[df["traded"]].copy()
    dcol = _hedge_delta_col(t, var, tag).astype(float)
    # Only days with a computable hedge delta are eligible to be hedged (real or placebo).
    ok = np.isfinite(dcol)
    t = t[ok].copy()
    dvals = _hedge_delta_col(t, var, tag).to_numpy(dtype=float)
    base_total = float(t[f"base_pnl_{tag}"].sum())

    fired = t["hedge_fired"].to_numpy()
    k = int(fired.sum())
    if k == 0:
        return {"skipped": "no fired days with computable hedge"}
    real_book = base_total + float(dvals[fired].sum())

    n = len(t)
    idx = np.arange(n)
    rng = np.random.default_rng(seed)
    placebo_totals = np.empty(n_draws)
    for i in range(n_draws):
        pick = rng.choice(idx, size=k, replace=False)
        placebo_totals[i] = base_total + float(dvals[pick].sum())
    frac_ge = float(np.mean(placebo_totals >= real_book))
    return {
        "k_hedge_days": k,
        "pool_days": n,
        "base_only_$": round(base_total, 2),
        "real_neggamma_book_$": round(real_book, 2),
        "placebo_p50_$": round(float(np.percentile(placebo_totals, 50)), 2),
        "placebo_p05_$": round(float(np.percentile(placebo_totals, 5)), 2),
        "placebo_p95_$": round(float(np.percentile(placebo_totals, 95)), 2),
        "frac_placebo_ge_real": round(frac_ge, 4),
        "neggamma_beats_placebo": frac_ge < 0.05,
    }


# --------------------------------------------------------------------------- #
# Full analysis: base vs base+hedge, TOTAL and neg-gamma bucket, fill band, OOS, per-year.
# --------------------------------------------------------------------------- #
def analyze(df: pd.DataFrame, var: str = HEADLINE_VARIANT, n_placebo: int = 2000,
            verbose: bool = True) -> dict:
    t = df[df["traded"]].copy()
    t["year"] = pd.to_datetime(t["day"]).dt.year
    fired = t["hedge_fired"]

    out = {"var": var, "n_traded": int(len(t)), "n_fired": int(fired.sum())}

    # --- neg-gamma bucket: base vs hedged, per fill (the whole point) ---
    neg = t[fired].copy()
    bucket = {}
    for frac in FILL_FRACS:
        tag = _FILL_TAG[frac]
        base_x = neg[f"base_pnl_{tag}"].to_numpy(dtype=float)
        hedged_x = neg[f"hedged_pnl_{var}_{tag}"].to_numpy(dtype=float)
        bucket[tag] = {"base": _bucket_stats(base_x), "hedged": _bucket_stats(hedged_x)}
    out["neg_bucket"] = bucket

    # --- overall book: base-only vs neg-gamma-hedged, per fill ---
    overall = {}
    for frac in FILL_FRACS:
        tag = _FILL_TAG[frac]
        base_book = t[f"base_pnl_{tag}"].to_numpy(dtype=float)
        hedged_book = book_pnl_col(t, var, tag, fired).to_numpy(dtype=float)
        overall[tag] = {"base": _bucket_stats(base_book), "hedged": _bucket_stats(hedged_book)}
    out["overall"] = overall

    # --- OOS split (headline fill) on the neg-gamma bucket AND the overall book ---
    htag = _FILL_TAG[HEADLINE_FILL]
    oos = {}
    for half in ("train", "test"):
        sub = t[t["half"] == half]
        subneg = sub[sub["hedge_fired"]]
        oos[half] = {
            "neg_base": _bucket_stats(subneg[f"base_pnl_{htag}"].to_numpy(dtype=float)),
            "neg_hedged": _bucket_stats(subneg[f"hedged_pnl_{var}_{htag}"].to_numpy(dtype=float)),
            "book_base": _bucket_stats(sub[f"base_pnl_{htag}"].to_numpy(dtype=float)),
            "book_hedged": _bucket_stats(
                book_pnl_col(sub, var, htag, sub["hedge_fired"]).to_numpy(dtype=float)),
        }
    out["oos"] = oos

    # --- per-year (headline fill): neg-gamma bucket base vs hedged + overall book ---
    year_rows = {}
    for yr, sub in t.groupby("year"):
        subneg = sub[sub["hedge_fired"]]
        year_rows[int(yr)] = {
            "neg_base_$": round(float(subneg[f"base_pnl_{htag}"].sum()), 0),
            "neg_hedged_$": round(float(subneg[f"hedged_pnl_{var}_{htag}"].sum()), 0),
            "neg_n": int(np.isfinite(subneg[f"hedged_pnl_{var}_{htag}"]).sum()),
            "book_base_$": round(float(sub[f"base_pnl_{htag}"].sum()), 0),
            "book_hedged_$": round(float(
                book_pnl_col(sub, var, htag, sub["hedge_fired"]).sum()), 0),
        }
    out["year_tbl"] = year_rows

    # --- matched random-day placebo (headline fill) ---
    out["placebo"] = random_day_placebo(df, var, htag, n_draws=n_placebo)
    out["headline_tag"] = htag

    if verbose:
        print(f"\n===== NEG-GAMMA HEDGE ({var}) — neg-gamma bucket, base vs hedged =====",
              flush=True)
        for tag in [_FILL_TAG[f] for f in FILL_FRACS]:
            b, h = bucket[tag]["base"], bucket[tag]["hedged"]
            print(f"  [{tag}] base total ${b.get('total_$')} worst ${b.get('worst_day_$')} "
                  f"p05 ${b.get('p05_$')} | hedged total ${h.get('total_$')} "
                  f"worst ${h.get('worst_day_$')} p05 ${h.get('p05_$')}", flush=True)
        print(f"\n  overall book (f50): base ${overall[htag]['base'].get('total_$')} "
              f"vs hedged ${overall[htag]['hedged'].get('total_$')}", flush=True)
        print(f"  placebo: {out['placebo']}", flush=True)
    return out


# --------------------------------------------------------------------------- #
# Markdown report (the dated deliverable).
# --------------------------------------------------------------------------- #
def _md_row(cells) -> str:
    return "| " + " | ".join(str(c) for c in cells) + " |"


def write_markdown_report(df: pd.DataFrame, res: dict, out_path: Path) -> Path:
    var = res["var"]
    htag = res["headline_tag"]
    L = []
    dmin, dmax = df["day"].min(), df["day"].max()
    n_days = len(df)
    n_traded = int(df["traded"].sum())
    n_fired = res["n_fired"]

    L.append("# 0DTE Iron-Condor NEGATIVE-GAMMA HEDGE overlay (Arm 2) — report\n")
    L.append(f"_Generated {_dt.date.today().isoformat()}. Window {dmin} -> {dmax}, "
             f"{n_days} session-days ({n_traded} traded, {n_fired} negative-gamma hedge days). "
             f"PAPER / research only. Base = managed condor profit-target-25% (B_pt25), NO "
             f"hedge. Headline hedge = two-sided 0.05-delta long tail (put+call), fired ONLY "
             f"on prior-EOD negative-gamma days (causal)._\n")

    # --- VERDICT up top (lead with the answer) ---
    b = res["neg_bucket"][htag]["base"]
    h = res["neg_bucket"][htag]["hedged"]
    ov = res["overall"][htag]
    pl = res["placebo"]
    neg_improves = h.get("total_$", 0) > b.get("total_$", 0)
    tail_capped = (h.get("worst_day_$", -1e9) > b.get("worst_day_$", -1e9)
                   and h.get("p05_$", -1e9) > b.get("p05_$", -1e9))
    not_worse_book = ov["hedged"].get("total_$", 0) >= ov["base"].get("total_$", 0)
    beats_placebo = bool(pl.get("neggamma_beats_placebo", False))
    passes = neg_improves and tail_capped and not_worse_book and beats_placebo

    L.append("## VERDICT\n")
    L.append(f"**{'PASS' if passes else 'FAIL'}** at the headline 50% fill. The neg-gamma "
             f"hedge {'PASSES' if passes else 'does NOT pass'} the pre-registered bar "
             f"(improve the neg-gamma bucket net of cost AND cap its tail AND not turn the "
             f"book into a worse-than-base wash AND beat the random-day placebo).\n")
    L.append(_md_row(["check", "result"]))
    L.append(_md_row(["---", "---"]))
    L.append(_md_row(["neg-gamma bucket $ improves (net of hedge cost)",
                      f"{'YES' if neg_improves else 'NO'} "
                      f"(${b.get('total_$')} -> ${h.get('total_$')})"]))
    L.append(_md_row(["neg-gamma tail capped (worst-day AND p05 improve)",
                      f"{'YES' if tail_capped else 'NO'} "
                      f"(worst ${b.get('worst_day_$')} -> ${h.get('worst_day_$')}; "
                      f"p05 ${b.get('p05_$')} -> ${h.get('p05_$')})"]))
    L.append(_md_row(["overall book not worse than base",
                      f"{'YES' if not_worse_book else 'NO'} "
                      f"(${ov['base'].get('total_$')} -> ${ov['hedged'].get('total_$')})"]))
    L.append(_md_row(["beats matched random-day placebo (frac < 0.05)",
                      f"{'YES' if beats_placebo else 'NO'} "
                      f"(frac placebo>=real = {pl.get('frac_placebo_ge_real')})"]))
    L.append("")

    # --- 1. neg-gamma bucket, base vs hedged, across the fill band ---
    L.append("## 1. Negative-gamma bucket — base vs base+hedge, across the fill band\n")
    L.append("The bucket this hedge targets. `total_$` net of hedge cost; `worst_day_$` and "
             "`p05_$` are the tail (the whole point is capping these).\n")
    L.append(_md_row(["fill", "arm", "n", "total_$", "avg_$", "win_rate",
                      "worst_day_$", "p05_$", "std_$"]))
    L.append(_md_row(["---"] * 9))
    for frac in FILL_FRACS:
        tag = _FILL_TAG[frac]
        for arm in ("base", "hedged"):
            s = res["neg_bucket"][tag][arm]
            L.append(_md_row([tag, arm, s.get("n"), s.get("total_$"), s.get("avg_$"),
                              s.get("win_rate"), s.get("worst_day_$"), s.get("p05_$"),
                              s.get("std_$")]))
    L.append("")

    # --- 2. overall book, base vs neg-gamma-hedged, across the fill band ---
    L.append("## 2. Overall book — base-only vs negative-gamma-hedged, across the fill band\n")
    L.append("Does hedging only neg-gamma days turn the whole book into a wash worse than "
             "base? (base pt25 P&L on all traded days; hedge added only on neg-gamma days.)\n")
    L.append(_md_row(["fill", "arm", "n", "total_$", "avg_$", "win_rate",
                      "worst_day_$", "p05_$"]))
    L.append(_md_row(["---"] * 8))
    for frac in FILL_FRACS:
        tag = _FILL_TAG[frac]
        for arm in ("base", "hedged"):
            s = res["overall"][tag][arm]
            L.append(_md_row([tag, arm, s.get("n"), s.get("total_$"), s.get("avg_$"),
                              s.get("win_rate"), s.get("worst_day_$"), s.get("p05_$")]))
    L.append("")

    # --- 3. OOS split (headline fill) ---
    L.append("## 3. OOS split (headline 50% fill) — train 2022..2024-06 / test 2024-07..end\n")
    L.append(_md_row(["half", "scope", "arm", "n", "total_$", "worst_day_$", "p05_$"]))
    L.append(_md_row(["---"] * 7))
    for half in ("train", "test"):
        o = res["oos"][half]
        L.append(_md_row([half, "neg-bucket", "base", o["neg_base"].get("n"),
                          o["neg_base"].get("total_$"), o["neg_base"].get("worst_day_$"),
                          o["neg_base"].get("p05_$")]))
        L.append(_md_row([half, "neg-bucket", "hedged", o["neg_hedged"].get("n"),
                          o["neg_hedged"].get("total_$"), o["neg_hedged"].get("worst_day_$"),
                          o["neg_hedged"].get("p05_$")]))
        L.append(_md_row([half, "book", "base", o["book_base"].get("n"),
                          o["book_base"].get("total_$"), o["book_base"].get("worst_day_$"),
                          o["book_base"].get("p05_$")]))
        L.append(_md_row([half, "book", "hedged", o["book_hedged"].get("n"),
                          o["book_hedged"].get("total_$"), o["book_hedged"].get("worst_day_$"),
                          o["book_hedged"].get("p05_$")]))
    L.append("")

    # --- 4. per-year (headline fill) ---
    L.append("## 4. Per-year total P&L (headline 50% fill)\n")
    L.append(_md_row(["year", "neg_n", "neg_base_$", "neg_hedged_$",
                      "book_base_$", "book_hedged_$"]))
    L.append(_md_row(["---"] * 6))
    for yr in sorted(res["year_tbl"]):
        r = res["year_tbl"][yr]
        L.append(_md_row([yr, r["neg_n"], r["neg_base_$"], r["neg_hedged_$"],
                          r["book_base_$"], r["book_hedged_$"]]))
    L.append("")

    # --- 5. placebo detail ---
    L.append("## 5. Matched random-day placebo (headline 50% fill)\n")
    L.append("Hedge a RANDOM set of the same number of traded days instead of the neg-gamma "
             "days; many draws. `neggamma_beats_placebo=True` means the neg-gamma-targeted "
             "book is in the top 5% vs random-day hedging.\n")
    L.append("```\n" + str(pl) + "\n```\n")

    # --- 6. hedge-variant sensitivity (put/call/both, headline) ---
    L.append("## 6. Hedge-variant sensitivity (headline 50% fill, neg-gamma bucket total_$)\n")
    L.append("Two-sided `both` is the pre-registered headline; `put`/`call` shown so no side "
             "is cherry-picked after the fact.\n")
    t = df[df["traded"] & df["hedge_fired"]].copy()
    L.append(_md_row(["variant", "neg_hedged_total_$", "neg_worst_$", "neg_p05_$"]))
    L.append(_md_row(["---"] * 4))
    for v in HEDGE_VARIANTS:
        x = t[f"hedged_pnl_{v}_{htag}"].to_numpy(dtype=float)
        s = _bucket_stats(x)
        L.append(_md_row([v, s.get("total_$"), s.get("worst_day_$"), s.get("p05_$")]))
    base_s = _bucket_stats(t[f"base_pnl_{htag}"].to_numpy(dtype=float))
    L.append(_md_row(["(base, no hedge)", base_s.get("total_$"), base_s.get("worst_day_$"),
                      base_s.get("p05_$")]))
    L.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(L), encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------- #
# Top-level.
# --------------------------------------------------------------------------- #
def run(verbose: bool = True, save: bool = True, n_placebo: int = 2000) -> dict:
    df = run_history(verbose=verbose, save=save)
    res = analyze(df, verbose=verbose, n_placebo=n_placebo)
    if save:
        out = write_markdown_report(
            df, res, Path(__file__).resolve().parent / "output"
            / f"condor_neggamma_hedge_{_dt.date.today():%Y%m%d}.md")
        if verbose:
            print(f"\nReport written: {out}", flush=True)
    return {"days": df, "analysis": res}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="0DTE condor NEGATIVE-GAMMA HEDGE overlay (Arm 2)")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="run only the first N days (smoke test)")
    ap.add_argument("--max-new-days", type=int, default=0,
                    help="process at most N not-yet-done days this run, then exit cleanly "
                         "(fresh-process chunk loop). 0 = no cap.")
    ap.add_argument("--history-only", action="store_true",
                    help="run the day history, skip analysis/report")
    ap.add_argument("--report-only", action="store_true",
                    help="load the finished days CSV and (re)build the report")
    ap.add_argument("--n-placebo", type=int, default=2000)
    args = ap.parse_args()
    if args.report_only:
        _df = pd.read_csv(OUTPUT_DIR / "condor_neggamma_hedge_days.csv")
        _df["traded"] = _df["traded"].astype(str).str.lower().isin(["true", "1"])
        _df["hedge_fired"] = _df["hedge_fired"].astype(str).str.lower().isin(["true", "1"])
        _df["day"] = pd.to_datetime(_df["day"]).dt.date
        _res = analyze(_df, verbose=not args.quiet, n_placebo=args.n_placebo)
        _out = write_markdown_report(
            _df, _res, Path(__file__).resolve().parent / "output"
            / f"condor_neggamma_hedge_{_dt.date.today():%Y%m%d}.md")
        print(f"Report written: {_out}", flush=True)
    elif args.history_only or args.limit or args.max_new_days:
        days = s5.available_days()
        if args.limit:
            days = days[: args.limit]
        run_history(days=days, verbose=not args.quiet, save=not args.no_save,
                    max_new_days=args.max_new_days)
    else:
        run(verbose=not args.quiet, save=not args.no_save, n_placebo=args.n_placebo)
