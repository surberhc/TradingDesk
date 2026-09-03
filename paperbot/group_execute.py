"""group_execute.py — the LIVE advisor-master target for the per-ticker group rail.

WHAT THIS IS
------------
The thin wiring layer between the pure planner (group_rebalance) and the proven block
executor (live_fa_block_execute.execute_fa_block_routes). It exists as its own module so
neither of those is edited: the block executor keeps working exactly as it does for the paper
master, and the per-account batch rail is not touched at all.

It also owns the LIVE advisor-master TargetGateway, which could not be written until the
master actually existed. It does now.

THE MASTER, READ LIVE 2026-09-03
--------------------------------
The port-4003 gateway login was switched from ``apsv1816`` to Andrew's advisor login
``asurber219`` and probed read-only:

    master              F6795549          (an F-prefixed live master; the PAPER one is DF...)
    client accounts     354               (the old login carried 18)
    existing FA groups  8                 Main 86, Ted 79, MainSmall 74, Rebalance 33,
                                          No Trade 14, Dougs Group 8, Income 8, Rob 4

All eight of those round-trip through fa_membership.parse_group_membership as a NO-OP, which
closed the standing MASTER_PLAN A.2 caveat that our group code had only ever seen fixtures.

STALE COMMENT WARNING. connections/clientids.py still says of consumer 63 that "the live 4003
login is NOT yet an advisor account: tested 2026-08-05 — 2 direct accounts, no master,
requestFA times out". That was true on 2026-08-05 and is NOT true now.

THE ACCOUNT WALL MATTERS MORE THAN IT USED TO
---------------------------------------------
The old login physically could not reach Ted's or Doug's books. This one carries all 354
accounts, so software is now the only thing that scopes a run. Verified 2026-09-03:
``roster.enrolled_roster()`` returns 185 — Andrew's book only, sourced from the CRM, with the
unfunded dropped. Every route still passes through the executor's own account wall on top of
that.
"""
from __future__ import annotations

from connections import clientids
from live_fa_block_execute import TargetGateway

# The live advisor master, read from the gateway on 2026-09-03. F-prefixed; the paper master
# is DF8922141. NEVER traded and NEVER pinned — the master's own account-update stream hangs
# the session (memory: fa-block-order-allocation), which is why connect pins to a client sub.
LIVE_MASTER_ACCOUNT = "F6795549"


def live_gateway(enrollment: dict, *, pin_account: str | None = None) -> TargetGateway:
    """Build the LIVE TargetGateway for the port-4003 advisor master.

    ``enrollment`` is ``{account -> model label}`` and MUST come from the CRM roster
    (roster.enrolled_roster_scan), never from config.ENROLLMENT. That hardcoded map is the
    paper build's five DU subs; on the live master it would be both wrong and dangerous,
    because the login now carries 354 accounts including two other advisors' books.

    ``pin_account`` defaults to the lowest-numbered enrolled account, deterministically, so
    two runs of the same scope pin the same way and a run is reproducible. It is only the
    connection pin — it confers nothing on that account and it is not traded differently.

    ``group_names`` is None on purpose. The per-tier group map (TIER_GROUPS) is meaningless
    here: this rail creates ONE GROUP PER TICKER PER RUN and the group name travels on the
    route itself (group_rebalance.routes_from_group_plans), so there is no static map to
    resolve and nothing that can drift between runs.

    Raises ValueError on an empty enrollment — an empty roster read must never silently
    produce a gateway that would then trade nothing, or worse, be widened by a later caller.
    """
    book = {str(a).strip(): str(v) for a, v in (enrollment or {}).items() if str(a).strip()}
    if not book:
        raise ValueError(
            "live_gateway: the enrollment is EMPTY. An empty roster read must never produce a "
            "live advisor-master gateway. FAILING LOUD.")

    pin = str(pin_account or "").strip() or sorted(book)[0]
    if pin not in book:
        raise ValueError(
            f"live_gateway: pin_account {pin!r} is not in the enrollment. The connection must "
            f"pin to an account this run is actually scoped to. FAILING LOUD.")
    if pin == LIVE_MASTER_ACCOUNT:
        raise ValueError(
            f"live_gateway: refusing to pin to the master {LIVE_MASTER_ACCOUNT} — its "
            f"account-update stream hangs the session. Pin a CLIENT sub-account.")

    return TargetGateway(
        name="LIVE",
        host="127.0.0.1",
        port=clientids.LIVE_TRADE_PORT,          # 4003
        clientid_consumer="live_fa_block_exec",  # 63, reserved for exactly this
        master_account=LIVE_MASTER_ACCOUNT,
        pin_account=pin,
        enrollment=book,
        group_names=None,                        # per-run groups; the name is on the route
    )
