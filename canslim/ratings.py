"""
ratings.py — CAN SLIM full-market SELECTION, Phase 2: the RATINGS leg.

Computes, DETERMINISTICALLY and POINT-IN-TIME, the CAN SLIM component ratings for each
universe member at each decision date, from the data Phase 1 already owns:

    * PRICE/VOLUME warehouse  (full_market_prices.py)   -> N, S(vol), L/RS
    * EDGAR quarterly PIT fundamentals (edgar_pipeline) -> C, A, S(quality), EPS/SMR

This is a DETERMINISTIC REPLICA of a *documented* methodology, NOT a tunable model. Every
threshold and formula below is sourced to one of two frozen docs and cited inline:

    [PLAN]  canslim/research/full_market_selection_PLAN.md  (Phase 2 section, lines ~79-88)
    [SPEC]  canslim/research/canslim_oneil_spec.md          (§1 letter rules, §2 ratings)

Where a definition is genuinely NOT numerically pinned in either doc (the proprietary IBD
weighting coefficients), we use the community approximation the SPEC *itself* documents and
endorses (SPEC §2 / §"Numbers I could NOT source"), and we LABEL it as an approximation in
the code and in the column provenance below. We invent no free parameter and tune nothing.

POINT-IN-TIME DISCIPLINE (inherited, not re-implemented)
--------------------------------------------------------
All fundamentals reads go through `full_market_join.fundamentals_asof` (leak-free by FILING
date) and all price reads through `full_market_join.prices_asof` (leak-free by bar date). At
decision date D this module NEVER sees a filing filed after D or a price bar dated after D.
The RS percentile is ranked across the survivorship-INCLUSIVE members live on D (delisted
names included) — ranking against survivors only would bias the percentile upward.

COMPONENT COVERAGE (honest — see coverage() and the Phase-2 report)
-------------------------------------------------------------------
  C  Current quarterly earnings growth   FULL   (eps_growth_yoy, most-recent as-of quarter)
  A  Annual earnings growth              FULL   (TTM-EPS YoY streak, 3yr, from the quarter ladder)
  N  New high / new-ness                 FULL   (price vs 52-wk high/low from prices)
  S  Supply/Demand                       PARTIAL: demand=volume-surge (FULL); float/buyback
                                                  UNAVAILABLE (no shares-outstanding/float column
                                                  in the fundamentals warehouse)
  L  Leader vs laggard (RS)              FULL   (RS raw + RS percentile across the live universe)
  I  Institutional sponsorship          UNAVAILABLE (no holdings/13-F data owned) -> None, never faked
  M  Market direction                    NOT A COMPONENT HERE — emergent by design
                                                  (memory: canslim-exposure-is-emergent; PLAN "SCOPE")

Only this CODE lives in the Drive repo; all data is off-Drive under C:/TradingDesk-Local/canslim/.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import full_market_join as fj  # leak-free PIT accessors (price + fundamentals)  # noqa: E402

OUT_DIR = Path(r"C:\TradingDesk-Local\canslim\ratings")


# ==========================================================================================
# FROZEN, SOURCED CONSTANTS  (public methodology — NOT tuned; each cites its doc)
# ==========================================================================================

# --- RS raw score (relative strength), [PLAN] Phase 2, VERBATIM formula ---
#   raw = 2*(C/C_63) + (C/C_126) + (C/C_189) + (C/C_252)
# i.e. the trailing 3/6/9/12-month price ratios with the most-recent quarter double-weighted,
# matching [SPEC §2] "most recent quarter weighted more heavily". Lookbacks in trading days:
RS_LOOKBACKS = (63, 126, 189, 252)          # ~3, 6, 9, 12 months  [PLAN]
RS_WEIGHTS = (2.0, 1.0, 1.0, 1.0)           # recent-quarter double weight  [PLAN]
RS_MIN_HISTORY = 126                        # need >= ~6mo of bars to score RS at all (else None)

# --- N (new-ness), [SPEC §1 "N"] ---
#   proxy: price near its 52-week high (closer to 52-wk high than 52-wk low).
N_WINDOW = 252                              # 52 weeks of trading days  [SPEC "N": 52-week high]

# --- S demand (volume surge), [SPEC §1 "S" / §4] ---
#   IBD breakout demand = latest volume vs the 50-day average daily volume.
S_VOL_WINDOW = 50                           # 50-day avg daily volume  [SPEC §4/§6: 50-day line]

# --- C threshold, [SPEC §1 "C"] codified screen number ---
C_YOY_THRESHOLD = 0.25                      # >= +25% YoY current-quarter EPS (codified)  [SPEC §1 C]

# --- A thresholds, [SPEC §1 "A"] ---
A_YEARS = 3                                 # each of the last 3 years  [SPEC §1 A]
A_YOY_THRESHOLD = 0.25                      # >= +25%/yr annual EPS growth (codified)  [SPEC §1 A]
A_ROE_FLOOR = 0.17                          # ROE >= 17% (O'Neil floor)  [SPEC §1 A]

# --- L gate, [SPEC §1 "L" / §2] ---
L_RS_GATE = 80                              # RS Rating >= 80 minimum (ideally 90)  [SPEC §1 L]

# --- EPS Rating blend, [SPEC §2] — proprietary weights UNDISCLOSED by IBD. ---
# The SPEC documents (and its "Numbers I could NOT source" section flags) that the exact IBD
# EPS-Rating coefficients are proprietary. We use the SPEC's own stated *structure* — combine
# (1) most-recent-quarter YoY, (2) prior-quarter YoY, (3) multi-year growth, (4) stability;
# "recent quarters weighted extra" — with a transparent, evenly-motivated weighting. This is an
# APPROXIMATION of IBD's EPS Rating, explicitly NOT parity (labeled in provenance). No knob is
# fit to any outcome.
#
# DISPLAY-ONLY WEIGHTS (never decision-gates): these EPS_W_* coefficients are a proprietary-weight
# APPROXIMATION and feed ONLY the display/analysis grades eps_rating + composite_rating. Phase 3
# selection gates ONLY on the raw spec-pinned components (C_pass/A_pass/N_pass/L_pass); these
# weights must never enter any entry/ranking/sizing decision. Enforced by the guard test
# test_composite_weights_do_not_affect_screen in tests/test_ratings.py — perturbing them is proven
# not to change the screen-pass set.
EPS_W_Q0 = 2.0          # most-recent quarter YoY  (double weight = "recent extra"  [SPEC §2])
EPS_W_Q1 = 1.0          # prior quarter YoY
EPS_W_MULTIYR = 1.0     # 3-yr annual growth component
EPS_W_STABILITY = 1.0   # earnings-stability component
EPS_MULTIYR_YEARS = 3   # trailing years for the multi-year growth leg  [SPEC §1 A / §2]

# --- Composite Rating blend weights, [SPEC §2 / PLAN] — DISPLAY-ONLY approximation. ---
# IBD's proprietary composite weights are undisclosed; this is the SPEC's stated structure
# ("EPS + RS heaviest; SMR from fundamentals; 52-wk-high distance"). Exposed as constants (not
# inlined) SOLELY so the guard test can perturb them and prove they never move the raw screen.
# Like EPS_W_*, these feed ONLY the display grade composite_rating — never a decision-gate.
COMPOSITE_W_EPS = 2.0       # eps_rating leg (heaviest)   [SPEC §2]
COMPOSITE_W_RS = 2.0        # rs_rating leg (heaviest)    [SPEC §2]
COMPOSITE_W_SMR = 1.0       # smr_rating_pct leg          [SPEC §2]
COMPOSITE_W_NEARHIGH = 1.0  # 52-wk-high proximity leg    [PLAN]


# ==========================================================================================
# Per-symbol RAW component computation (point-in-time; no cross-sectional info)
# ==========================================================================================

@dataclass
class RawRatings:
    """Per-(cik,symbol,date) raw component values. Cross-sectional percentiles added later."""
    cik: int
    ticker: str
    as_of: pd.Timestamp
    # --- price-derived ---
    rs_raw: float | None            # [PLAN] weighted trailing-return score (higher = stronger)
    pct_off_52w_high: float | None  # N: (52w_high - close)/52w_high  (0 == at the high)  [SPEC N]
    pct_above_52w_low: float | None # N: (close - 52w_low)/52w_low                          [SPEC N]
    near_52w_high: bool | None      # N gate proxy: closer to high than to low             [SPEC N]
    vol_surge: float | None         # S demand: latest_vol / 50d_avg_vol                   [SPEC S/§4]
    # --- fundamentals-derived ---
    c_eps_yoy: float | None         # C: most-recent as-of quarter EPS %chg YoY            [SPEC C]
    c_eps_yoy_prior: float | None   # prior quarter EPS %chg YoY (for EPS-rating blend)    [SPEC §2]
    c_accelerating: bool | None     # C: current-Q YoY >= prior-Q YoY (accel)             [SPEC C]
    c_sales_yoy: float | None       # C: current-quarter sales %chg YoY                    [SPEC C]
    a_eps_growth_3y: float | None   # A: TTM-EPS CAGR over ~3 yrs                          [SPEC A]
    a_each_year_up: bool | None     # A: TTM-EPS up each of last A_YEARS years             [SPEC A]
    a_roe: float | None             # A: TTM ROE (annualized)                              [SPEC A]
    eps_stability: float | None     # EPS-rating leg: 1/(1+cv) of TTM-EPS trail            [SPEC §2]
    net_margin: float | None        # SMR leg (margin)                                     [SPEC §2]
    sales_yoy: float | None         # SMR leg (sales)                                      [SPEC §2]
    # --- explicitly unavailable (never fabricated) ---
    s_float: None = None            # S float/shares-outstanding: NOT in warehouse
    i_institutional: None = None    # I institutional sponsorship: NO holdings data owned


def _price_components(px: pd.DataFrame) -> dict:
    """RS raw, 52-wk-high distance (N), and volume surge (S demand) from a leak-free price tail."""
    out = dict(rs_raw=None, pct_off_52w_high=None, pct_above_52w_low=None,
               near_52w_high=None, vol_surge=None)
    if px is None or px.empty:
        return out
    close = px["close"].to_numpy(dtype="float64")
    n = len(close)
    c0 = close[-1]

    # RS raw [PLAN]: 2*(C/C63)+(C/C126)+(C/C189)+(C/C252); need >= RS_MIN_HISTORY bars.
    if n >= RS_MIN_HISTORY:
        score = 0.0
        ok = True
        for w, lb in zip(RS_WEIGHTS, RS_LOOKBACKS):
            if n > lb and close[-1 - lb] > 0:
                score += w * (c0 / close[-1 - lb])
            elif lb <= 63:
                ok = False   # the double-weighted recent leg must exist; else RS is undefined
                break
            # longer legs (126/189/252) simply drop out when history is short (partial RS)
        out["rs_raw"] = float(score) if ok else None

    # N [SPEC]: distance from 52-wk high / low over the trailing 252 bars.
    win = close[-N_WINDOW:] if n >= N_WINDOW else close
    hi, lo = float(win.max()), float(win.min())
    if hi > 0:
        out["pct_off_52w_high"] = float((hi - c0) / hi)
    if lo > 0:
        out["pct_above_52w_low"] = float((c0 - lo) / lo)
    if hi > lo:  # closer to high than low == in the upper half of the 52-wk range
        out["near_52w_high"] = bool((c0 - lo) >= (hi - c0))

    # S demand [SPEC §4]: latest volume vs trailing 50-day average.
    if "volume" in px.columns and n >= 2:
        vol = px["volume"].to_numpy(dtype="float64")
        avg = vol[-(S_VOL_WINDOW + 1):-1]  # trailing avg EXCLUDING the latest bar
        avg = avg[avg > 0]
        if avg.size and vol[-1] >= 0:
            out["vol_surge"] = float(vol[-1] / avg.mean())
    return out


def _ttm_eps_series(hist: pd.DataFrame) -> pd.DataFrame:
    """
    Trailing-twelve-month diluted EPS per as-of-known quarter, from the PIT quarter ladder.
    TTM-EPS(t) = sum of the 4 most recent as-reported diluted EPS ending at quarter t. Requires
    4 consecutive non-null quarters. Returns a frame [period_end, ttm_eps] (chronological), the
    basis for the A (annual) rating and the multi-year EPS leg — all as-first-filed, no lookahead.
    """
    if hist is None or hist.empty:
        return pd.DataFrame(columns=["period_end", "ttm_eps"])
    d = hist.dropna(subset=["eps_diluted"]).sort_values(["period_end", "filed"])
    # one row per period_end (the last-known/most-recent filing for that period as-of already
    # enforced upstream by fundamentals_asof; keep the latest within the as-of frame)
    d = d.drop_duplicates(subset=["period_end"], keep="last").reset_index(drop=True)
    eps = d["eps_diluted"].to_numpy(dtype="float64")
    pes = d["period_end"].to_numpy()
    rows = []
    for i in range(3, len(eps)):
        rows.append((pes[i], float(eps[i - 3:i + 1].sum())))
    return pd.DataFrame(rows, columns=["period_end", "ttm_eps"])


def _annual_eps_components(hist: pd.DataFrame) -> dict:
    """
    A (annual earnings growth) from the TTM-EPS series [SPEC §1 A]:
      * a_eps_growth_3y : CAGR of TTM-EPS from ~3 yrs ago to now (only when both ends > 0),
      * a_each_year_up  : TTM-EPS higher than its value ~1yr prior in EACH of the last A_YEARS,
      * eps_stability   : 1/(1+coefficient-of-variation) of the trailing TTM-EPS (EPS-rating leg).
    Year steps use 4-quarter offsets on the TTM series (a "year" == 4 quarters back).
    """
    out = dict(a_eps_growth_3y=None, a_each_year_up=None, eps_stability=None)
    ttm = _ttm_eps_series(hist)
    if ttm.empty:
        return out
    vals = ttm["ttm_eps"].to_numpy(dtype="float64")
    n = len(vals)

    # 3-yr CAGR (need a point 3*4=12 quarters back and both endpoints positive)
    step = 4
    back = A_YEARS * step
    if n > back and vals[-1] > 0 and vals[-1 - back] > 0:
        out["a_eps_growth_3y"] = float((vals[-1] / vals[-1 - back]) ** (1.0 / A_YEARS) - 1.0)

    # each-year-up over the last A_YEARS (TTM_t > TTM_{t-4} each year)
    if n > back:
        ups = []
        for k in range(A_YEARS):
            cur = vals[-1 - k * step]
            prev = vals[-1 - (k + 1) * step]
            ups.append(cur > prev)
        out["a_each_year_up"] = bool(all(ups))

    # stability: inverse coefficient of variation over the trailing ~ up to 12 TTM points
    trail = vals[-min(n, 12):]
    m = float(np.mean(trail))
    if abs(m) > 1e-9:
        cv = float(np.std(trail) / abs(m))
        out["eps_stability"] = float(1.0 / (1.0 + cv))
    return out


def compute_raw(cik: int, ticker: str, as_of) -> RawRatings | None:
    """
    All raw (non-cross-sectional) rating components for one (cik, ticker) as-of a date, strictly
    point-in-time. Returns None if there is no price history as-of the date (can't rate it).
    """
    as_of = pd.Timestamp(as_of)
    snap = fj.joined_asof(cik, ticker, as_of)
    if snap is None:
        return None
    px = snap["prices"]
    hist = snap["fund_hist"]
    row = snap["fund_row"]

    pc = _price_components(px)
    ac = _annual_eps_components(hist)

    # C (current quarterly) from the most-recent as-of-known quarter [SPEC §1 C]
    c_eps_yoy = c_eps_yoy_prior = c_sales_yoy = None
    c_accel = None
    net_margin = sales_yoy = None
    if row is not None:
        c_eps_yoy = _f(row.get("eps_growth_yoy"))
        c_sales_yoy = _f(row.get("sales_growth_yoy"))
        net_margin = _f(row.get("net_margin"))
        sales_yoy = c_sales_yoy
    if hist is not None and len(hist) >= 2:
        prior = hist.iloc[-2]
        c_eps_yoy_prior = _f(prior.get("eps_growth_yoy"))
        if c_eps_yoy is not None and c_eps_yoy_prior is not None:
            c_accel = bool(c_eps_yoy >= c_eps_yoy_prior)   # acceleration [SPEC §1 C]

    # A ROE (annualized TTM) from the as-of row [SPEC §1 A]
    a_roe = _f(row.get("roe_ttm_annualized")) if row is not None else None

    return RawRatings(
        cik=int(cik), ticker=str(ticker), as_of=as_of,
        rs_raw=pc["rs_raw"],
        pct_off_52w_high=pc["pct_off_52w_high"],
        pct_above_52w_low=pc["pct_above_52w_low"],
        near_52w_high=pc["near_52w_high"],
        vol_surge=pc["vol_surge"],
        c_eps_yoy=c_eps_yoy, c_eps_yoy_prior=c_eps_yoy_prior, c_accelerating=c_accel,
        c_sales_yoy=c_sales_yoy,
        a_eps_growth_3y=ac["a_eps_growth_3y"], a_each_year_up=ac["a_each_year_up"], a_roe=a_roe,
        eps_stability=ac["eps_stability"],
        net_margin=net_margin, sales_yoy=sales_yoy,
    )


def _f(v):
    """Coerce a possibly-NaN scalar to float or None (never let a NaN masquerade as a value)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f) else f   # NaN check


# ==========================================================================================
# Cross-sectional ratings (1-99 percentiles) across the LIVE universe on a date
# ==========================================================================================

def _percentile_1_99(values: pd.Series) -> pd.Series:
    """
    IBD-style 1-99 percentile rank [SPEC §2]: rank each value against the cross-section and map
    to an integer 1..99 (99 == top). Ties share the average rank. NaNs stay NaN (unrated).
    """
    v = values.astype("float64").replace([np.inf, -np.inf], np.nan)  # inf ratios are unrateable
    valid = v.notna()
    out = pd.Series(np.nan, index=v.index)
    if valid.sum() == 0:
        return out
    r = v[valid].rank(method="average", pct=True)          # (0,1], higher value -> higher pct
    out[valid] = np.clip(np.ceil(r * 99).astype(int), 1, 99)
    return out


def _eps_rating_raw(df: pd.DataFrame) -> pd.Series:
    """
    Composite EPS-strength raw score to be percentile-ranked into the 1-99 EPS Rating [SPEC §2].
    APPROXIMATION (IBD's true weights are proprietary — see EPS_W_* constants): weighted blend of
    most-recent-quarter YoY (double), prior-quarter YoY, 3-yr growth, and stability. Missing legs
    drop out and the weight-normaliser shrinks accordingly, so a name is scored on what it has.
    """
    q0 = df["c_eps_yoy"].astype("float64")
    q1 = df["c_eps_yoy_prior"].astype("float64")
    g3 = df["a_eps_growth_3y"].astype("float64")
    st = df["eps_stability"].astype("float64")
    legs = [(q0, EPS_W_Q0), (q1, EPS_W_Q1), (g3, EPS_W_MULTIYR), (st, EPS_W_STABILITY)]
    num = pd.Series(0.0, index=df.index)
    den = pd.Series(0.0, index=df.index)
    for series, w in legs:
        m = series.notna()
        num[m] += w * series[m]
        den[m] += w
    raw = pd.Series(np.nan, index=df.index)
    ok = den > 0
    raw[ok] = num[ok] / den[ok]
    return raw


def _smr_raw(df: pd.DataFrame) -> pd.Series:
    """
    SMR (Sales/Margin/ROE) raw strength [SPEC §2] = z-summed sales_yoy + net_margin + roe, then
    percentile-ranked downstream (mapped A-E in the report). Each leg standardised within the
    cross-section so units don't dominate; missing legs drop out.
    """
    parts = []
    for col in ("sales_yoy", "net_margin", "a_roe"):
        # ±inf (e.g. a YoY ratio off a ~0 base) is not a usable z input -> treat as missing
        s = df[col].astype("float64").replace([np.inf, -np.inf], np.nan)
        valid = s.dropna()
        if len(valid) >= 2 and valid.std() > 0:
            parts.append((s - valid.mean()) / valid.std())
        else:                       # <2 valid points or zero spread -> no usable z leg
            parts.append(pd.Series(np.nan, index=df.index))
    z = pd.concat(parts, axis=1)
    return z.mean(axis=1, skipna=True)


def rate_cross_section(raws: list[RawRatings]) -> pd.DataFrame:
    """
    Given the RAW components for every live member on ONE decision date, add the cross-sectional
    1-99 ratings [PLAN Phase 2 / SPEC §2]:
        rs_rating       — percentile of rs_raw across THIS date's live (survivorship-incl) members
        eps_rating      — percentile of the EPS-strength blend (approx; proprietary weights)
        smr_rating_pct  — percentile of the SMR blend (mapped A-E by _smr_letter)
        composite_rating— percentile of (EPS + RS heaviest, + SMR, + 52-wk-high proximity) [PLAN]
    Percentiles are computed ONLY within this date's cross-section (never pooled across dates),
    which is the whole reason the survivorship-inclusive price leg matters.
    """
    if not raws:
        return pd.DataFrame()
    df = pd.DataFrame([asdict(r) for r in raws])

    df["rs_rating"] = _percentile_1_99(df["rs_raw"])

    # -----------------------------------------------------------------------------------------
    # DISPLAY-ONLY GRADES (never decision-gates). eps_rating, smr_rating_pct/smr_letter, and the
    # composite_rating below use PROPRIETARY-WEIGHT APPROXIMATIONS (IBD's coefficients are
    # undisclosed — see the EPS_W_* / composite-blend constants). They exist for DISPLAY/analysis
    # ONLY. Phase 3 selection gates ONLY on the raw spec-pinned components
    # (C_pass/A_pass/N_pass/L_pass from screen_flags); these grades must never enter any
    # entry/ranking/sizing decision. Guarded by test_composite_weights_do_not_affect_screen.
    # (rs_rating above is a raw cross-sectional RS percentile — NOT a proprietary-weight grade —
    # and DOES back the L_pass gate, which is spec-pinned at L_RS_GATE.)
    # -----------------------------------------------------------------------------------------
    df["eps_rating"] = _percentile_1_99(_eps_rating_raw(df))
    df["smr_raw"] = _smr_raw(df)
    df["smr_rating_pct"] = _percentile_1_99(df["smr_raw"])
    df["smr_letter"] = df["smr_rating_pct"].map(_smr_letter)

    # Composite [PLAN]: "EPS + RS heaviest; SMR from fundamentals; 52-wk-high distance".
    # proximity-to-52wk-high as a 1-99 leg (closer to high = higher); proprietary IBD composite
    # weights are undisclosed [SPEC §2] -> transparent EPS/RS-heaviest blend, labeled approximate.
    near_high = _percentile_1_99(-df["pct_off_52w_high"])   # smaller distance -> higher percentile
    comp_num = pd.Series(0.0, index=df.index)
    comp_den = pd.Series(0.0, index=df.index)
    for leg, w in ((df["eps_rating"], COMPOSITE_W_EPS), (df["rs_rating"], COMPOSITE_W_RS),
                   (df["smr_rating_pct"], COMPOSITE_W_SMR), (near_high, COMPOSITE_W_NEARHIGH)):
        m = leg.notna()
        comp_num[m] += w * leg[m]
        comp_den[m] += w
    comp = pd.Series(np.nan, index=df.index)
    ok = comp_den > 0
    comp[ok] = comp_num[ok] / comp_den[ok]
    df["composite_rating"] = _percentile_1_99(comp)

    # I (institutional) is genuinely unavailable — carry an explicit column so downstream code
    # can SEE it is None (never a fabricated 0 or a silent drop).
    df["i_institutional"] = None
    return df


def _smr_letter(pct) -> str | None:
    """Map an SMR 1-99 percentile to IBD's A-E letter [SPEC §2] (A top quintile ... E bottom)."""
    if pct is None or (isinstance(pct, float) and pct != pct):
        return None
    p = float(pct)
    if p >= 80: return "A"
    if p >= 60: return "B"
    if p >= 40: return "C"
    if p >= 20: return "D"
    return "E"


# ==========================================================================================
# Screen gates (deterministic, sourced) — booleans the Phase-3 backtest will consume
# ==========================================================================================

def screen_flags(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add the deterministic CAN SLIM component GATES [SPEC §1] as booleans (no tuning). These are
    the codified single-threshold forms of O'Neil's ranges (each cited to the SPEC constant).
    M is deliberately absent (emergent). I is absent (unavailable). These are FLAGS for Phase 3,
    not a combined verdict — Phase 3 decides how to use them.

    CRITICAL — WHAT PHASE 3 MAY GATE ON:
      Phase 3 selection MUST gate ONLY on these raw, spec-pinned components produced here:
      C_pass / A_pass / N_pass / L_pass. Each is derived from a frozen [SPEC §1] threshold
      (C_YOY_THRESHOLD, the A_* floors, the near-52wk-high proxy, L_RS_GATE on the raw RS
      percentile).
      The three IBD-style COMPOSITE GRADES — eps_rating, composite_rating, and
      smr_rating_pct / smr_letter — are computed for DISPLAY / analysis ONLY. They use
      PROPRIETARY-WEIGHT APPROXIMATIONS (IBD's true coefficients are undisclosed; see the
      EPS_W_* and COMPOSITE_W_* constants) and must NEVER appear in any entry, ranking, or
      sizing decision. This is enforced by the guard test
      test_composite_weights_do_not_affect_screen in tests/test_ratings.py, which perturbs a
      composite weight and asserts the screen-pass set is unchanged.
    """
    df = df.copy()
    df["C_pass"] = df["c_eps_yoy"].apply(lambda v: None if _f(v) is None else _f(v) >= C_YOY_THRESHOLD)
    df["A_pass"] = df.apply(_a_pass, axis=1)
    df["N_pass"] = df["near_52w_high"]                       # in upper half of 52-wk range [SPEC N]
    df["L_pass"] = df["rs_rating"].apply(lambda v: None if _f(v) is None else _f(v) >= L_RS_GATE)
    return df


def _a_pass(r) -> bool | None:
    g = _f(r.get("a_eps_growth_3y"))
    up = r.get("a_each_year_up")
    roe = _f(r.get("a_roe"))
    if g is None and up is None and roe is None:
        return None
    cond_growth = (g is not None and g >= A_YOY_THRESHOLD)
    cond_up = bool(up) if up is not None else False
    cond_roe = (roe is not None and roe >= A_ROE_FLOOR)
    return bool(cond_growth and cond_up and cond_roe)


# ==========================================================================================
# The date-driver: rate every live member on a decision date, point-in-time
# ==========================================================================================

def rate_universe_asof(as_of, members: pd.DataFrame | None = None) -> pd.DataFrame:
    """
    Rate EVERY universe member live on `as_of`, cross-sectionally, leak-free.

    `members`: a DataFrame with columns [cik, ticker] of the names to rate as-of this date. If
    None, uses the Phase-1 membership rows for as_of's calendar year (full_market_join). Only
    names with a price file on disk get a raw score; the rest are counted as pending (coverage()).
    Returns the rated cross-section (one row per rateable member) with all component + 1-99 cols.
    """
    as_of = pd.Timestamp(as_of)
    if members is None:
        members = fj.members_for_year(as_of.year)[["cik", "ticker"]].drop_duplicates()

    raws: list[RawRatings] = []
    for cik, ticker in members[["cik", "ticker"]].itertuples(index=False):
        rr = compute_raw(int(cik), str(ticker), as_of)
        if rr is not None:
            raws.append(rr)
    rated = rate_cross_section(raws)
    if rated.empty:
        return rated
    rated = screen_flags(rated)
    rated.insert(2, "rate_date", as_of)
    return rated


# ==========================================================================================
# Coverage / self-check
# ==========================================================================================

def coverage(as_of="2024-01-02") -> pd.DataFrame:
    """
    Honest coverage of the ratings on one date: how many members were rateable, and per component
    how many got a non-null value (FULL vs partial vs unavailable). Prints and returns the table.
    """
    as_of = pd.Timestamp(as_of)
    members = fj.members_for_year(as_of.year)[["cik", "ticker"]].drop_duplicates()
    rated = rate_universe_asof(as_of, members)
    n_mem = len(members)
    n_rated = len(rated)
    print(f"RATINGS COVERAGE as-of {as_of.date()}")
    print(f"  members (CIK x ticker) for the year : {n_mem:,}")
    print(f"  ... rateable (price file on disk)   : {n_rated:,}")
    if rated.empty:
        print("  (no rateable members yet — price pull still filling in)")
        return rated
    comps = {
        "C  (c_eps_yoy)": "c_eps_yoy",
        "A  (a_eps_growth_3y)": "a_eps_growth_3y",
        "N  (pct_off_52w_high)": "pct_off_52w_high",
        "S-demand (vol_surge)": "vol_surge",
        "L/RS (rs_rating)": "rs_rating",
        "EPS rating": "eps_rating",
        "Composite rating": "composite_rating",
        "SMR letter": "smr_letter",
    }
    print("  component availability (non-null of rateable):")
    for label, col in comps.items():
        nn = int(rated[col].notna().sum())
        print(f"    {label:<26}: {nn:>4}/{n_rated} ({100*nn/max(1,n_rated):5.1f}%)")
    print("    S-float / I-institutional  : UNAVAILABLE (no shares/float/holdings data owned)")
    print("    M market direction         : EMERGENT (not a component here, by design)")
    return rated


def write_ratings_asof(as_of, out_dir: Path = OUT_DIR) -> Path:
    """Materialise one date's rated cross-section to parquet (for Phase 3 to consume)."""
    as_of = pd.Timestamp(as_of)
    rated = rate_universe_asof(as_of)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"ratings_{as_of.date()}.parquet"
    rated.to_parquet(out, index=False)
    print(f"  wrote {out}  ({len(rated)} rated members)")
    return out


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "write":
        write_ratings_asof(sys.argv[2] if len(sys.argv) > 2 else "2024-01-02")
    else:
        coverage(sys.argv[1] if len(sys.argv) > 1 else "2024-01-02")
