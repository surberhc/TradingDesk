"""
S3 v1 — Fixed-Delta Iron Condor CONTROL engine (anti-curve-fit benchmark).

This is the deliberately SIMPLE, FIXED, NON-OPTIMIZED benchmark that every
adaptive condor version (v2 gamma-bands -> v3 +rvol -> v4 +ivol) must beat
out-of-sample. Per datacollector/STRATEGIES.md (S3 "v1 fixed-delta control").

Mechanics (all FIXED on purpose -- no tuning, no regime, no sweep):
  * Instrument: cash-settled European index options (default SPXW; SPX/XSP via --symbol).
  * Each cycle: SELL a symmetric iron condor at FIXED short-leg delta (default 0.16),
    with a FIXED wing width (default 50 index points), entered at EOD.
  * Tenor: FIXED target DTE (default ~14), held to EXPIRY (cash settlement). Cycles
    are NON-OVERLAPPING -- a new condor is opened only after the prior one expires.
  * Fills: cross the spread (sell shorts at BID, buy wings at ASK) + per-contract
    commission. Conservative; no optimistic mid-fills.
  * Cash/margin reserve + FIXED sizing: defined-risk condor. Per-contract reserve =
    max_loss * multiplier, where max_loss = wing_width - net_credit (worst side).
    Contracts/cycle are sized by the BINDING of two FIXED caps:
      - risk_frac  : cycle max-loss <= risk_frac * equity (default 10%)  <-- usually binds
      - reserve_util: reserved capital <= reserve_util * equity (default 80%)
    This models the spec's "cash-settled short side is only 'covered' with an
    explicit reserve" requirement AND keeps a single max-loss event from ending
    the account. The account is NEVER 100% deployed. (Constant-fraction sizing is
    a fixed RULE, not an optimization -- the control still does no tuning.)
  * Settlement: held to expiry; cash-settled against the expiry-day underlying close
    (read from the expiry-day warehouse file). 4-leg intrinsic payoff.

DATA: EOD option chains in the ThetaData warehouse:
  C:/TradingDesk-Local/warehouse/raw/options/{SYMBOL}/{YYYYMMDD}.parquet
  (full chain incl. delta, bid/ask, underlying_price, expiration, strike, right).

DEFERRED to intraday (1-min) data, NOT modeled here (by design -- v1 control is EOD):
  * 0DTE condors (gamma risk is concentrated intraday; open-to-close understates it).
  * Intraday regime-triggered exits / morning-gap "wait & measure" gate.
  * Intraday-path P&L marking (here we hold to expiry and settle on intrinsic).

Run (offline; no gateway; no network):
  C:/TradingDesk-Local/venv/Scripts/python.exe backtester/s3_condor_control.py
  optional flags:
    --symbol SPXW|SPX|XSP   (default SPXW)
    --dte 14                 target days-to-expiry at entry
    --delta 0.16             short-leg target |delta|
    --wing 50                wing width in index points (use ~5 for XSP)
    --start 20180101 --end 20261231
    --commission 0.65        per-contract per-leg commission ($)
    --capital 100000         starting account equity ($)
    --reserve-util 0.80      fraction of equity allowed in reserve per cycle
    --sanity 3               print N hand-checkable cycle traces, then continue
"""
from __future__ import annotations
import argparse
import glob
import math
import os
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

WAREHOUSE = "C:/TradingDesk-Local/warehouse/raw/options"
TRADING_DAYS_PER_YEAR = 252


# ----------------------------------------------------------------------------
# Data access
# ----------------------------------------------------------------------------
def list_session_dates(symbol: str, start: str, end: str) -> list[str]:
    files = sorted(glob.glob(os.path.join(WAREHOUSE, symbol, "*.parquet")))
    out = []
    for f in files:
        ymd = os.path.splitext(os.path.basename(f))[0]
        if not ymd.isdigit():
            continue
        if start <= ymd <= end:
            out.append(ymd)
    return out


def load_chain(symbol: str, ymd: str) -> pd.DataFrame | None:
    p = os.path.join(WAREHOUSE, symbol, f"{ymd}.parquet")
    if not os.path.exists(p):
        return None
    cols = ["expiration", "strike", "right", "timestamp",
            "bid", "ask", "delta", "underlying_price"]
    try:
        df = pd.read_parquet(p, columns=cols)
    except Exception:
        # Empty / placeholder files (e.g. holidays with 0 rows) lack the schema.
        try:
            df = pd.read_parquet(p)
        except Exception:
            return None
        if not set(cols).issubset(df.columns) or df.empty:
            return None
        df = df[cols]
    if df.empty:
        return None
    return df


def prep_chain(df: pd.DataFrame, ymd: str) -> pd.DataFrame:
    df = df.copy()
    df["exp"] = pd.to_datetime(df["expiration"])
    asof = pd.to_datetime(ymd, format="%Y%m%d")
    df["dte"] = (df["exp"] - asof).dt.days
    # Valid two-sided quote required to trade a leg.
    df = df[(df["bid"] > 0) & (df["ask"] > 0)]
    df["mid"] = (df["bid"] + df["ask"]) / 2.0
    return df


# ----------------------------------------------------------------------------
# Strike selection
# ----------------------------------------------------------------------------
def pick_expiry(chain: pd.DataFrame, target_dte: int) -> int | None:
    """Choose the available DTE closest to target (ties -> longer)."""
    dtes = sorted(d for d in chain["dte"].unique() if d >= 1)
    if not dtes:
        return None
    best = min(dtes, key=lambda d: (abs(d - target_dte), -d))
    return int(best)


def nearest_delta(legs: pd.DataFrame, target_abs_delta: float) -> pd.Series | None:
    if legs.empty:
        return None
    legs = legs.copy()
    legs["dd"] = (legs["delta"].abs() - target_abs_delta).abs()
    return legs.sort_values(["dd", "strike"]).iloc[0]


def find_wing(legs: pd.DataFrame, short_strike: float, wing: float, side: str) -> pd.Series | None:
    """Find the long wing strike. PUT side -> below short; CALL side -> above."""
    target = short_strike - wing if side == "PUT" else short_strike + wing
    cand = legs[np.isclose(legs["strike"], target)]
    if not cand.empty:
        return cand.iloc[0]
    # Fall back to the nearest available strike beyond the short by >= wing.
    if side == "PUT":
        beyond = legs[legs["strike"] <= short_strike - wing]
        if beyond.empty:
            return None
        return beyond.sort_values("strike", ascending=False).iloc[0]
    else:
        beyond = legs[legs["strike"] >= short_strike + wing]
        if beyond.empty:
            return None
        return beyond.sort_values("strike", ascending=True).iloc[0]


# ----------------------------------------------------------------------------
# Trade objects
# ----------------------------------------------------------------------------
@dataclass
class Cycle:
    entry_date: str
    expiry_date: str
    dte: int
    underlying_entry: float
    short_put_k: float
    long_put_k: float
    short_call_k: float
    long_call_k: float
    short_put_delta: float
    short_call_delta: float
    net_credit: float          # per 1 contract, in index points (after fills)
    max_loss: float            # per 1 contract, index points
    contracts: int
    multiplier: float
    commission_pts: float      # total commission converted to index points/contract
    # filled at settlement:
    underlying_settle: float = math.nan
    payoff: float = math.nan   # per 1 contract, index points (intrinsic paid out, <=0 cost)
    pnl_pts: float = math.nan  # per 1 contract net (credit - settlement payout)
    pnl_cash: float = math.nan # total $ for the cycle


def build_condor(
    chain: pd.DataFrame, ymd: str, target_dte: int, target_delta: float,
    wing: float, commission: float, multiplier: float,
) -> tuple[Cycle | None, str]:
    dte = pick_expiry(chain, target_dte)
    if dte is None:
        return None, "no_expiry"
    leg = chain[chain["dte"] == dte]
    puts = leg[leg["right"] == "PUT"]
    calls = leg[leg["right"] == "CALL"]
    sp = nearest_delta(puts, target_delta)
    sc = nearest_delta(calls, target_delta)
    if sp is None or sc is None:
        return None, "no_short_leg"
    lp = find_wing(puts, float(sp["strike"]), wing, "PUT")
    lc = find_wing(calls, float(sc["strike"]), wing, "CALL")
    if lp is None or lc is None:
        return None, "no_wing"
    if not (lp["strike"] < sp["strike"] < sc["strike"] < lc["strike"]):
        return None, "degenerate_strikes"

    # Fills: SELL shorts at BID, BUY wings at ASK (cross the spread).
    credit = (float(sp["bid"]) + float(sc["bid"])) - (float(lp["ask"]) + float(lc["ask"]))
    # Commission: 4 legs in + 4 legs out (settlement of wings; shorts expire) ->
    # be conservative: charge entry on 4 legs and exit on 4 legs.
    comm_pts = (commission * 8.0) / multiplier
    net_credit = credit - comm_pts
    if net_credit <= 0:
        return None, "no_credit"

    put_width = float(sp["strike"]) - float(lp["strike"])
    call_width = float(lc["strike"]) - float(sc["strike"])
    max_loss = max(put_width, call_width) - net_credit
    if max_loss <= 0:
        return None, "no_risk"  # shouldn't happen with realistic credits

    exp_ymd = pd.to_datetime(sp["exp"]).strftime("%Y%m%d")
    cyc = Cycle(
        entry_date=ymd, expiry_date=exp_ymd, dte=dte,
        underlying_entry=float(chain["underlying_price"].iloc[0]),
        short_put_k=float(sp["strike"]), long_put_k=float(lp["strike"]),
        short_call_k=float(sc["strike"]), long_call_k=float(lc["strike"]),
        short_put_delta=float(sp["delta"]), short_call_delta=float(sc["delta"]),
        net_credit=net_credit, max_loss=max_loss,
        contracts=0, multiplier=multiplier, commission_pts=comm_pts,
    )
    return cyc, "ok"


def settle(cyc: Cycle, settle_under: float) -> None:
    """Cash settlement on expiry: 4-leg intrinsic payoff (index points / 1 contract)."""
    S = settle_under
    # Long put intrinsic (we own) minus short put intrinsic (we owe), etc.
    put_spread = max(cyc.short_put_k - S, 0.0) - max(cyc.long_put_k - S, 0.0)
    call_spread = max(S - cyc.short_call_k, 0.0) - max(S - cyc.long_call_k, 0.0)
    # We are short both spreads -> we PAY (put_spread + call_spread), capped at width.
    payout = put_spread + call_spread          # >= 0, this is what we owe
    cyc.underlying_settle = S
    cyc.payoff = -payout
    cyc.pnl_pts = cyc.net_credit - payout
    cyc.pnl_cash = cyc.pnl_pts * cyc.multiplier * cyc.contracts


# ----------------------------------------------------------------------------
# Backtest loop
# ----------------------------------------------------------------------------
def run(args) -> dict:
    multiplier = {"SPX": 100.0, "SPXW": 100.0, "XSP": 100.0}.get(args.symbol, 100.0)
    dates = list_session_dates(args.symbol, args.start, args.end)
    if not dates:
        print(f"No warehouse files for {args.symbol} in {args.start}..{args.end}", file=sys.stderr)
        sys.exit(1)
    date_set = set(dates)

    equity = float(args.capital)
    equity_curve = []      # (date, equity)
    cycles: list[Cycle] = []
    skips: dict[str, int] = {}
    sanity_left = args.sanity

    i = 0
    n = len(dates)
    while i < n:
        ymd = dates[i]
        raw = load_chain(args.symbol, ymd)
        if raw is None:
            i += 1
            continue
        chain = prep_chain(raw, ymd)
        if chain.empty:
            i += 1
            continue

        cyc, status = build_condor(
            chain, ymd, args.dte, args.delta, args.wing, args.commission, multiplier
        )
        if cyc is None:
            skips[status] = skips.get(status, 0) + 1
            i += 1
            continue

        # FIXED sizing rule (control = constant fractional risk, not all-in):
        #   * risk cap : max-loss exposure of the cycle <= risk_frac * equity
        #   * reserve  : reserved capital (= max_loss notional) <= reserve_util * equity
        # The binding (smaller) of the two governs. This keeps a single max-loss
        # event from ending the account, while still never running 100% deployed.
        reserve_per = cyc.max_loss * multiplier            # $ reserved per contract
        risk_per = cyc.max_loss * multiplier               # $ at risk per contract (= reserve here)
        by_risk = int((equity * args.risk_frac) // risk_per)
        by_reserve = int((equity * args.reserve_util) // reserve_per)
        contracts = max(0, min(by_risk, by_reserve))
        if contracts < 1:
            skips["under_capitalized"] = skips.get("under_capitalized", 0) + 1
            i += 1
            continue
        cyc.contracts = contracts

        # Settle on expiry day (need the expiry-day file for the settlement underlying).
        if cyc.expiry_date not in date_set:
            skips["no_settle_file"] = skips.get("no_settle_file", 0) + 1
            i += 1
            continue
        exp_raw = load_chain(args.symbol, cyc.expiry_date)
        if exp_raw is None or exp_raw.empty:
            skips["no_settle_file"] = skips.get("no_settle_file", 0) + 1
            i += 1
            continue
        settle_under = float(exp_raw["underlying_price"].iloc[0])
        settle(cyc, settle_under)

        equity += cyc.pnl_cash
        cycles.append(cyc)
        equity_curve.append((cyc.expiry_date, equity))

        if sanity_left > 0:
            _print_sanity(cyc)
            sanity_left -= 1

        # Non-overlapping: jump to the first session strictly AFTER expiry.
        j = i + 1
        while j < n and dates[j] <= cyc.expiry_date:
            j += 1
        i = j

    return _summarize(cycles, equity_curve, float(args.capital), skips, args)


def _print_sanity(c: Cycle) -> None:
    print("\n--- SANITY CHECK CYCLE ---")
    print(f"  entry {c.entry_date} -> expiry {c.expiry_date}  (DTE={c.dte})")
    print(f"  underlying @entry = {c.underlying_entry:.2f}")
    print(f"  SHORT PUT  K={c.short_put_k:.0f} (delta {c.short_put_delta:+.3f})  "
          f"LONG PUT  K={c.long_put_k:.0f}")
    print(f"  SHORT CALL K={c.short_call_k:.0f} (delta {c.short_call_delta:+.3f})  "
          f"LONG CALL K={c.long_call_k:.0f}")
    print(f"  net credit = {c.net_credit:.2f} pts  (after {c.commission_pts:.3f} pts comm)")
    print(f"  max loss   = {c.max_loss:.2f} pts/contract  "
          f"reserve/contract = ${c.max_loss * c.multiplier:,.0f}")
    print(f"  contracts  = {c.contracts}")
    print(f"  underlying @settle = {c.underlying_settle:.2f}")
    inside = c.short_put_k <= c.underlying_settle <= c.short_call_k
    print(f"  settle {'INSIDE shorts (keep full credit)' if inside else 'BREACHED a short leg'}")
    print(f"  payout owed = {-c.payoff:.2f} pts   net P&L = {c.pnl_pts:+.2f} pts  "
          f"= ${c.pnl_cash:+,.0f}")


# ----------------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------------
def _summarize(cycles, equity_curve, capital, skips, args) -> dict:
    if not cycles:
        return {"error": "no cycles traded", "skips": skips}

    eq = pd.DataFrame(equity_curve, columns=["date", "equity"])
    eq["date"] = pd.to_datetime(eq["date"], format="%Y%m%d")
    eq = eq.sort_values("date").reset_index(drop=True)

    pnls = np.array([c.pnl_cash for c in cycles])
    rets = pnls / capital  # per-cycle return on starting capital (simple, comparable)

    final = eq["equity"].iloc[-1]
    years = (eq["date"].iloc[-1] - eq["date"].iloc[0]).days / 365.25
    cagr = (final / capital) ** (1 / years) - 1 if years > 0 and final > 0 else float("nan")

    # Drawdown on equity curve.
    roll_max = eq["equity"].cummax()
    dd = eq["equity"] / roll_max - 1.0
    max_dd = dd.min()

    # Per-cycle return stats. Annualize by cycles/year.
    cyc_per_yr = len(cycles) / years if years > 0 else float("nan")
    mean_r = rets.mean()
    std_r = rets.std(ddof=1) if len(rets) > 1 else float("nan")
    downside = rets[rets < 0]
    dstd = downside.std(ddof=1) if len(downside) > 1 else float("nan")
    sharpe = (mean_r / std_r) * math.sqrt(cyc_per_yr) if std_r and std_r > 0 else float("nan")
    sortino = (mean_r / dstd) * math.sqrt(cyc_per_yr) if dstd and dstd > 0 else float("nan")

    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    win_rate = len(wins) / len(pnls)
    avg_win = wins.mean() if len(wins) else 0.0
    avg_loss = losses.mean() if len(losses) else 0.0
    worst = pnls.min()
    best = pnls.max()

    # Turnover: total notional reserve cycled / avg equity / years.
    total_reserve = sum(c.max_loss * c.multiplier * c.contracts for c in cycles)
    avg_eq = eq["equity"].mean()
    turnover = (total_reserve / avg_eq / years) if (avg_eq and years > 0) else float("nan")

    res = dict(
        symbol=args.symbol, dte=args.dte, delta=args.delta, wing=args.wing,
        start=eq["date"].iloc[0].strftime("%Y-%m-%d"),
        end=eq["date"].iloc[-1].strftime("%Y-%m-%d"),
        years=round(years, 2),
        cycles=len(cycles), cycles_per_year=round(cyc_per_yr, 1),
        capital=capital, final_equity=round(final, 2),
        total_return=round(final / capital - 1, 4), cagr=round(cagr, 4),
        max_drawdown=round(max_dd, 4),
        sharpe=round(sharpe, 3), sortino=round(sortino, 3),
        win_rate=round(win_rate, 4),
        avg_win=round(avg_win, 2), avg_loss=round(avg_loss, 2),
        worst_loss=round(worst, 2), best_win=round(best, 2),
        avg_pnl_per_cycle=round(pnls.mean(), 2),
        turnover_x_per_year=round(turnover, 2),
        skips=skips,
    )
    return res, eq, cycles


def _print_report(res) -> None:
    print("\n" + "=" * 64)
    print("  S3 v1 FIXED-DELTA IRON CONDOR  --  CONTROL BENCHMARK")
    print("=" * 64)
    rows = [
        ("Instrument", res["symbol"]),
        ("Tenor (target DTE)", f"{res['dte']} (held to expiry)"),
        ("Short-leg delta / wing", f"{res['delta']} / {res['wing']} pts"),
        ("Period", f"{res['start']} -> {res['end']}  ({res['years']} yrs)"),
        ("Cycles (per year)", f"{res['cycles']} ({res['cycles_per_year']}/yr)"),
        ("Starting capital", f"${res['capital']:,.0f}"),
        ("Final equity", f"${res['final_equity']:,.0f}"),
        ("Total return", f"{res['total_return']*100:.1f}%"),
        ("CAGR", f"{res['cagr']*100:.2f}%"),
        ("Max drawdown", f"{res['max_drawdown']*100:.2f}%"),
        ("Sharpe (annualized)", f"{res['sharpe']}"),
        ("Sortino (annualized)", f"{res['sortino']}"),
        ("Win rate", f"{res['win_rate']*100:.1f}%"),
        ("Avg win / Avg loss", f"${res['avg_win']:,.0f} / ${res['avg_loss']:,.0f}"),
        ("Worst / Best cycle", f"${res['worst_loss']:,.0f} / ${res['best_win']:,.0f}"),
        ("Avg P&L per cycle", f"${res['avg_pnl_per_cycle']:,.0f}"),
        ("Turnover (reserve x/yr)", f"{res['turnover_x_per_year']}x"),
    ]
    for k, v in rows:
        print(f"  {k:<26} {v}")
    if res["skips"]:
        print(f"  {'Skipped sessions':<26} {res['skips']}")
    print("=" * 64)


def main():
    ap = argparse.ArgumentParser(description="S3 v1 fixed-delta iron condor control")
    ap.add_argument("--symbol", default="SPXW", choices=["SPXW", "SPX", "XSP"])
    ap.add_argument("--dte", type=int, default=14)
    ap.add_argument("--delta", type=float, default=0.16)
    ap.add_argument("--wing", type=float, default=50.0)
    ap.add_argument("--start", default="20180101")
    ap.add_argument("--end", default="20261231")
    ap.add_argument("--commission", type=float, default=0.65)
    ap.add_argument("--capital", type=float, default=100000.0)
    ap.add_argument("--reserve-util", type=float, default=0.80, dest="reserve_util",
                    help="hard cap: reserved capital per cycle <= this fraction of equity")
    ap.add_argument("--risk-frac", type=float, default=0.10, dest="risk_frac",
                    help="FIXED per-cycle risk: cycle max-loss <= this fraction of equity")
    ap.add_argument("--sanity", type=int, default=3)
    ap.add_argument("--dump-cycles", default="", help="optional CSV path for per-cycle log")
    args = ap.parse_args()

    out = run(args)
    if isinstance(out, dict):  # error
        print("ERROR:", out)
        sys.exit(1)
    res, eq, cycles = out
    _print_report(res)

    if args.dump_cycles:
        pd.DataFrame([c.__dict__ for c in cycles]).to_csv(args.dump_cycles, index=False)
        print(f"  per-cycle log -> {args.dump_cycles}")


if __name__ == "__main__":
    main()
