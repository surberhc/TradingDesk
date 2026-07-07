"""
account_monitor_run.py — the LIVE SHELL for the per-account cashflow monitor (Slice 6b).

This is the live layer that wraps the PURE decision core (account_monitor.decide), exactly
as rebalance_run.py wraps the pure rebalance_engine. The pure core stays free of any broker
import (a boundary test enforces it); THIS module is the one allowed to touch the gateway —
and it touches it READ-ONLY only.

WHAT IT DOES, per enrolled CLIENT sub-account (DU8922142..146; never the FA master DF…141):
  * reads account values READ-ONLY — NetLiquidation, TotalCashValue, and the real
    SettledCashByDate tag ('YYYYMMDD:amount'), decoded by accounts.parse_settled_cash_by_date
    and cross-checked against TotalCashValue;
  * pulls today's fills via reqExecutions(ExecutionFilter()) — an EMPTY set is normal/healthy
    and maps cleanly onto the pure core's Execution objects;
  * loads a per-account BASELINE (last-seen settled cash + date) from STATE_DIR, and operator
    EARMARKS from a STATE_DIR JSON file (operator-maintained; an empty example is created if
    none exists — NO real client data ships in this repo);
  * feeds (values + settled cash + baseline + fills + earmarks) into an AccountState, calls
    the pure decide(), and PRINTS a clean per-account verdict table;
  * PERSISTS the new baseline (today's settled cash + date) back to STATE_DIR so the next
    cycle can detect a deposit. The FIRST run just establishes the baseline.

PROPOSE-ONLY / READ-ONLY (hard): this shell connects with readonly=True (the session is
physically incapable of transmitting), and calls ONLY read endpoints — accountValues,
managedAccounts, positions, reqExecutions. It NEVER places, modifies, or cancels an order,
NEVER calls whatIfOrder (it HANGS), writes NO FA config, and changes NO gateway config. The
only thing it writes is the local STATE_DIR baseline file (off Drive, like ledger.py).

TIMEOUT DISCIPLINE (from the live probe): ib_async is asyncio/loop-bound — IB calls MUST run
on the main/loop-owning thread (running reqExecutions in a worker thread fails "no current
event loop in thread"). So we DO NOT thread the IB calls; we bound the connect with a tight
timeout and guard the whole cycle with a process-level watchdog. If a call hangs, we abort,
disconnect, and report — we never sit blocked.

Run (gateway auto-starts if down):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe ^
    "C:\\Users\\andre\\My Drive (andrew@surberhc.com)\\TradingDesk\\paperbot\\account_monitor_run.py"
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime

import account_monitor as mon
import accounts
import cashflows
import config
import nav_history
import strategy_target
import version
from connections import clientids, ibkr
from gateway_lock import GatewayBusySkip, gateway_lock

# --- daily status artifact (read by the EOD digest + heartbeat_alarm) ----------
# Same import trick heartbeat_alarm uses for the mailer: put the sibling dailyreport
# package dir on sys.path and import its `status` module. Every write is wrapped so
# it can NEVER break this read-only monitor cycle.
_DAILYREPORT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dailyreport")
if _DAILYREPORT_DIR not in sys.path:
    sys.path.insert(0, _DAILYREPORT_DIR)
try:
    import status as _status
except Exception:
    _status = None


def _write_monitor_status(st: str, metrics: dict | None = None, message: str = "") -> None:
    """Write the 'account_monitor' status JSON. Never raises into the caller.

    Severity here reflects REAL per-account drift (ALERT/REBALANCE verdicts from decide()),
    not just whether the read-only cycle completed cleanly. Before this, a week of live
    ALERT/UNTRACKED_POSITION verdicts (2026-07-01 -> 2026-07-07, every account) still showed
    "ok" in the nightly EOD email, because status only tracked rc==0 vs non-zero — the cycle
    ran fine even though every account needed a human. See _run_with_status()."""
    if _status is None:
        return
    try:
        _status.write("account_monitor", st, metrics=metrics or {}, message=message,
                      day=date.today().strftime("%Y%m%d"))
    except Exception:
        pass

# --- STATE_DIR artifacts (off Drive, same dir/pattern as ledger.py) ------------
BASELINES_JSON = os.path.join(config.STATE_DIR, "monitor_baselines.json")
EARMARKS_JSON = os.path.join(config.STATE_DIR, "monitor_earmarks.json")

# Process-level watchdog: if one full read-only cycle hasn't finished in this many seconds,
# something is hung — abort hard rather than block (supervise-long-ops rule).
CYCLE_WATCHDOG_SECONDS = 90


# --- STATE_DIR persistence (the ONLY writes this shell makes) ------------------
def load_baselines() -> dict:
    """Read the per-account settled-cash baselines from STATE_DIR. {account: {"settled_cash":
    float, "date": "YYYY-MM-DD"}}. Missing/garbled file -> {} (cold start, no false deposit)."""
    if not os.path.exists(BASELINES_JSON):
        return {}
    try:
        with open(BASELINES_JSON, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_baselines(baselines: dict) -> str:
    """Persist the per-account baselines back to STATE_DIR (off Drive). Returns the path."""
    os.makedirs(config.STATE_DIR, exist_ok=True)
    with open(BASELINES_JSON, "w", encoding="utf-8") as fh:
        json.dump(baselines, fh, indent=2, default=str)
    return BASELINES_JSON


def load_earmarks() -> dict:
    """Read the operator-maintained EARMARKS file from STATE_DIR. Shape:
        {"DU8922142": [{"amount": 60000, "note": "client X ad-hoc withdrawal"}], ...}
    Returns {account: [Earmark, ...]}. If the file is missing, write a clear EMPTY example
    (NO real client data) and return {} — the operator fills it in when needed."""
    if not os.path.exists(EARMARKS_JSON):
        _write_earmarks_example()
        return {}
    try:
        with open(EARMARKS_JSON, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    out: dict[str, list] = {}
    if not isinstance(data, dict):
        return out
    for acct, entries in data.items():
        if acct.startswith("_") or not isinstance(entries, list):
            continue   # skip comment keys like "_README"
        ems = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            try:
                amt = float(e.get("amount", 0.0))
            except (TypeError, ValueError):
                continue
            ems.append(mon.Earmark(account=acct, amount=amt, note=str(e.get("note", ""))))
        if ems:
            out[acct] = ems
    return out


def _write_earmarks_example() -> None:
    """Create an EMPTY, commented earmarks file so the operator knows the shape. No real
    client data — every entry list is empty."""
    os.makedirs(config.STATE_DIR, exist_ok=True)
    example = {
        "_README": ("Operator-maintained EARMARKS for the account monitor. To FENCE cash "
                    "raised for an ad-hoc client withdrawal, add an entry under the account "
                    "number: {\"amount\": <dollars>, \"note\": \"why\"}. The monitor adds it "
                    "to that account's reserve so the cash is never proposed for redeploy. "
                    "Remove the entry once the withdrawal is disbursed. NO real client data "
                    "is committed to the repo — this file lives in local STATE_DIR only."),
        "_example": [{"amount": 60000, "note": "EXAMPLE ONLY — delete me"}],
    }
    for acct in sorted(config.ENROLLMENT):
        example[acct] = []
    with open(EARMARKS_JSON, "w", encoding="utf-8") as fh:
        json.dump(example, fh, indent=2)


# --- read-only broker reads (main/loop-owning thread ONLY) ---------------------
def _account_value(values, account: str, tag: str) -> str | None:
    """Raw STRING value of an accountValues tag for one account (None if absent). Read-only."""
    for row in values:
        if row.account == account and row.tag == tag:
            return row.value
    return None


def _float_tag(values, account: str, tag: str) -> float | None:
    raw = _account_value(values, account, tag)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def read_account_cycle(ib, account: str, today: date) -> dict:
    """READ-ONLY snapshot for one account: NetLiq, TotalCashValue, decoded SettledCashByDate,
    positions, and today's fills. All on the loop-owning thread. Returns a plain dict; builds
    and transmits nothing."""
    values = ib.accountValues(account)
    net_liq = _float_tag(values, account, "NetLiquidation")
    total_cash = _float_tag(values, account, "TotalCashValue")

    settled_raw = _account_value(values, account, "SettledCashByDate")
    decoded = accounts.parse_settled_cash_by_date(settled_raw)
    settled_cash = decoded[1] if decoded else None
    settled_date = decoded[0] if decoded else None

    positions = {p.contract.symbol: p.position
                 for p in ib.positions(account) if p.position != 0}

    # Today's executions for THIS account. Empty set is normal/healthy.
    fills = _read_today_fills(ib, account, today)

    return {"account": account, "net_liq": net_liq, "total_cash": total_cash,
            "settled_cash": settled_cash, "settled_date": settled_date,
            "settled_raw": settled_raw, "positions": positions, "fills": fills}


def _read_today_fills(ib, account: str, today: date) -> list:
    """Map today's reqExecutions rows for `account` onto the pure core's Execution objects.
    READ-ONLY. An empty list is the normal, healthy case (no trades today)."""
    from ib_async import ExecutionFilter
    out: list = []
    flt = ExecutionFilter()
    try:
        fills = ib.reqExecutions(flt)
    except Exception as exc:           # never let a fills read hang/blow up the whole cycle
        print(f"    ! reqExecutions failed for {account}: {exc} (treating as 0 fills)")
        return out
    for f in fills:
        ex = getattr(f, "execution", None)
        contract = getattr(f, "contract", None)
        if ex is None or contract is None:
            continue
        if getattr(ex, "acctNumber", None) != account:
            continue
        # Only count today's fills (the baseline diff is a single-day signal).
        ex_time = getattr(ex, "time", None)
        if isinstance(ex_time, datetime) and ex_time.date() != today:
            continue
        out.append(mon.Execution(
            symbol=getattr(contract, "symbol", ""),
            side=getattr(ex, "side", ""),
            shares=float(getattr(ex, "shares", 0.0) or 0.0),
            price=float(getattr(ex, "price", 0.0) or 0.0)))
    return out


# --- assemble AccountState + decide (pure) -------------------------------------
def build_state(snap: dict, version_str: str, target, baseline: dict | None,
                earmarks: list, today: date,
                already_flagged: bool = False) -> mon.AccountState:
    """Compose one AccountState from a read-only snapshot + the persisted baseline +
    operator earmarks. PURE assembly — no broker calls here."""
    base_cash = baseline.get("settled_cash") if baseline else None
    base_date = None
    if baseline and baseline.get("date"):
        try:
            base_date = datetime.strptime(baseline["date"], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            base_date = None
    cash = snap.get("total_cash")
    return mon.AccountState(
        account=snap["account"], version=version_str,
        net_liq=snap.get("net_liq") or 0.0,
        cash=cash if cash is not None else (snap.get("settled_cash") or 0.0),
        positions=snap.get("positions", {}), schedule=cashflows.SCHEDULE.get(snap["account"], []),
        target=target,
        settled_cash=snap.get("settled_cash"), baseline_settled_cash=base_cash,
        baseline_date=base_date, as_of_date=today,
        fills=snap.get("fills", []), deposit_already_flagged_today=already_flagged,
        earmarks=earmarks)


def _targets_by_version() -> dict:
    """One tier model per distinct enrolled version (read-only compute, same path as the
    backtest). Identical to rebalance_run._targets_by_version."""
    return {v: strategy_target.current_target(version=v)
            for v in sorted(set(config.ENROLLMENT.values()))}


# --- the verdict table ---------------------------------------------------------
def print_verdict_table(rows: list) -> None:
    """rows: list of (snap, verdict). Clean per-account propose-only readout."""
    print(f"\n{'ACCOUNT':12s} {'VER':13s} {'NETLIQ':>13s} {'CASH':>13s} "
          f"{'SETTLED':>13s} {'FILLS':>5s}  {'ACTION':9s} REASON")
    print("-" * 104)
    for snap, v in rows:
        nl = snap.get("net_liq")
        tc = snap.get("total_cash")
        sc = snap.get("settled_cash")
        ver = v.detail.get("_ver", "")
        print(f"{snap['account']:12s} {ver:13s} "
              f"{(f'{nl:,.0f}' if nl is not None else 'n/a'):>13s} "
              f"{(f'{tc:,.0f}' if tc is not None else 'n/a'):>13s} "
              f"{(f'{sc:,.0f}' if sc is not None else 'n/a'):>13s} "
              f"{len(snap.get('fills', [])):>5d}  {v.action:9s} {v.reason}")
    print("-" * 104)


def run_cycle(snapshots: list, targets: dict, baselines: dict, earmarks_by_acct: dict,
              today: date, persist: bool = True) -> list:
    """PURE-ish orchestration over already-read snapshots: build each AccountState, decide,
    print, and (optionally) persist the new baselines. Separated from the live read so the
    SIMULATED path can reuse it with injected snapshots/baselines. Transmits nothing."""
    rows = []
    new_baselines = dict(baselines)
    for snap in snapshots:
        acct = snap["account"]
        ver = config.ENROLLMENT.get(acct, "Balanced")
        target = targets.get(ver) or next(iter(targets.values()))
        state = build_state(snap, ver, target, baselines.get(acct),
                            earmarks_by_acct.get(acct, []), today)
        verdict = mon.decide(state)
        verdict.detail["_ver"] = ver   # for the table only
        rows.append((snap, verdict))
        # Update the baseline to today's settled cash (only when we actually have one).
        if snap.get("settled_cash") is not None:
            new_baselines[acct] = {"settled_cash": snap["settled_cash"],
                                   "date": today.isoformat()}
    print_verdict_table(rows)
    if persist:
        path = save_baselines(new_baselines)
        print(f"\nBaselines persisted -> {path}")
    return rows


# --- the LIVE read-only cycle --------------------------------------------------
def main() -> tuple[int, dict]:
    print("=" * 104)
    print(f"ACCOUNT-CASHFLOW MONITOR — LIVE READ-ONLY CYCLE (propose-only, transmits "
          f"nothing)   [{version.banner()}]")
    print("=" * 104)
    print(f"connect: PAPER gateway {ibkr.HOST}:{ibkr.PAPER_PORT}  "
          f"clientId={clientids.get('paperbot_monitor')}  readonly=True")
    print("posture: reads accountValues / positions / reqExecutions ONLY. No order, no "
          "modify/cancel, NO whatIfOrder, no FA/gateway config write.")
    empty_summary: dict = {}

    today = date.today()
    baselines = load_baselines()
    earmarks_by_acct = load_earmarks()
    print(f"\nbaselines loaded: {len(baselines)} account(s) "
          f"({'cold start' if not baselines else 'have history'})")
    print(f"earmarks loaded:  {sum(len(v) for v in earmarks_by_acct.values())} active "
          f"across {len(earmarks_by_acct)} account(s)  (file: {EARMARKS_JSON})")

    # Tier models BEFORE connecting (fail fast on stale data).
    print("\n[1] Computing tier models (read-only, one per enrolled version)...")
    targets = _targets_by_version()
    for v, t in targets.items():
        print(f"    {v:13s} as_of={t.as_of.date()}  ({len(t.weights)} holdings)")

    # GATEWAY LOCK (Slice 2): acquire the single-process Gateway mutex BEFORE connecting and
    # hold it through the ENTIRE read-only session, releasing only after disconnect. The
    # monitor is automated + read-only, so it YIELDS to a human rebalance — on_busy="skip"
    # means a brief wait then a clean SKIP of this cycle (a non-event; the next cycle catches
    # up). This is the F2 interlock: the monitor can never read account state mid-rebalance.
    try:
        with gateway_lock(purpose="monitor",
                          client_id=clientids.get("paperbot_monitor"), on_busy="skip"):
            return _run_gateway_session(today, targets, baselines, earmarks_by_acct)
    except GatewayBusySkip as busy:
        holder = busy.holder or {}
        print(f"\n[2] gateway busy — held by {holder.get('purpose')} pid {holder.get('pid')} "
              f"clientId {holder.get('client_id')} since "
              f"{holder.get('acquired_at') or holder.get('acquired_ts')}; skipping this "
              f"monitor cycle. (Read-only; nothing read or transmitted. Next cycle retries.)")
        return 0, empty_summary


def _run_gateway_session(today, targets, baselines, earmarks_by_acct) -> tuple[int, dict]:
    """The connect -> read -> disconnect body, run only while the gateway lock is HELD.

    Factored out of main() so the `with gateway_lock(...)` block wraps the WHOLE session —
    connect, all reads, and disconnect — not just the connect call. The lock guarantees no
    other paperbot process operates the Gateway for the lifetime of this session."""
    # Connect READ-ONLY, bounded timeout. Watchdog deadline for the whole cycle.
    deadline = datetime.now().timestamp() + CYCLE_WATCHDOG_SECONDS
    print(f"\n[2] Connecting read-only (timeout=15s, cycle watchdog={CYCLE_WATCHDOG_SECONDS}s)...")
    try:
        ib = ibkr.connect("paperbot_monitor", readonly=True, launch=True, timeout=15)
    except Exception as exc:
        print(f"    COULD NOT CONNECT: {exc}")
        print("    -> Is IB Gateway up and logged into PAPER, API on port 4002? "
              "Reporting and exiting (Part A core + tests are unaffected).")
        return 1, {}

    try:
        managed = set(ib.managedAccounts())
        # Only enrolled CLIENT sub-accounts (DU…), never the FA master (DF…141).
        targets_accounts = [a for a in sorted(config.ENROLLMENT)
                            if a in managed and not a.startswith("DF")]
        print(f"\n[3] Reading {len(targets_accounts)} enrolled client sub-account(s) "
              f"(read-only): {', '.join(targets_accounts)}")
        if not targets_accounts:
            print("    none of the enrolled accounts are visible under the master. Done.")
            return 0, {}

        snapshots = []
        for acct in targets_accounts:
            if datetime.now().timestamp() > deadline:
                print("    ! cycle watchdog tripped — aborting reads, disconnecting.")
                return 3, {}
            snap = read_account_cycle(ib, acct, today)
            sc = snap["settled_cash"]
            tc = snap["total_cash"]
            cross = ("" if sc is None or tc is None
                     else f"  (settled-vs-total diff ${abs(sc - tc):,.0f})")
            print(f"    {acct}: NetLiq={_fmt(snap['net_liq'])}  TotalCash={_fmt(tc)}  "
                  f"SettledCashByDate={snap['settled_raw']!r}->{_fmt(sc)}  "
                  f"fills={len(snap['fills'])}{cross}")
            snapshots.append(snap)

        # NAV history: append today's per-account NetLiq snapshot to the local CSV,
        # regardless of verdict outcome (HOLD/REBALANCE/ALERT) — this is pure
        # observability, feeding the dashboard's "S0 Performance vs Model" section
        # and the EOD email's since-inception line. Never blocks/raises the cycle.
        try:
            nav_history.append_snapshot(today, snapshots)
        except Exception as exc:
            print(f"    ! nav_history.append_snapshot failed (non-fatal): {exc}")

        print("\n[4] Verdicts (pure decide() per account) — PROPOSE-ONLY:")
        rows = run_cycle(snapshots, targets, baselines, earmarks_by_acct, today, persist=True)
        verdict_summary = _summarize_verdicts(rows)

        print("\nDone. Read-only cycle complete. Nothing was transmitted; no order/whatIf; "
              "gateway left read-only.")
        return 0, verdict_summary
    finally:
        ib.disconnect()
        print("Read-only session closed.")


def _summarize_verdicts(rows: list) -> dict:
    """Roll up run_cycle()'s (snap, verdict) rows into a compact per-account + aggregate
    summary the status artifact can carry. PURE — reads only the rows it's given."""
    accounts = {}
    n_hold = n_rebalance = n_alert = 0
    for snap, v in rows:
        accounts[snap["account"]] = {"action": v.action, "reason": v.reason}
        if v.action == "HOLD":
            n_hold += 1
        elif v.action == "REBALANCE":
            n_rebalance += 1
        elif v.action == "ALERT":
            n_alert += 1
    return {"accounts": accounts, "n_hold": n_hold, "n_rebalance": n_rebalance,
            "n_alert": n_alert}


def _fmt(x) -> str:
    return "n/a" if x is None else f"{x:,.2f}"


# --- SIMULATED cycle (fixtures only — proves fence/nudge/deposit end-to-end) ----
def simulate() -> int:
    """Run the SAME run_cycle() over INJECTED fixtures (no broker). The live accounts are
    flat (0 fills), so no real fence/nudge/deposit event exists; this proves those paths
    end-to-end with synthetic snapshots, a fake prior baseline, and a simulated sale-raised
    cash delta — once WITH an earmark (fence) and once WITHOUT (nudge), plus a clean external
    deposit. NOTHING here touches the gateway or persists state (persist=False)."""
    print("=" * 104)
    print("ACCOUNT-CASHFLOW MONITOR — SIMULATED CYCLE (fixtures, NO broker, NO persist)")
    print("=" * 104)
    print("Why: the real DU accounts are flat (0 fills) so no live fence/nudge/deposit event "
          "exists. These INJECTED fixtures exercise those decide() paths end-to-end.")

    today = date.today()
    targets = _targets_by_version()
    target = targets["Balanced"]
    px = float(target.prices.iloc[0]) if len(target.prices) else 100.0
    sym = str(target.weights.index[0]) if len(target.weights) else "SPY"
    nav = 1_000_000.0
    invest = mon.reconcile._investable.compute_investable(nav, 0.0)
    on_tgt = int(invest // px)
    raised = round(0.06 * nav, 2)           # a 6% cash jump (clears both deposit guards)
    sold_shares = int(raised // px)
    sold_down = on_tgt - sold_shares        # the book after selling to raise the cash

    # A fake PRIOR baseline: yesterday each account had `nav*0.05` settled cash.
    base_cash = round(0.05 * nav, 2)
    baselines = {a: {"settled_cash": base_cash, "date": "2026-06-29"}
                 for a in config.ENROLLMENT}
    cur_cash = base_cash + raised           # today's cash after the +raised jump

    sld = mon.Execution(symbol=sym, side="SLD", shares=sold_shares, price=px)

    # Three injected accounts, one scenario each (using real enrolled account numbers).
    accts = sorted(config.ENROLLMENT)
    snapshots = [
        # (A) SALE-RAISED + EARMARKED -> FENCE: WITHDRAWAL_EARMARK_RESERVED, no rebalance.
        {"account": accts[0], "net_liq": nav, "total_cash": cur_cash,
         "settled_cash": cur_cash, "settled_date": today, "settled_raw": f"{today:%Y%m%d}:{cur_cash}",
         "positions": {sym: sold_down}, "fills": [sld]},
        # (B) SALE-RAISED + NOT earmarked -> NUDGE: SALE_RAISED_UNEARMARKED, no rebalance.
        {"account": accts[1], "net_liq": nav, "total_cash": cur_cash,
         "settled_cash": cur_cash, "settled_date": today, "settled_raw": f"{today:%Y%m%d}:{cur_cash}",
         "positions": {sym: sold_down}, "fills": [sld]},
        # (C) EXTERNAL DEPOSIT (no fill, no earmark) -> DEPOSIT_ARRIVED.
        {"account": accts[2], "net_liq": nav, "total_cash": cur_cash,
         "settled_cash": cur_cash, "settled_date": today, "settled_raw": f"{today:%Y%m%d}:{cur_cash}",
         "positions": {sym: on_tgt}, "fills": []},
    ]
    earmarks_by_acct = {
        accts[0]: [mon.Earmark(account=accts[0], amount=raised,
                               note="SIMULATED ad-hoc client withdrawal")],
    }

    print(f"\nfixtures: NAV ${nav:,.0f}  on-target {on_tgt} {sym}@{px:.2f}  "
          f"raised ${raised:,.0f} (sold {sold_shares} -> book {sold_down})")
    print(f"  (A) {accts[0]}: sale-raised + EARMARK ${raised:,.0f}  -> expect FENCE "
          "(WITHDRAWAL_EARMARK_RESERVED)")
    print(f"  (B) {accts[1]}: sale-raised + NO earmark           -> expect NUDGE "
          "(SALE_RAISED_UNEARMARKED)")
    print(f"  (C) {accts[2]}: external deposit (no fill/earmark)  -> expect DEPOSIT_ARRIVED")

    print("\n[SIM] Verdicts (pure decide() per fixture) — PROPOSE-ONLY, NOT persisted:")
    run_cycle(snapshots, targets, baselines, earmarks_by_acct, today, persist=False)
    print("\nSimulated cycle complete. No broker contact, no state written, nothing "
          "transmitted.")
    return 0


def _run_with_status() -> int:
    """Run the daily cycle and write an 'account_monitor' status artifact reflecting BOTH
    cycle health AND real per-account drift (rc==0 + verdicts -> ok/fail by verdict;
    non-zero rc or an exception -> fail). The status write is best-effort and never changes
    the return code / never raises.

    Status severity used to track ONLY whether the cycle ran cleanly (rc==0 -> "ok"), never
    the actual per-account verdicts decide() returned — so a week of live ALERT/
    UNTRACKED_POSITION verdicts on every account (2026-07-01 -> 2026-07-07) still showed
    "ok" in the nightly EOD email. Now: any ALERT verdict marks the day "fail" (a human
    needs to look), a REBALANCE-only day stays "ok" but carries the summary in metrics, and
    an all-HOLD day is "ok" as before."""
    try:
        rc, verdict_summary = main()
    except Exception as e:
        _write_monitor_status("fail", metrics={"rc": None},
                              message=f"cycle raised {type(e).__name__}: {e}")
        raise
    if rc == 0:
        n_alert = verdict_summary.get("n_alert", 0)
        n_rebalance = verdict_summary.get("n_rebalance", 0)
        if n_alert > 0:
            alert_accts = [a for a, v in verdict_summary.get("accounts", {}).items()
                           if v.get("action") == "ALERT"]
            _write_monitor_status(
                "fail", metrics={"rc": rc, **verdict_summary},
                message=(f"ALERT: {n_alert} account(s) drifted from S0 target — "
                         f"{', '.join(alert_accts)}"))
        elif n_rebalance > 0:
            _write_monitor_status(
                "ok", metrics={"rc": rc, **verdict_summary},
                message=(f"read-only monitor cycle completed; {n_rebalance} account(s) "
                         f"propose REBALANCE (no ALERT)"))
        else:
            _write_monitor_status(
                "ok", metrics={"rc": rc, **verdict_summary},
                message="read-only monitor cycle completed (or cleanly skipped)")
    else:
        _write_monitor_status("fail", metrics={"rc": rc},
                              message=f"monitor cycle returned non-zero rc={rc}")
    return rc


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--simulate":
        sys.exit(simulate())
    sys.exit(_run_with_status())
