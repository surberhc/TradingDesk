"""
flatten_accounts.py — sell every leftover position in the DU paper sub-accounts back to
ZERO. Approved decision `paperbot-flatten`. PAPER ONLY.

Lessons already paid for, applied here:
  * Trade the DU sub-accounts; pin the connection to a DU account so ib_async does not
    hang on the FA master's account-update stream.
  * Do NOT call whatIfOrder (it hangs). Place marketable-limit closing orders directly.
  * The gateway hard read-only lock is OFF, so no restart is needed - just connect.
  * Serialize: one closing order at a time; watch each fill; reconcile to zero; log.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ib_async import IB, Stock, LimitOrder   # noqa: E402

import ledger          # noqa: E402
import live_quotes     # noqa: E402
from connections import clientids, ibkr_paper      # noqa: E402

DU_ACCOUNTS = ["DU8922142", "DU8922143", "DU8922144", "DU8922145", "DU8922146"]
PIN = "DU8922142"   # pin connect to a DU account (avoids the FA-master hang)


def main() -> int:
    print("FLATTEN ALL DU ACCOUNTS -> zero (paper only)", flush=True)
    ib = IB()
    ib.connect(ibkr_paper.HOST, ibkr_paper.PAPER_PORT, clientId=clientids.get("paperbot_flatten"),
               readonly=False, timeout=15, account=PIN)
    try:
        ib.reqPositions()
        ib.sleep(2.0)
        leftovers = [p for p in ib.positions()
                     if p.account in DU_ACCOUNTS and p.position != 0]
        print(f"found {len(leftovers)} leftover position(s):", flush=True)
        for p in leftovers:
            print(f"  {p.account} {p.contract.symbol} {p.position:g}", flush=True)
        if not leftovers:
            print("ALL DU ACCOUNTS ALREADY FLAT. nothing to do.", flush=True)
            return 0

        fills = []
        for p in leftovers:
            sym = p.contract.symbol
            qty = p.position
            side = "SELL" if qty > 0 else "BUY"        # close longs / cover shorts
            q = live_quotes.fetch(ib, [sym]).get(sym)
            # marketable: hit the bid to sell, lift the ask to buy; fall back to last/close.
            ref = (q.bid if side == "SELL" else q.ask) if q else None
            if not ref:
                ref = (q.last or q.close) if q else None
            if not ref:
                print(f"  SKIP {p.account} {sym}: no usable price", flush=True)
                continue
            limit = round(ref, 2)

            contract = p.contract
            # Force SMART routing: a direct route to the listing exchange (ARCA/NASDAQ)
            # is rejected by IBKR's precautionary settings (Error 10311). conId still
            # pins the exact instrument.
            contract.exchange = "SMART"
            order = LimitOrder(side, abs(qty), limit)
            order.account = p.account
            order.tif = "DAY"
            # Sweep is running after the RTH close (16:09 CT). Allow extended-hours
            # execution so these liquid names actually fill instead of sitting RTH-only.
            order.outsideRth = True
            order.orderRef = f"paperbot:flatten:{p.account}:{sym}"
            order.transmit = True
            print(f"  {side} {abs(qty):g} {sym} @ {limit} in {p.account} ...", flush=True)
            trade = ib.placeOrder(contract, order)
            waited = 0
            while waited < 30 and not trade.isDone():
                ib.sleep(1.0)
                waited += 1
            st = trade.orderStatus
            print(f"    -> {st.status} filled={st.filled:g} @ {st.avgFillPrice or 0:.2f}", flush=True)
            fills.append({"account": p.account, "symbol": sym, "side": side,
                          "qty": abs(qty), "status": st.status,
                          "filled": float(st.filled), "avg": float(st.avgFillPrice or 0)})

        # Reconcile: re-read and confirm zero across all DU accounts.
        ib.reqPositions()
        ib.sleep(2.0)
        remaining = [(p.account, p.contract.symbol, p.position) for p in ib.positions()
                     if p.account in DU_ACCOUNTS and p.position != 0]
        print("\nRECONCILE:", flush=True)
        if remaining:
            print("  NOT flat yet:", remaining, flush=True)
        else:
            print("  ALL DU ACCOUNTS FLAT (zero positions).", flush=True)

        ledger.record_run({
            "mode": "FLATTEN", "account": "ALL_DU", "nav": 0.0, "daily_pnl": 0.0,
            "target_as_of": "n/a", "target_weights": {}, "intents": fills,
            "n_intents": len(fills), "n_approved": len(fills),
            "n_transmitted": len(fills), "halted": False, "halt_reason": "",
            "order_vetoes": [], "batch_vetoes": [], "remaining": remaining,
        })
        return 0 if not remaining else 3
    finally:
        ib.disconnect()
        print("disconnected.", flush=True)


if __name__ == "__main__":
    sys.exit(main())
