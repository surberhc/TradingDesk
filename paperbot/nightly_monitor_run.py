"""
nightly_monitor_run.py — NIGHTLY bounded-retry monitor + STAGE runner (automated pilot).

Scheduled ~9:15 PM CT, AFTER dailyreport\\eod_report.py's 9:00 PM CT EOD email (that
script is untouched and stays fully independent of gateway status — this file never
blocks or is blocked by it). Folds the old AccountMonitorDaily drift/cashflow check
into ONE nightly gateway touch that also decides whether tonight is a rebalance/signal
night, and if so, builds + guard-checks a trade list and STAGES it for the morning
executor (morning_execute_run.py) to pick up. PAPER account family (DU...141), port
4002. This file never says "live" and never transmits an order — it only reads,
computes, and (on a guard-passed rebalance night) writes a JSON staging file.

WHY A SEPARATE NIGHTLY SCRIPT (not just editing account_monitor_run.py in place):
account_monitor_run.py stays the read-only, human-triggerable Slice 6b monitor exactly
as before (still importable/runnable standalone). This file WRAPS the same read-only
work (reusing account_monitor_run's functions directly, not copy-pasting them) in a
BOUNDED retry connect and adds the staging decision on top. If the scheduled task is
retargeted at this file, account_monitor_run.py's own __main__ entry point is simply no
longer scheduled — nothing about the module itself changes.

BOUNDED RETRY: at most CONNECT_MAX_ATTEMPTS connect attempts, each up to
CONNECT_ATTEMPT_TIMEOUT_SECS, separated by CONNECT_RETRY_BACKOFF_SECS — roughly ten
minutes wall-clock worst case — then GIVE UP. This run must NEVER bleed into the next
morning: it either finishes (success or clean give-up) well before the 8:50 AM CT
morning script runs, or crashes loudly enough that the scheduled-task history/alert
email makes the miss visible.

CLEAN SHUTDOWN, ALWAYS: the whole connect -> work -> disconnect body runs inside
try/finally so the IB connection (and, if THIS run's connect call caused the Gateway
process itself to launch via ibkr_paper.ensure_gateway, nothing here re-kills a process that
may be legitimately needed for tomorrow — the Gateway process is intentionally LEFT
RUNNING; only the API connection this process opened is guaranteed closed) never leaks.
There is no scenario (error, timeout, KeyboardInterrupt) where this exits without
having tried to disconnect.

DECIDING "IS TONIGHT A REBALANCE NIGHT": reuses rebalance_engine.plan_account's
existing account-level no-trade-band decision (AccountPlan.needs_rebalance) — the SAME
byte-for-byte test the monitor and the human rebalance runner already use (see
rebalance_engine.band_breached). Tonight is a rebalance/signal night iff ANY enrolled,
funded client account's plan needs_rebalance. This piggybacks on an already-proven,
already-tested definition of "needs a trade" instead of re-deriving a fresh signal-date
comparison — the model can drift out of band on any trading day, not only a strict
calendar rebalance date (e.g. a delayed catch-up after a prior guard failure or a
missed night), so the band test is the more robust of the two options and is what
actually drives whether a real trade would be built.

STAGING FILE — the contract with morning_execute_run.py
---------------------------------------------------------
Path:  C:\\TradingDesk-Local\\pending_trades\\YYYY-MM-DD.json   (today's date, CT)
Written ONLY when: (a) at least one account needs rebalancing, AND (b) rebalance_guard
passes. On a guard FAIL, nothing is staged (see below) — an alert email goes out
instead so a human can look, and the standing manual rebalance_execute.py CLI remains
available to run by hand.

Schema (top-level JSON object):
{
  "date": "2026-07-09",                      # staging date (CT), also the filename
  "staged_at": "2026-07-09T21:17:03-05:00",   # ISO timestamp this file was written
  "paperbot_version": "0.14.0",
  "regime": {"raw": "...", "confirmed": "...", "as_of": "2026-07-09"},
  "guard": {"passed": true, "reasons": []},
  "as_of": {"Balanced": "2026-07-09", "Growth": "2026-07-09", ...},  # per-tier target as_of
  "accounts_needing_rebalance": ["DU8922142", ...],
  "routes": [
     {"route": "fa_block", "version": "Balanced", "symbol": "SPY", "side": "BUY",
      "total_qty": 12, "fa_group": "tier_balanced", "fa_method": "",
      "account": null, "per_account_split": {"DU8922143": 5, "DU8922144": 7},
      "reason": "REBALANCE_TO_MODEL"},
     {"route": "direct", "version": "Conservative", "symbol": "TFLO", "side": "SELL",
      "total_qty": 40, "fa_group": null, "fa_method": "", "account": "DU8922142",
      "per_account_split": {"DU8922142": 40}, "reason": "REBALANCE_TO_MODEL"}
  ],
  "prices_by_symbol": {"SPY": 512.34, "TFLO": 49.87, ...}   # reference prices used to
                                                              # size/value this plan
}
This is a plain, human-readable JSON file — inspect it any time with a text editor.
morning_execute_run.py re-validates (does NOT blindly trust) this file before acting on
it: it re-runs rebalance_guard.check() defensively before ever transmitting.

Run (gateway auto-starts if down; bounded — will give up rather than hang):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe ^
    "C:\\Users\\andre\\My Drive (andrew@surberhc.com)\\TradingDesk\\paperbot\\nightly_monitor_run.py"
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime

import accounts
import account_monitor_run as amr
import config
import live_quotes
import rebalance_guard
import strategy_target
import version
from connections import clientids, ibkr_paper
from gateway_lock import GatewayBusySkip, gateway_lock
from rebalance_engine import build_plan

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "dailyreport"))
import mailer  # noqa: E402  (sibling package, path inserted above)

try:
    import status as _status  # dailyreport/status.py
except Exception:
    _status = None

# --- bounded retry (never bleed into the morning) -------------------------------
CONNECT_MAX_ATTEMPTS = 3
CONNECT_ATTEMPT_TIMEOUT_SECS = 120     # ibkr_paper.connect's own timeout is short; this bounds
                                       # the ensure_gateway launch-and-wait inside it too
CONNECT_RETRY_BACKOFF_SECS = 90        # ~3 attempts * (up to ~120s each + 90s backoff)
                                       # worst case is comfortably under 10 minutes total

PENDING_TRADES_DIR = r"C:\TradingDesk-Local\pending_trades"


def _write_status(st: str, metrics: dict | None = None, message: str = "") -> None:
    """Best-effort status write; never raises into the caller."""
    if _status is None:
        return
    try:
        _status.write("nightly_monitor", st, metrics=metrics or {}, message=message,
                      day=date.today().strftime("%Y%m%d"))
    except Exception:
        pass


def _alert_email(subject: str, lines: list[str]) -> None:
    """Fire-and-forget alert email via the shared mailer. Never raises."""
    html = "<html><body><pre>" + "\n".join(lines) + "</pre></body></html>"
    try:
        mailer.send_html(f"[TradingDesk PAPER] {subject}", html)
    except Exception as exc:
        print(f"    ! alert email itself failed: {exc}")


def bounded_connect(consumer: str, readonly: bool = True):
    """Connect to the PAPER gateway with a HARD cap on total retry effort. Returns the
    connected IB instance, or None if every attempt failed (never raises past this
    point — the caller decides what a failed connect means). This function alone does
    NOT guarantee disconnect; the caller must still wrap its own work in try/finally."""
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
    print(f"    GIVING UP after {CONNECT_MAX_ATTEMPTS} attempts. Last error: "
          f"{type(last_exc).__name__}: {last_exc}" if last_exc else "    GIVING UP.")
    return None


def _stage_path(today: date) -> str:
    return os.path.join(PENDING_TRADES_DIR, f"{today.isoformat()}.json")


def _route_to_dict(r) -> dict:
    return {"route": r.route, "version": r.version, "symbol": r.symbol, "side": r.side,
            "total_qty": r.total_qty, "fa_group": r.fa_group, "fa_method": r.fa_method,
            "account": r.account, "per_account_split": dict(r.per_account_split),
            "reason": r.reason}


def stage_trade_list(today: date, routes: list, regime: dict, guard: "rebalance_guard.GuardResult",
                     targets: dict, needing: list[str], prices_by_symbol: dict) -> str:
    """Write the staging JSON. Only called after the guard has PASSED. Returns the path."""
    os.makedirs(PENDING_TRADES_DIR, exist_ok=True)
    payload = {
        "date": today.isoformat(),
        "staged_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "paperbot_version": version.VERSION,
        "regime": regime,
        "guard": {"passed": guard.passed, "reasons": guard.reasons},
        "as_of": {v: t.as_of.strftime("%Y-%m-%d") for v, t in targets.items()},
        "accounts_needing_rebalance": needing,
        "routes": [_route_to_dict(r) for r in routes],
        "prices_by_symbol": prices_by_symbol,
    }
    path = _stage_path(today)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)
    return path


def main() -> int:
    print("=" * 100)
    print(f"NIGHTLY MONITOR + STAGE (automated pilot)   [{version.banner()}]")
    print("=" * 100)
    today = date.today()

    ib = bounded_connect("paperbot_nightly_monitor", readonly=True)
    if ib is None:
        msg = (f"nightly_monitor_run: could not connect to the PAPER gateway after "
              f"{CONNECT_MAX_ATTEMPTS} attempts. No monitor cycle ran tonight, nothing "
              f"staged. Gateway may be down/paused — check it before the morning.")
        print(f"\n{msg}")
        _alert_email("nightly monitor: gateway connect FAILED", [msg])
        _write_status("fail", message=msg)
        return 1

    try:
        return _run_session(ib, today)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
        print("Nightly session closed (connection disconnected).")


def _run_session(ib, today: date) -> int:
    """The gateway-lock-held body: drift/cashflow monitor cycle + rebalance decision +
    guard + (maybe) stage. Runs only while the session's connection is open. Any
    exception here is allowed to propagate to main()'s finally (which still
    disconnects) — never swallowed silently."""
    try:
        with gateway_lock(purpose="nightly_monitor",
                          client_id=clientids.get("paperbot_nightly_monitor"),
                          on_busy="skip"):
            return _do_work(ib, today)
    except GatewayBusySkip as busy:
        holder = busy.holder or {}
        msg = (f"nightly monitor SKIPPED — gateway held by {holder.get('purpose')} pid "
              f"{holder.get('pid')} since {holder.get('acquired_at')}. No trades staged "
              f"tonight; tomorrow's morning script will simply see no staged file.")
        print(f"\n{msg}")
        _write_status("ok", message=msg)
        return 0


def _do_work(ib, today: date) -> int:
    # [1] Tier models.
    print("\n[1] Computing tier models (one per enrolled version)...")
    targets = amr._targets_by_version()
    for v, t in targets.items():
        print(f"    {v:13s} as_of={t.as_of.date()}  ({len(t.weights)} holdings)")

    # [2] Drift/cashflow monitor cycle (reuses account_monitor_run's own logic).
    print("\n[2] Drift/cashflow monitor cycle (read-only, reusing account_monitor_run)...")
    baselines = amr.load_baselines()
    earmarks_by_acct = amr.load_earmarks()
    managed = set(ib.managedAccounts())
    monitor_accounts = [a for a in sorted(config.ENROLLMENT)
                       if a in managed and not a.startswith("DF")]
    snapshots = [amr.read_account_cycle(ib, acct, today) for acct in monitor_accounts]
    amr.run_cycle(snapshots, targets, baselines, earmarks_by_acct, today, persist=True)

    # [3] Build account_inputs + decide whether tonight is a rebalance night, reusing
    # the SAME plan_account/band_breached logic the human rebalance path uses.
    print("\n[3] Building account plans (same engine as the human rebalance path)...")
    infos = accounts.discover(ib)
    clients = [i for i in infos if i.enrolled and i.funded and not i.is_master]
    if not clients:
        print("    no enrolled + funded client accounts. Nothing to stage.")
        _write_status("ok", message="no enrolled/funded accounts")
        return 0

    universe = sorted({s for t in targets.values() for s in t.weights.index})
    quotes = live_quotes.fetch(ib, universe)
    account_inputs: list[dict] = []
    for info in sorted(clients, key=lambda x: x.number):
        positions = {p.contract.symbol: p.position
                    for p in ib.positions(info.number) if p.position != 0}
        tier_prices = targets[info.version].prices
        prices = {}
        for sym in set(tier_prices.index) | set(positions):
            q = quotes.get(sym)
            ref = live_quotes.reference_price(q) if q else None
            prices[sym] = ref if (ref and ref > 0) else float(tier_prices.get(sym, float("nan")))
        account_inputs.append({"account": info.number, "version": info.version,
                              "net_liq": info.net_liq, "positions": positions, "prices": prices})

    out = build_plan(account_inputs, targets)
    plans = out["plans"]
    needing = [p.account for p in plans if p.needs_rebalance]
    if not needing:
        print("    every enrolled account is within the drift band — not a rebalance "
              "night. Nothing staged.")
        _write_status("ok", message="in-band; nothing staged")
        return 0

    print(f"    {len(needing)} account(s) need rebalancing: {needing}")

    # [4] Resolve FA groups (fail closed on ambiguity — reuse rebalance_run's resolver).
    print("\n[4] Resolving version->FA group via requestFA(1) (fail-closed)...")
    import rebalance_run
    enrolled_versions = {i.version for i in clients}
    try:
        tier_groups = rebalance_run.resolve_tier_groups(ib, enrolled_versions)
    except RuntimeError as exc:
        msg = f"nightly monitor: FA group resolution failed — {exc}. Nothing staged."
        print(f"    {msg}")
        _alert_email("nightly monitor: FA group resolution FAILED", [msg])
        _write_status("fail", message=msg)
        return 1
    out = build_plan(account_inputs, targets, tier_groups=tier_groups)
    routes = out["routes"]
    if not routes:
        print("    band test flagged accounts but no routes were built (e.g. all "
              "UNTRACKED-only) — nothing to stage.")
        _write_status("ok", message="no routes built despite band breach")
        return 0

    # [5] Regime this trade list is built under (same call site as the guard's own
    # cross-check, computed once here so the staged file can name it).
    print("\n[5] Computing today's regime (same method the 9PM email uses)...")
    raw_regime, confirmed_regime, regime_as_of = rebalance_guard.compute_regime_now()
    regime = {"raw": raw_regime, "confirmed": confirmed_regime, "as_of": regime_as_of}
    print(f"    raw={raw_regime}  confirmed={confirmed_regime}  as_of={regime_as_of}")

    # [6] Guard check — fail closed, never silently stage on ambiguity.
    print("\n[6] Guard check (ticker allow-list + turnover cap + regime cross-check)...")
    prices_by_symbol = {sym: px for ai in account_inputs for sym, px in ai["prices"].items()}
    guard = rebalance_guard.check(routes, account_inputs, prices_by_symbol,
                                  claimed_regime=confirmed_regime)
    if not guard.passed:
        msg_lines = [f"nightly monitor: GUARD FAILED — nothing staged tonight.",
                    f"Accounts needing rebalance: {needing}", ""] + \
                    [f"  - {r}" for r in guard.reasons]
        print("\n".join(msg_lines))
        _alert_email("nightly monitor: GUARD FAILED, nothing staged", msg_lines)
        _write_status("fail", metrics={"guard_reasons": guard.reasons},
                     message="guard failed; nothing staged")
        return 1

    # [7] Stage.
    path = stage_trade_list(today, routes, regime, guard, targets, needing, prices_by_symbol)
    print(f"\n[7] STAGED -> {path}")
    print(f"    {len(routes)} route(s) across {len(needing)} account(s). Morning "
          f"executor will pick this up ~8:50 AM CT.")
    _write_status("ok", metrics={"n_routes": len(routes), "accounts_needing_rebalance": needing},
                 message=f"staged {len(routes)} route(s) -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
