r"""
s7_income_condor.py — S7: SPX 45-DTE Managed Premium-Income Condor (EOD, honest fills).

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the warehouse.

The monthly-style, defined-risk premium seller income traders actually run — the version
never yet honestly tested on this desk (all prior condor work was 0DTE, refuted four ways).
Distinct strategy number S7 (Andrew's call); lineage S2/S3 iron-condor income.

Pre-registered in docs/PREREG_S7_income_condor_2026-07-04.md BEFORE any run. This file
implements exactly that chassis + grid; it does NOT tune anything to the data. A clean
refutation is a valid outcome and is reported as-is.

=== CHASSIS (pre-registered, frozen) ===
  * Structure: symmetric iron condor — short put ~target delta, short call ~target delta,
    long protective wings a FIXED 25-point width further OTM each side (SPX $100/pt).
  * Entry cadence: WEEKLY ladder — one new condor per calendar week (first trading day of
    the week), on the listed expiration nearest the target DTE. Positions held concurrently,
    managed independently.
  * Marking: each trading day, mark every open condor from that day's EOD bid/ask at the
    applied fill fraction.
  * Management (managed arms): close when open profit >= target% of entry credit OR when
    DTE <= 21, whichever first; else run to expiry, cash-settled at intrinsic (European
    index, defined risk, NO assignment). Control arm: hold to expiry, no take/stop.
  * Sizing: 1 lot per weekly entry. Equity = cumulative realized P&L.

=== FILLS — HONEST NET-COMBO (never mid) ===
  Fill fraction f in {0.0=mid, 0.25, 0.50=HEADLINE, 1.0=full cross} of each leg's half
  spread, applied on BOTH entry and every management close, and PROPAGATED THROUGH the
  profit-target trigger (a friendlier fill => more credit + cheaper close => target touches
  on a different day).

=== DATA CORRUPTION HANDLING (pre-registered) ===
  Vendor delta & implied_vol are CORRUPT (2021 total, 2020 partial). PRICING uses real
  bid/ask (clean all years). STRIKE SELECTION never uses the corrupt vendor delta on a
  degenerate day: we flag a day whose share of |delta| in {0,1} exceeds DEGENERATE_DELTA_FRAC
  and RE-INVERT a clean delta from mid + underlying + rate + T via the audited s6_recon BSM.
  We also re-invert any single leg whose vendor delta is missing/degenerate. Count reported.

=== NO LOOK-AHEAD ===
  Each condor's entry uses ONLY its entry-day snapshot. Its daily marks and exit walk days
  FORWARD and stop at the FIRST day a rule fires — never peeking at later days. A future
  day cannot change a past entry or a past close.
"""

from __future__ import annotations

import datetime as _dt
import glob
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np
import pandas as pd

import s6_recon as recon  # audited BSM: implied_vol_from_mid, bs_delta

# --------------------------------------------------------------------------- #
# Warehouse (READ-ONLY)
# --------------------------------------------------------------------------- #
WAREHOUSE = Path(r"C:\TradingDesk-Local\warehouse\raw\options\SPX")
OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "s7_research"

# VIX daily close series for the IV-rank entry filter (rebuild addendum). This parquet
# carries a clean `date` + `spot` (the VIX index close) 2018-01 -> present. READ-ONLY.
VIX_DAILY_PARQUET = Path(r"C:\TradingDesk-Local\warehouse\derived\VIX_gex_daily.parquet")

CONTRACT_MULTIPLIER = 100.0   # SPX options are $100/point
WING_WIDTH = 25.0             # default fixed protective-wing width (the 25-pt CONTROL arm)
TIME_STOP_DTE = 21            # managed arms close at DTE <= 21 (pre-registered)
IVR_WINDOW = 252              # trailing trading-day window for the IV-rank percentile
IVR_HIGH_THRESHOLD = 50.0     # 'high-IVR-only' opens only when IVR >= 50 (top half)

# Data-corruption guard (pre-registered): flag a day's vendor delta column as degenerate
# when the share of rows with |delta| exactly in {0,1} exceeds this. Chosen to sit well
# below the ~49% corrupt-2021 days and well above the ~2% clean days — a wide margin, not
# a fitted knob.
DEGENERATE_DELTA_FRAC = 0.35

# Rate / dividend for the clean BSM re-inversion (declared assumptions, not tuned).
RISK_FREE_RATE = recon.RISK_FREE_RATE
DIVIDEND_YIELD = recon.DIVIDEND_YIELD
_DAYS_PER_YEAR = 365.25


# --------------------------------------------------------------------------- #
# Day file access
# --------------------------------------------------------------------------- #
def _fpath(d: _dt.date) -> Path:
    return WAREHOUSE / f"{d:%Y%m%d}.parquet"


def available_days() -> list[_dt.date]:
    """Every trading day with a non-empty EOD chain file, sorted."""
    out = []
    for f in sorted(glob.glob(str(WAREHOUSE / "*.parquet"))):
        base = os.path.basename(f)[:8]
        try:
            d = _dt.datetime.strptime(base, "%Y%m%d").date()
        except ValueError:
            continue
        out.append(d)
    return out


def load_day(d: _dt.date) -> pd.DataFrame | None:
    """Load one EOD chain, normalized. Returns None if the file is missing/empty.

    Keeps only the columns we use; parses expiration to date; adds DTE.

    IMPORTANT DATA CAVEAT (discovered at build time, memorialized in the report):
    the warehouse bid/ask NBBO is ALL-ZERO for 2020-08-13 -> 2021-12-31 (~333 trading
    days) — a genuine quote BLACKOUT, not merely corrupt greeks. On those days only the
    last-trade `close` and `underlying_price` survive. We refuse to fabricate a spread
    there: rows with no usable two-sided quote are dropped, so a blackout day loads as an
    empty (unquotable) chain and the honest-fill engine skips it. See day_quote_ok().
    """
    p = _fpath(d)
    if not p.is_file():
        return None
    df = pd.read_parquet(p)
    if df is None or len(df) == 0 or "strike" not in df.columns:
        return None
    keep = ["expiration", "strike", "right", "bid", "ask", "delta",
            "implied_vol", "underlying_price"]
    df = df[keep].copy()
    df["expiration"] = pd.to_datetime(df["expiration"]).dt.date
    df["dte"] = df["expiration"].map(lambda e: (e - d).days)
    # Usable two-sided quote only (ask>0, bid>=0, ask>=bid). A cheap leg legitimately
    # quotes bid=0/ask=0.05 — keep it; drop only genuinely unquoted rows. During the
    # 2020-08→2021-12 quote blackout EVERY row is bid=0/ask=0 and gets dropped here.
    df = df[(df["ask"] > 0) & (df["bid"] >= 0) & (df["ask"] >= df["bid"])]
    return df.reset_index(drop=True)


def day_quote_ok(d: _dt.date) -> bool:
    """True if day d has a genuinely quoted chain (survives the blackout filter).

    Cheap coverage probe used to report the honest data window and to exclude blackout
    entry-weeks from the ladder. A day counts as quoted if >50% of its raw rows carry a
    positive ask (clean days are ~100%; blackout days are 0%)."""
    p = _fpath(d)
    if not p.is_file():
        return False
    try:
        df = pd.read_parquet(p, columns=["ask"])
    except Exception:
        return False
    if len(df) == 0:
        return False
    return bool((df["ask"] > 0).mean() > 0.5)


# --------------------------------------------------------------------------- #
# IV-rank entry filter (rebuild addendum) — trailing VIX percentile, CAUSAL
# --------------------------------------------------------------------------- #
def load_ivr_series() -> pd.Series | None:
    """Trailing-252-day IV-rank of the VIX close, indexed by date. None if VIX absent.

    IVR on day t = percentile rank of the VIX close within the trailing IVR_WINDOW closes
    UP TO AND INCLUDING t (0..100). Strictly causal: min_periods=window so early days are
    NaN (the filter simply won't gate before it has a full lookback — treated as fail-high
    below). If the VIX parquet cannot be found, returns None and the caller SKIPS the IVR
    arm (never fabricates the series)."""
    if not VIX_DAILY_PARQUET.is_file():
        return None
    try:
        df = pd.read_parquet(VIX_DAILY_PARQUET, columns=["date", "spot"])
    except Exception:
        return None
    if df is None or len(df) == 0 or "spot" not in df.columns:
        return None
    df = df.copy()
    df["d"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d").dt.date
    df = df.dropna(subset=["spot"]).sort_values("d")
    vix = df.set_index("d")["spot"].astype(float)

    def _pctile_rank(window_vals: np.ndarray) -> float:
        # rank of the LAST value within its trailing window (causal), 0..100.
        last = window_vals[-1]
        return 100.0 * float(np.mean(window_vals <= last))

    ivr = vix.rolling(IVR_WINDOW, min_periods=IVR_WINDOW).apply(_pctile_rank, raw=True)
    return ivr


def ivr_passes(ivr_series: pd.Series | None, d: _dt.date, ivr_filter: str) -> bool:
    """Entry gate. 'always' -> always True. 'high' -> IVR(d) >= threshold using ONLY the
    close on/before d (the series is precomputed causally). A missing/NaN IVR on d fails
    the 'high' gate (we don't open a high-IVR trade we can't verify is high-IVR)."""
    if ivr_filter == "always":
        return True
    if ivr_series is None:
        return True  # no VIX data -> IVR arm is skipped upstream; be permissive if reached.
    val = ivr_series.get(d, np.nan)
    return bool(np.isfinite(val) and val >= IVR_HIGH_THRESHOLD)


# --------------------------------------------------------------------------- #
# Corruption guard + clean delta re-inversion
# --------------------------------------------------------------------------- #
def delta_column_is_degenerate(df: pd.DataFrame) -> bool:
    """True if the vendor delta column is degenerate (corruption fingerprint)."""
    d = df["delta"].to_numpy(dtype=float)
    d = d[np.isfinite(d)]
    if len(d) == 0:
        return True
    ad = np.abs(d)
    share = float(np.mean((ad == 0.0) | (ad == 1.0)))
    return share > DEGENERATE_DELTA_FRAC


def _clean_delta_for_exp(sub: pd.DataFrame, d: _dt.date, expiration: _dt.date,
                         spot: float) -> pd.Series:
    """Re-invert a clean BSM delta for every row of one expiration from mid/spot/T.

    Uses the audited s6_recon BSM (implied_vol_from_mid -> bs_delta). Returns a Series of
    deltas aligned to sub.index (NaN where the mid is not arb-consistent).
    """
    t = max((expiration - d).days, 0) / _DAYS_PER_YEAR
    mids = ((sub["bid"] + sub["ask"]) / 2.0).to_numpy(dtype=float)
    strikes = sub["strike"].to_numpy(dtype=float)
    is_call = (sub["right"] == "CALL").to_numpy()
    out = np.full(len(sub), np.nan)
    for i in range(len(sub)):
        mid = mids[i]
        if not np.isfinite(mid) or mid <= 0 or t <= 0:
            continue
        iv = recon.implied_vol_from_mid(mid, spot, float(strikes[i]), t, bool(is_call[i]),
                                        RISK_FREE_RATE, DIVIDEND_YIELD)
        if np.isfinite(iv):
            out[i] = recon.bs_delta(spot, float(strikes[i]), t, iv, bool(is_call[i]),
                                    RISK_FREE_RATE, DIVIDEND_YIELD)
    return pd.Series(out, index=sub.index)


# --------------------------------------------------------------------------- #
# Fills — honest net-combo, fraction f of each leg's half-spread
# --------------------------------------------------------------------------- #
def _leg_row(snap: pd.DataFrame, expiration: _dt.date, strike: float,
             right: str) -> pd.Series | None:
    row = snap[(snap["expiration"] == expiration) & (snap["strike"] == strike)
               & (snap["right"] == right)]
    if row.empty:
        return None
    return row.iloc[0]


def _sell_price(bid: float, ask: float, f: float) -> float:
    """Price received SELLING a leg: mid at f=0, bid at f=1 (worse for us as f grows)."""
    mid = 0.5 * (bid + ask)
    return mid - f * (mid - bid)   # = mid - f*half_spread


def _buy_price(bid: float, ask: float, f: float) -> float:
    """Price paid BUYING a leg: mid at f=0, ask at f=1 (worse for us as f grows)."""
    mid = 0.5 * (bid + ask)
    return mid + f * (ask - mid)   # = mid + f*half_spread


@dataclass
class Condor:
    """One iron condor position (a laddered-book entry)."""
    entry_day: _dt.date
    expiration: _dt.date
    entry_dte: int
    short_put: float
    long_put: float
    short_call: float
    long_call: float
    entry_short_put_delta: float
    entry_short_call_delta: float
    entry_credit: float          # net credit received at fill fraction f (points)
    used_clean_delta: bool       # did strike selection use the re-inverted delta?
    # wing geometry (rebuild addendum): realized point widths + long-leg deltas so the
    # report can show the credit/max-loss ratio per config. put_width = short_put-long_put,
    # call_width = long_call-short_call; max_loss (points) = max(put_width, call_width).
    put_wing_width: float = float("nan")
    call_wing_width: float = float("nan")
    entry_long_put_delta: float = float("nan")
    entry_long_call_delta: float = float("nan")
    # filled by management:
    traded: bool = True
    exit_day: _dt.date | None = None
    exit_dte: int | None = None
    exit_debit: float = float("nan")   # cost to close (points); intrinsic if settled
    exit_reason: str = ""              # 'target' | 'time_stop' | 'settle' | 'expiry'
    pnl_points: float = float("nan")
    pnl_dollars: float = float("nan")


def _condor_open_credit(snap: pd.DataFrame, expiration: _dt.date,
                        sp: float, lp: float, sc: float, lc: float,
                        f: float) -> float | None:
    """Net credit to OPEN the condor at fill fraction f. None if any leg unquoted.

    Sell short put + short call (receive bid-side), buy long put + long call (pay ask-side).
    """
    rows = {
        ("sp", "PUT"): _leg_row(snap, expiration, sp, "PUT"),
        ("lp", "PUT"): _leg_row(snap, expiration, lp, "PUT"),
        ("sc", "CALL"): _leg_row(snap, expiration, sc, "CALL"),
        ("lc", "CALL"): _leg_row(snap, expiration, lc, "CALL"),
    }
    if any(v is None for v in rows.values()):
        return None
    sp_r, lp_r, sc_r, lc_r = (rows[("sp", "PUT")], rows[("lp", "PUT")],
                              rows[("sc", "CALL")], rows[("lc", "CALL")])
    credit = (_sell_price(sp_r["bid"], sp_r["ask"], f)
              + _sell_price(sc_r["bid"], sc_r["ask"], f)
              - _buy_price(lp_r["bid"], lp_r["ask"], f)
              - _buy_price(lc_r["bid"], lc_r["ask"], f))
    return float(credit)


def _condor_close_debit(snap: pd.DataFrame, c: Condor, f: float) -> float | None:
    """Net debit to CLOSE the condor at fill fraction f (mark or exit). None if unquoted.

    Buy back shorts (pay ask-side), sell longs (receive bid-side).
    """
    sp_r = _leg_row(snap, c.expiration, c.short_put, "PUT")
    lp_r = _leg_row(snap, c.expiration, c.long_put, "PUT")
    sc_r = _leg_row(snap, c.expiration, c.short_call, "CALL")
    lc_r = _leg_row(snap, c.expiration, c.long_call, "CALL")
    if sp_r is None or lp_r is None or sc_r is None or lc_r is None:
        return None
    debit = (_buy_price(sp_r["bid"], sp_r["ask"], f)
             + _buy_price(sc_r["bid"], sc_r["ask"], f)
             - _sell_price(lp_r["bid"], lp_r["ask"], f)
             - _sell_price(lc_r["bid"], lc_r["ask"], f))
    return float(debit)


def _condor_intrinsic(settle_price: float, c: Condor) -> float:
    """Cash-settlement value (debit to unwind) of the condor at expiry, given settle price.

    Put side loss (capped by wing): max(0, short_put - S) - max(0, long_put - S).
    Call side loss (capped by wing): max(0, S - short_call) - max(0, S - long_call).
    Returns the intrinsic debit (>=0) that the seller pays to settle.
    """
    S = settle_price
    put_loss = max(0.0, c.short_put - S) - max(0.0, c.long_put - S)
    call_loss = max(0.0, S - c.short_call) - max(0.0, S - c.long_call)
    return float(put_loss + call_loss)


# --------------------------------------------------------------------------- #
# Strike selection at entry (uses ONLY the entry-day snapshot => no look-ahead)
# --------------------------------------------------------------------------- #
def _pick_short_strike(exp_side: pd.DataFrame, right: str, target_delta: float,
                       delta_series: pd.Series) -> float | None:
    """Nearest-|delta| strike on one side, using the supplied (clean) delta series."""
    side = exp_side[exp_side["right"] == right].copy()
    side = side.assign(_d=delta_series.reindex(side.index))
    side = side[side["_d"].notna()]
    if side.empty:
        return None
    side["_err"] = (side["_d"].abs() - target_delta).abs()
    return float(side.sort_values("_err").iloc[0]["strike"])


def _choose_expiration(day_df: pd.DataFrame, target_dte: int) -> _dt.date | None:
    """Listed expiration whose DTE is nearest the target (>= 7 days out to avoid the
    near-week; a 45/30-DTE income trade is never opened inside a week of expiry)."""
    exps = day_df[day_df["dte"] >= 7][["expiration", "dte"]].drop_duplicates()
    if exps.empty:
        return None
    exps = exps.assign(err=(exps["dte"] - target_dte).abs())
    return exps.sort_values("err").iloc[0]["expiration"]


def _clean_delta_series(day_df: pd.DataFrame, sub: pd.DataFrame, d: _dt.date,
                        expiration: _dt.date, spot: float) -> tuple[pd.Series, bool]:
    """Return (clean delta series aligned to sub.index, used_clean flag).

    Vendor delta if the day's column is not degenerate (with per-leg BSM re-inversion of
    any missing/degenerate individual leg), else the full BSM re-inversion. Shared by the
    condor and CSP builders so strike selection is IDENTICAL to the pre-registered path.
    """
    used_clean = delta_column_is_degenerate(day_df)
    if used_clean:
        return _clean_delta_for_exp(sub, d, expiration, spot), True
    delta_series = sub["delta"].copy()
    ad = delta_series.abs()
    bad = delta_series.isna() | (ad == 0.0) | (ad == 1.0)
    if bad.any():
        clean = _clean_delta_for_exp(sub.loc[bad.index[bad]], d, expiration, spot)
        delta_series.loc[clean.index] = clean.values
    return delta_series, False


def _pick_long_wing(sub: pd.DataFrame, right: str, short_strike: float, spot: float,
                    wing_spec: tuple, delta_series: pd.Series) -> float | None:
    """Select the long protective wing strike for one side.

    wing_spec = ("points", W)  -> short_strike -/+ W (fixed-width control, the original path).
    wing_spec = ("delta",  d0) -> nearest-|delta|-to-d0 strike that is STRICTLY further OTM
      than the short (long_put < short_put < spot ; spot < short_call < long_call). If the
      nearest-delta strike is not further OTM, step outward to the next valid strike. Returns
      None if no valid further-OTM strike exists on this side.
    """
    kind, val = wing_spec
    is_put = (right == "PUT")
    if kind == "points":
        return (short_strike - val) if is_put else (short_strike + val)
    if kind != "delta":
        raise ValueError(f"unknown wing_spec kind {kind!r}")
    # delta-selected wing, restricted to strictly-further-OTM strikes on the correct side.
    side = sub[sub["right"] == right].copy()
    side = side.assign(_d=delta_series.reindex(side.index))
    side = side[side["_d"].notna()]
    if is_put:
        side = side[side["strike"] < short_strike]   # further OTM = LOWER strike
    else:
        side = side[side["strike"] > short_strike]   # further OTM = HIGHER strike
    if side.empty:
        return None
    side = side.assign(_err=(side["_d"].abs() - val).abs())
    return float(side.sort_values("_err").iloc[0]["strike"])


def build_condor(day_df: pd.DataFrame, d: _dt.date, target_dte: int,
                 target_delta: float, f: float,
                 wing_spec: tuple = ("points", WING_WIDTH)) -> Condor | None:
    """Open a condor on day d for the given DTE/delta targets at fill fraction f.

    Strike selection uses a CLEAN delta: vendor delta if the day's column is not
    degenerate, else (and per-leg where vendor delta is missing) the BSM re-inversion.
    `wing_spec` picks the long wings: ("delta", 0.05) for the CBOE 5-delta wing (primary
    hypothesis) or ("points", W) for a fixed-width control (25 or 50). Returns None if the
    structure cannot be built (missing legs / no expiration / no valid further-OTM wing).
    """
    expiration = _choose_expiration(day_df, target_dte)
    if expiration is None:
        return None
    sub = day_df[day_df["expiration"] == expiration].copy()
    if sub.empty:
        return None
    spot = float(sub["underlying_price"].iloc[0])
    if not np.isfinite(spot) or spot <= 0:
        return None

    delta_series, used_clean = _clean_delta_series(day_df, sub, d, expiration, spot)

    short_put = _pick_short_strike(sub, "PUT", target_delta, delta_series)
    short_call = _pick_short_strike(sub, "CALL", target_delta, delta_series)
    if short_put is None or short_call is None:
        return None
    if not (short_put < spot < short_call):
        # Degenerate placement (e.g. both strikes same side) — refuse to open.
        return None

    long_put = _pick_long_wing(sub, "PUT", short_put, spot, wing_spec, delta_series)
    long_call = _pick_long_wing(sub, "CALL", short_call, spot, wing_spec, delta_series)
    if long_put is None or long_call is None:
        return None
    # Guarantee the defined-risk ordering (delta wings can, in pathological chains, land
    # on/above the short even after the further-OTM filter — refuse to open if so).
    if not (long_put < short_put < spot < short_call < long_call):
        return None

    credit = _condor_open_credit(sub, expiration, short_put, long_put,
                                 short_call, long_call, f)
    if credit is None or not np.isfinite(credit) or credit <= 0:
        return None

    def _dlt(strike, right):
        r = sub[(sub["strike"] == strike) & (sub["right"] == right)]
        if r.empty:
            return float("nan")
        return float(delta_series.reindex(r.index).iloc[0])

    return Condor(
        entry_day=d, expiration=expiration, entry_dte=int((expiration - d).days),
        short_put=short_put, long_put=long_put, short_call=short_call, long_call=long_call,
        entry_short_put_delta=_dlt(short_put, "PUT"),
        entry_short_call_delta=_dlt(short_call, "CALL"),
        entry_credit=credit, used_clean_delta=used_clean,
        put_wing_width=float(short_put - long_put),
        call_wing_width=float(long_call - short_call),
        entry_long_put_delta=_dlt(long_put, "PUT"),
        entry_long_call_delta=_dlt(long_call, "CALL"),
    )


# --------------------------------------------------------------------------- #
# Fast per-(day, expiration) price map (memoized) — speed only, logic unchanged.
# --------------------------------------------------------------------------- #
def build_price_map(day_df: pd.DataFrame) -> dict:
    """One dict per day: {expiration: {(strike, right): (bid, ask)}, '_spot': {exp: spot}}.

    Turns the O(scan) `day_df[df.expiration==exp & df.strike==k & df.right==r]` lookups in
    the forward-walk into O(1) dict gets. Pure repackaging of the SAME rows — no logic
    change, no look-ahead. Memoized by the caller so it is built at most once per day.
    """
    out: dict = {}
    spot: dict = {}
    exp = day_df["expiration"].to_numpy()
    strike = day_df["strike"].to_numpy(dtype=float)
    right = day_df["right"].to_numpy()
    bid = day_df["bid"].to_numpy(dtype=float)
    ask = day_df["ask"].to_numpy(dtype=float)
    und = day_df["underlying_price"].to_numpy(dtype=float)
    for i in range(len(day_df)):
        e = exp[i]
        d = out.get(e)
        if d is None:
            d = {}
            out[e] = d
            spot[e] = float(und[i])
        d[(strike[i], right[i])] = (bid[i], ask[i])
    out["_spot"] = spot
    return out


def _pm_get(price_maps: dict, d: _dt.date, loader) -> dict | None:
    """Memoized price map for day d (built lazily from the cached day_df)."""
    if d in price_maps:
        return price_maps[d]
    ddf = loader(d)
    pm = build_price_map(ddf) if (ddf is not None and len(ddf)) else None
    price_maps[d] = pm
    return pm


def _close_debit_pm(pm: dict, c: Condor, f: float) -> float | None:
    """Net debit to CLOSE the condor from a price map (buy shorts, sell longs). None if any
    leg is not in the map for this expiration."""
    exp_map = pm.get(c.expiration)
    if not exp_map:
        return None
    sp = exp_map.get((c.short_put, "PUT"))
    lp = exp_map.get((c.long_put, "PUT"))
    sc = exp_map.get((c.short_call, "CALL"))
    lc = exp_map.get((c.long_call, "CALL"))
    if sp is None or lp is None or sc is None or lc is None:
        return None
    debit = (_buy_price(sp[0], sp[1], f) + _buy_price(sc[0], sc[1], f)
             - _sell_price(lp[0], lp[1], f) - _sell_price(lc[0], lc[1], f))
    return float(debit)


# --------------------------------------------------------------------------- #
# Management — walk days forward, causal, first rule wins
# --------------------------------------------------------------------------- #
def manage_condor(c: Condor, day_loader, all_days: list[_dt.date],
                  management: str, target_frac: float, f: float,
                  price_maps: dict | None = None) -> Condor:
    """Manage one condor forward from the day AFTER entry to expiry.

    management: 'hold' (control) | 'managed' (target_frac profit-take + 21-DTE time-stop).
    day_loader(d) -> normalized day_df (cached by caller). Causal: only marks with days
    strictly after entry and stops at the FIRST firing day. `price_maps` (optional) memoizes
    the per-day (strike,right)->(bid,ask) lookup — a pure speed cache, identical results.
    """
    if price_maps is None:
        price_maps = {}
    future = [d for d in all_days if c.entry_day < d <= c.expiration]
    take_debit = (1.0 - target_frac) * c.entry_credit  # close when debit <= this
    last_mark_debit = float("nan")
    last_mark_day = c.entry_day

    for d in future:
        # At/after expiration: cash-settle at intrinsic using that day's spot.
        if d >= c.expiration:
            break
        pm = _pm_get(price_maps, d, day_loader)
        if pm is None:
            continue
        debit = _close_debit_pm(pm, c, f)
        if debit is None:
            continue
        last_mark_debit = debit
        last_mark_day = d
        dte = (c.expiration - d).days

        if management == "managed":
            if debit <= take_debit:
                return _finalize(c, d, dte, debit, "target", f)
            if dte <= TIME_STOP_DTE:
                return _finalize(c, d, dte, debit, "time_stop", f)
        # 'hold' arm: never closes early; falls through to settlement.

    # Ran to expiry (or ran out of marks before it): cash-settle at intrinsic.
    settle_pm = _pm_get(price_maps, c.expiration, day_loader)
    settle_price = None
    if settle_pm is not None:
        settle_price = settle_pm.get("_spot", {}).get(c.expiration)
    if settle_price is None:
        # No expiry-day file: settle at the last available underlying we can see.
        for d in reversed(future):
            pm = _pm_get(price_maps, d, day_loader)
            if pm is not None and pm.get("_spot"):
                # any expiration's stored spot on that day is the same underlying close
                settle_price = next(iter(pm["_spot"].values()))
                last_mark_day = d
                break
    if settle_price is None:
        # Could not mark or settle at all — fall back to last mark (or a full loss cap).
        if np.isfinite(last_mark_debit):
            return _finalize(c, last_mark_day, (c.expiration - last_mark_day).days,
                             last_mark_debit, "settle", f)
        # No data whatsoever after entry: treat as unresolved max-defined-risk loss.
        # Worst case = the wider wing fully breached (defined risk on the losing side).
        widths = [w for w in (c.put_wing_width, c.call_wing_width) if np.isfinite(w)]
        max_debit = max(widths) if widths else WING_WIDTH
        return _finalize(c, c.expiration, 0, max_debit, "settle", f)

    intrinsic = _condor_intrinsic(settle_price, c)
    return _finalize(c, c.expiration, 0, intrinsic, "expiry", f)


def _finalize(c: Condor, exit_day: _dt.date, exit_dte: int, exit_debit: float,
              reason: str, f: float) -> Condor:
    c.exit_day = exit_day
    c.exit_dte = int(exit_dte)
    c.exit_debit = float(exit_debit)
    c.exit_reason = reason
    c.pnl_points = c.entry_credit - exit_debit
    c.pnl_dollars = c.pnl_points * CONTRACT_MULTIPLIER
    return c


# --------------------------------------------------------------------------- #
# ATM cash-secured PUT benchmark arm (rebuild addendum)
# --------------------------------------------------------------------------- #
@dataclass
class CashSecuredPut:
    """One cash-secured short put (a laddered-book entry). Held to expiry, cash-settled."""
    entry_day: _dt.date
    expiration: _dt.date
    entry_dte: int
    strike: float
    entry_delta: float
    entry_credit: float          # premium received at fill fraction f (points)
    used_clean_delta: bool
    exit_day: _dt.date | None = None
    exit_dte: int | None = None
    exit_debit: float = float("nan")   # intrinsic max(0, strike - settle) at expiry
    exit_reason: str = ""
    pnl_points: float = float("nan")
    pnl_dollars: float = float("nan")


def build_csp(day_df: pd.DataFrame, d: _dt.date, target_dte: int,
              f: float) -> CashSecuredPut | None:
    """Open an ATM (~0.50-delta) cash-secured put on day d at fill fraction f.

    Reuses the SAME clean-delta selection path as the condor. Falls back to nearest strike
    to spot if no clean delta is available. Sell the put (receive bid-side at fraction f).
    Returns None if the structure can't be built.
    """
    expiration = _choose_expiration(day_df, target_dte)
    if expiration is None:
        return None
    sub = day_df[day_df["expiration"] == expiration].copy()
    if sub.empty:
        return None
    spot = float(sub["underlying_price"].iloc[0])
    if not np.isfinite(spot) or spot <= 0:
        return None

    delta_series, used_clean = _clean_delta_series(day_df, sub, d, expiration, spot)

    strike = _pick_short_strike(sub, "PUT", 0.50, delta_series)
    if strike is None:
        # fallback: nearest listed put strike to spot.
        puts = sub[sub["right"] == "PUT"]
        if puts.empty:
            return None
        strike = float(puts.iloc[(puts["strike"] - spot).abs().argsort().iloc[0]]["strike"])

    row = _leg_row(sub, expiration, strike, "PUT")
    if row is None:
        return None
    credit = _sell_price(row["bid"], row["ask"], f)
    if not np.isfinite(credit) or credit <= 0:
        return None

    r = sub[(sub["strike"] == strike) & (sub["right"] == "PUT")]
    entry_delta = float(delta_series.reindex(r.index).iloc[0]) if not r.empty else float("nan")

    return CashSecuredPut(
        entry_day=d, expiration=expiration, entry_dte=int((expiration - d).days),
        strike=strike, entry_delta=entry_delta, entry_credit=float(credit),
        used_clean_delta=used_clean,
    )


def manage_csp(c: CashSecuredPut, day_loader, all_days: list[_dt.date]) -> CashSecuredPut:
    """Hold to expiry, cash-settle at intrinsic max(0, strike - settle_price).

    Same forward-walk settlement machinery as the condor 'hold' arm: find the expiry-day
    underlying (or the last available), settle at intrinsic. Causal.
    """
    future = [d for d in all_days if c.entry_day < d <= c.expiration]
    settle_df = day_loader(c.expiration)
    settle_price = None
    if settle_df is not None:
        s = settle_df[settle_df["expiration"] == c.expiration]
        if not s.empty:
            settle_price = float(s["underlying_price"].iloc[0])
    if settle_price is None:
        for d in reversed(future):
            ddf = day_loader(d)
            if ddf is not None and len(ddf):
                settle_price = float(ddf["underlying_price"].iloc[0])
                break
    if settle_price is None:
        # No post-entry data at all: worst-case cash-secured loss = full strike (put to 0)
        # is unrealistic; use last-known entry spot as settle (no move) -> intrinsic 0.
        settle_price = c.strike
    intrinsic = max(0.0, c.strike - settle_price)
    c.exit_day = c.expiration
    c.exit_dte = 0
    c.exit_debit = float(intrinsic)
    c.exit_reason = "settle"
    c.pnl_points = c.entry_credit - intrinsic
    c.pnl_dollars = c.pnl_points * CONTRACT_MULTIPLIER
    return c


# --------------------------------------------------------------------------- #
# CSP daily book mark-to-market (alpha-vs-beta study, pre-registered 2026-07-06)
#
# Marks the WHOLE open short-put book each trading day at the EOD buy-back price (fill f),
# producing a daily mark-to-market P&L series and a daily reserved-capital / dollar-delta
# series. Reuses the SAME honest-fill helper (_buy_price) and price-map cache as the condor
# forward-walk — no new pricing logic, no look-ahead (a day's mark uses only that day's
# quotes; a put contributes marks only on days within [entry, expiry)).
# --------------------------------------------------------------------------- #
def _put_buyback_pm(pm: dict, expiration: _dt.date, strike: float,
                    f: float) -> float | None:
    """Buy-back debit for ONE short put from a price map at fill fraction f. None if unquoted."""
    exp_map = pm.get(expiration)
    if not exp_map:
        return None
    leg = exp_map.get((strike, "PUT"))
    if leg is None:
        return None
    return float(_buy_price(leg[0], leg[1], f))


def csp_book_daily_marks(csps: list["CashSecuredPut"], day_loader,
                         all_days: list[_dt.date], f: float,
                         price_maps: dict | None = None) -> pd.DataFrame:
    """Daily mark-to-market of the whole weekly-laddered short-put book at fill fraction f.

    For each trading day d, sum over every put that is OPEN on d (entry_day < d <= expiry):
      * the put's book VALUE (liability) = current buy-back debit at fill f, in points; on the
        expiry day the value is the settled intrinsic max(0, K - settle) (no quote needed).
      * reserved capital = K * 100 dollars (cash-secured).
      * dollar-delta = |clean entry delta| * spot(d) * 100  (short-put long-market exposure).

    Book EQUITY on day d (dollars) = Σ entry_credit*100 (premium collected up-front)
                                     − Σ current_value*100 (mark-to-market liability).
    Daily P&L = equity(d) − equity(d-1). Daily return = daily P&L / reserved_capital(d).

    A put enters the book the day AFTER its entry (first mark) and leaves after its expiry
    mark. Strictly causal: day d's mark reads only day d's price map. Returns a DataFrame
    indexed by date with columns [equity, pnl, reserved_capital, dollar_delta, n_open, ret].
    """
    if price_maps is None:
        price_maps = {}
    if not csps:
        return pd.DataFrame(columns=["equity", "pnl", "reserved_capital",
                                     "dollar_delta", "n_open", "ret"])

    # Per-put static facts.
    credit100 = {i: c.entry_credit * CONTRACT_MULTIPLIER for i, c in enumerate(csps)}
    reserve100 = {i: c.strike * CONTRACT_MULTIPLIER for i, c in enumerate(csps)}
    entry_absdelta = {i: (abs(c.entry_delta) if np.isfinite(c.entry_delta) else 0.50)
                      for i, c in enumerate(csps)}

    first_entry = min(c.entry_day for c in csps)
    last_exp = max(c.expiration for c in csps)
    marks_days = [d for d in all_days if first_entry < d <= last_exp]

    rows = {}
    for d in marks_days:
        pm = _pm_get(price_maps, d, day_loader)
        # spot for dollar-delta: any expiration's stored spot is that day's underlying close.
        spot_d = None
        if pm is not None and pm.get("_spot"):
            spot_d = next(iter(pm["_spot"].values()))
        equity = 0.0
        reserved = 0.0
        ddelta = 0.0
        n_open = 0
        for i, c in enumerate(csps):
            if not (c.entry_day < d <= c.expiration):
                continue
            n_open += 1
            reserved += reserve100[i]
            if spot_d is not None:
                ddelta += entry_absdelta[i] * spot_d * CONTRACT_MULTIPLIER
            # current liability value (points)
            if d == c.expiration:
                # settled intrinsic — no quote needed (uses c.exit_debit set by manage_csp,
                # or recompute from settle spot if not yet managed).
                if np.isfinite(c.exit_debit):
                    value = c.exit_debit
                elif spot_d is not None:
                    value = max(0.0, c.strike - spot_d)
                else:
                    value = 0.0
            else:
                bb = _put_buyback_pm(pm, c.expiration, c.strike, f) if pm is not None else None
                if bb is None:
                    # unquoted mid-life day (e.g. blackout): carry last known value by using
                    # intrinsic vs current spot as a floor; if no spot, treat as full credit
                    # retained (value ~ entry_credit) to avoid fabricating a gain.
                    if spot_d is not None:
                        value = max(0.0, c.strike - spot_d)
                    else:
                        value = c.entry_credit
                else:
                    value = bb
            equity += credit100[i] - value * CONTRACT_MULTIPLIER
        rows[d] = dict(equity=equity, reserved_capital=reserved,
                       dollar_delta=ddelta, n_open=n_open)

    df = pd.DataFrame.from_dict(rows, orient="index").sort_index()
    if df.empty:
        return pd.DataFrame(columns=["equity", "pnl", "reserved_capital",
                                     "dollar_delta", "n_open", "ret"])
    df.index = pd.to_datetime(df.index)
    df["pnl"] = df["equity"].diff()
    df.loc[df.index[0], "pnl"] = df["equity"].iloc[0]  # day-1 P&L = first-day equity change
    # daily return on reserved capital (guard against 0 open days)
    denom = df["reserved_capital"].replace(0.0, np.nan)
    df["ret"] = df["pnl"] / denom
    df["ret"] = df["ret"].fillna(0.0)
    return df[["equity", "pnl", "reserved_capital", "dollar_delta", "n_open", "ret"]]


# --------------------------------------------------------------------------- #
# Weekly-laddered entry schedule
# --------------------------------------------------------------------------- #
def weekly_entry_days(days: list[_dt.date]) -> list[_dt.date]:
    """First available trading day of each ISO calendar week (the weekly ladder)."""
    seen: dict[tuple[int, int], _dt.date] = {}
    for d in sorted(days):
        key = (d.isocalendar().year, d.isocalendar().week)
        if key not in seen:
            seen[key] = d
    return sorted(seen.values())


# --------------------------------------------------------------------------- #
# Full backtest for one (dte, delta, management, fill) config
# --------------------------------------------------------------------------- #
def _wing_tag(wing_spec: tuple) -> str:
    kind, val = wing_spec
    # delta wings tagged by delta*100 (0.05 -> w5d); point wings by width (25 -> w25p).
    return f"w{int(round(val * 100))}d" if kind == "delta" else f"w{int(round(val))}p"


def config_tag(target_dte: int, target_delta: float, wing_spec: tuple, management: str,
               target_frac: float, ivr_filter: str, f: float) -> str:
    """Stable config identifier used across the grid tables and CSVs."""
    mgmt = management + (f"{int(target_frac*100)}" if management == "managed" else "")
    return (f"dte{target_dte}_d{int(target_delta*100)}_{_wing_tag(wing_spec)}"
            f"_{mgmt}_ivr{ivr_filter}_f{f}")


def run_config(target_dte: int, target_delta: float, management: str,
               target_frac: float, f: float,
               wing_spec: tuple = ("points", WING_WIDTH),
               ivr_filter: str = "always",
               ivr_series: pd.Series | None = None,
               days: list[_dt.date] | None = None,
               day_cache: dict | None = None,
               price_maps: dict | None = None,
               verbose: bool = False) -> pd.DataFrame:
    """Run the weekly-laddered condor book for one config. Returns a trade DataFrame.

    wing_spec threads the wing construction (delta vs points). ivr_filter gates entries on
    the causal IV-rank ('always' | 'high'); ivr_series is the precomputed trailing-VIX-rank.
    price_maps is a SHARED per-day (strike,right)->(bid,ask) cache reused across configs
    (pure speed; identical results). day_cache and price_maps should be passed by the runner
    so the day load + repackage happen at most once for the whole grid.
    """
    if days is None:
        days = available_days()
    if day_cache is None:
        day_cache = {}
    if price_maps is None:
        price_maps = {}

    def loader(d: _dt.date):
        if d not in day_cache:
            day_cache[d] = load_day(d)
        return day_cache[d]

    entries = weekly_entry_days(days)
    trades: list[dict] = []
    n = len(entries)
    tag = config_tag(target_dte, target_delta, wing_spec, management, target_frac,
                     ivr_filter, f)
    for i, ed in enumerate(entries, 1):
        if not ivr_passes(ivr_series, ed, ivr_filter):
            continue
        day_df = loader(ed)
        if day_df is None or len(day_df) == 0:
            continue
        c = build_condor(day_df, ed, target_dte, target_delta, f, wing_spec=wing_spec)
        if c is None:
            continue
        c = manage_condor(c, loader, days, management, target_frac, f,
                          price_maps=price_maps)
        rec = asdict(c)
        rec["config"] = tag
        trades.append(rec)
        if verbose and (i % 50 == 0 or i == n):
            print(f"  [{i}/{n}] entries processed", flush=True)
    return pd.DataFrame(trades)


def run_csp_config(target_dte: int, f: float,
                   days: list[_dt.date] | None = None,
                   day_cache: dict | None = None,
                   verbose: bool = False) -> pd.DataFrame:
    """Run the weekly-laddered ATM cash-secured-put book for one (DTE, fill). Hold-to-expiry."""
    if days is None:
        days = available_days()
    if day_cache is None:
        day_cache = {}

    def loader(d: _dt.date):
        if d not in day_cache:
            day_cache[d] = load_day(d)
        return day_cache[d]

    entries = weekly_entry_days(days)
    trades: list[dict] = []
    for ed in entries:
        day_df = loader(ed)
        if day_df is None or len(day_df) == 0:
            continue
        c = build_csp(day_df, ed, target_dte, f)
        if c is None:
            continue
        c = manage_csp(c, loader, days)
        rec = asdict(c)
        rec["config"] = f"csp_dte{target_dte}_f{f}"
        trades.append(rec)
    return pd.DataFrame(trades)


def run_csp_book(target_dte: int, f: float,
                 days: list[_dt.date] | None = None,
                 day_cache: dict | None = None) -> list["CashSecuredPut"]:
    """Same weekly-laddered ATM CSP book as run_csp_config, but return the managed OBJECTS.

    Identical entry/management path (byte-for-byte the same trades). The object list is what
    the daily book mark-to-market (csp_book_daily_marks) needs. Kept separate so the existing
    run_csp_config DataFrame API is untouched.
    """
    if days is None:
        days = available_days()
    if day_cache is None:
        day_cache = {}

    def loader(d: _dt.date):
        if d not in day_cache:
            day_cache[d] = load_day(d)
        return day_cache[d]

    out: list[CashSecuredPut] = []
    for ed in weekly_entry_days(days):
        day_df = loader(ed)
        if day_df is None or len(day_df) == 0:
            continue
        c = build_csp(day_df, ed, target_dte, f)
        if c is None:
            continue
        out.append(manage_csp(c, loader, days))
    return out


def spx_daily_returns(all_days: list[_dt.date], day_cache: dict,
                      day_loader) -> pd.Series:
    """SPX daily close + simple daily return from warehouse underlying_price.

    The warehouse option chains carry `underlying_price` per day — that is a valid SPX close
    series (the same close used to settle). We take one underlying_price per day (all rows on a
    day share it) and form simple returns. READ-ONLY; no external series needed. Blackout days
    still carry a valid underlying_price (only the NBBO was zeroed), so the SPX series is
    continuous across the blackout even though the option book skips those entry-weeks.
    Returns a Series indexed by Timestamp of daily simple returns (first day NaN dropped).
    """
    closes = {}
    for d in all_days:
        ddf = day_loader(d)
        if ddf is None or len(ddf) == 0:
            # blackout / empty chain: read underlying_price straight from the raw file.
            p = _fpath(d)
            if p.is_file():
                try:
                    raw = pd.read_parquet(p, columns=["underlying_price"])
                    up = raw["underlying_price"].dropna()
                    if len(up):
                        closes[d] = float(up.iloc[0])
                except Exception:
                    pass
            continue
        up = float(ddf["underlying_price"].iloc[0])
        if np.isfinite(up) and up > 0:
            closes[d] = up
    s = pd.Series(closes).sort_index()
    s.index = pd.to_datetime(s.index)
    return s.pct_change().dropna()
