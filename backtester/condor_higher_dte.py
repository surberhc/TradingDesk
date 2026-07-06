"""
condor_higher_dte.py — ARM 4 of the condor-reopen pre-registration
(docs/PREREG_condor_reopen_2026-07-06.md).

THESIS UNDER TEST
-----------------
The 0DTE iron condor was refuted four ways; the honest finding across every
refutation was that the binding constraint is TRANSACTION COST on thin 0DTE
premium, not the strategy logic. The evidence that "iron condors work" almost
always comes from WEEKLY/MONTHLY DTE, where the credit is THICK relative to the
4-leg bid/ask cost. Arm 4 tests that regime directly: a properly MANAGED 30- and
45-DTE iron condor on the ThetaData EOD warehouse chains.

WHY THIS DIFFERS FROM THE NAIVE 45-DTE BENCHMARK
------------------------------------------------
The prior "naive" 45-DTE benchmark (output/condor_management_20260703.md sec 7:
89 trades, 37% win rate, -$53k) already carried the 50%-profit / 21-DTE
management rule, but it had TWO holes that this harness closes:
  1. NO DISASTER STOP. A 16-delta condor breaches ~30% of the time; without a
     stop, the ~1-in-3 losers each ran toward near-max-loss (width - credit),
     and a fistful of full-width losses at ~$4k each buried 60+ small winners.
     Textbook management pairs the profit-take with a 2x-credit disaster stop.
  2. It marked / entered on the SPX root (25-pt strikes at these levels), which
     coarsens both strike selection and the honest exit debit. This harness runs
     SPXW (5-pt strikes) as primary, with SPX available via --symbol for a
     cross-instrument plateau check.
This harness therefore is the PROPER managed test the pre-registration calls for,
and it reports the naive benchmark alongside so the difference is explicit.

FROZEN GRID (pre-registered 2026-07-06 — NOT swept to a winner)
---------------------------------------------------------------
  * DTE in {30, 45}                          (the two cells; plateau = both agree)
  * short strike |delta| = 0.16              (matches s3_condor_control's convention)
  * wing width = 50 index points             (s3_condor_control's frozen wing)
  * MANAGEMENT (textbook):
      - take profit at 50% of ENTRY CREDIT captured, OR
      - time-exit at 21 DTE, whichever first, OR
      - DISASTER STOP at 2x entry credit of open loss, OR
      - expiry (cash-settled intrinsic) if none fired.
  * ENTRY CADENCE: one book, no overlap. Re-enter on the first EOD after a close.
    (Pre-registered choice: single-book, mirrors the control + naive benchmark so
    the comparison is a clean one-position series.)

HONEST FILLS
------------
  * ENTRY credit (worst-side): SELL shorts at BID, BUY wings at ASK.
  * EXIT debit-to-close (worst-side): BUY shorts at ASK, SELL wings at BID.
  * A fill BAND is also reported: worst-side (headline) and mid (optimistic
    ceiling). No modeled slippage discount; the headline is the honest worst side.
  * Commission: per-leg per-contract, charged on 4 entry legs + 4 exit legs.

NO LOOK-AHEAD
-------------
  * Strikes + entry credit come from the ENTRY-day EOD chain only.
  * Every management decision on day T uses ONLY day-T's EOD chain (the open
    profit is marked at T's honest debit-to-close). Nothing reads a future day.
  * Settlement uses the expiry-day underlying close (that day's own file).

DATA (READ-ONLY warehouse):
  C:/TradingDesk-Local/warehouse/raw/options/{SYMBOL}/{YYYYMMDD}.parquet
  Each file is an EOD snapshot: one last-quote row per strike/expiry/right.
  Usable range: 2018-01 .. 2026-07 for both SPXW and SPX.

Run (offline; no gateway; no network):
  C:/TradingDesk-Local/venv/Scripts/python.exe backtester/condor_higher_dte.py
    --symbol SPXW           SPXW (default) | SPX
    --dtes 30,45            comma list of target DTEs to run
    --oos 20240630          OOS split (train <= date < test)
    --out output/condor_higher_dte_20260706.md
    --placebo               also run the matched random-exit placebo per DTE
    --resume                reuse cached per-DTE trade CSVs if present
"""
from __future__ import annotations
import argparse
import datetime as _dt
import glob
import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

WAREHOUSE = "C:/TradingDesk-Local/warehouse/raw/options"
MULTIPLIER = 100.0
N_CONTRACTS = 1                 # one-lot book (clean single-book series like the control)

# ---- FROZEN pre-registered constants (rule #1: do NOT sweep) -----------------
SHORT_DELTA = 0.16             # short-leg |delta| — matches s3_condor_control
WING_WIDTH = 50.0              # index points — matches s3_condor_control frozen wing
PROFIT_TAKE = 0.50            # close at 50% of entry credit captured
EXIT_DTE = 21                # or at <= 21 DTE
DISASTER_MULT = 2.0          # disaster stop at 2x entry credit of open loss
MIN_DTE_FLOOR = 25           # only consider expiries >= this when picking entry
MIN_CREDIT = 0.50            # no-trade floor on entry credit (pts)
COMMISSION = 0.65            # $ per leg per contract
# ------------------------------------------------------------------------------

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


# =============================================================================
# Data access
# =============================================================================
_COLS = ["expiration", "strike", "right", "bid", "ask", "delta", "underlying_price"]


def session_days(symbol: str) -> list[_dt.date]:
    out = []
    for f in sorted(glob.glob(os.path.join(WAREHOUSE, symbol, "*.parquet"))):
        stem = os.path.splitext(os.path.basename(f))[0]
        if stem.isdigit():
            try:
                out.append(_dt.datetime.strptime(stem, "%Y%m%d").date())
            except ValueError:
                pass
    return out


def load_chain(symbol: str, d: _dt.date) -> pd.DataFrame | None:
    """One EOD chain, or None if missing/empty/placeholder (holidays lack schema)."""
    p = os.path.join(WAREHOUSE, symbol, f"{d.strftime('%Y%m%d')}.parquet")
    if not os.path.isfile(p):
        return None
    try:
        import pyarrow.parquet as pq
        names = set(pq.ParquetFile(p).schema.names)
    except Exception:
        return None
    if not {"expiration", "strike", "right"}.issubset(names):
        return None
    try:
        df = pd.read_parquet(p, columns=_COLS)
    except Exception:
        return None
    if df.empty:
        return None
    df = df[(df["bid"] > 0) & (df["ask"] > 0)].copy()
    if df.empty:
        return None
    df["exp_date"] = pd.to_datetime(df["expiration"]).dt.date
    return df


# =============================================================================
# Strike selection + leg quotes (honest, worst-side)
# =============================================================================
def pick_short_strike(sub: pd.DataFrame, right: str, target_abs_delta: float) -> float | None:
    side = sub[(sub["right"] == right) & sub["delta"].notna()].copy()
    if side.empty:
        return None
    side["d_err"] = (side["delta"].abs() - target_abs_delta).abs()
    return float(side.sort_values(["d_err", "strike"]).iloc[0]["strike"])


def leg_quote(sub: pd.DataFrame, strike: float, right: str) -> tuple[float, float] | None:
    row = sub[(np.isclose(sub["strike"], strike)) & (sub["right"] == right)]
    if row.empty:
        return None
    b, a = float(row["bid"].iloc[0]), float(row["ask"].iloc[0])
    if not (np.isfinite(b) and np.isfinite(a) and a > 0):
        return None
    return b, a


# A leg is (strike, right, sign): sign=+1 => SHORT (we sold), sign=-1 => LONG wing.
def entry_credit(sub: pd.DataFrame, legs, fill: str) -> float | None:
    """Credit collected at entry. worst-side: SELL shorts@BID, BUY wings@ASK."""
    total = 0.0
    for strike, right, sign in legs:
        q = leg_quote(sub, strike, right)
        if q is None:
            return None
        b, a = q
        mid = (a + b) / 2.0
        if fill == "mid":
            px = mid
        else:  # worst
            px = b if sign > 0 else a   # short sells at bid; wing bought at ask
        total += px if sign > 0 else -px
    return total


def exit_debit(sub: pd.DataFrame, legs, fill: str) -> float | None:
    """Cost to CLOSE. worst-side: BUY shorts@ASK, SELL wings@BID."""
    total = 0.0
    for strike, right, sign in legs:
        q = leg_quote(sub, strike, right)
        if q is None:
            return None
        b, a = q
        mid = (a + b) / 2.0
        if fill == "mid":
            px = mid
        else:  # worst
            px = a if sign > 0 else b   # buy back short at ask; sell wing at bid
        total += px if sign > 0 else -px
    return total


def intrinsic_settle(legs, S: float) -> float:
    """Debit-to-settle at expiry from 4-leg intrinsic (what we owe, >= 0)."""
    total = 0.0
    for strike, right, sign in legs:
        if right == "PUT":
            iv = max(strike - S, 0.0)
        else:
            iv = max(S - strike, 0.0)
        # short (sign>0) we OWE its intrinsic; long wing (sign<0) we RECEIVE it.
        total += iv if sign > 0 else -iv
    return total


# =============================================================================
# Backtest — one DTE, single book, textbook management
# =============================================================================
@dataclass
class Trade:
    dte_target: int
    entry_day: _dt.date
    exit_day: _dt.date
    expiration: _dt.date
    entry_dte: int
    hold_days: int
    exit_reason: str
    credit_worst: float
    credit_mid: float
    exit_worst: float
    exit_mid: float
    pnl_worst: float           # $ per book, headline
    pnl_mid: float             # $ per book, optimistic ceiling
    wing_width: float
    breached: bool = False


def run_dte(symbol: str, target_dte: int, comm: float = COMMISSION) -> list[Trade]:
    """One target-DTE book. No look-ahead: each day's decisions use only that day's chain."""
    days = session_days(symbol)
    n = len(days)
    comm_pts = (comm * 8.0) / MULTIPLIER   # 4 legs in + 4 legs out
    trades: list[Trade] = []
    pos = None  # dict(entry_day, expiration, legs, credit_worst, credit_mid, entry_dte)

    for i, d in enumerate(days):
        chain = load_chain(symbol, d)
        if chain is None:
            continue

        # ---- manage an open position using THIS day's chain only ----
        if pos is not None:
            exp = pos["expiration"]
            dte = (exp - d).days
            sub = chain[chain["exp_date"] == exp]
            closed = False
            if not sub.empty and dte > 0:
                dw = exit_debit(sub, pos["legs"], "worst")
                dm = exit_debit(sub, pos["legs"], "mid")
                if dw is not None and dm is not None:
                    open_profit_w = pos["credit_worst"] - dw
                    take = open_profit_w >= PROFIT_TAKE * pos["credit_worst"]
                    disaster = open_profit_w <= -DISASTER_MULT * pos["credit_worst"]
                    time_out = dte <= EXIT_DTE
                    if take or disaster or time_out:
                        reason = ("take" if take else
                                  "stop" if disaster else "dte21")
                        _book(trades, pos, d, exp, dw, dm, comm_pts, reason,
                              breached=False)
                        pos = None
                        closed = True
            if not closed and pos is not None and dte <= 0:
                # settle on expiry-day underlying close (that day's own file)
                S = float(chain["underlying_price"].iloc[0])
                pay_w = intrinsic_settle(pos["legs"], S)   # worst==mid at settle
                breached = pay_w > 1e-9
                _book(trades, pos, d, exp, pay_w, pay_w, comm_pts, "expiry",
                      breached=breached)
                pos = None

        # ---- enter a fresh condor when flat, from THIS day's chain ----
        if pos is None:
            chain2 = chain.copy()
            chain2["dte"] = chain2["exp_date"].map(lambda e: (e - d).days)
            fwd = chain2[chain2["dte"] >= MIN_DTE_FLOOR]
            if not fwd.empty:
                tgt_exp = fwd.iloc[(fwd["dte"] - target_dte).abs().argsort()].iloc[0]["exp_date"]
                sub = chain2[chain2["exp_date"] == tgt_exp]
                spk = pick_short_strike(sub, "PUT", SHORT_DELTA)
                sck = pick_short_strike(sub, "CALL", SHORT_DELTA)
                if spk is not None and sck is not None:
                    legs = [(spk, "PUT", +1), (spk - WING_WIDTH, "PUT", -1),
                            (sck, "CALL", +1), (sck + WING_WIDTH, "CALL", -1)]
                    cw = entry_credit(sub, legs, "worst")
                    cm = entry_credit(sub, legs, "mid")
                    if cw is not None and cm is not None and cw >= MIN_CREDIT:
                        pos = {"entry_day": d, "expiration": tgt_exp, "legs": legs,
                               "credit_worst": cw, "credit_mid": cm,
                               "entry_dte": int((tgt_exp - d).days),
                               "dte_target": target_dte}
    return trades


def _book(trades, pos, exit_day, exp, exit_w, exit_m, comm_pts, reason, breached):
    """Record a closed trade. exit_* are debits paid to close (>=0 typically)."""
    pnl_w_pts = pos["credit_worst"] - exit_w - comm_pts
    pnl_m_pts = pos["credit_mid"] - exit_m - comm_pts
    trades.append(Trade(
        dte_target=pos["dte_target"],
        entry_day=pos["entry_day"], exit_day=exit_day, expiration=exp,
        entry_dte=pos["entry_dte"],
        hold_days=(exit_day - pos["entry_day"]).days,
        exit_reason=reason,
        credit_worst=pos["credit_worst"], credit_mid=pos["credit_mid"],
        exit_worst=exit_w, exit_mid=exit_m,
        pnl_worst=pnl_w_pts * MULTIPLIER * N_CONTRACTS,
        pnl_mid=pnl_m_pts * MULTIPLIER * N_CONTRACTS,
        wing_width=WING_WIDTH, breached=breached,
    ))


# =============================================================================
# VIX regime tag (contango / backwardation) via VIX vs VIX3M warehouse-adjacent
# =============================================================================
VIX_DIR = "C:/TradingDesk-Local/bt_data"


def load_vix_regime() -> pd.DataFrame | None:
    """Return a per-date frame with vix_regime in {contango,backwardation}.

    VIX > VIX3M => backwardation (stress); else contango (calm). Reads the local
    bt_data parquets (DatetimeIndex named 'date', single value column). Returns
    None if either series is missing so the breakout degrades to 'unknown'.
    """
    pv = os.path.join(VIX_DIR, "_vix.parquet")
    p3 = os.path.join(VIX_DIR, "_vix3m.parquet")
    if not (os.path.isfile(pv) and os.path.isfile(p3)):
        return None
    try:
        vix = pd.read_parquet(pv)["vix"]
        vix3m = pd.read_parquet(p3)["vix3m"]
    except Exception:
        return None
    df = pd.DataFrame({"vix": vix, "vix3m": vix3m}).dropna()
    if df.empty:
        return None
    df["vix_regime"] = np.where(df["vix"] > df["vix3m"], "backwardation", "contango")
    df.index = pd.to_datetime(df.index).date
    return df[["vix_regime"]]


# =============================================================================
# Summaries
# =============================================================================
def _stats(pnl: np.ndarray) -> dict:
    if len(pnl) == 0:
        return dict(trades=0, total=0.0, win_rate=float("nan"), avg=float("nan"),
                    worst=float("nan"), best=float("nan"), std=float("nan"),
                    sharpe=float("nan"))
    wins = pnl[pnl > 0]
    return dict(
        trades=len(pnl), total=float(pnl.sum()),
        win_rate=len(wins) / len(pnl), avg=float(pnl.mean()),
        worst=float(pnl.min()), best=float(pnl.max()),
        std=float(pnl.std(ddof=1)) if len(pnl) > 1 else float("nan"),
        sharpe=(float(pnl.mean() / pnl.std(ddof=1)) if len(pnl) > 1 and pnl.std(ddof=1) > 0 else float("nan")),
    )


def trades_frame(trades: list[Trade]) -> pd.DataFrame:
    df = pd.DataFrame([t.__dict__ for t in trades])
    if df.empty:
        return df
    df["entry_day"] = pd.to_datetime(df["entry_day"])
    df["exit_day"] = pd.to_datetime(df["exit_day"])
    df["year"] = df["exit_day"].dt.year
    df["credit_pct_width"] = df["credit_worst"] / df["wing_width"]
    return df


# =============================================================================
# Matched random-exit PLACEBO
# =============================================================================
def placebo_random_exit(symbol: str, target_dte: int, real_trades: list[Trade],
                        n_iter: int = 300, seed: int = 7) -> dict:
    """For each real trade (same entry, same expiry, same legs), pick a RANDOM exit
    day within the real hold window and mark the honest worst-side debit there.
    Confirms the MANAGEMENT LOGIC (targeted exits), not merely 'being in the market',
    is the source of any edge. Matched on trade count + hold-window per trade."""
    rng = np.random.default_rng(seed)
    days = session_days(symbol)
    day_idx = {d: k for k, d in enumerate(days)}
    comm_pts = (COMMISSION * 8.0) / MULTIPLIER

    # Pre-load only the chains we may touch (entry..exit spans), cache per day.
    cache: dict[_dt.date, pd.DataFrame | None] = {}

    def chain_for(d):
        if d not in cache:
            cache[d] = load_chain(symbol, d)
        return cache[d]

    totals = []
    for _ in range(n_iter):
        tot = 0.0
        for t in real_trades:
            e_i = day_idx.get(t.entry_day.date() if hasattr(t.entry_day, "date") else t.entry_day)
            x_i = day_idx.get(t.exit_day.date() if hasattr(t.exit_day, "date") else t.exit_day)
            if e_i is None or x_i is None or x_i <= e_i:
                tot += t.pnl_worst  # degenerate (same-day); keep real
                continue
            legs = [(t.__dict__.get("_legs"))]  # placeholder; recomputed below
            # We didn't store legs on Trade; reconstruct from entry chain.
            rj = int(rng.integers(e_i + 1, x_i + 1))  # random exit strictly after entry, up to real exit
            rd = days[rj]
            ch = chain_for(rd)
            legs = _reconstruct_legs(symbol, t)
            if ch is None or legs is None:
                tot += t.pnl_worst
                continue
            exp = t.expiration.date() if hasattr(t.expiration, "date") else t.expiration
            sub = ch[ch["exp_date"] == exp]
            dte = (exp - rd).days
            if sub.empty or dte < 0:
                tot += t.pnl_worst
                continue
            if dte == 0:
                S = float(ch["underlying_price"].iloc[0])
                debit = intrinsic_settle(legs, S)
            else:
                debit = exit_debit(sub, legs, "worst")
                if debit is None:
                    tot += t.pnl_worst
                    continue
            tot += (t.credit_worst - debit - comm_pts) * MULTIPLIER * N_CONTRACTS
        totals.append(tot)
    totals = np.array(totals)
    real_total = sum(t.pnl_worst for t in real_trades)
    return dict(
        real_total=real_total,
        placebo_p50=float(np.percentile(totals, 50)),
        placebo_p95=float(np.percentile(totals, 95)),
        frac_placebo_ge_real=float((totals >= real_total).mean()),
        beats_placebo=bool((totals >= real_total).mean() < 0.05),
        n_iter=n_iter,
    )


_LEG_CACHE: dict = {}


def _reconstruct_legs(symbol: str, t: Trade):
    """Rebuild the 4 legs from the entry-day chain (deterministic — same selection)."""
    key = (symbol, t.entry_day, t.dte_target)
    if key in _LEG_CACHE:
        return _LEG_CACHE[key]
    ed = t.entry_day.date() if hasattr(t.entry_day, "date") else t.entry_day
    ch = load_chain(symbol, ed)
    if ch is None:
        _LEG_CACHE[key] = None
        return None
    exp = t.expiration.date() if hasattr(t.expiration, "date") else t.expiration
    sub = ch[ch["exp_date"] == exp]
    spk = pick_short_strike(sub, "PUT", SHORT_DELTA)
    sck = pick_short_strike(sub, "CALL", SHORT_DELTA)
    if spk is None or sck is None:
        _LEG_CACHE[key] = None
        return None
    legs = [(spk, "PUT", +1), (spk - WING_WIDTH, "PUT", -1),
            (sck, "CALL", +1), (sck + WING_WIDTH, "CALL", -1)]
    _LEG_CACHE[key] = legs
    return legs


# =============================================================================
# Naive 45-DTE benchmark reference (no disaster stop) — for the delta explanation
# =============================================================================
def run_naive(symbol: str, target_dte: int = 45) -> list[Trade]:
    """Same book but WITHOUT the disaster stop (mirrors the prior naive benchmark)."""
    days = session_days(symbol)
    comm_pts = (COMMISSION * 8.0) / MULTIPLIER
    trades: list[Trade] = []
    pos = None
    for d in days:
        chain = load_chain(symbol, d)
        if chain is None:
            continue
        if pos is not None:
            exp = pos["expiration"]; dte = (exp - d).days
            sub = chain[chain["exp_date"] == exp]
            closed = False
            if not sub.empty and dte > 0:
                dw = exit_debit(sub, pos["legs"], "worst")
                dm = exit_debit(sub, pos["legs"], "mid")
                if dw is not None and dm is not None:
                    op = pos["credit_worst"] - dw
                    take = op >= PROFIT_TAKE * pos["credit_worst"]
                    time_out = dte <= EXIT_DTE
                    if take or time_out:
                        _book(trades, pos, d, exp, dw, dm, comm_pts,
                              "take" if take else "dte21", breached=False)
                        pos = None; closed = True
            if not closed and pos is not None and dte <= 0:
                S = float(chain["underlying_price"].iloc[0])
                pay = intrinsic_settle(pos["legs"], S)
                _book(trades, pos, d, exp, pay, pay, comm_pts, "expiry", breached=pay > 1e-9)
                pos = None
        if pos is None:
            c2 = chain.copy(); c2["dte"] = c2["exp_date"].map(lambda e: (e - d).days)
            fwd = c2[c2["dte"] >= MIN_DTE_FLOOR]
            if not fwd.empty:
                te = fwd.iloc[(fwd["dte"] - target_dte).abs().argsort()].iloc[0]["exp_date"]
                sub = c2[c2["exp_date"] == te]
                spk = pick_short_strike(sub, "PUT", SHORT_DELTA)
                sck = pick_short_strike(sub, "CALL", SHORT_DELTA)
                if spk is not None and sck is not None:
                    legs = [(spk, "PUT", +1), (spk - WING_WIDTH, "PUT", -1),
                            (sck, "CALL", +1), (sck + WING_WIDTH, "CALL", -1)]
                    cw = entry_credit(sub, legs, "worst"); cm = entry_credit(sub, legs, "mid")
                    if cw is not None and cm is not None and cw >= MIN_CREDIT:
                        pos = {"entry_day": d, "expiration": te, "legs": legs,
                               "credit_worst": cw, "credit_mid": cm,
                               "entry_dte": int((te - d).days), "dte_target": target_dte}
    return trades


# =============================================================================
# Report
# =============================================================================
def build_report(symbol: str, oos: _dt.date, results: dict, naive: dict,
                 placebos: dict, vix_reg: pd.DataFrame | None) -> str:
    L = []
    A = L.append
    A(f"# ARM 4 — Higher-DTE managed iron condor (30 & 45 DTE)\n")
    A(f"_Generated 2026-07-06. Instrument **{symbol}** EOD warehouse chains. "
      f"PAPER / research only. Pre-registered in docs/PREREG_condor_reopen_2026-07-06.md._\n")
    A("\n## Setup (frozen, pre-registered)\n")
    A(f"- Short-leg |delta| **{SHORT_DELTA}**, wings **{WING_WIDTH:.0f}**-pt, "
      f"target DTE in **{sorted(results)}**.\n")
    A(f"- Management: take at **{int(PROFIT_TAKE*100)}%** of entry credit OR "
      f"**{EXIT_DTE}-DTE** OR **{DISASTER_MULT:.0f}x-credit** disaster stop, else expiry.\n")
    A(f"- Single-book, no overlap; re-enter first EOD after a close. Commission "
      f"${COMMISSION}/leg/contract, 8 legs round-trip.\n")
    A(f"- Honest fills: worst-side (**headline**) and mid (optimistic ceiling).\n")
    A(f"- OOS split at **{oos.isoformat()}** (train `entry < split`, test `>=`).\n")

    # headline table
    A("\n## Headline — total P&L (honest worst-side fill)\n")
    A("| DTE | trades | total $ | win% | avg $ | worst $ | avg hold (d) | credit/width | mid-fill total $ |\n")
    A("| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n")
    for dte in sorted(results):
        df = results[dte]
        s = _stats(df["pnl_worst"].values)
        sm = _stats(df["pnl_mid"].values)
        cw = df["credit_pct_width"].mean()
        A(f"| {dte} | {s['trades']} | {s['total']:,.0f} | {s['win_rate']*100:.1f} | "
          f"{s['avg']:,.0f} | {s['worst']:,.0f} | {df['hold_days'].mean():.1f} | "
          f"{cw*100:.1f}% | {sm['total']:,.0f} |\n")

    # per-year
    A("\n## Per-year total P&L (worst-side fill)\n")
    years = sorted(set().union(*[set(results[d]["year"]) for d in results]))
    A("| DTE | " + " | ".join(str(y) for y in years) + " |\n")
    A("| --- | " + " | ".join("---" for _ in years) + " |\n")
    for dte in sorted(results):
        df = results[dte]
        by = df.groupby("year")["pnl_worst"].sum()
        A(f"| {dte} | " + " | ".join(f"{by.get(y, 0):,.0f}" for y in years) + " |\n")

    # OOS split
    A("\n## OOS train/test (worst-side fill)\n")
    A("| DTE | train total $ | train win% | test total $ | test win% |\n")
    A("| --- | --- | --- | --- | --- |\n")
    for dte in sorted(results):
        df = results[dte]
        tr = df[df["entry_day"] < pd.Timestamp(oos)]
        te = df[df["entry_day"] >= pd.Timestamp(oos)]
        st, se = _stats(tr["pnl_worst"].values), _stats(te["pnl_worst"].values)
        A(f"| {dte} | {st['total']:,.0f} | {st['win_rate']*100:.1f} | "
          f"{se['total']:,.0f} | {se['win_rate']*100:.1f} |\n")

    # per-regime (VIX contango/backwardation)
    A("\n## Per-regime total P&L — VIX contango vs backwardation (worst-side)\n")
    if vix_reg is None:
        A("_VIX/VIX3M series not found in data dir — regime breakout skipped._\n")
    else:
        A("| DTE | contango $ | backwardation $ | unknown $ |\n")
        A("| --- | --- | --- | --- |\n")
        for dte in sorted(results):
            df = results[dte].copy()
            df["ed"] = df["entry_day"].dt.date
            df = df.merge(vix_reg, left_on="ed", right_index=True, how="left")
            df["vix_regime"] = df["vix_regime"].fillna("unknown")
            g = df.groupby("vix_regime")["pnl_worst"].sum()
            A(f"| {dte} | {g.get('contango', 0):,.0f} | "
              f"{g.get('backwardation', 0):,.0f} | {g.get('unknown', 0):,.0f} |\n")

    # exit-reason mix
    A("\n## Exit-reason mix (count) + P&L by reason (worst-side)\n")
    A("| DTE | take | dte21 | stop | expiry | take $ | dte21 $ | stop $ | expiry $ |\n")
    A("| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n")
    for dte in sorted(results):
        df = results[dte]
        cnt = df["exit_reason"].value_counts()
        pnl = df.groupby("exit_reason")["pnl_worst"].sum()
        A(f"| {dte} | " +
          " | ".join(str(int(cnt.get(r, 0))) for r in ("take", "dte21", "stop", "expiry")) +
          " | " +
          " | ".join(f"{pnl.get(r, 0):,.0f}" for r in ("take", "dte21", "stop", "expiry")) +
          " |\n")

    # honest-fill impact
    A("\n## Honest-fill impact (mid vs worst-side, total $)\n")
    A("| DTE | mid total $ | worst total $ | fill cost $ | fill cost as % of |mid| |\n")
    A("| --- | --- | --- | --- | --- |\n")
    for dte in sorted(results):
        df = results[dte]
        m = df["pnl_mid"].sum(); w = df["pnl_worst"].sum()
        pct = (m - w) / abs(m) * 100 if m != 0 else float("nan")
        A(f"| {dte} | {m:,.0f} | {w:,.0f} | {m-w:,.0f} | {pct:.1f}% |\n")

    # naive benchmark comparison
    A("\n## Vs the naive 45-DTE benchmark (no disaster stop)\n")
    A("| version | trades | total $ | win% | worst $ | avg hold (d) |\n")
    A("| --- | --- | --- | --- | --- | --- |\n")
    for label, df in naive.items():
        s = _stats(df["pnl_worst"].values)
        A(f"| {label} | {s['trades']} | {s['total']:,.0f} | {s['win_rate']*100:.1f} | "
          f"{s['worst']:,.0f} | {df['hold_days'].mean():.1f} |\n")

    # placebo
    if placebos:
        A("\n## Matched random-exit placebo (worst-side; run only if a DTE is positive)\n")
        A("| DTE | real total $ | placebo p50 $ | placebo p95 $ | frac placebo>=real | beats placebo |\n")
        A("| --- | --- | --- | --- | --- | --- |\n")
        for dte, pb in placebos.items():
            A(f"| {dte} | {pb['real_total']:,.0f} | {pb['placebo_p50']:,.0f} | "
              f"{pb['placebo_p95']:,.0f} | {pb['frac_placebo_ge_real']:.3f} | "
              f"{pb['beats_placebo']} |\n")

    return "".join(L)


# =============================================================================
# Driver
# =============================================================================
def main():
    ap = argparse.ArgumentParser(description="Arm 4 — higher-DTE managed iron condor")
    ap.add_argument("--symbol", default="SPXW", choices=["SPXW", "SPX", "XSP"])
    ap.add_argument("--dtes", default="30,45")
    ap.add_argument("--oos", default="20240630")
    ap.add_argument("--out", default=None)
    ap.add_argument("--placebo", action="store_true")
    ap.add_argument("--placebo-iter", type=int, default=300)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    dtes = [int(x) for x in args.dtes.split(",") if x.strip()]
    oos = _dt.datetime.strptime(args.oos, "%Y%m%d").date()
    os.makedirs(OUT_DIR, exist_ok=True)

    results = {}
    all_trades = {}
    for dte in dtes:
        cache_csv = os.path.join(OUT_DIR, f"condor_higher_dte_{args.symbol}_{dte}.csv")
        if args.resume and os.path.isfile(cache_csv):
            df = pd.read_csv(cache_csv, parse_dates=["entry_day", "exit_day"])
            df["year"] = df["exit_day"].dt.year
            print(f"[resume] DTE {dte}: {len(df)} trades from cache", flush=True)
            trades = None
        else:
            print(f"[run] DTE {dte} on {args.symbol} ...", flush=True)
            trades = run_dte(args.symbol, dte)
            df = trades_frame(trades)
            df.to_csv(cache_csv, index=False)
            print(f"[run] DTE {dte}: {len(df)} trades  total_worst=${df['pnl_worst'].sum():,.0f}", flush=True)
        results[dte] = df
        all_trades[dte] = trades

    # naive benchmark (no stop) on 45 for the delta explanation
    print("[run] naive 45-DTE (no stop) ...", flush=True)
    naive_trades = run_naive(args.symbol, 45)
    naive_df = trades_frame(naive_trades)
    naive = {"managed 45 (this harness)": results.get(45, naive_df),
             "naive 45 (no disaster stop)": naive_df}

    # placebo per positive DTE
    placebos = {}
    if args.placebo:
        for dte in dtes:
            df = results[dte]
            if df["pnl_worst"].sum() > 0 and all_trades.get(dte):
                print(f"[placebo] DTE {dte} ({args.placebo_iter} iters) ...", flush=True)
                placebos[dte] = placebo_random_exit(args.symbol, dte, all_trades[dte],
                                                     n_iter=args.placebo_iter)

    vix_reg = load_vix_regime()
    report = build_report(args.symbol, oos, results, naive, placebos, vix_reg)

    out = args.out or os.path.join(OUT_DIR, "condor_higher_dte_20260706.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(report)
    print("\n" + report)
    print(f"\n[report] -> {out}", flush=True)


if __name__ == "__main__":
    main()
