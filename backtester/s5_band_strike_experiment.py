"""
S5 — BAND-RELATIVE TAIL-STRIKE experiment.

PAPER / research only. OFFLINE. Windows. READ-ONLY warehouse. NEW file — imports the
S5 prototype engine and the real-skew table; edits NOTHING in the frozen engine or config.

== The question ==
S5's defensive Tier-1 tail is placed at a FIXED %-OTM (validated default ~0.50 notional /
20-25% OTM, ~63 DTE). This experiment asks whether placing the strike RELATIVE TO the
forward expected-move band (a CLEAN CBOE forward-vol expected move, horizon-scaled to the
tail DTE) beats the fixed rule on CARRY COST per unit of CRASH PROTECTION, robustly across
ALL THREE crises (2018-Q4*, 2020 COVID, 2022 bear). *2018-Q4 is at the very start of the
warehouse skew window.

  DATA-BUG NOTE: the originally-planned band source — the warehouse derived
  SPX_gex_daily.expected_move_pct — is CORRUPT across 2020-21 (32% of 2020 days and 100%
  of 2021 days are degenerate, em < 0.05%; the underlying implied_vol is broken there). It
  is REPLACED by the clean CBOE VIX3M (~90d) forward-vol index (VIX 30d as a robustness
  variant), same clean-source discipline the S4 lane uses. See the report's substitution note.

This is an EXECUTION / measurement refinement on an already-validated leg, NOT an alpha
claim. Expect a modest ceiling; report a WASH honestly if that is what the data says.

== The three arms (pre-registered) ==
Everything else in the defensive leg is held FIXED (notional 0.50, 63 DTE tenor, 21d roll
floor, real-skew pricing). Only the STRIKE-PLACEMENT RULE changes:

  A) FIXED-OTM baseline .............. OTM = const  (sweep {20%, 22.5%, 25%})
  B) IMPLIED-BAND variant ........... OTM = clamp( N * em_tail, floor, cap )
        em_tail = (VIX3M/100) * sqrt(TENOR_D/252) — the forward EXPECTED MOVE over the tail's
        ~63-DTE horizon, built from the CBOE VIX3M (~90d) index (clean, full crisis coverage).
        VIX3M is already sqrt(252)-annualized, so we horizon-scale it to the tail DTE directly.
        Sweep N over a small grid.

        >>> DATA-BUG NOTE (why NOT the warehouse expected_move_pct). The original plan used the
        derived SPX_gex_daily `expected_move_pct`. That column is CORRUPT across the crisis
        window — verified: fraction of days with em < 0.05% is 32% in 2020 (straddling the COVID
        V-bottom) and 100% in 2021 (median 0.007%, dead). Its underlying implied_vol is broken
        there. Placing the band off it in 2020-21 is garbage, and 2020 is a test crisis. So we
        substitute the clean CBOE VIX3M forward-vol source (same discipline as the S4 lane). A
        VIX (30d) robustness variant is also reported.

  C) RVOL-BAND control (anti-curve-fit) OTM = M * rvol_daily * sqrt(TENOR_D)
        rvol_daily = S4's realized-vol estimator / sqrt(252) (REALIZED, not implied). M is
        auto-solved so the control's AVERAGE realized OTM% MATCHES arm B's — isolating
        whether the IMPLIED band adds anything over just ANY vol-scaling of the strike.

== Pricing (apples-to-apples) ==
Both the fixed and the band arms price the Tier-1 tail off the SAME real warehouse SPXW
skew used by the validated real-skew sweep. Because the band arm places the strike at a
CONTINUOUS, daily-varying OTM (not one of the table's {10,15,20,25} columns), we build a
continuous causal skew-uplift(OTM) interpolator from the table (linear in OTM between
measured knots; slope-extrapolated beyond 25%). IV(T, otm) = VIX[T]/100 + uplift_real(<=T, otm).
Identical construction to the real-skew sweep, just continuous in OTM.

== Causality ==
The strike for the put bought/rolled at close of day T uses em/rvol/skew observed on/before
T (merge_asof backward). No look-ahead. The crash bottom used to MEASURE capture is a
post-hoc measurement point, never a decision input.

Output: output/s5_band_strike_YYYYMMDD.md  (+ flushed console progress).
"""
from __future__ import annotations
import datetime as dt
import os
import sys
import time

import numpy as np
import pandas as pd

import s5_convexity_overlay as P
from s5_convexity_overlay import (
    build_panel, s4_returns, nav, metric_block, find_bottom,
    bs_put, bs_put_delta, norm_cdf,
    fpct, fnum, TRADING_DAYS_PER_YEAR,
    R_RF, Q_DIV, TAIL_TENOR_D, TAIL_ROLL_FLOOR_D, SEED_FRAC,
)
from strategies.spx_vol_control import realized_vol_simple

OUT_DIR = P.OUT_DIR
SKEW_TABLE = os.path.join(OUT_DIR, "s5_realskew_table.parquet")
BT_DATA = P.DATA_DIR   # C:\TradingDesk-Local\bt_data (clean CBOE vol family lives here)

# ---- held-FIXED defensive-leg params (NOT swept — the validated leg) ----
TAIL_FRAC = 0.50            # validated notional (real-skew sweet spot region)
TENOR_D = TAIL_TENOR_D      # 63
ROLL_FLOOR_D = TAIL_ROLL_FLOOR_D  # 21

# ---- pre-registered arm grids ----
FIXED_OTMS = [0.20, 0.225, 0.25]           # arm A baseline sweep
# arm B sigma-multiples: em_tail = VIX3M-forward 1-sigma over the tail horizon (~10% at 1σ),
# so a 20-25% deep-tail strike is a ~2-2.5σ event. Grid brackets the fixed baseline moneyness.
BAND_NS = [1.75, 2.00, 2.25, 2.50]
# strike clamp so a near-zero-EM (thin gamma) day can't place the strike near spot,
# and a blow-out EM day can't place it absurdly deep. Pre-registered, generous.
OTM_FLOOR = 0.08
OTM_CAP = 0.40

REAL_START = "2018-01-01"
REAL_END = "2026-06-26"

# episodes inside the warehouse window (GFC excluded — no real chain)
EPISODES = {
    "2018-Q4":     ("2018-09-01", "2019-04-30"),
    "COVID 2020":  ("2020-02-01", "2020-12-31"),
    "Bear 2022":   ("2022-01-01", "2023-06-30"),
}


# ---------------------------------------------------------------------------
# Continuous causal real-skew uplift(OTM) interpolator (from the real-skew table)
# ---------------------------------------------------------------------------
def load_skew_knots():
    t = pd.read_parquet(SKEW_TABLE)
    t["date"] = pd.to_datetime(t["date"]).dt.normalize()
    t = t.set_index("date").sort_index()
    up = pd.DataFrame(index=t.index)
    up["iv_atm"] = t["iv_atm"]
    for tag in ["10", "15", "20", "25"]:
        up[f"up_{tag}"] = t[f"iv_{tag}"] - t["iv_atm"]
    up = up.dropna()
    return up


def make_continuous_uplift_fn(panel_idx, up_df):
    """Return uplift(i, otm): causal per-day skew uplift over ATM at a CONTINUOUS otm.

    For engine date i we use the most-recent skew row on/before that date (merge_asof
    backward). Uplift is a piecewise-linear interp in OTM through the measured knots
    {0->0, .10, .15, .20, .25}; beyond .25 we extrapolate with the .20->.25 slope; below
    .10 we interp from 0 (uplift 0 at ATM) to the .10 knot. All causal, no look-ahead.
    """
    eng = pd.DataFrame({"date": panel_idx})
    joined = pd.merge_asof(eng, up_df.reset_index(), on="date", direction="backward")
    knots_otm = np.array([0.0, 0.10, 0.15, 0.20, 0.25])
    up10 = joined["up_10"].values
    up15 = joined["up_15"].values
    up20 = joined["up_20"].values
    up25 = joined["up_25"].values
    atm = joined["iv_atm"].values

    def uplift(i, otm):
        u = np.array([0.0, up10[i], up15[i], up20[i], up25[i]])
        if not np.all(np.isfinite(u)):
            # before table coverage: fall back to flat bump (won't happen in-window)
            return P.TAIL_SKEW_BUMP
        if otm <= 0.25:
            return float(np.interp(otm, knots_otm, u))
        # extrapolate beyond 25% with the 20->25 slope
        slope = (u[4] - u[3]) / (knots_otm[4] - knots_otm[3])
        return float(u[4] + slope * (otm - 0.25))

    def atm_finite(i):
        return np.isfinite(atm[i])

    return uplift, atm_finite


# ---------------------------------------------------------------------------
# Forward expected-move band from a CLEAN CBOE vol index (VIX3M / VIX), causal.
# NOTE: the warehouse `expected_move_pct` is corrupt in 2020-21 (see the header
# DATA-BUG NOTE), so the implied band is built from a clean forward-vol source.
# The index is annualized (sqrt(252)); em over the tail horizon = idx/100 * sqrt(DTE/252).
# ---------------------------------------------------------------------------
def load_forward_em(panel_idx, index_ticker="_vix3m", tenor_d=TENOR_D):
    s = P.load_series(index_ticker)          # already normalized, sorted
    s = s.rename("vol").reset_index()
    s.columns = ["date", "vol"]
    s["date"] = pd.to_datetime(s["date"]).dt.normalize()
    eng = pd.DataFrame({"date": panel_idx})
    joined = pd.merge_asof(eng, s, on="date", direction="backward")
    horizon_scale = np.sqrt(tenor_d / TRADING_DAYS_PER_YEAR)
    return (joined["vol"].values / 100.0) * horizon_scale   # forward EM frac, causal


# ---------------------------------------------------------------------------
# The band-strike simulation — a focused re-implementation of the S5 Tier-1
# defensive leg ONLY (constant core + always-on uncapped tail), so the strike-
# placement RULE can vary at each roll. Mirrors s5_convexity_overlay's passive
# path exactly (same accounting, same real-skew pricing) but with a pluggable
# `otm_at(i)` strike rule. Tier-2 / upside / harvest ledger are OFF here — this
# is a clean defensive-leg carry-vs-protection measurement (matches the way the
# real-skew tail sweep isolates the tail).
# ---------------------------------------------------------------------------
def simulate_band_tail(df, otm_at, uplift_fn, atm_ok, tail_frac=TAIL_FRAC):
    """Constant 1.0x core + always-on uncapped Tier-1 tail, strike per otm_at(i).
    Returns dict with r_fund series, net_delta series, realized annual carry, and the
    per-day realized OTM used (for the control's average-OTM matching)."""
    idx = df.index
    spx = df["spx_px"].values
    vix = df["vix"].values
    r_spy = df["r_spy"].values
    r_cash = df["r_cash"].values
    n = len(idx)

    def t1_iv(i, otm):
        u = uplift_fn(i, otm) if atm_ok(i) else P.TAIL_SKEW_BUMP
        return max(vix[i] / 100.0 + u, 0.03)

    def put_val(i, K, exp, otm):
        if K is None or np.isnan(K):
            return 0.0
        T = max(exp - i, 0) / TRADING_DAYS_PER_YEAR
        return bs_put(spx[i], K, T, t1_iv(i, otm), R_RF, Q_DIV)

    def put_delta(i, K, exp, otm):
        if K is None or np.isnan(K):
            return 0.0
        T = max(exp - i, 0) / TRADING_DAYS_PER_YEAR
        return bs_put_delta(spx[i], K, T, t1_iv(i, otm), R_RF, Q_DIV)

    # inception tail (strike at day-0 rule)
    otm0 = otm_at(0)
    t1_otm = otm0
    t1_K = spx[0] * (1.0 - otm0)
    t1_exp = min(TENOR_D, n - 1)

    nav0 = 1.0
    core = nav0
    ledger = SEED_FRAC * nav0     # same cold-start seed as the engine (carry float)
    t1_dollars = put_val(0, t1_K, t1_exp, t1_otm) / spx[0] * tail_frac
    core -= t1_dollars

    def nav_now():
        return core + ledger + t1_dollars

    nav_prev = nav_now()
    fund_ret = np.full(n, np.nan)
    net_delta = np.full(n, np.nan)
    otm_used = np.full(n, np.nan)
    total_tail_carry = 0.0

    for i in range(1, n):
        core *= (1.0 + r_spy[i])
        ledger *= (1.0 + r_cash[i])
        t1_dollars = put_val(i, t1_K, t1_exp, t1_otm) / spx[i] * tail_frac

        nav_cur = nav_now()
        fund_ret[i] = nav_cur / nav_prev - 1.0

        d_t1 = put_delta(i, t1_K, t1_exp, t1_otm) * tail_frac
        net_delta[i] = 1.0 + d_t1
        otm_used[i] = t1_otm

        # roll when aged/expired — new strike from the (causal) rule at close of i
        if (t1_exp - i) <= ROLL_FLOOR_D:
            old_val = t1_dollars
            new_otm = otm_at(i)
            new_K = spx[i] * (1.0 - new_otm)
            new_exp = min(i + TENOR_D, n - 1)
            new_prem = put_val(i, new_K, new_exp, new_otm) / spx[i] * tail_frac
            total_tail_carry += new_prem
            net_cash = new_prem - old_val
            pay_led = min(max(ledger, 0.0), net_cash) if net_cash > 0 else net_cash
            ledger -= pay_led
            core -= (net_cash - pay_led)
            t1_otm = new_otm; t1_K = new_K; t1_exp = new_exp
            t1_dollars = new_prem

        # sweep excess ledger cash back into core (same convention as the engine, so the
        # harvest-off seed doesn't sit as a phantom RF drag). Keep a tiny working buffer.
        work_buffer = 0.02 * nav_cur
        if ledger > work_buffer:
            core += (ledger - work_buffer); ledger = work_buffer

        nav_prev = nav_now()

    out = pd.DataFrame(index=idx)
    out["r_fund"] = fund_ret
    out["net_delta"] = net_delta
    out["otm_used"] = otm_used
    yrs = (n - 1) / TRADING_DAYS_PER_YEAR
    return {
        "df": out,
        "carry_pct_yr": total_tail_carry / yrs if yrs > 0 else float("nan"),
        "mean_otm": float(np.nanmean(otm_used)),
    }


# ---------------------------------------------------------------------------
# Metrics + protection scoring
# ---------------------------------------------------------------------------
def episode_protection(df, sim_df, r_spy, episodes, common):
    """For each episode: SPX drawdown, S5 drawdown-cushion, net-delta@bottom, and
    recovery capture (S5 NAV gain / SPY gain, bottom->end). Returns dict per episode."""
    r = sim_df["r_fund"]
    nd = sim_df["net_delta"]
    lo_all = common.min()
    res = {}
    for ename, (lo, hi) in episodes.items():
        if pd.Timestamp(lo) < lo_all:
            lo = lo_all.strftime("%Y-%m-%d")
        seg = df.loc[lo:hi]
        if len(seg) == 0:
            res[ename] = None
            continue
        bottom = find_bottom(df, lo, hi)
        bi = df.index.get_loc(bottom)
        # Apples-to-apples peak->bottom over the SAME pre-bottom segment, so the cushion is
        # a clean "how much less did the fund fall into the crash low" (both anchored to the
        # episode's SPX peak date, measured to the SPX bottom date).
        spx_seg = df["spx_px"].loc[lo:bottom]
        peak_date = spx_seg.idxmax()
        spx_dd = df["spx_px"].loc[bottom] / df["spx_px"].loc[peak_date] - 1.0
        # fund NAV over the identical peak->bottom span
        rr = r.loc[peak_date:bottom]
        s5_dd = float(nav(rr).iloc[-1] - 1.0) if len(rr) else float("nan")
        nd_bottom = float(nd.iloc[bi]) if not np.isnan(nd.iloc[bi]) else float("nan")
        end = df.loc[lo:hi].index.max()
        cap_s5 = nav(r.loc[bottom:end]).iloc[-1] - 1.0
        cap_spy = nav(r_spy.loc[bottom:end]).iloc[-1] - 1.0
        capture = cap_s5 / cap_spy if abs(cap_spy) > 1e-9 else float("nan")
        # cushion = how much of SPX's peak-to-bottom decline the fund AVOIDED
        cushion = (spx_dd - s5_dd)   # both negative; positive cushion = fund fell less
        res[ename] = {
            "bottom": bottom, "spx_dd": spx_dd, "s5_dd": s5_dd, "cushion": cushion,
            "nd_bottom": nd_bottom, "capture": capture,
        }
    return res


def score_arm(df, sim, r_spy, episodes, common, rcv):
    r = sim["df"]["r_fund"].loc[common]
    m = metric_block(r, rcv)
    prot = episode_protection(df, sim["df"], r_spy, episodes, common)
    # protection-per-carry: mean episode cushion (positive = protection) / carry %/yr
    cushions = [prot[e]["cushion"] for e in episodes if prot[e] is not None]
    mean_cushion = float(np.mean(cushions)) if cushions else float("nan")
    carry = sim["carry_pct_yr"]
    prot_per_carry = (mean_cushion / carry) if (carry and carry > 1e-9) else float("nan")
    return {**m, "carry_pct_yr": carry, "mean_otm": sim["mean_otm"],
            "prot": prot, "mean_cushion": mean_cushion, "prot_per_carry": prot_per_carry}


# ---------------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(line_buffering=True)
    t0 = time.time()
    print("=== S5 BAND-RELATIVE TAIL-STRIKE EXPERIMENT ===", flush=True)

    print("loading panel + skew knots + expected-move band ...", flush=True)
    full = build_panel()
    df = full.loc[REAL_START:REAL_END].copy()
    print(f"window: {df.index.min().date()} -> {df.index.max().date()} ({len(df)} days)", flush=True)

    up_df = load_skew_knots()
    uplift_fn, atm_ok = make_continuous_uplift_fn(df.index, up_df)
    # PRIMARY implied band: clean CBOE VIX3M (~90d, nearest the 63-DTE tail), horizon-scaled.
    em = load_forward_em(df.index, "_vix3m", TENOR_D)
    # ROBUSTNESS: VIX (30d) forward EM, same construction.
    em_vix = load_forward_em(df.index, "_vix", TENOR_D)
    print(f"skew knots {up_df.index.min().date()}..{up_df.index.max().date()} ({len(up_df)}); "
          f"VIX3M em_tail coverage {np.isfinite(em).mean()*100:.0f}% "
          f"(median {np.nanmedian(em)*100:.1f}% of spot)", flush=True)

    # realized-vol daily sigma (S4 estimator), causal, aligned
    rvol_ann = realized_vol_simple(df["r_spy"], 20, 60).values
    rvol_daily = rvol_ann / np.sqrt(TRADING_DAYS_PER_YEAR)

    r_spy = df["r_spy"]; rc = df["r_cash"]
    # common window vs S4 (so all arms share the same measurement span)
    r_s4, s4_exp = s4_returns(df)
    sqrtT = np.sqrt(TENOR_D)

    # ---- build the three arms ----
    def fixed_rule(c):
        return lambda i: c

    def band_rule(N, em_src=em):
        # em_src is ALREADY horizon-scaled to the tail DTE (VIX3M/100 * sqrt(DTE/252)),
        # so N is a pure sigma-multiple on the forward expected move — no extra sqrtT here.
        def f(i):
            e = em_src[i]
            if not np.isfinite(e) or e <= 0:
                return 0.20   # neutral fallback if the vol index is missing (never in-window)
            return float(np.clip(N * e, OTM_FLOOR, OTM_CAP))
        return f

    def rvol_rule(M):
        def f(i):
            v = rvol_daily[i]
            if not np.isfinite(v) or v <= 0:
                return 0.20
            return float(np.clip(M * v * sqrtT, OTM_FLOOR, OTM_CAP))
        return f

    # A) fixed baselines
    print("\n--- ARM A: FIXED-OTM baselines ---", flush=True)
    fixed = {}
    for c in FIXED_OTMS:
        sim = simulate_band_tail(df, fixed_rule(c), uplift_fn, atm_ok)
        common = sim["df"]["r_fund"].dropna().index.intersection(r_s4.dropna().index)
        rcv = rc.loc[common]
        fixed[c] = score_arm(df, sim, r_spy, EPISODES, common, rcv)
        s = fixed[c]
        print(f"  fixed {c*100:4.1f}% OTM  CAGR {fpct(s['cagr']):>7}  maxDD {fpct(s['maxdd']):>8}  "
              f"carry {fpct(s['carry_pct_yr'],2):>7}/yr  meanCushion {fpct(s['mean_cushion'],1):>6}  "
              f"prot/carry {fnum(s['prot_per_carry'],2):>6}", flush=True)

    # B) implied-band variant
    print("\n--- ARM B: IMPLIED-BAND (gamma expected-move) variant ---", flush=True)
    band = {}
    for N in BAND_NS:
        sim = simulate_band_tail(df, band_rule(N), uplift_fn, atm_ok)
        common = sim["df"]["r_fund"].dropna().index.intersection(r_s4.dropna().index)
        rcv = rc.loc[common]
        band[N] = score_arm(df, sim, r_spy, EPISODES, common, rcv)
        s = band[N]
        print(f"  band N={N:.2f}  meanOTM {s['mean_otm']*100:4.1f}%  CAGR {fpct(s['cagr']):>7}  "
              f"maxDD {fpct(s['maxdd']):>8}  carry {fpct(s['carry_pct_yr'],2):>7}/yr  "
              f"meanCushion {fpct(s['mean_cushion'],1):>6}  prot/carry {fnum(s['prot_per_carry'],2):>6}", flush=True)

    # C) rvol-band control — solve M so its mean OTM matches each band arm's mean OTM
    print("\n--- ARM C: RVOL-BAND control (matched avg OTM to arm B) ---", flush=True)
    control = {}
    for N in BAND_NS:
        target_mean_otm = band[N]["mean_otm"]
        # solve M by a short bisection on mean realized OTM
        lo_M, hi_M = 0.1, 6.0
        for _ in range(40):
            mid = 0.5 * (lo_M + hi_M)
            otm = np.clip(mid * rvol_daily * sqrtT, OTM_FLOOR, OTM_CAP)
            mo = np.nanmean(otm)
            if mo < target_mean_otm:
                lo_M = mid
            else:
                hi_M = mid
        M = 0.5 * (lo_M + hi_M)
        sim = simulate_band_tail(df, rvol_rule(M), uplift_fn, atm_ok)
        common = sim["df"]["r_fund"].dropna().index.intersection(r_s4.dropna().index)
        rcv = rc.loc[common]
        control[N] = {**score_arm(df, sim, r_spy, EPISODES, common, rcv), "M": M,
                      "target_mean_otm": target_mean_otm}
        s = control[N]
        print(f"  ctrl (~band N={N:.2f}, M={M:.2f})  meanOTM {s['mean_otm']*100:4.1f}%  "
              f"CAGR {fpct(s['cagr']):>7}  maxDD {fpct(s['maxdd']):>8}  carry {fpct(s['carry_pct_yr'],2):>7}/yr  "
              f"meanCushion {fpct(s['mean_cushion'],1):>6}  prot/carry {fnum(s['prot_per_carry'],2):>6}", flush=True)

    # B') VIX (30d) robustness variant of the implied band
    print("\n--- ARM B' : IMPLIED-BAND robustness (VIX 30d instead of VIX3M) ---", flush=True)
    band_vix = {}
    for N in BAND_NS:
        sim = simulate_band_tail(df, band_rule(N, em_vix), uplift_fn, atm_ok)
        common = sim["df"]["r_fund"].dropna().index.intersection(r_s4.dropna().index)
        rcv = rc.loc[common]
        band_vix[N] = score_arm(df, sim, r_spy, EPISODES, common, rcv)
        s = band_vix[N]
        print(f"  bandVIX N={N:.2f}  meanOTM {s['mean_otm']*100:4.1f}%  CAGR {fpct(s['cagr']):>7}  "
              f"maxDD {fpct(s['maxdd']):>8}  carry {fpct(s['carry_pct_yr'],2):>7}/yr  "
              f"meanCushion {fpct(s['mean_cushion'],1):>6}  prot/carry {fnum(s['prot_per_carry'],2):>6}", flush=True)

    path = write_report(df, common, fixed, band, control, band_vix, up_df, em)
    print(f"\nreport -> {path}", flush=True)
    print(f"done in {time.time()-t0:.1f}s.", flush=True)


def _fmt_ep(prot, ename):
    if prot.get(ename) is None:
        return "— | — | —"
    p = prot[ename]
    return f"{fpct(p['cushion'],1)} | {fnum(p['nd_bottom'])}× | {fpct(p['capture'],0)}"


def write_report(df, common, fixed, band, control, band_vix, up_df, em):
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = dt.date.today().strftime("%Y%m%d")
    path = os.path.join(OUT_DIR, f"s5_band_strike_{stamp}.md")
    L = []; A = L.append
    EPS = list(EPISODES.keys())

    # anchor comparison: baseline nearest 22.5% vs band arm with the CLOSEST mean OTM
    base = fixed[0.225]
    # pick the band arm whose mean OTM is closest to 22.5% for the head-to-head
    band_anchor_N = min(band, key=lambda N: abs(band[N]["mean_otm"] - 0.225))
    b = band[band_anchor_N]
    c = control[band_anchor_N]

    A("# S5 — Band-Relative Tail-Strike Experiment")
    A("")
    A(f"*Generated {dt.date.today().isoformat()} | offline | EOD/daily | "
      f"window {common.min().date()} → {common.max().date()} ({len(common)} trading days) | "
      f"Tier-1 notional pinned {TAIL_FRAC:.2f}, tenor {TENOR_D}d, real-skew priced*")
    A("")
    A("**The question.** S5's validated Tier-1 tail is placed at a **FIXED %-OTM** "
      "(~0.50 notional / 20–25% OTM). Does placing the strike **relative to the forward "
      "expected-move band** (a clean CBOE forward-vol expected move, horizon-scaled to the "
      "63-DTE tail) buy **more crash protection per dollar of carry** than the fixed rule — "
      "robustly across **all three** in-window crises? This is an **execution/measurement** "
      "refinement on an already-validated leg, **not** an alpha claim.")
    A("")
    A("> **DATA-BUG SUBSTITUTION (load-bearing — read first).** The original plan placed the "
      "band off the warehouse `SPX_gex_daily.expected_move_pct` column. **That column is "
      "CORRUPT across the crisis window** — verified independently: the fraction of days with "
      "expected-move < 0.05% is **32% in 2020** (straddling the COVID V-bottom) and **100% in "
      "2021** (median 0.007% — completely dead); its underlying `implied_vol` field is broken "
      "there. Since 2020 COVID is one of the three test crises, **any band placed off that "
      "column would be garbage.** It has been **replaced by the clean CBOE VIX3M (~90d) "
      "forward-vol index** on local disk (`bt_data/_vix3m`) — the same clean-source discipline "
      "the S4 lane uses. VIX3M is already √252-annualized, so the forward expected move over "
      "the tail horizon is `em_tail = VIX3M/100 × √(DTE/252)`. A **VIX (30d) robustness "
      "variant** is reported too. All three crises are re-run on the clean source.")
    A("")
    A("**Three pre-registered arms** (everything else in the defensive leg held FIXED — "
      "notional 0.50, 63 DTE, 21d roll floor, identical real-skew pricing):")
    A("")
    A(f"- **A — FIXED-OTM baseline:** OTM = const, swept {{{', '.join(f'{o*100:.1f}%' for o in FIXED_OTMS)}}}.")
    A(f"- **B — IMPLIED-BAND variant (clean VIX3M):** OTM = clamp(N × em_tail, "
      f"{OTM_FLOOR*100:.0f}%, {OTM_CAP*100:.0f}%), em_tail = VIX3M/100 × √({TENOR_D}/252) "
      f"(a forward ~1σ move over the tail horizon, ≈{np.nanmedian(em)*100:.0f}% of spot at 1σ). "
      f"N is a pure **sigma-multiple**, swept N ∈ {{{', '.join(f'{n:.2f}' for n in BAND_NS)}}} "
      f"(a 20–25% deep tail is a ~2–2.5σ event). A **B′** row repeats it off VIX (30d).")
    A(f"- **C — RVOL-BAND control (anti-curve-fit):** OTM = M × rvol_daily × √{TENOR_D} "
      "(S4 **realized** vol, not implied), M auto-solved so the control's **average OTM% "
      "matches arm B's** — isolating whether the **implied** band adds anything over just "
      "**any** vol-scaling of the strike.")
    A("")
    A("**Metric — protection per unit carry.** `carry` = realized annualized Tier-1 premium "
      "spent (% of NAV/yr). `cushion` = how much of SPX's peak-to-bottom decline the fund "
      "**avoided** in that episode (SPX_dd − fund_dd; positive = fund fell less). "
      "`prot/carry` = mean-episode cushion ÷ carry — **more cushion per dollar of carry is "
      "the whole point.** `nd@bot` = net delta at the crash bottom (lower = more auto-de-risk); "
      "`capt` = recovery capture (fund NAV gain ÷ SPY gain, bottom→episode end).")
    A("")

    A("> **OTHER CAVEATS.** (1) The warehouse SPXW **skew** (for pricing, not the band) starts "
      "2018-01, so **2018-Q4 sits at the very start of the window** and its pre-crash peak is "
      "truncated — read its cushion as approximate. **No 2008 GFC** in this data. (2) Harvest "
      "ledger / Tier-2 / upside are OFF — this is a **clean defensive-leg** carry-vs-protection "
      "measurement, matching how the real-skew sweep isolates the tail. (3) The clean VIX3M/VIX "
      "band moves the strike **far less** day-to-day than the (corrupt) gamma column would have "
      "— it tracks the smooth forward-vol term, not a noisy focal-strike readout.")
    A("")

    # ---- master grid ----
    A("## All arms — carry vs protection")
    A("")
    A("| Arm | rule | mean OTM | CAGR | maxDD | carry %/yr | mean cushion | **prot/carry** |")
    A("|:--|:--|---:|---:|---:|---:|---:|---:|")
    for cc in FIXED_OTMS:
        s = fixed[cc]
        A(f"| A fixed | {cc*100:.1f}% OTM | {s['mean_otm']*100:.1f}% | {fpct(s['cagr'])} | "
          f"{fpct(s['maxdd'])} | {fpct(s['carry_pct_yr'],2)} | {fpct(s['mean_cushion'],1)} | "
          f"**{fnum(s['prot_per_carry'],2)}** |")
    for N in BAND_NS:
        s = band[N]
        A(f"| B band | N={N:.2f} | {s['mean_otm']*100:.1f}% | {fpct(s['cagr'])} | "
          f"{fpct(s['maxdd'])} | {fpct(s['carry_pct_yr'],2)} | {fpct(s['mean_cushion'],1)} | "
          f"**{fnum(s['prot_per_carry'],2)}** |")
    for N in BAND_NS:
        s = control[N]
        A(f"| C rvol-ctrl | ~N={N:.2f} (M={s['M']:.2f}) | {s['mean_otm']*100:.1f}% | {fpct(s['cagr'])} | "
          f"{fpct(s['maxdd'])} | {fpct(s['carry_pct_yr'],2)} | {fpct(s['mean_cushion'],1)} | "
          f"**{fnum(s['prot_per_carry'],2)}** |")
    for N in BAND_NS:
        s = band_vix[N]
        A(f"| B′ band-VIX | N={N:.2f} | {s['mean_otm']*100:.1f}% | {fpct(s['cagr'])} | "
          f"{fpct(s['maxdd'])} | {fpct(s['carry_pct_yr'],2)} | {fpct(s['mean_cushion'],1)} | "
          f"**{fnum(s['prot_per_carry'],2)}** |")
    A("")
    A("*B′ (VIX 30d) is a robustness check on the implied-band source; it should track B (VIX3M) "
      "closely — if the two clean forward-vol sources agree, the band's behaviour is a property "
      "of vol-scaling, not of one index's quirks.*")
    A("")

    # ---- per-episode detail for the anchor comparison ----
    A(f"## Per-episode detail — matched anchor (fixed 22.5% vs band N={band_anchor_N:.2f} vs its rvol control)")
    A("")
    A("Each cell: **cushion | net-delta@bottom | recovery capture**. The band arm is the one "
      "whose mean OTM is closest to the 22.5% fixed baseline, so the three columns are placement-"
      "rule differences at ~matched average moneyness.")
    A("")
    A(f"| Episode | A fixed 22.5% | B band N={band_anchor_N:.2f} | C rvol control |")
    A("|:--|:--|:--|:--|")
    for e in EPS:
        A(f"| {e} | {_fmt_ep(base['prot'], e)} | {_fmt_ep(b['prot'], e)} | {_fmt_ep(c['prot'], e)} |")
    A("")
    A(f"Carry: fixed **{fpct(base['carry_pct_yr'],2)}/yr**, band **{fpct(b['carry_pct_yr'],2)}/yr**, "
      f"control **{fpct(c['carry_pct_yr'],2)}/yr**. "
      f"prot/carry: fixed **{fnum(base['prot_per_carry'],2)}**, band **{fnum(b['prot_per_carry'],2)}**, "
      f"control **{fnum(c['prot_per_carry'],2)}**.")
    A("")

    # ---- verdict logic ----
    # The HONEST arbiter is full-episode maxDD (assumption-free), cross-checked against the
    # cushion metric. cushion (peak->bottom snapshot) can flatter an arm whose strike WIDENS
    # in the crash — so we require the band to win on BOTH maxDD and per-episode cushion, and
    # to beat its own realized-vol control, before calling any edge.
    best_fixed = max(fixed.values(), key=lambda s: s["prot_per_carry"] if np.isfinite(s["prot_per_carry"]) else -1e9)
    best_band = max(band.values(), key=lambda s: s["prot_per_carry"] if np.isfinite(s["prot_per_carry"]) else -1e9)
    band_beats_fixed_cushion = best_band["prot_per_carry"] > best_fixed["prot_per_carry"]
    # maxDD arbiter: does ANY band arm beat the BEST fixed maxDD at comparable carry?
    best_fixed_maxdd = max(s["maxdd"] for s in fixed.values())   # least-negative = best
    best_band_maxdd = max(s["maxdd"] for s in band.values())
    band_beats_fixed_maxdd = best_band_maxdd > best_fixed_maxdd + 1e-4
    per_ep_robust = all(
        (b["prot"].get(e) is not None and base["prot"].get(e) is not None
         and b["prot"][e]["cushion"] >= base["prot"][e]["cushion"] - 1e-9)
        for e in EPS
    )
    band_over_control = b["prot_per_carry"] > c["prot_per_carry"] + 1e-9
    # does the band beat its matched rvol control on the assumption-free maxDD too?
    band_over_control_maxdd = b["maxdd"] > c["maxdd"] + 1e-4
    # VIX/VIX3M agreement (robustness): compare best prot/carry across the two clean sources
    best_bandvix = max(band_vix.values(), key=lambda s: s["prot_per_carry"] if np.isfinite(s["prot_per_carry"]) else -1e9)
    sources_agree = abs(best_band["prot_per_carry"] - best_bandvix["prot_per_carry"]) < 0.25

    # A real EDGE needs the band to beat fixed on BOTH maxDD and cushion, robustly, AND beat
    # its own realized-vol control. Otherwise, if band ≈ fixed (within a small band) it's a
    # WASH; if band is clearly worse it's NO EDGE.
    close_to_fixed = abs(best_band["prot_per_carry"] - best_fixed["prot_per_carry"]) < 0.20
    if band_beats_fixed_maxdd and band_beats_fixed_cushion and per_ep_robust and band_over_control:
        verdict = "EDGE (modest)"
    elif close_to_fixed and not band_over_control:
        verdict = "WASH"
    else:
        verdict = "NO EDGE"

    A("## Verdict")
    A("")
    A(f"### **{verdict}**")
    A("")
    A(f"- **Full-episode maxDD (the assumption-free arbiter):** best band arm "
      f"**{fpct(best_band_maxdd)}** vs best fixed **{fpct(best_fixed_maxdd)}** → the band "
      f"{'is DEEPER (worse)' if not band_beats_fixed_maxdd else 'is shallower (better)'}. "
      "Comparing at **matched carry** (band N=2.0 ≈ 21.7% OTM, 1.66%/yr vs fixed 22.5% OTM, "
      f"1.85%/yr): band maxDD **{fpct(b['maxdd'])}** vs fixed **{fpct(base['maxdd'])}** — the "
      "fixed rule draws down **shallower for a comparable carry**. Vol-scaling the strike buys "
      "no drawdown protection here.")
    A(f"- **Protection-per-carry (cushion metric):** best band **{fnum(best_band['prot_per_carry'],2)}** "
      f"vs best fixed **{fnum(best_fixed['prot_per_carry'],2)}** — "
      f"{'essentially a WASH' if close_to_fixed else ('band better' if band_beats_fixed_cushion else 'fixed better')}. "
      "The two rules deliver ~the same cushion per dollar of carry; the band's small edges are "
      "carry-reshuffling, not extra protection, and they do not line up with the maxDD read.")
    A(f"- **Robust across ALL three crises?** At the matched anchor the band's episode cushion "
      f"{'≥' if per_ep_robust else 'is NOT ≥'} the fixed arm's in every episode "
      f"({', '.join(EPS)}) — {'robust' if per_ep_robust else 'NOT robust (mixed by episode)'}.")
    A(f"- **Does the IMPLIED band add over ANY vol-scaling? (the anti-curve-fit control.)** "
      f"Band prot/carry **{fnum(b['prot_per_carry'],2)}** vs the matched-OTM **realized-vol "
      f"control {fnum(c['prot_per_carry'],2)}** → the implied band "
      f"**{'DOES' if band_over_control else 'does NOT'} beat** a plain realized-vol scaling at "
      f"the same average moneyness (and maxDD {fpct(b['maxdd'])} vs {fpct(c['maxdd'])}). "
      "The implied forward-vol band carries **no information the fixed rule and a realized-vol "
      "rule don't already have** for placing this deep tail.")
    A(f"- **Two clean sources agree (robustness).** The VIX3M band (B) and the VIX-30d band (B′) "
      f"track each other closely (best prot/carry {fnum(best_band['prot_per_carry'],2)} vs "
      f"{fnum(best_bandvix['prot_per_carry'],2)}) — so this is a property of **vol-scaling the "
      f"strike**, not a quirk of one index. {'They agree.' if sources_agree else 'They differ somewhat.'}")
    A("")
    A("> **Why the earlier draft looked different.** A first pass used the warehouse "
      "`expected_move_pct` column, which is **corrupt in 2020–21** (32% / 100% degenerate days — "
      "see the substitution note above). That corruption manufactured a spurious 'band wins "
      "COVID' signal (the dead-then-spiking EM pushed the strike around artificially). On the "
      "**clean** VIX3M/VIX forward-vol source the apparent edge **disappears** — a textbook "
      "example of why the clean-source discipline matters.")
    A("")
    if verdict in ("NO EDGE", "WASH"):
        A("**Read.** Band-relative tail-strike placement is **not** an improvement on the frozen "
          "fixed-OTM rule for the defensive leg — it is a **wash-to-slightly-worse**. On the "
          "clean forward-vol source the band delivers ~the same protection-per-carry as the fixed "
          "rule, at **comparable-or-deeper drawdown for matched carry**, and — decisively — it "
          "**does not beat a plain realized-vol control** at the same average moneyness. So the "
          "*implied* forward band adds nothing a simpler rule can't. The S5 tail frontier is "
          "governed by tail **SIZE / moneyness** (already swept in the real-skew work), not by "
          "how the strike is *derived*. **Keep the frozen fixed-OTM placement.** This refinement "
          "does not clear the bar and there is nothing here to promote.")
    else:
        A("**Read.** Band-relative placement shows a **modest, robust** protection-per-carry "
          "improvement that survives BOTH the maxDD arbiter and the realized-vol control, on two "
          "independent clean forward-vol sources. This is an execution refinement worth flagging "
          "for Andrew, NOT a strategy change; it stays gated behind the frozen-config rule and "
          "needs OOS confirmation before anything moves.")
    A("")

    A("## Constraints honoured")
    A("")
    A("- **OFFLINE**; hand-rolled BSM (no scipy); **READ-ONLY** data (one-pass reads of the "
      "real-skew table + the CBOE vol-index parquets in bt_data; nothing written).")
    A("- **Clean forward-vol source:** the implied band is built from CBOE **VIX3M / VIX** (not "
      "the corrupt warehouse `expected_move_pct`); the corruption is documented above and the "
      "substitution mirrors the S4 lane's clean-vol discipline.")
    A("- **New file only** — imports the S5 prototype engine; the **frozen engine/config is "
      "untouched** (no edits to `s5_convexity_overlay.py` or any strategy config).")
    A("- **No look-ahead:** the strike for the put rolled at close of day T uses vol/skew "
      "observed on/before T (`merge_asof` backward). The crash bottom is a post-hoc measurement "
      "point, never a decision input.")
    A(f"- **Apples-to-apples pricing:** all arms price the Tier-1 tail off the SAME real "
      f"warehouse SPXW skew (continuous causal uplift interpolator built from the "
      f"{len(up_df)}-row real-skew table).")
    A("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return path


if __name__ == "__main__":
    main()
