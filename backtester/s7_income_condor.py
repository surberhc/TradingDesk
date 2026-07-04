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

CONTRACT_MULTIPLIER = 100.0   # SPX options are $100/point
WING_WIDTH = 25.0             # fixed 25-pt protective wing each side (pre-registered)
TIME_STOP_DTE = 21            # managed arms close at DTE <= 21 (pre-registered)

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


def build_condor(day_df: pd.DataFrame, d: _dt.date, target_dte: int,
                 target_delta: float, f: float) -> Condor | None:
    """Open a condor on day d for the given DTE/delta targets at fill fraction f.

    Strike selection uses a CLEAN delta: vendor delta if the day's column is not
    degenerate, else (and per-leg where vendor delta is missing) the BSM re-inversion.
    Returns None if the structure cannot be built (missing legs / no expiration).
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

    used_clean = delta_column_is_degenerate(day_df)
    if used_clean:
        delta_series = _clean_delta_for_exp(sub, d, expiration, spot)
    else:
        delta_series = sub["delta"].copy()
        # belt-and-suspenders: re-invert any individual leg whose vendor delta is
        # missing or degenerate (|delta| exactly 0 or 1), so a single bad leg near ATM
        # can't silently mis-select the short strike.
        ad = delta_series.abs()
        bad = delta_series.isna() | (ad == 0.0) | (ad == 1.0)
        if bad.any():
            clean = _clean_delta_for_exp(sub.loc[bad.index[bad]], d, expiration, spot)
            delta_series.loc[clean.index] = clean.values

    short_put = _pick_short_strike(sub, "PUT", target_delta, delta_series)
    short_call = _pick_short_strike(sub, "CALL", target_delta, delta_series)
    if short_put is None or short_call is None:
        return None
    if not (short_put < spot < short_call):
        # Degenerate placement (e.g. both strikes same side) — refuse to open.
        return None
    long_put = short_put - WING_WIDTH
    long_call = short_call + WING_WIDTH

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
    )


# --------------------------------------------------------------------------- #
# Management — walk days forward, causal, first rule wins
# --------------------------------------------------------------------------- #
def manage_condor(c: Condor, day_loader, all_days: list[_dt.date],
                  management: str, target_frac: float, f: float) -> Condor:
    """Manage one condor forward from the day AFTER entry to expiry.

    management: 'hold' (control) | 'managed' (target_frac profit-take + 21-DTE time-stop).
    day_loader(d) -> normalized day_df (cached by caller). Causal: only marks with days
    strictly after entry and stops at the FIRST firing day.
    """
    future = [d for d in all_days if c.entry_day < d <= c.expiration]
    take_debit = (1.0 - target_frac) * c.entry_credit  # close when debit <= this
    last_mark_debit = float("nan")
    last_mark_day = c.entry_day

    for d in future:
        # At/after expiration: cash-settle at intrinsic using that day's spot.
        if d >= c.expiration:
            break
        day_df = day_loader(d)
        if day_df is None:
            continue
        snap = day_df[day_df["expiration"] == c.expiration]
        if snap.empty:
            continue
        debit = _condor_close_debit(snap, c, f)
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
    settle_df = day_loader(c.expiration)
    settle_price = None
    if settle_df is not None:
        s = settle_df[settle_df["expiration"] == c.expiration]
        if not s.empty:
            settle_price = float(s["underlying_price"].iloc[0])
    if settle_price is None:
        # No expiry-day file: settle at the last available underlying we can see.
        for d in reversed(future):
            ddf = day_loader(d)
            if ddf is not None and len(ddf):
                settle_price = float(ddf["underlying_price"].iloc[0])
                last_mark_day = d
                break
    if settle_price is None:
        # Could not mark or settle at all — fall back to last mark (or a full loss cap).
        if np.isfinite(last_mark_debit):
            return _finalize(c, last_mark_day, (c.expiration - last_mark_day).days,
                             last_mark_debit, "settle", f)
        # No data whatsoever after entry: treat as unresolved max-defined-risk loss.
        max_debit = WING_WIDTH  # worst case: full wing breached one side
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
def run_config(target_dte: int, target_delta: float, management: str,
               target_frac: float, f: float,
               days: list[_dt.date] | None = None,
               day_cache: dict | None = None,
               verbose: bool = False) -> pd.DataFrame:
    """Run the weekly-laddered condor book for one config. Returns a trade DataFrame."""
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
    n = len(entries)
    for i, ed in enumerate(entries, 1):
        day_df = loader(ed)
        if day_df is None or len(day_df) == 0:
            continue
        c = build_condor(day_df, ed, target_dte, target_delta, f)
        if c is None:
            continue
        c = manage_condor(c, loader, days, management, target_frac, f)
        rec = asdict(c)
        rec["config"] = f"dte{target_dte}_d{int(target_delta*100)}_{management}" \
                        + (f"{int(target_frac*100)}" if management == "managed" else "") \
                        + f"_f{f}"
        trades.append(rec)
        if verbose and (i % 50 == 0 or i == n):
            print(f"  [{i}/{n}] entries processed", flush=True)
    return pd.DataFrame(trades)
