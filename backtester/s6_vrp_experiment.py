r"""
s6_vrp_experiment.py -- S6 VRP-TIMING experiment (day-selection overlay).

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the warehouse.
ASCII-ONLY console output (Windows cp1252). Write "delta"/"IV" as plain text -- never a
Greek symbol (a Unicode char crashed a prior S6 run at print time).

THE QUESTION
------------
Is there an edge in TIMING the sale of a FIXED-delta 0DTE credit spread by the VARIANCE
RISK PREMIUM -- selling only when implied vol at entry is RICH relative to recent realized
vol? We hold the short strike fixed at 0.15 delta (delta is the expected-move-normalized
distance, so it already bakes in IV) so this isolates TIMING (which DAYS to sell) from
strike distance. We REUSE the already-computed fixed-0.15-delta trade outcomes -- this is
purely a DAY-SELECTION overlay; we do NOT recompute any spread.

ANTI-CURVE-FIT (rule #1) -- baked into the method, not bolted on:
  * PRIMARY signal is PRE-REGISTERED: vrp_primary = impl_2pm - rv_morning. We do NOT swap
    to a better-looking signal after seeing results. Two SECONDARY cross-checks
    (vrp_trail = impl_2pm - rv_trail5 ; and the prior-close VIX9D/VIX term-structure) are
    reported honestly whether or not they agree.
  * We bucket days into EQUAL-COUNT quantiles decided a priori (terciles AND quintiles).
    NO tuned thresholds, NO hand-picked cutoffs, NO best-bucket cherry-picking.
  * The verdict requires a MONOTONIC gradient that survives BOTH time-halves AND all
    structures. An isolated bucket / one-half-only / one-structure gradient = PEAK = reject.
  * A null result ("no robust gradient -- VRP timing is a dead end") is a valid finding.

NO LOOK-AHEAD (proven by a test): the VRP signal for a day uses ONLY data at or before
14:00 ET that day (14:00 ATM IV, 09:30-14:00 realized vol) plus PRIOR trading days
(trailing 5-day close-to-close RV computed from closes strictly before today; prior-EOD
VIX ratio). The trade OUTCOME is the future result of the fixed-delta trade.

SIGNAL RECOVERY reuses s6_recon (put-call-parity spot + BS per-strike IV) and s5 exactly
as the control/strike harnesses do. The FIXED-DELTA TRADE OUTCOMES are reused from the
existing strike-experiment arm A_blind015 (fixed 0.15 delta), which carries per-day P&L,
exit reason, entry credit, spot_entry, and the breach flag, and does NOT silently drop
sub-$0.30-credit days -- so the overlay sees every fixed-delta day.

CRASH-RESILIENT + RESUMABLE: the (expensive) per-day signal computation checkpoints to a
partial CSV and resume-skips days already done; one bad day cannot abort the run.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd

import s5_intraday_data as s5
import s6_recon as recon

# --------------------------------------------------------------------------- #
# Declared constants (NOT tuned).
# --------------------------------------------------------------------------- #
ENTRY_TIME = _dt.time(14, 0)          # 14:00 ET entry -- signal cutoff.
MORNING_START = _dt.time(9, 30)       # session open for morning realized vol.
SETTLEMENT_TIME = _dt.time(16, 0)     # for recovering the daily close spot.
TRAIL_DAYS = 5                        # trailing close-to-close window (documented, not swept).
TRADING_DAYS_PER_YEAR = 252.0         # standard annualization for daily RV.
MINUTES_PER_TRADING_YEAR = 252.0 * 390.0  # 390 min/session -> annualize 1-min RV.
TRAIN_END = _dt.date(2024, 6, 30)     # same split as the rest of S6.

VIX_PARQUET = Path(r"C:\TradingDesk-Local\bt_data\_vix.parquet")
VIX9D_PARQUET = Path(r"C:\TradingDesk-Local\bt_data\_vix9d.parquet")

OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "s6_research"
_PARTIAL_CSV = OUTPUT_DIR / "s6_vrp_signal_partial.csv"
_LOG = OUTPUT_DIR / "_vrp_run.log"

# The fixed-0.15-delta trade outcomes to overlay (reused, NOT recomputed).
_ARM_A_TRADES = OUTPUT_DIR / "s6_strike_experiment_trades.csv"
STRUCTURES = ("bull_put", "bear_call", "iron_condor")


# --------------------------------------------------------------------------- #
# Signal record -- one per trading day.
# --------------------------------------------------------------------------- #
@dataclass
class DaySignal:
    day: _dt.date
    ok: bool = False
    skip_reason: str = ""
    spot_1400: float = float("nan")
    impl_2pm: float = float("nan")       # ATM 0DTE IV at 14:00 (annualized), avg of C+P.
    rv_morning: float = float("nan")     # 09:30-14:00 realized vol today (annualized).
    close_spot: float = float("nan")     # recovered spot near 16:00 (for the trailing series).
    n_morning_minutes: int = 0
    atm_strike: float = float("nan")


# --------------------------------------------------------------------------- #
# Logging helper (ASCII only).
# --------------------------------------------------------------------------- #
def _log(msg: str) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(_LOG, "a", encoding="ascii", errors="replace") as fh:
        fh.write(msg + "\n")


# --------------------------------------------------------------------------- #
# Step 1 signal helpers.
# --------------------------------------------------------------------------- #
def atm_iv_at(entry_snap: pd.DataFrame, minute: pd.Timestamp, day: _dt.date,
              spot: float) -> tuple[float, float]:
    """ATM 0DTE implied vol (annualized) at `minute`, plus the ATM strike used.

    Uses s6_recon.per_strike_delta to get per-strike BS-inverted IV, then averages the
    call and put IV at the strike NEAREST the recovered spot. Averaging the two sides is a
    plain smile-symmetry choice, not a tuned parameter. Returns (nan, nan) if no ATM IV.
    """
    tbl = recon.per_strike_delta(entry_snap, minute, day, spot)
    tbl = tbl[tbl["iv"].notna()].copy()
    if tbl.empty:
        return float("nan"), float("nan")
    strikes = tbl["strike"].unique()
    atm_strike = float(strikes[np.argmin(np.abs(strikes - spot))])
    at = tbl[tbl["strike"] == atm_strike]
    iv = float(at["iv"].mean())
    return iv, atm_strike


def realized_vol_from_spot(spot_series: pd.Series) -> tuple[float, int]:
    """Annualized realized vol from a 1-min spot series (log returns).

    RV = std(1-min log returns) * sqrt(minutes_per_trading_year). Returns (nan, 0) if
    fewer than 2 usable points. Population-consistent sample std (ddof=1).
    """
    s = spot_series.dropna()
    if len(s) < 3:
        return float("nan"), len(s)
    logret = np.log(s.to_numpy()[1:] / s.to_numpy()[:-1])
    logret = logret[np.isfinite(logret)]
    if len(logret) < 2:
        return float("nan"), len(logret)
    sd = float(np.std(logret, ddof=1))
    return sd * math.sqrt(MINUTES_PER_TRADING_YEAR), len(logret)


def compute_day_signal(d: _dt.date, day_data: s5.DayData | None = None) -> DaySignal:
    """Compute the at-14:00 VRP inputs for one day. Never raises on a single-day quirk.

    STRICTLY CAUSAL: reads ONLY minutes at/before 14:00 for impl_2pm and rv_morning. The
    close_spot (last minute <= 16:00) is recovered too, but it is used ONLY to build the
    trailing close-to-close series consumed by LATER days (t+... ), never for today's signal.
    """
    sig = DaySignal(day=d)
    try:
        dd = day_data if day_data is not None else s5.load_day(d)
        chain = s5.zero_dte_chain(d, day_data=dd)
        nbbo = chain.nbbo
        if nbbo.empty:
            sig.skip_reason = "no 0dte chain"
            return sig

        entry_minute = pd.Timestamp(_dt.datetime.combine(d, ENTRY_TIME))
        morning_start = pd.Timestamp(_dt.datetime.combine(d, MORNING_START))
        settle_minute = pd.Timestamp(_dt.datetime.combine(d, SETTLEMENT_TIME))
        minute_set = set(nbbo["minute"].unique())
        if entry_minute not in minute_set:
            sig.skip_reason = "no 14:00 snapshot"
            return sig

        # --- spot at 14:00 + ATM IV at 14:00 (CAUSAL: only the entry snapshot) ---
        entry_snap = nbbo[nbbo["minute"] == entry_minute][["strike", "right", "bid", "ask"]].copy()
        sr = recon.recover_forward_spot(entry_snap, entry_minute, d)
        if sr is None or not np.isfinite(sr.spot):
            sig.skip_reason = "spot recon failed at 14:00"
            return sig
        sig.spot_1400 = sr.spot
        iv, atm_strike = atm_iv_at(entry_snap, entry_minute, d, sr.spot)
        sig.impl_2pm = iv
        sig.atm_strike = atm_strike

        # --- morning realized vol: 09:30 .. 14:00 recovered spot (CAUSAL: <= 14:00) ---
        morning_minutes = sorted(m for m in minute_set
                                 if morning_start <= m <= entry_minute)
        morn = {}
        for m in morning_minutes:
            snap = nbbo[nbbo["minute"] == m][["strike", "right", "bid", "ask"]]
            s = recon.recover_forward_spot(snap, pd.Timestamp(m), d)
            if s is not None and np.isfinite(s.spot):
                morn[pd.Timestamp(m)] = s.spot
        morn_series = pd.Series(morn).sort_index()
        rv, nmin = realized_vol_from_spot(morn_series)
        sig.rv_morning = rv
        sig.n_morning_minutes = nmin

        # --- close spot (for the trailing series only; NOT today's signal) ---
        close_minutes = sorted(m for m in minute_set if m <= settle_minute)
        if close_minutes:
            last_m = close_minutes[-1]
            snap = nbbo[nbbo["minute"] == last_m][["strike", "right", "bid", "ask"]]
            s = recon.recover_forward_spot(snap, pd.Timestamp(last_m), d)
            if s is not None and np.isfinite(s.spot):
                sig.close_spot = s.spot

        sig.ok = np.isfinite(sig.impl_2pm) and np.isfinite(sig.rv_morning)
        if not sig.ok:
            sig.skip_reason = sig.skip_reason or "impl_2pm or rv_morning not computable"
        return sig
    except Exception as e:  # one bad day must not abort the run
        sig.skip_reason = f"error: {type(e).__name__}: {e}"
        return sig


# --------------------------------------------------------------------------- #
# Trailing 5-day close-to-close RV (knowable at 14:00 -> uses closes STRICTLY before today).
# --------------------------------------------------------------------------- #
def add_trailing_rv(sig_df: pd.DataFrame) -> pd.DataFrame:
    """Add rv_trail5 = annualized close-to-close RV over the TRAIL_DAYS closes strictly
    BEFORE today. No look-ahead: today's close is excluded from today's trailing signal.

    Uses the recovered close_spot series. A day gets rv_trail5 only if the prior window has
    >= TRAIL_DAYS usable closes; otherwise NaN (early days / gaps).
    """
    df = sig_df.sort_values("day").reset_index(drop=True).copy()
    closes = df["close_spot"].to_numpy()
    rv_trail = np.full(len(df), np.nan)
    for i in range(len(df)):
        # closes strictly before today with a valid value; need TRAIL_DAYS+1 to form
        # TRAIL_DAYS returns.
        prev = closes[:i]
        prev = prev[np.isfinite(prev)]
        if len(prev) < TRAIL_DAYS + 1:
            continue
        window = prev[-(TRAIL_DAYS + 1):]
        rets = np.log(window[1:] / window[:-1])
        rets = rets[np.isfinite(rets)]
        if len(rets) < TRAIL_DAYS:
            continue
        rv_trail[i] = float(np.std(rets, ddof=1)) * math.sqrt(TRADING_DAYS_PER_YEAR)
    df["rv_trail5"] = rv_trail
    return df


# --------------------------------------------------------------------------- #
# Prior-close VIX term-structure ratio (secondary cross-check).
# --------------------------------------------------------------------------- #
def _load_vix_ratio() -> pd.DataFrame:
    """Prior-EOD VIX9D/VIX ratio per date (ratio>1 = backwardation/stress). CAUSAL: the
    caller shifts to the PRIOR trading day so today uses only yesterday's close."""
    vix = pd.read_parquet(VIX_PARQUET)
    v9 = pd.read_parquet(VIX9D_PARQUET)
    j = v9.join(vix, how="inner")
    j["ratio"] = j["vix9d"] / j["vix"]
    j = j.reset_index().rename(columns={"index": "date"})
    if "date" not in j.columns:
        j = j.rename(columns={j.columns[0]: "date"})
    j["date"] = pd.to_datetime(j["date"]).dt.date
    return j[["date", "ratio"]].sort_values("date").reset_index(drop=True)


def add_vix_ts_prior(sig_df: pd.DataFrame) -> pd.DataFrame:
    """Add vix_ts_prior = the PRIOR trading day's VIX9D/VIX ratio (secondary signal).

    For each day we take the most recent VIX ratio STRICTLY before that day (no look-ahead).
    Higher ratio = short-end backwardation = stress; as a VRP-timing cross-check we test
    whether SELLING when the ratio is LOW (calm/contango) does better -- reported honestly.
    """
    df = sig_df.sort_values("day").reset_index(drop=True).copy()
    try:
        vr = _load_vix_ratio()
    except Exception as e:
        _log(f"vix ratio load failed: {type(e).__name__}: {e}")
        df["vix_ts_prior"] = np.nan
        return df
    v_dates = np.array(vr["date"].tolist())
    v_vals = vr["ratio"].to_numpy()
    out = np.full(len(df), np.nan)
    for i, dd in enumerate(pd.to_datetime(df["day"]).dt.date):
        mask = v_dates < dd
        if mask.any():
            out[i] = v_vals[mask][-1]
    df["vix_ts_prior"] = out
    return df


# --------------------------------------------------------------------------- #
# Build the signal table over history (crash-resilient + resumable).
# --------------------------------------------------------------------------- #
def build_signals(days: list[_dt.date] | None = None, verbose: bool = True,
                  resume: bool = True) -> pd.DataFrame:
    """Compute the per-day signal for every available 0DTE day, checkpointing per day."""
    if days is None:
        days = s5.available_days()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    done: set[str] = set()
    if resume and _PARTIAL_CSV.is_file():
        try:
            done = set(pd.read_csv(_PARTIAL_CSV, usecols=["day"])["day"].astype(str))
        except Exception:
            done = set()
    if verbose and done:
        msg = f"resume: {len(done)} days already in partial; skipping"
        print(msg, flush=True); _log(msg)

    n = len(days)
    import csv
    fieldnames = list(asdict(DaySignal(day=days[0])).keys())
    write_header = not _PARTIAL_CSV.is_file()
    with open(_PARTIAL_CSV, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for i, d in enumerate(days, 1):
            if str(d) in done:
                continue
            try:
                dd = s5.load_day(d)
            except Exception as e:
                msg = f"[{i}/{n}] {d} LOAD-SKIP {type(e).__name__}"
                if verbose:
                    print(msg, flush=True)
                _log(msg)
                # still write a non-ok row so we do not retry it forever
                writer.writerow(asdict(DaySignal(day=d, skip_reason="load error")))
                fh.flush()
                continue
            sig = compute_day_signal(d, day_data=dd)
            writer.writerow(asdict(sig))
            fh.flush()
            msg = f"[{i}/{n}] {d} done ok={sig.ok} impl={sig.impl_2pm:.4f} rvm={sig.rv_morning:.4f}"
            _log(msg)
            if verbose and (i % 25 == 0 or i == n):
                print(msg, flush=True)

    df = pd.read_csv(_PARTIAL_CSV)
    df["ok"] = df["ok"].astype(str).str.lower().isin(["true", "1"])
    return df


def finalize_signals(sig_df: pd.DataFrame, save: bool = True) -> pd.DataFrame:
    """Add rv_trail5 + vix_ts_prior + the three VRP signals; save the enriched table."""
    df = add_trailing_rv(sig_df)
    df = add_vix_ts_prior(df)
    df["vrp_primary"] = df["impl_2pm"] - df["rv_morning"]      # PRE-REGISTERED PRIMARY
    df["vrp_trail"] = df["impl_2pm"] - df["rv_trail5"]         # secondary cross-check
    # vix_ts_prior is itself the third (secondary) signal.
    if save:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(OUTPUT_DIR / "s6_vrp_signals.csv", index=False)
    return df


# --------------------------------------------------------------------------- #
# Step 2 -- join to fixed-delta outcomes + monotonic-gradient quantile tables.
# --------------------------------------------------------------------------- #
def load_fixed_delta_outcomes() -> pd.DataFrame:
    """Load the reused fixed-0.15-delta trade outcomes (arm A_blind015).

    One row per (day, structure) with entry_credit, pnl_dollars, breached, exit_reason,
    spot_entry, entry_short_delta. We do NOT recompute anything -- this is the overlay's
    outcome source.
    """
    df = pd.read_csv(_ARM_A_TRADES)
    df = df[df["arm"] == "A_blind015"].copy()
    for b in ("traded", "breached"):
        df[b] = df[b].astype(str).str.lower().isin(["true", "1"])
    df = df[df["traded"]].copy()
    df["day"] = pd.to_datetime(df["day"]).dt.date
    keep = ["day", "structure", "entry_credit", "pnl_dollars", "breached",
            "exit_reason", "spot_entry", "entry_short_delta"]
    return df[keep]


def join_signal_outcome(sig_df: pd.DataFrame, out_df: pd.DataFrame) -> pd.DataFrame:
    """Join per-day VRP signal to the per-(day,structure) fixed-delta outcome."""
    s = sig_df[sig_df["ok"]].copy()
    s["day"] = pd.to_datetime(s["day"]).dt.date
    cols = ["day", "impl_2pm", "rv_morning", "rv_trail5", "vix_ts_prior",
            "vrp_primary", "vrp_trail"]
    j = out_df.merge(s[cols], on="day", how="inner")
    j["half"] = np.where(pd.to_datetime(j["day"]) <= pd.Timestamp(TRAIN_END),
                         "train", "test")
    return j


def _quantile_labels(n_bins: int) -> list[str]:
    if n_bins == 3:
        return ["T1_low", "T2_mid", "T3_high"]
    if n_bins == 5:
        return ["Q1_low", "Q2", "Q3", "Q4", "Q5_high"]
    return [f"B{i+1}" for i in range(n_bins)]


def quantile_table(joined: pd.DataFrame, signal: str, structure: str,
                   n_bins: int, subset: str = "all") -> pd.DataFrame:
    """Equal-count quantile table for one signal x structure.

    Bins are EQUAL-COUNT (pd.qcut), decided a priori (n_bins). NO tuned cutoffs. Per bin we
    report n, avg impl_2pm, avg rv_morning, avg credit, breach rate, win rate, total P&L,
    and credit/breach (reward-for-risk). subset in {all,train,test}.
    """
    df = joined[(joined["structure"] == structure) & joined[signal].notna()].copy()
    if subset != "all":
        df = df[df["half"] == subset]
    if len(df) < n_bins * 2:
        return pd.DataFrame()
    labels = _quantile_labels(n_bins)
    try:
        df["bin"] = pd.qcut(df[signal], q=n_bins, labels=labels, duplicates="drop")
    except ValueError:
        return pd.DataFrame()
    rows = []
    for lab in labels:
        cell = df[df["bin"] == lab]
        if len(cell) == 0:
            continue
        breach = float(cell["breached"].mean())
        credit = float(cell["entry_credit"].mean())
        wins = int((cell["pnl_dollars"] > 0).sum())
        rows.append({
            "signal": signal, "structure": structure, "subset": subset,
            "bins": n_bins, "bin": lab, "n": len(cell),
            "avg_impl_2pm": round(float(cell["impl_2pm"].mean()), 4),
            "avg_rv_morning": round(float(cell["rv_morning"].mean()), 4),
            "avg_signal": round(float(cell[signal].mean()), 4),
            "avg_credit": round(credit, 3),
            "breach_rate": round(breach, 4),
            "win_rate": round(wins / len(cell), 4),
            "total_pnl": round(float(cell["pnl_dollars"].sum()), 2),
            "avg_pnl": round(float(cell["pnl_dollars"].mean()), 2),
            "credit_per_breach": round(credit / breach, 3) if breach > 0 else float("inf"),
        })
    return pd.DataFrame(rows)


def _is_monotonic(values: list[float], increasing: bool) -> bool:
    """Strict-direction monotonic check ignoring NaN-free ordered list."""
    v = [x for x in values if np.isfinite(x)]
    if len(v) < 2:
        return False
    diffs = np.diff(v)
    return bool(np.all(diffs > 0)) if increasing else bool(np.all(diffs < 0))


def monotonicity_call(qt: pd.DataFrame, metric: str = "avg_pnl") -> str:
    """Is the metric monotonic across ordered quantiles (low->high signal)?

    HYPOTHESIS: richest-VRP quantile = best -> avg_pnl INCREASING and breach_rate
    DECREASING from low to high signal. We report the plain finding.
    """
    if qt.empty:
        return "n/a (empty)"
    labels = _quantile_labels(int(qt["bins"].iloc[0]))
    ordered = qt.set_index("bin").reindex(labels).dropna(subset=[metric])
    vals = ordered[metric].tolist()
    if metric == "breach_rate":
        mono = _is_monotonic(vals, increasing=False)  # expect breach DOWN as signal rises
    else:
        mono = _is_monotonic(vals, increasing=True)    # expect P&L UP as signal rises
    hi_gt_lo = (np.isfinite(vals[-1]) and np.isfinite(vals[0]) and
                (vals[-1] > vals[0] if metric != "breach_rate" else vals[-1] < vals[0]))
    if mono:
        return "MONOTONIC (hypothesis direction)"
    if hi_gt_lo:
        return "directional-but-not-monotonic (top beats bottom, middle out of order)"
    return "NO gradient (top does not beat bottom in hypothesis direction)"


# --------------------------------------------------------------------------- #
# Diagnostic: top vs bottom tercile P&L spread (DESCRIPTIVE, not a tuned recommendation).
# --------------------------------------------------------------------------- #
def top_vs_bottom_tercile(joined: pd.DataFrame, signal: str,
                          structure: str) -> dict:
    """P&L if you traded ONLY the top tercile vs ONLY the bottom tercile of the signal.

    DIAGNOSTIC ONLY -- shows the spread; this is NOT an optimized cutoff or a recommended
    strategy. Uses a priori equal-count terciles.
    """
    qt = quantile_table(joined, signal, structure, 3, "all")
    if qt.empty:
        return {"structure": structure, "signal": signal, "note": "empty"}
    top = qt[qt["bin"] == "T3_high"]
    bot = qt[qt["bin"] == "T1_low"]
    return {
        "structure": structure, "signal": signal,
        "top_tercile_pnl": float(top["total_pnl"].iloc[0]) if len(top) else float("nan"),
        "top_tercile_n": int(top["n"].iloc[0]) if len(top) else 0,
        "bottom_tercile_pnl": float(bot["total_pnl"].iloc[0]) if len(bot) else float("nan"),
        "bottom_tercile_n": int(bot["n"].iloc[0]) if len(bot) else 0,
    }


# --------------------------------------------------------------------------- #
# Verdict engine -- robust gradient requires BOTH halves AND monotone direction.
# --------------------------------------------------------------------------- #
def structure_verdict(joined: pd.DataFrame, signal: str, structure: str) -> str:
    """Plain verdict for one signal x structure: robust monotone gradient, or dead end?

    ROBUST := the top tercile beats the bottom (P&L up AND breach down) in the FULL sample
    AND the same direction holds in BOTH the train and test halves. Anything less = not
    robust = reject (curve-fit caution).
    """
    def dir_ok(sub: str) -> tuple[bool, bool]:
        qt = quantile_table(joined, signal, structure, 3, sub)
        if qt.empty:
            return (False, False)
        qt = qt.set_index("bin")
        if "T1_low" not in qt.index or "T3_high" not in qt.index:
            return (False, False)
        pnl_up = qt.loc["T3_high", "avg_pnl"] > qt.loc["T1_low", "avg_pnl"]
        breach_dn = qt.loc["T3_high", "breach_rate"] < qt.loc["T1_low", "breach_rate"]
        return (bool(pnl_up), bool(breach_dn))

    all_pnl, all_br = dir_ok("all")
    tr_pnl, tr_br = dir_ok("train")
    te_pnl, te_br = dir_ok("test")

    robust_pnl = all_pnl and tr_pnl and te_pnl
    robust_br = all_br and tr_br and te_br
    if robust_pnl and robust_br:
        return (f"{structure}/{signal}: ROBUST -- top VRP tercile beats bottom on BOTH "
                f"P&L and breach in the full sample AND both halves.")
    if robust_pnl or robust_br:
        which = "P&L" if robust_pnl else "breach"
        return (f"{structure}/{signal}: PARTIAL -- {which} gradient survives both halves but "
                f"the other does not; treat as NOT robust (reject).")
    if all_pnl or all_br:
        return (f"{structure}/{signal}: PEAK -- a gradient shows in the full sample but does "
                f"NOT survive both halves; reject (curve-fit caution).")
    return f"{structure}/{signal}: DEAD END -- no gradient (top tercile does not beat bottom)."


# --------------------------------------------------------------------------- #
# Full report driver.
# --------------------------------------------------------------------------- #
SIGNALS = ("vrp_primary", "vrp_trail", "vix_ts_prior")


def run_report(joined: pd.DataFrame, save: bool = True, verbose: bool = True) -> dict:
    """Build all quantile tables (tercile+quintile, all/train/test), monotonicity calls,
    top-vs-bottom diagnostics, and verdicts. ASCII-only prints."""
    all_qt = []
    mono_rows = []
    for signal in SIGNALS:
        for structure in STRUCTURES:
            for n_bins in (3, 5):
                for subset in ("all", "train", "test"):
                    qt = quantile_table(joined, signal, structure, n_bins, subset)
                    if qt.empty:
                        continue
                    all_qt.append(qt)
                    mono_rows.append({
                        "signal": signal, "structure": structure, "bins": n_bins,
                        "subset": subset,
                        "mono_pnl": monotonicity_call(qt, "avg_pnl"),
                        "mono_breach": monotonicity_call(qt, "breach_rate"),
                    })
    qt_all = pd.concat(all_qt, ignore_index=True) if all_qt else pd.DataFrame()
    mono_df = pd.DataFrame(mono_rows)

    diag_rows = [top_vs_bottom_tercile(joined, sg, st)
                 for sg in SIGNALS for st in STRUCTURES]
    diag_df = pd.DataFrame(diag_rows)

    verdict_lines = []
    for signal in SIGNALS:
        for structure in STRUCTURES:
            verdict_lines.append(structure_verdict(joined, signal, structure))
    verdicts_txt = "\n".join(verdict_lines)

    if save:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        qt_all.to_csv(OUTPUT_DIR / "s6_vrp_quantile_tables.csv", index=False)
        mono_df.to_csv(OUTPUT_DIR / "s6_vrp_monotonicity.csv", index=False)
        diag_df.to_csv(OUTPUT_DIR / "s6_vrp_top_vs_bottom.csv", index=False)
        with open(OUTPUT_DIR / "s6_vrp_verdicts.txt", "w", encoding="ascii",
                  errors="replace") as fh:
            fh.write(verdicts_txt + "\n")

    if verbose:
        with pd.option_context("display.width", 240, "display.max_columns", 40,
                               "display.max_rows", 800):
            print("\n=== S6 VRP QUANTILE TABLES (equal-count bins) ===", flush=True)
            print(qt_all.to_string(index=False), flush=True)
            print("\n=== MONOTONICITY CALLS ===", flush=True)
            print(mono_df.to_string(index=False), flush=True)
            print("\n=== TOP vs BOTTOM TERCILE (diagnostic only) ===", flush=True)
            print(diag_df.to_string(index=False), flush=True)
        print("\n=== VERDICTS ===", flush=True)
        print(verdicts_txt, flush=True)

    return {"quantiles": qt_all, "monotonicity": mono_df,
            "diagnostic": diag_df, "verdicts": verdicts_txt}


def run(verbose: bool = True, save: bool = True) -> dict:
    """Full pipeline: build signals -> finalize -> join outcomes -> report."""
    sig = build_signals(verbose=verbose)
    sig = finalize_signals(sig, save=save)
    out = load_fixed_delta_outcomes()
    joined = join_signal_outcome(sig, out)
    if save:
        joined.to_csv(OUTPUT_DIR / "s6_vrp_joined.csv", index=False)
    rep = run_report(joined, save=save, verbose=verbose)
    rep["signals"] = sig
    rep["joined"] = joined
    return rep


if __name__ == "__main__":
    run()
