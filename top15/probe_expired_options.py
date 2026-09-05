"""
probe_expired_options.py — S9 (Top-15 synthetic equity) DATA-FEASIBILITY PROBE.

THE QUESTION: can IBKR's API serve usable HISTORICAL price data for EXPIRED
single-name US equity option contracts — long-dated LEAP calls — back to
September 2021? The S9 spec's original vendor (ThetaData) is retired and
unrecoverable; per standing IBKR-FIRST policy, IBKR is the first fallback tested.

READ-ONLY BY CONSTRUCTION. This file only ever calls reqContractDetails,
reqSecDefOptParams and reqHistoricalData. There is no order path anywhere in it:
no placeOrder, no whatIfOrder, no cancelOrder, no bracket/arming helper. It
connects through connections.ibkr_live_data.connect(), whose connect() has no
`readonly` parameter at all and always connects read-only to the port-4001
live-DATA Gateway. It never touches port 4002 (paper) or port 4003 (live trade).
It never launches, kills, or restarts a Gateway (launch=False).

EVERY IBKR error is logged VERBATIM via ib.errorEvent — code, message, and the
contract it was about — because the whole point of this probe is to distinguish
three failure modes that lead to completely different decisions:

  (a) NOT SUBSCRIBED  — an entitlement problem (codes like 354 / 10089 / 10090,
      or 162 whose text mentions a subscription). The data exists; we lack rights.
  (b) NOT RETAINED    — IBKR resolves the contract but returns no bars, or an
      empty/"HMDS query returned no data" style 162. The data is simply gone.
  (c) NOT RESOLVABLE  — error 200 "No security definition has been found for the
      request". The contract DEFINITION itself is gone from IBKR's database, so
      there is nothing to even ask for history about.

Never report one of those as another.

PACING: IBKR allows roughly 60 historical-data requests per 10 minutes. Every
reqHistoricalData here is followed by PACE_SECS of sleep and the total request
count is capped by MAX_HIST_REQUESTS, which aborts the run rather than pace-
violating. Raw results stream to a JSON file under C:\\TradingDesk-Local\\top15\\
after every step, so nothing is lost if a later step fails.

Run:  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe top15\\probe_expired_options.py
      (from C:\\TradingDesk so the `connections` package imports)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import traceback

from ib_async import IB, Option, Stock

from connections import ibkr_live_data

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
CONSUMER = "top15_option_history_probe"          # registered in connections.clientids (67)
OUT_DIR = r"C:\TradingDesk-Local\top15"
OUT_JSON = os.path.join(OUT_DIR, "probe_expired_options_results.json")

PACE_SECS = 11          # >10s between historical requests: ~5.5/min, well inside 60/10min
MAX_HIST_REQUESTS = 45  # hard cap; abort rather than risk a pacing violation
HIST_TIMEOUT = 60

TODAY = dt.date.today()

# Live/unexpired control target: a real listed LEAP expiry ~1.5-2.5y out.
CONTROL_DTE_LO, CONTROL_DTE_HI = 540, 910

# Expired LEAP call expiries to probe for resolution (Step 1) — third-Friday
# January expiries, the standard LEAP cycle.
EXPIRED_TARGETS = [
    ("2026-01-16", "~Jan 2026 (about 8 months expired)"),
    ("2024-01-19", "~Jan 2024 (about 20 months expired)"),
    ("2023-01-20", "~Jan 2023 (about 32 months expired)"),
]
# Step 3 boundary-narrowing expiries (only used if Step 1/2 show a cutoff).
BOUNDARY_TARGETS = [
    ("2026-06-18", "~Jun 2026 (about 2.5 months expired)"),
    ("2025-06-20", "~Jun 2025 (about 14.5 months expired)"),
    ("2025-01-17", "~Jan 2025 (about 20 months expired)"),
]
WHAT_TO_SHOW = ["MIDPOINT", "BID", "ASK", "TRADES"]

RESULTS: dict = {
    "probe": "S9 expired-option history feasibility",
    "run_started_utc": dt.datetime.utcnow().isoformat() + "Z",
    "today": TODAY.isoformat(),
    "port": ibkr_live_data.LIVE_DATA_PORT,
    "consumer": CONSUMER,
    "errors_verbatim": [],   # every ib.errorEvent, in order
    "steps": {},
    "hist_requests_made": 0,
    "aborted": None,
}

# Live ib_async Contract objects, kept out of RESULTS (not JSON), so later steps
# re-request history against the EXACT contract IBKR returned.
CONTROL_CONTRACT: list = []
NEAR_CONTRACT: list = []
CONTRACT_BY_CONID: dict = {}


def save() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, indent=2, default=str)


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Verbatim error capture
# --------------------------------------------------------------------------
def _on_error(reqId, errorCode, errorString, contract):
    rec = {
        "ts": dt.datetime.now().isoformat(),
        "reqId": reqId,
        "code": errorCode,
        "message": errorString,        # VERBATIM, never paraphrased
        "contract": (f"{contract.symbol} {contract.lastTradeDateOrContractMonth} "
                     f"{contract.strike} {contract.right} {contract.exchange}")
        if contract else None,
    }
    RESULTS["errors_verbatim"].append(rec)
    log(f"  IBKR error code={errorCode} reqId={reqId} :: {errorString}"
        + (f"  [{rec['contract']}]" if rec["contract"] else ""))


def errors_since(idx: int) -> list:
    """Every verbatim error recorded after list index `idx`."""
    return RESULTS["errors_verbatim"][idx:]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def hist(ib: IB, contract, *, end: str, duration: str, what: str) -> dict:
    """One paced reqHistoricalData. Returns a result record, never raises."""
    if RESULTS["hist_requests_made"] >= MAX_HIST_REQUESTS:
        RESULTS["aborted"] = "hit MAX_HIST_REQUESTS pacing cap"
        return {"whatToShow": what, "skipped": "pacing cap reached"}
    mark = len(RESULTS["errors_verbatim"])
    RESULTS["hist_requests_made"] += 1
    rec: dict = {"whatToShow": what, "endDateTime": end, "durationStr": duration}
    try:
        bars = ib.reqHistoricalData(
            contract, endDateTime=end, durationStr=duration,
            barSizeSetting="1 day", whatToShow=what, useRTH=True,
            formatDate=1, keepUpToDate=False, timeout=HIST_TIMEOUT,
        )
        rec["bar_count"] = len(bars)
        if bars:
            rec["first_bar_date"] = str(bars[0].date)
            rec["last_bar_date"] = str(bars[-1].date)
            rec["first_bar"] = {"date": str(bars[0].date), "open": bars[0].open,
                                "high": bars[0].high, "low": bars[0].low,
                                "close": bars[0].close, "volume": bars[0].volume}
            rec["last_bar"] = {"date": str(bars[-1].date), "open": bars[-1].open,
                               "high": bars[-1].high, "low": bars[-1].low,
                               "close": bars[-1].close, "volume": bars[-1].volume}
    except Exception as e:
        rec["exception"] = f"{type(e).__name__}: {e}"
        rec["bar_count"] = 0
    rec["errors"] = errors_since(mark)
    log(f"    {what}: bars={rec.get('bar_count')} "
        f"{rec.get('first_bar_date','')} -> {rec.get('last_bar_date','')}")
    save()
    time.sleep(PACE_SECS)
    return rec


def resolve(ib: IB, contract, label: str) -> tuple[dict, list]:
    """One reqContractDetails.

    Returns (record, resolved_contract_objects). The contract OBJECTS matter: any
    later reqHistoricalData is issued against the exact contract IBKR itself
    returned (conId, real exchange, real tradingClass), so a "no data" answer can
    never be an artifact of us re-guessing an ambiguous contract. Never raises.
    """
    mark = len(RESULTS["errors_verbatim"])
    rec: dict = {
        "label": label,
        "requested": {
            "symbol": contract.symbol,
            "lastTradeDateOrContractMonth": contract.lastTradeDateOrContractMonth,
            "strike": contract.strike, "right": contract.right,
            "exchange": contract.exchange,
            "includeExpired": getattr(contract, "includeExpired", None),
        },
    }
    objs: list = []
    try:
        cds = ib.reqContractDetails(contract)
        objs = [cd.contract for cd in cds]
        rec["n_resolved"] = len(cds)
        rec["resolved"] = len(cds) > 0
        rec["details"] = [
            {"conId": c.conId, "localSymbol": c.localSymbol,
             "expiry": c.lastTradeDateOrContractMonth,
             "strike": c.strike, "right": c.right,
             "exchange": c.exchange, "multiplier": c.multiplier,
             "tradingClass": c.tradingClass}
            for c in objs[:12]
        ]
        rec["all_strikes"] = sorted({c.strike for c in objs})
        if cds:
            rec["validExchanges"] = cds[0].validExchanges
    except Exception as e:
        rec["exception"] = f"{type(e).__name__}: {e}"
        rec["resolved"] = False
        rec["n_resolved"] = 0
    rec["errors"] = errors_since(mark)
    log(f"  resolve {label}: resolved={rec.get('resolved')} n={rec.get('n_resolved')}")
    save()
    time.sleep(1.5)
    return rec, objs


def underlying_close_on(ib: IB, symbol: str, on: dt.date) -> float | None:
    """Underlying daily close near `on`, used to pick a plausible ITM LEAP strike."""
    stk = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(stk)
    end = on.strftime("%Y%m%d") + " 23:59:59 US/Eastern"
    rec = hist(ib, stk, end=end, duration="5 D", what="TRADES")
    if rec.get("bar_count"):
        return rec["last_bar"]["close"]
    return None


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------
def step0_control(ib: IB) -> bool:
    """Prove plumbing + entitlement on a LIVE, UNEXPIRED long-dated AAPL call."""
    log("STEP 0 — control: live unexpired long-dated AAPL call")
    out: dict = {}
    RESULTS["steps"]["step0_control"] = out

    mark = len(RESULTS["errors_verbatim"])
    stk = Stock("AAPL", "SMART", "USD")
    ib.qualifyContracts(stk)
    out["underlying_conId"] = stk.conId

    # Discover REAL listed expiries + strikes; never guess.
    params = ib.reqSecDefOptParams(stk.symbol, "", stk.secType, stk.conId)
    out["secdefoptparams_errors"] = errors_since(mark)
    smart = [p for p in params if p.exchange == "SMART"] or list(params)
    if not smart:
        out["fatal"] = "reqSecDefOptParams returned nothing for AAPL"
        save()
        return False
    p = smart[0]
    expiries = sorted(p.expirations)
    strikes = sorted(p.strikes)
    out["n_expiries_listed"] = len(expiries)
    out["n_strikes_listed"] = len(strikes)

    # Pick a real listed expiry ~1.5-2.5y out.
    cands = []
    for e in expiries:
        d = dt.datetime.strptime(e, "%Y%m%d").date()
        dte = (d - TODAY).days
        if CONTROL_DTE_LO <= dte <= CONTROL_DTE_HI:
            cands.append((dte, e))
    if not cands:
        out["fatal"] = (f"no listed AAPL expiry between {CONTROL_DTE_LO} and "
                        f"{CONTROL_DTE_HI} DTE; listed expiries: {expiries[-8:]}")
        save()
        return False
    cands.sort()
    dte, expiry = cands[len(cands) // 2]
    out["chosen_expiry"] = expiry
    out["chosen_dte"] = dte
    log(f"  chosen live expiry {expiry} (DTE {dte})")

    spot = underlying_close_on(ib, "AAPL", TODAY)
    out["underlying_close"] = spot
    if spot is None:
        out["fatal"] = "could not read AAPL underlying close"
        save()
        return False
    # High-delta (deep ITM) call: strike ~75% of spot, snapped to a listed strike.
    target = spot * 0.75
    strike = min(strikes, key=lambda s: abs(s - target))
    out["chosen_strike"] = strike
    log(f"  AAPL close {spot}; target strike {target:.1f} -> listed {strike}")

    con = Option("AAPL", expiry, strike, "C", "SMART", currency="USD")
    rec, objs = resolve(ib, con, f"LIVE AAPL {expiry} {strike}C")
    out["resolution"] = rec
    if not rec.get("resolved"):
        out["fatal"] = "live/unexpired control contract did not even resolve"
        save()
        return False
    con = objs[0]                      # the exact contract IBKR returned
    CONTROL_CONTRACT.append(con)
    out["control_conId"] = con.conId
    out["control_localSymbol"] = con.localSymbol

    out["history"] = {}
    for what in WHAT_TO_SHOW:
        out["history"][what] = hist(ib, con, end="", duration="1 Y", what=what)
    save()

    ok = any(out["history"][w].get("bar_count", 0) > 0 for w in WHAT_TO_SHOW)
    out["control_passed"] = ok
    log(f"STEP 0 control_passed={ok}")
    return ok


def step0b_diagnose(ib: IB) -> bool:
    """Step 0 failed. Find out WHY before blaming entitlement.

    The Step-0 failure signature was error 162 naming an EXCHANGE ('BEST'), not a
    subscription. That could be any of three very different things, and calling it
    "entitlement" without testing would be a guess:

      1. SMART routing: IBKR maps SMART -> 'BEST' for options and the EOD-chart
         service may want a REAL exchange (CBOE/AMEX/...) instead.
      2. Contract liquidity: a Dec-2028 deep-ITM LEAP may simply have no EOD chart
         because almost nothing ever traded on it — which would say nothing at all
         about whether option history works in general.
      3. A genuine OPRA entitlement gap on this live-data login.

    So: vary the exchange, vary the contract to a liquid near-term one, vary the
    duration, and separately ask for a live/frozen market-data snapshot — a real
    entitlement gap announces itself with 354 / 10089 / 10090 / 10167, not with a
    message about an exchange. Returns True if ANY combination returned bars.
    """
    log("STEP 0b — diagnosing the Step-0 failure (exchange vs liquidity vs entitlement)")
    out: dict = {}
    RESULTS["steps"]["step0b_diagnosis"] = out
    ctrl = RESULTS["steps"]["step0_control"]
    got_any = False

    # --- (1) a LIQUID NEAR-TERM ATM AAPL call, on SMART -----------------------
    # If the LEAP's problem is "nothing ever traded on it", this one will work.
    spot = ctrl.get("underlying_close")
    near: dict = {}
    out["near_term_atm_smart"] = near
    stk = Stock("AAPL", "SMART", "USD")
    ib.qualifyContracts(stk)
    params = ib.reqSecDefOptParams(stk.symbol, "", stk.secType, stk.conId)
    smart = [p for p in params if p.exchange == "SMART"] or list(params)
    p = smart[0]
    near_exp = None
    for e in sorted(p.expirations):
        d = dt.datetime.strptime(e, "%Y%m%d").date()
        if 20 <= (d - TODAY).days <= 60:
            near_exp = e
            break
    near["chosen_expiry"] = near_exp
    if near_exp and spot:
        k = min(sorted(p.strikes), key=lambda s: abs(s - spot))
        near["chosen_strike"] = k
        rec, objs = resolve(ib, Option("AAPL", near_exp, k, "C", "SMART", currency="USD"),
                            f"LIVE near-term AAPL {near_exp} {k}C")
        near["resolution"] = {kk: vv for kk, vv in rec.items() if kk != "all_strikes"}
        if objs:
            NEAR_CONTRACT.append(objs[0])
            near["history"] = {w: hist(ib, objs[0], end="", duration="1 M", what=w)
                               for w in ("TRADES", "MIDPOINT")}
            if any(v.get("bar_count", 0) > 0 for v in near["history"].values()):
                got_any = True

    # --- (2) the SAME LEAP, on REAL exchanges instead of SMART ----------------
    ex_out: dict = {}
    out["leap_by_exchange"] = ex_out
    valid = (ctrl.get("resolution", {}).get("validExchanges") or "")
    out["validExchanges_for_leap"] = valid
    tried = [e for e in ("CBOE", "AMEX", "PHLX", "ISE", "BOX", "NASDAQOM", "MIAX")
             if e in valid][:4]
    out["exchanges_tried"] = tried
    if CONTROL_CONTRACT and tried:
        base = CONTROL_CONTRACT[0]
        for ex in tried:
            if RESULTS["hist_requests_made"] >= MAX_HIST_REQUESTS:
                break
            c = Option("AAPL", base.lastTradeDateOrContractMonth, base.strike,
                       "C", ex, currency="USD")
            c.tradingClass = base.tradingClass
            log(f"  LEAP on exchange {ex}")
            ex_out[ex] = hist(ib, c, end="", duration="1 M", what="TRADES")
            if ex_out[ex].get("bar_count", 0) > 0:
                got_any = True

    # --- (3) the NEAR-TERM contract on a real exchange too -------------------
    if NEAR_CONTRACT and tried and RESULTS["hist_requests_made"] < MAX_HIST_REQUESTS:
        b = NEAR_CONTRACT[0]
        c = Option("AAPL", b.lastTradeDateOrContractMonth, b.strike, "C",
                   tried[0], currency="USD")
        c.tradingClass = b.tradingClass
        log(f"  near-term on exchange {tried[0]}")
        out["near_term_on_real_exchange"] = hist(ib, c, end="", duration="1 M",
                                                 what="TRADES")
        if out["near_term_on_real_exchange"].get("bar_count", 0) > 0:
            got_any = True

    # --- (4) ENTITLEMENT, asked directly -------------------------------------
    # A market-data snapshot is the clean discriminator: a missing OPRA
    # subscription answers with 354 / 10089 / 10090 / 10167, which no amount of
    # exchange-fiddling would fix. Read-only: reqMktData/cancelMktData only.
    ent: dict = {}
    out["entitlement_snapshot"] = ent
    target = (NEAR_CONTRACT or CONTROL_CONTRACT)
    if target:
        for md_type, name in ((1, "live"), (2, "frozen"), (3, "delayed")):
            mark = len(RESULTS["errors_verbatim"])
            try:
                ib.reqMarketDataType(md_type)
                t = ib.reqMktData(target[0], "", True, False)
                ib.sleep(6)
                ent[name] = {
                    "bid": t.bid, "ask": t.ask, "last": t.last, "close": t.close,
                    "modelGreeks_delta": (t.modelGreeks.delta if t.modelGreeks else None),
                    "modelGreeks_iv": (t.modelGreeks.impliedVol if t.modelGreeks else None),
                }
                ib.cancelMktData(target[0])
            except Exception as e:
                ent[name] = {"exception": f"{type(e).__name__}: {e}"}
            ent[name]["errors"] = errors_since(mark)
            log(f"  market-data snapshot ({name}): {ent[name]}")
            save()

    out["any_option_history_returned"] = got_any
    save()
    return got_any


def step0c_delayed_history(ib: IB) -> bool:
    """Step 0b proved the LIVE option subscription is missing (354/10091/10167),
    and that DELAYED option data IS available. So ask the obvious next question:
    does asking for delayed market data also unlock reqHistoricalData for options?

    This matters because the desk's existing nightly IBKR option EOD feed
    (datacollector/ibkr_forward.py) runs on exactly this trick — reqMarketDataType(3)
    then reqMktData snapshots — so if historical honoured the delayed mode too, the
    entitlement gap would not be fatal. Returns True if any bars came back.
    """
    log("STEP 0c — does DELAYED market data unlock historical option bars?")
    out: dict = {}
    RESULTS["steps"]["step0c_delayed_history"] = out
    got = False
    targets = []
    if NEAR_CONTRACT:
        targets.append(("near_term", NEAR_CONTRACT[0]))
    if CONTROL_CONTRACT:
        targets.append(("leap", CONTROL_CONTRACT[0]))
    for md_type, name in ((3, "delayed"), (4, "delayed_frozen")):
        ib.reqMarketDataType(md_type)
        for tag, con in targets:
            if RESULTS["hist_requests_made"] >= MAX_HIST_REQUESTS:
                break
            log(f"  marketDataType={md_type} ({name}), {tag}")
            r = hist(ib, con, end="", duration="1 M", what="TRADES")
            out[f"{name}_{tag}_TRADES"] = r
            if r.get("bar_count", 0) > 0:
                got = True
    ib.reqMarketDataType(1)          # restore the default for anything after us
    out["any_bars"] = got
    save()
    return got


def step1_resolution_only(ib: IB) -> dict:
    """Expired-contract RESOLUTION, tested even though history is blocked.

    reqContractDetails needs NO market-data subscription — it reads IBKR's contract
    database, not the quote feed. So even with the entitlement wall of Step 0b in
    place, this half of the question is still honestly answerable: does IBKR still
    HOLD the contract definitions for expired 2021-2026 LEAP calls, or are they
    gone (error 200)? Answering it now means that if the entitlement gap is ever
    closed, we already know whether there is anything there to ask about.

    Costs zero historical-data requests, so it cannot cause a pacing violation.
    """
    log("STEP 1 (resolution only) — do EXPIRED LEAP contract definitions survive?")
    out: dict = {}
    RESULTS["steps"]["step1_resolution_only"] = out
    expiries = [
        ("20260116", "Jan 2026 — about 8 months expired"),
        ("20250117", "Jan 2025 — about 20 months expired"),
        ("20240119", "Jan 2024 — about 20 months expired"),
        ("20230120", "Jan 2023 — about 32 months expired"),
        ("20220121", "Jan 2022 — about 44 months expired"),
    ]
    for sym in ("AAPL", "MSFT", "NVDA"):
        rows = []
        out[sym] = rows
        for expiry, label in expiries:
            c = Option(sym, expiry, 0, "C", "SMART", currency="USD")
            c.includeExpired = True
            rec, objs = resolve(ib, c, f"{sym} {expiry} ALL strikes (includeExpired)")
            strikes = rec.get("all_strikes") or []
            rows.append({
                "expiry": expiry, "label": label,
                "resolved": rec.get("resolved"),
                "n_contracts": rec.get("n_resolved"),
                "strike_min": strikes[0] if strikes else None,
                "strike_max": strikes[-1] if strikes else None,
                "errors": rec.get("errors"),
                "verdict": ("RESOLVED — definition retained"
                            if rec.get("resolved")
                            else "NOT RESOLVABLE — definition gone (see errors)"),
            })
            save()
    return out


def step1b_specific_expired(ib: IB) -> dict:
    """Step 1 used a WILDCARD strike (strike=0) with includeExpired=True and got
    error 200 every time. That is suggestive but NOT yet conclusive, because IBKR's
    includeExpired handling is documented as fussy: it is specified to work on a
    FULLY-QUALIFIED contract (exact expiry + strike + right), and a wildcard query
    is exactly the shape most likely to be rejected for its own reasons.

    So re-ask the question the way IBKR wants it asked: real, fully-specified
    strikes that certainly existed on those dates, plus a contract that expired only
    days ago (if includeExpired works at all on this login, THAT one must resolve).
    Only then is a 200 safe to read as "the definition is gone".

    Costs zero historical-data requests — contract details only — so it cannot
    cause a pacing violation.
    """
    log("STEP 1b — expired resolution with FULLY-SPECIFIED strikes")
    out: dict = {}
    RESULTS["steps"]["step1b_specific_expired"] = out
    # (expiry, [strikes that were plausibly listed and near the money then], note)
    cases = [
        ("20260904", [320.0, 300.0], "AAPL, expired 1 day ago (weekly)"),
        ("20260821", [300.0, 280.0], "AAPL, expired ~2 weeks ago (monthly)"),
        ("20260619", [250.0, 220.0], "AAPL, expired ~2.5 months ago (monthly)"),
        ("20260116", [250.0, 200.0], "AAPL, expired ~8 months ago (LEAP-cycle Jan)"),
        ("20240119", [150.0, 190.0], "AAPL, expired ~20 months ago"),
        ("20230120", [130.0, 150.0], "AAPL, expired ~32 months ago"),
        ("20220121", [150.0, 170.0], "AAPL, expired ~44 months ago"),
    ]
    for expiry, strikes, note in cases:
        rows = []
        out[expiry] = {"note": note, "attempts": rows}
        for strike in strikes:
            for exch in ("SMART", ""):
                c = Option("AAPL", expiry, strike, "C", exch, currency="USD")
                c.includeExpired = True
                c.tradingClass = "AAPL"
                rec, objs = resolve(
                    ib, c, f"AAPL {expiry} {strike}C exch={exch or '(blank)'}")
                rows.append({
                    "strike": strike, "exchange": exch or "(blank)",
                    "resolved": rec.get("resolved"),
                    "n": rec.get("n_resolved"),
                    "conId": (objs[0].conId if objs else None),
                    "errors": rec.get("errors"),
                })
                save()
                if objs:      # one success is enough for this expiry
                    break
            if any(r["resolved"] for r in rows):
                break
        out[expiry]["any_resolved"] = any(r["resolved"] for r in rows)
    # The load-bearing control: if even a 1-day-expired contract will not resolve,
    # includeExpired is not functioning on this login and NOTHING here can be read
    # as evidence about long-term contract-definition retention.
    out["includeExpired_works_at_all"] = any(
        v.get("any_resolved") for k, v in out.items() if isinstance(v, dict))
    save()
    log(f"STEP 1b includeExpired_works_at_all="
        f"{out['includeExpired_works_at_all']}")
    return out


def step5_greeks_iv_only(ib: IB) -> dict:
    """Step 5 asked standalone: are historical GREEKS or IMPLIED VOL retrievable?

    The main flow never reaches Step 5 because Step 0's control fails first, but the
    question is answerable on its own and worth an explicit, recorded answer rather
    than an assumption. Two things are tested on a LIVE, resolvable contract:
      - reqHistoricalData whatToShow=OPTION_IMPLIED_VOLATILITY (the only historical
        volatility series the TWS API exposes for an option), and BID_ASK;
      - whether a live market-data snapshot returns modelGreeks at all.
    Everything is recorded verbatim.
    """
    log("STEP 5 (standalone) — historical greeks / implied vol")
    out: dict = {}
    RESULTS["steps"]["step5_greeks_iv"] = out
    out["api_surface_note"] = (
        "reqHistoricalData is the ONLY historical bar source in the TWS API. For an "
        "OPTION its whatToShow accepts TRADES, MIDPOINT, BID, ASK, BID_ASK and "
        "OPTION_IMPLIED_VOLATILITY — all of which are BARS (OHLC). There is no "
        "historical-greeks endpoint: delta/gamma/theta/vega/rho arrive only on the "
        "LIVE streaming tickOptionComputation path (reqMktData genericTick 106 / "
        "modelGreeks), which cannot be asked for a past date and cannot be asked at "
        "all for a contract that no longer resolves.")
    stk = Stock("AAPL", "SMART", "USD")
    ib.qualifyContracts(stk)
    params = ib.reqSecDefOptParams(stk.symbol, "", stk.secType, stk.conId)
    p = ([x for x in params if x.exchange == "SMART"] or list(params))[0]
    exp = next((e for e in sorted(p.expirations)
                if 20 <= (dt.datetime.strptime(e, "%Y%m%d").date() - TODAY).days <= 60),
               None)
    out["expiry"] = exp
    if not exp:
        out["fatal"] = "no near-term expiry found"
        save()
        return out
    k = min(sorted(p.strikes), key=lambda s: abs(s - 320.0))
    out["strike"] = k
    rec, objs = resolve(ib, Option("AAPL", exp, k, "C", "SMART", currency="USD"),
                        f"LIVE AAPL {exp} {k}C (step5)")
    if not objs:
        out["fatal"] = "control contract did not resolve"
        save()
        return out
    con = objs[0]
    for what in ("OPTION_IMPLIED_VOLATILITY", "BID_ASK"):
        out[what] = hist(ib, con, end="", duration="1 M", what=what)
    mark = len(RESULTS["errors_verbatim"])
    ib.reqMarketDataType(1)
    t = ib.reqMktData(con, "100,101,106", True, False)   # 106 = option greeks
    ib.sleep(8)
    out["live_snapshot_modelGreeks"] = {
        "delta": (t.modelGreeks.delta if t.modelGreeks else None),
        "impliedVol": (t.modelGreeks.impliedVol if t.modelGreeks else None),
        "bid": t.bid, "ask": t.ask,
    }
    try:
        ib.cancelMktData(con)
    except Exception:
        pass
    out["live_snapshot_errors"] = errors_since(mark)
    save()
    log(f"STEP 5 IV bars={out['OPTION_IMPLIED_VOLATILITY'].get('bar_count')} "
        f"BID_ASK bars={out['BID_ASK'].get('bar_count')} "
        f"greeks={out['live_snapshot_modelGreeks']}")
    return out


def step1c_definition_boundary(ib: IB) -> dict:
    """Pin the contract-DEFINITION retention boundary.

    Step 1b showed the extremes: an expiry from YESTERDAY resolves under
    includeExpired=True, one from ~2 weeks ago already returns 200. Walk the
    weekly expiries between them to find where it actually falls, using strikes
    close to today's spot so "the strike was never listed" cannot be confused with
    "the definition is gone". Contract details only; no historical requests.
    """
    log("STEP 1c — pinning the contract-definition retention boundary")
    out: dict = {}
    RESULTS["steps"]["step1c_definition_boundary"] = out
    spot = 320.0     # AAPL close read in Step 0; near-spot strikes are certainly listed
    expiries = ["20260904", "20260828", "20260821", "20260814",
                "20260807", "20260731", "20260717", "20260703"]
    for expiry in expiries:
        row = {"attempts": []}
        out[expiry] = row
        for strike in (spot, spot - 20):
            c = Option("AAPL", expiry, strike, "C", "SMART", currency="USD")
            c.includeExpired = True
            c.tradingClass = "AAPL"
            rec, objs = resolve(ib, c, f"AAPL {expiry} {strike}C")
            row["attempts"].append({
                "strike": strike, "resolved": rec.get("resolved"),
                "conId": (objs[0].conId if objs else None),
                "errors": rec.get("errors"),
            })
            save()
            if objs:
                break
        row["resolved"] = any(a["resolved"] for a in row["attempts"])
    resolved = [e for e in expiries if out[e]["resolved"]]
    gone = [e for e in expiries if not out[e]["resolved"]]
    out["oldest_expiry_that_resolves"] = min(resolved) if resolved else None
    out["newest_expiry_that_is_gone"] = max(gone) if gone else None
    save()
    log(f"STEP 1c oldest resolving={out['oldest_expiry_that_resolves']} "
        f"newest gone={out['newest_expiry_that_is_gone']}")
    return out


def probe_expiry(ib: IB, symbol: str, expiry: str, label: str,
                 spot_hint: float | None, *, full_history: bool) -> dict:
    """Resolve one expired LEAP call and, if it resolves, pull history for it."""
    exp_d = dt.datetime.strptime(expiry, "%Y%m%d").date()
    out: dict = {"symbol": symbol, "expiry": expiry, "label": label}

    # First ask WHAT STRIKES EXIST for that expired expiry (strike=0 wildcard),
    # so a "no data" answer can never be an artifact of a guessed strike.
    wild = Option(symbol, expiry, 0, "C", "SMART", currency="USD")
    wild.includeExpired = True
    wide, objs = resolve(ib, wild, f"{symbol} {expiry} ALL strikes (includeExpired)")
    out["wildcard_resolution"] = {k: v for k, v in wide.items()
                                  if k not in ("details", "all_strikes")}
    out["wildcard_n"] = wide.get("n_resolved", 0)
    all_strikes = wide.get("all_strikes") or []
    out["wildcard_strike_range"] = ([all_strikes[0], all_strikes[-1]]
                                    if all_strikes else None)

    if not wide.get("resolved"):
        out["verdict"] = "NOT RESOLVABLE (contract definition unavailable)"
        save()
        return out

    # Pick a specific deep-ITM strike that IBKR itself just told us exists, and
    # use IBKR's OWN contract object for it.
    if spot_hint and all_strikes:
        target = spot_hint * 0.75
        strike = min(all_strikes, key=lambda s: abs(s - target))
    else:
        strike = all_strikes[len(all_strikes) // 2] if all_strikes else None
    out["chosen_strike"] = strike
    if strike is None:
        out["verdict"] = "RESOLVED but no strike list returned"
        save()
        return out

    matches = [c for c in objs if c.strike == strike and c.right == "C"]
    if not matches:
        out["verdict"] = "RESOLVED but chosen strike missing from returned set"
        save()
        return out
    con = matches[0]
    out["conId"] = con.conId
    out["localSymbol"] = con.localSymbol
    out["resolved_exchange"] = con.exchange
    out["resolved_tradingClass"] = con.tradingClass
    CONTRACT_BY_CONID[con.conId] = con

    # History ending AT expiry. Duration long enough to reach back to listing.
    end = exp_d.strftime("%Y%m%d") + " 23:59:59 US/Eastern"
    whats = WHAT_TO_SHOW if full_history else ["MIDPOINT", "BID"]
    out["history"] = {}
    for what in whats:
        out["history"][what] = hist(ib, con, end=end, duration="1 Y", what=what)

    got = {w: out["history"][w].get("bar_count", 0) for w in out["history"]}
    out["bar_counts"] = got
    if any(v > 0 for v in got.values()):
        out["verdict"] = "RESOLVED + HISTORY RETURNED"
    else:
        out["verdict"] = "RESOLVED but NO HISTORY (retention)"
    save()
    return out


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    log(f"connecting READ-ONLY to live-data Gateway port "
        f"{ibkr_live_data.LIVE_DATA_PORT} as {CONSUMER}")
    ib = ibkr_live_data.connect(CONSUMER, launch=False, timeout=20)
    ib.errorEvent += _on_error
    RESULTS["connected"] = True
    RESULTS["server_version"] = ib.client.serverVersion()
    log(f"connected; server version {RESULTS['server_version']}")

    try:
        # --resolution-only: run ONLY the contract-definition steps (1 and 1b).
        # They need no market-data subscription and cost zero historical-data
        # requests, so this re-asks the resolution question without burning the
        # 60-per-10-minutes historical budget on stages already answered.
        # --step5-only: answer the greeks/IV question on its own. The main flow
        # never reaches Step 5 (Step 0's control fails first), but the question
        # stands independently and deserves a recorded answer, not an assumption.
        if "--step5-only" in sys.argv:
            log("STEP5-ONLY mode")
            RESULTS["mode"] = "step5-only"
            step5_greeks_iv_only(ib)
            save()
            return 0

        if "--resolution-only" in sys.argv:
            log("RESOLUTION-ONLY mode: skipping all historical-data stages")
            RESULTS["mode"] = "resolution-only"
            step1_resolution_only(ib)
            step1b_specific_expired(ib)
            step1c_definition_boundary(ib)
            save()
            return 0

        # ---- Step 0: control -------------------------------------------------
        if not step0_control(ib):
            # Do NOT jump to "entitlement" — that would be a guess. Diagnose first.
            if not step0b_diagnose(ib):
                # Entitlement wall. Two questions are still honestly answerable
                # WITHOUT that subscription, so answer them rather than stop blind:
                #   0c — does delayed market data unlock historical option bars?
                #   1  — do expired LEAP contract DEFINITIONS still resolve?
                unlocked = step0c_delayed_history(ib)
                step1_resolution_only(ib)
                step1b_specific_expired(ib)
                if unlocked:
                    RESULTS["conclusion"] = (
                        "Live option market data is NOT subscribed on this login, but "
                        "DELAYED mode returned historical option bars — see "
                        "steps.step0c_delayed_history. Re-run the full expired sweep "
                        "with reqMarketDataType(3) set before every request.")
                    log(RESULTS["conclusion"])
                    save()
                    return 3
                RESULTS["conclusion"] = (
                    "STEP 0 CONTROL FAILED and Step 0b could not get daily bars for "
                    "ANY live, unexpired US equity option — not on SMART, not on a "
                    "real options exchange, not near-term, not long-dated. The "
                    "problem is therefore on the ENTITLEMENT / CONNECTIVITY side, "
                    "NOT expiry. Expired-contract steps were deliberately not run: "
                    "they could not have been interpreted. See "
                    "steps.step0b_diagnosis.entitlement_snapshot for whether IBKR "
                    "named a subscription (354/10089/10090/10167) or an exchange. "
                    "Expired-contract HISTORY was therefore not attempted — it could "
                    "not have been interpreted. Expired-contract RESOLUTION was "
                    "attempted anyway (steps.step1_resolution_only), because "
                    "reqContractDetails needs no market-data subscription.")
                log(RESULTS["conclusion"])
                save()
                return 2
            # Something DID work. Adopt it as the control and continue.
            log("STEP 0b found a working option-history combination; continuing.")
            RESULTS["steps"]["step0_control"]["control_passed"] = "via step0b"

        spot = RESULTS["steps"]["step0_control"].get("underlying_close")

        # ---- Steps 1 + 2: resolution and history for expired LEAPs -----------
        log("STEP 1+2 — expired AAPL LEAP calls: resolution, then history")
        s12 = []
        RESULTS["steps"]["step1_2_aapl_expired"] = s12
        for iso, label in EXPIRED_TARGETS:
            expiry = iso.replace("-", "")
            log(f"  -> AAPL {iso} ({label})")
            s12.append(probe_expiry(ib, "AAPL", expiry, label, spot, full_history=True))
            save()

        any_resolved = any(r.get("wildcard_resolution", {}).get("resolved")
                           for r in s12)
        any_history = any(r.get("verdict") == "RESOLVED + HISTORY RETURNED" for r in s12)

        # ---- Step 3: boundary ------------------------------------------------
        if any_resolved:
            log("STEP 3 — narrowing the retention/resolution boundary")
            s3 = []
            RESULTS["steps"]["step3_boundary"] = s3
            for iso, label in BOUNDARY_TARGETS:
                if RESULTS["hist_requests_made"] >= MAX_HIST_REQUESTS:
                    log("  pacing cap reached; stopping boundary sweep")
                    break
                expiry = iso.replace("-", "")
                log(f"  -> AAPL {iso} ({label})")
                s3.append(probe_expiry(ib, "AAPL", expiry, label, spot,
                                       full_history=False))
                save()
        else:
            RESULTS["steps"]["step3_boundary"] = "skipped — nothing resolved in Step 1"

        # ---- Step 4: second and third issuer ---------------------------------
        log("STEP 4 — confirm on MSFT and NVDA")
        s4 = []
        RESULTS["steps"]["step4_other_issuers"] = s4
        for sym in ("MSFT", "NVDA"):
            if RESULTS["hist_requests_made"] >= MAX_HIST_REQUESTS:
                log("  pacing cap reached; stopping issuer confirmation")
                break
            sspot = underlying_close_on(ib, sym, TODAY)
            # One recent-ish expired expiry and one old one — short, per instructions.
            for iso, label in (EXPIRED_TARGETS[0], EXPIRED_TARGETS[2]):
                expiry = iso.replace("-", "")
                log(f"  -> {sym} {iso} ({label})")
                s4.append(probe_expiry(ib, sym, expiry, label, sspot,
                                       full_history=False))
                save()

        # ---- Step 5: greeks / IV ---------------------------------------------
        log("STEP 5 — historical greeks / implied vol availability")
        s5: dict = {}
        RESULTS["steps"]["step5_greeks_iv"] = s5
        s5["api_surface_note"] = (
            "reqHistoricalData's whatToShow enumeration is the ONLY historical "
            "bar source in the TWS API. For an OPTION contract the accepted "
            "values are TRADES, MIDPOINT, BID, ASK, BID_ASK and "
            "OPTION_IMPLIED_VOLATILITY. There is NO historical-greeks endpoint: "
            "delta/gamma/theta/vega/rho arrive only on the LIVE streaming "
            "tickOptionComputation path (reqMktData genericTickList 106 / "
            "modelGreeks), which cannot be requested for a past date and cannot "
            "be requested at all for an expired contract.")
        # Empirically test the two things worth testing: OPTION_IMPLIED_VOLATILITY
        # on the live control contract, and BID_ASK (which carries no greeks).
        if CONTROL_CONTRACT:
            con = CONTROL_CONTRACT[0]
            for what in ("OPTION_IMPLIED_VOLATILITY", "BID_ASK"):
                if RESULTS["hist_requests_made"] >= MAX_HIST_REQUESTS:
                    break
                log(f"  live control, whatToShow={what}")
                s5[f"live_{what}"] = hist(ib, con, end="", duration="1 Y", what=what)
        # And on the OLDEST expired contract that resolved, if any.
        expired_ok = [r for r in s12 if r.get("conId") in CONTRACT_BY_CONID]
        if expired_ok and RESULTS["hist_requests_made"] < MAX_HIST_REQUESTS:
            r = expired_ok[-1]
            exp_d = dt.datetime.strptime(r["expiry"], "%Y%m%d").date()
            con = CONTRACT_BY_CONID[r["conId"]]
            log(f"  expired {r['symbol']} {r['expiry']}, "
                f"whatToShow=OPTION_IMPLIED_VOLATILITY")
            s5["expired_OPTION_IMPLIED_VOLATILITY"] = hist(
                ib, con, end=exp_d.strftime("%Y%m%d") + " 23:59:59 US/Eastern",
                duration="1 Y", what="OPTION_IMPLIED_VOLATILITY")
        save()

        RESULTS["summary"] = {
            "step0_control_passed": True,
            "any_expired_contract_resolved": any_resolved,
            "any_expired_history_returned": any_history,
            "hist_requests_made": RESULTS["hist_requests_made"],
        }
        log("PROBE COMPLETE")
        save()
        return 0
    finally:
        RESULTS["run_ended_utc"] = dt.datetime.utcnow().isoformat() + "Z"
        save()
        try:
            ib.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        RESULTS["fatal_traceback"] = traceback.format_exc()
        save()
        traceback.print_exc()
        sys.exit(1)
