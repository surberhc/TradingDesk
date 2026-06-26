"""
Unit tests for the Duration / Inflation-Deflation Engine (SPEC.md §6).

Covers: long-Treasury permission earned in a clean bond bull; long banned on a
broken trend; the inflationary-bear filter (2022 guard) banning long and capping
intermediate low; the per-regime cap table; and the no-look-ahead property.
"""

import numpy as np
import pandas as pd
import pytest

from strategies import config
from strategies.parts import duration


# ---------------------------------------------------------------------------
# Synthetic frame helpers
# ---------------------------------------------------------------------------
def _idx(n):
    return pd.bdate_range("2012-01-02", periods=n)


def _line(n, start, step):
    return start + np.arange(n) * step


def _frame(n, **series) -> pd.DataFrame:
    """Build a prices frame; any role not supplied defaults to a flat line."""
    idx = _idx(n)
    base = {
        "TLT": _line(n, 100, 0.0), "IEF": _line(n, 100, 0.0),
        "BIL": _line(n, 100, 0.0), "SPY": _line(n, 100, 0.0),
        "IAU": _line(n, 100, 0.0), "DBC": _line(n, 100, 0.0),
        "USFR": _line(n, 100, 0.0),
    }
    base.update(series)
    return pd.DataFrame({k: v for k, v in base.items()}, index=idx)


# ---------------------------------------------------------------------------
# Permission
# ---------------------------------------------------------------------------
def test_permission_earned_in_bond_bull():
    n = 400
    # TLT steadily rising (above MAs, +3m, small drawdown, beats flat T-bills);
    # yield steadily falling; stocks flat. Should pass all 5 permission rules.
    df = _frame(
        n,
        TLT=_line(n, 100, 0.10),
        BIL=_line(n, 100, 0.001),
    )
    yld = pd.Series(_line(n, 4.0, -0.003), index=df.index)  # falling yield
    sig = duration.duration_signals(df, yield_10y=yld).iloc[-1]
    assert sig["perm_passes"] == 5
    assert bool(sig["long_allowed"]) is True
    assert bool(sig["long_banned"]) is False


def test_long_banned_on_broken_trend():
    n = 400
    # TLT falling hard -> below both MAs, deep drawdown -> banned regardless of count.
    df = _frame(n, TLT=_line(n, 200, -0.20), BIL=_line(n, 100, 0.001))
    yld = pd.Series(_line(n, 2.0, 0.004), index=df.index)  # rising yield
    sig = duration.duration_signals(df, yield_10y=yld).iloc[-1]
    assert bool(sig["long_banned"]) is True
    assert bool(sig["long_allowed"]) is False


def test_inflationary_bear_bans_long_and_caps_intermediate():
    n = 400
    # 2022-style: stocks down, TLT down, yield up & rising, T-bills/reals outperform.
    df = _frame(
        n,
        SPY=_line(n, 300, -0.20),
        TLT=_line(n, 300, -0.20),
        IEF=_line(n, 200, -0.05),
        BIL=_line(n, 100, 0.02),
        IAU=_line(n, 100, 0.10),
        DBC=_line(n, 100, 0.10),
        USFR=_line(n, 100, 0.02),
    )
    yld = pd.Series(_line(n, 1.0, 0.01), index=df.index)  # rising, above its average
    sig = duration.duration_signals(df, yield_10y=yld).iloc[-1]
    assert bool(sig["inflationary_bear"]) is True

    decision = duration.duration_decision(sig, regime="Defensive")
    assert decision["caps"]["long"] == (0.0, 0.0)
    assert decision["caps"]["intermediate"][1] <= config.INFLATIONARY_INTERMEDIATE_CAP
    assert decision["long_allowed"] is False


# ---------------------------------------------------------------------------
# Cap table
# ---------------------------------------------------------------------------
def test_caps_follow_regime_table_when_long_allowed():
    # A neutral signals row with long allowed: caps should match config per regime.
    row = pd.Series(
        {"long_allowed": True, "inflationary_bear": False,
         "deflationary_panic": False, "long_banned": False, "perm_passes": 5}
    )
    for regime in ("RiskOn", "Caution", "Defensive"):
        caps = duration.duration_decision(row, regime)["caps"]
        assert caps["long"] == config.DURATION_CAPS["long"][regime]
        assert caps["tbill"] == config.DURATION_CAPS["tbill"][regime]


def test_long_zeroed_when_not_allowed():
    row = pd.Series(
        {"long_allowed": False, "inflationary_bear": False,
         "deflationary_panic": False, "long_banned": True, "perm_passes": 2}
    )
    caps = duration.duration_decision(row, "Defensive")["caps"]
    assert caps["long"] == (0.0, 0.0)
    # Other buckets still follow the regime table.
    assert caps["short"] == config.DURATION_CAPS["short"]["Defensive"]


def test_unknown_regime_is_defensive_default():
    row = pd.Series({"long_allowed": True, "inflationary_bear": False,
                     "deflationary_panic": False, "long_banned": False, "perm_passes": 5})
    caps = duration.duration_decision(row, "Undefined")["caps"]
    assert caps["long"] == (0.0, 0.0)
    assert caps["tbill"][1] == 1.0  # cash ballast available


# ---------------------------------------------------------------------------
# No look-ahead (SPEC §3, §16)
# ---------------------------------------------------------------------------
def test_no_lookahead_truncation_matches():
    rng = np.random.default_rng(1)
    n = 400
    df = _frame(
        n,
        TLT=100 + np.cumsum(rng.normal(0, 1, n)),
        SPY=100 + np.cumsum(rng.normal(0, 1, n)),
        BIL=_line(n, 100, 0.001),
    )
    yld = pd.Series(3.0 + np.cumsum(rng.normal(0, 0.02, n)), index=df.index)
    full = duration.duration_signals(df, yield_10y=yld)
    for t in (300, 350, 390):
        cutoff = df.index[t]
        trunc = duration.duration_signals(df.loc[:cutoff], yield_10y=yld.loc[:cutoff])
        assert int(trunc.loc[cutoff, "perm_passes"]) == int(full.loc[cutoff, "perm_passes"])
        assert bool(trunc.loc[cutoff, "long_allowed"]) == bool(full.loc[cutoff, "long_allowed"])
