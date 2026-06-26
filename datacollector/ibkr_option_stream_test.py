"""
ibkr_option_stream_test.py — the DEFINITIVE "can it stream the option slice live,
all day, no surprises" test. Run it DURING market hours (>= 09:30 ET).

It builds a realistic intraday slice (near-money strikes, near expirations, both
rights) for the chosen underlyings, subscribes to LIVE streaming market data with
open-interest ticks, samples every interval, and reports:
  * how many contracts are actively QUOTING (bid/ask present)
  * how many have GREEKS (gamma/IV) and OPEN INTEREST populated
  * how many market-data lines we're holding (vs the ~100 limit)
  * any REAL errors — especially line-limit (326/322) or disconnects
    (IBKR status codes 2104/2106/2158/2107/2119 are "farm OK" noise, filtered out)

Run tonight (after hours) it still proves SLICE CONSTRUCTION works; quotes/OI/greeks
will be blank until options actually trade. Run it at the open for the real proof.

Usage:
    <venv python> ibkr_option_stream_test.py [SECONDS] [UNDERLYING ...]
    e.g.  ibkr_option_stream_test.py 120 SPXW SPY
"""

from __future__ import annotations

import statistics
import sys
from datetime import datetime

from ib_async import IB, Index, Option, Stock

SECS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
UNDERLYINGS = [u.upper() for u in sys.argv[2:]] or ["SPXW"]
STRIKES_EACH_SIDE = 20          # ~near-money band; ~ (2*N+1)*2 contracts per expiry
N_EXPIRATIONS = 1               # nearest (0DTE-ish) first; widen later
# Informational "farm OK" codes + 200 (contract-not-found during slice discovery is
# benign — we just drop those strikes). The errors that matter for an all-day run are
# line-limit (322/326) and disconnects, which are none of these.
OK_STATUS = {2104, 2106, 2158, 2107, 2119, 2100, 2150, 200}

real_errors: list[str] = []
disconnects: list[str] = []


def _build_underlying(ib: IB, sym: str):
    """Return (contract, spot, secType-for-params, tradingClass-filter) for sym."""
    if sym in ("SPXW", "SPX"):
        c = Index("SPX", "CBOE", "USD")
        tclass = "SPXW" if sym == "SPXW" else None
    elif sym == "XSP":
        c = Index("XSP", "CBOE", "USD")
        tclass = None
    else:
        c = Stock(sym, "SMART", "USD")
        tclass = None
    ib.qualifyContracts(c)
    [t] = ib.reqTickers(c)
    spot = t.marketPrice() or t.close or t.last
    return c, spot, tclass


def _build_slice(ib: IB, sym: str) -> list:
    c, spot, tclass = _build_underlying(ib, sym)
    params = ib.reqSecDefOptParams(c.symbol, "", c.secType, c.conId)
    if tclass:
        params = [p for p in params if p.tradingClass == tclass] or params
    exps = sorted({e for p in params for e in p.expirations})[:N_EXPIRATIONS]
    strikes = sorted({s for p in params for s in p.strikes})
    if spot != spot or spot is None:                 # spot NaN after hours
        spot = statistics.median(strikes) if strikes else 0
    atm_i = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    band = strikes[max(0, atm_i - STRIKES_EACH_SIDE): atm_i + STRIKES_EACH_SIDE + 1]
    underlying_sym = c.symbol
    tc = tclass or ""
    candidates = [Option(underlying_sym, e, k, r, "SMART", tradingClass=tc, currency="USD")
                  for e in exps for k in band for r in ("C", "P")]
    # qualifyContracts RETURNS only the contracts IBKR could resolve — use that, so
    # strikes that don't exist for this expiration are silently dropped (not streamed).
    # (After hours the US options farm is offline, so this can come back empty — that's
    # expected tonight; at the open it resolves the real slice.)
    qualified = ib.qualifyContracts(*candidates) or []
    contracts = [o for o in qualified if o is not None and o.conId]
    print(f"  {sym}: spot~{spot}  exps={exps}  {len(band)} strikes  -> {len(contracts)} contracts")
    return contracts


def main() -> None:
    ib = IB()
    ib.errorEvent += lambda rid, code, msg, c: (
        real_errors.append(f"[{code}] {msg}") if code not in OK_STATUS else None)
    ib.disconnectedEvent += lambda: disconnects.append(datetime.now().strftime("%H:%M:%S"))

    ib.connect("127.0.0.1", 4002, clientId=24, readonly=True, timeout=10)
    ib.reqMarketDataType(1)
    print(f"connected {ib.isConnected()} | building slice for {UNDERLYINGS} ...")

    tickers = []
    for sym in UNDERLYINGS:
        try:
            for o in _build_slice(ib, sym):
                tickers.append(ib.reqMktData(o, "100,101", False, False))  # 101=open interest
        except Exception as e:
            real_errors.append(f"slice {sym}: {e}")
    print(f"holding {len(tickers)} live option lines (limit ~100). streaming ~{SECS}s ...\n")

    for _ in range(max(1, SECS // 15)):
        ib.sleep(15)
        quoting = sum(1 for t in tickers if t.bid == t.bid and t.bid is not None)
        greeked = sum(1 for t in tickers if t.modelGreeks and t.modelGreeks.gamma is not None)
        oi = sum(1 for t in tickers if (t.callOpenInterest or t.putOpenInterest))
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"  {stamp}  quoting={quoting}/{len(tickers)}  greeks={greeked}  OI={oi}  "
              f"errors={len(real_errors)} disconnects={len(disconnects)}")

    ib.disconnect()
    print("\n=== RESULT ===")
    print(f"lines held       : {len(tickers)}")
    print(f"real errors      : {real_errors if real_errors else 'NONE'}")
    print(f"disconnects      : {disconnects if disconnects else 'NONE (clean)'}")
    print("(after hours: quoting/greeks/OI will read 0 — construction is what's proven tonight;\n"
          " run at the open for the live quote/OI/greeks + endurance proof.)")


if __name__ == "__main__":
    main()
