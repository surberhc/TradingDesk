r"""
s5_harvest_engine.py -- the S5 "real harvest engine": MEASURE the achievable calm-day
0DTE SPXW premium harvest with HONEST fills, then feed it into the S5 financing ledger.

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the warehouse.
ASCII-only console output (Windows cp1252). numpy/pandas + reused s6 recon/data plumbing.

================================================================================
WHAT THIS ANSWERS (the ONE remaining open S5 unknown -- see docs/S5_SPEC.md sec 1.2)
================================================================================
S5's DEFENSIVE half (the always-on uncapped tail) is validated. The OFFENSIVE half --
"can calm-day 0DTE SPXW selling, with HONEST fills, net-generate the cash to finance the
tail carry (~4.46%/yr full-notional deep tail)?" -- is the make-or-break question. This
module measures it, then runs the financing ledger.

================================================================================
WHY THIS IS *NOT* A RE-RUN OF THE REFUTED S6-FINANCING EXPERIMENT
================================================================================
The prior S6 work (s6_control / s6_matrix / s6_strike_experiment / s6_s5_financing;
memory s6-spx-cashflow-0dte) tested Brandon's CHASSIS: a defined-risk credit spread with
a 2x-CREDIT STOP-LOSS, intraday-managed. That stop is exactly what manufactures the
negative skew (loss/win ~2.7x) that sank it, and the matrix's "hold-to-settle" cell was
framed as an exit-tuning variant of that chassis.

THIS engine measures the rule the S5 SPEC actually implies for a financing leg:
  * a FIXED short-leg delta (documented constant, NOT swept),
  * entered ~14:00 ET at HONEST fills (SELL at BID, BUY at ASK -- never mid),
  * DEFINED-RISK (5-wide spread; the long wing caps each day's loss),
  * HELD FLAT TO 16:00 PM SETTLEMENT -- NO 2x stop. In S5 the tail IS the catastrophe
    backstop; the financing leg does not need (and should not pay for) an intraday stop.
  * plus a per-contract commission on every leg, entry AND settlement.

So the exit regime is DIFFERENT from every prior S6 arm. Measuring it cleanly is the
honest, non-redundant deliverable -- including "it still does not self-fund" if true.

================================================================================
THE FROZEN, PRE-REGISTERED RULE (rule #1: never curve-fit)
================================================================================
Declared once as module constants, never swept:
  ENTRY_TIME          14:00 ET      (documented S6 entry; reused verbatim)
  SETTLEMENT_TIME     16:00 ET      (PM settlement instant)
  SHORT_DELTA         0.15          (the documented fixed short-strike delta)
  SPREAD_WIDTH        5.0 points    (defined-risk wing)
  STRUCTURE           iron_condor   (two-sided; the natural calm-day premium harvest)
  COMMISSION_PER_LEG  $0.65/contract (a standard retail SPX commission; a stated cost,
                                      applied to entry legs AND any settlement close)
  NO STOP. HOLD TO SETTLEMENT. Flat by close (0DTE cannot gap overnight).

We do NOT choose the delta to make it work; 0.15 is the value every prior S6 arm used.
We do NOT gate on a min-credit floor for the primary measurement (that would silently
discard real observed trades); we REPORT the credit distribution as observed.

CALM-DAY DEFINITION (pre-specified, observable, NOT tuned to returns): a day is "calm"
by the S5 regime gate iff prior-close VIX <= CALM_VIX. We ALSO report the harvest on ALL
days and split loss clustering by day-type, because the financing question is: on the
days S5 would actually SELL, does the net credit distribution cover the tail carry?

SETTLEMENT P&L (honest, no look-ahead): the 0DTE structure is held to 16:00 and settles
at intrinsic against the recovered PM settlement spot. Loss on any leg is capped by the
5-wide wing (defined risk). We compute settlement intrinsic from the recovered spot (the
same put-call-parity spot recon the whole s6 stack validated to ~2.3pt). The realized
per-day net credit = entry_credit - settlement_intrinsic - commissions.

================================================================================
RELIABILITY: resumable-from-partial (per-day CSV append + resume-skip), heartbeat each
day flushed, per-day try/except so one bad day cannot abort the run. ASCII-only.
================================================================================
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

# --------------------------------------------------------------------------- #
# FROZEN pre-registered constants (see header). NOT swept.
# --------------------------------------------------------------------------- #
ENTRY_TIME = _dt.time(14, 0)
SETTLEMENT_TIME = _dt.time(16, 0)
SHORT_DELTA = 0.15
SPREAD_WIDTH = 5.0
CONTRACT_MULTIPLIER = 100.0
N_CONTRACTS = 1
COMMISSION_PER_LEG = 0.65          # $/contract/leg; standard retail SPX; a STATED cost.

# Calm-day gate (the S5 regime spine, prior-close VIX). Pre-specified, not tuned to P&L.
CALM_VIX = 15.0                    # matches s5_convexity_overlay HARVEST_CALM_VIX
CALM_VIX_LOOSE = 20.0             # a second, looser calm cut -- REPORTED, not chosen.

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
RESEARCH_DIR = OUTPUT_DIR / "s5_research"
_PARTIAL_CSV = RESEARCH_DIR / "s5_harvest_trades_partial.csv"

# VIX series on disk (daily) for the calm gate. Reuse the same bt_data path s5 uses.
BT_DATA = Path(r"C:\TradingDesk-Local\bt_data")


# --------------------------------------------------------------------------- #
# Trade record -- one per day (iron condor: one two-sided structure per day).
# --------------------------------------------------------------------------- #
@dataclass
class HarvestDay:
    day: _dt.date
    traded: bool = False
    skip_reason: str = ""
    spot_entry: float = float("nan")
    spot_settle: float = float("nan")
    # put side
    p_short_k: float = float("nan")
    p_long_k: float = float("nan")
    p_short_delta: float = float("nan")
    p_credit: float = float("nan")
    # call side
    c_short_k: float = float("nan")
    c_long_k: float = float("nan")
    c_short_delta: float = float("nan")
    c_credit: float = float("nan")
    entry_credit: float = float("nan")     # total condor credit (points)
    settle_intrinsic: float = float("nan")  # cost to settle (points, >=0)
    commission: float = float("nan")        # $ total commissions (entry + settle legs)
    pnl_points: float = float("nan")        # entry_credit - settle_intrinsic (points)
    pnl_dollars: float = float("nan")       # net of commission, $
    breached_put: bool = False
    breached_call: bool = False
    prior_vix: float = float("nan")
    calm: bool = False
    calm_loose: bool = False


# --------------------------------------------------------------------------- #
# Honest fills (reuse the s6_control discipline: sell bid / buy ask, no mid).
# --------------------------------------------------------------------------- #
def _snap_at(nbbo: pd.DataFrame, minute: pd.Timestamp) -> pd.DataFrame:
    return nbbo[nbbo["minute"] == minute][["strike", "right", "bid", "ask"]].copy()


def _leg_quote(snap: pd.DataFrame, strike: float, right: str):
    row = snap[(snap["strike"] == strike) & (snap["right"] == right)]
    if row.empty:
        return None
    b, a = float(row["bid"].iloc[0]), float(row["ask"].iloc[0])
    if not (np.isfinite(b) and np.isfinite(a)):
        return None
    return b, a


def _pick_short_by_delta(delta_tbl: pd.DataFrame, right: str, target_abs_delta: float):
    side = delta_tbl[(delta_tbl["right"] == right) & (delta_tbl["delta"].notna())].copy()
    if side.empty:
        return None
    side["d_err"] = (side["delta"].abs() - target_abs_delta).abs()
    return float(side.sort_values("d_err").iloc[0]["strike"])


def _build_condor(snap, delta_tbl, target_delta):
    """Iron condor at fixed short delta on both sides. HONEST fills:
    each short leg SOLD at BID, each long wing BOUGHT at ASK. Returns None if any
    leg is unquotable at entry."""
    p_short = _pick_short_by_delta(delta_tbl, "PUT", target_delta)
    c_short = _pick_short_by_delta(delta_tbl, "CALL", target_delta)
    if p_short is None or c_short is None:
        return None
    p_long = p_short - SPREAD_WIDTH
    c_long = c_short + SPREAD_WIDTH
    pq_s = _leg_quote(snap, p_short, "PUT"); pq_l = _leg_quote(snap, p_long, "PUT")
    cq_s = _leg_quote(snap, c_short, "CALL"); cq_l = _leg_quote(snap, c_long, "CALL")
    if pq_s is None or pq_l is None or cq_s is None or cq_l is None:
        return None
    p_credit = pq_s[0] - pq_l[1]     # sell short bid - buy long ask
    c_credit = cq_s[0] - cq_l[1]

    def _dof(k, r):
        row = delta_tbl[(delta_tbl["strike"] == k) & (delta_tbl["right"] == r)]
        return float(row["delta"].iloc[0]) if not row.empty else float("nan")

    return {
        "p_short_k": p_short, "p_long_k": p_long, "p_credit": p_credit,
        "c_short_k": c_short, "c_long_k": c_long, "c_credit": c_credit,
        "p_short_delta": _dof(p_short, "PUT"), "c_short_delta": _dof(c_short, "CALL"),
        "entry_credit": p_credit + c_credit,
    }


def _settlement_intrinsic(spot_settle: float, b: dict) -> float:
    """Cost (points, >=0) to settle the 0DTE condor at PM settlement against `spot_settle`.

    Cash-settled European: each spread settles at its intrinsic, capped by the 5-wide wing.
    Put spread loss  = clip(p_short_k - spot, 0, width).
    Call spread loss = clip(spot - c_short_k, 0, width).
    Total settlement debit = put_loss + call_loss (what we PAY to close at expiry).
    """
    put_loss = min(max(b["p_short_k"] - spot_settle, 0.0), SPREAD_WIDTH)
    call_loss = min(max(spot_settle - b["c_short_k"], 0.0), SPREAD_WIDTH)
    return put_loss + call_loss


# --------------------------------------------------------------------------- #
# Recover the PM-settlement spot at 16:00 from the 0DTE chain (put-call parity).
# Falls back to the last quoted minute before 16:00 if 16:00 is unquoted.
# --------------------------------------------------------------------------- #
def _recover_settle_spot(nbbo: pd.DataFrame, d: _dt.date):
    settle_minute = pd.Timestamp(_dt.datetime.combine(d, SETTLEMENT_TIME))
    minutes = sorted(m for m in nbbo["minute"].unique() if m <= settle_minute)
    for m in reversed(minutes):
        snap = _snap_at(nbbo, m)
        sr = recon.recover_forward_spot(snap, m, d)
        if sr is not None:
            return sr.spot, m
    return None, None


# --------------------------------------------------------------------------- #
# One day
# --------------------------------------------------------------------------- #
def run_day(d: _dt.date, vix_by_day: dict, day_data=None) -> HarvestDay:
    tr = HarvestDay(day=d)
    try:
        tr.prior_vix = float(vix_by_day.get(d, float("nan")))
        tr.calm = np.isfinite(tr.prior_vix) and tr.prior_vix <= CALM_VIX
        tr.calm_loose = np.isfinite(tr.prior_vix) and tr.prior_vix <= CALM_VIX_LOOSE

        dd = day_data if day_data is not None else s5.load_day(d)
        chain = s5.zero_dte_chain(d, day_data=dd)
        nbbo = chain.nbbo
        if nbbo.empty:
            tr.skip_reason = "no 0dte chain"
            return tr

        entry_minute = pd.Timestamp(_dt.datetime.combine(d, ENTRY_TIME))
        if entry_minute not in set(nbbo["minute"].unique()):
            tr.skip_reason = "no 14:00 snapshot"
            return tr

        entry_snap = _snap_at(nbbo, entry_minute)
        sr = recon.recover_forward_spot(entry_snap, entry_minute, d)
        if sr is None:
            tr.skip_reason = "spot recon failed at entry"
            return tr
        tr.spot_entry = sr.spot
        delta_tbl = recon.per_strike_delta(entry_snap, entry_minute, d, sr.spot)

        b = _build_condor(entry_snap, delta_tbl, SHORT_DELTA)
        if b is None:
            tr.skip_reason = "could not build condor at entry"
            return tr

        tr.p_short_k = b["p_short_k"]; tr.p_long_k = b["p_long_k"]
        tr.c_short_k = b["c_short_k"]; tr.c_long_k = b["c_long_k"]
        tr.p_short_delta = b["p_short_delta"]; tr.c_short_delta = b["c_short_delta"]
        tr.p_credit = b["p_credit"]; tr.c_credit = b["c_credit"]
        tr.entry_credit = b["entry_credit"]

        # Settlement spot (recovered at/nearest 16:00).
        spot_settle, _ = _recover_settle_spot(nbbo, d)
        if spot_settle is None or not np.isfinite(spot_settle):
            tr.skip_reason = "settle spot recon failed"
            return tr
        tr.spot_settle = spot_settle

        settle_debit = _settlement_intrinsic(spot_settle, b)
        tr.settle_intrinsic = settle_debit
        tr.breached_put = spot_settle < b["p_short_k"]
        tr.breached_call = spot_settle > b["c_short_k"]

        # Commissions: 4 legs at entry. At settlement, cash-settled legs that expire ITM
        # are exercised (no closing trade); OTM legs expire worthless (no trade). We charge
        # entry on all 4 legs; at settlement we charge only legs that are ITM (settled).
        entry_legs = 4
        settle_legs = 0
        if tr.breached_put:
            settle_legs += 2  # both put legs cash-settle
        if tr.breached_call:
            settle_legs += 2
        commission = (entry_legs + settle_legs) * COMMISSION_PER_LEG * N_CONTRACTS
        tr.commission = commission

        tr.pnl_points = b["entry_credit"] - settle_debit
        tr.pnl_dollars = tr.pnl_points * CONTRACT_MULTIPLIER * N_CONTRACTS - commission
        tr.traded = True
        return tr
    except Exception as e:
        tr.skip_reason = f"error: {type(e).__name__}: {e}"
        return tr


# --------------------------------------------------------------------------- #
# VIX loader (prior-close, causal)
# --------------------------------------------------------------------------- #
def load_prior_vix() -> dict:
    """Map trade-date -> PRIOR trading day's VIX close (causal calm gate).

    Reads _vix.parquet from bt_data. Each day's gate uses the PRIOR close so it is known
    before the 14:00 entry. Returns {} silently if the file is missing (calm flags NaN).
    """
    p = BT_DATA / "_vix.parquet"
    if not p.is_file():
        return {}
    df = pd.read_parquet(p)
    s = df.iloc[:, 0]
    s.index = pd.to_datetime(s.index).normalize()
    s = s.sort_index()
    prior = s.shift(1)   # prior-close VIX, causal
    return {d.date(): float(v) for d, v in prior.items() if np.isfinite(v)}


# --------------------------------------------------------------------------- #
# Full-history run -- resumable, heartbeat, ASCII.
# --------------------------------------------------------------------------- #
def run_history(days=None, resume=True, verbose=True) -> pd.DataFrame:
    if days is None:
        days = s5.available_days()
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    vix_by_day = load_prior_vix()
    if verbose:
        print(f"[init] {len(days)} days, VIX gate covers {len(vix_by_day)} dates",
              flush=True)

    done: set = set()
    if resume and _PARTIAL_CSV.is_file():
        try:
            done = set(pd.read_csv(_PARTIAL_CSV, usecols=["day"])["day"].astype(str))
        except Exception:
            done = set()
    if verbose and done:
        print(f"[resume] {len(done)} days already done; skipping", flush=True)

    fieldnames = list(asdict(HarvestDay(day=days[0])).keys())
    write_header = not _PARTIAL_CSV.is_file()
    n = len(days)
    with open(_PARTIAL_CSV, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        traded_ct = 0
        for i, d in enumerate(days, 1):
            if str(d) in done:
                continue
            try:
                dd = s5.load_day(d)
            except Exception as e:
                if verbose:
                    print(f"[{i}/{n}] {d} LOAD-SKIP {type(e).__name__}", flush=True)
                continue
            tr = run_day(d, vix_by_day, day_data=dd)
            if tr.traded:
                traded_ct += 1
            writer.writerow(asdict(tr))
            fh.flush()
            if verbose and (i % 25 == 0 or i == n):
                print(f"[{i}/{n}] {d} done  traded_this_run={traded_ct}", flush=True)

    df = pd.read_csv(_PARTIAL_CSV)
    for bcol in ("traded", "calm", "calm_loose", "breached_put", "breached_call"):
        df[bcol] = df[bcol].astype(str).str.lower().isin(["true", "1"])
    df.to_csv(RESEARCH_DIR / "s5_harvest_trades.csv", index=False)
    if verbose:
        print(f"[done] wrote {RESEARCH_DIR / 's5_harvest_trades.csv'} ({len(df)} rows)",
              flush=True)
    return df


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description="S5 real harvest engine (measure calm-day 0DTE)")
    ap.add_argument("--no-resume", action="store_true", help="ignore the partial CSV")
    ap.add_argument("--limit", type=int, default=0, help="run only first N days (smoke test)")
    args = ap.parse_args()
    days = s5.available_days()
    if args.limit > 0:
        days = days[: args.limit]
    run_history(days=days, resume=not args.no_resume)
