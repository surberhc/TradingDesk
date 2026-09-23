"""stub_notice.py — the ONE wording, and the ONE Action Center post, for a stranded stub.

WHAT A STUB IS. The desk's execution rail can only place whole-share orders: Interactive
Brokers refuses any fractional order through the API (errors 10243 / 10244), so a full-exit
sell of 13.8499 shares goes out as 13 and leaves 0.8499 behind. Nothing is ordered for that
remainder, nothing is recorded, and until now nothing said so. Over months more than 1,300 of
them accumulated across the book and it took a manual investigation to find them.

WHAT A STUB IS NOT — and this is the whole rule. A fraction is only a problem when the ticker
is LEAVING the account entirely: the model no longer holds it and a partial share is stranded.
A fraction left over on a position the model still wants is deliberately NOT reported. Owner's
words, 2026-09-23: "I'm not going to make a trade on a partial share if the client owns the
actual ticker anyway. We're only going to do the stub cleanup whenever we sell out of
something and no longer hold it in the portfolio and there's a partial share left." Paying a
commission to zero a fraction of a ticker the client goes on owning is money wasted, so this
module never asks anyone to.

The test is rebalance_engine.is_full_exit — the order path's OWN definition — never a second
rule assembled here. Two callers share this wording so the screen and the client system say
the same thing:

    paperbot/group_execute.py             at trade time, off group_rebalance.stranded_stubs
    dailyreport/outofspec_scan_check.py   nightly safety net, off the whole-book scan

POSTING reuses dashboard/desk/action_center.post_notice exactly as it stands — no new
credential, no new connection, and that module is not modified. The title leads with a capital
STUB so it stands out in the Action Center, and one dedup key keeps repeated runs to a single
open alert rather than a growing pile.

PLAIN ENGLISH (house rule #1): every sentence posted here is fully spelled out. The one
deliberate exception is the word STUB itself, which the owner asked for in capitals.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The Action Center poster lives with the desk dashboard. Derived from __file__ because the
# repo has moved before; mirrors the path insert action_center itself does for paperbot.
_DESK = Path(__file__).resolve().parents[1] / "dashboard" / "desk"
if _DESK.is_dir() and str(_DESK) not in sys.path:
    sys.path.insert(0, str(_DESK))

# ONE open alert at a time. Closing it in the client system is what lets the next run raise it
# again — the same contract every other desk alert follows.
DEDUP_KEY = "fractional_stub_open"

# post_notice maps kind -> the client system's task category. Its own name, so closing a
# drift alert can never hide a stub and closing a stub alert can never hide drift.
KIND = "fractional_stub"


def total_value(stubs) -> float:
    """What all the stranded fractions are worth together. 0.0 when nothing was priced."""
    return round(sum(float(s.get("value") or 0.0) for s in (stubs or [])), 2)


def summary_line(stubs) -> str:
    """The one-line headline, for the screen and for the alert title. Leads with STUB."""
    rows = list(stubs or [])
    n_acct = len({s.get("account") for s in rows})
    thing = "partial share" if len(rows) == 1 else "partial shares"
    acct = "account" if n_acct == 1 else "accounts"
    return (f"STUB — {len(rows)} {thing} left behind in {n_acct} {acct} after selling "
            f"out of a holding")


def describe(stubs) -> list[str]:
    """One fully spelled-out line per stranded fraction: account, ticker, shares, value."""
    lines = []
    for s in stubs or []:
        value = float(s.get("value") or 0.0)
        worth = f"worth about ${value:,.2f}" if value else "value not available"
        lines.append(f"  Account {s.get('account')} — {s.get('symbol')}, "
                     f"{float(s.get('quantity') or 0.0):.4f} of a share left, {worth}")
    return lines


def build_notice(stubs, *, where: str = "") -> tuple[str, str, str]:
    """(title, body, action_hint) for a set of stranded stubs. Pure — posts nothing.

    `where` names what found them, so the reader knows whether this came from a trade that
    just ran or from the overnight sweep."""
    rows = list(stubs or [])
    value = total_value(rows)
    worth = (f" They are worth about ${value:,.2f} in total."
             if value else " Their total value could not be priced.")
    body = (
        f"{summary_line(rows)}.\n\n"
        f"Someone was rolled out of a model holding: the whole shares were sold, but the "
        f"partial share left over could not be sold with them. Interactive Brokers will not "
        f"accept an order for part of a share through the automated connection the desk "
        f"trades on, so the desk could not clear these and they are still sitting in the "
        f"accounts.{worth}\n\n"
        f"These need to be sold by hand in the Interactive Brokers desktop platform, which "
        f"is the only place a partial share can be traded.\n\n"
        f"Accounts and holdings affected:\n" + "\n".join(describe(rows)))
    if where:
        body += f"\n\nFound by: {where}."
    hint = (
        "Sell each partial share listed above by hand in the Interactive Brokers desktop "
        "platform, then close this item. Nothing was traded because of this message and "
        "nothing will trade on its own — the desk cannot place an order for part of a share.")
    return summary_line(rows), body, hint


def post(stubs, *, where: str = "") -> str | None:
    """File ONE Action Center alert about these stubs. Returns whatever post_notice returns
    (the new task's id, SKIPPED when one is already open, FAILED when it could not post), or
    FAILED if the poster itself could not be loaded. Posts nothing when there are no stubs.

    NEVER raises into a caller: a run that has already traded must not die because an alert
    could not be filed."""
    rows = list(stubs or [])
    if not rows:
        return ""
    title, body, hint = build_notice(rows, where=where)
    try:
        import action_center  # noqa: PLC0415 — lazy so importing this module stays cheap
        return action_center.post_notice(
            kind=KIND, title=title, body=body, severity="warn",
            action_hint=hint, dedup_key=DEDUP_KEY, detail_json=rows)
    except Exception as exc:  # noqa: BLE001
        print(f"    !! could not file the STUB alert in the client system "
              f"({type(exc).__name__}: {exc}). The stubs are still listed above.")
        return None
