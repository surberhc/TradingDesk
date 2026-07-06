r"""
condor_cashsettle_hold.py -- ARM 5 of the 0DTE iron-condor reopen
(docs\PREREG_condor_reopen_2026-07-06.md + output\condor_width_sweep_20260706.md):
the PURE HOLD-TO-CASH-SETTLEMENT arm. NO management, NO early close, ZERO exit spread.

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the warehouse.

THE HYPOTHESIS (mechanism-first, Andrew-blessed)
------------------------------------------------
Arms 1-4 all refuted on ONE wall: the 4-leg bid/ask spread crossed on EXIT swamps the thin
0DTE credit. But SPXW 0DTE options are EUROPEAN and CASH-SETTLED at the 4pm print. If you
HOLD TO SETTLEMENT with NO early management, you cross ZERO exit spread: winners just expire
worthless, breaches settle to cash intrinsic against the 16:00 index level. This arm combines
the WIDEST WINGS (best credit-to-cost, from Arm 1) with ZERO-EXIT-CROSSING (pure cash
settlement) -- the one cell nobody tested.

It is genuinely two-sided: removing the exit tax could flip the condor net-positive, OR the
uncapped-until-the-wings breach tail (NO stop, NO management) could eat it. Either outcome is
valid. We do NOT move goalposts either way.

WHY THIS IS DIFFERENT FROM A_hold (the prior "hold-to-settle" arm)
------------------------------------------------------------------
In s6_control._scan_exit and condor_management_experiment/condor_width_sweep, "settle" closes
the position at the LAST QUOTED MINUTE'S honest 4-leg debit-to-close -- i.e. it BUYS BACK the
shorts at the ASK and SELLS the wings at the BID. That is a FULL 4-leg spread cross at ~16:00,
plus it also crossed the spread on any $0.05-winner / 2x-stop early close. So A_hold (which lost
-$32,905 at full fill on 5pt) is NOT a no-touch cash settlement.

THIS arm resolves EVERY position at COSTLESS CASH INTRINSIC against the recovered 16:00 index
level S*:
    put-side loss  = min(max(K_short_put  - S*, 0), width)      (breach capped at the wing)
    call-side loss = min(max(S* - K_short_call, 0), width)
    settle P&L     = entry_credit(fill) - put_loss - call_loss     (times 100 x 1 contract)
The ENTRY credit still uses the honest fill band (mid / f25 / f50 / full); ONLY the exit is
costless. No bid/ask is crossed at settlement -- that is the exact mechanism under test, and it
is stated prominently in the report.

FROZEN, inherited VERBATIM from the control (rule #1 anti-curve-fit)
-------------------------------------------------------------------
Entry 14:00 ET, short strikes at 0.15-delta (the control's own _pick_short_by_delta on the
control's own per-strike deltas), 16:00 settlement, honest 4-leg ENTRY fills, $0.30 min-credit
no-trade floor. The width ladder is Arm 1's exact 5 / 10 / 20 / 30 / 50-pt. MANAGEMENT IS NONE:
no profit target, no stop, no early close -- every position is held to 16:00 cash settlement.

FILL BAND: entry credit reported across mid / f25 / f50 (HEADLINE) / full worst-side, the same
blended net-combo axis as Arm 1 (reused cm._blended_credit_to_open). f=1.0 is the control's
honest worst-side entry.

TAIL IS FIRST-CLASS: with no management, breach risk is the WHOLE story. We report worst-day,
p01, p05, per-YEAR breakdown, breach rate and average breach-day loss for every width. A total
that is positive only because calm years outweigh catastrophic breach days is NOT adoptable, and
the report says so explicitly if that is the shape.

NO LOOK-AHEAD: entry uses ONLY the 14:00 snapshot; settlement uses ONLY the 16:00 snapshot (the
recovered index level at the settlement instant). No minute between is consulted for a decision,
so there is no path dependence to peek at. Pinned by tests/test_condor_cashsettle_hold.py + the
standing causality guard.

CRASH-RESILIENT + RESUMABLE: per-day incremental CSV append + resume-skip; a SINGLE supervised
--max-new-days chunk loop (NO detached nohup+relaunch -- a prior arm double-wrote its CSV that
way). del + gc.collect() each day. ASCII-only console output.
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
import condor_width_sweep as ws

# --------------------------------------------------------------------------- #
# Entry chassis -- inherited VERBATIM from the control (NOT re-tuned).
# --------------------------------------------------------------------------- #
ENTRY_TIME = ctrl.ENTRY_TIME              # 14:00 ET
SETTLEMENT_TIME = ctrl.SETTLEMENT_TIME    # 16:00 ET
TARGET_SHORT_DELTA = ctrl.TARGET_SHORT_DELTA  # 0.15
MIN_ENTRY_CREDIT = ctrl.MIN_ENTRY_CREDIT  # 0.30
CONTRACT_MULTIPLIER = ctrl.CONTRACT_MULTIPLIER
N_CONTRACTS = ctrl.N_CONTRACTS

# --------------------------------------------------------------------------- #
# Width ladder -- Arm 1's exact pre-registered grid (frozen; NOT swept to a winner).
# 5.0 is the control and MUST be present so the ladder contains its baseline.
# --------------------------------------------------------------------------- #
WIDTHS = ws.WIDTHS                         # (5.0, 10.0, 20.0, 30.0, 50.0)
assert ctrl.SPREAD_WIDTH in WIDTHS, "the ladder must contain the 5-pt control width"

# --------------------------------------------------------------------------- #
# FILL BAND -- ENTRY-only (the exit is costless cash settlement; there is no exit spread to
# blend). Reuse the management experiment's blended OPEN-credit so the entry fill math is one
# code path. f=0 mid / f=1 worst-side (the control's honest entry bound).
# --------------------------------------------------------------------------- #
FILL_FRACS = cm.FILL_FRACS                # (0.0, 0.25, 0.50, 1.0)
HEADLINE_FILL = cm.HEADLINE_FILL          # 0.50
_FILL_TAG = cm._FILL_TAG                   # {0.0:"mid",0.25:"f25",0.50:"f50",1.0:"full"}

TRAIN_END = mx.TRAIN_END                  # 2024-06-30, same OOS split as everything upstream

OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "condor_cashsettle_hold"
_PARTIAL_CSV = OUTPUT_DIR / "condor_cashsettle_hold_partial.csv"


def _wtag(w: float) -> str:
    return f"w{int(w)}"

WIDTH_TAGS = tuple(_wtag(w) for w in WIDTHS)


# --------------------------------------------------------------------------- #
# THE NEW MECHANISM: costless cash-settlement intrinsic of an iron condor at the 16:00 index.
# European, cash-settled -> NO bid/ask crossed. Each short spread's loss is the short-strike
# intrinsic against the settlement level S*, CAPPED at the wing width (defined-risk). Winners
# (S* between the shorts) settle to zero loss; the whole credit is kept.
# --------------------------------------------------------------------------- #
def condor_cash_settle_pnl(entry_credit: float, settle_spot: float,
                           short_put_k: float, short_call_k: float,
                           width: float) -> float:
    """P&L in POINTS of holding the iron condor to costless cash settlement at S* = settle_spot.

    entry_credit : net credit received at entry (points; whatever fill fraction was used).
    settle_spot  : the recovered 16:00 index level (the cash-settlement print).
    short_put_k  : the short put strike (breached when S* < short_put_k).
    short_call_k : the short call strike (breached when S* > short_call_k).
    width        : the wing distance (points); both breach losses are capped at it.

    Returns entry_credit - put_loss - call_loss. NO spread is crossed -- this is the mechanism
    being tested. Both sides cannot breach on the same day (short_put_k < short_call_k), so at
    most one loss term is nonzero.
    """
    put_loss = min(max(short_put_k - settle_spot, 0.0), width)
    call_loss = min(max(settle_spot - short_call_k, 0.0), width)
    return entry_credit - put_loss - call_loss


# --------------------------------------------------------------------------- #
# Per-day record: one row per day, with per-(width x fill) settle-P&L + the settlement diagnostics
# (recovered 16:00 index level, breach side, breach depth) so the tail is fully auditable.
# --------------------------------------------------------------------------- #
@dataclass
class DayRecord:
    day: _dt.date
    traded: bool = False
    skip_reason: str = ""
    entry_spot: float = float("nan")
    settle_spot: float = float("nan")     # recovered 16:00 index level (cash-settlement print)
    gamma_regime: str = "unknown"
    vix_regime: str = "unknown"
    half: str = ""
    widths: dict = None                   # per-width blocks attached dynamically; flattened on write

    def flat(self) -> dict:
        base = {k: v for k, v in asdict(self).items() if k != "widths"}
        w = self.widths or {}
        for wt in WIDTH_TAGS:
            sub = w.get(wt, {})
            base[f"short_put_k_{wt}"] = sub.get("short_put_k", float("nan"))
            base[f"short_call_k_{wt}"] = sub.get("short_call_k", float("nan"))
            base[f"breach_{wt}"] = sub.get("breach", "")          # 'none'|'put'|'call'
            base[f"breach_depth_{wt}"] = sub.get("breach_depth", float("nan"))  # points into the spread
            for frac in FILL_FRACS:
                tag = _FILL_TAG[frac]
                base[f"entry_credit_{wt}_{tag}"] = sub.get(tag, {}).get("entry_credit", float("nan"))
                base[f"pnl_{wt}_{tag}"] = sub.get(tag, {}).get("pnl", float("nan"))
        return base


def _flat_fieldnames() -> list[str]:
    base = [k for k in asdict(DayRecord(day=_dt.date(2022, 1, 3))).keys() if k != "widths"]
    for wt in WIDTH_TAGS:
        base += [f"short_put_k_{wt}", f"short_call_k_{wt}",
                 f"breach_{wt}", f"breach_depth_{wt}"]
        for frac in FILL_FRACS:
            tag = _FILL_TAG[frac]
            base += [f"entry_credit_{wt}_{tag}", f"pnl_{wt}_{tag}"]
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
# Recover the 16:00 cash-settlement index level from the 16:00 NBBO snapshot via the SAME
# put-call-parity forward/spot recovery the engine already trusts for entry. This is the
# settlement print; it crosses NO bid/ask (mids only, for parity), so it is not a fill.
# --------------------------------------------------------------------------- #
def recover_settle_spot(nbbo: pd.DataFrame, d: _dt.date,
                        settle_minute: pd.Timestamp) -> float | None:
    snap = ctrl._snap_at(nbbo, settle_minute)
    if snap.empty:
        return None
    sr = recon.recover_forward_spot(snap, settle_minute, d)
    return None if sr is None else float(sr.spot)


# --------------------------------------------------------------------------- #
# One day: for each width, build the 0.15-delta condor at 14:00 (honest entry across the fill
# band), then settle EVERY width to costless cash intrinsic at the recovered 16:00 index level.
# NO management, NO early exit, NO exit spread.
# --------------------------------------------------------------------------- #
def run_day(d: _dt.date, clf: mx.DayClassifier,
            day_data: s5.DayData | None = None) -> DayRecord:
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
    minute_set = set(nbbo["minute"].unique())
    if entry_minute not in minute_set:
        rec.skip_reason = "no 14:00 snapshot"
        return rec
    if settle_minute not in minute_set:
        rec.skip_reason = "no 16:00 snapshot"
        return rec

    entry_snap = ctrl._snap_at(nbbo, entry_minute)
    sr = recon.recover_forward_spot(entry_snap, entry_minute, d)
    if sr is None:
        rec.skip_reason = "spot recon failed at entry"
        return rec
    rec.entry_spot = float(sr.spot)
    delta_tbl = recon.per_strike_delta(entry_snap, entry_minute, d, rec.entry_spot)

    # THE cash-settlement print: the recovered 16:00 index level. Required for every width.
    settle_spot = recover_settle_spot(nbbo, d, settle_minute)
    if settle_spot is None or not np.isfinite(settle_spot):
        rec.skip_reason = "settle-spot recon failed at 16:00"
        return rec
    rec.settle_spot = float(settle_spot)

    any_traded = False
    for w in WIDTHS:
        wt = _wtag(w)
        build = ws.build_condor_at_width(entry_snap, delta_tbl, w)
        if build is None:
            rec.widths[wt] = {"skip_reason": "could not build condor at width"}
            continue
        credit_honest = build["entry_credit"]   # worst-side (full) credit -- the control's honest math
        if not np.isfinite(credit_honest) or credit_honest < MIN_ENTRY_CREDIT:
            rec.widths[wt] = {"skip_reason": f"credit {credit_honest:.2f} < {MIN_ENTRY_CREDIT}"}
            continue

        spk, sck = build["short_put_k"], build["short_call_k"]
        # Breach diagnostics (independent of fill fraction -- settlement level is the same).
        if settle_spot < spk:
            breach = "put"
            breach_depth = float(min(spk - settle_spot, w))
        elif settle_spot > sck:
            breach = "call"
            breach_depth = float(min(settle_spot - sck, w))
        else:
            breach = "none"
            breach_depth = 0.0

        block = {"short_put_k": spk, "short_call_k": sck,
                 "breach": breach, "breach_depth": breach_depth}
        wt_traded = True
        for frac in FILL_FRACS:
            tag = _FILL_TAG[frac]
            credit_f = cm._blended_credit_to_open(entry_snap, build["legs"], frac)
            if credit_f is None or not np.isfinite(credit_f):
                block[tag] = {"entry_credit": float("nan"), "pnl": float("nan")}
                wt_traded = False
                continue
            pnl_pts = condor_cash_settle_pnl(credit_f, settle_spot, spk, sck, w)
            pnl = pnl_pts * CONTRACT_MULTIPLIER * N_CONTRACTS
            block[tag] = {"entry_credit": float(credit_f), "pnl": float(pnl)}
        rec.widths[wt] = block
        any_traded = any_traded or wt_traded

    rec.traded = any_traded
    if not any_traded and not rec.skip_reason:
        rec.skip_reason = "no width tradeable"
    return rec


# --------------------------------------------------------------------------- #
# Full-history run -- crash-resilient + resumable + heartbeat + chunk cap.
# SINGLE supervised process per chunk (no detached relaunch); the partial CSV is durable per day.
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
                      f"(last {d}, traded={rec.traded}, settle_spot={rec.settle_spot:.1f}) "
                      f"[crash-skips: {n_crash_skips}]", flush=True)
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
        df.to_csv(OUTPUT_DIR / "condor_cashsettle_hold_days.csv", index=False)
        if verbose:
            print(f"Saved {OUTPUT_DIR / 'condor_cashsettle_hold_days.csv'}", flush=True)
    return df


# --------------------------------------------------------------------------- #
# Stats helpers.
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
    """Per-width stats at ONE entry-fill fraction (column pnl_{wt}_{tag}). Tail-first: worst-day,
    p01, p05, plus the breach rate and average breach-day loss for the width."""
    t = (sub if sub is not None else df)
    t = t[t["traded"]].copy()
    col = f"pnl_{wt}_{tag}"
    if col not in t.columns:
        return {"width": wt, "fill": tag, "trades": 0, "total_$": 0.0}
    mask = np.isfinite(t[col].to_numpy(dtype=float))
    tt = t[mask]
    x = tt[col].to_numpy(dtype=float)
    n = len(x)
    if n == 0:
        return {"width": wt, "fill": tag, "trades": 0, "total_$": 0.0}
    wins = x[x > 0]
    bcol = f"breach_{wt}"
    breached = tt[bcol].astype(str).isin(["put", "call"]) if bcol in tt.columns else pd.Series(False, index=tt.index)
    n_breach = int(breached.sum())
    breach_loss = x[breached.to_numpy()] if n_breach else np.array([])
    return {
        "width": wt, "fill": tag, "trades": n,
        "total_$": round(float(x.sum()), 2),
        "win_rate": round(len(wins) / n, 4),
        "avg_$": round(float(x.mean()), 2),
        "worst_day_$": round(float(x.min()), 2),
        "p01_$": round(float(np.percentile(x, 1)), 2),
        "p05_$": round(float(np.percentile(x, 5)), 2),
        "std_$": round(float(x.std(ddof=1)), 2),
        "sharpe_ann": round(_ann_sharpe(x), 3),
        "sortino_ann": round(_ann_sortino(x), 3),
        "breach_rate": round(n_breach / n, 4),
        "n_breach": n_breach,
        "avg_breach_loss_$": round(float(breach_loss.mean()), 2) if n_breach else float("nan"),
    }


# --------------------------------------------------------------------------- #
# Matched random-day sit-out PLACEBO: is being-in-the-market the source, or the structure?
# For a width, draw a random subset of days sat out (traded on the rest) and total the book;
# repeat. If the arm's full-participation total is NOT in the top tail of "trade a random subset
# of the same days", then being in the market on all days is the driver, not the structure.
# (There is no exit-timing to randomize -- the exit is deterministic cash settlement -- so the
# apt placebo is random PARTICIPATION, matched to the arm's own trade count.)
# --------------------------------------------------------------------------- #
def random_dayout_placebo(pnls: np.ndarray, n_draws: int = 5000, seed: int = 7) -> dict:
    """pnls: the per-day settle P&L for a width (finite days only). The arm trades EVERY day, so
    arm_total = sum(pnls). Placebo: for each draw, keep a random fraction p ~ U(0.5,1.0) of the
    days (matched-count families) and sum -- the distribution of totals under random participation
    on the SAME days. Report the fraction of draws that meet/beat the arm. If the arm is not in the
    top tail, the structure adds nothing beyond simply being in the market."""
    x = pnls[np.isfinite(pnls)]
    if len(x) == 0:
        return {"skipped": "no finite pnls"}
    arm_total = float(x.sum())
    rng = np.random.default_rng(seed)
    n = len(x)
    totals = np.empty(n_draws)
    for k in range(n_draws):
        keep = rng.random(n) < rng.uniform(0.5, 1.0)
        totals[k] = float(x[keep].sum()) if keep.any() else 0.0
    frac_ge = float(np.mean(totals >= arm_total))
    return {
        "n_days": n,
        "arm_total_$": round(arm_total, 2),
        "placebo_p50_$": round(float(np.percentile(totals, 50)), 2),
        "placebo_p95_$": round(float(np.percentile(totals, 95)), 2),
        "frac_placebo_ge_arm": round(frac_ge, 4),
        "arm_beats_placebo": frac_ge < 0.05,
    }


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


def analyze(df: pd.DataFrame, verbose: bool = True) -> dict:
    t = df[df["traded"]].copy()
    t["year"] = pd.to_datetime(t["day"]).dt.year
    htag = _FILL_TAG[HEADLINE_FILL]

    scopes = {"overall": t, "train": t[t["half"] == "train"], "test": t[t["half"] == "test"]}

    # width x fill total-$ pivot.
    def total_pivot(sub):
        piv = {}
        for frac in FILL_FRACS:
            tag = _FILL_TAG[frac]
            piv[tag] = {_wtag(w): round(float(sub[f"pnl_{_wtag(w)}_{tag}"].sum()), 0) for w in WIDTHS}
        p = pd.DataFrame(piv).reindex(WIDTH_TAGS)
        p.index.name = "width"
        return p
    total_by_fill = {s: total_pivot(sub) for s, sub in scopes.items()}

    # full per-width stats at headline fill (tail-first).
    hband = pd.DataFrame([width_stats_fill(df, _wtag(w), htag, sub=t) for w in WIDTHS]).set_index("width")

    # per-year at headline fill.
    year_tbl = {_wtag(w): t.groupby("year")[f"pnl_{_wtag(w)}_{htag}"].sum().round(0).to_dict()
                for w in WIDTHS}
    # per-regime at headline fill.
    regime_tbl = {}
    for rk in ("gamma_regime", "vix_regime"):
        regime_tbl[rk] = {_wtag(w): t.groupby(rk)[f"pnl_{_wtag(w)}_{htag}"].sum().round(0).to_dict()
                          for w in WIDTHS}

    # placebo at headline fill for any width net-positive overall.
    placebos = {}
    for w in WIDTHS:
        wt = _wtag(w)
        x = t[f"pnl_{wt}_{htag}"].to_numpy(dtype=float)
        x = x[np.isfinite(x)]
        if x.sum() <= 0:
            placebos[wt] = {"skipped": "not net-positive overall at f50"}
            continue
        placebos[wt] = random_dayout_placebo(x)

    if verbose:
        print("\n===== TOTAL $ by WIDTH x FILL (overall) =====", flush=True)
        print(total_by_fill["overall"].to_string(), flush=True)
        print("\n===== HEADLINE (f50) per-width tail stats =====", flush=True)
        print(hband.to_string(), flush=True)
        print("\n===== f50 PLACEBO =====", flush=True)
        for wt, p in placebos.items():
            print(f"  {wt}: {p}", flush=True)

    return {"total_by_fill": total_by_fill, "hband": hband, "year_tbl": year_tbl,
            "regime_tbl": regime_tbl, "placebos": placebos, "headline_tag": htag}


def write_markdown_report(df: pd.DataFrame, an: dict, out_path: Path,
                          arm1_days_csv: Path | None = None) -> Path:
    htag = an["headline_tag"]
    tot = an["total_by_fill"]
    placebos = an["placebos"]

    traded = df[df["traded"]].copy()
    n_traded = len(traded)
    n_days = len(df)
    n_skip = int((~df["traded"]).sum())
    crash_skips = df["skip_reason"].astype(str).str.startswith("crash-skip").sum()
    dmin, dmax = df["day"].min(), df["day"].max()

    # PASS determination: positive plateau across >=3 ADJACENT widths at f50, OOS-stable,
    # AND a survivable (non-catastrophic) tail, AND beats the matched placebo.
    f50_by_width = {_wtag(w): float(traded[f"pnl_{_wtag(w)}_{htag}"].sum()) for w in WIDTHS}
    pos_flags = [f50_by_width[_wtag(w)] > 0 for w in WIDTHS]
    best_run = cur = 0
    for f in pos_flags:
        cur = cur + 1 if f else 0
        best_run = max(best_run, cur)
    plateau_ok = best_run >= 3
    test = traded[traded["half"] == "test"]
    f50_test = {_wtag(w): float(test[f"pnl_{_wtag(w)}_{htag}"].sum()) for w in WIDTHS}
    passed_placebo = [wt for wt, p in placebos.items()
                      if isinstance(p, dict) and p.get("arm_beats_placebo")]
    robust_widths = [_wtag(w) for w in WIDTHS
                     if f50_by_width[_wtag(w)] > 0 and f50_test[_wtag(w)] > 0
                     and _wtag(w) in passed_placebo]
    overall_pass = plateau_ok and bool(robust_widths)

    # Head-to-head vs Arm 1's pt25-managed f50 number at each width, if the Arm 1 CSV is present.
    arm1_f50 = None
    if arm1_days_csv and Path(arm1_days_csv).is_file():
        try:
            a1 = pd.read_csv(arm1_days_csv)
            a1["traded"] = a1["traded"].astype(str).str.lower().isin(["true", "1"])
            a1t = a1[a1["traded"]]
            arm1_f50 = {_wtag(w): round(float(a1t[f"pnl_{_wtag(w)}_{htag}"].sum()), 0)
                        for w in WIDTHS if f"pnl_{_wtag(w)}_{htag}" in a1t.columns}
        except Exception:
            arm1_f50 = None

    L = []
    L.append("# 0DTE Iron-Condor PURE HOLD-TO-CASH-SETTLEMENT (ARM 5) -- finished-window report\n")
    L.append(f"_Generated {_dt.date.today().isoformat()}. Window {dmin} -> {dmax}, "
             f"{n_days} session-days ({n_traded} traded, {n_skip} no-trade/skip, "
             f"{crash_skips} crash-skipped). PAPER / research only._\n")
    L.append("Pre-registration: `docs/PREREG_condor_reopen_2026-07-06.md` (Arm 5, the "
             "hold-to-settlement card noted in `output/condor_width_sweep_20260706.md`). Entry "
             "chassis frozen from the control (14:00 entry, 0.15-delta shorts, honest 4-leg ENTRY "
             "fills, $0.30 min-credit floor). Width ladder = Arm 1's **5 (control) / 10 / 20 / 30 "
             "/ 50-pt**.\n")

    L.append("## 0. The mechanism under test (stated prominently)\n")
    L.append("**Management is NONE.** No profit target, no stop, no early close. Every position is "
             "held to 16:00 and resolved at COSTLESS CASH INTRINSIC against the recovered 16:00 "
             "index level S* -- SPXW 0DTE options are European and cash-settled, so **ZERO exit "
             "bid/ask spread is crossed**. Put-side loss = min(max(K_short_put - S*, 0), width); "
             "call-side loss = min(max(S* - K_short_call, 0), width); settle P&L = entry_credit - "
             "losses. The ENTRY credit still uses the honest fill band (mid/f25/f50/full). This is "
             "the single thing that distinguishes Arm 5 from the prior 'hold-to-settle' A_hold arm, "
             "which closed at the last quoted minute's full 4-leg bid/ask debit (a real exit "
             "spread) and lost -$32,905 at full fill on 5pt.\n")

    L.append("## 1. Total P&L ($) by WIDTH x ENTRY-FILL FRACTION -- OVERALL\n")
    L.append("`mid`=0% (optimistic entry), `f25`=25%, `f50`=50% (**HEADLINE**), `full`=100% "
             "worst-side (the control's honest entry bound). The EXIT is costless cash settlement "
             "at every fill -- only the entry credit moves with the fraction.\n")
    L.append(_md_table(tot["overall"]) + "\n")
    L.append("### TRAIN (2022-01 .. 2024-06)\n")
    L.append(_md_table(tot["train"]) + "\n")
    L.append("### TEST / OOS (2024-07 .. end)\n")
    L.append(_md_table(tot["test"]) + "\n")

    L.append("## 2. TAIL IS FIRST-CLASS -- per-width stats at the HEADLINE f50 fill\n")
    L.append("No management => breach risk is the whole story. `breach_rate` = fraction of days "
             "the index settled beyond a short strike; `avg_breach_loss_$` = mean P&L on those "
             "days (credit minus capped intrinsic).\n")
    keep = [c for c in ("trades", "total_$", "win_rate", "avg_$", "worst_day_$", "p01_$", "p05_$",
                        "std_$", "sharpe_ann", "sortino_ann", "breach_rate", "n_breach",
                        "avg_breach_loss_$") if c in an["hband"].columns]
    L.append(_md_table(an["hband"][keep], floatfmt="{:,.3f}") + "\n")

    L.append("## 3. Per-year total P&L per width (headline f50 fill)\n")
    L.append("The tail test: is a positive total driven by calm years while a single breach year "
             "is catastrophic? A calm-carry-plus-catastrophe shape is NOT adoptable.\n")
    ydf = pd.DataFrame(an["year_tbl"]).T
    ydf.index.name = "width"
    L.append(_md_table(ydf) + "\n")

    L.append("## 4. Per-regime total P&L per width (headline f50 fill)\n")
    for rk, tbl in an["regime_tbl"].items():
        rdf = pd.DataFrame(tbl).T
        rdf.index.name = "width"
        L.append(f"### by {rk}\n")
        L.append(_md_table(rdf) + "\n")

    L.append("## 5. Head-to-head vs Arm 1 (pt25-managed) at f50, per width\n")
    L.append("Does removing the exit spread (pure cash settle, this arm) actually beat MANAGING "
             "the same condor (Arm 1's 25%-profit-target + 2x stop, which crosses the exit spread)? "
             "Both at the f50 headline fill.\n")
    if arm1_f50:
        h2h = pd.DataFrame({
            "arm5_cashsettle_$": {wt: round(f50_by_width[wt], 0) for wt in WIDTH_TAGS},
            "arm1_pt25_managed_$": {wt: arm1_f50.get(wt, float("nan")) for wt in WIDTH_TAGS},
        })
        h2h["arm5_minus_arm1_$"] = h2h["arm5_cashsettle_$"] - h2h["arm1_pt25_managed_$"]
        h2h.index.name = "width"
        L.append(_md_table(h2h) + "\n")
    else:
        L.append("_Arm 1 days CSV not found; head-to-head omitted. "
                 f"(Arm 5 f50 by width: " +
                 ", ".join(f"{wt}=${f50_by_width[wt]:,.0f}" for wt in WIDTH_TAGS) + ".)_\n")

    L.append("## 6. Matched random-day-out PLACEBO (headline f50 fill)\n")
    L.append("The exit is deterministic (cash settlement), so the apt placebo is random "
             "PARTICIPATION: trade a random subset of the SAME days and total the book. If the "
             "arm's full-participation total is not in the top 5% tail, being in the market -- not "
             "the structure -- is the source. Run only for widths net-positive overall at f50.\n")
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
        L.append("_No width net-positive at f50 -- no placebo needed._\n")

    L.append("## 7. VERDICT\n")
    L.append(f"**f50 total P&L by width:** " +
             ", ".join(f"{wt}=${f50_by_width[wt]:,.0f}" for wt in WIDTH_TAGS) + ".\n")
    L.append(f"**(a) Positive plateau across >=3 ADJACENT widths at f50?** "
             f"Longest adjacent-positive run = {best_run} width(s) -> "
             f"{'YES' if plateau_ok else 'NO'}.\n")
    L.append(f"**(b) OOS-stable (positive in the TEST half)?** f50 test-half by width: " +
             ", ".join(f"{wt}=${f50_test[wt]:,.0f}" for wt in WIDTH_TAGS) + ".\n")
    L.append(f"**(c) Survivable tail?** See Section 2 (worst-day / p01 / breach loss) and Section 3 "
             f"(per-year). A positive total that is a calm-carry-plus-catastrophe shape does NOT "
             f"count as survivable.\n")
    L.append(f"**(d) Beats matched random-day-out placebo?** Widths clearing the 5% bar: "
             f"{passed_placebo if passed_placebo else 'NONE'}.\n")
    L.append(f"**Widths clearing f50-positive AND OOS-positive AND placebo:** "
             f"{robust_widths if robust_widths else 'NONE'}.\n")
    L.append(f"\n> **VERDICT: {'PASS -- robust cash-settle plateau' if overall_pass else 'REFUTED'}.** "
             + ("A positive plateau of >=3 adjacent widths clears OOS and the placebo. The tail "
                "shape (Sections 2-3) must ALSO be judged survivable before any adoption, which "
                "still requires Andrew's explicit blessing."
                if overall_pass else
                "No positive plateau of >=3 adjacent widths that is also OOS-stable and beats the "
                "matched placebo. Removing the exit spread via pure cash settlement does not, on "
                "its own, rescue the 0DTE condor at a realistic (f50) entry fill. See the tail "
                "sections for whether any positive cell is merely calm-carry masking breach "
                "catastrophe.") + "\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(L), encoding="utf-8")
    return out_path


def run(verbose: bool = True, save: bool = True) -> dict:
    df = run_history(verbose=verbose, save=save)
    an = analyze(df, verbose=verbose)
    if save:
        arm1_csv = (Path(__file__).resolve().parent / "output" / "condor_width_sweep"
                    / "condor_width_sweep_days.csv")
        out = write_markdown_report(
            df, an, OUTPUT_DIR.parent / f"condor_cashsettle_hold_{_dt.date.today():%Y%m%d}.md",
            arm1_days_csv=arm1_csv)
        if verbose:
            print(f"\nReport written: {out}", flush=True)
    return {"days": df, "analysis": an}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="0DTE iron-condor PURE HOLD-TO-CASH-SETTLEMENT (ARM 5)")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="run only the first N days (smoke test)")
    ap.add_argument("--days", type=str, default="", help="comma-separated YYYY-MM-DD days to run")
    ap.add_argument("--history-only", action="store_true", help="run history, skip report")
    ap.add_argument("--resume", action="store_true", help="resume from the partial CSV (default)")
    ap.add_argument("--max-new-days", type=int, default=0,
                    help="process at most N not-yet-done days this run, then exit cleanly "
                         "(supervised chunk loop). 0 = no cap.")
    ap.add_argument("--report-only", action="store_true",
                    help="load the finished days CSV and (re)build the dated markdown report")
    args = ap.parse_args()
    if args.report_only:
        _df = pd.read_csv(OUTPUT_DIR / "condor_cashsettle_hold_days.csv")
        _df["traded"] = _df["traded"].astype(str).str.lower().isin(["true", "1"])
        _df["day"] = pd.to_datetime(_df["day"]).dt.date
        _an = analyze(_df, verbose=not args.quiet)
        _arm1 = (Path(__file__).resolve().parent / "output" / "condor_width_sweep"
                 / "condor_width_sweep_days.csv")
        _out = write_markdown_report(
            _df, _an, OUTPUT_DIR.parent / f"condor_cashsettle_hold_{_dt.date.today():%Y%m%d}.md",
            arm1_days_csv=_arm1)
        print(f"Report written: {_out}", flush=True)
    elif args.days:
        _days = [_dt.datetime.strptime(s.strip(), "%Y-%m-%d").date()
                 for s in args.days.split(",") if s.strip()]
        run_history(days=_days, verbose=not args.quiet, save=not args.no_save)
    elif args.history_only or args.limit or args.resume or args.max_new_days:
        days = s5.available_days()
        if args.limit:
            days = days[: args.limit]
        run_history(days=days, verbose=not args.quiet, save=not args.no_save,
                    max_new_days=args.max_new_days)
    else:
        run(verbose=not args.quiet, save=not args.no_save)
