"""
ibkr_forward.py — forward EOD option-chain collector.

The free, perpetual continuation of the warehouse. After the one-time ThetaData
bulk grab ends (subscription cancelled), THIS records each new trading day's EOD
option chains via IB Gateway and writes them into the SAME warehouse, in the SAME
one-file-per-(symbol,day) parquet shape, with the SAME 41-column schema — so the
DuckDB catalog unions ThetaData history + IBKR-forward days with no seam.

Design choices that keep it safe alongside other Gateway clients:
  * Its OWN clientId (`datacollector_forward` = 25). IBKR allows many simultaneous
    API clients; collisions happen ONLY on a shared id. 25 ≠ paperbot's 30, so the
    two never knock each other off.
  * `readonly=True` — physically cannot place/modify/cancel orders. It only reads.
  * Streams in batches under IBKR's ~100 simultaneous market-data-line cap, so it
    won't exhaust the account's data lines out from under another client.

What IBKR can fill vs. ThetaData: the core GEX/MSR inputs (strike, right, expiration,
bid/ask, delta, gamma, vega, theta, implied_vol, underlying_price, open_interest,
volume, close) come straight from IBKR's model greeks + OI ticks. ThetaData's
higher-order greeks (vanna/charm/vomma/…) are not provided by IBKR and are written
as null — recomputable from IV+spot+strike+t downstream, consistent with the
"compute 2nd-order greeks ourselves" decision.

Usage:
    <venv python> ibkr_forward.py --test [ROOT]     # safe small slice (≈40 lines), default SPY
    <venv python> ibkr_forward.py SPY QQQ           # full chains for these roots, today
    <venv python> ibkr_forward.py                   # full universe (config.all_roots()), today
"""

from __future__ import annotations

import sys
from datetime import date, datetime

import pandas as pd
from ib_async import IB, Index, Option, Stock

import config
import storage

# The shared connection layer owns the clientId registry + the Gateway-launch fix.
from connections import ibkr_paper as gw

CLIENT = "datacollector_forward"          # clientId 25 (see connections.clientids)

# Exact warehouse column order (matches the ThetaData parquet so the catalog unions).
SCHEMA_COLS = [
    "date", "symbol", "expiration", "strike", "right", "timestamp",
    "open", "high", "low", "close", "volume", "count", "bid_size", "bid",
    "ask_size", "ask", "delta", "theta", "vega", "rho", "epsilon", "lambda",
    "gamma", "vanna", "charm", "vomma", "veta", "vera", "speed", "zomma",
    "color", "ultima", "d1", "d2", "dual_delta", "dual_gamma", "implied_vol",
    "iv_error", "underlying_timestamp", "underlying_price", "open_interest",
]

LINE_LIMIT = 90           # stay safely under IBKR's ~100 simultaneous data-line cap
SETTLE_SECS = 6           # seconds to let greeks + OI populate before harvesting a batch
QUALIFY_CHUNK = 100       # qualifyContracts chunk size (pacing-friendly)

# IBKR status codes that are informational "data farm OK" / benign, not real errors.
OK_STATUS = {2104, 2106, 2158, 2107, 2119, 2100, 2150, 2168, 2169, 200, 354}

# Index roots and their IBKR listing exchange (verified live 2026-06-26 via
# reqSecDefOptParams: RUT resolves on RUSSELL not CBOE; NDX on NASDAQ).
_INDEX_EXCH = {"SPX": "CBOE", "SPXW": "CBOE", "VIX": "CBOE", "RUT": "RUSSELL",
               "XSP": "CBOE", "NDX": "NASDAQ"}

# Roots whose warehouse name differs from the IBKR contract symbol. We STORE the
# warehouse root (e.g. "BRKB", matching ThetaData) but must REQUEST IBKR's symbol.
_STOCK_SYMBOL_MAP = {"BRKB": "BRK B"}


def _num(x):
    """IBKR returns NaN for missing numerics; normalise NaN/None -> None."""
    if x is None:
        return None
    try:
        return None if x != x else float(x)
    except (TypeError, ValueError):
        return None


def _int(x):
    v = _num(x)
    return None if v is None else int(v)


def _fmt_exp(yyyymmdd: str) -> str:
    """IBKR '20260619' -> warehouse '2026-06-19'."""
    s = str(yyyymmdd)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s


def _underlying(ib: IB, sym: str):
    """Return (underlying_contract, spot, tradingClass_filter) for a root."""
    if sym in ("SPX", "SPXW"):
        c = Index("SPX", "CBOE", "USD")
        tclass = "SPXW" if sym == "SPXW" else "SPX"
    elif sym in _INDEX_EXCH:
        c = Index(sym, _INDEX_EXCH[sym], "USD")
        tclass = None
    else:
        c = Stock(_STOCK_SYMBOL_MAP.get(sym, sym), "SMART", "USD")
        tclass = None
    ib.qualifyContracts(c)
    [t] = ib.reqTickers(c)
    spot = t.marketPrice() or t.close or t.last
    return c, spot, tclass


def _qualify(ib: IB, candidates: list[Option]) -> list[Option]:
    """Qualify in pacing-friendly chunks; keep only contracts IBKR resolved."""
    out: list[Option] = []
    for i in range(0, len(candidates), QUALIFY_CHUNK):
        chunk = candidates[i:i + QUALIFY_CHUNK]
        out.extend(o for o in (ib.qualifyContracts(*chunk) or []) if o and o.conId)
    return out


def build_chain(ib: IB, sym: str, band: int | None = None, max_exps: int | None = None):
    """Construct the option chain for a root.

    band     : if set, keep only ±band strikes around ATM (test mode). None = all strikes.
    max_exps : if set, keep only the nearest N expirations (test mode). None = all.
    Returns (underlying_contract, spot, [qualified Option contracts]).
    """
    c, spot, tclass = _underlying(ib, sym)
    params = ib.reqSecDefOptParams(c.symbol, "", c.secType, c.conId)
    if tclass:
        params = [p for p in params if p.tradingClass == tclass] or params
    exps = sorted({e for p in params for e in p.expirations})
    strikes = sorted({s for p in params for s in p.strikes})
    if max_exps:
        exps = exps[:max_exps]
    if band and spot == spot and spot:
        atm = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
        strikes = strikes[max(0, atm - band): atm + band + 1]
    tc = tclass or ""
    candidates = [Option(c.symbol, e, k, r, "SMART", tradingClass=tc, currency="USD")
                  for e in exps for k in strikes for r in ("C", "P")]
    return c, spot, _qualify(ib, candidates)


def snapshot_chain(ib: IB, contracts: list[Option]) -> list[dict]:
    """Stream the chain in line-limit batches, harvest greeks+OI+quotes, cancel."""
    rows: list[dict] = []
    for i in range(0, len(contracts), LINE_LIMIT):
        batch = contracts[i:i + LINE_LIMIT]
        tickers = [ib.reqMktData(o, "100,101", False, False) for o in batch]  # 101=OI
        ib.sleep(SETTLE_SECS)
        for o, t in zip(batch, tickers):
            mg = t.modelGreeks
            oi = t.callOpenInterest if o.right == "C" else t.putOpenInterest
            rows.append({
                "expiration": _fmt_exp(o.lastTradeDateOrContractMonth),
                "strike": float(o.strike),
                "right": "CALL" if o.right == "C" else "PUT",
                "bid": _num(t.bid), "ask": _num(t.ask),
                "bid_size": _int(t.bidSize), "ask_size": _int(t.askSize),
                "close": _num(t.last) if _num(t.last) is not None else _num(t.close),
                "volume": _int(t.volume),
                "delta": _num(mg.delta) if mg else None,
                "gamma": _num(mg.gamma) if mg else None,
                "vega": _num(mg.vega) if mg else None,
                "theta": _num(mg.theta) if mg else None,
                "implied_vol": _num(mg.impliedVol) if mg else None,
                "underlying_price": _num(mg.undPrice) if mg else None,
                "open_interest": _num(oi),
            })
        for o in batch:
            ib.cancelMktData(o)
        ib.sleep(0.2)
    return rows


def _to_df(rows: list[dict], sym: str, daystr: str, snap_ts: str, spot) -> pd.DataFrame:
    """Assemble harvested rows into the exact 41-column warehouse schema."""
    df = pd.DataFrame(rows)
    df["date"] = daystr
    df["symbol"] = sym
    df["timestamp"] = snap_ts
    df["underlying_timestamp"] = snap_ts
    # Per-row undPrice from greeks is best; fall back to the underlying spot.
    # spot can be None (index roots: reqTickers on an Index often returns NaN for
    # marketPrice/close/last, whereas the model-greeks undPrice IS populated). Guard
    # the fill: fillna(None) raises ValueError("Must specify a fill 'value' or
    # 'method'.") in pandas >=2, so only backfill when we actually have a spot —
    # otherwise leave the per-row undPrice values intact.
    spot_val = _num(spot)
    if "underlying_price" in df:
        if spot_val is not None:
            df["underlying_price"] = df["underlying_price"].fillna(spot_val)
    else:
        df["underlying_price"] = spot_val
    for col in SCHEMA_COLS:
        if col not in df.columns:
            df[col] = pd.NA          # ThetaData-only columns IBKR can't supply
    return df[SCHEMA_COLS]


def collect_day(ib: IB, sym: str, daystr: str,
                band: int | None = None, max_exps: int | None = None) -> tuple[str, int]:
    """Snapshot one root for one day and write it. Resumable: skips days on disk.

    Deliberately does NOT write an empty marker when nothing comes back (unlike the
    ThetaData backfill). A forward run that finds no data is almost always transient
    (after-hours, farm hiccup), and a false marker would poison the day forever via
    have_day(). Leaving it unwritten lets the next run retry.
    """
    if storage.have_day(sym, daystr):
        return ("skip", 0)
    c, spot, contracts = build_chain(ib, sym, band, max_exps)
    if not contracts:
        return ("no-chain", 0)
    snap_ts = datetime.now().isoformat(timespec="milliseconds")
    rows = snapshot_chain(ib, contracts)
    populated = sum(1 for r in rows if r["delta"] is not None or r["open_interest"] is not None)
    if populated == 0:
        return ("no-data", 0)        # don't poison the day; retry next run
    df = _to_df(rows, sym, daystr, snap_ts, spot)
    n = storage.write_day(sym, daystr, df)
    return ("ok", n)


def main() -> None:
    args = sys.argv[1:]
    test = "--test" in args
    launch = "--launch" in args
    full = "--full" in args              # override the band, capture the literal full chain
    roots = [a.upper() for a in args if not a.startswith("--")]
    if test and not roots:
        roots = ["SPY"]
    if not roots:
        roots = config.all_roots()
    daystr = date.today().strftime("%Y%m%d")
    if test:
        band, max_exps = 10, 1           # tiny safe slice
    elif full:
        band, max_exps = None, None      # literal full chain (~9.8h universe)
    else:
        band = max_exps = None           # per-root via config.forward_depth() below

    real_errors: list[str] = []
    ib = gw.connect(CLIENT, readonly=True, launch=launch)
    ib.errorEvent += lambda rid, code, msg, c: (
        real_errors.append(f"[{code}] {msg}") if code not in OK_STATUS else None)
    ib.reqMarketDataType(3)              # ask for delayed — EOD snapshot doesn't need live entitlement
    print(f"connected={ib.isConnected()} clientId={gw.clientids.get(CLIENT)} "
          f"{'TEST slice' if test else 'FULL chain'} | day={daystr} | roots={roots}")

    try:
        for sym in roots:
            t0 = datetime.now()
            b, mx = (band, max_exps) if (test or full) else config.forward_depth(sym)
            status, n = collect_day(ib, sym, daystr, band=b, max_exps=mx)
            dt = (datetime.now() - t0).total_seconds()
            print(f"  {sym:6} {status:9} rows={n:<7} {dt:5.1f}s"
                  + (f"  errors={len(real_errors)}" if real_errors else ""))
    finally:
        ib.disconnect()

    if real_errors:
        print("\nREAL errors (non-farm):")
        for e in real_errors[:20]:
            print("  ", e)
    else:
        print("\nno real errors (farm-OK status codes filtered).")


if __name__ == "__main__":
    main()
