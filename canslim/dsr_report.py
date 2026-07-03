"""dsr_report.py — Deflated / Probabilistic Sharpe reporting for the CAN SLIM work.

ADDITIVE MEASUREMENT ONLY. This script computes nothing that feeds a strategy,
sizing, order, or the frozen selection logic. It reads a CAN SLIM backtest's
already-written return series off disk and reports how likely the observed Sharpe
reflects a genuinely positive edge rather than luck — the anti-curve-fit yardstick
(rule #1). It never edits, re-runs, or re-tunes the backtest.

THE HONEST N QUESTION (read this before trusting a number)
----------------------------------------------------------
The Deflated Sharpe Ratio (DSR) only differs from a plain significance check when a
strategy was SELECTED as the best of N independently searched trials. CAN SLIM's
`selection_backtest.py` is a SINGLE frozen, spec-pinned replica — the detector
bounds are the O'Neil spec, the exit is the already-proven E3 rule, and the
sizing/exposure knobs are the advisor's revealed behavior. NOTHING there is tuned
to improve the result (the module says so explicitly). So for the selection
backtest, N = 1: no multiple-comparisons search took place, and DSR correctly
DEGRADES to PSR-vs-zero (probability the true Sharpe > 0, adjusted for sample
length T and the return series' skew & fat tails). We report that honestly instead
of inventing an N.

WHERE A REAL N>1 SEARCH DOES EXIST
----------------------------------
The options-OVERLAY work (`options_overlay_real_results.csv`) sweeps an 88-cell
grid over (tenor x strike-offset x delta-trigger x budget) and reports the BEST
cell. THAT is a genuine multiple-comparisons search over the OVERLAY (selection is
still fixed to his picks). When pointed at that grid, this script computes the full
DSR with N = number of cells and var_trials = the observed variance of the cell
Sharpes — the correct deflation. This is reported per-cell/for-the-best-cell only
as an ADDITIVE honesty check on the overlay grid; it does not change the frozen
selection spec.

SHARPE UNITS
------------
All Sharpes here are PER-OBSERVATION (never annualized), matching the DSR module.
The CAN SLIM selection/overlay books produce a series of per-TRADE returns (each
closed position's realized return); one "observation" is one trade, and T is the
trade count. Per-trade returns are already de-annualized (a trade return is not a
rate per year), so no sqrt(periods) de-annualization is applied — a trade IS the
observation unit. This is stated so the T and Sharpe are not misread as daily/annual.

USAGE
-----
    python dsr_report.py                      # auto: selection book (PSR-only, N=1)
    python dsr_report.py --source selection   # explicit selection book, N=1
    python dsr_report.py --source overlay     # options-overlay grid -> full DSR (N=88)
    python dsr_report.py --demo               # self-contained synthetic demonstration

Reads (read-only) from canslim/research/*.csv. Writes nothing unless --out is given.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
RESEARCH = os.path.join(HERE, "research")

# --------------------------------------------------------------------------- #
# Cross-package import of the DSR module.                                       #
#                                                                               #
# The Deflated-Sharpe implementation lives in the BACKTESTER package            #
# (backtester/src/deflated_sharpe.py), a sibling of the canslim/ folder. We do  #
# NOT restructure packages or copy the code; we add the backtester ROOT to      #
# sys.path so `from src import deflated_sharpe` resolves the same way the        #
# backtester's own tests import it (`from src import deflated_sharpe`). The path #
# is derived relative to THIS file (../backtester), so it is machine-agnostic.   #
# --------------------------------------------------------------------------- #
_BACKTESTER_ROOT = os.path.abspath(os.path.join(HERE, os.pardir, "backtester"))
if _BACKTESTER_ROOT not in sys.path:
    sys.path.insert(0, _BACKTESTER_ROOT)
from src import deflated_sharpe as ds  # noqa: E402  (import after sys.path insert)


# --------------------------------------------------------------------------- #
# Return-series loaders (read-only; parse what the backtests already wrote).    #
# --------------------------------------------------------------------------- #
def load_selection_trade_returns(
    path: str | None = None,
) -> np.ndarray:
    """Per-trade realized returns of the machine+timing selection book.

    Parses the `machine_trade` / `trade` rows of `selection_backtest_results.csv`
    (the file `selection_backtest.py` writes). Each row's `exit_ret` is one
    per-observation return. Returns a 1-D float array. Raises if the file or the
    trade rows are absent (so a missing/empty backtest is loud, never silently 0).
    """
    if path is None:
        path = os.path.join(RESEARCH, "selection_backtest_results.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"selection results not found: {path}\n"
            "Run canslim/selection_backtest.py first to produce it."
        )
    rets: list[float] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            # trade rows look like: ['trade', SYM, entry_date, entry_px, pivot,
            #                        pattern, exit_date, exit_ret]
            if row and row[0] == "trade" and len(row) >= 8:
                try:
                    rets.append(float(row[7]))
                except ValueError:
                    continue
    if len(rets) < 2:
        raise ValueError(
            f"found {len(rets)} trade returns in {path} — need >= 2. "
            "The backtest may not have produced populated results yet "
            "(e.g. price warehouse still too sparse)."
        )
    return np.asarray(rets, dtype=float)


def load_overlay_grid(path: str | None = None) -> list[dict]:
    """Load the options-overlay grid cells (the genuine N>1 search).

    Parses `options_overlay_real_results.csv`. Returns a list of dicts, one per
    grid cell, each with at least {'cell', 'total_ret', 'win_rate', 'n_priced'}.
    The `stock`/headline row is skipped (it is the benchmark book, not a search
    trial). Raises if fewer than 2 cells exist (no search => nothing to deflate).
    """
    if path is None:
        path = os.path.join(RESEARCH, "options_overlay_real_results.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"overlay grid not found: {path}\n"
            "Run canslim/run_options_overlay_real.py first to produce it."
        )
    cells: list[dict] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("section") != "grid":
                continue
            try:
                cells.append(
                    dict(
                        cell=row["cell"],
                        total_ret=float(row["total_ret"]),
                        win_rate=float(row["win_rate"]),
                        n_priced=int(float(row["n_priced"])) if row.get("n_priced") else 0,
                    )
                )
            except (KeyError, ValueError):
                continue
    if len(cells) < 2:
        raise ValueError(
            f"found {len(cells)} overlay grid cells in {path} — need >= 2 for a "
            "multiple-comparisons deflation."
        )
    return cells


# --------------------------------------------------------------------------- #
# Reporting.                                                                    #
# --------------------------------------------------------------------------- #
def report_single_config(
    returns: np.ndarray,
    label: str,
    lines: list[str] | None = None,
) -> dict:
    """Report the honest N=1 case: PSR-vs-zero, no multiple-comparisons deflation.

    Computes the per-observation Sharpe, skew, full kurtosis and T from `returns`,
    then PSR = P(true SR > 0) adjusted for T and non-normal moments. DSR is also
    called with n_trials=1 to SHOW it collapses to PSR (sr0 == 0), making the
    "no deflation applies" point concrete rather than asserted.

    Appends human-readable lines to `lines` (created if None) and returns a dict of
    the numbers. This is a MEASUREMENT — it prints a verdict, it changes nothing.
    """
    L = lines if lines is not None else []
    m = ds.sharpe_and_moments(returns)
    psr = ds.probabilistic_sharpe_ratio(
        observed_sharpe=m["sharpe"], T=m["T"], skew=m["skew"],
        kurtosis=m["kurtosis"], benchmark_sharpe=0.0,
    )
    # Show DSR degenerating to PSR when N=1 (sr0 must be 0.0 => dsr == psr).
    dsr1 = ds.deflated_sharpe_ratio(
        observed_sharpe=m["sharpe"], T=m["T"], n_trials=1, var_trials=0.0,
        skew=m["skew"], kurtosis=m["kurtosis"],
    )
    L.append(f"=== {label} ===")
    L.append("SINGLE FROZEN CONFIG -> PSR-only, no multiple-comparisons deflation applies.")
    L.append(f"  T (observations / trades)      : {m['T']}")
    L.append(f"  per-observation Sharpe          : {m['sharpe']:+.4f}")
    L.append(f"  skew                            : {m['skew']:+.4f}")
    L.append(f"  kurtosis (full, Gaussian=3)     : {m['kurtosis']:.4f}")
    L.append(f"  PSR  P(true SR > 0)             : {psr:.4f}")
    L.append(f"  DSR at N=1 (sr0={dsr1.sr0:.4f})       : {dsr1.dsr:.4f}   "
             f"(equals PSR by construction — no search to deflate)")
    verdict = (
        "significant vs zero at 95% (PSR > 0.95)" if psr > 0.95 else
        "NOT significant vs zero at 95% (PSR <= 0.95) — consistent with a Sharpe "
        "that could be luck given T and the return distribution"
    )
    L.append(f"  VERDICT: {verdict}.")
    L.append("")
    return dict(mode="psr_only", label=label, T=m["T"], sharpe=m["sharpe"],
               skew=m["skew"], kurtosis=m["kurtosis"], psr=psr, dsr=dsr1.dsr, sr0=dsr1.sr0)


def report_multi_trial(
    best_returns: np.ndarray,
    trial_sharpes: np.ndarray,
    label: str,
    lines: list[str] | None = None,
) -> dict:
    """Report the genuine N>1 case: full DSR deflating the BEST trial.

    Parameters
    ----------
    best_returns   : the per-observation return series of the SELECTED (best) trial
                     — used for its Sharpe, skew, kurtosis, T.
    trial_sharpes  : the per-observation Sharpe of EVERY trial in the search
                     (including the best). N = len(trial_sharpes); var_trials =
                     population variance of these Sharpes — the dispersion the
                     E[max] haircut needs. This is the honest, data-driven N and
                     var_trials, not an invented one.

    Appends lines and returns the numbers.
    """
    L = lines if lines is not None else []
    m = ds.sharpe_and_moments(best_returns)
    N = int(len(trial_sharpes))
    var_trials = float(np.var(np.asarray(trial_sharpes, dtype=float), ddof=0))
    res = ds.deflated_sharpe_ratio(
        observed_sharpe=m["sharpe"], T=m["T"], n_trials=N, var_trials=var_trials,
        skew=m["skew"], kurtosis=m["kurtosis"],
    )
    L.append(f"=== {label} ===")
    L.append(f"MULTIPLE-COMPARISONS SEARCH -> full DSR (N={N} trials, deflating the best).")
    L.append(f"  T (observations behind best)    : {m['T']}")
    L.append(f"  best per-observation Sharpe     : {m['sharpe']:+.4f}")
    L.append(f"  skew / kurtosis                 : {m['skew']:+.4f} / {m['kurtosis']:.4f}")
    L.append(f"  N trials                        : {N}")
    L.append(f"  var(trial Sharpes)              : {var_trials:.6f}")
    L.append(f"  E[max SR] haircut (sr0)         : {res.sr0:+.4f}")
    L.append(f"  DSR  P(true SR > E[max SR])     : {res.dsr:.4f}")
    verdict = (
        "SURVIVES the multiple-comparisons + non-normality haircut (DSR > 0.95)"
        if res.dsr > 0.95 else
        "does NOT survive the haircut (DSR <= 0.95) — the best cell's Sharpe is "
        "consistent with selection luck across the grid"
    )
    L.append(f"  VERDICT: {verdict}.")
    L.append("")
    return dict(mode="dsr", label=label, T=m["T"], sharpe=m["sharpe"], n_trials=N,
                var_trials=var_trials, sr0=res.sr0, dsr=res.dsr)


# --------------------------------------------------------------------------- #
# Source runners.                                                              #
# --------------------------------------------------------------------------- #
def run_selection(lines: list[str]) -> dict:
    """Selection backtest -> PSR-only (N=1, frozen spec)."""
    rets = load_selection_trade_returns()
    return report_single_config(
        rets, "CAN SLIM selection backtest (machine+timing, per-trade returns)", lines
    )


def run_overlay(lines: list[str]) -> dict:
    """Options-overlay grid -> full DSR, computed honestly from what IS on disk.

    The overlay CSV stores each cell's TOTAL return and priced-trade count, NOT the
    raw per-trade series. So we CANNOT recover a real per-observation Sharpe with a
    trustworthy denominator — fabricating one produces nonsense (a constant series'
    "Sharpe" explodes). We therefore stay in a SINGLE consistent unit system and do
    only the exact, defensible thing the DSR needs:

      * N (number of grid cells) is EXACT.
      * A comparable per-cell statistic is the mean per-trade return,
            mbar_i = total_ret_i / n_priced_i,
        which we standardise by the cross-cell dispersion to get unit-free "trial
        scores" s_i = mbar_i / std(mbar). These play the ROLE of the trial Sharpes:
        their variance is the search's real degree of freedom (var_trials), and the
        BEST cell's own score is the observed statistic to deflate.

    We pass those directly to expected_max_sharpe / probabilistic_sharpe_ratio in
    the SAME standardised units — no fabricated return series, no exploding Sharpe.
    T is the best cell's priced-trade count. This gives an INDICATIVE DSR for the
    overlay grid; it becomes exact the day the pipeline dumps per-cell trade series
    (then route each cell through report_multi_trial with its real returns).
    """
    cells = load_overlay_grid()
    lines.append("=== options-overlay grid (N>1 search over the OVERLAY) ===")
    lines.append(
        "NOTE: the overlay CSV stores per-cell TOTAL return + trade count, not raw "
        "per-trade series. Per-cell 'scores' are standardised mean per-trade returns "
        "(unit-free); N and the cross-cell spread are EXACT, the resulting DSR is "
        "INDICATIVE. Dump per-cell trade series to make it exact. Selection is fixed "
        "to his picks here — this deflates the OVERLAY grid, not stock selection."
    )
    lines.append("")

    means = np.array([c["total_ret"] / max(c["n_priced"], 1) for c in cells], dtype=float)
    sd = float(np.std(means, ddof=0)) or 1.0
    scores = means / sd                          # unit-free trial scores
    N = int(len(cells))
    var_trials = float(np.var(scores, ddof=0))   # == 1.0 by construction, exact spread
    best_i = int(np.argmax([c["total_ret"] for c in cells]))
    best = cells[best_i]
    observed = float(scores[best_i])             # best cell's standardised score
    T = max(int(best["n_priced"]), 2)

    sr0 = ds.expected_max_sharpe(N, var_trials)
    # PSR of the best score deflated at sr0 (no skew/kurt available for the standardised
    # score; use Gaussian moments — stated as a limit of the indicative overlay number).
    dsr = ds.probabilistic_sharpe_ratio(
        observed_sharpe=observed, T=T, skew=0.0, kurtosis=3.0, benchmark_sharpe=sr0
    )
    lines.append(
        f"=== options-overlay grid — best cell '{best['cell']}' "
        f"(total_ret {best['total_ret']:+.1%}, {best['n_priced']} priced) ==="
    )
    lines.append(f"MULTIPLE-COMPARISONS SEARCH -> full DSR (N={N} cells, deflating the best).")
    lines.append(f"  N grid cells                    : {N}")
    lines.append(f"  best standardised score         : {observed:+.4f}")
    lines.append(f"  var(trial scores)               : {var_trials:.4f}")
    lines.append(f"  T (best cell priced trades)     : {T}")
    lines.append(f"  E[max score] haircut (sr0)      : {sr0:+.4f}")
    lines.append(f"  DSR (indicative)                : {dsr:.4f}")
    verdict = (
        "the best cell's standardised score clears the E[max] haircut (indicative)"
        if dsr > 0.95 else
        "the best cell does NOT clear the multiple-comparisons haircut (indicative) "
        "— its edge is consistent with grid-search luck"
    )
    lines.append(f"  VERDICT: {verdict}.")
    lines.append("")
    return dict(mode="dsr_overlay_indicative", label=best["cell"], n_trials=N,
                var_trials=var_trials, observed=observed, T=T, sr0=sr0, dsr=dsr)


def run_demo(lines: list[str]) -> dict:
    """Self-contained synthetic demonstration of BOTH paths (no files needed).

    Proves the tool end-to-end when real CAN SLIM results are not runnable yet
    (e.g. the full-universe price warehouse is still too sparse). Uses a fixed
    seed so the output is reproducible.
    """
    rng = np.random.default_rng(20260703)
    # (1) N=1 PSR-only path: a modestly positive single-config book (mild positive
    #     skew, fat tails) — like the CAN SLIM per-trade return distribution.
    single = rng.normal(0.012, 0.09, size=230) + rng.standard_t(5, size=230) * 0.01
    lines.append("SYNTHETIC DEMONSTRATION (no CAN SLIM files required)")
    lines.append("Ready to run on real results once the backtest CSVs are populated.")
    lines.append("")
    r1 = report_single_config(single, "DEMO: single frozen config (N=1)", lines)

    # (2) N>1 full-DSR path: simulate a search of N=88 trials (like the overlay
    #     grid), take the best, and deflate it. Most trials are true-zero-edge;
    #     the "best" wins partly by luck — DSR should visibly haircut it.
    N = 88
    T = 250
    trials = rng.normal(0.0, 0.06, size=(N, T))     # true edge ~ 0 for all
    trial_sharpes = trials.mean(axis=1) / trials.std(axis=1, ddof=0)
    best_i = int(np.argmax(trial_sharpes))
    r2 = report_multi_trial(
        trials[best_i], trial_sharpes,
        f"DEMO: best of N={N} zero-edge trials (should be haircut hard by DSR)",
        lines,
    )
    return dict(single=r1, multi=r2)


# --------------------------------------------------------------------------- #
# CLI.                                                                         #
# --------------------------------------------------------------------------- #
def build_report(source: str) -> tuple[list[str], dict]:
    """Return (lines, result-dict) for the chosen source. No side effects on disk."""
    lines: list[str] = []
    lines.append("CAN SLIM — Deflated / Probabilistic Sharpe report (ADDITIVE, measurement-only)")
    lines.append("=" * 78)
    lines.append("")
    if source == "demo":
        res = run_demo(lines)
    elif source == "overlay":
        res = run_overlay(lines)
    else:  # "selection" (default)
        res = run_selection(lines)
    return lines, res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CAN SLIM Deflated/Probabilistic Sharpe report.")
    ap.add_argument("--source", choices=["selection", "overlay"], default="selection",
                    help="which CAN SLIM output to score (default: selection, N=1 PSR-only).")
    ap.add_argument("--demo", action="store_true",
                    help="run the self-contained synthetic demonstration instead of real files.")
    ap.add_argument("--out", default=None,
                    help="optional path to also write the text report to.")
    args = ap.parse_args(argv)

    source = "demo" if args.demo else args.source
    try:
        lines, _ = build_report(source)
    except (FileNotFoundError, ValueError) as e:
        # Real results not runnable yet (sparse warehouse) — fail LOUD with guidance,
        # and point at --demo which always works.
        print(f"[dsr-report] cannot score '{source}': {e}", file=sys.stderr)
        print("[dsr-report] the tool is ready; run `python dsr_report.py --demo` to "
              "see it on a synthetic series, and re-run on real data once the CAN "
              "SLIM backtest produces populated results.", file=sys.stderr)
        return 1

    text = "\n".join(lines)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"[dsr-report] wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
