"""
s0_live_deploy.py — the S0 GROWTH-tier full-account DEPLOY executor (real-transmission,
explicit --conform mode).

WHAT THIS IS
------------
The tiny-test executor (s0_live_exec.py) proves the review -> arm -> transmit path with ONE
1-share order. This is its DEPLOY sibling: it takes a funded account holding ANYTHING and
rebalances it FULLY into the S0 GROWTH target — including SELLING holdings that are outside
the S0 universe (a corp-action / manual position the ongoing rebalancer deliberately leaves
alone). It is pinned to the trust account U14438624 on the Live-Trade Gateway (port 4003),
the same account + gateway the tiny-test uses, and it reuses every one of the tiny-test's
safety idioms verbatim.

2026-07-30 EXTRACTED TO THE SHARED SAFE-EXECUTION ENGINE (conductor #64/#66, spec §2/§7)
----------------------------------------------------------------------------------------
This file is now a THIN CALLER. Every proven execution guarantee below — the ordered leg
build, the full fail-closed pre-flight gate, the two-phase cash-gated transmit, the straggler
re-price, the per-run ref, the gateway read-only probe, the notional caps, the in-process
READONLY/DRY_RUN flip-and-restore-in-finally — was MOVED VERBATIM (not rewritten) into the
shared engine `safe_execute.execute_plan`. This module keeps only what is caller-specific:
the S0/GROWTH/U14438624 pinning, the connect lane choice, the account/positions/quotes read,
the plan sizing, the safety banner, and the CLI. It builds an ExecutionRequest and delegates
the gate + legs + two-phase transmit to the engine. Behavior is byte-for-byte identical; the
moved names are re-exported below so existing imports/tests keep working unchanged.

2026-07-28 INCIDENT -> REBUILT SAFER (conductor #63 / log #145)
---------------------------------------------------------------
The first live deploy on U14438624 left the account ~$40k NEGATIVE. Root cause was in THIS
executor's funding sequence: it transmitted ALL 15 legs at once — buying against the cash the
sells were EXPECTED to raise, WITHOUT waiting for the sells to fill. One sell (BUCK ~$40k)
CANCELLED (thin/stale quote, no re-price), so that cash never landed -> ~$115k of buys were
committed against ~$76k of real cash -> the account went negative and had to be hand-traded
out. Two other flaws surfaced: (a) a cancelled straggler was left as a hole instead of being
re-priced/chased, and (b) a legitimate re-buy of a manually-sold symbol was dedup-BLOCKED
because the ref was keyed on the monthly as_of date, so a bought-then-sold symbol still looked
"already done". The account was also still inside IBKR model "Main" (98.8% allocated) which our
sub-account read-only view CANNOT see. This rebuild fixes all four:

  1. TWO-PHASE, CASH-GATED execution. Phase 1 transmits ONLY the sells (plan sells + conform
     ALIEN liquidations) and WAITS for them to reach a terminal state. Between phases it
     RE-READS the account's live TotalCashValue (realized cash, never the plan's expected
     proceeds). Phase 2 sizes the buys to that ACTUAL cash — total BUY notional is held
     <= available cash minus a small safety buffer, scaling down / skipping whole-share buys
     if a sell fell short. Buying against money that has not arrived is now structurally
     impossible.
  2. RE-PRICE / chase stragglers. Any leg that has not filled within REPRICE_AFTER_SEC is
     cancelled and re-placed at a MORE aggressive marketable limit (toward the far touch),
     up to REPRICE_MAX_ATTEMPTS. A leg that still will not fill is left cancelled and reported
     LOUDLY — never silently dropped.
  3. PER-RUN order ref. The deploy ref now carries a per-run stamp, so a fresh run's legs can
     never match a prior run's fills. Correctness comes from the ENGINE's delta-vs-current-
     positions (plan.orders already reflects what is held), NOT from historical-fill dedup.
     Double-submit protection is kept by a symbol+side check against currently WORKING/open
     orders — so a still-live identical order is not duplicated, but a re-fire to complete the
     remaining gap is allowed.

NOTE (2026-07-29): the 2026-07-28 rebuild also added a --model-clear affirmation gate and a
best-effort IBKR model-overlay detection. Both were REMOVED per the account owner's explicit
direction — he manages model divestment manually, and the check was unwanted friction. All
other deploy safety gates below are unchanged.

WHY A --conform MODE
--------------------
rebalance_engine.plan_account marks a held symbol that is NOT in the S0 universe ALIEN and
NEVER emits a sell for it (the correct default for an ongoing rebalance — a spinoff/rename is
not churned into a taxable round-trip). That default is WRONG for a deliberate one-time
deploy where the whole point is to conform ANY existing book to the target. So this executor
adds an EXPLICIT opt-in: with --conform, every ALIEN line becomes a full-liquidation SELL
(to 0). Without --conform, ALIEN holdings are left untouched and the preview lists them as
"would remain". The ALIEN guard is NOT removed — it is opted out of, deliberately, per run.

DEFAULT IS SAFE. With no flag it runs a PREVIEW: it sizes the plan, builds the full ordered
order list (sells first, then buys), prints exactly what WOULD be transmitted, and sends
nothing. To actually transmit, a human must line up ALL of:
  * the exact CLI token  --arm-i-understand  (sets armed=True; never defaulted/auto-set),
  * the explicit  --conform  flag  (this executor is a conform-deploy tool; transmit requires
    it — separate from the arm token, BOTH required),
  * NO kill-switch sentinel present,
  * the target account is EXACTLY U14438624 (any other refused — single-account wall),
  * every leg whole-share, priced, and through order_router's HARD price guard,
  * total BUY notional <= investable (never over-deploy / no margin), AND every order past
    the per-order fat-finger rail (BUY <= 2x the model's own target dollars for that symbol;
    SELL <= the shares actually held), and
  * the Gateway physically ARMED (Read-Only API toggle OFF — measured live with the
    zero-transmission cancel-a-fabricated-order probe).
Miss ANY one and the run is a preview that transmits nothing and prints WHY.

There is NO auto-arm, and nothing here is scheduled. A human runs it, reviews the preview,
arms the Gateway by hand, and re-runs with the tokens to fire the deploy.

Run — PREVIEW with the conform list (default; transmits nothing):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe s0_live_deploy.py --conform

Run — ARMED conform deploy (human-supervised; requires an armed Gateway + no kill switch):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe s0_live_deploy.py --conform \\
      --arm-i-understand
"""
from __future__ import annotations

import os
import sys

import live_quotes
import rebalance_engine
import s0_live
import strategy_target
import version
import safe_execute
# The shared safe-execution engine (conductor #64/#66). Every guarantee this file used to
# implement inline now lives here; s0_live_deploy builds an ExecutionRequest and delegates.
from safe_execute import (
    ExecutionCaps, ExecutionRequest, MODE_ARMED, MODE_PREVIEW,
    # Re-exported so existing imports/tests (and s0_live_deploy's own callers) keep working
    # unchanged — these MOVED to safe_execute but are the SAME function/constant objects.
    build_deploy_legs, _scale_buys_to_cash, _leg_cap, _more_aggressive_cap,
    _working_order_present, _transmit_phase, _report_phase, _probe_gateway_readonly,
    _buying_power, _total_cash_value, _buying_power_ok, _trade_done, _cum_filled,
    _deploy_ref, _run_id, DEPLOY_REF_TAG,
    MAX_ORDER_MODEL_MULTIPLE, PHASE_TERMINAL_TIMEOUT_SEC, REPRICE_AFTER_SEC,
    REPRICE_MAX_ATTEMPTS, POLL_SEC, CASH_SETTLE_SEC, CASH_SAFETY_BUFFER_PCT,
    _TERMINAL_STATUSES,
)
# Reuse, don't reimplement: the read-only pilot already has the NetLiq parse and the
# strategy-universe accessor (for ROTATE_OUT vs ALIEN classification). Importing it runs no
# broker connection at import time.
import s0_live_pilot_run as sp

# ----------------------------------------------------------------------------------------
# SAFETY CONSTANTS — enforced in CODE (in the engine's gate). The docstring is not the wall.
# ----------------------------------------------------------------------------------------
# The single account this DEPLOY executor may ever transmit on — the funded, PDT-clear trust
# account (same account the tiny-test targets). Any OTHER account is refused (single-account
# wall, identical in kind to s0_live_exec's).
EXEC_ACCOUNT = "U14438624"
ALLOWED_ACCOUNT = "U14438624"

# The risk tier to deploy. Andrew specified GROWTH (2026-07-28) — NOT the Balanced default.
# Growth: equity_allowance 1.00, tbill_floor 0.00, real-asset sleeve 0.20 (strategies/config
# CLIENT_VERSIONS). Passed explicitly to strategy_target.current_target(version=...) so the
# deploy can never silently fall back to the config.STRATEGY_VERSION (Balanced) default.
DEPLOY_VERSION = "Growth"

# The exact arm token — typed in full, no abbreviation, no default (mirrors
# rebalance_execute.ARM_TOKEN / s0_live_exec.ARM_TOKEN).
ARM_TOKEN = "--arm-i-understand"
# The explicit conform flag — turns ALIEN (non-S0) holdings into full-liquidation SELLs AND
# is a REQUIRED transmit gate (this executor is a conform-deploy tool; without it, transmit
# nothing). Separate from the arm token — BOTH are required to liquidate + transmit.
CONFORM_FLAG = "--conform"

# KILL SWITCH — same sentinel s0_live_exec / morning_execute_run honor. Mirrored as a literal
# so this module pulls in none of their module-level state; if the file exists (any content)
# this run is preview-only.
KILL_SWITCH = r"C:\TradingDesk-Local\AUTOTRADE_DISABLED"
# Register the exact sentinel path with the engine so the "KILL_SWITCH sentinel present (...)"
# block reason reads byte-for-byte identically to the pre-extraction message.
safe_execute.register_kill_switch_label(KILL_SWITCH)


def arm_requested(argv: list[str]) -> bool:
    """True ONLY if the exact arm token is present in argv — the single thing that sets
    armed=True. Mirrors s0_live_exec.arm_requested."""
    return ARM_TOKEN in argv


def conform_requested(argv: list[str]) -> bool:
    """True ONLY if the exact --conform flag is present. Turns ALIEN holdings into
    liquidation SELLs AND is a required transmit gate."""
    return CONFORM_FLAG in argv


def _kill_switch_present() -> bool:
    """True if the AUTOTRADE_DISABLED sentinel exists -> force preview-only."""
    return os.path.exists(KILL_SWITCH)


def _account_safety_ok() -> tuple[bool, str]:
    """Constant-level account guard: EXEC_ACCOUNT must be EXACTLY the single ALLOWED_ACCOUNT
    and no other. Read at call time so a test/monkeypatch of EXEC_ACCOUNT is honored. Hard
    single-account wall — identical in kind to s0_live_exec's. Thin wrapper over the engine's
    generalized account wall (safe_execute.account_wall_ok)."""
    return safe_execute.account_wall_ok(EXEC_ACCOUNT, [ALLOWED_ACCOUNT])


def _safety_banner(armed: bool, conform: bool, kill: bool) -> None:
    permit_intent = armed and conform and not kill
    print("\n" + "#" * 88)
    print(f"# SAFETY STATE   armed={armed}   arm_token={'present' if armed else 'absent'}   "
          f"conform={'ON' if conform else 'off'}   "
          f"kill_switch={'PRESENT' if kill else 'absent'}")
    print(f"# account={EXEC_ACCOUNT}   target=S0 {DEPLOY_VERSION} tier   "
          f"gateway=Live-Trade port 4003")
    print(f"#   (single-account wall: refuses ANY account other than {ALLOWED_ACCOUNT})")
    print(f"# RAILS  total BUY notional <= investable   per-order BUY <= "
          f"{MAX_ORDER_MODEL_MULTIPLE:g}x the model's target dollars for that symbol   "
          f"per-order SELL <= shares actually held   whole-share   price-guarded")
    print(f"# EXECUTION   two-phase cash-gated (sells -> re-read TotalCashValue -> buys sized "
          f"to REALIZED cash)   straggler re-price x{REPRICE_MAX_ATTEMPTS:.0f}")
    if permit_intent:
        print("# *** ARMED + CONFORM INTENT: this run MAY liquidate non-S0")
        print("#     holdings and transmit a TWO-PHASE cash-gated deploy on a FUNDED account IF")
        print("#     every remaining gate passes. Review the full order list below. ***")
    else:
        print("# PREVIEW: sizes + builds the full ordered order list and prints it —")
        print("# transmits NOTHING (not armed, conform off, or kill switch present).")
    print("#" * 88)


def main(armed: bool = False, conform: bool = False, today: object = None) -> int:
    """DEPLOY executor. PREVIEW by default; transmits the TWO-PHASE cash-gated deploy ONLY when
    armed AND conform AND every gate passes. `today` is accepted for signature parity with the
    other runners; the shared brain always runs to the most recent data date."""
    print("=" * 88)
    print(f"S0 LIVE DEPLOY EXECUTOR ({DEPLOY_VERSION} tier) — preview by default, two-phase "
          f"cash-gated conform deploy when armed   [{version.banner()}]")
    print("=" * 88)

    # [1] Compute the GROWTH target BEFORE connecting (fail fast on stale data; connect
    # nothing on failure). Explicit version=DEPLOY_VERSION — never the Balanced default.
    print(f"\n[1] Computing the S0 {DEPLOY_VERSION} target (shared brain; stale-data "
          f"guarded)...")
    try:
        target = strategy_target.current_target(version=DEPLOY_VERSION)
    except Exception as exc:
        print(f"    COULD NOT BUILD TARGET: {exc}. Nothing connected, nothing transmitted.")
        return 2
    print(f"    {target.version}   as_of={target.as_of.date()}  "
          f"price_date={target.price_date.date()}  ({len(target.weights)} holdings)")

    kill = _kill_switch_present()
    permit_intent = armed and conform and not kill

    # [2] Safety banner — armed/conform/preview state, account, caps.
    _safety_banner(armed, conform, kill)

    # [3] Connect. ARMED intent -> the transmit-capable lane (readonly=False, clientId
    # s0_live_exec); otherwise the read-only pilot lane (readonly=True). A bare armed connection
    # still transmits nothing on its own — only order_router.place(armed=True) does. Whole
    # session in try/finally so it ALWAYS disconnects.
    if permit_intent:
        print("\n[3] Connecting ARMED (readonly=False) to the Live-Trade Gateway "
              "(port 4003)...")
        try:
            ib = s0_live.connect_s0_live_armed()
        except Exception as exc:
            print(f"    could not connect ARMED to the Live-Trade Gateway (port 4003): "
                  f"{type(exc).__name__}: {exc}. Nothing sized, nothing transmitted.")
            return 1
        armed_conn = True
    else:
        print("\n[3] Connecting READ-ONLY to the Live-Trade Gateway (port 4003) for the "
              "preview...")
        try:
            ib = s0_live.connect_s0_live()
        except Exception as exc:
            print(f"    could not connect READ-ONLY to the Live-Trade Gateway (port 4003): "
                  f"{type(exc).__name__}: {exc}. Nothing sized, nothing transmitted.")
            return 1
        armed_conn = False

    try:
        return _run_session(ib, target, armed=armed, conform=conform,
                            armed_conn=armed_conn, kill=kill)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
        print("Session closed.")


def _run_session(ib, target, *, armed: bool, conform: bool,
                 armed_conn: bool, kill: bool) -> int:
    account = EXEC_ACCOUNT

    # [4] Read + FILTER to EXEC_ACCOUNT (the login also exposes the individual account +
    # an 'All' aggregate). Refuse if the target account is not present under the login.
    print(f"\n[4] Reading account summary + positions, filtering to {account}...")
    summary = s0_live.filter_account_summary(ib.accountSummary(), account=account)
    if not summary:
        print(f"    target account {account} not found under the Live-Trade login — "
              f"REFUSING. Nothing sized, nothing transmitted.")
        return 1
    net_liq = sp._net_liq(summary)
    if not net_liq or net_liq <= 0:
        print(f"    could not read a positive NetLiquidation for {account} — REFUSING.")
        return 1
    positions_raw = s0_live.filter_positions(ib.positions(), account=account)
    positions = {p.contract.symbol: p.position for p in positions_raw if p.position != 0}
    print(f"    account={account}   NetLiq={net_liq:,.2f}   open_positions={len(positions)}")

    # [5] Live prices over the union of target + held symbols (same merge the pilot uses).
    universe = sorted(set(target.weights.index) | set(positions))
    print(f"\n[5] Fetching live quotes for {len(universe)} symbol(s) on port 4003...")
    quotes = live_quotes.fetch(ib, universe)
    prices: dict = {}
    for sym in universe:
        q = quotes.get(sym)
        ref = live_quotes.reference_price(q) if q else None
        prices[sym] = ref if (ref and ref > 0) else float(
            target.prices.get(sym, float("nan")))

    # [6] Size the REAL account with the UNCHANGED engine (identical call to the pilot /
    # tiny-test). ALIEN (non-S0) holdings land on plan.alien_lines; plan.orders has the
    # sells+buys to reach the GROWTH target.
    strat_universe = sp._strategy_universe()
    print("\n[6] Sizing the plan with rebalance_engine.plan_account (UNCHANGED engine)...")
    plan = rebalance_engine.plan_account(account, target.version, net_liq, positions,
                                         target, prices=prices, universe=strat_universe)

    # [7]-[9] Delegate the ordered leg build, the full pre-flight gate, and the two-phase
    # cash-gated transmit to the shared engine. This is the extracted logic — moved, not
    # rewritten — so the printed order list / gate reasons / two-phase logs are identical.
    request = ExecutionRequest(
        account=account,
        strategy_version=target.version,
        plan=plan,
        target=target,
        quotes=quotes,
        prices=prices,
        allowed_accounts=[ALLOWED_ACCOUNT],
        caps=ExecutionCaps(per_order_model_multiple=MAX_ORDER_MODEL_MULTIPLE,
                           total_buy_le_investable=True, max_total_notional=None),
        conform=conform,
        run_id=None,
        net_liq=net_liq,
        summary=summary,
        armed=armed,
        kill=kill,
    )
    mode = MODE_ARMED if armed_conn else MODE_PREVIEW
    result = safe_execute.execute_plan(request, mode=mode, ib=ib)
    return result.rc


def cli(argv: list[str] | None = None) -> int:
    """CLI entry: --arm-i-understand sets armed=True; --conform sets conform=True. BOTH are
    required to actually liquidate + transmit."""
    argv = sys.argv[1:] if argv is None else argv
    return main(armed=arm_requested(argv), conform=conform_requested(argv))


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(cli())
