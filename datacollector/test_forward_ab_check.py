"""test_forward_ab_check.py — offline regression tests for the ThetaData-vs-IBKR
forward-validation A/B check (forward_ab_check.py).

Fully offline / parquet-only: build tiny-but-valid synthetic SPX/SPXW chains into
two scratch temp namespaces (patched onto config.RAW_OPTIONS / RAW_OPTIONS_IBKR),
redirect the log/heartbeat/jobstatus writes to temp, and drive forward_ab_check.run().
The chains carry exactly the columns features.gex.day_features() needs (date, symbol,
expiration, strike, right, gamma, implied_vol, underlying_price, open_interest) so the
frozen GEX math returns a real dict — no dependency on the real warehouse.

Run from datacollector/:
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest test_forward_ab_check.py -q
"""

from __future__ import annotations

import pandas as pd
import pytest

import config
import forward_ab_check as ab


def _chain(sym: str, day: str, *, gamma: float = 0.0012, iv: float = 0.15,
           spot: float = 5000.0, call_oi: float = 1000.0, put_oi: float = 100.0
           ) -> pd.DataFrame:
    """A small valid EOD chain: 9 strikes around spot, C+P, one near expiration.

    call_oi >> put_oi makes net dealer gamma clearly Positive (CALL_SIGN=+1,
    PUT_SIGN=-1 -> net = call_gex - put_gex), so gamma_state is deterministic.
    """
    strikes = [spot + 25 * i for i in range(-4, 5)]   # 4900..5100 step 25
    rows = []
    for k in strikes:
        for right, oi in (("CALL", call_oi), ("PUT", put_oi)):
            rows.append({
                "date": day,
                "symbol": sym,
                "expiration": "2026-02-01",
                "strike": float(k),
                "right": right,
                "gamma": gamma,
                "implied_vol": iv,
                "underlying_price": spot,
                "open_interest": oi,
            })
    return pd.DataFrame(rows)


@pytest.fixture
def ab_env(tmp_path, monkeypatch):
    """Patch the two namespaces + DATA_ROOT to scratch temps and capture jobstatus."""
    theta = tmp_path / "raw" / "options"
    ibkr = tmp_path / "raw" / "options_ibkr"
    theta.mkdir(parents=True)
    ibkr.mkdir(parents=True)
    monkeypatch.setattr(config, "RAW_OPTIONS", theta)
    monkeypatch.setattr(config, "RAW_OPTIONS_IBKR", ibkr)
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)

    captured: list[dict] = []

    def _fake_write(job, status, metrics=None, message="", day=None):
        captured.append({"job": job, "status": status, "metrics": metrics or {},
                         "message": message, "day": day})
        return None

    monkeypatch.setattr(ab.jobstatus, "write", _fake_write)
    return theta, ibkr, captured


def _write(base, df, sym, day):
    d = base / sym
    d.mkdir(parents=True, exist_ok=True)
    df.to_parquet(d / f"{day}.parquet", engine="pyarrow", index=False)


def test_clean_match_verdict_ok(ab_env):
    theta, ibkr, captured = ab_env
    day = "20260115"
    for sym in ("SPX", "SPXW"):
        _write(theta, _chain(sym, day), sym, day)
        _write(ibkr, _chain(sym, day), sym, day)   # identical -> ~0 diff, full gamma

    res = ab.run()
    assert res["day"] == day
    assert res["overall"] == "ok"
    assert res["symbols"] == {"SPX": "ok", "SPXW": "ok"}
    for r in res["results"]:
        assert r["gex"]["gamma_state_match"] is True
        assert r["greeks"]["ibkr_gamma_frac"] == pytest.approx(1.0)
        assert r["gex"]["net_gex_rel_pct"] == pytest.approx(0.0, abs=1e-6)
    # jobstatus recorded ok.
    assert captured and captured[-1]["job"] == "forward_ab_check"
    assert captured[-1]["status"] == "ok"


def test_degraded_ibkr_greeks_verdict_fail(ab_env):
    theta, ibkr, captured = ab_env
    day = "20260115"
    for sym in ("SPX", "SPXW"):
        _write(theta, _chain(sym, day), sym, day)                 # full gamma
        _write(ibkr, _chain(sym, day, gamma=0.0), sym, day)       # delayed: zero gamma

    res = ab.run()
    assert res["day"] == day
    assert res["overall"] == "fail"
    for r in res["results"]:
        assert r["verdict"] == "fail"
        assert r["greeks"]["ibkr_gamma_frac"] == pytest.approx(0.0)
        # the low-gamma-fraction reason must be the (or a) stated cause
        assert any("gamma-present fraction" in why for why in r["reasons"])
    assert captured[-1]["status"] == "fail"


def test_no_common_day_is_skip(ab_env):
    theta, ibkr, captured = ab_env
    # ThetaData has a day; IBKR has nothing -> no common day.
    _write(theta, _chain("SPX", "20260115"), "SPX", "20260115")
    _write(theta, _chain("SPXW", "20260115"), "SPXW", "20260115")

    res = ab.run()
    assert res["day"] is None
    assert res["overall"] == "skip"
    assert res["symbols"] == {}
    assert captured and captured[-1]["status"] == "stale"


def test_forced_day_missing_one_side_skips_symbol(ab_env):
    """A forced day where one symbol is absent on a side degrades that symbol to
    skipped rather than crashing; the present symbol still gets compared."""
    theta, ibkr, captured = ab_env
    day = "20260115"
    _write(theta, _chain("SPX", day), "SPX", day)
    _write(ibkr, _chain("SPX", day), "SPX", day)
    # SPXW present only on ThetaData side.
    _write(theta, _chain("SPXW", day), "SPXW", day)

    res = ab.run(forced_day=day)
    assert res["day"] == day
    assert "SPXW" in res["skipped"]
    assert res["symbols"] == {"SPX": "ok"}
    assert res["overall"] == "ok"
