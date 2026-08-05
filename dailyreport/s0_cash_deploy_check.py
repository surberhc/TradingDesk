"""
s0_cash_deploy_check.py — the S0 idle-cash "consider deploying" proposal job.

WHAT IT IS
----------
Under the propose-and-arm posture, the desk automatically surfaces a proposal when the S0
account is holding meaningfully more cash than the small standing buffer it keeps on purpose
(deposits landed, dividends accrued). This job reads the account READ-ONLY, and if the free
cash held ABOVE the standing buffer exceeds an operational fraction of NAV, posts a
plain-English "idle cash — consider deploying" notice to the in-app Action Center
(dashboard/desk/action_center.py). It NEVER trades: the notice only points the operator at
the Control Plane, where a deploy still requires the deliberate review -> arm -> transmit gate.

Cadence: run on a schedule (e.g. daily, after the close). Below the threshold it posts
nothing. It de-duplicates by an OPEN-notice key, so repeated runs while the same idle cash
sits there keep ONE current notice (with fresh numbers), not a growing pile.

THRESHOLD (operational, NOT frozen strategy config)
---------------------------------------------------
CASH_DEPLOY_THRESHOLD_PCT is the fraction of NAV of EXCESS cash (cash held ABOVE the standing
buffer) that justifies surfacing a proposal. It is an operational alerting knob, deliberately
separate from the FROZEN strategy/regime/sizing config — changing it changes only WHEN a
heads-up appears, never how the strategy sizes or trades. Working default 2%.

SCOPE / SAFETY — INFORMATIONAL + READ-ONLY, ZERO-TRANSMIT
--------------------------------------------------------
Connects to the transmit-CAPABLE live-trading Gateway (port 4003) but is read-only by
construction: ibkr_live_trade.connect(readonly=True), and there is NO order path in this file
(it only reads accountSummary()). Not order-affecting: no paperbot version bump.

USAGE
-----
    <venv python> s0_cash_deploy_check.py            # real run (task uses this)
    <venv python> s0_cash_deploy_check.py --dry-run  # read live, decide + print, post NOTHING
    <venv python> s0_cash_deploy_check.py --threshold 0.03   # override the threshold once
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _p in (str(_HERE), str(_REPO / "connections"), str(_REPO / "paperbot"),
           str(_REPO / "dashboard" / "desk")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import investable  # paperbot/investable.py — the single buffer accessor  # noqa: E402

# The S0 account (owner-retargeted 2026-07-28 to the funded trust account). Single, clearly
# named constant so re-pointing is one edit; mirrors s0_month_end_snapshot.SNAPSHOT_ACCOUNT.
ACCOUNT = "U14438624"

# Operational alerting knob (NOT frozen strategy config): excess-cash fraction of NAV that
# triggers a deploy proposal. See module docstring.
CASH_DEPLOY_THRESHOLD_PCT = 0.02

_DEDUP_KEY = "s0_cash_deploy_open"


def _log(msg: str) -> None:
    import datetime as dt
    try:
        print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Pure decision — no broker, unit-testable.
# --------------------------------------------------------------------------- #
def decide(net_liq: float | None, total_cash: float | None, *,
           buffer: float | None = None,
           threshold: float = CASH_DEPLOY_THRESHOLD_PCT) -> dict:
    """Decide whether idle cash warrants a deploy proposal. Pure function.

    Returns {ok, should_propose, net_liq, total_cash, buffer, cash_pct, excess_cash,
    excess_pct, threshold, reason}. ok=False (never proposes) when NAV is missing/non-positive
    or cash is unreadable. Trigger is STRICTLY excess_pct > threshold."""
    if buffer is None:
        buffer = investable.buffer_pct()
    out = {"ok": False, "should_propose": False, "net_liq": net_liq,
           "total_cash": total_cash, "buffer": buffer, "cash_pct": None,
           "excess_cash": None, "excess_pct": None, "threshold": threshold, "reason": ""}
    if not net_liq or net_liq <= 0:
        out["reason"] = "no positive NetLiquidation could be read"
        return out
    if total_cash is None:
        out["reason"] = "no cash balance could be read"
        return out
    cash_pct = total_cash / net_liq
    excess_pct = cash_pct - buffer
    excess_cash = total_cash - buffer * net_liq
    out.update({"ok": True, "cash_pct": cash_pct, "excess_pct": excess_pct,
                "excess_cash": excess_cash})
    if (excess_pct - threshold) > 1e-9:
        out["should_propose"] = True
        out["reason"] = "excess cash above the buffer exceeds the deploy threshold"
    else:
        out["reason"] = "cash is within the buffer + threshold band"
    return out


# --------------------------------------------------------------------------- #
# Read-only account read (mirrors s0_month_end_snapshot.read_holdings).
# --------------------------------------------------------------------------- #
def _filter_account(rows, account):
    return [r for r in rows if getattr(r, "account", None) == account]


def _tag(summary_rows, tag: str):
    for r in summary_rows:
        if getattr(r, "tag", None) == tag:
            try:
                return float(r.value)
            except (TypeError, ValueError):
                return None
    return None


def read_cash(account: str = ACCOUNT) -> dict:
    """Connect READ-ONLY to the live-trading Gateway (port 4003), read the account's
    NetLiquidation + TotalCashValue. Transmits NOTHING. Raises on any connect/read problem so
    the caller can log honestly and post nothing."""
    from connections import ibkr_live_trade
    ib = ibkr_live_trade.connect("s0_cash_deploy_check", launch=True, readonly=True)
    try:
        summary = _filter_account(ib.accountSummary(), account)
        if not summary:
            seen = sorted(str(a) for a in
                          {getattr(r, "account", None) for r in ib.accountSummary()}
                          if a is not None)
            raise RuntimeError(
                f"account {account} not found under the live-trading login "
                f"(accounts seen: {seen})")
        net_liq = _tag(summary, "NetLiquidation")
        total_cash = _tag(summary, "TotalCashValue")
        return {"net_liq": net_liq, "total_cash": total_cash}
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Notice text (plain English, per the dashboard-labels standard).
# --------------------------------------------------------------------------- #
def build_notice(d: dict) -> tuple[str, str, str]:
    """(title, body, action_hint) for a should_propose decision."""
    cash = d["total_cash"]
    excess = d["excess_cash"]
    cash_pct = d["cash_pct"] * 100
    excess_pct = d["excess_pct"] * 100
    buffer_pct = d["buffer"] * 100
    title = f"Idle cash to deploy — about ${excess:,.0f} above the reserve"
    body = (
        f"Account {ACCOUNT} is holding ${cash:,.0f} in cash — about {cash_pct:.1f}% of the "
        f"account. That is roughly ${excess:,.0f} ({excess_pct:.1f}% of the account) more "
        f"than the {buffer_pct:.1f}% cash reserve it normally keeps to cover fees. That's "
        f"enough to consider putting to work in the Strategy 0 target."
    )
    hint = (
        "Open the Control Plane to review a read-only preview and, if you want to deploy, arm "
        "and execute it there. Nothing trades until you do — this is only a heads-up."
    )
    return title, body, hint


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="read the account live and print the decision, but post NOTHING to "
                         "the Action Center.")
    ap.add_argument("--threshold", type=float, default=CASH_DEPLOY_THRESHOLD_PCT,
                    help="override the excess-cash fraction-of-NAV threshold for this run.")
    args = ap.parse_args(argv)

    try:
        data = read_cash()
    except Exception as e:
        _log(f"could not read the account ({type(e).__name__}: {e}); posting nothing. The "
             f"live-trading Gateway (port 4003) may be down or not logged in.")
        return 1

    d = decide(data.get("net_liq"), data.get("total_cash"), threshold=args.threshold)
    if not d["ok"]:
        _log(f"cash check inconclusive: {d['reason']}; posting nothing.")
        return 1

    _log(f"account={ACCOUNT} NetLiq={d['net_liq']:,.2f} cash={d['total_cash']:,.2f} "
         f"cash_pct={d['cash_pct']*100:.2f}% excess={d['excess_cash']:,.2f} "
         f"excess_pct={d['excess_pct']*100:.2f}% threshold={d['threshold']*100:.2f}% "
         f"-> should_propose={d['should_propose']}")

    if not d["should_propose"]:
        print(f"No deploy proposal: cash is within the reserve + threshold band "
              f"({d['cash_pct']*100:.2f}% cash vs "
              f"{(d['buffer']+d['threshold'])*100:.2f}% trigger).")
        return 0

    title, body, hint = build_notice(d)
    if args.dry_run:
        print("[dry-run] WOULD post an Action Center notice (posting nothing):")
        print(f"  title: {title}")
        print(f"  body:  {body}")
        print(f"  hint:  {hint}")
        return 0

    import action_center
    # Snooze / "ignore for N days": if the operator snoozed this idle-cash notice, SKIP posting
    # while the snooze is live. Dismiss alone does NOT durably suppress (the next run re-posts a
    # fresh notice) — the poster-side is_snoozed skip is what actually silences the daily nag.
    if action_center.is_snoozed(_DEDUP_KEY):
        print("Idle-cash notice is snoozed (ignored) by the operator; posting nothing.")
        return 0
    key = action_center.post_notice(kind="cash_deploy", title=title, body=body,
                                    severity="warn", action_hint=hint, dedup_key=_DEDUP_KEY)
    if key:
        print(f"Posted idle-cash deploy proposal to the Action Center (notice {key}).")
        return 0
    _log("posting the Action Center notice failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
