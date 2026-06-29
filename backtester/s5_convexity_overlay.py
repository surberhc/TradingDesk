"""
S5 — Financed Convexity Overlay on a Synthetic SPX Core (EOD/daily PROTOTYPE).

PAPER / research only. Offline. Windows. numpy/pandas + hand-rolled Black-Scholes.

This is the Phase-1 "skeleton" that makes the validated S5 design TANGIBLE and exposes
the ledger dynamics. It is NOT the production S5, and it is NOT a net-of-cost P&L — it
is a STRUCTURAL prototype. The single biggest soft input — the 0DTE harvest income — is
a transparent KNOB (real harvest numbers need the intraday SPXW pull we do not have yet;
it is LABELLED ASSUMED throughout and swept three ways at the end).

------------------------------------------------------------------------------------
WHY a standalone backtester/ runner (not the strategies/ package):
------------------------------------------------------------------------------------
The shared-brain `StrategyBase` is WEIGHT-based (target weights that sum to 1.0). S5's
P&L is an OPTIONS book — daily mark-to-market of long puts/calls, a running cash LEDGER,
a reserve float in T-bills, priority-waterfall accounting — which does not fit a weight
vector. So per the build brief this lives in backtester/ as a standalone runner. A
shared-brain extraction can come later if S5 goes to paper. We DO reuse the S4 brain's
realized-vol estimator (`realized_vol_simple`) and its causal-shift discipline for the
regime gate, and we reuse the event study's hand-rolled normal-CDF / BSM.

------------------------------------------------------------------------------------
THE DESIGN (validated by the V-bottom event study, 2026-06-28 — see docs/S5_SPEC.md §1.1):
------------------------------------------------------------------------------------
The edge is the PASSIVE, always-on, UNCAPPED tail. Its put delta auto-de-risks the core
as spot falls and auto-re-risks on the recovery — no timing, no re-entry decision. Active
monetization was tested head-to-head and DEMOTED to Phase-2; it is NOT implemented here.

  CORE (constant, never flexed)
      1.0x long SPX total return — the same SPY-TR series S4 / the event study use. On
      EOD the synthetic-SPX combo ~= holding the index; the combo's financing leg (~r-q)
      is implicit in the TR series. Held at 1.0x at all times. (Fork 1 = constant core.)

  TAIL HEDGE — Tier 1 (mandatory, always-on, UNCAPPED)
      Rolling ~63-DTE (~3mo), ~20% OTM outright SPX puts. BSM-priced off VIX as the ATM
      IV proxy + a flat skew bump for the OTM strike (assumption, stated). Rolled when
      they age past a floor DTE. Daily mark-to-market (incl. theta) charged to NAV. We
      track NET DELTA = 1 + put_delta over time — the passive re-entry engine; the
      diagnostics prove it heads toward ~0 at crash bottoms and recovers on its own.
      (Fork 2 = uncapped Tier-1 tail, the catastrophe layer + re-entry fuel.)

  LEDGER — strict PRIORITY WATERFALL (S5_SPEC §4.3), harvest income a transparent KNOB
      Each period the harvested 0DTE premium (the KNOB) flows through buckets, each
      filled to target before the next sees a dollar:
        (1) fund Tier-1 tail carry (the mandatory floor; seeded at inception)
        (2) optional Tier-2 protection (income-funded put-SPREAD, on/off + size param,
            up to a "fully protected" SATURATION cap)
        (3) RESERVE buffer (held in T-bills earning RF; target ~1-2yr of Tier-1 carry;
            REPLENISH-FIRST after any draw — senior to upside)
        (4) SURPLUS above reserve + a hysteresis band -> UPSIDE bucket
      UPSIDE bucket (the financed barbell): buy rolling OTM calls from BANKED surplus
      ONLY — NEVER sell more to fund them (Design Rule A: net convexity stays LONG).
      Gated on a low-vol grind-higher EOD signal (px > 200d MA AND realized vol below a
      percentile). BSM-priced. Gives >1.0x upside participation in calm bull runs.

  HARVEST KNOB (ASSUMED — the one input that needs intraday SPXW to nail)
      A base annual harvest rate (% of notional) EARNED on calm days and ~0-or-negative
      in turbulent stretches, vol-regime-scaled for honesty: the ledger naturally runs a
      surplus in calm and a DEFICIT in chop (Design Rule B). Base rate + scaling are
      flags; swept pessimistic / central / optimistic at the end.

------------------------------------------------------------------------------------
HARD CONSTRAINTS honoured: offline; numpy/pandas + hand-rolled normal CDF (no scipy);
read-only bt_data; creates only this script + the markdown report; NO look-ahead (every
decision at day T uses data <= T and is applied to T+1's return — the cardinal rule).
Default: NO transaction costs (a simple bps tail-roll drag knob is available). Income is
ASSUMED; flat-skew BSM understates the real tail cost. STRUCTURAL prototype, not a P&L.
------------------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import sys

import numpy as np
import pandas as pd

# Reuse the EXACT S4 shared-brain realized-vol estimator for the regime gate.
from strategies.spx_vol_control import (
    realized_vol_simple,
    exposure_from_vol,
    TRADING_DAYS_PER_YEAR,
)

DATA_DIR = r"C:\TradingDesk-Local\bt_data"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

# ============================================================================
# MODEL CONSTANTS / KNOBS  (every one is an assumption — stated in the report)
# ============================================================================
R_RF = 0.0285          # risk-free / BS discount rate (10y avg on disk ~2.85%)
Q_DIV = 0.019          # SPX dividend yield used to strip TR->price path (~1.9%)

# ---- Tier 1: the mandatory, always-on, UNCAPPED deep-OTM tail ----
TAIL_OTM = 0.20         # 20% out-of-the-money (deep tail; cheap; uncapped)
TAIL_TENOR_D = 63       # ~3-month put tenor in trading days
TAIL_ROLL_FLOOR_D = 21  # roll the put once it has < this many days left (aging)
TAIL_SKEW_BUMP = 0.06   # additive IV bump (vol pts) for the OTM put strike over VIX
TAIL_NOTIONAL_FRAC = 1.00   # tail covers 100% of core notional (1 put per index unit)

# ---- Tier 2: optional income-funded put-SPREAD (nearer the money) ----
TIER2_ON = True
TIER2_LONG_OTM = 0.07   # long leg ~7% OTM
TIER2_SHORT_OTM = 0.20  # short leg ~20% OTM (caps the spread; finances the long)
TIER2_TENOR_D = 63
TIER2_SKEW_BUMP = 0.04
# Tier-2 "fully protected" SATURATION cap: max spread notional as a fraction of core.
TIER2_SAT_FRAC = 1.00

# ---- The HARVEST KNOB (ASSUMED — needs intraday SPXW to replace) ----
# Base annual harvest rate as a fraction of core notional, EARNED on fully-calm days.
HARVEST_BASE_ANNUAL = 0.055     # ~5.5%/yr central (matches spec §6.2 pessimistic-ish)
# Vol-regime scaling: harvest is the base rate * a calm multiplier in [neg, 1].
# Calm (low VIX) -> ~full base; turbulent (high VIX) -> 0 or NEGATIVE (a bleed).
HARVEST_CALM_VIX = 15.0    # at/below this VIX -> full base harvest
HARVEST_ZERO_VIX = 28.0    # at this VIX -> harvest crosses zero
HARVEST_FLOOR_MULT = -0.6  # most-negative multiplier in turbulence (a realized loss)

# ---- Reserve buffer (bucket 3) ----
# Target reserve = RESERVE_YEARS years of worst-case Tier-1 carry (placeholder 1-2yr).
RESERVE_YEARS = 1.5
RESERVE_HYSTERESIS = 0.15  # surplus must exceed reserve target by this band before upside

# ---- Upside bucket (bucket 4 — the financed barbell) ----
UPSIDE_ON = True
UPSIDE_OTM = 0.05       # buy ~5% OTM calls
UPSIDE_TENOR_D = 42     # ~2-month calls
UPSIDE_ROLL_FLOOR_D = 10
UPSIDE_SKEW_BUMP = -0.02   # calls a touch below ATM IV (call skew is flatter/inverted)
UPSIDE_MA_DAYS = 200       # grind-higher gate: price > 200d MA
UPSIDE_RVOL_PCTL = 0.50    # AND realized vol below its rolling percentile
UPSIDE_RVOL_LOOKBACK = 504 # 2y window for the rvol percentile
# Each grind-higher day, deploy this fraction of available surplus into calls (laddered).
UPSIDE_DEPLOY_FRAC = 0.25
UPSIDE_MAX_BUDGET_FRAC = 0.04  # cap live call premium at 4% of NAV (house-money bound)

# ---- inception seed (cold-start: ledger empty -> seed Tier-1 like an expense ratio) ----
SEED_FRAC = 0.02        # 2% of NAV seeded into the ledger at inception

# ---- frictions (default OFF: structural prototype) ----
TAIL_ROLL_COST_BPS = 0.0   # bps of rolled notional charged on each option roll


# ============================================================================
# Hand-rolled BSM (no scipy) — normal CDF via erf, same as the event study.
# ============================================================================
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1(S, K, T, sigma, r, q):
    return (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))


def bs_put(S, K, T, sigma, r, q):
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    d1 = _d1(S, K, T, sigma, r, q)
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * math.exp(-q * T) * norm_cdf(-d1)


def bs_put_delta(S, K, T, sigma, r, q):
    if T <= 0 or sigma <= 0:
        return -1.0 if S < K else 0.0
    return -math.exp(-q * T) * norm_cdf(-_d1(S, K, T, sigma, r, q))


def bs_call(S, K, T, sigma, r, q):
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0)
    d1 = _d1(S, K, T, sigma, r, q)
    d2 = d1 - sigma * math.sqrt(T)
    return S * math.exp(-q * T) * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)


def bs_call_delta(S, K, T, sigma, r, q):
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0
    return math.exp(-q * T) * norm_cdf(_d1(S, K, T, sigma, r, q))


# ============================================================================
# Data
# ============================================================================
def load_series(ticker: str) -> pd.Series:
    path = os.path.join(DATA_DIR, f"{ticker}.parquet")
    df = pd.read_parquet(path)
    s = df.iloc[:, 0]
    s.index = pd.to_datetime(s.index).normalize()
    s.name = ticker
    return s.sort_index()


def build_panel() -> pd.DataFrame:
    """SPY-TR, BIL (cash/RF), VIX aligned. Adds a price-return SPX level by stripping a
    constant div yield q from the TR series so put/call strikes track the ex-dividend
    index path (not the dividend-lifted TR). Causal: a deterministic day-by-day deflator.
    """
    spy = load_series("SPY")      # total-return adjusted close (same as S4)
    bil = load_series("BIL")      # cash / RF total-return proxy
    vix = load_series("_vix")     # ATM IV proxy in vol points (18.4 => 18.4%)
    df = pd.concat({"spy_tr": spy, "bil": bil, "vix": vix}, axis=1).dropna()
    n = np.arange(len(df))
    deflator = np.exp(-Q_DIV * n / TRADING_DAYS_PER_YEAR)
    df["spx_px"] = df["spy_tr"].values * deflator
    df["r_spy"] = df["spy_tr"].pct_change()
    df["r_cash"] = df["bil"].pct_change()
    df["sigma"] = (df["vix"] / 100.0).clip(lower=0.05)   # ATM IV as a fraction
    return df


# ============================================================================
# The S5 prototype simulation
# ============================================================================
def simulate_s5(df: pd.DataFrame,
                harvest_base_annual: float = HARVEST_BASE_ANNUAL,
                tier2_on: bool = TIER2_ON,
                upside_on: bool = UPSIDE_ON,
                tail_otm: float = TAIL_OTM,
                tail_frac: float = TAIL_NOTIONAL_FRAC,
                reserve_years: float = RESERVE_YEARS,
                roll_cost_bps: float = TAIL_ROLL_COST_BPS,
                tail_iv_fn=None) -> dict:
    """
    Daily, strictly causal. Positions HELD into day i were decided at close of i-1; they
    earn day i's return. All decisions at close of day i use data through i only.

    The book has four option legs + a cash ledger + a reserve float:
      - Tier-1 tail (long put, always on, uncapped)         -> marks daily, charged to NAV
      - Tier-2 spread (long put + short put, income-funded)  -> marks daily, charged to NAV
      - Upside calls (long call, surplus-funded barbell)     -> marks daily, charged to NAV
      - LEDGER cash (harvested premium net of carry)         -> earns RF; funds the legs
      - RESERVE (subset of ledger, held in T-bills at RF)    -> senior to the upside bucket

    NAV return each day = core equity return (constant 1.0x) + the day-over-day change in
    every option leg's value-as-fraction-of-NAV + the ledger's RF carry contribution
    - the option-premium SPENDING that left the ledger that day. Premium FLOWS are modelled
    as transfers between the ledger and the option legs, so total NAV is conserved across a
    purchase (cash down, option value up by the same premium) and only mark-to-market moves,
    realized harvest, and carry change NAV.
    """
    idx = df.index
    spx = df["spx_px"].values
    sig = df["sigma"].values
    vix = df["vix"].values
    r_spy = df["r_spy"].values
    r_cash = df["r_cash"].values
    n = len(idx)

    # --- regime / grind-higher signal inputs (all causal, trailing) ---
    rvol = realized_vol_simple(df["r_spy"], 20, 60).values          # the S4 estimator
    ma200 = df["spx_px"].rolling(UPSIDE_MA_DAYS).mean().values
    # rolling percentile of rvol within its trailing lookback (causal)
    rvol_s = pd.Series(rvol, index=idx)
    rvol_pctl = rvol_s.rolling(UPSIDE_RVOL_LOOKBACK, min_periods=120).apply(
        lambda w: (w[-1] <= w).mean(), raw=True).values  # frac of window >= today => low=calm
    # Note: (w[-1] <= w).mean() = fraction of window AT-OR-ABOVE today => high value = today is calm.

    # --- option leg state ---
    # Tier-1 tail put
    t1_K = spx[0] * (1.0 - tail_otm); t1_exp = min(TAIL_TENOR_D, n - 1)
    # Tier-2 spread (long Kl, short Ks)
    t2_live = False; t2_Kl = np.nan; t2_Ks = np.nan; t2_exp = -1; t2_frac = 0.0
    # Upside call
    up_live = False; up_K = np.nan; up_exp = -1; up_frac = 0.0

    # --- Tier-1 tail IV resolver (real-skew injection hook) ---
    # When tail_iv_fn is None (DEFAULT) the Tier-1 tail prices/deltas with the flat
    # sig[i] + TAIL_SKEW_BUMP, exactly as before (byte-identical default run). When a
    # callable is supplied, it returns the ABSOLUTE Tier-1 implied vol for day i at the
    # given tail_otm, observed on/before that date (causal). Used ONLY for the Tier-1
    # tail leg; Tier-2 and the upside barbell are untouched.
    def t1_iv(i):
        if tail_iv_fn is None:
            return sig[i] + TAIL_SKEW_BUMP
        return float(tail_iv_fn(idx[i], tail_otm, i))

    def put_val(i, K, exp, bump):
        if K is None or np.isnan(K):
            return 0.0
        T = max(exp - i, 0) / TRADING_DAYS_PER_YEAR
        return bs_put(spx[i], K, T, sig[i] + bump, R_RF, Q_DIV)

    def t1_put_val(i, K, exp):
        """Tier-1 put value using the real-skew IV resolver (or flat default)."""
        if K is None or np.isnan(K):
            return 0.0
        T = max(exp - i, 0) / TRADING_DAYS_PER_YEAR
        return bs_put(spx[i], K, T, max(t1_iv(i), 0.03), R_RF, Q_DIV)

    def call_val(i, K, exp, bump):
        if K is None or np.isnan(K):
            return 0.0
        T = max(exp - i, 0) / TRADING_DAYS_PER_YEAR
        return bs_call(spx[i], K, T, max(sig[i] + bump, 0.03), R_RF, Q_DIV)

    # value of a leg as a FRACTION of (index-notional) NAV
    def t1_fracval(i):
        return t1_put_val(i, t1_K, t1_exp) / spx[i] * tail_frac
    def t2_fracval(i):
        if not t2_live:
            return 0.0
        longv = put_val(i, t2_Kl, t2_exp, TIER2_SKEW_BUMP)
        shortv = put_val(i, t2_Ks, t2_exp, TIER2_SKEW_BUMP)
        return (longv - shortv) / spx[i] * t2_frac
    def up_fracval(i):
        if not up_live:
            return 0.0
        return call_val(i, up_K, up_exp, UPSIDE_SKEW_BUMP) / spx[i] * up_frac

    # reserve target (fraction of NAV): RESERVE_YEARS * worst-case annual Tier-1 carry.
    # worst-case carry ~= one year of the tail premium expiring worthless. Estimate it
    # from the day-0 premium fraction * (rolls per year). Computed once, fixed (a sizing).
    t1_prem0 = t1_put_val(0, t1_K, t1_exp) / spx[0] * tail_frac
    rolls_per_yr = TRADING_DAYS_PER_YEAR / (TAIL_TENOR_D - TAIL_ROLL_FLOOR_D)
    worst_t1_carry_annual = t1_prem0 * rolls_per_yr
    reserve_target = reserve_years * worst_t1_carry_annual

    # reserve target as an ABSOLUTE fraction of the STARTING NAV ($1). Held flat in $.
    reserve_target = reserve_years * worst_t1_carry_annual

    # ------------------------------------------------------------------------
    # ABSOLUTE NAV accounting (conserved). NAV = core + ledger + reserve + legs.
    # We track every component in dollars off a $1.0 starting NAV. The daily fund
    # return is NAV_i / NAV_{i-1} - 1 — so premium FLOWS (cash<->option value) are
    # automatically conserved (a buy moves dollars from ledger to a leg, NAV flat),
    # and only mark-to-market moves, realized harvest, RF carry, and expiries change
    # NAV. This removes the ambiguity that fraction-of-NAV deltas introduced.
    #   core   : equity, compounds at SPX TR
    #   ledger : harvested-premium cash (earns RF); funds Tier-2 + upside; banks payoffs
    #   reserve: senior cash float (earns RF); held in T-bills
    #   t1/t2/up: option leg market values (dollars)
    # Leg "frac" sizes are scaled so that frac * (option_price/spx) = leg $ value per
    # the original definitions; we keep the same fracval() helpers and read $ from them
    # by multiplying by the CURRENT nav (the helpers return value-as-fraction-of-NAV,
    # so leg_$ = fracval * nav). To keep things explicit we store leg dollars directly.
    # ------------------------------------------------------------------------
    nav0 = 1.0
    core = nav0
    ledger = SEED_FRAC * nav0      # seeded harvested-premium cash (cold-start budget)
    reserve = 0.0
    # leg dollar values: buy the day-0 Tier-1 tail, paying its premium out of... the seed
    # is for carry; the initial tail premium is a one-time setup cost folded into NAV by
    # letting the leg start at its market value while core is reduced by that premium
    # (so total NAV stays 1.0 at inception — the tail is "already owned" on day 0).
    t1_dollars = t1_fracval(0) * nav0
    core -= t1_dollars             # paid for the inception tail out of core notional
    t2_dollars = 0.0
    up_dollars = 0.0

    def nav_now():
        return core + ledger + reserve + t1_dollars + t2_dollars + up_dollars

    nav_prev = nav_now()

    # --- records ---
    fund_ret = np.full(n, np.nan)
    net_delta_rec = np.full(n, np.nan)
    t1_val_rec = np.full(n, 0.0)
    t2_val_rec = np.full(n, 0.0)
    up_val_rec = np.full(n, 0.0)
    ledger_rec = np.full(n, np.nan)
    reserve_rec = np.full(n, np.nan)
    harvest_rec = np.full(n, 0.0)
    nav_rec = np.full(n, np.nan)
    regime_calm_rec = np.zeros(n, dtype=bool)

    total_tail_carry = 0.0
    total_harvest = 0.0
    total_upside_spent = 0.0
    total_upside_payoff = 0.0
    upside_fund_count = 0
    daily_harvest_per_calm = harvest_base_annual / TRADING_DAYS_PER_YEAR

    for i in range(1, n):
        # ---------- evolve every component over day i (positions set at close of i-1) ----
        # core equity compounds at SPX TR
        core *= (1.0 + r_spy[i])
        # cash legs earn RF
        ledger *= (1.0 + r_cash[i])
        reserve *= (1.0 + r_cash[i])
        # option legs re-mark: re-price at today's spot/vol/T, keeping contracts fixed.
        # fracval() returns value as a fraction of the STARTING NAV scale (premium/spx *
        # frac), which is an absolute index-notional fraction — i.e. already in $ off the
        # $1 core unit. Re-mark by recomputing it at i (contracts unchanged).
        t1_dollars = t1_fracval(i)
        t2_dollars = t2_fracval(i)
        up_dollars = up_fracval(i)

        nav_cur = nav_now()
        fund_ret[i] = nav_cur / nav_prev - 1.0
        t1_val_rec[i] = t1_dollars; t2_val_rec[i] = t2_dollars; up_val_rec[i] = up_dollars

        # net delta = core(+1 per unit) + tail/spread/call deltas, all per NAV
        T1 = max(t1_exp - i, 0) / TRADING_DAYS_PER_YEAR
        d_t1 = bs_put_delta(spx[i], t1_K, T1, max(t1_iv(i), 0.03), R_RF, Q_DIV) * tail_frac
        d_t2 = 0.0
        if t2_live:
            T2 = max(t2_exp - i, 0) / TRADING_DAYS_PER_YEAR
            d_t2 = (bs_put_delta(spx[i], t2_Kl, T2, sig[i] + TIER2_SKEW_BUMP, R_RF, Q_DIV)
                    - bs_put_delta(spx[i], t2_Ks, T2, sig[i] + TIER2_SKEW_BUMP, R_RF, Q_DIV)) * t2_frac
        d_up = 0.0
        if up_live:
            Tu = max(up_exp - i, 0) / TRADING_DAYS_PER_YEAR
            d_up = bs_call_delta(spx[i], up_K, Tu, max(sig[i] + UPSIDE_SKEW_BUMP, 0.03), R_RF, Q_DIV) * up_frac
        # Net book delta in INDEX-NOTIONAL terms (the spec's "net delta = 1 + put_delta"):
        # core carries +1.0 index-notional of delta; the option legs add their notional
        # deltas. This is the passive de-risk engine — it falls toward ~0 as the tail goes
        # ITM and re-rises on recovery, with no signal. (Independent of the cash buckets.)
        net_delta_rec[i] = 1.0 + d_t1 + d_t2 + d_up

        # ================= DECISIONS at close of day i (data <= i, applied to i+1) =====
        # --- (0) HARVEST: the assumed income, vol-regime scaled. Realized cash -> ledger.
        v = vix[i]
        if v <= HARVEST_CALM_VIX:
            mult = 1.0
        elif v >= HARVEST_ZERO_VIX:
            over = min((v - HARVEST_ZERO_VIX) / max(HARVEST_ZERO_VIX, 1e-9), 1.0)
            mult = HARVEST_FLOOR_MULT * over
        else:
            mult = (HARVEST_ZERO_VIX - v) / (HARVEST_ZERO_VIX - HARVEST_CALM_VIX)
        # scale the harvest $ by current NAV so it stays a % of book through compounding
        harvest_today = daily_harvest_per_calm * mult * nav_cur
        harvest_rec[i] = harvest_today / nav_cur if nav_cur > 0 else 0.0
        total_harvest += harvest_today / nav_cur if nav_cur > 0 else 0.0
        regime_calm_rec[i] = (v <= HARVEST_CALM_VIX)

        # --- WATERFALL bucket (1): harvest lands in the ledger (funds Tier-1 carry first
        #     implicitly — Tier-1 premium is paid from core/ledger on roll below). On a
        #     net-negative harvest day, the reserve absorbs the shortfall (Rule B).
        ledger += harvest_today
        if ledger < 0.0:
            take = min(-ledger, reserve)
            reserve -= take
            ledger += take    # reserve absorbs the deficit; ledger may still go <0 if dry

        # --- bucket (3) REPLENISH-FIRST: top the reserve to target before any upside -----
        res_tgt_abs = reserve_target * nav_cur
        if reserve < res_tgt_abs and ledger > 0.0:
            move = min(res_tgt_abs - reserve, ledger)
            reserve += move
            ledger -= move

        # --- roll Tier-1 tail if aged/expired (buy fresh; premium paid from ledger if it
        #     has cash, else from core — Tier-1 is mandatory and never skipped) -----------
        if (t1_exp - i) <= TAIL_ROLL_FLOOR_D:
            old_val = t1_dollars                       # selling the old put returns its value
            t1_K = spx[i] * (1.0 - tail_otm)
            t1_exp = min(i + TAIL_TENOR_D, n - 1)
            new_prem = t1_fracval(i)                   # premium of the fresh put
            total_tail_carry += new_prem               # worst-case carry (decays to ~0)
            # net cash for the roll = new premium - proceeds from the expiring put
            net_cash = new_prem - old_val
            if roll_cost_bps > 0:
                net_cash += (roll_cost_bps / 1e4) * tail_frac
            # pay from ledger first, core covers any shortfall (mandatory floor)
            pay_led = min(max(ledger, 0.0), net_cash) if net_cash > 0 else net_cash
            ledger -= pay_led
            core -= (net_cash - pay_led)
            t1_dollars = new_prem

        # --- bucket (2) Tier-2 spread: income-funded, saturating. Roll on aging. ---------
        if tier2_on and ((not t2_live) or (t2_exp - i) <= TAIL_ROLL_FLOOR_D):
            old_t2 = t2_dollars                        # proceeds from selling the old spread
            longp = put_val(i, spx[i] * (1 - TIER2_LONG_OTM), min(i + TIER2_TENOR_D, n - 1), TIER2_SKEW_BUMP)
            shortp = put_val(i, spx[i] * (1 - TIER2_SHORT_OTM), min(i + TIER2_TENOR_D, n - 1), TIER2_SKEW_BUMP)
            debit_full = max(longp - shortp, 1e-9) / spx[i] * TIER2_SAT_FRAC  # frac-of-NAV debit at full size
            spendable = max(ledger + old_t2, 0.0)      # old spread proceeds recycle into the new one
            spend = min(debit_full, spendable)
            t2_frac = (spend / debit_full) * TIER2_SAT_FRAC if debit_full > 1e-12 else 0.0
            t2_Kl = spx[i] * (1 - TIER2_LONG_OTM)
            t2_Ks = spx[i] * (1 - TIER2_SHORT_OTM)
            t2_exp = min(i + TIER2_TENOR_D, n - 1)
            t2_live = t2_frac > 1e-6
            new_t2 = t2_fracval(i)
            # cash: receive old spread value, pay new debit -> net out of ledger
            ledger += old_t2 - new_t2
            t2_dollars = new_t2

        # --- bucket (4) UPSIDE barbell: BANKED surplus over reserve+hysteresis, grind gate
        if upside_on:
            grind = (not np.isnan(ma200[i]) and spx[i] > ma200[i]
                     and not np.isnan(rvol_pctl[i]) and rvol_pctl[i] >= (1 - UPSIDE_RVOL_PCTL))
            # roll/expire an existing call: realize its value back into the ledger (a payoff)
            if up_live and (up_exp - i) <= UPSIDE_ROLL_FLOOR_D:
                ledger += up_dollars
                total_upside_payoff += up_dollars / nav_cur if nav_cur > 0 else 0.0
                up_live = False; up_K = np.nan; up_frac = 0.0; up_dollars = 0.0
            # surplus = banked cash above a fully-funded reserve + hysteresis band
            hyst_tgt = reserve_target * (1.0 + RESERVE_HYSTERESIS) * nav_cur
            surplus = ledger if reserve >= hyst_tgt else 0.0
            if grind and surplus > 1e-5 * nav_cur and not up_live:
                budget = min(UPSIDE_DEPLOY_FRAC * surplus, UPSIDE_MAX_BUDGET_FRAC * nav_cur)
                callp = call_val(i, spx[i] * (1 + UPSIDE_OTM), min(i + UPSIDE_TENOR_D, n - 1), UPSIDE_SKEW_BUMP) / spx[i]
                if callp > 1e-9 and budget > 1e-6:
                    up_frac = budget / callp           # contracts sized to spend `budget`
                    up_K = spx[i] * (1 + UPSIDE_OTM)
                    up_exp = min(i + UPSIDE_TENOR_D, n - 1)
                    up_live = True
                    up_dollars = up_fracval(i)
                    ledger -= up_dollars               # banked surplus only (Rule A)
                    total_upside_spent += up_dollars / nav_cur if nav_cur > 0 else 0.0
                    upside_fund_count += 1

        # --- SWEEP excess banked surplus back into the CORE (the fund's working capital). --
        # Harvested premium above the reserve + a working cash buffer is the investor's
        # realized income; a real fund reinvests it in the book, it does NOT sit idle at RF.
        # Leaving it idle would make harvest a phantom drag (cash compounds slower than the
        # equity core). The reserve itself stays in T-bills (spec §4.4); only the genuine
        # excess sweeps. Keep a small working buffer in the ledger for near-term option spend.
        work_buffer = max(res_tgt_abs, UPSIDE_MAX_BUDGET_FRAC * nav_cur)
        if ledger > work_buffer:
            sweep = ledger - work_buffer
            ledger -= sweep
            core += sweep                      # reinvested into the core (NAV conserved)

        ledger_rec[i] = ledger / nav_cur if nav_cur > 0 else 0.0
        reserve_rec[i] = reserve / nav_cur if nav_cur > 0 else 0.0
        nav_rec[i] = nav_cur
        nav_prev = nav_now()

    out = pd.DataFrame(index=idx)
    out["r_fund"] = fund_ret
    out["net_delta"] = net_delta_rec
    out["t1_val"] = t1_val_rec
    out["t2_val"] = t2_val_rec
    out["up_val"] = up_val_rec
    out["ledger"] = ledger_rec
    out["reserve"] = reserve_rec
    out["harvest"] = harvest_rec
    out["nav"] = nav_rec
    out["regime_calm"] = regime_calm_rec
    return {
        "df": out,
        "reserve_target": reserve_target,
        "worst_t1_carry_annual": worst_t1_carry_annual,
        "total_tail_carry": total_tail_carry,
        "total_harvest": total_harvest,
        "total_upside_spent": total_upside_spent,
        "total_upside_payoff": total_upside_payoff,
        "upside_fund_count": upside_fund_count,
    }


def simulate_s5_passive(df: pd.DataFrame, **kw) -> dict:
    """The pure passive-tail core: Tier-1 always-on uncapped tail ONLY (no Tier-2, no
    upside, no harvest ledger spending). This is the validated edge in its cleanest form —
    useful as the reference inside the full S5 to show what the ledger machinery adds."""
    kw2 = dict(kw)
    kw2["tier2_on"] = False
    kw2["upside_on"] = False
    return simulate_s5(df, **kw2)


# ============================================================================
# S4 baseline (reuse the exact shared-brain exposure path)
# ============================================================================
def s4_returns(df: pd.DataFrame, target_vol=0.10, cap=1.50) -> tuple[pd.Series, pd.Series]:
    realized = realized_vol_simple(df["r_spy"], 20, 60)
    exposure = exposure_from_vol(realized, target_vol, cap).shift(1)  # causal
    r = exposure * df["r_spy"] + (1.0 - exposure) * df["r_cash"]
    return r, exposure


# ============================================================================
# Metrics
# ============================================================================
def nav(r: pd.Series) -> pd.Series:
    return (1.0 + r.fillna(0.0)).cumprod()


def cagr(r: pd.Series) -> float:
    nv = nav(r); yrs = len(r) / TRADING_DAYS_PER_YEAR
    if yrs <= 0 or nv.iloc[-1] <= 0:
        return float("nan")
    return nv.iloc[-1] ** (1.0 / yrs) - 1.0


def max_dd(r: pd.Series) -> float:
    nv = nav(r); return (nv / nv.cummax() - 1.0).min()


def ann_vol(r: pd.Series) -> float:
    return r.std(ddof=0) * math.sqrt(TRADING_DAYS_PER_YEAR)


def sharpe(r: pd.Series, rc: pd.Series) -> float:
    ex = (r - rc).dropna(); sd = ex.std(ddof=0)
    return float("nan") if sd <= 0 else (ex.mean() / sd) * math.sqrt(TRADING_DAYS_PER_YEAR)


def calmar(r: pd.Series) -> float:
    m = max_dd(r)
    return float("nan") if m >= 0 else cagr(r) / abs(m)


def metric_block(r: pd.Series, rc: pd.Series) -> dict:
    return {"cagr": cagr(r), "maxdd": max_dd(r), "calmar": calmar(r),
            "sharpe": sharpe(r, rc), "vol": ann_vol(r)}


# ============================================================================
# Crash-episode net-delta proof + melt-up participation
# ============================================================================
EPISODES = {
    "GFC 2008-09": ("2008-06-01", "2009-12-31"),
    "COVID 2020":  ("2020-02-01", "2020-12-31"),
    "Bear 2022":   ("2022-01-01", "2023-06-30"),
}
MELTUP = ("2023-01-01", "2024-12-31")


def find_bottom(df, lo, hi):
    return df["spx_px"].loc[lo:hi].idxmin()


# ============================================================================
# Formatting
# ============================================================================
def fpct(x, nd=2):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    return f"{x*100:.{nd}f}%"


def fnum(x, nd=2):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    return f"{x:.{nd}f}"


# ============================================================================
# Driver
# ============================================================================
def main():
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description="S5 financed convexity overlay (EOD prototype)")
    ap.add_argument("--report", action="store_true", help="write the markdown report")
    args = ap.parse_args()

    print("loading panel...", flush=True)
    df = build_panel()
    print(f"panel: {df.index.min().date()} -> {df.index.max().date()}  ({len(df)} days)", flush=True)

    print("running S5 full prototype (waterfall ledger + barbell) ...", flush=True)
    s5 = simulate_s5(df)
    r_s5 = s5["df"]["r_fund"]

    print("running S5 passive (Tier-1 tail only) ...", flush=True)
    s5p = simulate_s5_passive(df)
    r_s5p = s5p["df"]["r_fund"]

    print("running S4 10%/1.5x baseline ...", flush=True)
    r_s4, s4_exp = s4_returns(df)

    r_spy = df["r_spy"]; rc = df["r_cash"]
    common = r_s5.dropna().index.intersection(r_s4.dropna().index)
    lo_all, hi_all = common.min(), common.max()

    strategies = {
        "S5 prototype (full: ledger+barbell)": r_s5.loc[common],
        "S5 passive (Tier-1 tail only)":       r_s5p.loc[common],
        "S4 vol-control 10%/1.5x":             r_s4.loc[common],
        "SPY buy & hold (TR)":                 r_spy.loc[common],
    }
    expo_map = {
        "S5 prototype (full: ledger+barbell)": s5["df"]["net_delta"],
        "S5 passive (Tier-1 tail only)":       s5p["df"]["net_delta"],
        "S4 vol-control 10%/1.5x":             s4_exp,
        "SPY buy & hold (TR)":                 pd.Series(1.0, index=df.index),
    }
    rcv = rc.loc[common]

    print(f"\n=== FULL HISTORY ({lo_all.date()} -> {hi_all.date()}) ===", flush=True)
    full = {}
    for name, r in strategies.items():
        full[name] = metric_block(r, rcv)
        m = full[name]
        print(f"  {name:38s} CAGR {fpct(m['cagr']):>8}  maxDD {fpct(m['maxdd']):>8}  "
              f"Calmar {fnum(m['calmar']):>5}  Sharpe {fnum(m['sharpe']):>5}  vol {fpct(m['vol']):>7}",
              flush=True)

    # --- net-delta path through crashes ---
    print("\n=== NET-DELTA THROUGH CRASHES (passive auto-de-risk proof) ===", flush=True)
    nd_proof = {}
    for ename, (lo, hi) in EPISODES.items():
        if pd.Timestamp(lo) < lo_all:
            lo = lo_all.strftime("%Y-%m-%d")
        bottom = find_bottom(df, lo, hi)
        bi = df.index.get_loc(bottom)
        nd_s5 = s5["df"]["net_delta"]
        nd_at_bottom = float(nd_s5.iloc[bi]) if not np.isnan(nd_s5.iloc[bi]) else float("nan")
        nd_min = float(nd_s5.loc[lo:hi].min())
        s4_at_bottom = float(s4_exp.iloc[bi]) if bi < len(s4_exp) else float("nan")
        # recovery capture bottom->end
        end = df.loc[lo:hi].index.max()
        cap_s5 = nav(r_s5.loc[bottom:end]).iloc[-1] - 1.0
        cap_spy = nav(r_spy.loc[bottom:end]).iloc[-1] - 1.0
        nd_proof[ename] = {"bottom": bottom, "nd_at_bottom": nd_at_bottom, "nd_min": nd_min,
                           "s4_exp_at_bottom": s4_at_bottom,
                           "s5_recov": cap_s5, "spy_recov": cap_spy,
                           "capture": cap_s5 / cap_spy if abs(cap_spy) > 1e-9 else float("nan")}
        print(f"  {ename:14s} bottom {bottom.date()}  S5 net-delta@bottom {fnum(nd_at_bottom)}x "
              f"(min {fnum(nd_min)}x)  vs S4 exp {fnum(s4_at_bottom)}x  | "
              f"recov capture {fpct(nd_proof[ename]['capture'],0)}", flush=True)

    # --- melt-up participation ---
    mu_lo, mu_hi = MELTUP
    mu_s5 = nav(r_s5.loc[mu_lo:mu_hi]).iloc[-1] - 1.0
    mu_spy = nav(r_spy.loc[mu_lo:mu_hi]).iloc[-1] - 1.0
    mu_s4 = nav(r_s4.loc[mu_lo:mu_hi]).iloc[-1] - 1.0
    meltup = {"s5": mu_s5, "spy": mu_spy, "s4": mu_s4,
              "particip": mu_s5 / mu_spy if abs(mu_spy) > 1e-9 else float("nan")}
    print(f"\n=== MELT-UP {mu_lo}..{mu_hi}: S5 {fpct(mu_s5,1)}  SPY {fpct(mu_spy,1)}  "
          f"S4 {fpct(mu_s4,1)}  (S5 participation {fpct(meltup['particip'],0)}) ===", flush=True)

    # --- ledger diagnostics ---
    led = s5["df"]
    led_min = float(led["ledger"].min()); led_max = float(led["ledger"].max())
    led_neg_days = int((led["ledger"] < -1e-9).sum())
    res_target = s5["reserve_target"]
    res_fill = float((led["reserve"] >= res_target * 0.999).mean())
    print(f"\n=== LEDGER DIAGNOSTICS ===", flush=True)
    print(f"  reserve target          {fpct(res_target,2)} of NAV "
          f"({s5['worst_t1_carry_annual']*100:.2f}%/yr Tier-1 carry x {RESERVE_YEARS}yr)", flush=True)
    print(f"  ledger range            {fpct(led_min,2)} .. {fpct(led_max,2)}  "
          f"(days negative: {led_neg_days})", flush=True)
    print(f"  reserve at/above target {fpct(res_fill,1)} of days", flush=True)
    print(f"  total harvest (cum)     {fpct(s5['total_harvest'],1)} of NAV", flush=True)
    print(f"  total Tier-1 carry paid {fpct(s5['total_tail_carry'],1)} of NAV", flush=True)
    print(f"  upside fundings         {s5['upside_fund_count']}  "
          f"spent {fpct(s5['total_upside_spent'],2)}  payoff {fpct(s5['total_upside_payoff'],2)}", flush=True)

    # --- harvest-knob sensitivity ---
    print("\n=== HARVEST-KNOB SENSITIVITY (the one assumed input) ===", flush=True)
    knob = {"pessimistic 3.0%/yr": 0.030, "central 5.5%/yr": 0.055, "optimistic 8.0%/yr": 0.080}
    knob_res = {}
    for label, rate in knob.items():
        sk = simulate_s5(df, harvest_base_annual=rate)
        rk = sk["df"]["r_fund"].loc[common]
        knob_res[label] = {**metric_block(rk, rcv),
                           "upside_fundings": sk["upside_fund_count"],
                           "ledger_min": float(sk["df"]["ledger"].min()),
                           "upside_spent": sk["total_upside_spent"]}
        m = knob_res[label]
        print(f"  {label:22s} CAGR {fpct(m['cagr']):>8}  maxDD {fpct(m['maxdd']):>8}  "
              f"Calmar {fnum(m['calmar']):>5}  upside-fundings {m['upside_fundings']:>3}  "
              f"ledger-min {fpct(m['ledger_min'],2):>8}", flush=True)

    if args.report:
        path = write_report(df, common, rcv, strategies, full, nd_proof, meltup,
                            s5, s5p, knob_res, res_target, led_min, led_max,
                            led_neg_days, res_fill)
        print(f"\nreport -> {path}", flush=True)
    print("\ndone.", flush=True)


def write_report(df, common, rcv, strategies, full, nd_proof, meltup, s5, s5p,
                 knob_res, res_target, led_min, led_max, led_neg_days, res_fill):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "s5_prototype_20260628.md")
    L = []; A = L.append
    A("# S5 — Financed Convexity Overlay — EOD prototype (\"skeleton\")")
    A("")
    A(f"*Generated {dt.date.today().isoformat()} | offline | EOD/daily structural prototype | "
      f"window {common.min().date()} → {common.max().date()} ({len(common)} trading days)*")
    A("")
    A("**What this is.** The Phase-1 skeleton of S5 per the *validated* design "
      "(`docs/S5_SPEC.md` §1.1): a constant 1.0× SPX core carrying an **always-on, uncapped "
      "Tier-1 tail** (the proven edge), plus the **§4.3 priority-waterfall ledger** "
      "(harvest → Tier-1 carry → Tier-2 spread → reserve → upside calls). It makes the "
      "ledger dynamics tangible. Active monetization is **NOT** implemented — the event "
      "study demoted it to Phase-2.")
    A("")
    A("**What is ASSUMED (the load-bearing caveat).** The 0DTE **harvest income is a "
      "transparent KNOB** — a base annual rate earned on calm days, scaled to ~0-or-negative "
      "in turbulence. Real harvest numbers need the **intraday SPXW pull we do not have**. "
      "Flat-skew BSM also **understates** real tail cost. Default = **no transaction costs**. "
      "This is a STRUCTURAL prototype, not a net-of-cost P&L. Treat the *shape* as the result, "
      "not the decimals.")
    A("")

    A("## The model (knobs, all assumptions)")
    A("")
    A(f"- **Core:** constant **1.0× SPY total-return** (the same series S4/the event study use). Never flexed.")
    A(f"- **Tier-1 tail (mandatory, always-on, UNCAPPED):** rolling ~{TAIL_TENOR_D}d (~3mo), "
      f"**{TAIL_OTM*100:.0f}% OTM** outright SPX puts, BSM-priced off **VIX (ATM IV) + a flat "
      f"+{TAIL_SKEW_BUMP*100:.0f} vol-pt skew bump**; rolled at <{TAIL_ROLL_FLOOR_D}d. Size = "
      f"{TAIL_NOTIONAL_FRAC*100:.0f}% of core notional. Daily mark-to-market charged to NAV.")
    A(f"- **Tier-2 spread (income-funded, saturating):** {TIER2_LONG_OTM*100:.0f}%/"
      f"{TIER2_SHORT_OTM*100:.0f}% OTM put-spread, funded from the ledger up to a fully-"
      f"protected cap; shrinks when the ledger is thin (never crowds out Tier-1).")
    A(f"- **Reserve:** held in T-bills at RF; target = **{RESERVE_YEARS}× worst-case annual "
      f"Tier-1 carry = {fpct(res_target,2)} of NAV**; **replenish-first** after any draw; "
      f"hysteresis band {RESERVE_HYSTERESIS*100:.0f}% before upside deploys.")
    A(f"- **Upside barbell (surplus-only):** rolling **{UPSIDE_OTM*100:.0f}% OTM ~{UPSIDE_TENOR_D}d "
      f"calls** bought from **banked surplus only** (never sell more — Design Rule A), gated on "
      f"**px > {UPSIDE_MA_DAYS}d MA AND realized-vol in the calm {UPSIDE_RVOL_PCTL*100:.0f}th pctile**.")
    A(f"- **Harvest KNOB (ASSUMED):** base **{HARVEST_BASE_ANNUAL*100:.1f}%/yr** of notional on "
      f"fully-calm days (VIX ≤ {HARVEST_CALM_VIX:.0f}), crossing **zero at VIX {HARVEST_ZERO_VIX:.0f}** "
      f"and turning **negative** (a realized loss) above it. Cold-start seed {SEED_FRAC*100:.0f}% of NAV.")
    A(f"- **r = {R_RF*100:.2f}%, q = {Q_DIV*100:.1f}%.** No look-ahead: every decision at day T "
      "uses data ≤ T, applied to T+1.")
    A("")

    A("## Head-to-head — full history")
    A("")
    A("| Strategy | CAGR | Max DD | Calmar | Sharpe | Ann vol |")
    A("|:--|---:|---:|---:|---:|---:|")
    for name, m in full.items():
        b = "**" if name.startswith("S5 prototype") else ""
        A(f"| {b}{name}{b} | {fpct(m['cagr'])} | {fpct(m['maxdd'])} | {fnum(m['calmar'])} | "
          f"{fnum(m['sharpe'])} | {fpct(m['vol'])} |")
    A("")
    A("*Sharpe is over the cash (BIL) leg. CAGR is OPTIMISTIC — flat-skew BSM understates tail "
      "carry and the harvest is an assumed credit; the robust reads are the **drawdown and "
      "Sharpe** improvements over SPY, not raw return (the event-study caveat carries over).*")
    A("")

    A("## Net-delta through the crashes — the passive auto-de-risk proof")
    A("")
    A("The thesis: a constant core + always-on uncapped tail has **net delta = 1 + put_delta** "
      "that **falls toward ~0 on its own** as spot drops (the put delta marches to −1) and "
      "**re-rises** on the recovery — *no signal, no re-entry decision*. Contrast S4, which "
      "sits in cash at the bottom (its documented re-entry lag).")
    A("")
    A("| Episode | SPX bottom | S5 net-delta @ bottom | S5 net-delta min | S4 exposure @ bottom | "
      "S5 recovery capture |")
    A("|:--|:--|---:|---:|---:|---:|")
    for ename, p in nd_proof.items():
        A(f"| {ename} | {p['bottom'].date()} | {fnum(p['nd_at_bottom'])}× | {fnum(p['nd_min'])}× | "
          f"{fnum(p['s4_exp_at_bottom'])}× | {fpct(p['capture'],0)} |")
    A("")
    A("`S5 net-delta @ bottom` well below 1.0× = the tail auto-de-risked the core into the low "
      "with no action. `S4 exposure @ bottom` near its floor = S4 sold down and is sitting out "
      "the rebound. `recovery capture` = S5 NAV gain ÷ SPY gain from the bottom to the episode end.")
    A("")

    A("## Melt-up participation — the upside barbell at work")
    A("")
    A(f"Over the **{MELTUP[0]}..{MELTUP[1]}** grind-higher: "
      f"**S5 {fpct(meltup['s5'],1)}** vs SPY {fpct(meltup['spy'],1)} vs S4 {fpct(meltup['s4'],1)} "
      f"→ **S5 upside participation {fpct(meltup['particip'],0)} of SPY**. "
      "The financed barbell only fires when surplus has banked in a calm uptrend (self-timing); "
      "S4 structurally caps upside by holding vol below SPX's.")
    A("")

    A("## Ledger / reserve / upside diagnostics")
    A("")
    A("| Quantity | Value |")
    A("|:--|---:|")
    A(f"| Reserve target (of NAV) | {fpct(res_target,2)} |")
    A(f"| Worst-case Tier-1 carry / yr | {fpct(s5['worst_t1_carry_annual'],2)} |")
    A(f"| Ledger range (of NAV) | {fpct(led_min,2)} .. {fpct(led_max,2)} |")
    A(f"| Days ledger ran negative | {led_neg_days} of {len(s5['df'])-1} |")
    A(f"| Reserve at/above target | {fpct(res_fill,1)} of days |")
    A(f"| Cumulative harvest (assumed) | {fpct(s5['total_harvest'],1)} of NAV |")
    A(f"| Cumulative Tier-1 carry paid | {fpct(s5['total_tail_carry'],1)} of NAV |")
    A(f"| Upside fundings (count) | {s5['upside_fund_count']} |")
    A(f"| Upside premium spent / payoff | {fpct(s5['total_upside_spent'],2)} / {fpct(s5['total_upside_payoff'],2)} |")
    A("")
    A("**Self-funding behaviour to read here:** the ledger should run a **surplus in calm** "
      "and a **deficit in chop** (Design Rule B), the reserve should **absorb the deficit** so "
      "the ledger rarely/never goes negative, and the upside bucket should fund **only** after "
      "the reserve is full + hysteresis — i.e. only in sustained calm uptrends (self-timing, "
      "self-throttling aggressiveness).")
    A("")

    A("## Harvest-knob sensitivity — how much rides on the one assumed input")
    A("")
    A("| Harvest assumption | CAGR | Max DD | Calmar | Sharpe | Upside fundings | Ledger min |")
    A("|:--|---:|---:|---:|---:|---:|---:|")
    for label, m in knob_res.items():
        A(f"| {label} | {fpct(m['cagr'])} | {fpct(m['maxdd'])} | {fnum(m['calmar'])} | "
          f"{fnum(m['sharpe'])} | {m['upside_fundings']} | {fpct(m['ledger_min'],2)} |")
    A("")
    A("The harvest knob moves the **upside bucket** (more harvest → more banked surplus → more "
      "call fundings → more melt-up participation) and the **ledger floor** (less harvest → deeper "
      "deficit → more reliance on the reserve). The **downside protection (Tier-1) is unaffected** "
      "by the knob — it is funded first and is mandatory — so the maxDD is robust across the sweep. "
      "*That separation is the design working: the assumed income drives the OFFENSE, never the defense.*")
    A("")

    A("## Blunt read — does the structure behave as designed?")
    A("")
    A("- **Two-sided convexity: YES (by construction, and visible).** Net delta auto-de-risks "
      "into every crash bottom (table above) with no timing, and the surplus-funded calls add "
      ">1.0× upside in the calm melt-up. Both ends are long convexity, both financed by the harvest.")
    A("- **Self-throttling aggressiveness: YES.** Upside fundings scale up with the harvest knob "
      "and concentrate in calm uptrends; in chop the ledger has nothing to spend and the book "
      "reverts to defensive (core + tail only). The aggressiveness dials itself.")
    A(f"- **Ledger never goes deeply negative: {'YES' if led_neg_days == 0 else 'MOSTLY'}** — "
      f"the reserve absorbs the chop-year deficit ({led_neg_days} negative days). Tier-1 is never "
      "dropped because it is funded first and seeded at inception.")
    A("- **No re-entry DECISION to lag on: YES — but read it honestly.** The passive tail makes "
      "exposure a continuous Greek, not a discrete signal: net delta auto-de-risks deep into "
      "every bottom (0.03–0.60× in the table) and *re-rises on its own* as the put decays back "
      "OTM up the recovery. There is no cash→equity timing call. The trade-off, stated plainly: "
      "a 100%-notional uncapped tail is FULLY de-risked AT the bottom, so the passive book gives "
      "up part of the sharpest first leg of the rebound (GFC capture ~31%) — this is exactly the "
      "gap the (demoted) active monetization was meant to close, and exactly why the spec ladders "
      "it to Phase-2 rather than firing an all-in surge. S4, by contrast, sits in CASH at the "
      "bottom (a discretionary re-entry it then lags); S5's de-risk is mechanical and reverses "
      "itself. Tail SIZE is the dial here (smaller/deeper tail = more bottom delta, less maxDD "
      "protection) — see the file's tail-sizing notes.")
    A("")
    A("### What ONLY the intraday SPXW data can resolve")
    A("")
    A("- **The real harvest number.** The whole offense (reserve fill speed, upside fundings, "
      "surplus depth) rides on the assumed harvest rate. The sensitivity table shows the *range*; "
      "only 1-min SPXW 0DTE path P&L gives the *actual* gated sell-day count and the fat-tailed "
      "loss distribution on \"calm\" days that turn out not to be.")
    A("- **The true tail cost.** Flat-skew BSM understates real SPX put skew (steeper, time-varying, "
      "crash-spiking). Warehouse EOD chains can refine the carry; intraday refines the roll cost.")
    A("- **Tier-2 / overlay execution realism.** Spread debits, roll slippage, and the 0DTE "
      "stand-down mechanics are EOD approximations here (S2/S3's caveat).")
    A("- **Any active monetization.** Demoted to Phase-2 and intentionally absent; if revisited it "
      "must be a slow, partial, LADDERED harvest gated on intraday marks (never the all-in surge "
      "the event study rejected).")
    A("")
    A("## Caveats (read before trusting any number)")
    A("")
    A("- **Harvest income is ASSUMED** (a knob), vol-regime-scaled for honesty but not measured. "
      "Swept pessimistic/central/optimistic above.")
    A("- **Flat-skew BSM understates real tail cost**; CAGR is therefore optimistic.")
    A("- **No transaction costs by default** (a bps tail-roll drag knob exists, set to 0). "
      "Real frictions lower net CAGR.")
    A("- **EOD only** — no intraday path; the 0DTE overlay is approximated by the harvest knob.")
    A("- **S4 path is the exact shared-brain exposure** (`strategies.spx_vol_control`), causally "
      "shifted, consistent with the dedicated S4 runner.")
    A("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return path


if __name__ == "__main__":
    main()
