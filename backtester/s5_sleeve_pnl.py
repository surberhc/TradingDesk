r"""
s5_sleeve_pnl.py -- FIRST-CUT year-by-year P&L of the standalone S5 HEDGE SLEEVE.

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the options warehouse.
numpy / pandas + the committed s5_financing_harness (honest fills). ASCII console output.

================================================================================
WHAT THIS IS -- and the mistake it deliberately does NOT repeat
================================================================================
The SLEEVE = [owned always-on deep tail]  +  [short-premium financing overlay], with
BOTH legs' FULL P&L netted into one stream. NO core equity. A client bolts this onto
their own book; the question is: as a self-contained sleeve, how many years does it make
money vs lose, how big are the crash payoffs, and what does it cost to hold in calm years?

The mistake NOT to repeat (see the 2026-07-05 synthesis): the old `sell_against_owned_tail`
metric counted ONLY the short leg's premium and used the deep tail merely as a risk CAP --
so the tail's own crash payoff was invisible and the put-write "blew up -95% in 2022".
Here the tail's FULL P&L (theta carry AND crash payoff) is counted and SUMMED with the
financing leg. The 2022 short-leg loss must be offset by the tail leg's 2022 payoff -- that
netting is the entire point.

    sleeve P&L = TAIL leg  (mark-to-market incl. theta carry + crash payoff, continuously rolled)
               + FIN  leg  (financing premium collected - financing losses, continuously rolled)

================================================================================
ACCOUNTING CONVENTION (stated explicitly)
================================================================================
* Sleeve notional unit = ONE SPX index unit = (index level) * 100 $.  Every leg's daily
  P&L is expressed as a fraction of the CURRENT sleeve notional (index_level * 100), so
  annual figures are "%/yr of the SPX notional the sleeve is sized against."
* TAIL leg size  = TAIL_FRAC contracts of the deep put per index unit (base 0.50).
* FIN  leg size  = FIN_FRAC  contracts of the short put per index unit (base = TAIL_FRAC,
  i.e. 1:1 financing:tail notional). NET LONG CONVEXITY guardrail: FIN_FRAC <= TAIL_FRAC.
* Single BOOK per leg, NON-overlapping: one position open at a time, rolled at exit. This
  gives one unambiguous daily P&L stream per leg (unlike the enter-every-day sweep, whose
  ~250 concurrent positions cannot be summed against one unit of notional).

================================================================================
DATA
================================================================================
* TAIL leg priced daily off `output/s5_realskew_table.parquet` -- the committed daily
  REAL-SKEW table (und, dte, per-strike mid/delta/iv at 10/15/20/25% OTM, 63-DTE roll).
  Marked to the real-skew MID (a long book we hold; mid-marking a held hedge is standard).
* FIN leg priced off the warehouse SPXW EOD chain via s5_financing_harness (HONEST fills:
  sell at bid, buy at ask, $0.65/leg commission, cash-settled European mechanics).
* 2021 IS A DATA HOLE for honest two-sided quotes (the harness dead window
  2020-08-13..2021-12-31). The FIN leg cannot be honestly filled there, so the COMBINED
  sleeve EXCLUDES 2021 entirely. Clean windows: A=2018..2020-08-12, B=2022..2026-07-02.
  (The tail table happens to carry mid prices through 2021, but we do NOT combine a leg we
  can't honestly finance against -- 2021 is reported as MISSING, per the brief.)

FIRST CUT to see the PROFILE, not a validated ratio. Only TWO real crash tests (COVID 2020,
2022 bear) + a 2021 hole => ~7 usable years, 2 crash episodes: enough for the SHAPE, not to
fine-tune a precise sizing. Do NOT curve-fit the ratio.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import numpy as np
import pandas as pd

import math

import s5_financing_harness as h

HERE = Path(__file__).resolve().parent
REALSKEW = HERE / "output" / "s5_realskew_table.parquet"
OUT_DIR = HERE / "output" / "s5_financing"

CONTRACT_MULT = 100.0

# ---- TAIL leg base spec (the validated always-on deep tail) ----
TAIL_FRAC_BASE = 0.50        # 0.50 contracts per index unit (validated sweet-spot size)
TAIL_OTM_COL = "20"          # 20% OTM strike column in the realskew table (10/15/20/25)
TAIL_ROLL_DTE = 21           # roll the ~63-DTE put once it ages to <= this many DTE

# ---- FIN leg base spec (put-write financing overlay) ----
FIN_DTE = 45                 # ~30-45 DTE base case
FIN_MGMT = "hold"            # hold-to-expiry (cleanest, no early-close artifact)

# clean windows (mirror the harness)
WIN_A = (dt.date(2018, 1, 2), dt.date(2020, 8, 12))
WIN_B = (dt.date(2022, 1, 3), dt.date(2026, 7, 2))

R_RF = 0.0285      # BS discount rate (10y avg on disk)
Q_DIV = 0.019      # SPX dividend yield
TRADING_DAYS = 252.0


# ---- hand-rolled Black-Scholes put (no scipy) ----
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_put(S, K, T, sigma, r=R_RF, q=Q_DIV) -> float:
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * math.exp(-q * T) * _norm_cdf(-d1)


# ============================================================================
# TAIL LEG: continuously-rolled long deep-OTM put book, real-skew mid marks.
# ============================================================================
def tail_daily_pnl(tail_frac: float = TAIL_FRAC_BASE,
                   otm_col: str = TAIL_OTM_COL,
                   roll_dte: int = TAIL_ROLL_DTE) -> pd.DataFrame:
    """Daily P&L of a single continuously-rolled long deep-OTM put book, as a FRACTION of
    sleeve notional (index_level * 100). Real-skew IV from the table; BS daily marks.

    Mechanics: HOLD `tail_frac` contracts of a fixed put (strike K, calendar expiry). Each
    day re-price the SAME held contract with Black-Scholes at:
        S      = today's index level (table `und`),
        K      = the held strike,
        T      = the held contract's remaining calendar DTE / 365 (decays daily -> THETA),
        sigma  = the REAL-SKEW implied vol read from the table at the held contract's LIVE
                 moneyness (interpolated across the 10/15/20/25% OTM iv columns), so a
                 vol spike in a crash lifts the mark (crash payoff) and calm decay bleeds it.
    At roll (held DTE <= roll_dte) SELL the aged put at its BS mark and BUY a fresh ~63-DTE
    `otm_col`% OTM put at its BS mark. The swap is cash-neutral at mid (sell old value, buy
    new value of equal cash); the CARRY then bleeds out day-by-day as the fresh put decays.
    So the standalone book bleeds ~-carry%/yr in calm and spikes large-positive in crashes.

    Returns per-day frame: index=date, columns r_tail (fraction of notional), tail_val
    (held mark as fraction of notional), und (index level).
    """
    t = pd.read_parquet(REALSKEW).copy()
    t["date"] = pd.to_datetime(t["date"])
    t = t.sort_values("date").reset_index(drop=True)

    otm_map = {"10": 0.10, "15": 0.15, "20": 0.20, "25": 0.25}
    otm_levels = np.array([0.10, 0.15, 0.20, 0.25])

    def iv_at_moneyness(row, target_otm: float) -> float:
        """Real-skew IV (fraction) at a given OTM level, interpolated across the four quoted
        strikes; flat-extrapolated beyond the ends (a crash pushes the held strike ITM ->
        clamp to the richest quoted skew point, then BS handles the deep-ITM intrinsic)."""
        ivs = np.array([row["iv_10"], row["iv_15"], row["iv_20"], row["iv_25"]], float)
        x = otm_levels
        if target_otm <= x[0]:
            return float(ivs[0])
        if target_otm >= x[-1]:
            return float(ivs[-1])
        return float(np.interp(target_otm, x, ivs))

    dates = t["date"].tolist()
    und = t["und"].to_numpy(float)
    dte_tab = t["dte"].to_numpy(int)
    n = len(t)

    # seed the held contract on day 0 at the quoted otm_col% OTM strike
    held_K = und[0] * (1.0 - otm_map[otm_col])
    held_dte = int(dte_tab[0])
    iv0 = float(t.iloc[0][f"iv_{otm_col}"])
    held_mark = _bs_put(und[0], held_K, held_dte / 365.0, iv0)

    r_tail = np.zeros(n)
    tail_val = np.zeros(n)
    tail_val[0] = tail_frac * held_mark / und[0]
    prev_mark = held_mark   # option points of the held contract at the prior close

    for i in range(1, n):
        cal_gap = (dates[i] - dates[i - 1]).days
        held_dte = max(held_dte - cal_gap, 0)
        s = und[i]
        otm_now = 1.0 - held_K / s               # live OTM fraction of the held strike
        iv = iv_at_moneyness(t.iloc[i], otm_now)
        mark = _bs_put(s, held_K, held_dte / 365.0, iv)

        # daily P&L IN OPTION POINTS = tail_frac * (mark_today - mark_yesterday) for the SAME
        # held contract. Express as a fraction of TODAY's sleeve notional (index level S). This
        # is a proper point-P&L over a consistent notional -- not a difference of two fractions
        # with different denominators (the earlier bug that broke the monotonic-decline case).
        r_tail[i] = tail_frac * (mark - prev_mark) / s
        tail_val[i] = tail_frac * mark / s

        if held_dte <= roll_dte:
            # ROLL: swap the aged put for a fresh ~63-DTE put at the same moneyness. The old
            # put's mark move up to today is already booked in r_tail[i]; the swap itself is a
            # fair value-for-value trade (sell old at mark, buy new at premium) that books NO
            # P&L -- it only re-seats the held contract. Tomorrow's decay/gain is measured from
            # the NEW premium.
            held_K = s * (1.0 - otm_map[otm_col])
            held_dte = int(dte_tab[i])
            new_iv = float(t.iloc[i][f"iv_{otm_col}"])
            held_mark = _bs_put(s, held_K, held_dte / 365.0, new_iv)
            tail_val[i] = tail_frac * held_mark / s
            prev_mark = held_mark
        else:
            prev_mark = mark

    out = pd.DataFrame({"date": dates, "r_tail": r_tail, "tail_val": tail_val, "und": und})
    return out.set_index("date")


# ============================================================================
# FIN LEG: continuously-rolled NON-overlapping short put-write book, honest fills.
# ============================================================================
def fin_daily_pnl(fin_frac: float,
                  short_delta: float,
                  dte: int = FIN_DTE) -> pd.DataFrame:
    """Daily P&L of a single NON-overlapping short put-write book, as a FRACTION of sleeve
    notional. One short put open at a time; on exit, re-enter on the next clean day.

    Honest fills via the harness (sell at bid on entry, buy at ask to close/settle at
    intrinsic, $0.65/leg commission). Each trade's net P&L is spread across its hold days by
    its daily marks (the harness records a mark path); we distribute realized P&L to the
    exit day for a clean daily stream keyed to sleeve notional at entry.

    Returns per-day frame: index=date, columns:
      r_fin : day-over-day FIN P&L as fraction of sleeve notional (booked at trade exit)
    """
    days = h.available_days(clean_only=True)
    struct = h.put_write(dte=dte, short_delta=short_delta, management=h.Management(mode="hold"))

    # walk a single book: enter on the earliest clean day, hold to exit, re-enter next clean day
    recs = []           # (exit_date, entry_underlying, net_pnl)
    i = 0
    N = len(days)
    while i < N:
        d = days[i]
        res = h.run_trade(struct, d, days)
        if res is None:
            i += 1
            continue
        recs.append((res.exit_date, res.entry_underlying, res.net_pnl))
        # advance to the first clean day strictly AFTER the exit date (non-overlapping)
        j = i + 1
        while j < N and days[j] <= res.exit_date:
            j += 1
        i = j

    # build a daily stream: book each trade's net P&L (as frac of its entry sleeve notional)
    # on its EXIT date. Days with no exit get 0.
    all_days = pd.to_datetime(days)
    r = pd.Series(0.0, index=all_days)
    for (exit_date, und, net_pnl) in recs:
        core = und * CONTRACT_MULT
        frac = fin_frac * net_pnl / core
        r.loc[pd.Timestamp(exit_date)] += frac
    out = pd.DataFrame({"r_fin": r})
    out.index.name = "date"
    return out, recs


# ============================================================================
# COMBINE + annualize.
# ============================================================================
def in_clean_window(d: pd.Timestamp) -> bool:
    dd = d.date()
    return (WIN_A[0] <= dd <= WIN_A[1]) or (WIN_B[0] <= dd <= WIN_B[1])


def combine_sleeve(tail_frac: float, fin_frac: float, short_delta: float,
                   fin_dte: int = FIN_DTE):
    """Combine tail + fin daily P&L into one sleeve stream, restricted to clean windows
    (2021 excluded). Returns (daily_df, fin_recs)."""
    tail = tail_daily_pnl(tail_frac=tail_frac)
    fin, fin_recs = fin_daily_pnl(fin_frac=fin_frac, short_delta=short_delta, dte=fin_dte)

    # restrict BOTH to clean-window days only (drop 2021 + dead window)
    df = tail.join(fin, how="outer")
    df["r_fin"] = df["r_fin"].fillna(0.0)
    df["r_tail"] = df["r_tail"].fillna(0.0)
    df = df[[in_clean_window(ts) for ts in df.index]]
    df["r_sleeve"] = df["r_tail"] + df["r_fin"]
    df["year"] = df.index.year
    return df, fin_recs


def annual_table(df: pd.DataFrame) -> pd.DataFrame:
    """Per-year summed P&L (fraction of notional) for tail, fin, sleeve.
    Simple sum of daily fractions within the year (non-compounded; a carry quote)."""
    g = df.groupby("year").agg(
        r_tail=("r_tail", "sum"),
        r_fin=("r_fin", "sum"),
        r_sleeve=("r_sleeve", "sum"),
        n_days=("r_sleeve", "size"),
    )
    return g


def net_delta_guardrail(tail_frac: float, fin_frac: float, short_delta: float,
                        fin_dte: int = FIN_DTE) -> dict:
    """Confirm the sleeve stays LONG CONVEXITY (net short delta from the tail dominates the
    short put's positive delta) through the 2020 and 2022 crash bottoms. Reads the tail's
    real-skew delta at the held strike and the short put's delta near each bottom.

    Sleeve net delta (per index unit, sleeve-only, NO core) = tail_frac * put_delta_tail
    + fin_frac * (-short_put_delta_contribution). A LONG put = negative delta (good, pays in
    a crash); a SHORT put = positive delta (loses in a crash). Net delta << 0 = still long
    convexity / the hedge still pays. Net delta >= 0 = financing has cancelled the hedge."""
    t = pd.read_parquet(REALSKEW).copy()
    t["date"] = pd.to_datetime(t["date"])
    bottoms = {"COVID 2020": dt.date(2020, 3, 23), "Bear 2022": dt.date(2022, 10, 12)}
    out = {}
    otm_map = {"10": 0.10, "15": 0.15, "20": 0.20, "25": 0.25}
    for label, bd in bottoms.items():
        row = t[t["date"] <= pd.Timestamp(bd)].iloc[-1]
        tail_delta = float(row[f"delta_{TAIL_OTM_COL}"])   # deep put delta (negative)
        # short put delta: a |short_delta| put; short position contributes +|delta| to net.
        short_put_delta = short_delta   # magnitude; short => +short_delta to net delta
        net = tail_frac * tail_delta + fin_frac * short_put_delta
        out[label] = {
            "date": str(row["date"].date()),
            "tail_put_delta": tail_delta,
            "tail_contrib": tail_frac * tail_delta,
            "fin_contrib": fin_frac * short_put_delta,
            "net_delta": net,
            "long_convexity": net < 0,
        }
    return out
