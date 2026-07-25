"""test_ibkr_forward_hang.py — offline tests for the unattended-run safety bounds
added to ibkr_forward_live.py (conductor #59).

Fully offline: a minimal FAKE ib (no real gateway, no network) exercises the three
bounds so an unattended nightly pull can never wedge on one bad root:

 (a) EARLY-OUT — a root whose reqMktData tickers never populate greeks/OI (the QQQ /
     Error-10091 no-entitlement case): snapshot_chain must stop after the sample
     threshold instead of walking every batch, and collect_day returns "no-data".
 (b) PER-ROOT DEADLINE — a fake clock that ib.sleep advances so the wall-clock
     deadline is exceeded mid-run: snapshot_chain aborts with a partial, collect_day
     returns "timeout", and a main-style loop over two roots CONTINUES to the second
     root after the first times out.
 (c) SPOT TIMEOUT — reqTickers/util.run timing out in _underlying: spot falls back to
     None without raising, and collection still proceeds.
 (d) HAPPY PATH — a normally-populated root still collects the full chain into the
     exact warehouse schema (regression: nothing about the happy path changed).

Run from datacollector/:
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest test_ibkr_forward_hang.py -q
"""

from __future__ import annotations

import asyncio

import pandas as pd
from ib_async import Stock

import ibkr_forward_live as m

NAN = float("nan")


# --------------------------------------------------------------------------- #
# Minimal fakes — only what build_chain / snapshot_chain / _underlying touch.
# --------------------------------------------------------------------------- #
class FakeGreeks:
    delta = -0.42
    gamma = 0.011
    vega = 0.13
    theta = -0.08
    impliedVol = 0.19
    undPrice = 101.5


class FakeTicker:
    """A reqMktData ticker. populate=False mimics a no-entitlement option: modelGreeks
    is None and OI is NaN (exactly what IBKR returns under Error 10091)."""

    def __init__(self, populate: bool = True):
        if populate:
            self.bid, self.ask = 1.10, 1.20
            self.bidSize, self.askSize = 5, 7
            self.last, self.close, self.volume = 1.15, 1.12, 250
            self.callOpenInterest, self.putOpenInterest = 800.0, 600.0
            self.modelGreeks = FakeGreeks()
        else:
            self.bid = self.ask = self.bidSize = self.askSize = NAN
            self.last = self.close = self.volume = NAN
            self.callOpenInterest = self.putOpenInterest = NAN
            self.modelGreeks = None

    def marketPrice(self):
        return self.last


class FakeSpotTicker:
    close = 100.5
    last = 100.7

    def marketPrice(self):
        return 101.0


class FakeParam:
    def __init__(self, exps, strikes, tradingClass=""):
        self.expirations = set(exps)
        self.strikes = set(strikes)
        self.tradingClass = tradingClass


class FakeClock:
    """A controllable monotonic clock; FakeIB.sleep advances it."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def monotonic(self):
        return self.t

    def advance(self, secs):
        self.t += secs


class FakeIB:
    """Satisfies exactly the ib calls the collector makes — offline, no gateway."""

    def __init__(self, roots: dict):
        self.roots = roots          # {sym: {"exps":[...], "strikes":[...], "populate":bool}}
        self.clock: FakeClock | None = None
        self._conid = 1000
        self.cancelled = 0

    def qualifyContracts(self, *contracts):
        out = []
        for c in contracts:
            self._conid += 1
            c.conId = self._conid   # real ib_async mutates in place; mirror that
            out.append(c)
        return out

    async def reqTickersAsync(self, *contracts):
        return [FakeSpotTicker()]

    def reqSecDefOptParams(self, symbol, exch, secType, conId):
        cfg = self.roots[symbol]
        return [FakeParam(cfg["exps"], cfg["strikes"])]

    def reqMktData(self, o, genTicks, snapshot, regSnapshot):
        return FakeTicker(populate=self.roots[o.symbol]["populate"])

    def sleep(self, secs):
        if self.clock is not None:
            self.clock.advance(secs)

    def cancelMktData(self, o):
        self.cancelled += 1

    def reqMarketDataType(self, x):
        pass


def _rootcfg(n_exps: int, n_strikes: int, populate: bool) -> dict:
    exps = [f"2026{mo:02d}20" for mo in range(1, n_exps + 1)]     # yyyymmdd, chronological
    strikes = [100.0 + i for i in range(n_strikes)]
    return {"exps": exps, "strikes": strikes, "populate": populate}


# --------------------------------------------------------------------------- #
# (a) fast early-out on a no-entitlement / no-data root (the QQQ case)
# --------------------------------------------------------------------------- #
def test_early_out_stops_before_full_walk():
    # 6 exps * 40 strikes * 2 rights = 480 contracts -> 6 batches of 90 if fully walked.
    ib = FakeIB({"QQQ": _rootcfg(6, 40, populate=False)})
    _, _, contracts = m.build_chain(ib, "QQQ")
    total = len(contracts)
    assert total == 480

    rows, status = m.snapshot_chain(ib, contracts)

    assert status == "no-data"
    populated = sum(1 for r in rows if r["delta"] is not None or r["open_interest"] is not None)
    assert populated == 0
    # Must have STOPPED early — well before walking all 480 (the 300-contract sample
    # threshold trips first, ~360 rows), not the full chain.
    assert len(rows) < total
    assert 300 <= len(rows) <= 360


def test_early_out_collect_day_returns_no_data(monkeypatch, tmp_path):
    monkeypatch.setattr(m.config, "RAW_OPTIONS_IBKR", tmp_path)
    ib = FakeIB({"QQQ": _rootcfg(6, 40, populate=False)})
    status, n = m.collect_day(ib, "QQQ", "20260724")
    assert status == "no-data"
    assert n == 0
    # No-data must NOT poison the day (nothing written -> next run retries).
    assert not m.storage.have_day("QQQ", "20260724", base=tmp_path)


# --------------------------------------------------------------------------- #
# (b) per-root wall-clock deadline + main-style loop continues to the next root
# --------------------------------------------------------------------------- #
def test_deadline_timeout_and_loop_continues(monkeypatch, tmp_path):
    monkeypatch.setattr(m.config, "RAW_OPTIONS_IBKR", tmp_path)
    clock = FakeClock()
    monkeypatch.setattr(m.time, "monotonic", clock.monotonic)

    ib = FakeIB({
        "SPX": _rootcfg(6, 40, populate=True),   # 480 contracts -> would need 6 batches
        "SPY": _rootcfg(1, 30, populate=True),   # 60 contracts  -> 1 batch, finishes fast
    })
    ib.clock = clock

    results: dict[str, tuple[str, int]] = {}
    timeout = 20.0                                # SETTLE_SECS=6/batch -> trips ~batch 4
    for sym in ["SPX", "SPY"]:                    # mirror main()'s per-root loop
        deadline = m.time.monotonic() + timeout
        results[sym] = m.collect_day(ib, sym, "20260724", deadline=deadline)

    # First root hit the deadline mid-run and returned a PARTIAL (written, populated>0).
    assert results["SPX"][0] == "timeout"
    assert 0 < results["SPX"][1] < 480
    # The loop CONTINUED to the second root, which collected fully — one bad root
    # never aborts the run.
    assert results["SPY"] == ("ok", 60)


def test_deadline_with_no_populated_rows_does_not_write(monkeypatch, tmp_path):
    # Deadline hit AND nothing populated -> "timeout" but no write (don't poison day).
    monkeypatch.setattr(m.config, "RAW_OPTIONS_IBKR", tmp_path)
    clock = FakeClock()
    monkeypatch.setattr(m.time, "monotonic", clock.monotonic)
    ib = FakeIB({"SPX": _rootcfg(6, 40, populate=False)})
    ib.clock = clock
    # Tiny deadline so it trips before the early-out sample threshold is reached.
    deadline = clock.monotonic() + 5.0
    status, n = m.collect_day(ib, "SPX", "20260724", deadline=deadline)
    assert status == "timeout"
    assert n == 0
    assert not m.storage.have_day("SPX", "20260724", base=tmp_path)


# --------------------------------------------------------------------------- #
# (c) spot lookup timeout in _underlying falls back to None (no raise)
# --------------------------------------------------------------------------- #
def test_spot_timeout_falls_back_to_none(monkeypatch):
    def boom(*a, **k):
        for x in a:                          # close the un-run coroutine (no warning)
            getattr(x, "close", lambda: None)()
        raise asyncio.TimeoutError()

    monkeypatch.setattr(m.util, "run", boom)
    ib = FakeIB({"SPY": _rootcfg(2, 20, populate=True)})
    c = Stock("SPY", "SMART", "USD")
    assert m._spot_with_timeout(ib, c) is None          # no raise, degrades to None


def test_spot_timeout_collection_still_proceeds(monkeypatch, tmp_path):
    monkeypatch.setattr(m.config, "RAW_OPTIONS_IBKR", tmp_path)

    def boom(*a, **k):
        for x in a:                          # close the un-run coroutine (no warning)
            getattr(x, "close", lambda: None)()
        raise asyncio.TimeoutError()

    monkeypatch.setattr(m.util, "run", boom)             # every spot lookup "times out"
    ib = FakeIB({"SPY": _rootcfg(2, 20, populate=True)})
    status, n = m.collect_day(ib, "SPY", "20260724")
    # spot=None tolerated: per-row model-greeks undPrice backfills the underlying.
    assert status == "ok"
    assert n == 2 * 20 * 2
    df = pd.read_parquet(tmp_path / "SPY" / "20260724.parquet")
    assert df["underlying_price"].notna().all()


# --------------------------------------------------------------------------- #
# (d) regression: a normal populated root collects fully, schema unchanged
# --------------------------------------------------------------------------- #
def test_normal_root_collects_fully(monkeypatch, tmp_path):
    monkeypatch.setattr(m.config, "RAW_OPTIONS_IBKR", tmp_path)
    ib = FakeIB({"SPY": _rootcfg(3, 25, populate=True)})   # 3*25*2 = 150 rows
    status, n = m.collect_day(ib, "SPY", "20260724")
    assert status == "ok"
    assert n == 150
    assert m.storage.have_day("SPY", "20260724", base=tmp_path)
    df = pd.read_parquet(tmp_path / "SPY" / "20260724.parquet")
    assert list(df.columns) == m.SCHEMA_COLS               # exact warehouse schema, unchanged
    assert len(df) == 150
    assert df["delta"].notna().all()
    assert df["open_interest"].notna().all()


def test_snapshot_full_walk_status_ok():
    # Generous deadline -> the whole chain is walked and status is "ok".
    ib = FakeIB({"SPY": _rootcfg(2, 20, populate=True)})
    _, _, contracts = m.build_chain(ib, "SPY")
    rows, status = m.snapshot_chain(ib, contracts, deadline=m.time.monotonic() + 10_000)
    assert status == "ok"
    assert len(rows) == len(contracts) == 80
