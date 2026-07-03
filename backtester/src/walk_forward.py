"""
walk_forward.py — Rolling / anchored MULTI-window walk-forward validation.

This is ADDITIVE measurement instrumentation. It is strictly opt-in: importing or
calling anything here changes no existing output, and it touches no strategy config.
It is NOT a strategy change — it only *scores* an existing strategy more honestly.

Why this exists (and how it differs from metrics.split_walk_forward)
--------------------------------------------------------------------
`src/metrics.py:split_walk_forward()` makes ONE fixed cut: everything before a date
is in-sample, everything after is out-of-sample. That answers "did the frozen rules
survive the single most recent regime?" but it leans on one arbitrary boundary.

`rolling_walk_forward()` here chops the history into N sequential windows. Inside each
window the first `is_frac` is in-sample and the trailing remainder is out-of-sample.
We keep ONLY the OOS tail of each window, stitch every tail into one continuous series,
and score Sharpe + max drawdown on that STITCHED OOS series. The stitched OOS is the
honest yardstick: it is composed exclusively of periods that were never "in-sample" for
their own window, sampled across many different regimes rather than one.

  mode="rolling"  : each window's in-sample slides forward with the window (a sliding
                    train that always sits immediately before its own OOS tail).
  mode="anchored" : the in-sample always begins at the very start of history and grows;
                    only the OOS tail moves forward. (Classic "anchored walk-forward".)

No-look-ahead is structural here:
  * Within a window, OOS is strictly AFTER its in-sample (contiguous, non-overlapping).
  * Between windows, the windows are laid end-to-end over time, so no window's data is
    ever earlier than a prior window's OOS — later data can never leak into an earlier
    score. The caller-supplied `run_fn` is handed each window's IS/OOS slices separately
    and must not peek past what it is given (see the run_fn contract below).

Honest thin-sample guard (baked in, loud by design)
---------------------------------------------------
Slow strategies (e.g. S0, a few trades/yr) produce OOS tails with very few observations.
A Sharpe on 12 days is noise dressed as a number. So every per-window OOS length is
surfaced in the result, and any window whose OOS length < THIN_N (default 30 trading
days, mirroring s6_matrix.THIN_N) is flagged with a WARNING. The stitched OOS is also
checked. Thin results are made LOUD, never silent.

DESIGN CHOICES — BLESSED 2026-07-03 by Andrew (accepted defaults, NOT frozen config)
------------------------------------------------------------------------------------
  * n_windows = 5   — how many sequential windows to cut. More windows = more regimes
                      sampled but thinner tails. Blessed default.
  * is_frac   = 0.70 — in-sample fraction inside each window (70/30 train/test). Blessed.
  * mode      = "rolling" — rolling vs anchored default. Blessed.
  * THIN_N    = 30  — reused from s6_matrix's convention (min usable trade-days). Blessed.
These are function parameters with documented defaults so they stay easy to change —
nothing here is tuned to a period.

OPERATIONAL NOTE — WHAT THIS HARNESS IS FOR (and what it is NOT)
---------------------------------------------------------------
This harness is intended for CAN SLIM and other higher-frequency signals that generate
enough observations to fill 5 OOS tails. It is NOT to be run on S0 (a slow allocator):
5 windows x 30% OOS slices fall below THIN_N, so the result is noise, not a finding.
S0's validation stays episode/regime + block bootstrap — do not walk-forward S0 here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import warnings

import numpy as np
import pandas as pd

from src import metrics

# Minimum OOS observations for a window's score to be trustworthy. Mirrors the
# established s6_matrix.THIN_N = 30 (trade-days) convention rather than inventing a new one.
THIN_N = 30


@dataclass
class WindowResult:
    """One walk-forward window's in-sample / out-of-sample spans, lengths, and OOS metrics."""

    index: int
    is_start: pd.Timestamp
    is_end: pd.Timestamp
    oos_start: pd.Timestamp
    oos_end: pd.Timestamp
    is_len: int
    oos_len: int
    oos_thin: bool
    oos_metrics: pd.DataFrame  # compute_metrics() table on this window's OOS segment
    oos_navs: pd.DataFrame  # the OOS-segment NAV frame (re-based to 1.0 at its start)


@dataclass
class WalkForwardResult:
    """Structured output of rolling_walk_forward()."""

    mode: str
    n_windows: int
    is_frac: float
    thin_n: int
    windows: list[WindowResult] = field(default_factory=list)
    stitched_oos_navs: pd.DataFrame = field(default_factory=pd.DataFrame)
    stitched_oos_metrics: pd.DataFrame = field(default_factory=pd.DataFrame)
    stitched_oos_len: int = 0
    stitched_oos_thin: bool = False
    warnings: list[str] = field(default_factory=list)

    # Convenience accessors for the two headline numbers on the stitched OOS series.
    @property
    def stitched_oos_sharpe(self) -> float:
        return float(self.stitched_oos_metrics.loc["Sharpe", "strategy"])

    @property
    def stitched_oos_max_drawdown(self) -> float:
        return float(self.stitched_oos_metrics.loc["Max drawdown", "strategy"])


def _as_navs_frame(obj) -> pd.DataFrame:
    """
    Normalize an input to a NAV DataFrame with a 'strategy' column on a DatetimeIndex.

    Accepts: a NAV Series (growth of $1), a returns Series (auto-detected & compounded),
    or a DataFrame already shaped for compute_metrics (must contain 'strategy').
    """
    if isinstance(obj, pd.DataFrame):
        if "strategy" not in obj.columns:
            raise ValueError("DataFrame input must contain a 'strategy' column")
        df = obj.copy()
    elif isinstance(obj, pd.Series):
        s = obj.dropna()
        # Heuristic: NAVs are positive and don't look like small daily returns. If the
        # series ever goes <= 0 or its typical magnitude is tiny, treat it as returns.
        looks_like_returns = (s <= 0).any() or s.abs().median() < 0.5
        nav = (1.0 + s).cumprod() if looks_like_returns else s
        df = nav.to_frame("strategy")
    else:
        raise TypeError("returns_or_navs must be a pandas Series or DataFrame")

    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError("input must have a DatetimeIndex")
    return df.sort_index()


def _rebase(navs: pd.DataFrame) -> pd.DataFrame:
    """Re-base every column so it starts at 1.0 at the segment's first row."""
    first = navs.iloc[0]
    # Avoid divide-by-zero on any degenerate column.
    first = first.replace(0.0, np.nan)
    return navs.divide(first, axis=1)


def _window_bounds(n: int, n_windows: int, is_frac: float) -> list[tuple[int, int, int, int]]:
    """
    Compute integer index bounds for each window as
        (is_lo, is_hi_exclusive, oos_lo, oos_hi_exclusive)
    where is_hi_exclusive == oos_lo (contiguous, no overlap) and windows tile [0, n)
    with no gaps and no overlap between successive OOS tails.

    Rolling caller uses is_lo = window start; anchored caller overrides is_lo = 0.
    """
    if n_windows < 1:
        raise ValueError("n_windows must be >= 1")
    if not (0.0 < is_frac < 1.0):
        raise ValueError("is_frac must be strictly between 0 and 1")
    if n < n_windows:
        raise ValueError(f"history length {n} is shorter than n_windows {n_windows}")

    # Even, contiguous window edges over [0, n]. Using linspace keeps windows balanced
    # and guarantees full coverage with no gaps/overlaps.
    edges = np.linspace(0, n, n_windows + 1).round().astype(int)
    bounds = []
    for w in range(n_windows):
        lo, hi = int(edges[w]), int(edges[w + 1])
        width = hi - lo
        # At least 1 obs in-sample and 1 obs out-of-sample within the window.
        is_hi = lo + max(1, min(width - 1, int(round(width * is_frac))))
        bounds.append((lo, is_hi, is_hi, hi))
    return bounds


def _default_run_fn(is_navs: pd.DataFrame, oos_navs: pd.DataFrame, window_index: int) -> pd.DataFrame:
    """
    Default backtest callable: passthrough. The input series is treated as an already-run
    NAV path, so a window's OOS "backtest" is simply that window's OOS slice, re-based to
    1.0 at the OOS start. In-sample is ignored (there is nothing to fit — the strategy is
    frozen). Real callers pass their own run_fn that re-runs the backtest constrained to
    each window's data; the harness never fits anything itself.
    """
    return _rebase(oos_navs)


def rolling_walk_forward(
    returns_or_navs,
    run_fn=None,
    n_windows: int = 5,
    is_frac: float = 0.70,
    mode: str = "rolling",
    thin_n: int = THIN_N,
) -> WalkForwardResult:
    """
    Multi-window walk-forward validation. See the module docstring for the full rationale.

    Parameters
    ----------
    returns_or_navs : pd.Series | pd.DataFrame
        A NAV series (growth of $1), a daily-returns series, or a DataFrame shaped for
        metrics.compute_metrics (must contain a 'strategy' column; benchmark columns like
        'SPY'/'60/40'/'T-bills' are carried through so OOS metrics stay comparable).
    run_fn : callable | None
        Backtest callable with signature run_fn(is_navs, oos_navs, window_index) -> navs,
        returning the window's OUT-OF-SAMPLE NAV frame (containing at least 'strategy').
        It MUST honor the split: it may look at `is_navs` to decide anything, but the OOS
        result must depend only on data at/after oos_start — no peeking past the OOS tail.
        Defaults to a passthrough that re-bases the OOS slice of the input (the common case
        where the input is an already-run NAV path).
    n_windows : int, default 5   (DESIGN CHOICE — blessed 2026-07-03)
    is_frac   : float, default 0.70  (DESIGN CHOICE — blessed 2026-07-03)
    mode      : {"rolling", "anchored"}, default "rolling"  (DESIGN CHOICE — blessed)
    thin_n    : int, default THIN_N (30)  min OOS trade-days for a trustworthy score.
        Intended for CAN SLIM / higher-frequency signals — NOT for S0 (see module docstring).

    Returns
    -------
    WalkForwardResult with per-window spans/lengths/OOS-metrics, the stitched-OOS series,
    its Sharpe / max-drawdown, per-window OOS lengths, and any thin-sample warnings.
    """
    if mode not in ("rolling", "anchored"):
        raise ValueError("mode must be 'rolling' or 'anchored'")
    if run_fn is None:
        run_fn = _default_run_fn

    navs = _as_navs_frame(returns_or_navs)
    n = len(navs)
    bounds = _window_bounds(n, n_windows, is_frac)

    result = WalkForwardResult(
        mode=mode, n_windows=n_windows, is_frac=is_frac, thin_n=thin_n
    )
    oos_pieces: list[pd.DataFrame] = []

    for w, (is_lo, is_hi, oos_lo, oos_hi) in enumerate(bounds):
        # Anchored: in-sample always starts at history start. Rolling: it starts at the
        # window's own left edge. OOS is identical either way (strictly after in-sample).
        real_is_lo = 0 if mode == "anchored" else is_lo

        is_slice = navs.iloc[real_is_lo:is_hi]
        oos_slice = navs.iloc[oos_lo:oos_hi]

        oos_result_navs = run_fn(is_slice, oos_slice, w)
        oos_result_navs = _as_navs_frame(oos_result_navs)

        oos_len = len(oos_result_navs)
        is_len = len(is_slice)
        thin = oos_len < thin_n
        if thin:
            result.warnings.append(
                f"Window {w}: OOS length {oos_len} < THIN_N {thin_n} — score is thin-sample "
                f"noise, do NOT treat as a finding."
            )

        wr = WindowResult(
            index=w,
            is_start=is_slice.index[0],
            is_end=is_slice.index[-1],
            oos_start=oos_result_navs.index[0],
            oos_end=oos_result_navs.index[-1],
            is_len=is_len,
            oos_len=oos_len,
            oos_thin=thin,
            oos_metrics=metrics.compute_metrics(oos_result_navs),
            oos_navs=oos_result_navs,
        )
        result.windows.append(wr)
        oos_pieces.append(oos_result_navs)

    # Stitch the OOS tails into one continuous series. Each piece is chained onto the end
    # of the previous piece's NAV level so the combined path is a single growth-of-$1 curve
    # (compounding across the joins, not resetting to 1.0 at every seam).
    stitched = _chain_navs(oos_pieces)
    result.stitched_oos_navs = stitched
    result.stitched_oos_len = len(stitched)
    result.stitched_oos_metrics = metrics.compute_metrics(stitched)
    result.stitched_oos_thin = len(stitched) < thin_n
    if result.stitched_oos_thin:
        result.warnings.append(
            f"STITCHED OOS length {len(stitched)} < THIN_N {thin_n} — the combined "
            f"out-of-sample sample is too thin to be meaningful."
        )

    for msg in result.warnings:
        warnings.warn(msg, stacklevel=2)

    return result


def _chain_navs(pieces: list[pd.DataFrame]) -> pd.DataFrame:
    """
    Chain a list of per-window OOS NAV frames into one continuous NAV curve.

    Each piece is first converted to its within-piece growth factors, then multiplied onto
    the running level from the prior piece so the seams compound rather than reset. Columns
    are aligned across pieces (missing benchmark columns in a piece are simply dropped from
    the intersection to keep the chained frame rectangular and honest).
    """
    pieces = [p for p in pieces if len(p) > 0]
    if not pieces:
        return pd.DataFrame()

    common_cols = set(pieces[0].columns)
    for p in pieces[1:]:
        common_cols &= set(p.columns)
    cols = [c for c in pieces[0].columns if c in common_cols]

    chained_parts: list[pd.DataFrame] = []
    running_level = pd.Series(1.0, index=cols)
    for p in pieces:
        seg = _rebase(p[cols])  # each segment starts at 1.0 internally
        seg = seg.multiply(running_level, axis=1)
        running_level = seg.iloc[-1]
        chained_parts.append(seg)

    out = pd.concat(chained_parts, axis=0)
    # Guard against any duplicated timestamps at seams (adjacent OOS tails are disjoint in
    # the normal case, but be defensive): keep the first occurrence.
    out = out[~out.index.duplicated(keep="first")].sort_index()
    return out


def format_report(result: WalkForwardResult) -> str:
    """Plain-text summary of a WalkForwardResult (lead with the answer, CLAUDE.md style)."""
    lines: list[str] = []
    m = result.stitched_oos_metrics
    lines.append(
        f"Rolling walk-forward ({result.mode}, {result.n_windows} windows, "
        f"is_frac={result.is_frac:.2f}) — STITCHED out-of-sample:"
    )
    lines.append(
        f"  Sharpe {result.stitched_oos_sharpe:.2f} | "
        f"max DD {result.stitched_oos_max_drawdown:.1%} | "
        f"OOS length {result.stitched_oos_len} days"
        + ("   [THIN — untrustworthy]" if result.stitched_oos_thin else "")
    )
    lines.append("  Per-window OOS tails:")
    for w in result.windows:
        sh = w.oos_metrics.loc["Sharpe", "strategy"]
        dd = w.oos_metrics.loc["Max drawdown", "strategy"]
        flag = "  [THIN]" if w.oos_thin else ""
        lines.append(
            f"    W{w.index}: IS {w.is_start.date()}..{w.is_end.date()} "
            f"({w.is_len}d) -> OOS {w.oos_start.date()}..{w.oos_end.date()} "
            f"({w.oos_len}d)  Sharpe {sh:.2f}, maxDD {dd:.1%}{flag}"
        )
    if result.warnings:
        lines.append("  WARNINGS:")
        for msg in result.warnings:
            lines.append(f"    ! {msg}")
    return "\n".join(lines)
