r"""
s5_financing_sweep.py -- SHARED sweep + evaluation DRIVER for S5 financing structures.

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the options warehouse.
numpy / pandas. ASCII-only console output.

================================================================================
WHY THIS EXISTS (Phase-2a foundation)
================================================================================
Every S5 financing structure -- put credit spreads, iron condors, put-writes,
calendars/diagonals, sell-against-owned-tail -- will be judged by THIS driver, so the
bar is IDENTICAL across all of them and no structure gets a bespoke, curve-fit-friendly
evaluation. A Phase-2b structure worker declares a STRUCTURE SPEC (via s5_financing_harness
`Structure`/`Leg`/`Management` or the convenience builders) and a KNOB GRID, and calls
`sweep_structure(...)`. It gets back a standardized, comparable per-cell table.

This module OWNS the comparison protocol; it does NOT own the fill mechanics. All fills,
selection, management, and P&L come from `s5_financing_harness` (committed). This module
never re-implements a fill or a leg -- it only:
  * expands the pre-registered KNOB GRID into structures,
  * runs each cell on BOTH clean OOS windows SEPARATELY via the harness,
  * expresses net income as %/yr of a STATED core-notional sizing convention,
  * runs the shared EVALUATION BATTERY (OOS-by-window, matched placebo, regime buckets),
  * stores, per cell, the per-trade return series that deflated_sharpe.py needs so a LATER
    cross-structure SYNTHESIS step can run exact DSR over ALL cells at once.

DSR / multiple-comparisons is DELIBERATELY NOT applied here. Applying DSR per-structure would
under-correct (each structure is a slice of one big search). The driver stores the raw
per-trade returns per cell; synthesis pools every cell across every structure and deflates
against the TRUE trial count N. See `store` columns `ret_series_*` / the returned per-cell
`ret_series` payload.

================================================================================
KNOB GRID (the pre-registered space -- accepted as parameters, never hardcoded to one point)
================================================================================
  tenor_dte    : {7, 14, 30, 45}
  management   : {hold_to_expiry, profit_50, dte_21, profit_50_or_dte_21, stop_2x}
  short_delta  : {0.10, 0.15, 0.20, 0.30}
  regime       : {ungated, calm_only}
                 calm_only = STAND DOWN (take no new entry) on a day whose VIX term
                 structure is BACKWARDATED (VIX > VIX3M) OR whose VIX level is >= a stated
                 level (default 25). Both are CAUSAL: the signal uses only the entry day's
                 own EOD VIX / VIX3M close, which is known at the 16:00 mark that also prices
                 the entry fills -- no look-ahead.

A structure SPEC decides how these knobs map onto its legs (a put-write ignores `wing`;
a condor uses short_delta symmetrically; a calendar overrides per-leg DTE). The driver passes
(tenor_dte, management, short_delta) into the spec's builder and applies `regime` as an
entry-day filter on top -- so the SAME driver runs every structure family.

================================================================================
SIZING / METRIC -- comparable to the tail carry
================================================================================
Sizing convention (STATED explicitly): ONE structure per unit of CORE SPX notional, where
one unit of core notional = (entry index level) * CONTRACT_MULTIPLIER dollars = the dollar
value of 1 SPX index unit at entry. A trade's return for the period is:

    trade_return = net_pnl / core_notional_at_entry
                 = net_pnl / (entry_underlying * CONTRACT_MULTIPLIER)

Net income is then reported as %/yr of core notional = (sum of net_pnl over a window)
/ (mean core notional) / (window years). This is directly comparable to a tail-hedge carry
quoted as %/yr-of-core: both are "dollars per year per dollar of SPX exposure."

Also reported per cell: Sharpe & Sortino (over cash rf=0) on the per-trade return series,
win rate, loss/win ratio (mean loss magnitude / mean win), and the CRASH-WINDOW realized
exit cost (managed early closes pay honest bid/ask -- surfaced, not hidden).
"""
from __future__ import annotations

import datetime as _dt
import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np
import pandas as pd

import s5_financing_harness as h

try:
    from src.deflated_sharpe import sharpe_and_moments  # reused, never reimplemented
except Exception:  # pragma: no cover - allow import from the backtester/ folder directly
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from src.deflated_sharpe import sharpe_and_moments


# --------------------------------------------------------------------------- #
# The pre-registered KNOB GRID (the SPACE, not a chosen point).
# --------------------------------------------------------------------------- #
TENOR_DTE_GRID: tuple[int, ...] = (7, 14, 30, 45)
SHORT_DELTA_GRID: tuple[float, ...] = (0.10, 0.15, 0.20, 0.30)
MANAGEMENT_GRID: tuple[str, ...] = (
    "hold_to_expiry", "profit_50", "dte_21", "profit_50_or_dte_21", "stop_2x",
)
REGIME_GRID: tuple[str, ...] = ("ungated", "calm_only")

# The two clean OOS windows -- evaluated SEPARATELY (mirrors the harness's clean windows).
WINDOW_A = (_dt.date(2018, 1, 2), _dt.date(2020, 8, 12))
WINDOW_B = (_dt.date(2022, 1, 3), _dt.date(2026, 7, 2))
WINDOWS = {"A": WINDOW_A, "B": WINDOW_B}

# calm_only gate default level: stand down when VIX >= this OR when VIX > VIX3M
# (term-structure backwardation). A STATED threshold, not tuned to any P&L.
CALM_VIX_LEVEL = 25.0

# Crash windows for the realized-exit-cost surface (managed early closes in stress). These
# are STATED, well-known equity stress windows -- used only to LABEL trades for reporting,
# never to select or tune.
CRASH_WINDOWS = {
    "covid_2020": (_dt.date(2020, 2, 19), _dt.date(2020, 4, 7)),
    "bear_2022": (_dt.date(2022, 1, 3), _dt.date(2022, 10, 14)),
    "aug_2024_vol": (_dt.date(2024, 8, 1), _dt.date(2024, 8, 9)),
}

TRADING_DAYS_PER_YEAR = 252.0

_VIX_DIR = Path(r"C:\TradingDesk-Local\bt_data")


# --------------------------------------------------------------------------- #
# Management-knob -> harness Management mapping.
# --------------------------------------------------------------------------- #
def build_management(management: str) -> h.Management:
    """Map a management KNOB (from MANAGEMENT_GRID) to a harness `Management` rule.

    hold_to_expiry      -> hold (cash-settle every leg)
    profit_50           -> close at +50% of entry credit
    dte_21              -> close at 21 calendar DTE
    profit_50_or_dte_21 -> whichever of the two fires first
    stop_2x             -> hold, plus a 2x-credit loss stop
    """
    if management == "hold_to_expiry":
        return h.Management(mode="hold")
    if management == "profit_50":
        return h.Management(mode="profit_target", profit_target=0.50)
    if management == "dte_21":
        return h.Management(mode="time_exit", time_exit_dte=21)
    if management == "profit_50_or_dte_21":
        return h.Management(mode="target_or_time", profit_target=0.50, time_exit_dte=21)
    if management == "stop_2x":
        return h.Management(mode="hold", stop_mult=2.0)
    raise ValueError(f"unknown management knob {management!r}; must be in {MANAGEMENT_GRID}")


# --------------------------------------------------------------------------- #
# STRUCTURE SPEC -- what a Phase-2b worker declares.
# --------------------------------------------------------------------------- #
@dataclass
class StructureSpec:
    """Declares a structure FAMILY: a builder that turns (tenor_dte, short_delta, management)
    into a concrete harness `Structure`, plus which knob axes actually apply to it.

    `builder(tenor_dte, short_delta, management_obj) -> h.Structure`.
    A put-write builder ignores `short_delta`-driven wings; a condor uses it symmetrically;
    a calendar builder can override per-leg DTE. The driver never assumes a leg shape -- it
    only calls the builder and then applies the regime filter uniformly.

    `net_credit` declares whether the structure is a net-CREDIT seller (True; income = carry
    like the harness's put credit spread / condor / put-write) or a net-DEBIT structure
    (False; e.g. a long calendar). It only affects reporting labels, not P&L.
    """
    name: str
    builder: Callable[[int, float, h.Management], h.Structure]
    net_credit: bool = True
    # which knob axes to actually sweep for this family (subset of the full grid). Defaults
    # to the full pre-registered grid; a family that (say) is delta-insensitive can pin it.
    tenor_dte_grid: Sequence[int] = TENOR_DTE_GRID
    short_delta_grid: Sequence[float] = SHORT_DELTA_GRID
    management_grid: Sequence[str] = MANAGEMENT_GRID
    regime_grid: Sequence[str] = REGIME_GRID


def put_credit_spread_spec(wing: float = 10.0) -> StructureSpec:
    """The validation structure family: an `wing`-wide put credit spread whose short strike
    tracks the `short_delta` knob. Wing width is a STATED structural choice of the family
    (10-wide here), not a swept knob."""
    def _build(dte: int, short_delta: float, mgmt: h.Management) -> h.Structure:
        return h.put_credit_spread(dte=dte, short_delta=short_delta, wing=wing,
                                   management=mgmt,
                                   name=f"pcs_{dte}d_{short_delta}d_{int(wing)}w")
    return StructureSpec(name=f"put_credit_spread_{int(wing)}w", builder=_build,
                         net_credit=True)


# --------------------------------------------------------------------------- #
# CAUSAL VIX / term-structure regime signal (for calm_only).
# --------------------------------------------------------------------------- #
_VIX_CACHE: dict[str, pd.Series] = {}


def _load_vix_series(name: str) -> pd.Series:
    """Load a date-indexed VIX-family close series (READ-ONLY) from bt_data, cached.
    `name` in {'vix','vix3m'}. Index is python date; value is the close."""
    if name in _VIX_CACHE:
        return _VIX_CACHE[name]
    path = _VIX_DIR / f"_{name}.parquet"
    if not path.is_file():
        raise FileNotFoundError(f"no VIX-family series {name} at {path}")
    df = pd.read_parquet(path)
    s = df.iloc[:, 0]
    s.index = pd.to_datetime(s.index).normalize()
    s = s.sort_index()
    s.index = [d.date() for d in s.index]
    s = pd.Series(s.values, index=list(s.index), name=name)
    _VIX_CACHE[name] = s
    return s


def calm_entry_filter(vix_level: float = CALM_VIX_LEVEL) -> Callable[[_dt.date], bool]:
    """Return a CAUSAL entry-day predicate: True = OK to enter (calm), False = stand down.

    Stand down (return False) on a day whose OWN EOD close shows VIX >= `vix_level` OR
    VIX > VIX3M (term-structure backwardation). Uses each series' as-of value on the entry
    day (the last available close on or before `d` -- a small tail gap in VIX3M is handled by
    as-of ffill, NEVER by peeking at a future close). If a signal is entirely missing for a
    day, we STAND DOWN (fail-safe: no entry rather than a blind entry).
    """
    vix = _load_vix_series("vix")
    vix3m = _load_vix_series("vix3m")
    vix_dates = np.array(sorted(vix.index))
    v3_dates = np.array(sorted(vix3m.index))

    def _asof(dates: np.ndarray, series: pd.Series, d: _dt.date) -> Optional[float]:
        # last index <= d (causal). None if d precedes the series start.
        pos = np.searchsorted(dates, d, side="right") - 1
        if pos < 0:
            return None
        return float(series.loc[dates[pos]])

    def _ok_to_enter(d: _dt.date) -> bool:
        v = _asof(vix_dates, vix, d)
        v3 = _asof(v3_dates, vix3m, d)
        if v is None or v3 is None:
            return False               # fail-safe: stand down when either signal is missing
        if v >= vix_level:
            return False               # elevated absolute vol -> stand down
        if v > v3:
            return False               # backwardation (VIX > VIX3M) -> stand down
        return True

    return _ok_to_enter


def _crash_label(d: _dt.date) -> Optional[str]:
    """Name of the crash window containing entry date `d`, or None."""
    for label, (lo, hi) in CRASH_WINDOWS.items():
        if lo <= d <= hi:
            return label
    return None


def _vix_asof(d: _dt.date) -> float:
    """Causal VIX close as-of entry day `d` (for the VIX-tercile regime bucket). NaN if
    before the series start."""
    vix = _load_vix_series("vix")
    dates = np.array(sorted(vix.index))
    pos = np.searchsorted(dates, d, side="right") - 1
    if pos < 0:
        return float("nan")
    return float(vix.loc[dates[pos]])


# --------------------------------------------------------------------------- #
# %/yr-of-core metric + per-trade return series.
# --------------------------------------------------------------------------- #
def _core_notional(entry_underlying: float) -> float:
    """One unit of core SPX notional at entry = index level * CONTRACT_MULTIPLIER ($)."""
    return float(entry_underlying) * h.CONTRACT_MULTIPLIER


def _trade_returns(trades: pd.DataFrame) -> pd.Series:
    """Per-trade return as a fraction of core notional at entry (the DSR-ready series)."""
    if trades.empty:
        return pd.Series(dtype=float)
    core = trades["entry_underlying"].astype(float) * h.CONTRACT_MULTIPLIER
    return (trades["net_pnl"].astype(float) / core).reset_index(drop=True)


def _window_years(start: _dt.date, end: _dt.date) -> float:
    return max((end - start).days / 365.25, 1e-9)


def _pct_yr_of_core(trades: pd.DataFrame, start: _dt.date, end: _dt.date) -> float:
    """Net income over [start,end] as %/yr of mean core notional. This is the headline metric,
    directly comparable to a tail carry quoted %/yr-of-core.

    Convention: ONE structure per unit of core notional per entry day. Total net income
    (sum net_pnl) / (mean core notional) gives cumulative return-on-core over the window;
    divide by window years to annualize. (Non-compounded / simple annualization -- the
    honest quote for a carry program sized at one unit of core.)
    """
    if trades.empty:
        return 0.0
    total_pnl = float(trades["net_pnl"].sum())
    mean_core = float((trades["entry_underlying"].astype(float) * h.CONTRACT_MULTIPLIER).mean())
    if mean_core <= 0:
        return 0.0
    return (total_pnl / mean_core) / _window_years(start, end)


def _sharpe_sortino(returns: pd.Series) -> tuple[float, float]:
    """Per-trade Sharpe & Sortino over cash (rf=0), ANNUALIZED by sqrt(trades/yr proxy).

    We annualize the per-trade Sharpe by sqrt(N_per_year). With one entry per clean trading
    day, N_per_year ~= TRADING_DAYS_PER_YEAR, so the factor is sqrt(252). This makes the
    number comparable to a daily-return Sharpe. (Stored per-trade returns remain
    per-observation for exact DSR later; only this reported number is annualized.)
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 2:
        return float("nan"), float("nan")
    mu = r.mean()
    sd = r.std(ddof=0)
    ann = np.sqrt(TRADING_DAYS_PER_YEAR)
    sharpe = float(mu / sd * ann) if sd > 0 else float("nan")
    downside = r[r < 0.0]
    dd = np.sqrt((downside ** 2).mean()) if downside.size else 0.0
    sortino = float(mu / dd * ann) if dd > 0 else float("nan")
    return sharpe, sortino


def _win_stats(trades: pd.DataFrame) -> tuple[float, float]:
    """Win rate and loss/win ratio (mean loss magnitude / mean win magnitude)."""
    if trades.empty:
        return float("nan"), float("nan")
    pnl = trades["net_pnl"].astype(float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    win_rate = float((pnl > 0).mean())
    mean_win = float(wins.mean()) if len(wins) else 0.0
    mean_loss = float(-losses.mean()) if len(losses) else 0.0
    lw = float(mean_loss / mean_win) if mean_win > 0 else float("nan")
    return win_rate, lw


def _crash_exit_cost(trades: pd.DataFrame) -> dict:
    """Realized net P&L per stress window, split by managed-early-close vs. settle, so the
    honest crash-exit cost of managed structures is SURFACED (not hidden in the average).

    Returns {crash_label: {'n', 'net_pnl_per_core_pct', 'n_managed_close', 'n_settle',
    'managed_net_per_core_pct'}}. Keyed by ENTRY-day crash label."""
    out: dict = {}
    if trades.empty:
        return out
    t = trades.copy()
    t["crash"] = t["entry_date"].apply(_crash_label)
    t["core"] = t["entry_underlying"].astype(float) * h.CONTRACT_MULTIPLIER
    for label in CRASH_WINDOWS:
        sub = t[t["crash"] == label]
        if sub.empty:
            continue
        managed = sub[sub["exit_reason"].isin(("profit_target", "time_exit", "stop"))]
        settle = sub[sub["exit_reason"] == "settle"]
        mean_core = float(sub["core"].mean())
        out[label] = {
            "n": int(len(sub)),
            "net_pnl_per_core_pct": float(sub["net_pnl"].sum() / mean_core * 100.0)
                                    if mean_core > 0 else float("nan"),
            "n_managed_close": int(len(managed)),
            "n_settle": int(len(settle)),
            "managed_net_per_core_pct": (
                float(managed["net_pnl"].sum() / mean_core * 100.0)
                if (mean_core > 0 and len(managed)) else float("nan")
            ),
        }
    return out


# --------------------------------------------------------------------------- #
# REGIME BUCKETS: net income by VIX tercile AND by calendar year.
# --------------------------------------------------------------------------- #
def _regime_buckets(trades: pd.DataFrame) -> dict:
    """Break net %/yr-of-core-equivalent (here: mean per-trade %-of-core, and total $ share)
    down by (a) VIX tercile of the entry day and (b) calendar year -- to expose a result that
    lives in one regime. Returns {'vix_tercile': {...}, 'year': {...}}."""
    out: dict = {"vix_tercile": {}, "year": {}}
    if trades.empty:
        return out
    t = trades.copy()
    t["core"] = t["entry_underlying"].astype(float) * h.CONTRACT_MULTIPLIER
    t["ret"] = t["net_pnl"].astype(float) / t["core"]
    t["vix"] = t["entry_date"].apply(_vix_asof)
    t["year"] = t["entry_date"].apply(lambda d: d.year)

    valid_vix = t[t["vix"].notna()]
    if len(valid_vix) >= 3:
        try:
            t["vix_bucket"] = pd.qcut(t["vix"], 3, labels=["low", "mid", "high"],
                                      duplicates="drop")
        except ValueError:
            t["vix_bucket"] = np.nan
        for b in ("low", "mid", "high"):
            sub = t[t["vix_bucket"] == b]
            if len(sub):
                out["vix_tercile"][b] = {
                    "n": int(len(sub)),
                    "mean_ret_pct": float(sub["ret"].mean() * 100.0),
                    "total_net_pnl": float(sub["net_pnl"].sum()),
                    "vix_range": [float(sub["vix"].min()), float(sub["vix"].max())],
                }
    for yr, sub in t.groupby("year"):
        out["year"][int(yr)] = {
            "n": int(len(sub)),
            "mean_ret_pct": float(sub["ret"].mean() * 100.0),
            "total_net_pnl": float(sub["net_pnl"].sum()),
        }
    return out


# --------------------------------------------------------------------------- #
# MATCHED PLACEBO (the test that killed the gap-gate).
# --------------------------------------------------------------------------- #
def _placebo_percentile(all_trades: pd.DataFrame, gated_trades: pd.DataFrame,
                        n_draws: int, seed: int, window_years: float) -> dict:
    """Compare the REAL rule's net-%/yr-of-core to `n_draws` random controls that trade the
    SAME number of days, drawn from the same candidate entry-day universe.

    For a REGIME-GATED cell, `gated_trades` is the subset the gate actually took; the placebo
    randomly SITS OUT to reach the same trade count from `all_trades` (random sit-out).
    For an UNGATED cell, `gated_trades == all_trades` and the placebo is a random-entry-day
    control of the same count drawn from `all_trades` (degenerate -> real==sampled universe;
    percentile reflects sampling variability of the count-matched mean).

    The metric is EXACTLY `_pct_yr_of_core` = sum(net_pnl)/mean(core)/window_years, computed
    here in vectorized numpy over all draws at once (the frame is small but n_draws is large,
    so per-draw DataFrame slicing was the sweep's hot spot).

    Returns {'real_metric', 'placebo_mean', 'placebo_std', 'percentile', 'n_draws',
    'n_selected', 'n_universe', 'beats_placebo'}. `percentile` = fraction of placebo draws
    whose metric is <= the real metric (so ~1.0 means the real rule is at the top -> a genuine
    edge; ~0.5 means indistinguishable from random selection -> no edge).
    """
    n_universe = len(all_trades)
    n_selected = len(gated_trades)

    def _metric_arrays(pnl: np.ndarray, core: np.ndarray) -> float:
        if pnl.size == 0:
            return 0.0
        mc = core.mean()
        if mc <= 0:
            return 0.0
        return (pnl.sum() / mc) / window_years

    real_pnl = gated_trades["net_pnl"].to_numpy(dtype=float) if n_selected else np.empty(0)
    real_core = ((gated_trades["entry_underlying"].to_numpy(dtype=float)
                  * h.CONTRACT_MULTIPLIER) if n_selected else np.empty(0))
    real_metric = _metric_arrays(real_pnl, real_core)
    result = {
        "real_metric": float(real_metric) if np.isfinite(real_metric) else float("nan"),
        "placebo_mean": float("nan"), "placebo_std": float("nan"),
        "percentile": float("nan"), "n_draws": int(n_draws),
        "n_selected": int(n_selected), "n_universe": int(n_universe),
        "beats_placebo": None,
    }
    if n_universe == 0 or n_selected == 0 or n_selected >= n_universe:
        # nothing to randomize against (or gate took everything) -> placebo undefined.
        return result

    all_pnl = all_trades["net_pnl"].to_numpy(dtype=float)
    all_core = all_trades["entry_underlying"].to_numpy(dtype=float) * h.CONTRACT_MULTIPLIER
    rng = np.random.default_rng(seed)
    idx = np.arange(n_universe)
    # Vectorized draws: build an (n_draws, n_selected) index matrix by argsort-of-random
    # (an exact without-replacement sample per row), then gather + reduce along axis 1.
    keys = rng.random((n_draws, n_universe))
    picks = np.argpartition(keys, n_selected - 1, axis=1)[:, :n_selected]
    pnl_draws = all_pnl[picks]                      # (n_draws, n_selected)
    core_draws = all_core[picks]
    mc = core_draws.mean(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        draws = (pnl_draws.sum(axis=1) / mc) / window_years
    draws = draws[np.isfinite(draws)]
    if draws.size == 0:
        return result
    pct = float((draws <= real_metric).mean())
    result.update({
        "placebo_mean": float(draws.mean()),
        "placebo_std": float(draws.std(ddof=0)),
        "percentile": pct,
        "beats_placebo": bool(pct >= 0.95),   # top-5% of the matched null
    })
    return result


# --------------------------------------------------------------------------- #
# One CELL (one grid point) on one window.
# --------------------------------------------------------------------------- #
def _run_cell_window(spec: StructureSpec, tenor_dte: int, short_delta: float,
                     management: str, regime: str, window_key: str,
                     placebo_draws: int, seed: int) -> dict:
    """Run one grid CELL on one clean window. Returns a flat dict of metrics + a nested
    payload (crash cost, regime buckets, placebo, DSR-ready returns)."""
    start, end = WINDOWS[window_key]
    mgmt = build_management(management)
    structure = spec.builder(tenor_dte, short_delta, mgmt)

    # UNGATED: enter every clean day in the window. GATED: pass a causal calm-only entry set.
    entry_days = None
    if regime == "calm_only":
        ok = calm_entry_filter()
        all_days = [d for d in h.available_days(clean_only=True) if start <= d <= end]
        entry_days = [d for d in all_days if ok(d)]

    trades = h.backtest_structure(structure, start=start, end=end, entry_days=entry_days)
    # For the placebo of a GATED cell we need the SAME structure run UNGATED over the window
    # (the candidate universe the gate selected from). For an ungated cell it is the same run.
    if regime == "calm_only":
        all_trades = h.backtest_structure(structure, start=start, end=end)
    else:
        all_trades = trades

    ret_series = _trade_returns(trades)
    net_pct_yr = _pct_yr_of_core(trades, start, end)
    sharpe, sortino = _sharpe_sortino(ret_series)
    win_rate, loss_win = _win_stats(trades)

    # placebo metric = net %/yr-of-core on the count-matched subset (the headline metric).
    placebo = _placebo_percentile(all_trades, trades, placebo_draws, seed,
                                  _window_years(start, end))

    return {
        "window": window_key,
        "n_trades": int(len(trades)),
        "n_universe": int(len(all_trades)),
        "net_pct_yr_of_core": float(net_pct_yr),
        "sharpe_ann": sharpe,
        "sortino_ann": sortino,
        "win_rate": win_rate,
        "loss_win_ratio": loss_win,
        "mean_entry_credit": float(trades["entry_credit"].mean()) if len(trades) else float("nan"),
        "mean_net_pnl": float(trades["net_pnl"].mean()) if len(trades) else float("nan"),
        "truncated_dropped": int(trades.attrs.get("truncated_dropped", 0)),
        "entry_rejects": dict(trades.attrs.get("entry_rejects", {})),
        "fill_rate": (float(len(trades)) / float(len(all_trades))
                      if (regime == "ungated" and len(all_trades)) else float("nan")),
        # nested payloads (stored on the store row as objects; flattened columns above)
        "_crash_exit_cost": _crash_exit_cost(trades),
        "_regime_buckets": _regime_buckets(trades),
        "_placebo": placebo,
        "_ret_series": ret_series.tolist(),   # DSR-ready per-trade returns (per-observation)
    }


# --------------------------------------------------------------------------- #
# THE PUBLIC DRIVER.
# --------------------------------------------------------------------------- #
def sweep_structure(spec: StructureSpec,
                    tenor_dte_grid: Optional[Sequence[int]] = None,
                    short_delta_grid: Optional[Sequence[float]] = None,
                    management_grid: Optional[Sequence[str]] = None,
                    regime_grid: Optional[Sequence[str]] = None,
                    placebo_draws: int = 500,
                    seed: int = 12345,
                    out_dir: Optional[Path] = None,
                    write: bool = True,
                    verbose: bool = True) -> pd.DataFrame:
    """Sweep `spec` over the (subset of the) pre-registered knob grid and evaluate every cell
    on BOTH clean windows SEPARATELY with the shared battery. Returns a tidy per-cell-per-
    window DataFrame AND (if write) persists it to `out_dir` (default backtester/output/,
    gitignored) as parquet + CSV.

    A Phase-2b structure worker calls THIS. Grids default to the spec's declared axes (which
    default to the full pre-registered grid). `placebo_draws` controls the matched-placebo
    resample count; `seed` fixes it.

    Each returned ROW is one (tenor_dte x management x short_delta x regime x window) cell:
      identity columns : structure, tenor_dte, management, short_delta, regime, window
      metric columns   : net_pct_yr_of_core, sharpe_ann, sortino_ann, win_rate,
                         loss_win_ratio, n_trades, n_universe, fill_rate, mean_entry_credit,
                         mean_net_pnl, truncated_dropped
      placebo columns  : placebo_percentile, placebo_beats (bool), placebo_real,
                         placebo_mean, placebo_n_selected, placebo_n_universe
      object columns   : crash_exit_cost (dict), regime_buckets (dict), entry_rejects (dict),
                         ret_series (list[float]) -- the DSR-ready per-trade returns.

    A SEPARATE synthesis step consumes ret_series across ALL structures to run exact DSR
    against the TRUE trial count N. df.attrs['sign_consistency'] holds, per (tenor,mgmt,
    delta,regime) cell, whether its net %/yr has the SAME SIGN in window A and window B.
    """
    tenor_dte_grid = tenor_dte_grid if tenor_dte_grid is not None else spec.tenor_dte_grid
    short_delta_grid = short_delta_grid if short_delta_grid is not None else spec.short_delta_grid
    management_grid = management_grid if management_grid is not None else spec.management_grid
    regime_grid = regime_grid if regime_grid is not None else spec.regime_grid

    combos = list(itertools.product(tenor_dte_grid, management_grid, short_delta_grid,
                                    regime_grid))
    rows = []
    n_cells = len(combos) * len(WINDOWS)
    done = 0
    for (tenor_dte, management, short_delta, regime) in combos:
        for window_key in WINDOWS:
            r = _run_cell_window(spec, tenor_dte, short_delta, management, regime,
                                 window_key, placebo_draws, seed)
            placebo = r.pop("_placebo")
            row = {
                "structure": spec.name,
                "tenor_dte": tenor_dte,
                "management": management,
                "short_delta": short_delta,
                "regime": regime,
                "window": r["window"],
                "net_pct_yr_of_core": r["net_pct_yr_of_core"],
                "sharpe_ann": r["sharpe_ann"],
                "sortino_ann": r["sortino_ann"],
                "win_rate": r["win_rate"],
                "loss_win_ratio": r["loss_win_ratio"],
                "n_trades": r["n_trades"],
                "n_universe": r["n_universe"],
                "fill_rate": r["fill_rate"],
                "mean_entry_credit": r["mean_entry_credit"],
                "mean_net_pnl": r["mean_net_pnl"],
                "truncated_dropped": r["truncated_dropped"],
                "placebo_percentile": placebo["percentile"],
                "placebo_beats": placebo["beats_placebo"],
                "placebo_real": placebo["real_metric"],
                "placebo_mean": placebo["placebo_mean"],
                "placebo_n_selected": placebo["n_selected"],
                "placebo_n_universe": placebo["n_universe"],
                # object columns (kept for the report + synthesis)
                "crash_exit_cost": r["_crash_exit_cost"],
                "regime_buckets": r["_regime_buckets"],
                "entry_rejects": r["entry_rejects"],
                "ret_series": r["_ret_series"],
            }
            rows.append(row)
            done += 1
            if verbose:
                print(f"[{done}/{n_cells}] {spec.name} dte={tenor_dte} "
                      f"mgmt={management} d={short_delta} {regime} win={window_key} "
                      f"n={r['n_trades']} netpctyr={r['net_pct_yr_of_core']:+.4f} "
                      f"plc={placebo['percentile']}", flush=True)

    df = pd.DataFrame(rows)

    # sign-consistency across windows A/B per (tenor,mgmt,delta,regime) cell.
    sign_consistency: dict = {}
    if not df.empty:
        for key, g in df.groupby(["tenor_dte", "management", "short_delta", "regime"]):
            by_win = g.set_index("window")["net_pct_yr_of_core"]
            a = by_win.get("A", float("nan"))
            b = by_win.get("B", float("nan"))
            same = (np.isfinite(a) and np.isfinite(b) and (np.sign(a) == np.sign(b))
                    and a != 0 and b != 0)
            sign_consistency[key] = {
                "A": float(a) if np.isfinite(a) else None,
                "B": float(b) if np.isfinite(b) else None,
                "sign_consistent": bool(same),
            }
    df.attrs["sign_consistency"] = sign_consistency

    if write:
        out_dir = out_dir or (Path(__file__).resolve().parent / "output")
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = _dt.date.today().strftime("%Y%m%d")
        base = out_dir / f"s5_sweep_{spec.name}_{stamp}"
        # object columns don't round-trip to parquet cleanly -> JSON-encode them for storage.
        store = df.copy()
        # df.attrs['sign_consistency'] is tuple-keyed (in-memory convenience); pandas tries to
        # serialize .attrs into the parquet schema metadata and chokes on tuple keys, so drop
        # attrs from the STORED frame. Sign-consistency is recoverable from the window rows,
        # and also persisted as its own JSON sidecar below.
        store.attrs = {}
        import json
        for col in ("crash_exit_cost", "regime_buckets", "entry_rejects", "ret_series"):
            store[col] = store[col].apply(json.dumps)
        try:
            store.to_parquet(base.with_suffix(".parquet"), index=False)
        except Exception as e:   # pragma: no cover - parquet engine optional
            print(f"[warn] parquet write failed ({type(e).__name__}: {e}); CSV only",
                  flush=True)
        store.to_csv(base.with_suffix(".csv"), index=False)
        # sign-consistency sidecar (tuple keys stringified) so synthesis/reports can read the
        # cross-window sign read without recomputing.
        sc = {"|".join(str(x) for x in k): v
              for k, v in df.attrs.get("sign_consistency", {}).items()}
        base.with_name(base.name + "_sign_consistency.json").write_text(json.dumps(sc, indent=2))
        if verbose:
            print(f"[written] {base}.parquet / .csv  ({len(df)} cells)", flush=True)

    return df


# --------------------------------------------------------------------------- #
# Small console summary (for the validation run).
# --------------------------------------------------------------------------- #
def print_summary(df: pd.DataFrame) -> None:
    """ASCII per-cell summary table + the sign-consistency read."""
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cols = ["structure", "tenor_dte", "management", "short_delta", "regime", "window",
            "n_trades", "net_pct_yr_of_core", "sharpe_ann", "win_rate", "loss_win_ratio",
            "fill_rate", "placebo_percentile"]
    show = df[cols].copy()
    show["net_pct_yr_of_core"] = (show["net_pct_yr_of_core"] * 100).round(3)
    show = show.rename(columns={"net_pct_yr_of_core": "net%/yr"})
    for c in ("sharpe_ann", "win_rate", "loss_win_ratio", "fill_rate", "placebo_percentile"):
        show[c] = show[c].round(3)
    print(show.to_string(index=False))
    print("\nsign-consistency across windows A/B (net %/yr sign):")
    for key, v in df.attrs.get("sign_consistency", {}).items():
        print(f"  dte={key[0]} mgmt={key[1]} d={key[2]} {key[3]}: "
              f"A={v['A']} B={v['B']} consistent={v['sign_consistent']}")
