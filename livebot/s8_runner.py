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

CONNECTION TARGET — THE LIVE-TRADING GATEWAY, NEVER THE PAPER GATEWAY
------------------------------------------------------------------------------------------
This file connects EXCLUSIVELY to the live-TRADING Gateway (`connections.ibkr_live_trade`,
port 4003) for BOTH steps of its monitoring/decision cycle — the account-summary read
for the margin gate AND the option-chain snapshot for strike selection. That is a real,
FUNDED, transmit-CAPABLE account limited to two individual test accounts — NOT the paper
Gateway (`connections.ibkr_paper`, port 4002) and NOT the earlier port-4001 live-DATA login
(`connections.ibkr_live_data`), neither of which this file imports or calls anywhere.
The live-trading account serves real-time market data directly, so no delayed-data
fallback (`reqMarketDataType`) is needed for the quotes S8 reads.

Zero-transmit is currently STRUCTURAL: there is NO transmit code path in this file at
all. `order_router.place()` / `place_laddered()` / `ib.placeOrder()` are never called
anywhere here — the runner only ever logs/emails "WOULD HAVE TRANSMITTED: ...". Two
declared walls sit on top of that absence:

  1. PRIMARY: `PILOT_MODE = True` (hardcoded below). It is the declared primary control
     and becomes the operative gate the day the future S8 executor (reserved clientId
     `paperbot_s8_exec` = 50) is built with a real transmit path. Today, with no transmit
     code in this file, PILOT_MODE is belt-and-suspenders over that absence — but it is
     still the wall of record, not the read-only default.
  2. SECONDARY fail-safe: the connection is read-only by default
     (`ibkr_live_trade.connect(readonly=True)` — this file never passes `readonly=False`).
     Because the account IS transmit-capable at the broker level, read-only is a real,
     honored session flag here (unlike the port-4001 live-data login, whose read-only-ness
     was structural). It is a fail-safe default, not a substitute for PILOT_MODE.

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

ACCOUNT == "TBD" REFUSAL (loud, not silent) — GENUINELY ACTIVE, NOT VESTIGIAL
------------------------------------------------------------------------------------------
s8_config.ACCOUNT is reset to the "TBD" placeholder: the S8 live-trading TEST account
(one of the two individual accounts under the new live login) has NOT yet been provided
by Andrew. Until it is, this check fires for real — the runner refuses loudly (returns 2)
BEFORE any gateway contact. This is not a hypothetical/belt-and-suspenders path any more;
it is the operative fail-closed guard that keeps the runner from doing anything at all
until a real test account number is set. Once Andrew supplies the account, s8_config.ACCOUNT
is updated to it and this refusal stops firing; the account string is then threaded into
`order_ref` and the ledger record below as honest provenance for the cycle's decisions.

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
order_router.place and assert it is never invoked.) PILOT_MODE is the declared PRIMARY
control (operative the day a transmit path exists); the live-trading connection's
read-only-by-default session (see "CONNECTION TARGET" above) is a second, independent
fail-safe underneath it, not a substitute for it.

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
    "C:\\TradingDesk\\livebot\\s8_runner.py"
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

# ledger.py, order_router.py, version.py stay in paperbot/ (this module was relocated
# to livebot/ in the S8-package split); add paperbot/ to sys.path so the bare
# `import ledger` / `import order_router` / `import version` below still resolve. The
# s8_* siblings moved WITH this file into livebot/, so they resolve without help.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "paperbot"))

# connections/ and strategies/ are separate top-level packages installed editable into
# the venv. After the 2026-07-16 move off Drive the editable installs still point at the
# old (now-deleted) My Drive location, so `from connections import ...` fails from a clean
# venv invocation -- which would break this scheduled runner. Rather than depend on that
# install being regenerated, add the repo's own connections/ and strategies/ parents to
# sys.path (derived from __file__, per CLAUDE.md) so this runner is self-contained --
# same pattern already used above for paperbot/ and below for dailyreport/. Fixing the
# venv editable install desk-wide is a SEPARATE matter; this makes S8 independent of it.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _pkg_parent in ("connections", "strategies"):
    _p = os.path.join(_REPO_ROOT, _pkg_parent)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ledger  # noqa: E402
import order_router  # noqa: E402
import s8_capture  # noqa: E402
import s8_chain  # noqa: E402
import s8_config  # noqa: E402
import s8_risk  # noqa: E402
import s8_strategy  # noqa: E402
import version  # noqa: E402
from connections import ibkr_live_trade  # noqa: E402
from order_router import _base_fields, _check_limit_price  # noqa: E402

# NOTE on gateway_lock: every OTHER paperbot script that imports gateway_lock does so
# because it operates the shared PAPER Gateway (127.0.0.1:4002) and needs the
# inter-process mutex documented in gateway_lock.py's own module docstring ("single-
# process mutex on the paper Gateway"). This file is the one exception (see "CONNECTION
# TARGET" above): it never touches the paper Gateway at all, so wrapping its live-trading
# work in that mutex would protect a resource this script doesn't use, while providing
# zero real protection on the resource it does use (the live-TRADING Gateway,
# connections.ibkr_live_trade, port 4003, whose own launch coordination lives in that module's
# ensure_gateway() launch mutex). Deliberately not imported/used here -- not an oversight.

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

    order_ref = f"s8_live:{account}:{expiration}:{pick.template_name}"

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
        mailer.send_html(f"[TradingDesk S8 LIVE] {subject}", html)
    except Exception as exc:
        print(f"    ! alert email itself failed: {exc}")


def bounded_connect(consumer: str):
    """Identical bounded-retry policy to morning_execute_run.bounded_connect, repointed
    at the live-TRADING Gateway (connections.ibkr_live_trade, port 4003) instead of the paper
    Gateway -- see the module docstring's "CONNECTION TARGET" section. Duplicated (not
    imported) on purpose — same rationale stated there: each scheduled script must be
    independently robust and self-contained, and the policy is tiny/stable enough that a
    shared import would be over-engineering for these call sites.

    Connects read-only by DEFAULT: `ibkr_live_trade.connect()` exposes a real `readonly`
    parameter (this account is transmit-capable at the broker level), but it defaults to
    True and this function never passes readonly=False. The pilot only ever reads, so a
    read-only session is a fail-safe that costs it nothing -- PILOT_MODE, not this
    default, is the primary zero-transmit wall."""
    last_exc: Exception | None = None
    for attempt in range(1, CONNECT_MAX_ATTEMPTS + 1):
        print(f"    connect attempt {attempt}/{CONNECT_MAX_ATTEMPTS} "
              f"(consumer={consumer}, live-trading Gateway, read-only by default)...")
        try:
            ib = ibkr_live_trade.connect(consumer, launch=True,
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

    # ACCOUNT == "TBD": loud, deliberate refusal -- GENUINELY ACTIVE (see module
    # docstring's "ACCOUNT == TBD REFUSAL" section). s8_config.ACCOUNT is the "TBD"
    # placeholder because the S8 live-trading TEST account has not been provided yet,
    # so this gate fires for real and refuses BEFORE any gateway contact -- it is the
    # fail-closed guard, not a vestigial check.
    if s8_config.ACCOUNT == "TBD":
        msg = ("S8 SAFETY STOP: s8_config.ACCOUNT is still the placeholder \"TBD\" -- "
              "no live-trading TEST account has been provided for S8 yet "
              "(see s8_config.py's own comment). "
              "REFUSING to proceed until s8_config.ACCOUNT is updated to a real test "
              "account -- this is a LOUD, deliberate no-op, not a silent skip, and it "
              "fires before any Gateway contact.")
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

    # PILOT_MODE never needs write access to the account; bounded_connect uses
    # ibkr_live_trade.connect()'s read-only default (see bounded_connect's docstring / module
    # docstring's "CONNECTION TARGET" section) -- readonly=False is never passed here.
    ib = bounded_connect("s8_live_pilot")
    if ib is None:
        msg = (f"S8 runner: could not connect to the live-trading Gateway after "
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


def filter_account_summary(summary, account: str):
    """Keep only the accountSummary rows for `account`.

    The live-trade login exposes MORE THAN ONE managed account (e.g. a trust + an
    individual test account), so `ib.accountSummary()` returns rows for every account
    under the login PLUS an aggregate 'All' scope. s8_risk._summary_map collapses rows
    last-write-wins, so handing it the unfiltered blend would let the margin preflight
    read the WRONG account's AccountType/BuyingPower/NetLiquidation. Filtering to the
    target account first makes the preflight deterministic and correct.

    A dict {tag: value} already represents a single account's summary (the offline-test
    shape, same dual-shape convention as s8_risk._summary_map) and is returned unchanged.
    A live accountSummary() list is filtered to the rows whose `.account` matches.
    """
    if isinstance(summary, dict):
        return summary
    return [r for r in summary if getattr(r, "account", None) == account]


def evaluate_and_capture_due_template(ib, chain_snap, summary, template_name: str,
                                      slot: str, account: str,
                                      qty: int = QTY_PER_ENTRY) -> dict:
    """Evaluate ONE due (template, slot) end to end and return its outcome dict.

    This is the SINGLE SHARED entry code path used by BOTH s8_runner._do_work (the
    scheduled standalone runner) AND livebot/s8_service.py (the unified all-day service),
    so the per-template decision logic exists in exactly one place and can never drift
    between the two. It performs, in order:

      1. s8_strategy.pick_spread_by_credit  — the FROZEN pick (rule #1; never re-derived)
      2. s8_risk.margin_preflight           — margin gate on the (filtered) account summary
      3. build_entry_order_group            — builds (NEVER transmits) the entry/stop/B2 group
      4. the "WOULD HAVE TRANSMITTED" log line
      5. s8_capture.capture_and_persist_entry — rich open-TradeRecord capture (best-effort)

    ZERO-TRANSMIT: like build_entry_order_group, this only ever BUILDS the order group and
    LOGS the would-have line — order_router.place() / ib.placeOrder() are NEVER called here.

    Returns an outcome dict shaped exactly as _do_work's per-template results always were:
      {"template", "slot"} always; then one of:
        {"error": ...}                              (pick raised / order-group build failed)
        {"pick": None, "reason": ...}               (no viable spread)
        {"pick": {...}, "preflight": {...}, "reason": "margin preflight REFUSED"}
        {"pick": {...}, "preflight": {...}, "would_transmit": <line>, "trade_id": <id|None>}
    An approved entry is exactly the outcome carrying a "would_transmit" line.
    """
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
        return outcome

    if pick is None:
        outcome["pick"] = None
        outcome["reason"] = "no viable spread within tolerance of target credit"
        print(f"    [{template_name}@{slot}] SKIP -- {outcome['reason']}")
        return outcome

    outcome["pick"] = {
        "short_strike": pick.short_strike, "long_strike": pick.long_strike,
        "width": pick.width, "realized_credit": pick.realized_credit,
        "short_delta": pick.short_delta, "delta_note": pick.delta_note,
    }

    preflight = s8_risk.margin_preflight(
        summary, width_points=pick.width, realized_credit=pick.realized_credit,
        qty=qty)
    outcome["preflight"] = {"ok": preflight.ok, "reasons": preflight.reasons,
                            "required_notional": preflight.required_notional}
    if not preflight.ok:
        outcome["reason"] = "margin preflight REFUSED"
        print(f"    [{template_name}@{slot}] SKIP -- margin preflight REFUSED: "
              f"{'; '.join(preflight.reasons)}")
        return outcome

    try:
        group = build_entry_order_group(ib, chain_snap, pick, cfg, account, qty)
    except Exception as exc:
        outcome["error"] = f"order-group construction FAILED: {type(exc).__name__}: {exc}"
        print(f"    [{template_name}@{slot}] SKIP -- {outcome['error']}")
        return outcome

    # PILOT_MODE: log/email only. order_router.place() / ib.placeOrder() are NEVER
    # called anywhere in this function (see module docstring's self-check).
    line = (f"WOULD HAVE TRANSMITTED: S8 entry {template_name} "
            f"short={pick.short_strike:g}/long={pick.long_strike:g} "
            f"qty={qty} stop={group.stop_price:.2f} + B2 child")
    print(f"    [{template_name}@{slot}] {line}")
    outcome["would_transmit"] = line

    # RICH ENTRY CAPTURE (Phase 1, observation-only): grab both legs live
    # (quotes + model greeks/IV) + spot + VIX at the entry instant, assemble an
    # "open" TradeRecord and persist it via s8_store. This is a pure data-capture
    # side effect that NEVER transmits and — being best-effort by contract — must
    # NEVER break the pilot cycle. Wrapped defensively here too (belt-and-suspenders
    # over capture_and_persist_entry's own internal try/except): a capture failure
    # logs a warning and the cycle proceeds exactly as before.
    try:
        # `slot` (this function's own parameter) is the ENTRY_GRID_CT label of the slot
        # being entered. It MUST be passed through: it becomes TradeRecord.slot / the
        # trade_id, and s8_service.slot_already_entered looks up exactly that label. When
        # the capture derived the slot from its own wall clock instead, the guard never
        # matched inside the +/-2min tolerance window and the same grid slot was re-entered
        # every cycle (observed live 2026-07-20).
        trade_id = s8_capture.capture_and_persist_entry(
            ib, pick, cfg, account, qty, chain_snap, group.stop_price, slot=slot)
        outcome["trade_id"] = trade_id
    except Exception as exc:
        print(f"    [{template_name}@{slot}] ! entry capture raised "
              f"({type(exc).__name__}: {exc}); pilot cycle continues")
        outcome["trade_id"] = None

    return outcome


def _do_work(ib, due: list[tuple[str, str]]) -> int:
    account = s8_config.ACCOUNT  # provenance for order_ref/ledger AND the summary filter
    # below. The live-trade login exposes MORE THAN ONE managed account, so the account
    # summary MUST be filtered to this account before the margin preflight (see
    # filter_account_summary). An earlier version assumed a single account under the login
    # and did NOT filter, which let the preflight read the wrong account's numbers.

    print("\n[1] Reading account summary from the live-trading connection and filtering "
          f"to s8_config.ACCOUNT={account!r} (the live-trade login exposes more than one "
          f"account, so this filter is REQUIRED -- see filter_account_summary)...")
    try:
        summary_all = ib.accountSummary()
    except Exception as exc:
        msg = f"S8 runner: could not read accountSummary() from the live-trading connection: {exc}"
        print(f"    {msg}")
        _alert_email("S8 runner: accountSummary FAILED", [msg])
        return 1

    summary = filter_account_summary(summary_all, account)
    if not summary:
        seen = sorted(str(a) for a in {getattr(r, "account", None) for r in summary_all}
                      if a is not None)
        msg = (f"S8 runner: target account {account!r} not found under the live-trade "
               f"login (accounts seen: {seen}) -- REFUSING this cycle. Check "
               f"s8_config.ACCOUNT against the login's managed accounts.")
        print(f"    {msg}")
        _alert_email("S8 runner: target account not found", [msg])
        ledger.record_run({
            "mode": "s8_live_pilot", "account": account,
            "due_templates": [n for n, _ in due], "n_intents": len(due),
            "n_approved": 0, "n_transmitted": 0, "halted": True,
            "error": f"target account {account!r} not found under login; accounts seen: {seen}",
        })
        return 1

    print("\n[2] Snapshotting today's live 0DTE SPXW chain...")
    try:
        chain_snap = s8_chain.snapshot_0dte_chain(ib)
    except Exception as exc:
        msg = f"S8 runner: chain snapshot FAILED: {type(exc).__name__}: {exc}"
        print(f"    {msg}")
        _alert_email("S8 runner: chain snapshot FAILED", [msg])
        ledger.record_run({
            "mode": "s8_live_pilot", "account": account,
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
        # The per-template decision logic now lives in evaluate_and_capture_due_template
        # (shared verbatim with livebot/s8_service.py — one code path, no drift). This
        # loop only accumulates the cycle-level ledger/email aggregates around it.
        outcome = evaluate_and_capture_due_template(
            ib, chain_snap, summary, template_name, slot, account)
        results.append(outcome)
        if outcome.get("would_transmit"):
            would_lines.append(f"  {outcome['would_transmit']}")
            n_approved += 1

    ledger.record_run({
        "mode": "s8_live_pilot", "account": account,
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
