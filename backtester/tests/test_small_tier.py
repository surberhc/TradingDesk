"""
Unit tests for the whole-share SMALL-ACCOUNT tier (strategies/small_tier.py).

Decided 2026-08-06 for Growth; extended 2026-08-24 to all three client versions.

Covers: label round-tripping for every version; the collapse projects onto exactly two
tickers summing to 1; a risk-off target collapses to all-defensive; each version collapses
to ITS OWN equity split (the versions must stay distinct); and the NAV hysteresis promotes,
demotes, and - the point of it - does NOT churn inside the dead band.
"""

import pandas as pd
import pytest

from strategies import config, small_tier


VERSIONS = list(config.CLIENT_VERSIONS)


def test_all_three_versions_are_covered():
    assert set(VERSIONS) == {"Conservative", "Balanced", "Growth"}


@pytest.mark.parametrize("version", VERSIONS)
def test_label_round_trip(version):
    lbl = small_tier.small_label(version)
    assert lbl == f"{version} (Small)"
    assert small_tier.is_small(lbl)
    assert not small_tier.is_small(version)
    assert small_tier.parent_version(lbl) == version


@pytest.mark.parametrize("version", VERSIONS)
def test_small_label_is_idempotent(version):
    once = small_tier.small_label(version)
    assert small_tier.small_label(once) == once


def test_collapse_projects_onto_two_tickers():
    w = pd.Series({"SPY": 0.50, "RSP": 0.25, "PDBC": 0.15, "USFR": 0.10})
    out = small_tier.collapse(w)
    assert set(out.index) == {config.SMALL_TIER_EQUITY, config.SMALL_TIER_DEFENSIVE}
    assert out.sum() == pytest.approx(1.0)
    # SPY + RSP are equity; PDBC and USFR are not.
    assert out[config.SMALL_TIER_EQUITY] == pytest.approx(0.75)


def test_collapse_counts_sector_holdings_as_equity():
    """The sector-neutral arm must project correctly too, for free."""
    w = pd.Series({s: 1.0 / len(config.SECTORS) for s in config.SECTORS})
    out = small_tier.collapse(w)
    assert out[config.SMALL_TIER_EQUITY] == pytest.approx(1.0)


def test_risk_off_target_collapses_to_all_defensive():
    w = pd.Series({"USFR": 0.6, "BIL": 0.4})
    out = small_tier.collapse(w)
    assert out[config.SMALL_TIER_DEFENSIVE] == pytest.approx(1.0)
    assert out[config.SMALL_TIER_EQUITY] == pytest.approx(0.0)


def test_versions_stay_distinct_after_collapse():
    """Conservative/Balanced/Growth must NOT collapse onto the same proxy."""
    targets = {
        "Conservative": pd.Series({"SPY": 0.427, "RSP": 0.213, "PDBC": 0.20, "USFR": 0.16}),
        "Balanced":     pd.Series({"SPY": 0.533, "RSP": 0.267, "PDBC": 0.15, "USFR": 0.05}),
        "Growth":       pd.Series({"SPY": 0.567, "RSP": 0.283, "PDBC": 0.15}),
    }
    eq = {v: small_tier.collapse(w)[config.SMALL_TIER_EQUITY] for v, w in targets.items()}
    assert eq["Conservative"] < eq["Balanced"] < eq["Growth"]


@pytest.mark.parametrize("version", VERSIONS)
def test_new_account_uses_the_plain_threshold(version):
    assert small_tier.tier_for(10_000, version) == small_tier.small_label(version)
    assert small_tier.tier_for(60_000, version) == version


@pytest.mark.parametrize("version", VERSIONS)
def test_hysteresis_does_not_churn_inside_the_dead_band(version):
    """The whole reason hysteresis exists: inside the band, whatever is held STAYS held."""
    small = small_tier.small_label(version)
    for nav in (23_000, 25_000, 27_000):
        assert small_tier.tier_for(nav, version, current_label=small) == small
        assert small_tier.tier_for(nav, version, current_label=version) == version


@pytest.mark.parametrize("version", VERSIONS)
def test_promotion_and_demotion_fire_outside_the_band(version):
    small = small_tier.small_label(version)
    assert small_tier.tier_for(27_500, version, current_label=small) == version
    assert small_tier.tier_for(22_499, version, current_label=version) == small


def test_whole_share_fit_reports_infeasible_when_a_share_is_unaffordable():
    w = pd.Series({"SPY": 0.85, "USFR": 0.15})
    fit = small_tier.whole_share_fit(w, 500, {"SPY": 765.72, "USFR": 50.49})
    assert fit["shares"]["SPY"] == 0
    assert fit["feasible"] is False


def test_small_proxy_is_feasible_where_the_full_model_is_not():
    """The entire justification for the tier, as a test."""
    prices = {"SPY": 765.72, "RSP": 221.67, "PDBC": 18.65, "USFR": 50.49, "SCHB": 29.61}
    full = pd.Series({"SPY": 0.567, "RSP": 0.283, "PDBC": 0.15})
    small = small_tier.collapse(full)
    assert small_tier.whole_share_fit(full, 1_000, prices)["feasible"] is False
    assert small_tier.whole_share_fit(small, 1_000, prices)["feasible"] is True
