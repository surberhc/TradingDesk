r"""
short_strangle.py — Short strangle: is there VRP ALPHA once the trade is delta-neutral?

Pre-registered in docs/PREREG_short_strangle_alpha_2026-07-06.md, committed BEFORE this run.

THE DECISIVE VRP TEST ON SPX. The managed iron condor is refuted (both tenors). The
cash-secured put is refuted-as-alpha (its +P&L was long-equity beta, daily Sharpe ~0.00).
The short strangle is the last vehicle: short OTM put + short OTM call, offsetting deltas,
so it is ~DELTA-NEUTRAL by construction. Almost no beta explains its returns, so any
positive risk-adjusted return net of honest fills IS the volatility risk premium, isolated.

A strangle = a CONDOR WITH NO WINGS: two short legs, settled at UNCAPPED intrinsic
(put side max(0, K_put - S), call side max(0, S - K_call)). We REUSE the S7 machinery for
everything (fill helpers, day cache, clean-delta selection, price-map cache, forward-walk
management, corruption + blackout handling) and the CSP study's regression / block-bootstrap
/ benchmark code. Nothing is re-implemented and nothing is tuned to the data.

PAPER / research only. OFFLINE. STRICTLY READ-ONLY on the warehouse.
"""

from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd

import s7_income_condor as s7
import csp_alpha_beta as cab   # reuse ols_alpha_beta / bootstrap_alpha_ci / daily_metrics

REPORT = Path(__file__).resolve().parent / "output" / "short_strangle_alpha_2026-07-06.md"
CSV_DIR = Path(__file__).resolve().parent / "output" / "s7_research"
CSV_DIR.mkdir(parents=True, exist_ok=True)

WINDOW_START = _dt.date(2018, 6, 1)
WINDOW_END = _dt.date(2026, 7, 31)
OOS_SPLIT = _dt.date(2022, 1, 1)   # train < split ; test >= split

DELTAS = [0.16, 0.20]
DTES = [30, 45]
FILLS = [0.0, 0.25, 0.50, 1.0]
MANAGEMENTS = ["managed", "hold"]

HEADLINE_DELTA = 0.16
HEADLINE_DTE = 45
HEADLINE_MGMT = "managed"
HEADLINE_F = 0.50
MANAGED_TARGET_FRAC = 0.50   # close managed arm at 50% of credit captured

TRADING_DAYS = cab.TRADING_DAYS
CONTRACT_MULTIPLIER = s7.CONTRACT_MULTIPLIER

# Cash / T-bill benchmark: a flat annualized risk-free rate used only for the "beats cash"
# framing. Declared, not tuned. The delta-neutral book's real benchmark is cash, not SPX.
RISK_FREE_ANNUAL = 0.03   # ~3% average T-bill over 2018-2026 (context only; Sharpe uses rf=0)

PLACEBO_SEEDS = 300
PLACEBO_SEED0 = 20260706


# --------------------------------------------------------------------------- #
# Strangle position = wingless condor. UNCAPPED intrinsic settlement.
# --------------------------------------------------------------------------- #
@dataclass
class Strangle:
    """One short strangle (a laddered-book entry): short OTM put + short OTM call, no wings."""
    entry_day: _dt.date
    expiration: _dt.date
    entry_dte: int
    short_put: float
    short_call: float
    entry_short_put_delta: float
    entry_short_call_delta: float
    entry_credit: float           # net credit received at fill fraction f (points)
    used_clean_delta: bool
    net_entry_delta: float = float("nan")   # put_delta + call_delta (should be ~0)
    traded: bool = True
    exit_day: _dt.date | None = None
    exit_dte: int | None = None
    exit_debit: float = float("nan")   # cost to close (points); UNCAPPED intrinsic if settled
    exit_reason: str = ""              # 'target' | 'time_stop' | 'settle' | 'expiry'
    pnl_points: float = float("nan")
    pnl_dollars: float = float("nan")


def _strangle_intrinsic(settle_price: float, s: Strangle) -> float:
    """UNCAPPED cash-settlement debit of the strangle at expiry, given settle price S.

    Put side loss (UNCAPPED — no long wing): max(0, K_put - S).
    Call side loss (UNCAPPED — no long wing): max(0, S - K_call).
    Returns the intrinsic debit (>=0) the seller pays to settle. Unlike the condor this is
    NOT capped at any wing width — a deep crash produces an arbitrarily large put-side loss.
    """
    S = settle_price
    put_loss = max(0.0, s.short_put - S)
    call_loss = max(0.0, S - s.short_call)
    return float(put_loss + call_loss)


def _strangle_open_credit(sub: pd.DataFrame, expiration: _dt.date, sp: float, sc: float,
                          f: float) -> float | None:
    """Net credit to OPEN the strangle at fill fraction f (sell put + sell call). None if any
    leg unquoted. Reuses the S7 honest-fill _sell_price on each short leg."""
    sp_r = s7._leg_row(sub, expiration, sp, "PUT")
    sc_r = s7._leg_row(sub, expiration, sc, "CALL")
    if sp_r is None or sc_r is None:
        return None
    credit = (s7._sell_price(sp_r["bid"], sp_r["ask"], f)
              + s7._sell_price(sc_r["bid"], sc_r["ask"], f))
    return float(credit)


def build_strangle(day_df: pd.DataFrame, d: _dt.date, target_dte: int,
                   target_delta: float, f: float) -> Strangle | None:
    """Open a short strangle on day d for the DTE/delta targets at fill fraction f.

    REUSES the S7 clean-delta selection path (_clean_delta_series + _pick_short_strike) and
    _choose_expiration verbatim, so strike selection is IDENTICAL to the pre-registered
    condor/CSP path. Requires short_put < spot < short_call. NO wings selected. Returns None
    if the structure cannot be built (missing legs / no expiration / degenerate placement).
    """
    expiration = s7._choose_expiration(day_df, target_dte)
    if expiration is None:
        return None
    sub = day_df[day_df["expiration"] == expiration].copy()
    if sub.empty:
        return None
    spot = float(sub["underlying_price"].iloc[0])
    if not np.isfinite(spot) or spot <= 0:
        return None

    delta_series, used_clean = s7._clean_delta_series(day_df, sub, d, expiration, spot)

    short_put = s7._pick_short_strike(sub, "PUT", target_delta, delta_series)
    short_call = s7._pick_short_strike(sub, "CALL", target_delta, delta_series)
    if short_put is None or short_call is None:
        return None
    if not (short_put < spot < short_call):
        return None

    credit = _strangle_open_credit(sub, expiration, short_put, short_call, f)
    if credit is None or not np.isfinite(credit) or credit <= 0:
        return None

    def _dlt(strike, right):
        r = sub[(sub["strike"] == strike) & (sub["right"] == right)]
        if r.empty:
            return float("nan")
        return float(delta_series.reindex(r.index).iloc[0])

    put_d = _dlt(short_put, "PUT")
    call_d = _dlt(short_call, "CALL")
    return Strangle(
        entry_day=d, expiration=expiration, entry_dte=int((expiration - d).days),
        short_put=short_put, short_call=short_call,
        entry_short_put_delta=put_d, entry_short_call_delta=call_d,
        entry_credit=credit, used_clean_delta=used_clean,
        net_entry_delta=float(put_d + call_d) if np.isfinite(put_d) and np.isfinite(call_d)
        else float("nan"),
    )


# --------------------------------------------------------------------------- #
# Close debit from a price map (buy back BOTH shorts). Wingless.
# --------------------------------------------------------------------------- #
def _strangle_close_debit_pm(pm: dict, s: Strangle, f: float) -> float | None:
    """Net debit to CLOSE the strangle from a price map (buy back both shorts at fill f).
    None if either leg is not in the map for this expiration."""
    exp_map = pm.get(s.expiration)
    if not exp_map:
        return None
    sp = exp_map.get((s.short_put, "PUT"))
    sc = exp_map.get((s.short_call, "CALL"))
    if sp is None or sc is None:
        return None
    debit = s7._buy_price(sp[0], sp[1], f) + s7._buy_price(sc[0], sc[1], f)
    return float(debit)


# --------------------------------------------------------------------------- #
# Management — forward-walk, causal, first rule wins. Mirrors manage_condor but
# closes on BOTH short legs and settles at UNCAPPED intrinsic.
# --------------------------------------------------------------------------- #
def manage_strangle(s: Strangle, day_loader, all_days: list[_dt.date],
                    management: str, target_frac: float, f: float,
                    price_maps: dict | None = None) -> Strangle:
    """Manage one strangle forward from the day AFTER entry to expiry.

    management: 'hold' (control) | 'managed' (target_frac profit-take + 21-DTE time-stop).
    Causal: only marks with days strictly after entry, stops at the FIRST firing day.
    Settlement is UNCAPPED intrinsic (no wings). Same forward-walk skeleton as manage_condor.
    """
    if price_maps is None:
        price_maps = {}
    future = [d for d in all_days if s.entry_day < d <= s.expiration]
    take_debit = (1.0 - target_frac) * s.entry_credit
    last_mark_debit = float("nan")
    last_mark_day = s.entry_day

    for d in future:
        if d >= s.expiration:
            break
        pm = s7._pm_get(price_maps, d, day_loader)
        if pm is None:
            continue
        debit = _strangle_close_debit_pm(pm, s, f)
        if debit is None:
            continue
        last_mark_debit = debit
        last_mark_day = d
        dte = (s.expiration - d).days
        if management == "managed":
            if debit <= take_debit:
                return _finalize(s, d, dte, debit, "target", f)
            if dte <= s7.TIME_STOP_DTE:
                return _finalize(s, d, dte, debit, "time_stop", f)

    # Cash-settle at UNCAPPED intrinsic on the expiry-day underlying (or last available).
    settle_pm = s7._pm_get(price_maps, s.expiration, day_loader)
    settle_price = None
    if settle_pm is not None:
        settle_price = settle_pm.get("_spot", {}).get(s.expiration)
    if settle_price is None:
        for d in reversed(future):
            pm = s7._pm_get(price_maps, d, day_loader)
            if pm is not None and pm.get("_spot"):
                settle_price = next(iter(pm["_spot"].values()))
                last_mark_day = d
                break
    if settle_price is None:
        # No data after entry at all: fall back to last mark; else no move -> intrinsic 0.
        if np.isfinite(last_mark_debit):
            return _finalize(s, last_mark_day, (s.expiration - last_mark_day).days,
                             last_mark_debit, "settle", f)
        return _finalize(s, s.expiration, 0, 0.0, "settle", f)

    intrinsic = _strangle_intrinsic(settle_price, s)
    return _finalize(s, s.expiration, 0, intrinsic, "expiry", f)


def _finalize(s: Strangle, exit_day: _dt.date, exit_dte: int, exit_debit: float,
              reason: str, f: float) -> Strangle:
    s.exit_day = exit_day
    s.exit_dte = int(exit_dte)
    s.exit_debit = float(exit_debit)
    s.exit_reason = reason
    s.pnl_points = s.entry_credit - exit_debit
    s.pnl_dollars = s.pnl_points * CONTRACT_MULTIPLIER
    return s


# --------------------------------------------------------------------------- #
# Weekly-laddered book (managed OBJECTS) for one (delta, dte, management, fill)
# --------------------------------------------------------------------------- #
def run_strangle_book(target_dte: int, target_delta: float, management: str, f: float,
                      days: list[_dt.date], day_cache: dict,
                      price_maps: dict | None = None) -> list[Strangle]:
    """Weekly-laddered short-strangle book -> list of managed Strangle objects. Byte-identical
    entry/management to what the daily mark needs. Reuses S7 weekly_entry_days + day cache."""
    if price_maps is None:
        price_maps = {}

    def loader(d):
        if d not in day_cache:
            day_cache[d] = s7.load_day(d)
        return day_cache[d]

    out: list[Strangle] = []
    for ed in s7.weekly_entry_days(days):
        day_df = loader(ed)
        if day_df is None or len(day_df) == 0:
            continue
        s = build_strangle(day_df, ed, target_dte, target_delta, f)
        if s is None:
            continue
        out.append(manage_strangle(s, loader, days, management, MANAGED_TARGET_FRAC, f,
                                   price_maps=price_maps))
    return out


# --------------------------------------------------------------------------- #
# Daily mark-to-market of the WHOLE open strangle book (buy back BOTH legs).
# Mirrors csp_book_daily_marks but wingless + uncapped intrinsic at expiry.
# --------------------------------------------------------------------------- #
def strangle_book_daily_marks(strangles: list[Strangle], day_loader,
                              all_days: list[_dt.date], f: float,
                              price_maps: dict | None = None) -> pd.DataFrame:
    """Daily mark-to-market of the whole weekly-laddered strangle book at fill fraction f.

    For each trading day d, sum over every strangle OPEN on d (entry_day < d <= expiry):
      * book VALUE (liability) = current buy-back debit (both legs) at fill f, in points; on
        the expiry day the value is the settled UNCAPPED intrinsic (no quote needed).
      * reserved capital = put_strike * 100 dollars (per the prereg basis).
      * net dollar-delta = (put_delta + call_delta) * spot(d) * 100 (should be ~0 -> beta ~0).

    Book EQUITY(d) = Σ entry_credit*100 - Σ current_value*100. Daily P&L = equity(d)-equity(d-1).
    Daily return = daily P&L / reserved_capital(d). Strictly causal (day d reads only day d).
    Returns a DataFrame indexed by date with columns
    [equity, pnl, reserved_capital, net_dollar_delta, n_open, ret].
    """
    if price_maps is None:
        price_maps = {}
    cols = ["equity", "pnl", "reserved_capital", "net_dollar_delta", "n_open", "ret"]
    if not strangles:
        return pd.DataFrame(columns=cols)

    credit100 = {i: s.entry_credit * CONTRACT_MULTIPLIER for i, s in enumerate(strangles)}
    reserve100 = {i: s.short_put * CONTRACT_MULTIPLIER for i, s in enumerate(strangles)}
    net_delta = {i: (s.net_entry_delta if np.isfinite(s.net_entry_delta) else 0.0)
                 for i, s in enumerate(strangles)}

    first_entry = min(s.entry_day for s in strangles)
    last_exp = max(s.expiration for s in strangles)
    marks_days = [d for d in all_days if first_entry < d <= last_exp]

    rows = {}
    for d in marks_days:
        pm = s7._pm_get(price_maps, d, day_loader)
        spot_d = None
        if pm is not None and pm.get("_spot"):
            spot_d = next(iter(pm["_spot"].values()))
        equity = 0.0
        reserved = 0.0
        ndelta = 0.0
        n_open = 0
        for i, s in enumerate(strangles):
            if not (s.entry_day < d <= s.expiration):
                continue
            n_open += 1
            reserved += reserve100[i]
            if spot_d is not None:
                ndelta += net_delta[i] * spot_d * CONTRACT_MULTIPLIER
            if d == s.expiration:
                if np.isfinite(s.exit_debit):
                    value = s.exit_debit
                elif spot_d is not None:
                    value = _strangle_intrinsic(spot_d, s)
                else:
                    value = 0.0
            else:
                bb = _strangle_close_debit_pm(pm, s, f) if pm is not None else None
                if bb is None:
                    # unquoted mid-life day (e.g. blackout): fall back to intrinsic vs spot;
                    # if no spot, carry the entry credit (value ~ credit -> no fabricated gain).
                    if spot_d is not None:
                        value = _strangle_intrinsic(spot_d, s)
                    else:
                        value = s.entry_credit
                else:
                    value = bb
            equity += credit100[i] - value * CONTRACT_MULTIPLIER
        rows[d] = dict(equity=equity, reserved_capital=reserved,
                       net_dollar_delta=ndelta, n_open=n_open)

    df = pd.DataFrame.from_dict(rows, orient="index").sort_index()
    if df.empty:
        return pd.DataFrame(columns=cols)
    df.index = pd.to_datetime(df.index)
    df["pnl"] = df["equity"].diff()
    df.loc[df.index[0], "pnl"] = df["equity"].iloc[0]
    denom = df["reserved_capital"].replace(0.0, np.nan)
    df["ret"] = (df["pnl"] / denom).fillna(0.0)
    return df[cols]


# --------------------------------------------------------------------------- #
# Management-vs-hold-vs-placebo (on TOTAL P&L)
# --------------------------------------------------------------------------- #
def random_exit_placebo_total_pnl(hold_strangles: list[Strangle],
                                  managed_hold_days: np.ndarray, day_loader,
                                  all_days: list[_dt.date], f: float,
                                  price_maps: dict, n_seeds: int = PLACEBO_SEEDS,
                                  seed0: int = PLACEBO_SEED0) -> dict:
    """Placebo: exit each HOLD-arm strangle after a RANDOM number of trading days drawn to
    match the managed arm's realized holding-period distribution. If the strangle is still
    open at that random day, close it at the marked buy-back debit (fill f); otherwise it
    already settled (use its settle P&L). Repeat over n_seeds and report the mean/percentiles
    of total book P&L. Tests whether the managed timing is better than random timing of the
    same distribution — the pre-registered 'is management just luck?' control."""
    hold_days_pool = managed_hold_days[managed_hold_days > 0]
    if len(hold_days_pool) == 0:
        return dict(mean=float("nan"), lo=float("nan"), hi=float("nan"), n=0)

    # Precompute each strangle's forward trading days + marked debit path once (causal).
    per_trade_paths = []
    for s in hold_strangles:
        future = [d for d in all_days if s.entry_day < d <= s.expiration]
        debits = []
        for d in future:
            pm = s7._pm_get(price_maps, d, day_loader)
            debit = _strangle_close_debit_pm(pm, s, f) if pm is not None else None
            debits.append(debit)
        settle_pnl = s.pnl_dollars  # hold arm's realized (settled) P&L
        per_trade_paths.append((future, debits, settle_pnl, s.entry_credit))

    rng = np.random.default_rng(seed0)
    totals = np.empty(n_seeds)
    for b in range(n_seeds):
        total = 0.0
        for (future, debits, settle_pnl, credit) in per_trade_paths:
            if len(future) == 0:
                total += settle_pnl
                continue
            hold_n = int(rng.choice(hold_days_pool))
            idx = hold_n - 1  # exit after hold_n trading days from entry
            if idx >= len(future) or idx < 0:
                # random exit lands at/after expiry -> the trade settled: use its P&L
                total += settle_pnl
                continue
            debit = debits[idx]
            if debit is None:
                total += settle_pnl
                continue
            total += (credit - debit) * CONTRACT_MULTIPLIER
        totals[b] = total
    lo, hi = np.percentile(totals, [2.5, 97.5])
    return dict(mean=float(totals.mean()), lo=float(lo), hi=float(hi), n=n_seeds)


# --------------------------------------------------------------------------- #
# One (delta, dte, management, fill) cell: build, mark, regress, bootstrap
# --------------------------------------------------------------------------- #
def analyze_cell(delta: float, dte: int, management: str, f: float, all_days, day_cache,
                 price_maps, spx_ret: pd.Series, do_bootstrap: bool) -> dict:
    strangles = run_strangle_book(dte, delta, management, f, all_days, day_cache,
                                  price_maps=price_maps)
    book = strangle_book_daily_marks(strangles, lambda d: day_cache.get(d), all_days, f,
                                     price_maps=price_maps)
    total_pnl = sum(s.pnl_dollars for s in strangles if np.isfinite(s.pnl_dollars))
    n_trades = len(strangles)
    win = float(np.mean([s.pnl_dollars > 0 for s in strangles])) if strangles else float("nan")
    net_delta = float(np.nanmean([s.net_entry_delta for s in strangles])) if strangles \
        else float("nan")

    r_str = book["ret"].reindex(spx_ret.index).dropna()
    common = r_str.index.intersection(spx_ret.index)
    r_str = r_str.loc[common]
    r_spx = spx_ret.loc[common]
    open_mask = book["n_open"].reindex(common).fillna(0) > 0
    r_str = r_str[open_mask]
    r_spx = r_spx[open_mask]

    if len(r_str) > 10:
        reg = cab.ols_alpha_beta(r_str.to_numpy(), r_spx.to_numpy())
    else:
        reg = dict(alpha_daily=float("nan"), beta=float("nan"), r2=float("nan"),
                   t_alpha=float("nan"), se_alpha_daily=float("nan"), n=len(r_str))
    alpha_ann = reg["alpha_daily"] * TRADING_DAYS
    if do_bootstrap and len(r_str) > 10:
        ci = cab.bootstrap_alpha_ci(r_str.to_numpy(), r_spx.to_numpy())
    else:
        ci = dict(alpha_ann_lo=float("nan"), alpha_ann_hi=float("nan"),
                  alpha_ann_boot_mean=float("nan"))

    dm = cab.daily_metrics(r_str)
    avg_reserve = float(book.loc[book["n_open"] > 0, "reserved_capital"].mean())
    avg_net_ddelta = float(book.loc[book["n_open"] > 0, "net_dollar_delta"].mean())

    return dict(
        delta=delta, dte=dte, management=management, f=f, n_trades=n_trades,
        total_pnl=float(total_pnl), win_rate=win, avg_net_entry_delta=net_delta,
        alpha_daily=reg["alpha_daily"], alpha_ann=alpha_ann, beta=reg["beta"],
        r2=reg["r2"], t_alpha=reg["t_alpha"], n_days=reg["n"],
        alpha_ann_lo=ci["alpha_ann_lo"], alpha_ann_hi=ci["alpha_ann_hi"],
        sharpe=dm["sharpe"], sortino=dm["sortino"], maxdd=dm["max_dd"],
        ann_ret=dm["ann_ret"], ann_vol=dm["ann_vol"], total_ret=dm["total_ret"],
        avg_reserve=avg_reserve, avg_net_ddelta=avg_net_ddelta,
        _r_str=r_str, _r_spx=r_spx, _book=book, _strangles=strangles,
    )


def oos_alpha(r_str: pd.Series, r_spx: pd.Series, split: _dt.date) -> dict:
    split_ts = pd.Timestamp(split)
    out = {}
    for name, mask in (("train", r_str.index < split_ts), ("test", r_str.index >= split_ts)):
        y = r_str[mask].to_numpy()
        x = r_spx[mask].to_numpy()
        if len(y) > 10:
            reg = cab.ols_alpha_beta(y, x)
            out[f"{name}_alpha_ann"] = reg["alpha_daily"] * TRADING_DAYS
            out[f"{name}_beta"] = reg["beta"]
            out[f"{name}_n"] = reg["n"]
        else:
            out[f"{name}_alpha_ann"] = float("nan")
            out[f"{name}_beta"] = float("nan")
            out[f"{name}_n"] = len(y)
    return out


def crisis_totrets(r_str: pd.Series) -> dict:
    windows = {
        "2018Q4": (_dt.date(2018, 10, 1), _dt.date(2018, 12, 31)),
        "COVID": (_dt.date(2020, 2, 1), _dt.date(2020, 4, 30)),
        "2022": (_dt.date(2022, 1, 1), _dt.date(2022, 12, 31)),
    }
    out = {}
    for name, (lo, hi) in windows.items():
        m = (r_str.index >= pd.Timestamp(lo)) & (r_str.index <= pd.Timestamp(hi))
        y = r_str[m]
        out[f"{name}_totret"] = float((1.0 + y).prod() - 1.0) if len(y) else float("nan")
        out[f"{name}_n"] = int(len(y))
    return out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    t0 = time.time()
    print("[strangle] loading day universe...", flush=True)
    all_days = [d for d in s7.available_days() if WINDOW_START <= d <= WINDOW_END]
    print(f"[strangle] {len(all_days)} trading days {all_days[0]}..{all_days[-1]}", flush=True)

    entries = s7.weekly_entry_days(all_days)
    quoted = [d for d in entries if s7.day_quote_ok(d)]
    blackout_weeks = len(entries) - len(quoted)
    print(f"[strangle] weekly entries={len(entries)} quoted={len(quoted)} "
          f"blackout-skipped={blackout_weeks}", flush=True)

    day_cache: dict = {}
    price_maps: dict = {}

    def loader(d):
        if d not in day_cache:
            day_cache[d] = s7.load_day(d)
        return day_cache[d]

    print("[strangle] building SPX daily return series (warehouse underlying_price)...",
          flush=True)
    spx_ret = s7.spx_daily_returns(all_days, day_cache, loader)
    print(f"[strangle] SPX daily returns: {len(spx_ret)} days "
          f"{spx_ret.index.min().date()}..{spx_ret.index.max().date()}", flush=True)

    # ---- run every delta x dte x management x fill cell ----
    cells = {}
    for delta in DELTAS:
        for dte in DTES:
            for mgmt in MANAGEMENTS:
                for f in FILLS:
                    do_boot = (f in (0.0, 0.25, 0.50))
                    res = analyze_cell(delta, dte, mgmt, f, all_days, day_cache, price_maps,
                                       spx_ret, do_boot)
                    cells[(delta, dte, mgmt, f)] = res
                    print(f"  [d{delta} dte{dte} {mgmt} f{f}] pnl=${res['total_pnl']:,.0f} "
                          f"alpha_ann={res['alpha_ann']:.4f} beta={res['beta']:.3f} "
                          f"R2={res['r2']:.3f} sharpe={res['sharpe']:.2f} "
                          f"netD={res['avg_net_entry_delta']:.3f}", flush=True)
    print(f"[strangle] cells done {time.time()-t0:.0f}s", flush=True)

    # ---- headline deep-dive ----
    hl = cells[(HEADLINE_DELTA, HEADLINE_DTE, HEADLINE_MGMT, HEADLINE_F)]
    r_str_hl, r_spx_hl = hl["_r_str"], hl["_r_spx"]
    hl_reg = cab.ols_alpha_beta(r_str_hl.to_numpy(), r_spx_hl.to_numpy())
    hl_ci = cab.bootstrap_alpha_ci(r_str_hl.to_numpy(), r_spx_hl.to_numpy())
    hl_metrics = cab.daily_metrics(r_str_hl)
    oos = oos_alpha(r_str_hl, r_spx_hl, OOS_SPLIT)
    crisis = crisis_totrets(r_str_hl)

    # ---- management vs hold vs placebo (headline delta/dte/fill) ----
    managed_strangles = hl["_strangles"]
    managed_total = hl["total_pnl"]
    managed_hold_days = np.array(
        [max((s.exit_day - s.entry_day).days, 0) for s in managed_strangles
         if s.exit_day is not None], dtype=float)
    # convert calendar holding gap to a rough trading-day count for the placebo draw
    managed_hold_tdays = np.maximum(np.round(managed_hold_days * 5.0 / 7.0), 1).astype(int)

    hold_cell = cells[(HEADLINE_DELTA, HEADLINE_DTE, "hold", HEADLINE_F)]
    hold_strangles = hold_cell["_strangles"]
    hold_total = hold_cell["total_pnl"]
    placebo = random_exit_placebo_total_pnl(
        hold_strangles, managed_hold_tdays, lambda d: day_cache.get(d), all_days,
        HEADLINE_F, price_maps)

    # ---- CSVs ----
    hl["_book"].to_csv(CSV_DIR / "strangle_headline_daily.csv")
    grid_rows = []
    for c in cells.values():
        grid_rows.append({k: v for k, v in c.items() if not k.startswith("_")})
    pd.DataFrame(grid_rows).to_csv(CSV_DIR / "strangle_grid.csv", index=False)
    pd.DataFrame([{k: v for k, v in asdict(s).items()} for s in managed_strangles]).to_csv(
        CSV_DIR / "strangle_headline_trades.csv", index=False)

    write_report(cells, hl, hl_reg, hl_ci, hl_metrics, oos, crisis, managed_total,
                 hold_total, placebo, all_days, entries, quoted, blackout_weeks, spx_ret,
                 time.time() - t0)
    print(f"[strangle] DONE {time.time()-t0:.0f}s -> {REPORT}", flush=True)


def _fmt(x, nd=2):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "n/a"
    return f"{x:,.{nd}f}"


def write_report(cells, hl, hl_reg, hl_ci, hl_m, oos, crisis, managed_total, hold_total,
                 placebo, all_days, entries, quoted, blackout_weeks, spx_ret, runtime_s):
    alpha_ann = hl_reg["alpha_daily"] * TRADING_DAYS
    beta = hl_reg["beta"]
    r2 = hl_reg["r2"]
    ci_lo, ci_hi = hl_ci["alpha_ann_lo"], hl_ci["alpha_ann_hi"]

    band = [cells[(HEADLINE_DELTA, HEADLINE_DTE, HEADLINE_MGMT, f)] for f in (0.0, 0.25, 0.50)]

    # ---- 6 pre-registered criteria ----
    c1_band_pos = all(c["alpha_ann"] > 0 for c in band)
    c1_ci_excl0 = (ci_lo > 0) or (ci_hi < 0)
    c1 = bool(c1_band_pos and alpha_ann > 0 and c1_ci_excl0 and ci_lo > 0)

    # C2: genuinely delta-neutral (|beta| small)
    c2 = bool(np.isfinite(beta) and abs(beta) < 0.15)

    # C3: beats cash risk-adjusted -> positive daily Sharpe & Sortino
    c3 = bool(np.isfinite(hl_m["sharpe"]) and np.isfinite(hl_m["sortino"])
              and hl_m["sharpe"] > 0 and hl_m["sortino"] > 0)

    # C4: OOS positive alpha both halves
    c4 = bool(np.isfinite(oos["train_alpha_ann"]) and np.isfinite(oos["test_alpha_ann"])
              and oos["train_alpha_ann"] > 0 and oos["test_alpha_ann"] > 0)

    # C5: plateau across delta{16,20} x dte{30,45} x fill band (managed)
    plateau_cells = [cells[(dl, dt_, HEADLINE_MGMT, f)]
                     for dl in DELTAS for dt_ in DTES for f in (0.0, 0.25, 0.50)]
    plateau_share = float(np.mean([c["alpha_ann"] > 0 for c in plateau_cells]))
    c5 = plateau_share >= 0.5 and c1_band_pos

    # C6: crisis survivability -> full-cycle alpha>0 AND (managed headline) mgmt beats hold+placebo
    mgmt_beats_hold = managed_total > hold_total
    mgmt_beats_placebo = managed_total > placebo["hi"] if np.isfinite(placebo["hi"]) \
        else False
    c6 = bool(alpha_ann > 0 and mgmt_beats_hold and mgmt_beats_placebo)

    all_pass = c1 and c2 and c3 and c4 and c5 and c6
    if all_pass:
        verdict = ("CLEAN VRP ALPHA — a delta-neutral SPX short strangle produces risk-adjusted "
                   "return not explained by equity beta")
    elif alpha_ann <= 0 or ci_lo <= 0:
        verdict = ("REFUTED — the VRP premium is EATEN: alpha <= 0 / CI spans 0 net of honest "
                   "fills and uncapped crash losses. Mechanical SPX premium-selling shows no "
                   "clean VRP alpha (condor + CSP + strangle all refuted).")
    else:
        verdict = ("REFUTED — positive intercept but fails a robustness criterion "
                   "(delta-neutrality / cash / OOS / plateau / crisis-mgmt).")

    def yn(b):
        return "PASS" if b else "FAIL"

    L = []
    L.append("# SHORT STRANGLE — VRP ALPHA (delta-neutral) — RESULTS + VERDICT\n")
    L.append(f"**Run:** 2026-07-06  |  **Runtime:** {runtime_s:.0f}s  |  pre-registered in "
             f"`docs/PREREG_short_strangle_alpha_2026-07-06.md` (committed BEFORE this run).\n")
    L.append("## VERDICT (lead)\n")
    L.append(f"### **{verdict}**\n")
    L.append(f"Headline: SPX short strangle, {int(HEADLINE_DELTA*100)}-delta, {HEADLINE_DTE} "
             f"DTE, {HEADLINE_MGMT} (50%-target or 21-DTE), weekly ladder, f={HEADLINE_F}.\n")
    L.append(f"- **Annualized alpha intercept: {alpha_ann:+.2%}** "
             f"(bootstrap 95% CI [{ci_lo:+.2%}, {ci_hi:+.2%}], "
             f"{'EXCLUDES' if (ci_lo>0 or ci_hi<0) else 'SPANS'} 0), "
             f"**beta {beta:.3f}**, R² {r2:.3f}, alpha t-stat {hl_reg['t_alpha']:.2f}.")
    L.append(f"- Delta-neutrality check: book behaves like **{beta:.3f}×** SPX daily return "
             f"(|beta|<0.15 => {'CONFIRMED neutral' if abs(beta)<0.15 else 'NOT neutral'}); "
             f"avg net entry delta {hl['avg_net_entry_delta']:+.3f}.")
    L.append(f"- Daily-return **Sharpe {_fmt(hl_m['sharpe'])}, Sortino {_fmt(hl_m['sortino'])}**, "
             f"maxDD {_fmt(hl_m['max_dd'],3)}, ann.ret {hl_m['ann_ret']:+.2%}, "
             f"ann.vol {hl_m['ann_vol']:.2%} (vs cash rf~{RISK_FREE_ANNUAL:.0%}).")
    L.append(f"- Total book P&L (managed): **${managed_total:,.0f}**; hold-to-expiry "
             f"${hold_total:,.0f}; random-exit placebo mean ${placebo['mean']:,.0f} "
             f"(95% [{placebo['lo']:,.0f}, {placebo['hi']:,.0f}]).\n")

    L.append("### Six pre-registered pass criteria\n")
    L.append(f"1. **Positive alpha, CI excl. 0, across mid->0.50 band:** {yn(c1)} — "
             f"band alphas {[round(c['alpha_ann'],4) for c in band]}, "
             f"headline CI [{ci_lo:+.2%},{ci_hi:+.2%}].")
    L.append(f"2. **Genuinely delta-neutral (|beta|<0.15):** {yn(c2)} — beta {beta:.3f}.")
    L.append(f"3. **Beats cash risk-adjusted (Sharpe & Sortino > 0):** {yn(c3)} — "
             f"Sharpe {_fmt(hl_m['sharpe'])}, Sortino {_fmt(hl_m['sortino'])}.")
    L.append(f"4. **OOS positive alpha in BOTH halves:** {yn(c4)} — "
             f"train {oos['train_alpha_ann']:+.2%} (n={oos['train_n']}), "
             f"test {oos['test_alpha_ann']:+.2%} (n={oos['test_n']}).")
    L.append(f"5. **Plateau across delta x dte x fill:** {yn(c5)} — "
             f"{plateau_share:.0%} of managed delta×dte×fill cells have positive alpha.")
    L.append(f"6. **Crisis survivability + mgmt beats hold+placebo:** {yn(c6)} — "
             f"full-cycle alpha {alpha_ann:+.2%}; managed ${managed_total:,.0f} "
             f"{'>' if mgmt_beats_hold else '<='} hold ${hold_total:,.0f}; "
             f"{'>' if mgmt_beats_placebo else '<='} placebo 97.5% ${placebo['hi']:,.0f}.\n")

    L.append("## Beta regression grid (alpha annualized) — delta × DTE × management × fill\n")
    L.append("| delta | dte | mgmt | f | n_days | total P&L $ | net entry Δ | alpha_ann | "
             "95% CI | beta | R² | t(alpha) | Sharpe | Sortino | maxDD | win% |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for delta in DELTAS:
        for dte in DTES:
            for mgmt in MANAGEMENTS:
                for f in FILLS:
                    c = cells[(delta, dte, mgmt, f)]
                    ci = (f"[{c['alpha_ann_lo']:+.1%},{c['alpha_ann_hi']:+.1%}]"
                          if np.isfinite(c["alpha_ann_lo"]) else "n/a")
                    L.append(f"| {delta} | {dte} | {mgmt} | {f} | {c['n_days']} "
                             f"| {_fmt(c['total_pnl'],0)} | {_fmt(c['avg_net_entry_delta'],3)} "
                             f"| {c['alpha_ann']:+.2%} | {ci} | {_fmt(c['beta'],3)} "
                             f"| {_fmt(c['r2'],3)} | {_fmt(c['t_alpha'],2)} "
                             f"| {_fmt(c['sharpe'])} | {_fmt(c['sortino'])} "
                             f"| {_fmt(c['maxdd'],3)} | {_fmt(c['win_rate']*100,0)} |")
    L.append("")

    L.append("## Cash / risk-free benchmark (headline book, daily returns on reserved capital)\n")
    L.append("| arm | Sharpe | Sortino | maxDD | ann. return | ann. vol | total return |")
    L.append("|---|---|---|---|---|---|---|")
    L.append(f"| Strangle {int(HEADLINE_DELTA*100)}d {HEADLINE_DTE}DTE {HEADLINE_MGMT} "
             f"f{HEADLINE_F} | {_fmt(hl_m['sharpe'])} | {_fmt(hl_m['sortino'])} "
             f"| {_fmt(hl_m['max_dd'],3)} | {hl_m['ann_ret']:+.2%} | {hl_m['ann_vol']:.2%} "
             f"| {hl_m['total_ret']:+.2%} |")
    L.append(f"| Cash / T-bill (~{RISK_FREE_ANNUAL:.0%} rf) | n/a | n/a | 0.000 "
             f"| {RISK_FREE_ANNUAL:+.2%} | 0.00% | — |\n")
    L.append("_Because the strangle is ~delta-neutral, CASH (not a delta-matched SPX arm) is the "
             "relevant benchmark: does it beat the risk-free rate on a risk-adjusted basis at all, "
             "net of honest two-leg fills and uncapped crash losses? Sharpe/Sortino use rf=0._\n")

    L.append("## Management vs hold vs random-exit placebo (headline delta/dte/fill, TOTAL P&L)\n")
    L.append("| arm | total book P&L $ |")
    L.append("|---|---|")
    L.append(f"| Managed (50%-target or 21-DTE) | {_fmt(managed_total,0)} |")
    L.append(f"| Hold-to-expiry (control) | {_fmt(hold_total,0)} |")
    L.append(f"| Random-exit placebo (mean of {placebo['n']} seeds) | {_fmt(placebo['mean'],0)} "
             f"[95% {_fmt(placebo['lo'],0)}, {_fmt(placebo['hi'],0)}] |\n")
    L.append("_Placebo draws a random holding period matching the managed arm's realized "
             "holding-period distribution and exits the HOLD-arm trades there. Management earns "
             "credit only if it beats BOTH hold and the placebo's 97.5% percentile — i.e. the "
             "timing is skill, not luck._\n")

    L.append("## OOS split (headline) — alpha must be positive in BOTH halves\n")
    L.append("| half | window | n_days | alpha_ann | beta |")
    L.append("|---|---|---|---|---|")
    L.append(f"| train | 2018-06→2021-12 | {oos['train_n']} | {oos['train_alpha_ann']:+.2%} "
             f"| {_fmt(oos['train_beta'],3)} |")
    L.append(f"| test | 2022-01→2026-07 | {oos['test_n']} | {oos['test_alpha_ann']:+.2%} "
             f"| {_fmt(oos['test_beta'],3)} |\n")

    L.append("## Per-crisis (headline strangle daily-return compounded total over each window)\n")
    L.append("| window | n_days | strangle total return |")
    L.append("|---|---|---|")
    for name in ("2018Q4", "COVID", "2022"):
        L.append(f"| {name} | {crisis[f'{name}_n']} | {crisis[f'{name}_totret']:+.2%} |")
    L.append("_A naked short-vol book is SUPPOSED to bleed here; the full-cycle alpha is the "
             "question, not any single crisis._\n")

    L.append("## Data window & coverage\n")
    L.append(f"- Trading days in window: {len(all_days)} ({all_days[0]}..{all_days[-1]}).")
    L.append(f"- SPX daily-return series: {len(spx_ret)} days "
             f"({spx_ret.index.min().date()}..{spx_ret.index.max().date()}), "
             f"source = warehouse `underlying_price` (continuous across the NBBO blackout).")
    L.append(f"- Weekly ladder entries: {len(entries)}; genuinely quoted: {len(quoted)}; "
             f"blackout-skipped weeks: {blackout_weeks} (2020-08-13→2021-12-31 NBBO blackout).")
    L.append(f"- Strangle = wingless condor; UNCAPPED intrinsic settlement max(0,K_put−S)+"
             f"max(0,S−K_call). Reuses S7 honest-fill helpers (_sell_price/_buy_price), "
             f"clean-delta selection, price-map cache, forward-walk; strictly causal.\n")

    L.append("## Method notes\n")
    L.append("- Daily book mark-to-market: equity(d) = Σ premium collected − Σ current two-leg "
             "buy-back liability at fill f; expiry marks use UNCAPPED settled intrinsic. Daily "
             "return = Δequity ÷ reserved capital (Σ put_strike·100 over open strangles that day).")
    L.append("- Regression r_str = alpha + beta·r_spx + e on aligned daily returns; alpha "
             f"annualized ×252. 95% CI via stationary block bootstrap (block≈{cab.BOOT_BLOCK}d, "
             f"{cab.BOOT_RESAMPLES} resamples, seed {cab.BOOT_SEED}). Placebo seed {PLACEBO_SEED0}.")
    L.append("- No parameter tuned to the data. Warehouse read-only. Frozen config untouched.\n")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
