"""
test_margin_monitor.py — offline unit tests for margin_monitor.py (conductor #26).

NO broker, NO gateway, NO network. Proves the PURE core (snapshot_from_summary / delta /
to_record) parses both a {tag:value} dict and an ib_async-style row list, distinguishes an
ABSENT tag (-> None) from a present "0" (-> 0.0), diffs None-safely, and that the single
broker driver (read_snapshot) fails soft to None. record_impact is exercised against a
tmp STATE_DIR so the audit write is hermetic.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_margin_monitor.py -q
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import config
import margin_monitor as mm


# --- fake accountSummary row (ib_async row has .tag/.value) -------------------------
def _row(tag, value):
    return SimpleNamespace(tag=tag, value=value)


_FULL = {
    "AccountType": "MARGIN",
    "NetLiquidation": "1000000",
    "BuyingPower": "3800000",
    "ExcessLiquidity": "900000",
    "InitMarginReq": "100000",
    "MaintMarginReq": "80000",
    "AvailableFunds": "850000",
}


# --- snapshot_from_summary: dict + list, both parse identically ---------------------
def test_snapshot_from_dict():
    snap = mm.snapshot_from_summary(_FULL, "DU1")
    assert snap.account == "DU1"
    assert snap.account_type == "MARGIN"
    assert snap.net_liq == 1_000_000.0
    assert snap.buying_power == 3_800_000.0
    assert snap.excess_liquidity == 900_000.0
    assert snap.init_margin == 100_000.0
    assert snap.maint_margin == 80_000.0
    assert snap.available_funds == 850_000.0


def test_snapshot_from_row_list_matches_dict():
    rows = [_row(k, v) for k, v in _FULL.items()]
    from_list = mm.snapshot_from_summary(rows, "DU1")
    from_dict = mm.snapshot_from_summary(_FULL, "DU1")
    assert from_list.as_dict() == from_dict.as_dict()


def test_absent_tag_is_none_but_zero_is_zero():
    # BuyingPower missing entirely -> None; ExcessLiquidity present as "0" -> 0.0.
    summary = {"AccountType": "CASH", "NetLiquidation": "500000", "ExcessLiquidity": "0"}
    snap = mm.snapshot_from_summary(summary, "DU1")
    assert snap.buying_power is None          # ABSENT
    assert snap.excess_liquidity == 0.0       # present-but-zero, NOT None
    assert snap.net_liq == 500_000.0
    assert snap.account_type == "CASH"


def test_unparseable_numeric_is_none():
    snap = mm.snapshot_from_summary({"NetLiquidation": "n/a"}, "DU1")
    assert snap.net_liq is None


def test_as_dict_shape():
    snap = mm.snapshot_from_summary(_FULL, "DU1")
    d = snap.as_dict()
    assert set(d) == {"account", "account_type", "net_liq", "buying_power",
                      "excess_liquidity", "init_margin", "maint_margin", "available_funds"}


# --- delta: computes and is None-safe ----------------------------------------------
def test_delta_computes():
    before = mm.snapshot_from_summary(_FULL, "DU1")
    after_map = dict(_FULL, BuyingPower="3500000", ExcessLiquidity="850000")
    after = mm.snapshot_from_summary(after_map, "DU1")
    d = mm.delta(before, after)
    assert d["buying_power_delta"] == -300_000.0
    assert d["excess_liquidity_delta"] == -50_000.0
    assert d["net_liq_delta"] == 0.0


def test_delta_none_when_field_missing_one_side():
    before = mm.snapshot_from_summary({"BuyingPower": "100"}, "DU1")   # net_liq absent
    after = mm.snapshot_from_summary(_FULL, "DU1")
    d = mm.delta(before, after)
    assert d["net_liq_delta"] is None            # before.net_liq is None
    assert d["buying_power_delta"] is not None    # both present


def test_delta_empty_when_snapshot_missing():
    snap = mm.snapshot_from_summary(_FULL, "DU1")
    assert mm.delta(None, snap) == {}
    assert mm.delta(snap, None) == {}
    assert mm.delta(None, None) == {}


# --- to_record shape ----------------------------------------------------------------
def test_to_record_shape():
    before = mm.snapshot_from_summary(_FULL, "DU1")
    after = mm.snapshot_from_summary(dict(_FULL, BuyingPower="3500000"), "DU1")
    rec = mm.to_record(before, after, account="DU1", context="place")
    assert rec["account"] == "DU1"
    assert rec["context"] == "place"
    assert rec["before"]["buying_power"] == 3_800_000.0
    assert rec["after"]["buying_power"] == 3_500_000.0
    assert rec["delta"]["buying_power_delta"] == -300_000.0


def test_to_record_none_snapshots():
    rec = mm.to_record(None, None, account="DU1", context="place")
    assert rec == {"account": "DU1", "context": "place", "before": None,
                   "after": None, "delta": {}}


# --- read_snapshot: only broker touch, fail-soft to None ----------------------------
class _GoodIB:
    def accountSummary(self, account):
        return [_row(k, v) for k, v in _FULL.items()]


class _RaisingIB:
    def accountSummary(self, account):
        raise TimeoutError("simulated accountSummary timeout")


def test_read_snapshot_reads():
    snap = mm.read_snapshot(_GoodIB(), "DU1")
    assert snap is not None
    assert snap.buying_power == 3_800_000.0


def test_read_snapshot_none_on_exception():
    assert mm.read_snapshot(_RaisingIB(), "DU1") is None


# --- record_impact: writes a kind='margin_impact' ledger record ---------------------
def test_record_impact_writes_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    import ledger
    monkeypatch.setattr(ledger, "RUNS_JSONL", os.path.join(str(tmp_path), "runs.jsonl"))
    monkeypatch.setattr(ledger, "LOG_TXT", os.path.join(str(tmp_path), "paperbot.log"))

    before = mm.snapshot_from_summary(_FULL, "DU1")
    after = mm.snapshot_from_summary(dict(_FULL, BuyingPower="3500000"), "DU1")
    path = mm.record_impact(before, after, account="DU1", context="place")
    assert path is not None

    lines = open(path, encoding="utf-8").read().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "margin_impact"
    assert rec["account"] == "DU1"
    assert rec["before"]["buying_power"] == 3_800_000.0
    assert rec["after"]["buying_power"] == 3_500_000.0
    assert rec["delta"]["buying_power_delta"] == -300_000.0


def test_record_impact_via_captured_record_run(monkeypatch):
    # Alternative: monkeypatch ledger.record_run to capture the dict directly.
    import ledger
    captured = {}
    monkeypatch.setattr(ledger, "record_run", lambda rec: captured.update(rec) or "PATH")
    before = mm.snapshot_from_summary(_FULL, "DU1")
    after = mm.snapshot_from_summary(_FULL, "DU1")
    assert mm.record_impact(before, after, account="DU1") == "PATH"
    assert captured["kind"] == "margin_impact"
    assert "before" in captured and "after" in captured and "delta" in captured


def test_record_impact_skips_when_both_none(monkeypatch):
    import ledger
    monkeypatch.setattr(ledger, "record_run",
                        lambda rec: (_ for _ in ()).throw(AssertionError("must not write")))
    assert mm.record_impact(None, None, account="DU1") is None
