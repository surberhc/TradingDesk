"""
s0_month_end_snapshot.py — Job A of the S0 month-end EXACT-verdict pair.

WHAT IT IS
----------
Strategy 0 (Adaptive All-Weather Core) rebalances PURE MONTHLY: the signal is taken on
the LAST TRADING DAY of the month and the trade executes the NEXT session (T+1). To turn
the evening month-end heads-up (s0_month_end_notice.py, Job B) from a vague "rebalance due"
into an EXACT "TRADE tomorrow / NO trade tomorrow" verdict, Job B needs the account's ACTUAL
holdings at the month-end close. But the two facts it needs live on different clocks:

  * HOLDINGS need the live-trading Gateway, which is only up until the ~3:05pm CT teardown.
  * The EXACT TARGET needs the FINAL close, which the nightly Tiingo pull lands ~7pm CT.

So this is split. Job A (THIS file) runs near the close (~2:50pm CT) on the month-end SIGNAL
day, connects READ-ONLY to the live-trading Gateway, snapshots the account's positions +
NetLiquidation, and writes them to an off-repo JSON stamped with the date. Job B loads that
snapshot in the evening, computes the target on final close data, sizes the plan, and emails
the exact verdict. Because the S0 month-end account is buy-and-hold (no intraday trading), a
near-close snapshot equals the day's FINAL holdings.

SCOPE / SAFETY — INFORMATIONAL + READ-ONLY, ZERO-TRANSMIT
---------------------------------------------------------
This connects to the transmit-CAPABLE live-trading Gateway (port 4003) but is read-only by
construction and never transmits:
  * It calls ibkr_live_trade.connect(readonly=True) and NEVER passes readonly=False. The
    account is transmit-capable at the broker level, so read-only is a real, honored session
    flag — a bare connection here physically cannot write.
  * There is NO order path in this file: it builds no order object and calls no
    place()/placeOrder()/transmit method. It only reads accountSummary() + positions().
Not order-affecting: no paperbot version bump.

CALENDAR / IDEMPOTENCY
----------------------
Reuses Job B's verified NYSE calendar decision (is_month_end_signal_day). On any weekday
that is NOT the month-end signal day it does nothing and exits 0. A snapshot is written only
on the signal day; if the Gateway is unreachable it writes a FAILED marker (ok=false) so Job
B can fail HONESTLY ("could not read holdings at close") rather than guess a verdict.

ACCOUNT
-------
SNAPSHOT_ACCOUNT is the account whose month-end holdings drive the S0 verdict. It is a
single, clearly-named constant so it is trivial to re-point. See the note on the constant.

USAGE
-----
    <venv python> s0_month_end_snapshot.py              # real run (task uses this)
    <venv python> s0_month_end_snapshot.py --dry-run    # decide + print; connect nothing
    <venv python> s0_month_end_snapshot.py --dry-run --as-of 2026-07-31
    <venv python> s0_month_end_snapshot.py --as-of 2026-07-31   # force-decide a date

The snapshot file path can be overridden with the env var TRADINGDESK_S0_MONTHEND_SNAPSHOT
(used by tests / verification so the real state file is never touched).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

# --- repo-relative path setup (derive from __file__, per CLAUDE.md) --------- #
# dailyreport is a direct child of the repo root. Add this file's own dir so the sibling
# `import s0_month_end_notice` resolves, and the connections dir so `from connections
# import ...` resolves — regardless of the cwd the task launches us from.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _p in (str(_HERE), str(_REPO / "connections")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# The month-end signal-day decision is Job B's; import it so both jobs use ONE calendar rule.
from s0_month_end_notice import is_month_end_signal_day  # noqa: E402

# The S0 month-end account. NOTE (2026-07-30): the launching spec named U14438624 (the
# individual test/trust account under the 4003 live-trading login) as the month-end
# snapshot target. paperbot/s0_live.py's separate morning-pilot lane instead uses
# S0_LIVE_ACCOUNT = "U5721712"; the two must be reconciled by the owner if they are meant
# to be the same account. This constant is deliberately isolated so re-pointing is one edit.
SNAPSHOT_ACCOUNT = "U14438624"

# Off-repo state (never synced, never committed). Overridable for tests/verification.
_DEFAULT_SNAPSHOT = Path(r"C:\TradingDesk-Local\state\dailyreport\s0_month_end_snapshot.json")


def snapshot_path() -> Path:
    """Where the close-time holdings snapshot is written / read. Env override lets a
    test or verification run point at a temp file instead of the real state file."""
    env = os.environ.get("TRADINGDESK_S0_MONTHEND_SNAPSHOT")
    return Path(env) if env else _DEFAULT_SNAPSHOT


def _log(msg: str) -> None:
    """Everything to stderr so a real run's stdout stays clean. Never raises."""
    try:
        print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Reading the live account (READ-ONLY) — filtered to SNAPSHOT_ACCOUNT.
# --------------------------------------------------------------------------- #
def _filter_account(rows, account: str):
    """Keep only rows whose .account is `account`. The 4003 login exposes more than one
    managed account plus an 'All' aggregate, so filtering makes every read deterministic
    and guarantees we never read the wrong account's numbers."""
    return [r for r in rows if getattr(r, "account", None) == account]


def _net_liq(summary_rows) -> float | None:
    for r in summary_rows:
        if getattr(r, "tag", None) == "NetLiquidation":
            try:
                return float(r.value)
            except (TypeError, ValueError):
                return None
    return None


def read_holdings(account: str = SNAPSHOT_ACCOUNT) -> dict:
    """Connect READ-ONLY to the live-trading Gateway (port 4003), read the account's
    positions + NetLiquidation, and return them as plain data. Transmits NOTHING.

    Raises on any connection/read problem so the caller writes an honest FAILED marker.
    """
    from connections import ibkr_live_trade
    ib = ibkr_live_trade.connect("s0_month_end_snapshot", launch=True, readonly=True)
    try:
        summary = _filter_account(ib.accountSummary(), account)
        if not summary:
            seen = sorted(str(a) for a in
                          {getattr(r, "account", None) for r in ib.accountSummary()}
                          if a is not None)
            raise RuntimeError(
                f"account {account} not found under the live-trading login "
                f"(accounts seen: {seen})")
        net_liq = _net_liq(summary)
        if not net_liq or net_liq <= 0:
            raise RuntimeError(f"could not read a positive NetLiquidation for {account}")
        positions_raw = _filter_account(ib.positions(), account)
        positions = {p.contract.symbol: p.position
                     for p in positions_raw if getattr(p, "position", 0) != 0}
        return {"net_liq": net_liq, "positions": positions}
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Snapshot file I/O.
# --------------------------------------------------------------------------- #
def write_snapshot(record: dict) -> Path:
    """Write the snapshot JSON (success or failed marker). Never raises into the caller."""
    p = snapshot_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as e:
        _log(f"could not write snapshot to {p} ({type(e).__name__}: {e})")
    return p


def _record(as_of: dt.date, *, ok: bool, net_liq=None, positions=None, error=None) -> dict:
    rec = {
        "as_of": as_of.isoformat(),
        "account": SNAPSHOT_ACCOUNT,
        "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
        "ok": ok,
    }
    if ok:
        rec["net_liq"] = net_liq
        rec["positions"] = positions or {}
    else:
        rec["error"] = error
    return rec


# --------------------------------------------------------------------------- #
# Main.
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="decide + print what WOULD be snapshotted; connect nothing, "
                         "write no file.")
    ap.add_argument("--as-of", metavar="YYYY-MM-DD", default=None,
                    help="override 'today' (for testing).")
    args = ap.parse_args(argv)

    try:
        as_of = (dt.date.fromisoformat(args.as_of) if args.as_of else dt.date.today())
    except ValueError as e:
        _log(f"bad --as-of value {args.as_of!r}: {e}")
        return 2

    try:
        signal_day = is_month_end_signal_day(as_of)
    except Exception as e:
        _log(f"FATAL deciding signal day for {as_of}: {type(e).__name__}: {e}")
        return 1

    if not signal_day:
        _log(f"{as_of} is not the S0 month-end signal day — no snapshot to take.")
        if args.dry_run:
            print(f"[dry-run] {as_of} is NOT the month-end signal day. "
                  f"No snapshot would be taken.")
        return 0

    if args.dry_run:
        print(f"[dry-run] {as_of} IS the month-end signal day. Would connect READ-ONLY "
              f"to the live-trading Gateway (port 4003, clientId s0_month_end_snapshot) "
              f"and snapshot account {SNAPSHOT_ACCOUNT}'s positions + NetLiquidation to:")
        print(f"    {snapshot_path()}")
        print("[dry-run] Connecting nothing, writing nothing. (Read-only, zero-transmit.)")
        return 0

    # Real run: connect READ-ONLY, read, write. Any failure -> honest FAILED marker.
    try:
        data = read_holdings()
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        _log(f"snapshot FAILED for {as_of} ({msg}); writing failed marker so Job B "
             f"can fail-honest.")
        p = write_snapshot(_record(as_of, ok=False, error=msg))
        _log(f"failed marker written to {p}")
        return 1

    p = write_snapshot(_record(as_of, ok=True,
                               net_liq=data["net_liq"], positions=data["positions"]))
    _log(f"snapshot written to {p}: account={SNAPSHOT_ACCOUNT} "
         f"NetLiq={data['net_liq']:,.2f} positions={len(data['positions'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
