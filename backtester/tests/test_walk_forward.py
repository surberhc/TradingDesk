"""
Tests for src/walk_forward.py — the multi-window rolling/anchored walk-forward harness.

Covers:
  * window math: spans/lengths, full coverage, no overlap of IS into a prior OOS.
  * no-look-ahead within a window (OOS strictly after IS) and between windows (a window's
    data is never earlier than a prior window's OOS) — including that run_fn is only ever
    handed data up to its own OOS end.
  * a synthetic regime break where IS looks great but OOS is bad — the stitched OOS must
    reflect the bad OOS, not the good IS.
  * thin-sample warning fires on a short series (and per-window OOS lengths are surfaced).
  * rolling vs anchored behave as specified (anchored IS always starts at history start;
    rolling IS slides; OOS identical between the two modes).
"""

import warnings

import numpy as np
import pandas as pd
import pytest

from src import walk_forward as wf


def _nav_from_returns(rets, start="2015-01-01"):
    idx = pd.bdate_range(start, periods=len(rets))
    return (1.0 + pd.Series(rets, index=idx)).cumprod()


def _steady_navs(n, drift=0.0004, vol=0.006, seed=0, start="2015-01-01"):
    rng = np.random.default_rng(seed)
    return _nav_from_returns(rng.normal(drift, vol, n), start=start)


# --------------------------------------------------------------------------- window math


def test_window_bounds_tile_with_no_gaps_or_overlap():
    n, n_windows, is_frac = 1000, 5, 0.70
    bounds = wf._window_bounds(n, n_windows, is_frac)
    assert len(bounds) == n_windows
    # Contiguous IS->OOS inside each window, and windows tile [0, n) end to end.
    assert bounds[0][0] == 0
    assert bounds[-1][3] == n
    for is_lo, is_hi, oos_lo, oos_hi in bounds:
        assert is_lo < is_hi == oos_lo < oos_hi  # IS then OOS, contiguous, non-empty
    # No gap / no overlap between successive windows.
    for prev, cur in zip(bounds, bounds[1:]):
        assert prev[3] == cur[0]


def test_is_frac_controls_split_ratio():
    bounds = wf._window_bounds(1000, 1, 0.70)
    is_lo, is_hi, oos_lo, oos_hi = bounds[0]
    is_len, oos_len = is_hi - is_lo, oos_hi - oos_lo
    assert is_len / (is_len + oos_len) == pytest.approx(0.70, abs=0.02)


def test_per_window_spans_and_lengths_reported():
    navs = _steady_navs(600)
    res = wf.rolling_walk_forward(navs, n_windows=4, is_frac=0.75)
    assert len(res.windows) == 4
    for w in res.windows:
        assert w.is_len > 0 and w.oos_len > 0
        assert w.is_end < w.oos_start  # OOS strictly after IS (timestamps)
        assert w.oos_len == len(w.oos_navs)


def test_rejects_bad_params():
    navs = _steady_navs(200)
    with pytest.raises(ValueError):
        wf.rolling_walk_forward(navs, n_windows=0)
    with pytest.raises(ValueError):
        wf.rolling_walk_forward(navs, is_frac=1.0)
    with pytest.raises(ValueError):
        wf.rolling_walk_forward(navs, mode="sideways")
    with pytest.raises(ValueError):
        wf._window_bounds(3, 5, 0.7)  # history shorter than n_windows


# ------------------------------------------------------------------- no-look-ahead guards


def test_run_fn_never_sees_future_data():
    """Each window's run_fn call may only receive data up to its own OOS end."""
    navs = _steady_navs(800)
    seen_max_dates = []

    def spy_run_fn(is_navs, oos_navs, w):
        # IS must be strictly before OOS; the latest date handed in is the OOS end.
        assert is_navs.index[-1] < oos_navs.index[0]
        seen_max_dates.append(oos_navs.index[-1])
        return wf._rebase(oos_navs)

    res = wf.rolling_walk_forward(navs, run_fn=spy_run_fn, n_windows=5)
    # Each successive window's max date advances (windows laid out forward in time).
    assert seen_max_dates == sorted(seen_max_dates)
    # And no window ever saw beyond the full history end.
    assert max(seen_max_dates) <= navs.index[-1]


def test_between_window_no_leak_oos_tails_are_time_ordered():
    navs = _steady_navs(900)
    res = wf.rolling_walk_forward(navs, n_windows=6)
    # A later window's OOS starts after an earlier window's OOS ends: no earlier score can
    # contain later data.
    for prev, cur in zip(res.windows, res.windows[1:]):
        assert prev.oos_end <= cur.oos_start


# ----------------------------------------------------------- regime break: OOS reflects it


def test_stitched_oos_reflects_bad_oos_not_good_is():
    """
    Construct each window so the in-sample rips up and the out-of-sample bleeds down.
    With is_frac inside every window, the IS is strongly positive and the OOS strongly
    negative; the STITCHED OOS series (tails only) must be a clear loser even though a
    naive full-series score would look fine-ish.
    """
    n_windows, is_frac = 5, 0.70
    per_window = 200
    n = n_windows * per_window
    idx = pd.bdate_range("2010-01-01", periods=n)

    rets = np.empty(n)
    # Recreate the harness's integer window edges so we paint IS-up / OOS-down per window.
    edges = np.linspace(0, n, n_windows + 1).round().astype(int)
    for w in range(n_windows):
        lo, hi = edges[w], edges[w + 1]
        width = hi - lo
        is_hi = lo + max(1, min(width - 1, int(round(width * is_frac))))
        rets[lo:is_hi] = 0.004     # in-sample: strong up
        rets[is_hi:hi] = -0.004    # out-of-sample: strong down
    navs = (1.0 + pd.Series(rets, index=idx)).cumprod()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = wf.rolling_walk_forward(navs, n_windows=n_windows, is_frac=is_frac)

    # Every window's OOS tail is a drawdown, so the stitched OOS is a decisive loser.
    assert res.stitched_oos_metrics.loc["CAGR", "strategy"] < 0
    assert res.stitched_oos_max_drawdown < -0.10
    assert res.stitched_oos_sharpe < 0
    for w in res.windows:
        assert w.oos_metrics.loc["CAGR", "strategy"] < 0


# ------------------------------------------------------------------- thin-sample guard


def test_thin_sample_warning_fires_on_short_series():
    # ~120 business days over 5 windows -> ~24/window, OOS tail ~7 days -> well under THIN_N.
    navs = _steady_navs(120)
    with pytest.warns(UserWarning, match="THIN_N"):
        res = wf.rolling_walk_forward(navs, n_windows=5, is_frac=0.70)
    assert any(w.oos_thin for w in res.windows)
    assert any("THIN_N" in msg for msg in res.warnings)
    # Per-window OOS lengths are surfaced (the loud-not-silent requirement).
    assert all(isinstance(w.oos_len, int) for w in res.windows)


def test_no_thin_warning_on_ample_series():
    navs = _steady_navs(3000)  # plenty of data per window
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning would fail the test
        res = wf.rolling_walk_forward(navs, n_windows=5, is_frac=0.70)
    assert not res.stitched_oos_thin
    assert all(not w.oos_thin for w in res.windows)


def test_thin_n_matches_s6_convention():
    assert wf.THIN_N == 30


# ------------------------------------------------------------------- rolling vs anchored


def test_anchored_is_starts_at_history_start_rolling_slides():
    navs = _steady_navs(1000)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        rolling = wf.rolling_walk_forward(navs, n_windows=5, mode="rolling")
        anchored = wf.rolling_walk_forward(navs, n_windows=5, mode="anchored")

    start = navs.index[0]
    # Anchored: every window's IS begins at the very start of history.
    for w in anchored.windows:
        assert w.is_start == start
    # Anchored IS grows monotonically (each window's IS is a superset window in length).
    for prev, cur in zip(anchored.windows, anchored.windows[1:]):
        assert cur.is_len > prev.is_len

    # Rolling: later windows' IS starts move forward (do NOT all sit at history start).
    assert rolling.windows[-1].is_start > start
    for prev, cur in zip(rolling.windows, rolling.windows[1:]):
        assert cur.is_start >= prev.is_start
    assert rolling.windows[-1].is_start > rolling.windows[0].is_start


def test_oos_tails_identical_between_modes():
    navs = _steady_navs(1000)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        rolling = wf.rolling_walk_forward(navs, n_windows=5, mode="rolling")
        anchored = wf.rolling_walk_forward(navs, n_windows=5, mode="anchored")
    # OOS tails depend only on the window edges, not on the IS start, so they must match.
    for rw, aw in zip(rolling.windows, anchored.windows):
        assert rw.oos_start == aw.oos_start
        assert rw.oos_end == aw.oos_end
        assert rw.oos_len == aw.oos_len
    pd.testing.assert_frame_equal(
        rolling.stitched_oos_navs, anchored.stitched_oos_navs
    )


# ------------------------------------------------------------------- stitching + inputs


def test_stitched_series_is_continuous_and_covers_all_oos_tails():
    navs = _steady_navs(1000)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = wf.rolling_walk_forward(navs, n_windows=5)
    total_oos = sum(w.oos_len for w in res.windows)
    assert res.stitched_oos_len == total_oos
    # Stitched index is sorted and unique.
    assert res.stitched_oos_navs.index.is_monotonic_increasing
    assert res.stitched_oos_navs.index.is_unique


def test_accepts_returns_series_and_dataframe_inputs():
    rets = _steady_navs(600).pct_change().dropna()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from_rets = wf.rolling_walk_forward(rets, n_windows=4)
    assert from_rets.stitched_oos_len > 0

    idx = pd.bdate_range("2015-01-01", periods=600)
    rng = np.random.default_rng(2)
    df = pd.DataFrame(
        {
            "strategy": (1 + pd.Series(rng.normal(0.0003, 0.006, 600), index=idx)).cumprod(),
            "SPY": (1 + pd.Series(rng.normal(0.0004, 0.010, 600), index=idx)).cumprod(),
        }
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = wf.rolling_walk_forward(df, n_windows=4)
    # Benchmark column carried through so OOS metrics stay comparable.
    assert "SPY" in res.stitched_oos_metrics.columns


def test_dataframe_without_strategy_column_rejected():
    idx = pd.bdate_range("2015-01-01", periods=100)
    df = pd.DataFrame({"SPY": np.ones(100)}, index=idx)
    with pytest.raises(ValueError, match="strategy"):
        wf.rolling_walk_forward(df, n_windows=3)


def test_format_report_runs():
    navs = _steady_navs(1000)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = wf.rolling_walk_forward(navs, n_windows=5)
    text = wf.format_report(res)
    assert "STITCHED out-of-sample" in text
    assert "W0:" in text
