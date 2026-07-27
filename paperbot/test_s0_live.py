"""
test_s0_live.py — offline unit tests for the S0 read-only LIVE-PILOT connection lane.

NO broker, NO real gateway, NO network. Proves the zero-transmit plumbing guarantees for
Slice 1 of the S0 live pilot:
  * S0_LIVE_ACCOUNT is the INDIVIDUAL account U5721712, and is NOT the trust/S8 account
    U14438624.
  * connect_s0_live() connects READ-ONLY (readonly is identity True, never False) under the
    "s0_live_pilot" consumer id, with launch defaulting False and passing through.
  * filter_account_summary / filter_positions pin every read to S0_LIVE_ACCOUNT, so the S0
    lane can never read the trust account's rows.
  * the two new clientIds (s0_live_pilot=57, s0_live_exec=58) are registered, differ from
    the S8 ids, and introduce no collision into the registry.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s0_live.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import s0_live
from connections import clientids


# --- account pin: individual U5721712, never the trust/S8 account ------------------
def test_s0_live_account_is_the_individual_account():
    assert s0_live.S0_LIVE_ACCOUNT == "U5721712"


def test_s0_live_account_is_not_the_trust_s8_account():
    assert s0_live.S0_LIVE_ACCOUNT != "U14438624"


# --- connect_s0_live is READ-ONLY, never readonly=False ----------------------------
def test_connect_s0_live_is_read_only(monkeypatch):
    calls = []
    sentinel = object()

    def spy(consumer, launch=False, readonly=True, timeout=10):
        calls.append({"consumer": consumer, "launch": launch,
                      "readonly": readonly, "timeout": timeout})
        return sentinel

    monkeypatch.setattr(s0_live.ibkr_live_trade, "connect", spy)

    result = s0_live.connect_s0_live()

    assert result is sentinel
    assert len(calls) == 1
    call = calls[0]
    assert call["consumer"] == "s0_live_pilot"
    # Identity True, not merely truthy — the read-only session flag must be the real bool.
    assert call["readonly"] is True
    # readonly=False must NEVER be passed.
    assert call["readonly"] is not False
    # launch defaults False.
    assert call["launch"] is False


def test_connect_s0_live_passes_launch_through(monkeypatch):
    calls = []

    def spy(consumer, launch=False, readonly=True, timeout=10):
        calls.append({"consumer": consumer, "launch": launch,
                      "readonly": readonly, "timeout": timeout})
        return object()

    monkeypatch.setattr(s0_live.ibkr_live_trade, "connect", spy)

    s0_live.connect_s0_live(launch=True)

    assert calls[0]["launch"] is True
    # Still read-only even when launching.
    assert calls[0]["readonly"] is True
    assert calls[0]["readonly"] is not False


# --- filter_account_summary pins to S0_LIVE_ACCOUNT --------------------------------
def _row(account, tag="NetLiquidation", value="0"):
    return SimpleNamespace(account=account, tag=tag, value=value)


def test_filter_account_summary_keeps_only_s0_account():
    rows = [
        _row("U5721712", "NetLiquidation", "100"),
        _row("U14438624", "NetLiquidation", "999"),   # trust/S8 — must be dropped
        _row("All", "NetLiquidation", "1099"),         # aggregate — must be dropped
        _row("U5721712", "BuyingPower", "50"),
    ]

    kept = s0_live.filter_account_summary(rows)

    assert len(kept) == 2
    assert {r.account for r in kept} == {"U5721712"}
    assert all(r.account != "U14438624" for r in kept)


def test_filter_account_summary_returns_dict_unchanged():
    d = {"NetLiquidation": "100", "BuyingPower": "50"}
    result = s0_live.filter_account_summary(d)
    assert result is d


# --- filter_positions pins to S0_LIVE_ACCOUNT --------------------------------------
def _pos(account, symbol="SPY", qty=1):
    return SimpleNamespace(account=account, symbol=symbol, position=qty)


def test_filter_positions_keeps_only_s0_account():
    positions = [
        _pos("U5721712", "SPY", 10),
        _pos("U14438624", "SPX", 5),   # trust/S8 — must be dropped
        _pos("All", "AGG", 1),          # aggregate — must be dropped
        _pos("U5721712", "BIL", 3),
    ]

    kept = s0_live.filter_positions(positions)

    assert len(kept) == 2
    assert {p.account for p in kept} == {"U5721712"}
    assert all(p.account != "U14438624" for p in kept)


# --- clientId registry: new ids registered, distinct from S8, no collision ---------
def test_s0_live_clientids_registered():
    assert clientids.get("s0_live_pilot") == 57
    assert clientids.get("s0_live_exec") == 58


def test_s0_live_clientids_differ_from_s8_ids():
    s0_ids = {clientids.get("s0_live_pilot"), clientids.get("s0_live_exec")}
    s8_ids = {clientids.get("s8_live_pilot"),
              clientids.get("s8_monitor"),
              clientids.get("s8_collector")}
    assert s0_ids.isdisjoint(s8_ids)


def test_no_clientid_collision_introduced():
    values = list(clientids.CLIENT_IDS.values())
    assert len(values) == len(set(values))
