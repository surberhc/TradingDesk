"""
eod_report.py — the end-of-day digest.

Runs LAST in the day (after the forward collector finishes) and emails ONE concise,
phone-readable status email covering S0 (Adaptive All-Weather Core) — the one
strategy actually live-paper-tested. It does not re-run anything — it READS each
subsystem's status artifact (the small JSONs in status.py, plus native files like
the supervisor heartbeat and the Tiingo manifest) and renders a section per job.
A job that crashed or never ran shows as fail/stale rather than taking the report
down.

Trimmed to S0-only sections on 2026-07-07, then restyled into a phone-concise S0
command center on 2026-07-08 (single status banner up top; freshness/account rows
collapse to one line unless something is actually stale or off-band) — both per
Andrew's request. The 7 non-S0 section builders (forward collector, EDGAR,
gamma/GEX, system/gateway health, staleness alarm, the old generic Tiingo section)
were archived out of this file to archive/non_s0_sections.py — see that file to
reinstate a section (or build a per-strategy digest) when another strategy needs
its own reporting.

Run manually any time:  <venv python> eod_report.py
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import sys
import threading
from pathlib import Path

import mailer
import status

# S0 (Adaptive All-Weather Core) reads the same shared-brain data path the
# backtester and paperbot use — see paperbot\strategy_target.py for the
# original pattern. The backtester is a local `src` package, not an installed
# dependency; add its folder to sys.path exactly like strategy_target.py does.
_BACKTESTER = Path(__file__).resolve().parent.parent / "backtester"
if str(_BACKTESTER) not in sys.path:
    sys.path.insert(0, str(_BACKTESTER))

# paperbot's nav_history module (S0 live-vs-model since-inception line in
# build_account) — same sys.path-add pattern as _BACKTESTER above.
_PAPERBOT = Path(__file__).resolve().parent.parent / "paperbot"
if str(_PAPERBOT) not in sys.path:
    sys.path.insert(0, str(_PAPERBOT))

LOG = Path(r"C:\TradingDesk-Local\state\dailyreport\eod_report.log")

TODAY = dt.date.today()
TODAY_STR = TODAY.strftime("%Y%m%d")

# Freshness anchor — the most recent session whose EOD data should already exist
# when this nightly report runs (after the close). On a trading day that's today;
# on a weekend/holiday it's the last real session. Every "is this fresh?" check
# below measures against THIS, not the literal calendar day, so holidays and
# weekends no longer false-flag as stale. See connections/market_calendar.py.
try:
    from connections import market_calendar as _mktcal
    _IS_TRADING_TODAY = _mktcal.is_trading_day(TODAY)
    EXPECTED_SESSION = _mktcal.last_trading_day(TODAY)
    _HOLIDAY_TODAY = _mktcal.holiday_name(TODAY)          # None unless full closure
    _EARLY_CLOSE_TODAY = _mktcal.early_close_name(TODAY)  # None unless a 1pm close
    _CAL_ERR = None
except Exception as _e:  # unknown year / import issue -> degrade to a weekday rule, loudly
    _IS_TRADING_TODAY = TODAY.weekday() < 5
    EXPECTED_SESSION = TODAY
    _HOLIDAY_TODAY = None
    _EARLY_CLOSE_TODAY = None
    _CAL_ERR = f"{type(_e).__name__}: {_e}"
EXPECTED_SESSION_STR = EXPECTED_SESSION.strftime("%Y%m%d")


def _is_fresh(date_str) -> bool:
    """A status/heartbeat date (YYYYMMDD string) is fresh if it is from the expected
    session or later. Uses '>=' not '==' so a job that ALSO ran today on a holiday
    (stamping today) still counts, while a genuinely old status still fails."""
    if not date_str:
        return False
    return str(date_str) >= EXPECTED_SESSION_STR


# Some macro inputs publish on a lag, so "last session" is the WRONG freshness bar
# for them. FRED's ICE BofA HY OAS (BAMLH0A0HYM2) posts next-business-day and, at
# this report's 21:00 CT slot, routinely sits 1-2 trading days behind the session.
# Verified 2026-07-28: FRED's OWN latest value was 2026-07-24 while the warehouse
# held exactly 2026-07-24 -- fully synced to source -- yet the same-session bar flagged
# it "stale" every night (19 nights running). Allow a per-input trading-day lag so
# only a GENUINE break (falling further behind FRED's own cadence) alarms. VIX (CBOE,
# same-day) stays at 0. Keyed by the manifest key used in build_s0_data.
_MACRO_MAX_LAG_SESSIONS = {"_hy_oas": 3, "_vix": 0}


def _fresh_floor_str(max_lag_sessions: int) -> str:
    """YYYYMMDD of the OLDEST last_date still counted fresh: EXPECTED_SESSION stepped
    back `max_lag_sessions` trading days (0 -> EXPECTED_SESSION itself). Degrades to
    EXPECTED_SESSION_STR if the market calendar is unavailable."""
    d = EXPECTED_SESSION
    try:
        from connections import market_calendar as _mc
        for _ in range(max(0, max_lag_sessions)):
            d = _mc.last_trading_day(d, inclusive=False)
    except Exception:
        return EXPECTED_SESSION_STR
    return d.strftime("%Y%m%d")


def _is_fresh_lagged(date_str, max_lag_sessions: int) -> bool:
    """Like _is_fresh but tolerant of a known publication lag: fresh if the date is
    within `max_lag_sessions` trading days of the expected session. lag 0 == _is_fresh."""
    if not date_str:
        return False
    if max_lag_sessions <= 0:
        return _is_fresh(date_str)
    return str(date_str) >= _fresh_floor_str(max_lag_sessions)

# status -> (dot color, label). Severity order used for the overall headline.
DOT = {"ok": "#22c55e", "info": "#3b82f6", "stale": "#9ca3af",
       "partial": "#fbbf24", "warn": "#f59e0b", "fail": "#ef4444"}
SEVERITY = ["ok", "info", "stale", "partial", "warn", "fail"]

# Severity rank used specifically to make s0_regime inherit s0_data's status when
# s0_data is worse (2026-07-09 fix) — a subset/superset of SEVERITY restricted to
# the statuses build_s0_data/build_s0_regime actually emit: ok < stale < warn < fail.
_DATA_STATUS_RANK = {"ok": 0, "stale": 1, "warn": 2, "fail": 3}


def _log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _sec(key, title, st, headline, rows):
    return {"key": key, "title": title, "status": st, "headline": headline, "rows": rows}


# Non-S0 sections (build_forward/build_thetadata/build_edgar/build_tiingo/build_system/
# build_gex/build_alarm) archived to archive/non_s0_sections.py 2026-07-08 — see that
# file to reinstate when another strategy needs its own EOD reporting.


# --------------------------------------------------------------------------- #
# S0 (Adaptive All-Weather Core regime engine) sections — added 2026-07-07.
# Both read the shared brain's OWN price loader + regime scorer directly
# (strategies.parts.regime.market_health_score over backtester/src/data_loader's
# frame) rather than running a full run_backtest() — cheap file reads + one
# vectorized score computation, safe to do inline in the nightly email job.
# --------------------------------------------------------------------------- #


def build_s0_regime(data_status: str | None = None):
    """S0 (Adaptive All-Weather Core) — today's Market Health Score + regime.

    Computes the score the SAME way the shared strategy brain does (see
    strategies\\parts\\regime.py market_health_score(), the exact function
    strategies.all_weather.AdaptiveAllWeather.warmup() calls) over the SAME
    price/macro data paperbot\\strategy_target.py loads for the live target
    book. This is NOT a second run_backtest() — it is the underlying causal,
    vectorized score computation alone (sub-second), giving TODAY's raw daily
    reading. The regime that actually governs today's portfolio (after the
    confirmation-buffer/dead-zone hysteresis, SPEC §4) is also shown, since a
    raw score can wiggle across a band boundary for a day or two without the
    confirmed regime (and therefore the traded band) actually changing.

    Never raises: any failure (missing data, import issue) degrades to a
    'fail' section rather than taking the whole report down.

    data_status: build_s0_data's already-computed status string, passed in so
    s0_regime can never report a HEALTHIER verdict than s0_data for the same
    night (2026-07-09 fix — they previously disagreed: s0_data warned on stale
    tickers while s0_regime independently said ok). If data_status outranks
    this section's own computed status (severity ok < stale < warn < fail),
    this section's status is downgraded to match and the headline is prefixed
    so the reason stays visible. None (the default) skips this — used only for
    back-compat / standalone calls where s0_data hasn't run."""
    title = "S0 Regime (Adaptive All-Weather Core)"
    try:
        from src import data_loader
        from strategies import config as s_config
        from strategies.parts import regime as s_regime

        prices = data_loader.load_prices()
        hyg = data_loader.load_prices([s_config.CREDIT_PROXY[0]])[s_config.CREDIT_PROXY[0]]
        denom_t = s_config.CREDIT_PROXY[1]
        credit_denom = (prices[denom_t] if denom_t in prices.columns
                        else data_loader.load_prices([denom_t])[denom_t])
        vix, vix_src = data_loader.load_vix()
        hy_oas, hy_oas_src = data_loader.load_hy_oas()

        score_df = s_regime.market_health_score(
            prices, hyg=hyg, credit_denom=credit_denom, vix=vix, hy_oas=hy_oas)
        confirmed = s_regime.apply_hysteresis(score_df["score"])

        # "Data as-of" must be the OLDEST last-real-value date across S0's required
        # price inputs, not score_df.index[-1] (the union of every ticker's dates,
        # which can read "fresh" even when a required ticker's row that day is NaN
        # — 2026-07-09 fix, paired with the NaN-as-bearish fix in regime.py).
        _required_cols = [c for c in (["SPY", "RSP"] + list(s_config.SECTORS)) if c in prices.columns]
        _real_dates = [prices[c].last_valid_index() for c in _required_cols]
        _real_dates += [hyg.last_valid_index(), credit_denom.last_valid_index()]
        _real_dates = [d for d in _real_dates if d is not None]
        as_of = min(_real_dates) if _real_dates else score_df.index[-1]
        last = score_df.loc[as_of]
        raw_regime = last["regime"]
        confirmed_regime = confirmed.iloc[-1]
        fresh = _is_fresh(as_of.strftime("%Y%m%d"))

        band_lo, band_hi = s_regime.equity_band(confirmed_regime)

        rows = [
            ("Data as-of", as_of.strftime("%Y-%m-%d")),
            ("Score (0-100)", f"{last['score']:.1f}"),
            ("Raw regime (today's score)", raw_regime),
            ("Confirmed regime (governs the book)", confirmed_regime),
            ("Equity band (confirmed regime)", f"{band_lo:.0%}-{band_hi:.0%}"),
            ("Trend component", f"{last['trend']:.2f}"),
            ("Breadth component", f"{last['breadth']:.2f}"),
            ("Stress component", f"{last['stress']:.2f}"),
            ("VIX source", vix_src),
            ("HY OAS source", hy_oas_src),
        ]

        if not fresh:
            st = "stale"
            headline = (f"score {last['score']:.1f} / {raw_regime} as of "
                        f"{as_of.strftime('%Y-%m-%d')}  ⚠ not fresh (expected "
                        f"session {EXPECTED_SESSION_STR})")
        else:
            st = "ok"
            if raw_regime == confirmed_regime:
                headline = (f"score {last['score']:.1f} — {confirmed_regime} "
                            f"(equity band {band_lo:.0%}-{band_hi:.0%})")
            else:
                headline = (f"score {last['score']:.1f} reads {raw_regime} today, "
                            f"but the CONFIRMED/traded regime is still "
                            f"{confirmed_regime} (equity band {band_lo:.0%}-{band_hi:.0%}) "
                            f"— hysteresis hasn't confirmed the move yet")

        # Inherit s0_data's status if it's worse (2026-07-09 fix): s0_regime must
        # never look healthier than s0_data for the same report. Downgrade, don't
        # silently swallow — prepend a visible note to the headline.
        if (data_status is not None
                and _DATA_STATUS_RANK.get(data_status, 0) > _DATA_STATUS_RANK.get(st, 0)):
            headline = f"[data {data_status}] " + headline
            st = data_status

        status.write("s0_regime", st, metrics={
            "score": float(last["score"]), "raw_regime": raw_regime,
            "confirmed_regime": confirmed_regime, "as_of": as_of.strftime("%Y%m%d"),
        }, message=headline, day=as_of.strftime("%Y%m%d"))

        return _sec("s0_regime", title, st, headline, rows)
    except Exception as e:
        return _sec("s0_regime", title, "fail",
                    f"could not compute S0 regime: {type(e).__name__}: {e}", [])


# S0's required tickers (strategies\config.py): the equity core + 11 sector
# ETFs used for trend/breadth, plus HYG/IEF (credit proxy) — VIX and HY OAS
# are checked separately since they're not per-ticker parquet files.
_S0_TICKERS = ["SPY", "RSP", "XLC", "XLY", "XLP", "XLE", "XLF", "XLV",
              "XLI", "XLB", "XLRE", "XLK", "XLU", "HYG", "IEF"]


def build_s0_data():
    """Tiingo/macro data freshness SCOPED to S0's own inputs only (SPY, RSP, the
    11 sector ETFs, HYG, IEF, plus VIX and HY OAS) — not a generic warehouse-wide
    check (that's build_tiingo, archived to archive/non_s0_sections.py). Reads the
    SAME manifest file strategies\\config.py / backtester\\src\\data_loader.py
    resolve (config.MANIFEST_FILE, the authoritative LOCAL path — NOT the stale
    Drive path build_tiingo's TIINGO_MANIFEST points at, see CLAUDE.md 2026-06-27
    'data moved off Drive'). Never raises.

    DISPLAY ONLY changed 2026-07-08: the freshness CHECK below (the per-ticker
    fresh/QC-flag evaluation feeding `problems`) is unchanged. What changed is the
    rendered `rows` — when every input is fresh, show one summary row instead of
    listing every ticker unconditionally; only expand to name the specific stale/
    missing/flagged tickers when something is actually wrong."""
    title = "S0 Data Freshness"
    try:
        from strategies import config as s_config

        manifest_path = Path(s_config.MANIFEST_FILE)
        if not manifest_path.exists():
            return _sec("s0_data", title, "fail",
                        f"manifest not found at {manifest_path}", [])
        mani = json.loads(manifest_path.read_text())
        tickers = mani.get("tickers", {})

        # Per-ticker freshness rows are computed exactly as before (same check, same
        # data) but only RENDERED when something is actually stale/missing/QC-flagged
        # — see problem_rows below. detail_rows still holds every ticker's row so the
        # section stays inspectable via status.py even when the email collapses it.
        detail_rows = []
        problem_rows = []
        problems = []
        oldest_date = None
        for t in _S0_TICKERS:
            info = tickers.get(t)
            if not info:
                problems.append(f"{t} missing from manifest")
                detail_rows.append((t, "MISSING"))
                problem_rows.append((t, "MISSING"))
                continue
            last_date = info.get("last_date", "")
            qc = info.get("qc_flags") or []
            fresh = _is_fresh(last_date.replace("-", "")) if last_date else False
            row_val = f"{last_date}{'  QC:' + str(qc) if qc else ''}"
            if not fresh:
                problems.append(f"{t} stale (last_date={last_date})")
                problem_rows.append((t, row_val))
            if qc:
                problems.append(f"{t} has QC flags: {qc}")
                if fresh:  # avoid double-adding the same row if also stale
                    problem_rows.append((t, row_val))
            detail_rows.append((t, row_val))
            if last_date and (oldest_date is None or last_date < oldest_date):
                oldest_date = last_date

        for name, key in (("VIX", "_vix"), ("HY OAS", "_hy_oas")):
            info = tickers.get(key)
            if not info:
                problems.append(f"{name} missing from manifest")
                detail_rows.append((name, "MISSING"))
                problem_rows.append((name, "MISSING"))
                continue
            last_date = info.get("last_date", "")
            qc = info.get("qc_flags") or []
            fresh = (_is_fresh_lagged(last_date.replace("-", ""),
                                      _MACRO_MAX_LAG_SESSIONS.get(key, 0))
                     if last_date else False)
            row_val = (f"{last_date}  ({info.get('source', '?')})"
                       f"{'  QC:' + str(qc) if qc else ''}")
            if not fresh:
                problems.append(f"{name} stale (last_date={last_date})")
                problem_rows.append((name, row_val))
            if qc:
                problems.append(f"{name} has QC flags: {qc}")
                if fresh:
                    problem_rows.append((name, row_val))
            detail_rows.append((name, row_val))

        gen = mani.get("generated_at", "")
        pulled_today = gen[:10] >= EXPECTED_SESSION.isoformat() if gen else False

        n_total = len(_S0_TICKERS) + 2  # + VIX + HY OAS
        if problems:
            st = "warn"
            headline = f"{len(problems)} issue(s): " + "; ".join(problems[:4])
            if len(problems) > 4:
                headline += f" (+{len(problems) - 4} more)"
            # Collapsed display: only the specific stale/missing/QC-flagged rows,
            # not all n_total — that's the point of the collapse. Full detail_rows
            # remains available via status.write below for anyone reading the JSON.
            rows = problem_rows + [
                ("Manifest generated", gen[:19] if gen else "—"),
                ("Pulled today", "yes" if pulled_today else "no"),
            ]
        else:
            st = "ok"
            headline = (f"S0's {len(_S0_TICKERS)} tickers + VIX/HY OAS all fresh, "
                        f"no QC flags (oldest last_date {oldest_date})")
            rows = [
                (f"All {n_total} inputs", f"fresh (oldest last_date {oldest_date})"),
                ("Manifest generated", gen[:19] if gen else "—"),
                ("Pulled today", "yes" if pulled_today else "no"),
            ]

        status.write("s0_data", st, metrics={
            "n_tickers": len(_S0_TICKERS), "n_problems": len(problems),
            "pulled_today": pulled_today, "detail_rows": detail_rows,
        }, message=headline, day=TODAY_STR)

        return _sec("s0_data", title, st, headline, rows)
    except Exception as e:
        return _sec("s0_data", title, "fail",
                    f"could not check S0 data freshness: {type(e).__name__}: {e}", [])


def _since_inception_line() -> str | None:
    """One short since-inception performance line: aggregate total-desk paper NAV
    % change vs a NAV-weighted blend of the 3 backtest curves over the same
    tracked window. Simplest honest aggregate for a one-line email context (no
    per-version breakdown here — the dashboard's Performance section has that).

    Returns None if fewer than 2 distinct tracked dates exist yet (expected for
    the first night this feature ships, 2026-07-07) — the caller omits the line
    entirely rather than show a broken/empty stat. Never raises into the caller.
    """
    try:
        import nav_history
        hist = nav_history.load_history()
        if hist.empty or hist["date"].nunique() < 2:
            return None

        start_date = hist["date"].min()
        end_date = hist["date"].max()

        # Paper: total desk NAV (all accounts, all versions) at start vs latest.
        by_date = hist.groupby("date")["net_liq"].sum().sort_index()
        paper_start, paper_end = by_date.iloc[0], by_date.iloc[-1]
        if paper_start <= 0:
            return None
        paper_pct = (paper_end / paper_start - 1.0) * 100.0

        # Model: NAV-weighted blend of the 3 backtest curves, weighted by each
        # version's tracked-window start NAV (so the blend matches how paper
        # capital is actually split across versions).
        from src import backtest as bt
        weighted_num = 0.0
        weight_total = 0.0
        for v, v_df in hist.groupby("version"):
            v_by_date = v_df.groupby("date")["net_liq"].sum().sort_index()
            w = v_by_date.iloc[0]
            with contextlib.redirect_stdout(io.StringIO()):
                res = bt.run_backtest(version=v, end=None)
            curve = res["nav"].loc[start_date:end_date]
            if len(curve) < 2 or curve.iloc[0] <= 0:
                continue
            model_pct_v = (curve.iloc[-1] / curve.iloc[0] - 1.0)
            weighted_num += w * model_pct_v
            weight_total += w
        if weight_total <= 0:
            return None
        model_pct = weighted_num / weight_total * 100.0

        return (f"Since {start_date}: paper {paper_pct:+.1f}% vs model {model_pct:+.1f}%")
    except Exception:
        return None


def build_account():
    """Account cash-flow monitor — the propose-only, read-only per-account cycle
    (paperbot\\account_monitor_run.py) that runs ~4:30 PM CT. It writes an
    'account_monitor' status JSON (status.write) on both its success and failure
    paths: metrics={'rc': <int|None>} plus a human message. This section MIRRORS the
    other status-backed builders (read → freshness → ok/stale/fail) and NEVER raises.

    fresh + rc==0 → ok; a non-zero rc / raised cycle → fail (carried in the status
    'status' field); a status from a previous day → stale. Missing entirely → a
    graceful 'not yet reported' line (the monitor may not have run today)."""
    title = "Account Cash-Flow Monitor"
    s = status.read("account_monitor")
    if not s:
        return _sec("account", title, "stale",
                    "No status written yet — did the 4:30 PM monitor cycle run? "
                    "(paperbot\\account_monitor_run.py writes this on every run.)", [])
    fresh = _is_fresh(s.get("date"))
    st = s.get("status", "fail") if fresh else "stale"
    m = s.get("metrics", {})
    rc = m.get("rc")
    rc_label = {0: "0 (clean cycle or clean skip)"}.get(rc, rc)

    # Per-account verdicts (paperbot\account_monitor_run.py _summarize_verdicts):
    # metrics["accounts"] = {account: {"action": "HOLD"/"REBALANCE"/"ALERT", "reason": ...}}.
    # One line per account (name + status word) is enough for HOLD/in-band accounts;
    # only accounts that need attention (REBALANCE/ALERT) get their reason expanded.
    accounts = m.get("accounts") or {}
    account_rows = []
    for acct, v in accounts.items():
        action = v.get("action", "?")
        if action == "HOLD":
            account_rows.append((acct, "in-band (HOLD)"))
        else:
            reason = v.get("reason", "")
            account_rows.append((acct, f"{action}" + (f" — {reason}" if reason else "")))

    # Any other richer metrics the monitor may add later (deposits/withdrawals
    # detected, buffer %) beyond rc/accounts/n_* are rendered generically so a
    # future field isn't silently dropped.
    known_keys = {"rc", "accounts", "n_hold", "n_rebalance", "n_alert"}
    extra = [(k, v) for k, v in m.items() if k not in known_keys]

    # Since-inception performance line (paper NAV vs backtest-expected curve) —
    # omitted entirely until >=2 tracked days exist (expected for tonight,
    # 2026-07-07, the day this feature ships).
    perf_row = []
    perf_line = _since_inception_line()
    if perf_line:
        perf_row = [("Performance", perf_line)]

    rows = ([("Run date", s.get("date")),
             ("Return code", rc_label if rc_label is not None else "—")]
            + account_rows
            + extra
            + perf_row
            + [("Posture", "read-only / propose-only (transmits nothing)"),
               ("Last update", s.get("ts"))])
    headline = s.get("message", "") + ("" if fresh else "  ⚠ status is from a previous day")
    return _sec("account", title, st, headline, rows)


# NOTE: build_s0_data must run BEFORE build_s0_regime so s0_regime can inherit
# s0_data's status when it's worse (2026-07-09 fix, see build_s0_regime's
# data_status param) — main() special-cases this ordering; SECTIONS here is kept
# only for any other caller that iterates it uniformly (e.g. archive tooling).
SECTIONS = [build_s0_regime, build_s0_data, build_account]


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
def _overall(sections):
    worst = "ok"
    for s in sections:
        st = s.get("status", "fail")
        idx = SEVERITY.index(st) if st in SEVERITY else len(SEVERITY) - 1
        if idx > SEVERITY.index(worst):
            worst = st if st in SEVERITY else "fail"
    return worst


def _status_banner(sections, overall) -> str:
    """One-line, phone-first banner: overall dot + today's confirmed regime + equity
    band + a fresh/stale summary across sections. Reuses values already computed by
    build_s0_regime's section dict (its 'rows' list) — does NOT recompute regime or
    band independently, so this can never drift from the S0 Regime section below it.
    """
    regime_txt = "regime unknown"
    band_txt = ""
    s0 = next((s for s in sections if s.get("key") == "s0_regime"), None)
    if s0 is not None:
        row_map = dict(s0.get("rows", []))
        confirmed = row_map.get("Confirmed regime (governs the book)")
        band = row_map.get("Equity band (confirmed regime)")
        if confirmed:
            regime_txt = str(confirmed)
        if band:
            band_txt = f" · {band} equity band"

    n = len(sections)
    n_ok = sum(1 for s in sections if s.get("status") == "ok")
    systems_txt = "all systems fresh" if n_ok == n else f"{n_ok}/{n} systems fresh"

    dot_html = (f'<span style="display:inline-block;width:11px;height:11px;'
                f'border-radius:50%;background:{DOT.get(overall, "#9ca3af")};'
                f'margin-right:8px;"></span>')
    return (f'<div style="font-size:15px;font-weight:600;color:#111827;margin:2px 0 8px;">'
            f'{dot_html}S0: {regime_txt}{band_txt} · {systems_txt}</div>')


def _session_banner() -> str:
    """A one-line context banner shown when today isn't a normal full session, so a
    report that looks quiet on a holiday/weekend reads as expected, not broken."""
    exp = EXPECTED_SESSION.strftime("%a %b %d")
    if _CAL_ERR is not None:
        msg = (f"⚠ No verified market calendar for {TODAY.year} — using a weekday-only "
               f"rule (holidays may mis-flag). Update connections/market_calendar.py. [{_CAL_ERR}]")
        bg, fg = "#fef3c7", "#92400e"
    elif _HOLIDAY_TODAY:
        msg = (f"\U0001f3e6 Market holiday today — {_HOLIDAY_TODAY}. No new session; freshness "
               f"is measured against the last session ({exp}).")
        bg, fg = "#e0f2fe", "#075985"
    elif not _IS_TRADING_TODAY:
        msg = (f"\U0001f5d3 Weekend — no session today. Freshness is measured against the "
               f"last session ({exp}).")
        bg, fg = "#e0f2fe", "#075985"
    elif _EARLY_CLOSE_TODAY:
        msg = f"\U0001f550 Early close today (1:00pm ET) — {_EARLY_CLOSE_TODAY}."
        bg, fg = "#e0f2fe", "#075985"
    else:
        return ""
    return (f'<div style="font-size:12px;background:{bg};color:{fg};border-radius:6px;'
            f'padding:7px 10px;margin:6px 0;">{msg}</div>')


def render_html(sections, overall):
    def dot(st):
        return (f'<span style="display:inline-block;width:11px;height:11px;'
                f'border-radius:50%;background:{DOT.get(st, "#9ca3af")};margin-right:8px;"></span>')

    blocks = []
    for s in sections:
        rows_html = "".join(
            f'<tr><td style="padding:2px 14px 2px 0;color:#6b7280;white-space:nowrap;">{k}</td>'
            f'<td style="padding:2px 0;color:#111827;">{"" if v is None else v}</td></tr>'
            for k, v in s["rows"])
        table = (f'<table style="border-collapse:collapse;font-size:13px;margin-top:6px;">'
                 f'{rows_html}</table>') if s["rows"] else ""
        blocks.append(
            f'<div style="border:1px solid #e5e7eb;border-radius:8px;padding:12px 14px;'
            f'margin:10px 0;background:#fff;">'
            f'<div style="font-size:15px;font-weight:600;color:#111827;">'
            f'{dot(s["status"])}{s["title"]}'
            f'<span style="float:right;font-size:11px;text-transform:uppercase;'
            f'letter-spacing:.05em;color:{DOT.get(s["status"], "#9ca3af")};">{s["status"]}</span></div>'
            f'<div style="font-size:13px;color:#374151;margin-top:4px;">{s["headline"]}</div>'
            f'{table}</div>')

    stamp = dt.datetime.now().strftime("%A %b %d, %Y  %I:%M %p")
    return (
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;'
        f'max-width:640px;margin:0 auto;color:#111827;">'
        f'<div style="font-size:18px;font-weight:700;">{dot(overall)}Trading Desk — End of Day</div>'
        f'<div style="font-size:12px;color:#6b7280;margin:2px 0 6px;">{stamp} · '
        f'overall: <b style="color:{DOT.get(overall, "#9ca3af")};text-transform:uppercase;">{overall}</b></div>'
        f'{_status_banner(sections, overall)}'
        f'{_session_banner()}'
        f'{"".join(blocks)}'
        f'<div style="font-size:11px;color:#9ca3af;margin-top:10px;">'
        f'Automated end-of-day digest · TradingDesk\\dailyreport\\eod_report.py</div></div>')


def _run_section(build, timeout: float = 30.0):
    """Run one section builder with a hard timeout so no single builder (a slow file
    read, a wedged probe) can stall or crash the whole report. Returns a section dict;
    never raises. A builder that overruns is abandoned (daemon thread) and rendered as
    a 'fail: timed out' section so the email still goes out."""
    result: dict = {}

    def worker():
        try:
            result["sec"] = build()
        except Exception as e:
            result["err"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return _sec(build.__name__, build.__name__, "fail",
                    f"section timed out after {int(timeout)}s (skipped so the report could send)", [])
    if "sec" in result:
        return result["sec"]
    return _sec(build.__name__, build.__name__, "fail",
                f"section error: {result.get('err', 'unknown')}", [])


def main() -> bool:
    """Build + email the EOD digest. Returns whether the email was actually sent
    (so __main__ can exit non-zero -> Task Scheduler shows the run red)."""
    _log(f"=== EOD report {TODAY_STR} start ===")
    sent = False
    try:
        # build_s0_data runs BEFORE build_s0_regime (order-affecting, 2026-07-09
        # fix) so s0_regime can inherit s0_data's status when it's worse — see
        # build_s0_regime's data_status param. Display order stays s0_regime,
        # s0_data, account (unchanged) since _status_banner and the rendered
        # email both expect that order.
        s0_data_sec = _run_section(build_s0_data)
        _build_s0_regime_with_data_status = lambda: build_s0_regime(
            data_status=s0_data_sec.get("status"))
        _build_s0_regime_with_data_status.__name__ = "build_s0_regime"
        s0_regime_sec = _run_section(_build_s0_regime_with_data_status)
        # account_monitor section DE-LISTED 2026-07-28: AccountMonitorDaily is
        # deliberately paused (gateway quarantine since 2026-07-08, "don't re-enable
        # without Andrew" pin). A paused-on-purpose job was rendering "stale" every
        # night and dragging the whole digest to warn/fail -- a false alarm, not a
        # data failure. Re-add `account_sec` here (build_account is retained below)
        # when the monitor is revived / folded into the CRM per-account loop
        # (CRM_DESIGN groups_brain.md 12.2/12.3).
        sections = [s0_regime_sec, s0_data_sec]
        overall = _overall(sections)
        html = render_html(sections, overall)
        subject = f"Trading Desk EOD — {TODAY.strftime('%b %d')} — {overall.upper()}"
        sent = mailer.send_html(subject, html)
        _log(f"sections={[s['status'] for s in sections]} overall={overall} "
             f"emailed={'YES' if sent else 'NO'} -> {mailer.recipient()}")
        status.write("eod_report", "ok" if sent else "fail",
                     metrics={"overall": overall, "emailed": sent}, day=TODAY_STR)
    except Exception as e:
        # Last-resort guard: the generator itself failed. Still send SOMETHING and
        # record a fail status so the independent watchdog also alarms.
        import traceback
        tb = traceback.format_exc()
        _log(f"FATAL in main(): {type(e).__name__}: {e}\n{tb}")
        sent = False   # generator failed; the fallback below re-computes this
        try:
            fb_html = (
                f'<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;'
                f'max-width:640px;margin:0 auto;color:#111827;">'
                f'<div style="font-size:16px;font-weight:700;color:#ef4444;">'
                f'Trading Desk EOD — generator error</div>'
                f'<div style="font-size:13px;margin-top:8px;">The end-of-day report failed to '
                f'build. Raw error below.</div>'
                f'<pre style="font-size:12px;white-space:pre-wrap;background:#f9fafb;'
                f'border:1px solid #e5e7eb;border-radius:6px;padding:10px;">'
                f'{type(e).__name__}: {e}\n\n{tb}</pre></div>')
            sent = mailer.send_html(f"Trading Desk EOD — {TODAY.strftime('%b %d')} — ERROR", fb_html)
        except Exception as e2:
            _log(f"FATAL fallback email also failed: {type(e2).__name__}: {e2}")
        try:
            status.write("eod_report", "fail",
                         metrics={"overall": "fail", "emailed": sent,
                                  "error": f"{type(e).__name__}: {e}"}, day=TODAY_STR)
        except Exception:
            pass
    _log(f"=== EOD report {TODAY_STR} done (emailed={'YES' if sent else 'NO'}) ===")
    return sent


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
