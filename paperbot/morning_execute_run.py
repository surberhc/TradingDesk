"""
morning_execute_run.py — MORNING execution runner for a staged, guard-approved trade
list (automated pilot, PAPER account family DU...141, port 4002).

Scheduled ~8:50 AM CT (~9:50 AM ET, ~20 minutes after the 9:30 AM ET open) daily. On a
normal day (no staged file) this makes ZERO gateway contact — it reads one directory
listing and exits. Only on a night nightly_monitor_run.py staged a guard-approved trade
list does this script do any real work.

PILOT_MODE (see constant below, defaults True): while pilot mode is on, this script
does everything EXCEPT the actual transmit — it connects, re-validates the guard,
reads live quotes for a realistic log/email, but calls no order-placing code. It emails
"WOULD HAVE TRANSMITTED: ..." instead. PILOT_MODE must be flipped to False by Andrew,
by hand, only after reviewing enough pilot cycles — nothing in this build flips it.

KILL SWITCH: C:\\TradingDesk-Local\\AUTOTRADE_DISABLED (a sentinel file, any content or
empty) — if present, skip transmission entirely (even in pilot mode's "would have"
logging is still fine to compute, but no live-quote gateway session is opened past the
kill-switch check), send an alert, leave the staged file in place for a human to run by
hand via `rebalance_execute.py --arm-i-understand`, and exit.

CLEAN SHUTDOWN, ALWAYS: connect -> work -> disconnect wrapped in try/finally, mirroring
nightly_monitor_run.py's discipline — full fill, partial fill, timeout, or error, the
connection is always closed.

STAGED FILE HANDLING: after processing (successful pilot "would have" report, or a real
armed execution when PILOT_MODE=False), the staging file is MOVED to
C:\\TradingDesk-Local\\pending_trades\\archive\\ so it can never be reprocessed by a
later run. If the kill-switch is tripped, the file is intentionally NOT moved (a human
still needs it to act manually).

Run (no-ops instantly if nothing is staged for today):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe ^
    "C:\\TradingDesk\\paperbot\\morning_execute_run.py"
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import date, datetime

# =================================================================================
# PILOT MODE — flip to False ONLY after Andrew reviews N pilot cycles' emailed
# "WOULD HAVE TRANSMITTED" reports and explicitly decides to arm unattended morning
# execution. Nothing in this build flips this automatically. Defaults True.
# =================================================================================
PILOT_MODE = True

import accounts  # noqa: E402
import arming  # noqa: E402
import config  # noqa: E402
import ledger  # noqa: E402
import live_quotes  # noqa: E402
import order_router  # noqa: E402
import rebalance_execute  # noqa: E402
import rebalance_guard  # noqa: E402
import rebalance_run  # noqa: E402
import reconcile  # noqa: E402
import transmit_journal  # noqa: E402
import version  # noqa: E402
from connections import clientids, ibkr_paper  # noqa: E402
from gateway_lock import GatewayBusyRefuse, gateway_lock  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "dailyreport"))
import mailer  # noqa: E402

try:
    import status as _status  # dailyreport/status.py
except Exception:
    _status = None

PENDING_TRADES_DIR = r"C:\TradingDesk-Local\pending_trades"
ARCHIVE_DIR = os.path.join(PENDING_TRADES_DIR, "archive")
AUTOTRADE_DISABLED_SENTINEL = r"C:\TradingDesk-Local\AUTOTRADE_DISABLED"

# Bounded retry — identical policy to nightly_monitor_run.py.
CONNECT_MAX_ATTEMPTS = 3
CONNECT_ATTEMPT_TIMEOUT_SECS = 120
CONNECT_RETRY_BACKOFF_SECS = 90


def _write_status(st: str, metrics: dict | None = None, message: str = "") -> None:
    if _status is None:
        return
    try:
        _status.write("morning_execute", st, metrics=metrics or {}, message=message,
                      day=date.today().strftime("%Y%m%d"))
    except Exception:
        pass


def _alert_email(subject: str, lines: list[str]) -> None:
    html = "<html><body><pre>" + "\n".join(lines) + "</pre></body></html>"
    try:
        mailer.send_html(f"[TradingDesk PAPER] {subject}", html)
    except Exception as exc:
        print(f"    ! alert email itself failed: {exc}")


def _leg_summary(res: dict) -> dict:
    """Distill a place()/place_laddered() result into the transmit-journal SENT fields.
    place() returns per-order `fills`; place_laddered() returns filled/remaining/rested."""
    fills = res.get("fills") or []
    filled = res.get("filled")
    if filled is None:
        filled = sum(float(f.get("filled", 0.0) or 0.0) for f in fills) if fills else None
    rested = res.get("rested")
    if rested is None and fills:
        rested = any(f.get("rested_gtc") for f in fills)
    return {"filled": filled, "remaining": res.get("remaining"),
            "rested_gtc": rested, "avg_px": res.get("avgFillPrice")}


def _ledger_cycle(*, mode: str, routes: list, as_of, n_sent: int, n_skipped: int,
                  fills: list, pilot: bool) -> None:
    """Coarse per-cycle audit line via ledger.record_run() (closes finding #4 — the
    morning path never called it before). Best-effort: never let a logging failure break
    the run's disconnect/archive discipline."""
    try:
        ledger.record_run({
            "mode": mode, "account": "ALL_ENROLLED", "nav": 0.0,
            "target_as_of": str(as_of),
            "intents": [{"route": r.route, "side": r.side, "symbol": r.symbol,
                         "qty": r.total_qty, "group": r.fa_group, "account": r.account}
                        for r in routes],
            "n_intents": len(routes), "n_approved": len(routes),
            "n_transmitted": n_sent, "n_skipped": n_skipped, "fills": fills,
            "pilot_mode": pilot, "halted": False,
            **version.stamp(),
        })
    except Exception as exc:
        print(f"    ! ledger.record_run failed (non-fatal): {exc}")


def _strategy_universe() -> set[str] | None:
    """S0's tradeable universe via the strategy's own universe() accessor. None on failure
    (alien detection is then skipped — it is informational and must never block execution)."""
    try:
        from strategies.all_weather import universe as s0_universe
        return s0_universe()
    except Exception as exc:
        print(f"    ! could not resolve strategy universe ({exc}); skipping alien check.")
        return None


def _corp_action_review(ib, clients: dict) -> list[str]:
    """Read each enrolled account's positions and surface any ALIEN (corp-action / manual)
    holding for human review — a held symbol the strategy does not know that the bot will
    NEVER auto-trade. Best-effort and READ-ONLY: any failure returns [] (never blocks the
    morning run). Emits its own distinct alert if aliens are found; returns the alert body."""
    uni = _strategy_universe()
    if uni is None:
        return []
    whitelist = set(getattr(config, "SWEEP_WHITELIST", set()))
    body: list[str] = []
    try:
        for acct in sorted(clients):
            positions = {p.contract.symbol: p.position
                         for p in ib.positions(acct) if p.position != 0}
            aliens = reconcile.alien_holdings(positions, uni, whitelist)
            if aliens:
                body.append(f"  account {acct}:")
                for sym, sh in aliens:
                    body.append(f"    {sym:6s} qty={sh:,.4f}")
    except Exception as exc:
        print(f"    ! alien holdings check failed (non-fatal): {exc}")
        return []
    if body:
        body = [
            "CORP-ACTION / UNTRACKED REVIEW — a held symbol the strategy does NOT know was "
            "found. The bot transmits NOTHING against it (fail-closed). A human must: "
            "(a) manually sell via rebalance_execute.py --arm-i-understand, (b) update "
            "ENROLLMENT/model if it belongs, or (c) accept + whitelist (config.SWEEP_WHITELIST).",
            "",
            "Alien holdings:",
        ] + body
        _alert_email("CORP-ACTION / UNTRACKED REVIEW (alien holding found)", body)
    return body


def _stage_path(today: date) -> str:
    return os.path.join(PENDING_TRADES_DIR, f"{today.isoformat()}.json")


def _archive(path: str) -> str:
    """Move a processed staging file into the archive subfolder. Never raises into the
    caller (best-effort — worst case the file is simply left in place and a human
    notices a stale file, which is safe, not silently reprocessed since the morning
    script only ever looks at TODAY's date)."""
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        dest = os.path.join(ARCHIVE_DIR, os.path.basename(path))
        shutil.move(path, dest)
        return dest
    except Exception as exc:
        print(f"    ! could not archive staged file {path}: {exc}")
        return ""


def bounded_connect(consumer: str, readonly: bool = True):
    """Identical bounded-retry policy to nightly_monitor_run.bounded_connect. Duplicated
    (not imported) on purpose: these two scripts must each be independently robust and
    self-contained for a scheduled task, and the policy is tiny/stable enough that a
    shared import would be over-engineering for two call sites with the exact same
    ~15-line body. If this drifts between the two files in a future change, consider
    factoring it into a shared leaf module."""
    last_exc: Exception | None = None
    for attempt in range(1, CONNECT_MAX_ATTEMPTS + 1):
        print(f"    connect attempt {attempt}/{CONNECT_MAX_ATTEMPTS} "
              f"(consumer={consumer}, readonly={readonly})...")
        try:
            ib = ibkr_paper.connect(consumer, readonly=readonly, launch=True,
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


class _StagedIntent:
    """Duck-typed route-ish object rebuilt from the staged JSON, matching the shape
    rebalance_execute.py's execute loop expects (route/version/symbol/side/total_qty/
    fa_group/fa_method/account/per_account_split/reason)."""
    def __init__(self, d: dict):
        self.route = d["route"]
        self.version = d["version"]
        self.symbol = d["symbol"]
        self.side = d["side"]
        self.total_qty = int(d["total_qty"])
        self.fa_group = d.get("fa_group")
        self.fa_method = d.get("fa_method", "")
        self.account = d.get("account")
        self.per_account_split = dict(d.get("per_account_split") or {})
        self.reason = d.get("reason", "REBALANCE_TO_MODEL")


def main() -> int:
    today = date.today()
    stage_path = _stage_path(today)
    if not os.path.exists(stage_path):
        # The common case: no rebalance was staged tonight. ZERO gateway touch.
        print(f"No staged trade file for {today.isoformat()} — nothing to do. "
              f"(checked {stage_path})")
        return 0

    print("=" * 100)
    print(f"MORNING EXECUTE (automated pilot, PILOT_MODE={PILOT_MODE})   "
          f"[{version.banner()}]")
    print("=" * 100)

    with open(stage_path, "r", encoding="utf-8") as fh:
        staged = json.load(fh)
    print(f"\nLoaded staged trade list: {stage_path}")
    print(f"    staged_at={staged.get('staged_at')}  regime={staged.get('regime')}  "
          f"{len(staged.get('routes', []))} route(s)")

    if os.path.exists(AUTOTRADE_DISABLED_SENTINEL):
        msg = (f"AUTOTRADE_DISABLED sentinel present ({AUTOTRADE_DISABLED_SENTINEL}) — "
              f"morning execute is SKIPPING transmission. The staged file is LEFT IN "
              f"PLACE at {stage_path} for manual review/execution via "
              f"rebalance_execute.py --arm-i-understand.")
        print(f"\n{msg}")
        _alert_email("morning execute: AUTOTRADE_DISABLED, skipped", [msg])
        _write_status("fail", message="autotrade disabled sentinel present")
        return 0

    routes = [_StagedIntent(d) for d in staged.get("routes", [])]

    ib = bounded_connect("paperbot_morning_execute",
                        readonly=PILOT_MODE)   # pilot mode never needs write access
    if ib is None:
        msg = (f"morning execute: could not connect to the PAPER gateway after "
              f"{CONNECT_MAX_ATTEMPTS} attempts. Staged file LEFT IN PLACE at "
              f"{stage_path} for manual execution.")
        print(f"\n{msg}")
        _alert_email("morning execute: gateway connect FAILED", [msg])
        _write_status("fail", message=msg)
        return 1

    try:
        return _run_session(ib, today, stage_path, staged, routes)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
        print("Morning session closed (connection disconnected).")


def _run_session(ib, today: date, stage_path: str, staged: dict, routes: list) -> int:
    try:
        with gateway_lock(purpose="morning_execute",
                          client_id=clientids.get("paperbot_morning_execute"),
                          on_busy="refuse"):
            return _do_work(ib, today, stage_path, staged, routes)
    except GatewayBusyRefuse as busy:
        holder = busy.holder or {}
        msg = (f"morning execute REFUSED to start — gateway held by "
              f"{holder.get('purpose')} pid {holder.get('pid')} since "
              f"{holder.get('acquired_at')}. Staged file LEFT IN PLACE at {stage_path}; "
              f"re-run manually once the holder finishes, or use "
              f"rebalance_execute.py --arm-i-understand.")
        print(f"\n{msg}")
        _alert_email("morning execute: gateway busy, refused", [msg])
        _write_status("fail", message=msg)
        return 2


def _do_work(ib, today: date, stage_path: str, staged: dict, routes: list) -> int:
    # [1] Live quotes for logging + defense-in-depth re-validation.
    universe = sorted({r.symbol for r in routes})
    print(f"\n[1] Fetching live quotes for {len(universe)} symbol(s)...")
    quotes = live_quotes.fetch(ib, universe)
    prices_by_symbol = dict(staged.get("prices_by_symbol") or {})
    for sym in universe:
        q = quotes.get(sym)
        ref = live_quotes.reference_price(q) if q else None
        if ref and ref > 0:
            prices_by_symbol[sym] = ref   # prefer a fresh morning quote when available

    # [2] Defense-in-depth: re-run the guard NOW, against fresh prices/regime. A staged
    # file must never be trusted blindly — re-validate before ever transmitting, pilot
    # or not.
    print("\n[2] Re-validating guard (defense in depth, fresh regime + prices)...")
    infos = accounts.discover(ib)
    clients = {i.number: i for i in infos if i.enrolled and i.funded and not i.is_master}
    account_inputs = [{"account": a, "net_liq": clients[a].net_liq}
                      for a in sorted(clients) if a in clients]
    claimed_regime = (staged.get("regime") or {}).get("confirmed")
    guard = rebalance_guard.check(routes, account_inputs, prices_by_symbol,
                                  claimed_regime=claimed_regime)
    if not guard.passed:
        msg_lines = [f"morning execute: RE-VALIDATION FAILED — transmitting nothing.",
                    ""] + [f"  - {r}" for r in guard.reasons]
        print("\n".join(msg_lines))
        _alert_email("morning execute: guard re-validation FAILED", msg_lines)
        _write_status("fail", metrics={"guard_reasons": guard.reasons},
                     message="re-validation failed; staged file left in place")
        # Leave the staged file in place — do NOT archive a rejected run silently.
        return 1

    # [2b] CORP-ACTION / UNTRACKED REVIEW — surface any alien holding for a human. This is
    # informational and NON-BLOCKING: the staged ROTATE_OUT/DRIFTED/MISSING trade list
    # proceeds normally (an alien holding never generates a route in the first place).
    print("\n[2b] Alien-holding review (read-only, non-blocking)...")
    aliens = _corp_action_review(ib, clients)
    if aliens:
        print("    " + "\n    ".join(aliens))
    else:
        print("    no alien holdings found.")

    # The cycle's as_of (per-tier); used to derive the deterministic orderRef the gate and
    # journal key on, matching what build()/build_fa_block() stamp on the wire.
    as_of = staged.get("as_of", {})
    default_as_of = next(iter(as_of.values()), today.isoformat())

    # [3] PILOT MODE: run the REAL dedup gate as a ZERO-TRANSMIT rehearsal (Q3) — the exact
    # reqAllOpenOrders/reqExecutions read path the armed run uses, on the read-only session —
    # log "WOULD SEND / WOULD SKIP", email "WOULD HAVE TRANSMITTED", transmit nothing.
    if PILOT_MODE:
        lines = [f"PILOT MODE — nothing was transmitted. This is what WOULD have been "
                f"sent if PILOT_MODE were False (with the dedup gate rehearsed live):", ""]
        n_would_send = 0
        n_would_skip = 0
        alerts: list[str] = []
        for r in routes:
            px = prices_by_symbol.get(r.symbol, float("nan"))
            order_ref = order_router.order_ref_for_route(r, default_as_of)
            state = order_router.already_present(ib, order_ref, r.total_qty, day=today)
            if state == order_router.LegState.FRESH:
                n_would_send += 1
                verb = "WOULD SEND (FRESH)"
            else:
                n_would_skip += 1
                verb = f"WOULD SKIP ({state})"
                if order_router.leg_state_needs_alert(state):
                    alerts.append(f"{state}: {r.side} {r.symbol} x{r.total_qty} ref={order_ref}")
            if r.route == "fa_block":
                split = ", ".join(f"{a}:{q}" for a, q in sorted(r.per_account_split.items()))
                lines.append(f"  {verb} | fa_block {r.side} {r.symbol} "
                            f"x{r.total_qty} @~{px:.2f}  group={r.fa_group}  split=[{split}]")
            else:
                lines.append(f"  {verb} | direct {r.side} {r.symbol} "
                            f"x{r.total_qty} @~{px:.2f}  account={r.account}")
        if alerts:
            lines += ["", "GATE ALERTS (partial/uncertain legs — would need a human):"] + \
                     [f"  - {a}" for a in alerts]
        print("\n".join(lines))
        _alert_email(f"morning execute PILOT: {n_would_send} would send / {n_would_skip} "
                    f"would skip", lines)
        _write_status("ok", metrics={"n_routes": len(routes), "pilot_mode": True,
                                     "would_send": n_would_send, "would_skip": n_would_skip},
                     message=f"pilot dry-run: {len(routes)} route(s) rehearsed, nothing transmitted")
        _ledger_cycle(mode="MORNING_EXECUTE_PILOT", routes=routes, as_of=default_as_of,
                      n_sent=0, n_skipped=n_would_skip, fills=[], pilot=True)
        transmit_journal.record_cycle_complete(as_of=default_as_of, n_routes=len(routes),
                                                n_sent=0, n_skipped=n_would_skip, day=today)
        dest = _archive(stage_path)
        print(f"\nStaged file archived -> {dest or '(archive failed, left in place)'}")
        return 0

    # [4] REAL execution path (PILOT_MODE=False, not exercised by this build). Confirm
    # armed, then call into rebalance_execute's proven building blocks — never
    # reimplemented here.
    print("\n[4] PILOT_MODE=False: confirming Gateway is armed before transmitting...")
    if arming.probe_api_readonly():
        print("    Gateway is currently Read-Only — arming now...")
        try:
            arming.arm()
        except RuntimeError as exc:
            msg = f"morning execute: ARM FAILED — {exc}. Transmitting nothing."
            print(f"    {msg}")
            _alert_email("morning execute: ARM FAILED", [msg])
            _write_status("fail", message=msg)
            return 2

    fills: list[dict] = []
    backup_path = ""
    n_sent = 0
    n_skipped = 0
    gate_alerts: list[str] = []
    try:
        backup_path = rebalance_execute.backup_fa_groups(ib)
        print(f"    FA groups backed up -> {backup_path}")
        for r in routes:
            order_ref = order_router.order_ref_for_route(r, default_as_of)
            # Snapshot the journal BEFORE we write our own ATTEMPTING, so neither our gate
            # here nor place()'s internal gate trips on this run's own record. The gate reads
            # broker truth against this pre-run snapshot.
            snap = transmit_journal.state_for(order_ref, today)
            state = order_router.already_present(ib, order_ref, r.total_qty, day=today,
                                                 journal_state=snap)
            if state != order_router.LegState.FRESH:
                n_skipped += 1
                msg = (f"GATE SKIP ({state}): {r.side} {r.symbol} x{r.total_qty} "
                       f"ref={order_ref}")
                print(f"    {msg} — transmitting nothing for this leg.")
                if order_router.leg_state_needs_alert(state):
                    gate_alerts.append(msg)
                continue

            # FRESH: journal ATTEMPTING (pre-place tripwire), transmit, journal SENT.
            transmit_journal.record_attempting(order_ref, as_of=default_as_of, symbol=r.symbol,
                                                side=r.side, target_qty=r.total_qty, day=today)
            if r.route == "fa_block":
                limit = round(float(prices_by_symbol.get(r.symbol, float("nan"))), 2)
                print(f"    [block] {r.side} {r.symbol} x{r.total_qty} group={r.fa_group} "
                      f"limit={limit}")
                rebalance_execute.set_group_contracts_or_shares(ib, r.fa_group,
                                                                r.per_account_split)
                bo = order_router.build_fa_block(r.symbol, r.side, r.total_qty, limit,
                                                  r.fa_group, r.fa_method, default_as_of, ib=ib)
                res = order_router.place(ib, [bo], armed=True, day=today,
                                         journal_states={order_ref: snap})
            else:
                q = quotes.get(r.symbol)
                res = rebalance_execute._place_direct_laddered(
                    ib, r, q, default_as_of, armed=True, day=today, journal_state=snap)
                if res is None:
                    limit = round(float(prices_by_symbol.get(r.symbol, float("nan"))), 2)
                    intent = rebalance_run._DirectIntent(r.symbol, r.side, r.total_qty, limit)
                    built = order_router.build([intent], r.account, default_as_of, ib=ib)
                    res = order_router.place(ib, built, armed=True, day=today,
                                             journal_states={order_ref: snap})
            res = res or {}
            if res.get("skipped"):
                # A rare race: broker state changed between our gate and place()'s gate.
                # Nothing transmitted — alert, and DON'T mark SENT (leave ATTEMPTING so the
                # next run also fails closed on this uncertain leg).
                n_skipped += 1
                gate_alerts.append(f"GATE SKIP (place internal): {r.side} {r.symbol} "
                                   f"ref={order_ref}")
            else:
                summary = _leg_summary(res)
                transmit_journal.record_sent(order_ref, day=today, **summary)
                n_sent += 1
                fills.extend(res.get("fills", []))
    finally:
        lines = [f"ARMED morning execution complete. {n_sent} leg(s) sent, "
                 f"{n_skipped} skipped by the dedup gate. {len(fills)} fill event(s).", ""]
        for f in fills:
            lines.append(f"  {f}")
        if gate_alerts:
            lines += ["", "GATE ALERTS (partial/uncertain legs — human review needed):"] + \
                     [f"  - {a}" for a in gate_alerts]
        subject = f"morning execute ARMED: {n_sent} sent / {n_skipped} skipped"
        if gate_alerts:
            subject = "[ACTION NEEDED] " + subject
        _alert_email(subject, lines)
        _write_status("ok", metrics={"n_routes": len(routes), "n_fills": len(fills),
                                     "n_sent": n_sent, "n_skipped": n_skipped,
                                     "pilot_mode": False},
                     message=f"armed execution: {n_sent} sent, {n_skipped} skipped, "
                             f"{len(fills)} fill event(s)")
        _ledger_cycle(mode="MORNING_EXECUTE_ARMED", routes=routes, as_of=default_as_of,
                      n_sent=n_sent, n_skipped=n_skipped, fills=fills, pilot=False)
        transmit_journal.record_cycle_complete(as_of=default_as_of, n_routes=len(routes),
                                                n_sent=n_sent, n_skipped=n_skipped, day=today)
        dest = _archive(stage_path)
        print(f"\nStaged file archived -> {dest or '(archive failed, left in place)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
