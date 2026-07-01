r"""
s2s3_intraday_condor.py — the S2/S3 intraday 0DTE IRON-CONDOR backtest + morning-gap gate.

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the warehouse.

WHAT THIS IS (and how it relates to the S6 stack)
-------------------------------------------------
S3 (datacollector/STRATEGIES.md) is the intraday/adaptive evolution of S2's iron-condor
income idea. Its `s3_condor_control.py` is an EOD, held-to-expiry fixed-delta condor that
EXPLICITLY DEFERS four intraday pieces to the 1-minute warehouse:
   (1) 0DTE condors on the intraday P&L PATH,
   (2) intraday regime-triggered exits,
   (3) an intraday morning-gap "wait & measure" gate,
   (4) intraday-path P&L marking.

Pieces (1),(2),(4) were ALREADY built, tested, and RUN on this exact warehouse by the S6
stack (Brandon W's "SPX Cash Flow 0DTE"): s6_control.py is a fixed-delta 0DTE credit-spread
+ IRON-CONDOR engine with honest fills and minute-by-minute intraday P&L marking; s6_matrix.py
is the intraday exit-mode (2x/3x-stop vs hold-to-settle) sensitivity conditioned by day-type
with a plateau-vs-peak discipline. Their honest verdict is a full refutation: the fixed 0DTE
iron condor LOSES (-$29,475 over 1050 days), and all 36 exit x gamma x VIX cells are losses.

To AVOID duplicating that work while still delivering S3's deferred agenda, this module:
  * REUSES the s6 fill/recon/condor engines UNCHANGED (structural agreement, no re-implementation)
    to reproduce the fixed 0DTE iron-condor CONTROL under the S2/S3 name (the yardstick), and
  * BUILDS the one deferred piece the S6 stack did NOT build: the S3 "wait & measure" MORNING-GAP
    gate, plus the S3 research-agenda MEASUREMENTS it is predicated on:
       - overnight/open GAP  ->  day RANGE      (does a gap predict a big move?)
       - morning realized vol -> afternoon RANGE (does AM vol predict PM move?)
  * DECOMPOSES BEFORE BUILDING: it first MEASURES those two relationships (a pure observation,
    always allowed) and only then tests a FROZEN gap gate as a version that must BEAT the fixed
    control OUT-OF-SAMPLE by day-type. A gate that does not beat the control is a valid finding.
  * EMITS the S2/S3 "PV-band" (probability-band) data: the empirical distribution of the day's
    close relative to the entry-time expected move, so the condor's short-strike coverage can be
    read off directly.

THE ANTI-CURVE-FIT SPINE (rule #1)
----------------------------------
  * The CONTROL is the fixed-delta 0DTE iron condor, un-optimized, inherited verbatim from
    s6_control's documented constants (14:00 entry, 5-wide, 0.15 delta, honest bid/ask fills,
    intraday P&L path, hold-to-settle marking). It is the anti-curve-fit benchmark.
  * The gap-gate threshold is NOT swept to a winner. We MEASURE the gap->range relationship,
    pre-register ONE plain gate rule ("sit out days whose open gap exceeds the trailing-median
    gap by a fixed multiple" — a NON-fitted, self-scaling definition), FREEZE it, and report the
    gated version vs control on BOTH time-halves and every day-type bucket. We DEMAND A PLATEAU
    (holds in both halves AND across day-types), not an isolated peak.
  * OBSERVABLE vs TUNABLE: reading historical spot/gaps/ranges is an OBSERVABLE (allowed). The
    gate rule is pre-registered and frozen (not fit). If making it "work" needs tuning, we STOP.

NO LOOK-AHEAD (load-bearing):
  * The gap and morning-rvol are measured from bars that CLOSED at/before the 14:00 entry.
  * The gate decision uses ONLY information knowable by 14:00 (open gap + AM realized vol +
    a trailing median of PAST days' gaps). The trailing median excludes the current day.
  * The condor entry/exit reuse s6_control's causal minute-walk (never peeks past a firing minute).
  * Pinned by tests/test_s2s3_intraday_condor.py + the standing causality guard.

CRASH-RESILIENT + RESUMABLE: per-day incremental CSV append + resume-skip, with a heartbeat
progress line flushed each block, exactly like the s6 harnesses. A killed run loses at most the
in-flight day. ASCII-only console output.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd

import s5_intraday_data as s5
import s6_recon as recon
import s6_control as ctrl
import s6_matrix as mx

# --------------------------------------------------------------------------- #
# Frozen constants. The condor mechanics are inherited VERBATIM from the control
# (NOT re-tuned). The gate constants are pre-registered plain choices, not swept.
# --------------------------------------------------------------------------- #
ENTRY_TIME = ctrl.ENTRY_TIME              # 14:00 ET (the S6/S3 documented entry)
SETTLEMENT_TIME = ctrl.SETTLEMENT_TIME    # 16:00 ET
SPREAD_WIDTH = ctrl.SPREAD_WIDTH          # 5.0
TARGET_SHORT_DELTA = ctrl.TARGET_SHORT_DELTA  # 0.15
MIN_ENTRY_CREDIT = ctrl.MIN_ENTRY_CREDIT  # 0.30 (documented no-trade rule; kept for the control)
CONTRACT_MULTIPLIER = ctrl.CONTRACT_MULTIPLIER
N_CONTRACTS = ctrl.N_CONTRACTS

# Morning reference minutes for the gap + AM realized-vol measurement (all <= 14:00 entry,
# so strictly causal). These are DEFINITIONAL clock choices, not tuned knobs.
OPEN_MINUTE = _dt.time(9, 31)     # first reliably-quoted 0DTE minute (09:30 chain often thin)
MORNING_END = _dt.time(11, 0)     # "morning" window end for AM realized vol (09:31..11:00)

# The gate (DECISION, pre-registered + FROZEN — see docstring). A day is a "big-gap" day if
# its open gap (|open - prior close| / prior close) exceeds GAP_GATE_MULT x the trailing median
# open gap over the prior GAP_LOOKBACK days. This is SELF-SCALING (no absolute % threshold to
# fit) and uses only PAST days. GAP_GATE_MULT=2.0 is the plain "twice the typical gap" choice
# (mirrors S6's frozen 2.0x departure clearance); we do NOT sweep it. The S3 spec's own words:
# the gate is a "wait & measure" gate, NOT a veto -- gaps often precede MUTED days. So the gate's
# hypothesis is DIRECTIONAL and pre-registered: IF big gaps precede bigger ranges, sitting them
# out helps; IF big gaps precede muted days (the spec's stated suspicion), the gate HURTS and
# that refutation is the finding.
GAP_GATE_MULT = 2.0
GAP_LOOKBACK = 20                 # trailing window (trading days) for the median-gap baseline

TRAIN_END = mx.TRAIN_END          # 2024-06-30 (same fixed OOS split as the S6 matrix)

OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "s2s3_research"
_PARTIAL_CSV = OUTPUT_DIR / "s2s3_intraday_condor_partial.csv"


# --------------------------------------------------------------------------- #
# Per-day record: the condor CONTROL outcome + the morning observables (gap, AM rvol)
# + the day's realized range. One row per traded/considered day (iron condor only —
# S2/S3 are the iron-condor income strategy; the single-sided spreads live in S6).
# --------------------------------------------------------------------------- #
@dataclass
class DayRecord:
    day: _dt.date
    # --- morning observables (all causal to <= 14:00) ---
    prior_close: float = float("nan")
    open_spot: float = float("nan")
    entry_spot: float = float("nan")
    gap_abs_pct: float = float("nan")        # |open - prior_close| / prior_close
    gap_signed_pct: float = float("nan")     # (open - prior_close) / prior_close
    am_rvol_pct: float = float("nan")        # realized vol of 09:31..11:00 minute returns (stdev, %)
    # --- day range (realized, for the measurement studies; NOT used by the causal gate) ---
    day_range_pct: float = float("nan")      # (session high - low) / open, full session
    pm_range_pct: float = float("nan")       # (14:00..16:00 high - low) / entry_spot (post-entry)
    close_spot: float = float("nan")
    # --- expected move at entry (for the PV-band) ---
    exp_move_pts: float = float("nan")       # entry_spot * atm_iv * sqrt(T_to_settle)
    close_vs_entry_pts: float = float("nan") # close_spot - entry_spot (signed)
    close_in_em: float = float("nan")        # close_vs_entry / exp_move (z-score in EM units)
    # --- iron-condor CONTROL outcome (fixed 0.15 delta, honest fills, intraday path) ---
    traded: bool = False
    skip_reason: str = ""
    entry_credit: float = float("nan")
    short_put_k: float = float("nan")
    short_call_k: float = float("nan")
    exit_reason: str = ""
    pnl_dollars: float = float("nan")
    # --- gate provenance ---
    gap_baseline_pct: float = float("nan")   # trailing median gap (prior GAP_LOOKBACK days)
    big_gap: bool = False                    # gate flag: is this a big-gap (sit-out) day?
    # --- day-type + split ---
    gamma_regime: str = "unknown"
    vix_regime: str = "unknown"
    half: str = ""


# --------------------------------------------------------------------------- #
# Spot recovery helpers.
# --------------------------------------------------------------------------- #
def _spot_at(nbbo: pd.DataFrame, d: _dt.date, t: _dt.time) -> float:
    """Recovered SPX spot at one minute, or NaN if that minute is absent / unrecoverable."""
    m = pd.Timestamp(_dt.datetime.combine(d, t))
    snap = nbbo[nbbo["minute"] == m][["strike", "right", "bid", "ask"]]
    if snap.empty:
        return float("nan")
    sr = recon.recover_forward_spot(snap, m, d)
    return float(sr.spot) if sr is not None and np.isfinite(sr.spot) else float("nan")


def _spot_series(nbbo: pd.DataFrame, d: _dt.date,
                 t0: _dt.time, t1: _dt.time) -> pd.Series:
    """Per-minute recovered spot over [t0, t1] inclusive (minutes present in the chain)."""
    lo = pd.Timestamp(_dt.datetime.combine(d, t0))
    hi = pd.Timestamp(_dt.datetime.combine(d, t1))
    out = {}
    for m in sorted(nbbo["minute"].unique()):
        if m < lo or m > hi:
            continue
        snap = nbbo[nbbo["minute"] == m][["strike", "right", "bid", "ask"]]
        sr = recon.recover_forward_spot(snap, pd.Timestamp(m), d)
        if sr is not None and np.isfinite(sr.spot):
            out[pd.Timestamp(m)] = sr.spot
    if not out:
        return pd.Series(dtype=float)
    return pd.Series(out).sort_index()


def _atm_iv_at_entry(nbbo: pd.DataFrame, d: _dt.date, entry_minute: pd.Timestamp,
                     spot: float) -> float:
    """ATM implied vol at entry (median of the per-strike IVs nearest the money).

    Reused for the expected-move / PV-band. Observable (read from real quotes), not tuned.
    """
    snap = nbbo[nbbo["minute"] == entry_minute][["strike", "right", "bid", "ask"]]
    if snap.empty:
        return float("nan")
    dtbl = recon.per_strike_delta(snap, entry_minute, d, spot)
    dtbl = dtbl[np.isfinite(dtbl["iv"])]
    if dtbl.empty:
        return float("nan")
    dtbl = dtbl.copy()
    dtbl["dist"] = (dtbl["strike"] - spot).abs()
    near = dtbl.sort_values("dist").head(6)   # ~3 strikes each side
    return float(near["iv"].median())


# --------------------------------------------------------------------------- #
# One day: measure morning observables + run the fixed iron-condor control.
# --------------------------------------------------------------------------- #
def run_day(d: _dt.date, clf: mx.DayClassifier,
            day_data: s5.DayData | None = None) -> DayRecord:
    """Compute one DayRecord: morning observables (causal) + fixed iron-condor control.

    The overnight GAP is NOT computed here. It is derived POST-HOC in `_apply_gap_gate`
    from the recovered `open_spot` of THIS day vs the recovered `close_spot` of the PRIOR
    processed day, over the sorted full history. Doing the gap post-hoc (rather than
    threading a running `prior_close` through the loop) makes the run resume-safe: a killed
    + restarted run cannot corrupt the gap by losing its running prior-close, because the
    gap is always recomputed from the CSV's own sorted open/close columns. Strictly causal:
    day i's gap uses only day i's open and day (i-1)'s close, both already realized.
    Never raises on a single-day quirk; returns a non-traded record with a skip_reason.
    """
    rec = DayRecord(day=d)
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

    # --- Morning observables (causal, all <= entry) ---
    # open_spot is recovered here; the GAP (open vs prior day's close) is filled post-hoc
    # in _apply_gap_gate from the sorted history (see run_day docstring).
    rec.open_spot = _spot_at(nbbo, d, OPEN_MINUTE)

    # AM realized vol: stdev of 1-min log returns 09:31..11:00 (annualization-free; a %/min proxy).
    am = _spot_series(nbbo, d, OPEN_MINUTE, MORNING_END)
    if len(am) >= 10:
        r = np.diff(np.log(am.to_numpy()))
        rec.am_rvol_pct = float(np.std(r, ddof=1) * 100.0)  # %/min stdev

    # Full-session range (realized; used only by the MEASUREMENT studies, never the causal gate).
    full = _spot_series(nbbo, d, OPEN_MINUTE, SETTLEMENT_TIME)
    if len(full) >= 10:
        hi, lo = float(full.max()), float(full.min())
        op = rec.open_spot if np.isfinite(rec.open_spot) else float(full.iloc[0])
        if op > 0:
            rec.day_range_pct = (hi - lo) / op * 100.0

    # --- Entry snapshot: spot, ATM IV, expected move (for the PV-band) ---
    minute_set = set(nbbo["minute"].unique())
    if entry_minute not in minute_set:
        rec.skip_reason = "no 14:00 snapshot"
        return rec
    entry_snap = ctrl._snap_at(nbbo, entry_minute)
    sr = recon.recover_forward_spot(entry_snap, entry_minute, d)
    if sr is None:
        rec.skip_reason = "spot recon failed at entry"
        return rec
    rec.entry_spot = float(sr.spot)

    atm_iv = _atm_iv_at_entry(nbbo, d, entry_minute, rec.entry_spot)
    t_years = recon.time_to_expiry_years(entry_minute, d)
    if np.isfinite(atm_iv) and t_years > 0:
        rec.exp_move_pts = rec.entry_spot * atm_iv * np.sqrt(t_years)

    # PM range (post-entry path) + close.
    pm = full[(full.index >= entry_minute) & (full.index <= settle_minute)] if len(full) else pd.Series(dtype=float)
    if len(pm) >= 2 and rec.entry_spot > 0:
        rec.pm_range_pct = (float(pm.max()) - float(pm.min())) / rec.entry_spot * 100.0
    rec.close_spot = _spot_at(nbbo, d, SETTLEMENT_TIME)
    if not np.isfinite(rec.close_spot) and len(full):
        rec.close_spot = float(full.iloc[-1])
    if np.isfinite(rec.close_spot) and np.isfinite(rec.entry_spot):
        rec.close_vs_entry_pts = rec.close_spot - rec.entry_spot
        if np.isfinite(rec.exp_move_pts) and rec.exp_move_pts > 0:
            rec.close_in_em = rec.close_vs_entry_pts / rec.exp_move_pts

    # --- The fixed iron-condor CONTROL (reuse s6_control's engine UNCHANGED) ---
    delta_tbl = recon.per_strike_delta(entry_snap, entry_minute, d, rec.entry_spot)
    build = ctrl._build_iron_condor(entry_snap, delta_tbl, TARGET_SHORT_DELTA)
    if build is None:
        rec.skip_reason = "could not build iron condor at entry"
        return rec
    rec.short_put_k = build["short_strike"]
    rec.short_call_k = build["short_strike_2"]
    rec.entry_credit = build["entry_credit"]
    if not np.isfinite(build["entry_credit"]) or build["entry_credit"] < MIN_ENTRY_CREDIT:
        rec.skip_reason = f"entry credit {build['entry_credit']:.2f} < {MIN_ENTRY_CREDIT}"
        return rec

    reason, _exit_minute, exit_debit = ctrl._scan_exit(
        nbbo, build["legs"], build["entry_credit"], entry_minute, settle_minute)
    if not np.isfinite(exit_debit):
        rec.skip_reason = "no quoted minute to mark/close"
        return rec
    rec.traded = True
    rec.exit_reason = reason
    rec.pnl_dollars = (build["entry_credit"] - exit_debit) * CONTRACT_MULTIPLIER * N_CONTRACTS
    return rec


# --------------------------------------------------------------------------- #
# Full-history run — crash-resilient + resumable + heartbeat.
# --------------------------------------------------------------------------- #
def run_history(days: list[_dt.date] | None = None, verbose: bool = True,
                save: bool = True, resume: bool = True) -> pd.DataFrame:
    """Run the per-day measurement + control over every available 0DTE day, checkpointing."""
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
    fieldnames = list(asdict(DayRecord(day=days[0])).keys())
    write_header = not _PARTIAL_CSV.is_file()

    with open(_PARTIAL_CSV, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for i, d in enumerate(days, 1):
            if str(d) in done_days:
                continue   # resume-safe: gap is derived post-hoc from the CSV, not threaded here
            try:
                dd = s5.load_day(d)
            except Exception as e:
                if verbose:
                    print(f"[{i}/{n}] {d} LOAD-SKIP {type(e).__name__}", flush=True)
                continue
            rec = run_day(d, clf, day_data=dd)
            writer.writerow(asdict(rec))
            fh.flush()
            if verbose and (i % 25 == 0 or i == n):
                print(f"[{i}/{n}] {d} done  (last pnl={rec.pnl_dollars})", flush=True)

    df = pd.read_csv(_PARTIAL_CSV)
    df["traded"] = df["traded"].astype(str).str.lower().isin(["true", "1"])
    df["day"] = pd.to_datetime(df["day"]).dt.date
    df = df.sort_values("day").reset_index(drop=True)
    # Recompute the trailing-median gap baseline + big_gap flag over the FULL sorted history
    # (this is causal: the trailing median at row i uses rows < i only).
    df = _apply_gap_gate(df)
    if save:
        df.to_csv(OUTPUT_DIR / "s2s3_intraday_condor_days.csv", index=False)
        if verbose:
            print(f"Saved {OUTPUT_DIR / 's2s3_intraday_condor_days.csv'}", flush=True)
    return df


def _apply_gap_gate(df: pd.DataFrame) -> pd.DataFrame:
    """Fill the overnight GAP + add the causal trailing-median baseline + big_gap flag.

    Step 1 (GAP, causal): sort by day; each day's overnight gap = (open_spot - prior day's
    close_spot) / prior close, where "prior day" is the immediately-preceding processed row.
    prior_close = the previous row's close_spot (a shift), so day i's gap uses only realized
    open/close of days <= i -> no look-ahead. Recomputed from the CSV every run, so a resumed
    run cannot corrupt it.

    Step 2 (GATE, pre-registered + frozen): for each day i (in date order),
    baseline = median of the prior GAP_LOOKBACK days' gap_abs_pct (STRICTLY earlier rows),
    big_gap = gap_abs_pct > GAP_GATE_MULT * baseline. Days without enough history OR without a
    computable gap are big_gap=False (we do not sit out what we cannot measure -> the default is
    to TRADE, matching the control on those days).
    """
    df = df.sort_values("day").reset_index(drop=True).copy()

    # Step 1: overnight gap from the prior processed day's close (a strict backward shift).
    prior_close = df["close_spot"].shift(1)
    df["prior_close"] = prior_close
    with np.errstate(invalid="ignore", divide="ignore"):
        signed = (df["open_spot"] - prior_close) / prior_close * 100.0
    signed = signed.where(np.isfinite(prior_close) & (prior_close > 0)
                          & np.isfinite(df["open_spot"]))
    df["gap_signed_pct"] = signed
    df["gap_abs_pct"] = signed.abs()

    # Step 2: trailing-median baseline + frozen gate flag.
    gaps = df["gap_abs_pct"].to_numpy(dtype=float)
    baseline = np.full(len(df), np.nan)
    big = np.zeros(len(df), dtype=bool)
    for i in range(len(df)):
        past = gaps[max(0, i - GAP_LOOKBACK):i]
        past = past[np.isfinite(past)]
        if len(past) >= max(5, GAP_LOOKBACK // 2):
            b = float(np.median(past))
            baseline[i] = b
            g = gaps[i]
            if np.isfinite(g) and b > 0:
                big[i] = g > GAP_GATE_MULT * b
    df["gap_baseline_pct"] = baseline
    df["big_gap"] = big
    return df


# --------------------------------------------------------------------------- #
# Measurement studies (DECOMPOSE BEFORE BUILDING) — pure observation, no strategy.
# --------------------------------------------------------------------------- #
def measure_relationships(df: pd.DataFrame) -> str:
    """Report the two S3-agenda relationships the gate is predicated on, BY REGIME.

    (A) gap_abs_pct -> day_range_pct : Spearman rank corr overall + per gamma/vix bucket, plus a
        contingency of big-gap vs normal-gap day-range terciles.
    (B) am_rvol_pct  -> pm_range_pct : same, morning realized vol vs the post-entry PM range.
    A relationship must be POSITIVE and CONSISTENT across halves+buckets to justify a gate. The
    S3 spec's own suspicion (gaps precede MUTED days) predicts a WEAK/NEGATIVE (A) -- in which
    case the gate cannot help and we say so.
    """
    from scipy.stats import spearmanr

    lines = ["===== MEASUREMENT (decompose before building) ====="]
    d = df.dropna(subset=["gap_abs_pct", "day_range_pct"]).copy()

    def sp(a, b, sub):
        s = sub.dropna(subset=[a, b])
        if len(s) < 30:
            return float("nan"), len(s)
        rho, _p = spearmanr(s[a], s[b])
        return float(rho), len(s)

    lines.append("\n(A) open GAP (abs %) -> full-day RANGE (%)")
    rho, nn = sp("gap_abs_pct", "day_range_pct", d)
    lines.append(f"    overall Spearman rho = {rho:+.3f}  (n={nn})")
    for half in ("train", "test"):
        rho, nn = sp("gap_abs_pct", "day_range_pct", d[d["half"] == half])
        lines.append(f"      {half}: rho = {rho:+.3f} (n={nn})")
    for g in ("positive", "negative"):
        for v in ("contango", "backwardation"):
            cell = d[(d["gamma_regime"] == g) & (d["vix_regime"] == v)]
            rho, nn = sp("gap_abs_pct", "day_range_pct", cell)
            lines.append(f"      {g[:3]}/{v[:4]}: rho = {rho:+.3f} (n={nn})")

    lines.append("\n(B) morning realized vol (%/min) -> POST-ENTRY PM range (%)")
    d2 = df.dropna(subset=["am_rvol_pct", "pm_range_pct"]).copy()
    rho, nn = sp("am_rvol_pct", "pm_range_pct", d2)
    lines.append(f"    overall Spearman rho = {rho:+.3f}  (n={nn})")
    for half in ("train", "test"):
        rho, nn = sp("am_rvol_pct", "pm_range_pct", d2[d2["half"] == half])
        lines.append(f"      {half}: rho = {rho:+.3f} (n={nn})")

    # Big-gap vs normal-gap: mean day range + mean condor P&L (does sitting out big-gap days help?)
    lines.append("\n(C) BIG-GAP vs NORMAL-GAP days (the gate's raw premise)")
    tr = df[df["traded"]].copy()
    bg = tr[tr["big_gap"]]
    ng = tr[~tr["big_gap"]]
    def mrange(x):
        return round(float(x["day_range_pct"].dropna().mean()), 3) if len(x) else float("nan")
    def mpnl(x):
        return round(float(x["pnl_dollars"].mean()), 2) if len(x) else float("nan")
    lines.append(f"    big-gap days:    n={len(bg)}  mean day-range={mrange(bg)}%  "
                 f"mean condor P&L=${mpnl(bg)}")
    lines.append(f"    normal-gap days: n={len(ng)}  mean day-range={mrange(ng)}%  "
                 f"mean condor P&L=${mpnl(ng)}")
    lines.append("    (If big-gap days do NOT have a lower mean condor P&L, the gate removes "
                 "profitable days and cannot help -- refuted.)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CONTROL vs GATED comparison (the version that must beat the control OOS + by day-type).
# --------------------------------------------------------------------------- #
def _stats(sub: pd.DataFrame) -> dict:
    t = sub[sub["traded"]].copy()
    n = len(t)
    if n == 0:
        return {"trades": 0, "total_pnl_$": 0.0, "win_rate": float("nan"),
                "avg_pnl_$": float("nan"), "worst_day_$": float("nan")}
    daily = t.sort_values("day").groupby("day")["pnl_dollars"].sum()
    wins = t[t["pnl_dollars"] > 0]
    return {
        "trades": n,
        "total_pnl_$": round(float(t["pnl_dollars"].sum()), 2),
        "win_rate": round(len(wins) / n, 4),
        "avg_pnl_$": round(float(t["pnl_dollars"].mean()), 2),
        "worst_day_$": round(float(daily.min()), 2),
    }


def compare_control_vs_gate(df: pd.DataFrame) -> str:
    """CONTROL (trade every day) vs GATED (skip big-gap days). Overall + halves + day-types.

    GATED P&L on a day = 0 if big_gap else the control P&L (the gate SITS OUT, it does not
    re-place). The gate beats the control only if it raises total P&L AND does so in BOTH halves
    AND is not driven by a single bucket (plateau, not peak).
    """
    lines = ["\n===== CONTROL vs GAP-GATED (iron condor) ====="]
    tr = df[df["traded"]].copy()
    ctrl_pnl = tr
    gate_pnl = tr[~tr["big_gap"]]        # gated = the days the gate would still trade

    def block(name, sub_ctrl, sub_gate):
        c = _stats(sub_ctrl)
        g = _stats(sub_gate)
        return (f"  {name:<22} CONTROL: n={c['trades']:>4} tot=${c['total_pnl_$']:>9} "
                f"avg=${c['avg_pnl_$']:>7} | GATED: n={g['trades']:>4} "
                f"tot=${g['total_pnl_$']:>9} avg=${g['avg_pnl_$']:>7} | "
                f"delta_tot=${round(g['total_pnl_$'] - c['total_pnl_$'], 2):>9}")

    lines.append(block("ALL", ctrl_pnl, gate_pnl))
    for half in ("train", "test"):
        lines.append(block(f"half={half}",
                           ctrl_pnl[ctrl_pnl["half"] == half],
                           gate_pnl[gate_pnl["half"] == half]))
    for g in ("positive", "negative"):
        for v in ("contango", "backwardation"):
            lines.append(block(
                f"{g[:3]}/{v[:4]}",
                ctrl_pnl[(ctrl_pnl["gamma_regime"] == g) & (ctrl_pnl["vix_regime"] == v)],
                gate_pnl[(gate_pnl["gamma_regime"] == g) & (gate_pnl["vix_regime"] == v)]))

    # Verdict: plateau demand.
    c_all = _stats(ctrl_pnl); g_all = _stats(gate_pnl)
    c_tr = _stats(ctrl_pnl[ctrl_pnl["half"] == "train"]); g_tr = _stats(gate_pnl[gate_pnl["half"] == "train"])
    c_te = _stats(ctrl_pnl[ctrl_pnl["half"] == "test"]); g_te = _stats(gate_pnl[gate_pnl["half"] == "test"])
    better_all = g_all["total_pnl_$"] > c_all["total_pnl_$"]
    better_tr = g_tr["total_pnl_$"] > c_tr["total_pnl_$"]
    better_te = g_te["total_pnl_$"] > c_te["total_pnl_$"]
    lines.append("\n  VERDICT:")
    if better_all and better_tr and better_te:
        lines.append("    Gate raises total P&L in BOTH halves = PLATEAU on the OOS axis. "
                     "(Still note: raising a NEGATIVE control toward zero is loss-reduction, "
                     "NOT a profitable strategy -- see the control sign.)")
    elif better_all:
        lines.append("    Gate raises total P&L overall but NOT in both halves = PEAK -- "
                     "not robust; reject per curve-fit caution.")
    else:
        lines.append("    Gate does NOT raise total P&L overall -- the morning-gap wait-and-measure "
                     "gate does not help the fixed 0DTE iron condor. Refuted.")
    lines.append(f"    (control total P&L = ${c_all['total_pnl_$']}; if negative, the underlying "
                 f"fixed 0DTE condor is unprofitable regardless -- consistent with the S6 refutation.)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# PV-band (probability-band) artifact — empirical close-vs-expected-move distribution.
# --------------------------------------------------------------------------- #
def build_pv_band(df: pd.DataFrame) -> pd.DataFrame:
    """The S2/S3 'PV-band': empirical distribution of (close - entry) in EXPECTED-MOVE units.

    For every traded day we have close_in_em = (close - entry) / expected_move. The PV-band is
    the empirical CDF of that z-score: what fraction of days the close landed within +/- k
    expected moves of entry. Directly readable as 'how far out must the condor's short strikes sit
    to cover X% of days'. Also the fraction of days the CLOSE breached the control's own short
    strikes (a realized-coverage check of the fixed 0.15-delta placement). Reported overall + by
    regime; saved to CSV for charting.
    """
    t = df[df["traded"]].dropna(subset=["close_in_em"]).copy()
    rows = []
    ks = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]

    def band_row(name, sub):
        z = sub["close_in_em"].to_numpy(dtype=float)
        row = {"bucket": name, "n": len(z)}
        for k in ks:
            row[f"within_{k}EM"] = round(float(np.mean(np.abs(z) <= k)), 4) if len(z) else float("nan")
        # realized coverage of the control's short strikes (close outside [short_put, short_call]).
        sc = sub.dropna(subset=["short_put_k", "short_call_k", "close_spot"])
        if len(sc):
            inside = ((sc["close_spot"] >= sc["short_put_k"]) &
                      (sc["close_spot"] <= sc["short_call_k"]))
            row["close_inside_shorts"] = round(float(inside.mean()), 4)
        else:
            row["close_inside_shorts"] = float("nan")
        return row

    rows.append(band_row("ALL", t))
    for half in ("train", "test"):
        rows.append(band_row(f"half={half}", t[t["half"] == half]))
    for g in ("positive", "negative"):
        for v in ("contango", "backwardation"):
            rows.append(band_row(f"{g[:3]}/{v[:4]}",
                                 t[(t["gamma_regime"] == g) & (t["vix_regime"] == v)]))
    return pd.DataFrame(rows)


def _plot_pv_band(df: pd.DataFrame, path: Path) -> bool:
    """Render the PV-band chart (empirical CDF of |close - entry| in EM units). Returns ok."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return False
    t = df[df["traded"]].dropna(subset=["close_in_em"]).copy()
    if t.empty:
        return False
    fig, ax = plt.subplots(figsize=(8, 5))
    xs = np.linspace(0, 3.5, 200)
    for name, sub in [("ALL", t), ("train", t[t["half"] == "train"]),
                      ("test", t[t["half"] == "test"])]:
        z = np.abs(sub["close_in_em"].to_numpy(dtype=float))
        if len(z) < 10:
            continue
        ys = [np.mean(z <= x) for x in xs]
        ax.plot(xs, ys, label=f"{name} (n={len(z)})")
    ax.axvline(1.0, color="grey", ls="--", lw=0.8)
    ax.set_xlabel("|close - entry| in expected-move units (k)")
    ax.set_ylabel("fraction of days close within +/- k EM")
    ax.set_title("S2/S3 PV-band: 0DTE close coverage vs entry expected move")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return True


# --------------------------------------------------------------------------- #
# Top-level pipeline.
# --------------------------------------------------------------------------- #
def run(verbose: bool = True, save: bool = True) -> dict:
    df = run_history(verbose=verbose, save=save)
    meas = measure_relationships(df)
    cmp = compare_control_vs_gate(df)
    pv = build_pv_band(df)
    if save:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        pv.to_csv(OUTPUT_DIR / "s2s3_pv_band.csv", index=False)
        _plot_pv_band(df, OUTPUT_DIR / "s2s3_pv_band.png")
    if verbose:
        print("\n" + meas, flush=True)
        print("\n" + cmp, flush=True)
        print("\n===== PV-BAND (close coverage in expected-move units) =====", flush=True)
        with pd.option_context("display.width", 200, "display.max_columns", 30):
            print(pv.to_string(index=False), flush=True)
    return {"days": df, "measurement": meas, "comparison": cmp, "pv_band": pv}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="S2/S3 intraday 0DTE iron-condor + morning-gap gate")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    run(verbose=not args.quiet, save=not args.no_save)
