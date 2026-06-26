"""
Integration tests — portfolio assembly (SPEC.md §11, §12).

Exercises build_target_weights with synthetic engine outputs: weights sum to 1,
the equity target is honored, T-bill floors apply, duration caps are respected,
long Treasuries are excluded when their cap is zero, the real-asset slot is
capped, and the whipsaw rank bonus keeps an incumbent over a marginal challenger.
The full end-to-end run is exercised by run.py; this keeps the assembly logic
isolated per CLAUDE.md rule 5.
"""

import pandas as pd
import pytest

from strategies import config
from strategies.parts import portfolio


def _caps(regime="Defensive", long=None):
    """A duration_decision dict using the config cap table, optional long override."""
    caps = {b: config.DURATION_CAPS[b][regime] for b in config.DURATION_CAPS}
    if long is not None:
        caps["long"] = long
    return {"caps": caps, "long_allowed": long != (0.0, 0.0), "reasons": []}


def _ranking(**scores):
    return pd.Series(scores, dtype=float)


DEF_RANK = _ranking(BIL=70, SGOV=68, USFR=60, SHY=55, VGSH=52, IEF=50, TLT=45, TFLO=40)


def test_weights_sum_to_one():
    out = portfolio.build_target_weights(
        regime="Caution", equity_target=0.5,
        equity_sleeve=pd.Series({"SPY": 1.0}),
        duration_decision=_caps("Caution"),
        defensive_ranking=DEF_RANK, version="Balanced",
    )
    assert out["weights"].sum() == pytest.approx(1.0)


def test_equity_target_honored():
    out = portfolio.build_target_weights(
        regime="RiskOn", equity_target=0.8,
        equity_sleeve=pd.Series({"SPY": 1.0}),
        duration_decision=_caps("RiskOn"),
        defensive_ranking=DEF_RANK, version="Balanced",
    )
    assert out["weights"]["SPY"] == pytest.approx(0.8, abs=1e-6)


def test_tbill_floor_applied():
    # Conservative has a 10% T-bill floor; a defensive regime keeps cash present.
    out = portfolio.build_target_weights(
        regime="Defensive", equity_target=0.2,
        equity_sleeve=pd.Series({"SPY": 1.0}),
        duration_decision=_caps("Defensive"),
        defensive_ranking=DEF_RANK, version="Conservative",
    )
    cash = sum(out["weights"].get(t, 0.0) for t in config.TBILLS + config.FLOATING_RATE)
    assert cash >= config.CLIENT_VERSIONS["Conservative"]["tbill_floor"] - 1e-9


def test_duration_caps_respected():
    out = portfolio.build_target_weights(
        regime="Defensive", equity_target=0.1,
        equity_sleeve=pd.Series({"SPY": 1.0}),
        duration_decision=_caps("Defensive"),
        defensive_ranking=DEF_RANK, version="Growth",
    )
    w = out["weights"]
    assert w.get("TLT", 0.0) <= config.DURATION_CAPS["long"]["Defensive"][1] + 1e-9
    assert w.get("IEF", 0.0) <= config.DURATION_CAPS["intermediate"]["Defensive"][1] + 1e-9


def test_long_excluded_when_cap_zero():
    out = portfolio.build_target_weights(
        regime="Defensive", equity_target=0.1,
        equity_sleeve=pd.Series({"SPY": 1.0}),
        duration_decision=_caps("Defensive", long=(0.0, 0.0)),
        defensive_ranking=_ranking(TLT=99, BIL=50),  # TLT ranks top but is banned
        version="Growth",
    )
    assert out["weights"].get("TLT", 0.0) == pytest.approx(0.0)


def test_real_asset_basket_sized_by_version_and_split_by_weight():
    # A two-leg inverse-vol basket: the sleeve equals the version target, split
    # across the legs by their weights (60% gold / 40% commodities here).
    basket = {"legs": [
        {"ticker": "IAU", "category": "gold", "cap": config.CAP_MAX_GOLD, "weight": 0.6},
        {"ticker": "PDBC", "category": "commodities", "cap": config.CAP_MAX_COMMODITIES, "weight": 0.4},
    ]}
    for version in ("Conservative", "Balanced", "Growth"):
        out = portfolio.build_target_weights(
            regime="Defensive", equity_target=0.0,
            equity_sleeve=pd.Series({"SPY": 1.0}),
            duration_decision=_caps("Defensive"),
            defensive_ranking=DEF_RANK, real_basket=basket, version=version,
        )
        target = config.REAL_ASSET_SLEEVE_TARGET[version]
        gold_w, com_w = out["weights"].get("IAU", 0.0), out["weights"].get("PDBC", 0.0)
        assert gold_w + com_w == pytest.approx(target, abs=1e-6)   # whole sleeve = target
        assert gold_w == pytest.approx(target * 0.6, abs=1e-6)     # split by leg weight
        assert com_w == pytest.approx(target * 0.4, abs=1e-6)
        assert out["real_asset"] == "IAU+PDBC"
    assert config.REAL_ASSET_SLEEVE_TARGET["Growth"] > config.REAL_ASSET_SLEEVE_TARGET["Conservative"]


def test_real_asset_sleeve_scales_with_macro_regime():
    # The dynamic cap scales the sleeve target by the detected macro regime.
    basket = {"legs": [
        {"ticker": "IAU", "category": "gold", "cap": config.CAP_MAX_GOLD, "weight": 0.5},
        {"ticker": "PDBC", "category": "commodities", "cap": config.CAP_MAX_COMMODITIES, "weight": 0.5},
    ]}

    def sleeve(macro):
        dd = _caps("Defensive")
        dd["macro_regime"] = macro
        out = portfolio.build_target_weights(
            regime="Defensive", equity_target=0.0, equity_sleeve=pd.Series({"SPY": 1.0}),
            duration_decision=dd, defensive_ranking=DEF_RANK, real_basket=basket, version="Balanced",
        )
        return out["weights"].get("IAU", 0.0) + out["weights"].get("PDBC", 0.0)

    base = config.REAL_ASSET_SLEEVE_TARGET["Balanced"]
    cap = config.REAL_ASSET_SLEEVE_MAX
    legs = [(0.5, config.CAP_MAX_GOLD), (0.5, config.CAP_MAX_COMMODITIES)]  # 50/50 split

    def expect(macro):  # scaled target, clamped to the ceiling AND per-leg §12 caps
        tgt = min(base * config.REAL_ASSET_REGIME_SCALE[macro], cap)
        return sum(min(tgt * w, leg_cap) for w, leg_cap in legs)

    assert sleeve("neutral") == pytest.approx(expect("neutral"), abs=1e-6)
    assert sleeve("deflation") == pytest.approx(expect("deflation"), abs=1e-6)
    assert sleeve("inflation") == pytest.approx(expect("inflation"), abs=1e-6)
    assert sleeve("stagflation") == pytest.approx(expect("stagflation"), abs=1e-6)
    # Lean order: stagflation > inflation > neutral > deflation.
    assert sleeve("stagflation") > sleeve("inflation") > sleeve("neutral") > sleeve("deflation")
    # And never above the hard ceiling.
    assert sleeve("stagflation") <= config.REAL_ASSET_SLEEVE_MAX + 1e-9


def test_whipsaw_keeps_incumbent():
    # RiskOn has a zero T-bill floor, so the small defense budget actually
    # contests the duration slot (long cap 0-10%, intermediate cap 0-10%).
    common = dict(
        regime="RiskOn", equity_target=0.9,
        equity_sleeve=pd.Series({"SPY": 1.0}),
        duration_decision=_caps("RiskOn"),
        defensive_ranking=_ranking(TLT=60, IEF=55, BIL=40), version="Growth",
    )
    fresh = portfolio.build_target_weights(**common)
    incumbent = portfolio.build_target_weights(**common, prev_weights=pd.Series({"IEF": 0.1}))
    # Fresh: TLT (higher raw score) takes the duration slot.
    assert fresh["weights"].get("TLT", 0) > fresh["weights"].get("IEF", 0)
    # Incumbent IEF gets the +10 bonus and is kept over the marginal TLT.
    assert incumbent["weights"].get("IEF", 0) > incumbent["weights"].get("TLT", 0)
