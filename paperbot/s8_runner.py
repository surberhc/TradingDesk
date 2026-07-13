"""
s8_runner.py — S8 (British IC + B2 long-leg auto-close) SCHEDULED ENTRY POINT.
Stage 5 (final stage) of the 5-stage S8 build (see docs/S8_SPEC.md and the approved
build plan at plans/calm-riding-hammock.md). Modeled tightly on morning_execute_run.py's
proven safe-rollout shape.

WHAT THIS SCRIPT IS FOR
------------------------
Fired frequently during market hours by a Windows Scheduled Task with an OS-level
repetition-interval trigger (the same mechanism already used for
HeartbeatStalenessAlarm/CanslimOverlayWatchdog — NOT N separate fixed-time triggers).
Registering that scheduled task is explicitly OUT OF SCOPE for this build (see the
approved plan) — this file is built and independently testable first.

CONNECTION TARGET — LIVE-DATA GATEWAY ONLY, NEVER THE PAPER GATEWAY (decided 2026-07-13)
------------------------------------------------------------------------------------------
This file connects EXCLUSIVELY to the separate, deliberately read-only-only live-side
Gateway (`connections.ibkr_live_data`, port 4001) for BOTH steps of its monitoring/
decision cycle — the account-summary read for the margin gate AND the option-chain
snapshot for strike selection. It never imports or calls `connections.ibkr` (the paper
Gateway, port 4002) anywhere. This was a deliberate pivot decided in session on
2026-07-13, away from the build plan's original framing (paper Gateway, clientId
`paperbot_s8`): `connections/ibkr_live_data.py` was built in the neutral `connections/`
package specifically so either a future paper-side or live-side consumer could use it,
and this is that legitimate use — not a boundary violation of anything. Because the
live-data Gateway only ever authenticates as exactly one personal account (no sub-account
selection exists or is needed on that connection), the `accountSummary()` this file reads
is that real personal account's own summary, not any paper DU sub-account's.

PILOT_MODE (below) remains the PRIMARY control blocking any transmission — unchanged by
this pivot. The live-data connection's own structural read-only-ness (see
`ibkr_live_data.connect()`'s docstring: no `readonly` override exists, it is hardcoded
True, and that module exposes no order-placement method anywhere) is a SECOND,
independent backstop layered underneath PILOT_MODE, not a replacement for it — the two
guardrails are orthogonal and both must hold.

DUE-CHECK FAST PATH (ZERO gateway contact on the common no-op cycle)
----------------------------------------------------------------------
Every fire, BEFORE touching IBKR at all: is any template's entry time due right now?
s8_config.ENTRY_GRID_CT gives each template's real, empirically-derived core entry
slots in CT (US/Central) — see that module's own header for the derivation. Templates
whose ENTRY_GRID_CT is None (the real MATCHED-fills sample was too thin to name a grid,
per s8_config.py's own comment) are NEVER checked, NEVER fire, at ANY time of day.

TOLERANCE WINDOW: +/- 2 minutes (DUE_TOLERANCE_MINUTES below). This scheduled task
fires on a repetition interval, not exactly on the minute, so some slack is required to
reliably catch a grid slot at all. The window is sized against the data, not guessed:
the tightest gap between any two slots within any one template's own ENTRY_GRID_CT
(checked across all 11 templates) is 5 minutes (e.g. Puts-80-$4's 08:45/08:50). A
+/-2min half-window per slot is a 4-minute total window per slot — strictly inside
that 5-minute floor — so this tolerance can never make two of one template's own grid
slots simultaneously "due" (which would create an ambiguous double-fire). Widening the
tolerance further would risk exactly that collision; 2 minutes is the largest half-
window that stays safely under the observed 5-minute floor with margin to spare.

If nothing is due for ANY template, this makes literally zero IBKR contact (no connect,
no clientId used, no ledger write) — mirrors morning_execute_run.py's own "no staged
file -> zero gateway touch" fast path.

ACCOUNT == "TBD" REFUSAL (loud, not silent) — NOW A VESTIGIAL/INFORMATIONAL-ONLY CHECK
------------------------------------------------------------------------------------------
s8_config.ACCOUNT was the "TBD" placeholder at the time this refusal was first written;
Andrew has since decided it (== "DU8922146", 2026-07-13 — see s8_config.py and
conductor/ACCOUNT_ALLOCATION.md), so this check no longer fires in practice. More
importantly, as of the live-data pivot (also 2026-07-13, see "CONNECTION TARGET" above),
this check no longer gates a paper-account CONNECTION at all — this file's Gateway
connection doesn't touch, select, or depend on any paper DU sub-account any more; the
live-data Gateway sees exactly one (real, personal) account with no sub-account concept.
s8_config.ACCOUNT is kept and still read here purely for PROVENANCE/LOGGING (it's
threaded into `order_ref` and the ledger record below as "which future paper/live
transmission path this pilot cycle's decisions would eventually belong to") — it is
informational/reserved for a future paper- or live-transmission build, NOT the account
this connection actually queries. The refusal itself is left in place as a cheap,
harmless belt-and-suspenders check (if `s8_config.ACCOUNT` is ever reset to "TBD" for
some reason, still refuse loudly rather than log provenance-less records) rather than
because it protects anything about today's connection path.

PILOT_MODE = True (HARDCODED — see the constant below)
---------------------------------------------------------
Identical pattern and identical safety intent to morning_execute_run.py's own
PILOT_MODE: this build ONLY logs/emails "WOULD HAVE TRANSMITTED: ..." — it NEVER calls
order_router.place() (or place_laddered(), or ib.placeOrder() directly) anywhere in
this file. There is no env var, no CLI flag, no code path in this file that flips it;
the only way PILOT_MODE ever becomes False is a deliberate future source edit by
Andrew, after reviewing enough pilot cycles — exactly as morning_execute_run.py's own
docstring states for its own PILOT_MODE. (Self-check performed before reporting this
stage done: grepping this file for "order_router.place" and "placeOrder" turns up zero
call sites — only the import of order_router for its private, non-transmitting helpers
_check_limit_price/_base_fields, and the bare module import so tests can monkeypatch
order_router.place and assert it is never invoked.) PILOT_MODE is the PRIMARY control;
the live-data connection's hardcoded read-only-ness (see "CONNECTION TARGET" above) is a
second, independent backstop underneath it, not a substitute for it.

THE ORDER GROUP (design already settled earlier this session — implemented faithfully
here, not re-derived; see calm-riding-hammock.md's "Design already settled" section)
----------------------------------------------------------------------------------------
For one picked spread (s8_strategy.pick_spread_by_credit -> SpreadPick):
  1. ENTRY — two separate per-leg orders that open the credit spread: SELL the short
     leg at its live bid, BUY the long leg at its live ask (the same "honest fill"
     convention s8_strategy.py's own credit search already uses — sell at bid, buy at
     ask, never mid).
  2. STOP (PARENT) — a StopOrder on the SHORT leg's own contract, BUY to close,
     triggered at s8_strategy.stop_price(realized_credit, stop_multiple) (S8_SPEC.md
     Sec 2.3's frozen formula). tif="GTC" so it rests server-side at IBKR — "every entry
     transmits its exit logic in the same breath ... no live monitoring loop decides
     exits" (the settled design).
  3. B2 CLOSE (CHILD) — a MarketOrder on the LONG leg's OWN (DIFFERENT) contract, SELL
     to close, `parentId` = the parent stop's orderId. IBKR's documented Hedging-order
     pattern: a parentId-attached child on a different contract is "submitted only on
     execution of the parent" — no synthetic price-mirroring needed. This is S8_SPEC.md
     Sec 3's "B2" rule: "close [the long leg] the instant its paired short leg stops
     out. No profit target, no timer, no discretion."
The parent's orderId is reserved via `ib.client.getReqId()` — the identical mechanism
ib_async's own `IB.bracketOrder()` uses internally to link parent/child WITHOUT
transmitting anything (a local sequence-counter reservation, not a network call).

Every order built here has `.transmit` forced False (order_router.py's own
`_base_fields` convention, reused not reinvented) and is never passed to
`ib.placeOrder`/`order_router.place` in this file — PILOT_MODE logs the group and moves
on.

Every cycle where something was due (whether or not it ultimately produced a pick)
persists via `ledger.record_run()` — due templates, each one's picked spread (or None +
why), the margin-preflight verdict, and the would-have-transmitted line. The pure
"nothing due" fast path deliberately does NOT write a ledger record (mirrors
morning_execute_run.py: the truly-empty common case gets zero side effects of any kind,
not just zero gateway contact).

Run (fires harmlessly and instantly on a day/minute nothing is due):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe ^
    "C:\\Users\\andre\\My Drive (andrew@surberhc.com)\\TradingDesk\\paperbot\\s8_runner.py"
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dt_time
from zoneinfo import ZoneInfo

import pandas as pd
from ib_async import LimitOrder, MarketOrder, Option, StopOrder

# =================================================================================
# PILOT MODE — flip to False ONLY after Andrew reviews N pilot cycles' emailed
# "WOULD HAVE TRANSMITTED" reports and explicitly decides to arm S8 entries. Nothing
# in this build flips this automatically; there is no env-var or CLI override. Defaults
# True, identical pattern to morning_execute_run.py's own PILOT_MODE.
# =================================================================================
PILOT_MODE = True

import ledger  # noqa: E402
import order_router  # noqa: E402
import s8_chain  # noqa: E402
import s8_config  # noqa: E402
import s8_risk  # noqa: E402
import s8_strategy  # noqa: E402
import version  # noqa: E402
from connections import ibkr_live_data  # noqa: E402
from order_router import _base_fields, _check_limit_price  # noqa: E402

# NOTE on gateway_lock: every OTHER paperbot script that imports gateway_lock does so
# because it operates the shared PAPER Gateway (127.0.0.1:4002) and needs the
# inter-process mutex documented in gateway_lock.py's own module docstring ("single-
# process mutex on the paper Gateway"). This file is the one exception, post the
# 2026-07-13 live-data pivot (see "CONNECTION TARGET" above): it never touches the paper
# Gateway at all, so wrapping its live-data work in that mutex would protect a resource
# this script doesn't use, while providing zero real protection on the resource it does
# use (the live-data Gateway, port 4001, which has no analogous cross-process lock built
# yet). Deliberately not imported/used here for that reason -- not an oversight.

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "dailyreport"))
import mailer  # noqa: E402

# --- Due-check ---------------------------------------------------------------------
CT_ZONE = ZoneInfo("America/Chicago")

# See the module docstring's "TOLERANCE WINDOW" section for the arithmetic
# justification (5-minute observed floor between any two of one template's own grid
# slots; a +/-2min half-window stays strictly inside that with margin to spare).
DUE_TOLERANCE_MINUTES = 2

# Pilot default: 1 contract per entry. NOT a strategy knob (S8's real position-sizing
# decision has not been made yet, independent of the account-allocation decision
# s8_config.ACCOUNT now records); this is simply the smallest sane unit for a
# PILOT_MODE dry run that never transmits anything regardless of its value.
QTY_PER_ENTRY = 1

# Bounded retry — identical policy to morning_execute_run.py's own bounded_connect.
CONNECT_MAX_ATTEMPTS = 3
CONNECT_ATTEMPT_TIMEOUT_SECS = 120
CONNECT_RETRY_BACKOFF_SECS = 90


def current_ct_time() -> dt_time:
    """Real wall-clock time right now, converted to US/Central via zoneinfo (DST-
    correct, independent of whatever local timezone the host machine happens to be set
    to) — the same approach already verified live in
    connections/gateway_watchdog.py's own timezone handling."""
    return datetime.now(tz=CT_ZONE).time()


def due_templates(now: dt_time) -> list[tuple[str, str]]:
    """PURE, offline-testable: [(template_name, matched_slot_HHMM), ...] for every S8
    template whose s8_config.ENTRY_GRID_CT has a slot within DUE_TOLERANCE_MINUTES of
    `now` (a plain datetime.time, already assumed to be CT wall-clock — no IBKR, no
    gateway contact, no real clock read inside this function).

    Templates whose ENTRY_GRID_CT is None are SKIPPED unconditionally and can never
    appear in the result at any time of day — see s8_config.py's own comment: these are
    the templates whose real MATCHED-fills sample was too thin (n < 3 at any single
    slot) to name a grid at all; "consumers ... must treat None as 'do not fire this
    template on a schedule yet'."
    """
    now_min = now.hour * 60 + now.minute
    due: list[tuple[str, str]] = []
    for name, grid in s8_config.ENTRY_GRID_CT.items():
        if not grid:
            continue
        for slot in grid:
            h, m = (int(x) for x in slot.split(":"))
            if abs((h * 60 + m) - now_min) <= DUE_TOLERANCE_MINUTES:
                due.append((name, slot))
                break  # one matched slot is enough to mark this template due
    return due


# --- Order-group construction (never transmitted; see module docstring) ------------
_SIDE_TO_OPT_RIGHT = {"PUT": "P", "CALL": "C"}  # SpreadPick.side -> ib_async Option.right


@dataclass
class S8OrderGroup:
    template_name: str
    short_contract: Option
    long_contract: Option
    entry_short_order: object   # SELL qty LMT @ short_bid  -- opens the short leg (credit)
    entry_long_order: object    # BUY  qty LMT @ long_ask   -- opens the long leg (debit)
    stop_order: object          # BUY  qty STP @ stop_price -- PARENT: closes the short leg
    b2_close_order: object      # SELL qty MKT, parentId=stop_order.orderId -- CHILD
    qty: int
    stop_price: float


def _quote(chain_snap: pd.DataFrame, right: str, strike: float) -> tuple[float | None, float | None]:
    """(bid, ask) for one strike/right in a chain snapshot DataFrame, or (None, None) if
    that exact strike/right isn't present or its quotes are missing (None/NaN)."""
    row = chain_snap[(chain_snap["right"] == right) & (chain_snap["strike"] == strike)]
    if row.empty:
        return None, None
    r = row.iloc[0]
    bid = r["bid"]
    ask = r["ask"]
    bid = float(bid) if (bid is not None and bid == bid) else None   # NaN != NaN
    ask = float(ask) if (ask is not None and ask == ask) else None
    return bid, ask


def build_entry_order_group(ib, chain_snap: pd.DataFrame, pick, template_config: dict,
                            account: str, qty: int) -> S8OrderGroup:
    """Construct (NEVER place) the full S8 order group for one picked spread. See the
    module docstring's "THE ORDER GROUP" section for the design (settled earlier this
    session, implemented faithfully here). Every order's `.transmit` stays False; this
    function never calls ib.placeOrder or order_router.place — it only builds objects.
    """
    right = _SIDE_TO_OPT_RIGHT[pick.side]
    expiration = chain_snap.attrs.get("expiration")
    if not expiration:
        raise RuntimeError(
            "chain_snap.attrs['expiration'] is missing -- cannot build option contracts "
            "(see s8_chain.snapshot_0dte_chain's docstring: this attr is always set on a "
            "real snapshot; its absence here means the snapshot object was malformed).")

    short_contract = Option("SPX", expiration, pick.short_strike, right, "SMART",
                            tradingClass=s8_chain._SPXW_TRADING_CLASS, currency="USD")
    long_contract = Option("SPX", expiration, pick.long_strike, right, "SMART",
                           tradingClass=s8_chain._SPXW_TRADING_CLASS, currency="USD")

    short_bid, _short_ask = _quote(chain_snap, pick.side, pick.short_strike)
    _long_bid, long_ask = _quote(chain_snap, pick.side, pick.long_strike)
    if short_bid is None or long_ask is None:
        raise RuntimeError(
            f"could not re-derive per-leg quotes from the chain snapshot for "
            f"{pick.template_name} (short={pick.short_strike}, long={pick.long_strike}) "
            f"-- refusing to build an order at a missing/NaN price.")
    short_bid = _check_limit_price(f"SPXW {pick.short_strike:g}{right}", short_bid)
    long_ask = _check_limit_price(f"SPXW {pick.long_strike:g}{right}", long_ask)

    order_ref = f"paperbot_s8:{account}:{expiration}:{pick.template_name}"

    entry_short = LimitOrder("SELL", qty, short_bid)     # honest fill: sell short at bid
    _base_fields(entry_short, account, None, "", order_ref + ":short_entry")
    entry_long = LimitOrder("BUY", qty, long_ask)        # honest fill: buy long at ask
    _base_fields(entry_long, account, None, "", order_ref + ":long_entry")

    stop_px = _check_limit_price(
        f"SPXW {pick.short_strike:g}{right} stop",
        s8_strategy.stop_price(pick.realized_credit, template_config["stop_multiple"]))
    # ib.client.getReqId() reserves a local orderId WITHOUT transmitting anything -- the
    # identical mechanism ib_async's own IB.bracketOrder() uses to link parent/child.
    stop_order = StopOrder("BUY", qty, stop_px, orderId=ib.client.getReqId())
    _base_fields(stop_order, account, None, "", order_ref + ":stop_parent")
    stop_order.tif = "GTC"   # resting server-side (the settled design: no monitoring loop)

    b2_child = MarketOrder("SELL", qty, parentId=stop_order.orderId)
    _base_fields(b2_child, account, None, "", order_ref + ":b2_child")
    b2_child.tif = "DAY"     # 0DTE settles same day; fires the instant the parent fills

    return S8OrderGroup(
        template_name=pick.template_name, short_contract=short_contract,
        long_contract=long_contract, entry_short_order=entry_short,
        entry_long_order=entry_long, stop_order=stop_order, b2_close_order=b2_child,
        qty=qty, stop_price=stop_px)


# --- Connect / lock / alert plumbing (mirrors morning_execute_run.py) ---------------
def _alert_email(subject: str, lines: list[str]) -> None:
    html = "<html><body><pre>" + "\n".join(lines) + "</pre></body></html>"
    try:
        mailer.send_html(f"[TradingDesk PAPER] {subject}", html)
    except Exception as exc:
        print(f"    ! alert email itself failed: {exc}")


def bounded_connect(consumer: str):
    """Identical bounded-retry policy to morning_execute_run.bounded_connect, repointed
    at the LIVE-DATA Gateway (connections.ibkr_live_data, port 4001) instead of the paper
    Gateway -- see the module docstring's "CONNECTION TARGET" section. Duplicated (not
    imported) on purpose — same rationale stated there: each scheduled script must be
    independently robust and self-contained, and the policy is tiny/stable enough that a
    shared import would be over-engineering for these call sites.

    No `readonly` argument here (unlike morning_execute_run.bounded_connect's paper-side
    equivalent): `ibkr_live_data.connect()` has no readonly parameter at all -- every
    connection it makes is hardcoded read-only, so there is nothing for this function to
    pass through or toggle."""
    last_exc: Exception | None = None
    for attempt in range(1, CONNECT_MAX_ATTEMPTS + 1):
        print(f"    connect attempt {attempt}/{CONNECT_MAX_ATTEMPTS} "
              f"(consumer={consumer}, live-data Gateway, hardcoded read-only)...")
        try:
            ib = ibkr_live_data.connect(consumer, launch=True,
                                        timeout=CONNECT_ATTEMPT_TIMEOUT_SECS)
            print(f"    connected on attempt {attempt}.")
            return ib
        except Exception as exc:
            last_exc = exc
            print(f"    attempt {attempt} failed: {type(exc).__name__}: {exc}")
            if attempt < CONNECT_MAX_ATTEMPTS:
                print(f"    backing off {CONNECT_RETRY_BACKOFF_SECS}s before retrying...")
                time.sleep(CONNECT_RETRY_BACKOFF_SECS)
    print(f"    GIVING UP after {CONNECT_MAX_ATTEMPTS} attempts.")
    return None


def main() -> int:
    print("=" * 100)
    print(f"S8 RUNNER (British IC + B2 long-leg auto-close)   PILOT_MODE={PILOT_MODE}   "
          f"[{version.banner()}]")
    print("=" * 100)

    # ACCOUNT == "TBD": loud, deliberate refusal -- NOT the same as the due-check
    # fast path below (see module docstring's now-updated "ACCOUNT == TBD REFUSAL"
    # section: s8_config.ACCOUNT is decided ("DU8922146") and this check no longer
    # gates a paper-account connection -- this runner's Gateway connection doesn't
    # touch any paper sub-account at all. Left in place as a cheap belt-and-suspenders
    # check in case ACCOUNT is ever reset, not because it protects today's connection.
    if s8_config.ACCOUNT == "TBD":
        msg = ("S8 SAFETY STOP: s8_config.ACCOUNT is still the placeholder \"TBD\" -- "
              "no account is on record for S8's future paper/live transmission path "
              "(see s8_config.py's own comment and conductor/ACCOUNT_ALLOCATION.md). "
              "REFUSING to proceed until s8_config.ACCOUNT is updated -- this is a LOUD, "
              "deliberate no-op, not a silent skip. (Note: this connection itself never "
              "touches a paper sub-account regardless -- see the module docstring's "
              "\"CONNECTION TARGET\" section -- but a decided ACCOUNT is still required "
              "for honest provenance/logging.)")
        print(f"\n{msg}")
        return 2

    # DUE-CHECK FAST PATH: zero gateway contact if nothing is due right now.
    now = current_ct_time()
    due = due_templates(now)
    if not due:
        print(f"\nNo S8 template due right now ({now.strftime('%H:%M')} CT, "
              f"+/-{DUE_TOLERANCE_MINUTES}min tolerance) -- nothing to do. "
              f"ZERO gateway contact.")
        return 0

    print(f"\nDue this cycle ({now.strftime('%H:%M')} CT): "
          + ", ".join(f"{n}@{s}" for n, s in due))

    # PILOT_MODE never needs write access to the account; the live-data Gateway is
    # hardcoded read-only regardless (see bounded_connect's docstring / module
    # docstring's "CONNECTION TARGET" section) -- there is no readonly toggle to pass.
    ib = bounded_connect("paperbot_s8_livedata")
    if ib is None:
        msg = (f"S8 runner: could not connect to the live-data Gateway after "
              f"{CONNECT_MAX_ATTEMPTS} attempts. Skipping this cycle; the next "
              f"scheduled fire will retry.")
        print(f"\n{msg}")
        _alert_email("S8 runner: gateway connect FAILED", [msg])
        return 1

    try:
        return _do_work(ib, due)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
        print("S8 session closed (connection disconnected).")


# NOTE: no gateway_lock() wrapping here (unlike every other paperbot runner) -- see the
# import-site comment above and the module docstring's "CONNECTION TARGET" section: this
# file never operates the shared PAPER Gateway, so that mutex is not applicable to it.


def _do_work(ib, due: list[tuple[str, str]]) -> int:
    account = s8_config.ACCOUNT  # provenance/logging only (order_ref, ledger record) --
    # see the module docstring's "ACCOUNT == TBD REFUSAL" section. NOT passed to
    # accountSummary() below: the live-data connection has no concept of this (or any)
    # paper DU sub-account, so filtering by it would be meaningless on this connection.

    print("\n[1] Reading account summary from the live-data connection "
          f"(exactly one real personal account is visible here; s8_config.ACCOUNT="
          f"{account!r} is informational-only provenance, not a filter on this call)...")
    try:
        summary = ib.accountSummary()
    except Exception as exc:
        msg = f"S8 runner: could not read accountSummary() from the live-data connection: {exc}"
        print(f"    {msg}")
        _alert_email("S8 runner: accountSummary FAILED", [msg])
        return 1

    print("\n[2] Snapshotting today's live 0DTE SPXW chain...")
    try:
        chain_snap = s8_chain.snapshot_0dte_chain(ib)
    except Exception as exc:
        msg = f"S8 runner: chain snapshot FAILED: {type(exc).__name__}: {exc}"
        print(f"    {msg}")
        _alert_email("S8 runner: chain snapshot FAILED", [msg])
        ledger.record_run({
            "mode": "s8_runner_pilot", "account": account,
            "due_templates": [n for n, _ in due], "n_intents": len(due),
            "n_approved": 0, "n_transmitted": 0, "halted": True,
            "error": f"chain snapshot failed: {exc}",
        })
        return 1

    results: list[dict] = []
    would_lines = ["PILOT MODE -- nothing was transmitted. This is what WOULD have "
                  "been sent if PILOT_MODE were False:", ""]
    n_approved = 0

    print(f"\n[3] Evaluating {len(due)} due template(s)...")
    for template_name, slot in due:
        cfg = s8_config.TEMPLATES[template_name]
        outcome: dict = {"template": template_name, "slot": slot}

        try:
            pick = s8_strategy.pick_spread_by_credit(
                chain_snap, template_name, cfg,
                spot=chain_snap.attrs.get("spot"),
                expiration=chain_snap.attrs.get("expiration"))
        except Exception as exc:
            outcome["error"] = f"pick_spread_by_credit raised: {type(exc).__name__}: {exc}"
            print(f"    [{template_name}@{slot}] SKIP -- {outcome['error']}")
            results.append(outcome)
            continue

        if pick is None:
            outcome["pick"] = None
            outcome["reason"] = "no viable spread within tolerance of target credit"
            print(f"    [{template_name}@{slot}] SKIP -- {outcome['reason']}")
            results.append(outcome)
            continue

        outcome["pick"] = {
            "short_strike": pick.short_strike, "long_strike": pick.long_strike,
            "width": pick.width, "realized_credit": pick.realized_credit,
            "short_delta": pick.short_delta, "delta_note": pick.delta_note,
        }

        preflight = s8_risk.margin_preflight(
            summary, width_points=pick.width, realized_credit=pick.realized_credit,
            qty=QTY_PER_ENTRY)
        outcome["preflight"] = {"ok": preflight.ok, "reasons": preflight.reasons,
                               "required_notional": preflight.required_notional}
        if not preflight.ok:
            outcome["reason"] = "margin preflight REFUSED"
            print(f"    [{template_name}@{slot}] SKIP -- margin preflight REFUSED: "
                  f"{'; '.join(preflight.reasons)}")
            results.append(outcome)
            continue

        try:
            group = build_entry_order_group(ib, chain_snap, pick, cfg, account,
                                            QTY_PER_ENTRY)
        except Exception as exc:
            outcome["error"] = f"order-group construction FAILED: {type(exc).__name__}: {exc}"
            print(f"    [{template_name}@{slot}] SKIP -- {outcome['error']}")
            results.append(outcome)
            continue

        # PILOT_MODE: log/email only. order_router.place() / ib.placeOrder() are NEVER
        # called anywhere in this function (see module docstring's self-check).
        line = (f"WOULD HAVE TRANSMITTED: S8 entry {template_name} "
               f"short={pick.short_strike:g}/long={pick.long_strike:g} "
               f"qty={QTY_PER_ENTRY} stop={group.stop_price:.2f} + B2 child")
        print(f"    [{template_name}@{slot}] {line}")
        would_lines.append(f"  {line}")
        outcome["would_transmit"] = line
        n_approved += 1
        results.append(outcome)

    ledger.record_run({
        "mode": "s8_runner_pilot", "account": account,
        "due_templates": [n for n, _ in due], "n_intents": len(due),
        "n_approved": n_approved, "n_transmitted": 0, "halted": False,
        "results": results,
    })

    if n_approved:
        _alert_email(
            f"S8 runner PILOT: {n_approved} entr{'y' if n_approved == 1 else 'ies'} "
            f"would have transmitted", would_lines)

    print(f"\nDone. {len(due)} template(s) due, {n_approved} would-have-transmitted "
         f"(PILOT_MODE={PILOT_MODE}, nothing actually sent).")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
