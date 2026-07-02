"""
download.py — one-time bulk pull of EOD option chains into the local warehouse.

Run DURING the paid ThetaData month. Idempotent + resumable: it skips any
(symbol, day) already on disk, so you can stop/restart freely. After it finishes
you cancel the subscription and extend the data FORWARD for free with the IBKR
collector (same one-file-per-day shape).

ThetaData's EOD endpoints require expiration=* one DAY at a time, so for each root
and each business day in [GRAB_START, GRAB_END] it:
  1. pulls EOD greeks       (gamma, implied_vol, OHLC, underlying_price, full greeks)
  2. pulls EOD open_interest (per strike)
  3. joins on (symbol, expiration, strike, right)   <- NOT timestamp; the two
     endpoints stamp different intraday times for the same trading day
  4. writes raw/options/{SYMBOL}/{YYYYMMDD}.parquet

Usage:
    python download.py SPX             # one root (start here)
    python download.py SPX SPXW VIX    # the core gamma set
    python download.py                 # full universe (long; resumable)
"""

from __future__ import annotations

import sys

import pandas as pd

import config
import storage
import thetadata_client as td

JOIN_KEYS = ["symbol", "expiration", "strike", "right"]
# EOD-meaningless microstructure noise — dropped to keep files lean. Everything
# else (all greeks incl. vanna/charm/etc., IV, prices, sizes, OI) is kept so we
# never have to re-grab for a future second-order-greek strategy.
DROP_COLS = ["bid_exchange", "bid_condition", "ask_exchange", "ask_condition"]


def _business_days(start: str, end: str) -> list[str]:
    rng = pd.bdate_range(pd.to_datetime(start), pd.to_datetime(end))
    return [d.strftime("%Y%m%d") for d in rng]


def _live_expirations_for_day(symbol: str, daystr: str) -> list[str]:
    """Expirations for `symbol` that are still open on `daystr` (>= that day).

    Used ONLY on the current-day path. Returns YYYYMMDD strings (the format the
    history endpoints take, matching `daystr`); the catalog endpoint hands back
    dashed strings, so we strip the dashes. Expirations that already expired before
    `daystr` are dropped — they carry no current-day data and only waste requests.
    """
    exps = td.list_expirations(symbol)          # dashed, all-time
    out = [e.replace("-", "") for e in exps]
    return sorted(e for e in out if e >= daystr)


def pull_day(symbol: str, daystr: str, current_day: bool = False) -> pd.DataFrame:
    """Greeks ⨝ open_interest for one root on one day. Empty if no data (holiday).

    `current_day=True` switches to the per-expiration path required for the CURRENT
    (unsettled) trading day: the history endpoints reject expiration=* for today
    (HTTP 400 "Cannot fetch current-day data without specifying an expiration"), so
    we enumerate the root's live expirations and request each explicitly. Historical
    (settled) days keep the single fast expiration=* call unchanged.
    """
    if current_day:
        exps = _live_expirations_for_day(symbol, daystr)
        if not exps:
            return pd.DataFrame()               # unknown root / nothing live -> empty
        greeks = td.eod_greeks_current_day(symbol, daystr, exps)
        if greeks.empty:
            return greeks
        oi = td.eod_open_interest_current_day(symbol, daystr, exps)
    else:
        greeks = td.eod_greeks(symbol, daystr, daystr)
        if greeks.empty:
            return greeks
        oi = td.eod_open_interest(symbol, daystr, daystr)
    if not oi.empty and "open_interest" in oi.columns:
        greeks = greeks.merge(oi[JOIN_KEYS + ["open_interest"]], on=JOIN_KEYS, how="left")
    greeks = greeks.drop(columns=[c for c in DROP_COLS if c in greeks.columns])
    greeks.insert(0, "date", daystr)
    return greeks


def main(roots: list[str]) -> None:
    if not td.connected():
        sys.exit(f"Theta Terminal not reachable at {config.THETA_BASE_URL}. "
                 "Start it first:  python start_terminal.py")
    days = _business_days(config.GRAB_START, config.GRAB_END)
    for symbol in roots:
        done = pulled = rows = 0
        for daystr in days:
            if storage.have_day(symbol, daystr):
                done += 1
                continue
            try:
                df = pull_day(symbol, daystr)
                n = storage.write_day(symbol, daystr, df)
                pulled += 1
                rows += n
            except Exception as e:                       # keep going; resume later
                print(f"  FAIL  {symbol} {daystr}: {e}", flush=True)
            if pulled and pulled % 50 == 0:
                print(f"  {symbol}: {pulled} days pulled, {rows:,} rows so far", flush=True)
        print(f"DONE {symbol}: {pulled} new days ({rows:,} rows), {done} already had.", flush=True)
    storage.rebuild_catalog()
    print(f"\nCatalog rebuilt: {config.CATALOG_DB}", flush=True)


if __name__ == "__main__":
    requested = [r.upper() for r in sys.argv[1:]] or config.all_roots()
    main(requested)
