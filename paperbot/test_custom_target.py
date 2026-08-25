"""test_custom_target.py — the desk-side adapter for Andrew-authored ("custom") CRM model
portfolios. NO LIVE DATABASE: every CRM read is a fake, and every price load is injected.

The four traps this suite exists to hold shut (see custom_target's module docstring):
  1. dispatch keys on allocation SOURCE, never on the label's spelling — a "(Small, Custom)"
     label must NOT be collapsed to {SCHB, USFR} by the S0 path;
  2. Target.version round-trips the caller's targets-dict key VERBATIM (no "@v7");
  3. an unknown ticker fails LOUD, naming the ticker and the model (never a silent drop);
  4. the universe function exposes the allocation's own tickers plus the held ones, so a
     rotation out of a custom ticker cannot be silently swallowed as ALIEN.
Plus: "no published allocation" is an ERROR, never an empty (account-liquidating) Target.
"""
import pandas as pd
import pytest

import crm_roster
import custom_target as ct


# --- fakes ----------------------------------------------------------------------
def _row(label, ticker, pct, *, version_number=3,
         version_id="11111111-1111-1111-1111-111111111111",
         effective_from="2026-08-20", published_at="2026-08-19T14:00:00+00:00",
         strategy_code="GROWTH_CUSTOM",
         strategy_id="22222222-2222-2222-2222-222222222222"):
    """One row shaped exactly like v_tradingdesk_custom_allocations."""
    return {"strategy_name": label, "strategy_code": strategy_code, "ticker": ticker,
            "weight_pct": pct, "version_number": version_number,
            "effective_from": effective_from, "published_at": published_at,
            "version_id": version_id, "strategy_id": strategy_id}


def _alloc(label="Growth (Custom)"):
    """A well-formed 3-ticker book: percentages summing to exactly 100."""
    return [_row(label, "SCHB", 60), _row(label, "VEA", 25), _row(label, "USFR", 15)]


def _prices(tickers, last="2026-08-25"):
    idx = pd.to_datetime(["2026-08-24", last])
    return pd.DataFrame({t: [10.0 + i, 11.0 + i] for i, t in enumerate(tickers)}, index=idx)


class _FakeCrm:
    """Stands in for crm_roster's two custom-allocation readers."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def fetch(self, strategy_names=None, conn=None):
        self.calls.append(strategy_names)
        if strategy_names is None:
            return list(self.rows)
        names = [str(n) for n in strategy_names]
        if not names:
            return []
        return [r for r in self.rows if r["strategy_name"] in names]

    def labels(self, conn=None):
        return {r["strategy_name"] for r in self.rows}


@pytest.fixture
def crm(monkeypatch):
    """Install a fake CRM holding one well-formed Growth (Custom) book."""
    fake = _FakeCrm(_alloc())
    monkeypatch.setattr(ct.crm_roster, "fetch_custom_allocations", fake.fetch)
    monkeypatch.setattr(ct.crm_roster, "custom_allocation_labels", fake.labels)
    return fake


# --- weights: PERCENT (100) -> FRACTION (1.0) ------------------------------------
def test_weights_convert_percent_to_fraction():
    w = ct.weights_from_rows(_alloc(), "Growth (Custom)")
    assert dict(w) == pytest.approx({"SCHB": 0.60, "VEA": 0.25, "USFR": 0.15})
    assert float(w.sum()) == pytest.approx(1.0, abs=1e-12)


def test_weights_sum_to_one_for_thirds():
    """Uneven thirds (33.33/33.33/33.34) still land on 1.0 after the /100 conversion."""
    rows = [_row("X", "AAA", 33.33), _row("X", "BBB", 33.33), _row("X", "CCC", 33.34)]
    w = ct.weights_from_rows(rows, "X")
    assert float(w.sum()) == pytest.approx(1.0, abs=1e-9)


def test_weights_that_do_not_sum_to_100_fail_loud():
    rows = [_row("X", "AAA", 60), _row("X", "BBB", 25)]     # 85%
    with pytest.raises(ct.CustomAllocationError) as e:
        ct.weights_from_rows(rows, "X")
    assert "85" in str(e.value)


def test_duplicate_ticker_fails_loud():
    rows = [_row("X", "AAA", 50), _row("X", "AAA", 50)]
    with pytest.raises(ct.CustomAllocationError) as e:
        ct.weights_from_rows(rows, "X")
    assert "more than once" in str(e.value)


def test_negative_weight_fails_loud():
    rows = [_row("X", "AAA", 110), _row("X", "BBB", -10)]
    with pytest.raises(ct.CustomAllocationError) as e:
        ct.weights_from_rows(rows, "X")
    assert "negative" in str(e.value)


def test_zero_weight_lines_are_dropped():
    rows = [_row("X", "AAA", 100), _row("X", "BBB", 0)]
    w = ct.weights_from_rows(rows, "X")
    assert list(w.index) == ["AAA"]


# --- TRAP: "no allocation" is never an empty Target -------------------------------
def test_no_rows_raises_no_custom_allocation_not_empty_target():
    with pytest.raises(ct.NoCustomAllocation):
        ct.weights_from_rows([], "Growth (Custom)")


def test_build_target_with_no_rows_cannot_produce_an_empty_target():
    """An empty Target would be executed as 'hold nothing' == LIQUIDATE the account.
    There must be no input that yields one."""
    with pytest.raises(ct.NoCustomAllocation) as e:
        ct.build_target([], "Growth (Custom)", prices=_prices(["SCHB"]))
    assert "liquidat" in str(e.value).lower()


def test_all_zero_weights_cannot_produce_an_empty_target():
    rows = [_row("X", "AAA", 0), _row("X", "BBB", 0)]
    with pytest.raises(ct.CustomAllocationError):
        ct.weights_from_rows(rows, "X")   # 0 != 100 -> loud, never an empty book


def test_current_target_for_unpublished_model_reports_no_allocation(crm):
    """The live case today: the view returns ZERO rows for everything."""
    with pytest.raises(ct.NoCustomAllocation):
        ct.current_target("Balanced (Custom)", prices=_prices(["SCHB"]))


def test_empty_crm_view_yields_no_labels_and_no_targets(monkeypatch):
    """Exactly the live state verified on this machine: view readable, zero rows."""
    fake = _FakeCrm([])
    monkeypatch.setattr(ct.crm_roster, "fetch_custom_allocations", fake.fetch)
    monkeypatch.setattr(ct.crm_roster, "custom_allocation_labels", fake.labels)
    assert ct.custom_allocation_labels() == set()
    assert ct.is_custom_allocation("Growth (Custom)") is False
    assert ct.custom_targets_for(["Growth (Custom)", "Growth"]) == {}
    assert ct.split_labels(["Growth (Custom)", "Growth"]) == ([], ["Growth (Custom)", "Growth"])


# --- TRAP 2: Target.version IS the caller's dict key ------------------------------
def test_target_version_round_trips_the_label_exactly():
    label = "Growth (Custom)"
    target, meta = ct.build_target(_alloc(label), label,
                                   prices=_prices(["SCHB", "VEA", "USFR"]))
    assert target.version == label
    # The allocation's version travels SEPARATELY (decorating Target.version would be a hard
    # KeyError at crm_execute.py:78 -> targets[plan.version]).
    assert "@" not in target.version and "v3" not in target.version
    assert meta.version_number == 3
    assert str(meta.version_id).startswith("1111")
    assert str(meta.effective_from) == "2026-08-20"


def test_targets_dict_key_lookup_pattern_resolves(crm):
    """Mimics batch_rebalance_execute:442 -> plan.version -> crm_execute.py:78
    targets[plan.version]. A decorated version string would KeyError here."""
    monkey_prices = _prices(["SCHB", "VEA", "USFR"])
    targets = {"Growth (Custom)": ct.build_target(_alloc(), "Growth (Custom)",
                                                  prices=monkey_prices)[0]}
    plan_version = targets["Growth (Custom)"].version      # what the plan carries
    assert targets[plan_version] is targets["Growth (Custom)"]


def test_meta_stamp_carries_the_audit_fields():
    _t, meta = ct.build_target(_alloc(), "Growth (Custom)",
                               prices=_prices(["SCHB", "VEA", "USFR"]))
    stamp = meta.stamp()
    assert stamp["custom_model"] == "Growth (Custom)"
    assert stamp["custom_version_number"] == 3
    assert stamp["custom_effective_from"] == "2026-08-20"


def test_rows_spanning_two_versions_fail_loud():
    rows = _alloc()
    rows[1] = dict(rows[1], version_number=4, version_id="99999999-9999-9999-9999-999999999999")
    with pytest.raises(ct.CustomAllocationError) as e:
        ct.meta_from_rows(rows, "Growth (Custom)")
    assert "MULTIPLE" in str(e.value)


# --- TRAP 3: unknown ticker fails LOUD --------------------------------------------
class _FakeLoader:
    """Mimics backtester data_loader.load_prices: raises KeyError for tickers it lacks when
    given an explicit list, and SILENTLY DROPS them when given None (the real trap)."""

    def __init__(self, have):
        self.have = list(have)
        self.calls = []

    def __call__(self, tickers=None):
        self.calls.append(tickers)
        if tickers is None:
            wanted = list(self.have)
        else:
            missing = [t for t in tickers if t not in self.have]
            if missing:
                raise KeyError(f"Requested tickers not found in data/: {missing}. "
                               "Was the downloader run for them?")
            wanted = list(tickers)
        return _prices(wanted)


def test_unknown_ticker_raises_loudly_naming_ticker_and_model(monkeypatch):
    loader = _FakeLoader(["SCHB", "USFR"])          # no VEA on disk
    monkeypatch.setattr(ct.data_loader, "load_prices", loader)
    with pytest.raises(ct.CustomAllocationError) as e:
        ct.build_target(_alloc(), "Growth (Custom)")
    msg = str(e.value)
    assert "VEA" in msg                              # names the offending ticker
    assert "Growth (Custom)" in msg                  # names the model
    assert "NO PRICE HISTORY" in msg


def test_price_loader_is_always_called_with_an_explicit_ticker_list(monkeypatch):
    """tickers=None makes data_loader silently omit unknown tickers -> price NaN ->
    target_shares 0 -> never bought AND never band-breaching. We must never pass None."""
    loader = _FakeLoader(["SCHB", "VEA", "USFR"])
    monkeypatch.setattr(ct.data_loader, "load_prices", loader)
    ct.build_target(_alloc(), "Growth (Custom)")
    assert loader.calls, "the loader was never called"
    for call in loader.calls:
        assert call is not None
        assert sorted(call) == ["SCHB", "USFR", "VEA"]


def test_loader_that_silently_drops_a_column_still_fails_loud(monkeypatch):
    """Belt-and-suspenders: even if a future loader stopped raising, a missing column must
    not become a silent under-hold."""
    def sneaky(tickers=None):
        return _prices([t for t in tickers if t != "VEA"])
    monkeypatch.setattr(ct.data_loader, "load_prices", sneaky)
    with pytest.raises(ct.CustomAllocationError) as e:
        ct.build_target(_alloc(), "Growth (Custom)")
    assert "VEA" in str(e.value)


def test_all_nan_price_column_fails_loud():
    idx = pd.to_datetime(["2026-08-24", "2026-08-25"])
    frame = pd.DataFrame({"SCHB": [10.0, 11.0], "VEA": [float("nan")] * 2,
                          "USFR": [50.0, 50.0]}, index=idx)
    with pytest.raises(ct.CustomAllocationError) as e:
        ct.build_target(_alloc(), "Growth (Custom)", prices=frame)
    assert "VEA" in str(e.value)


def test_prices_use_the_last_row_with_ffill():
    idx = pd.to_datetime(["2026-08-24", "2026-08-25"])
    frame = pd.DataFrame({"SCHB": [10.0, 12.0], "VEA": [20.0, float("nan")],
                          "USFR": [50.0, 50.5]}, index=idx)
    target, _m = ct.build_target(_alloc(), "Growth (Custom)", prices=frame)
    assert target.price_date == pd.Timestamp("2026-08-25")
    assert float(target.prices["SCHB"]) == 12.0
    assert float(target.prices["VEA"]) == 20.0          # ffilled, not NaN
    assert target.as_of == pd.Timestamp("2026-08-20")   # effective_from, not the price date


# --- TRAP 1: dispatch on SOURCE, never on the name --------------------------------
def test_small_named_custom_label_is_not_collapsed(monkeypatch):
    """'Growth (Small, Custom)' must keep ALL its tickers. The S0 path
    (strategy_target.py:54-62) would collapse a small label to {SCHB, USFR} by re-running the
    backtester; this module never goes near it."""
    label = "Growth (Small, Custom)"
    rows = [_row(label, "SCHB", 40), _row(label, "VEA", 35), _row(label, "USFR", 25)]
    fake = _FakeCrm(rows)
    monkeypatch.setattr(ct.crm_roster, "fetch_custom_allocations", fake.fetch)
    monkeypatch.setattr(ct.crm_roster, "custom_allocation_labels", fake.labels)
    monkeypatch.setattr(ct.data_loader, "load_prices",
                        _FakeLoader(["SCHB", "VEA", "USFR"]))

    target = ct.current_target(label)
    assert sorted(target.weights.index) == ["SCHB", "USFR", "VEA"]
    assert float(target.weights["VEA"]) == pytest.approx(0.35)
    assert target.version == label


def test_module_never_touches_the_s0_collapse_path():
    """Static guard on trap 1: the adapter must not import small_tier or call
    strategy_target.current_target — a rename in the CRM would then be able to route a
    hand-authored book through the S0 backtester."""
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path(ct.__file__).read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported |= {a.name for a in node.names}
    assert not any("small_tier" in m for m in imported), imported

    # No call to the S0 target builder, under any spelling.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name == "current_target":
                owner = getattr(getattr(fn, "value", None), "id", None)
                assert owner != "strategy_target", "custom path must not call the S0 target"
            assert name not in {"collapse", "parent_version", "is_small"}

    # The module also does not import strategy_target's builder by name.
    assert not hasattr(ct, "current_target_s0")
    assert getattr(ct, "small_tier", None) is None


def test_is_custom_allocation_is_source_based_not_name_based(crm):
    assert ct.is_custom_allocation("Growth (Custom)") is True
    # Published-source says no, even though the NAME screams custom.
    assert ct.is_custom_allocation("Balanced (Custom)") is False
    # ...and a plain S0 label is likewise decided by source alone.
    assert ct.is_custom_allocation("Growth") is False


def test_split_labels_partitions_by_source(crm):
    custom, other = ct.split_labels(["Growth", "Growth (Custom)", "Balanced (Small)"])
    assert custom == ["Growth (Custom)"]
    assert other == ["Growth", "Balanced (Small)"]


def test_custom_targets_for_returns_only_custom_labels(crm, monkeypatch):
    monkeypatch.setattr(ct.data_loader, "load_prices",
                        _FakeLoader(["SCHB", "VEA", "USFR"]))
    out = ct.custom_targets_for(["Growth", "Growth (Custom)"])
    assert list(out) == ["Growth (Custom)"]
    assert out["Growth (Custom)"].version == "Growth (Custom)"


def test_custom_targets_for_empty_labels_does_no_crm_read(crm):
    assert ct.custom_targets_for([]) == {}
    assert crm.calls == []


def test_custom_targets_with_meta_pairs_target_and_meta(crm, monkeypatch):
    monkeypatch.setattr(ct.data_loader, "load_prices",
                        _FakeLoader(["SCHB", "VEA", "USFR"]))
    out = ct.custom_targets_with_meta(["Growth (Custom)"])
    target, meta = out["Growth (Custom)"]
    assert target.version == "Growth (Custom)" and meta.version_number == 3


def test_custom_targets_for_raises_when_a_custom_book_is_unbuildable(crm, monkeypatch):
    """A model that IS custom must never fall through to the S0 path (trap 1) — if its book
    cannot be built, the run stops."""
    monkeypatch.setattr(ct.data_loader, "load_prices", _FakeLoader(["SCHB", "USFR"]))
    with pytest.raises(ct.CustomAllocationError):
        ct.custom_targets_for(["Growth (Custom)"])


# --- TRAP 4: the universe ---------------------------------------------------------
def test_universe_includes_allocation_tickers_and_held():
    target, _m = ct.build_target(_alloc(), "Growth (Custom)",
                                 prices=_prices(["SCHB", "VEA", "USFR"]))
    uni = ct.universe_for(target, held=["XLK", "GLDM"])
    assert {"SCHB", "VEA", "USFR"} <= uni
    assert {"XLK", "GLDM"} <= uni


def test_universe_held_ticker_is_what_makes_a_rotation_possible():
    """A held ticker NOT in the universe is ALIEN, and ALIEN is in
    rebalance_engine._NO_AUTOTRADE_STATUSES — it never breaches the band and never produces a
    delta, so the rotation would silently do nothing."""
    import rebalance_engine
    target, _m = ct.build_target(_alloc(), "Growth (Custom)",
                                 prices=_prices(["SCHB", "VEA", "USFR"]))
    assert "ALIEN" in rebalance_engine._NO_AUTOTRADE_STATUSES
    assert "OLDTKR" not in ct.universe_for(target)                 # would be ALIEN
    assert "OLDTKR" in ct.universe_for(target, held=["OLDTKR"])    # rotatable


def test_universe_base_lets_an_account_rotate_off_its_old_s0_sleeve():
    target, _m = ct.build_target(_alloc(), "Growth (Custom)",
                                 prices=_prices(["SCHB", "VEA", "USFR"]))
    uni = ct.universe_for(target, base={"XLK", "XLV"})
    assert {"XLK", "XLV", "SCHB"} <= uni


def test_universe_for_targets_unions_every_model():
    a, _ = ct.build_target(_alloc("A"), "A", prices=_prices(["SCHB", "VEA", "USFR"]))
    rows_b = [_row("B", "IVV", 50), _row("B", "USFR", 50)]
    b, _ = ct.build_target(rows_b, "B", prices=_prices(["IVV", "USFR"]))
    uni = ct.universe_for_targets({"A": a, "B": b}, held=["ZZZ"])
    assert uni == {"SCHB", "VEA", "USFR", "IVV", "ZZZ"}


# --- the CRM reader itself (no socket) --------------------------------------------
def test_fetch_custom_allocations_without_dsn_raises(monkeypatch):
    monkeypatch.delenv(crm_roster.DSN_ENV, raising=False)
    with pytest.raises(crm_roster.CrmRosterUnavailable):
        crm_roster.fetch_custom_allocations()


def test_custom_allocation_labels_without_dsn_raises(monkeypatch):
    monkeypatch.delenv(crm_roster.DSN_ENV, raising=False)
    with pytest.raises(crm_roster.CrmRosterUnavailable):
        crm_roster.custom_allocation_labels()


def test_fetch_custom_allocations_empty_name_list_short_circuits(monkeypatch):
    """An explicit empty request must not open a connection at all."""
    def boom():
        raise AssertionError("_connect() must not be called for an empty name list")
    monkeypatch.setattr(crm_roster, "_connect", boom)
    assert crm_roster.fetch_custom_allocations([]) == []


class _FakeCursor:
    def __init__(self, rows, columns):
        self._rows, self._columns = rows, columns
        self.executed = []
        self.description = [(c,) for c in columns]

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.closed = False

    def cursor(self):
        return self._cur

    def close(self):
        self.closed = True


def test_fetch_custom_allocations_is_parameterised_and_read_only():
    cur = _FakeCursor([("Growth (Custom)", "GC", "SCHB", 60, 3, "2026-08-20",
                        "2026-08-19", "vid", "sid")],
                      list(crm_roster._CUSTOM_ALLOCATION_COLUMNS))
    conn = _FakeConn(cur)
    rows = crm_roster.fetch_custom_allocations(["Growth (Custom)"], conn=conn)
    sql, params = cur.executed[0]
    assert sql.lstrip().lower().startswith("select")          # read-only
    assert "%s" in sql and params == [["Growth (Custom)"]]    # parameterised, not f-string
    assert crm_roster.CUSTOM_ALLOCATIONS_VIEW in sql
    assert rows[0]["ticker"] == "SCHB" and rows[0]["weight_pct"] == 60
    assert conn.closed is False        # caller-owned connection is NOT closed by the reader


def test_fetch_custom_allocations_wraps_query_errors():
    class _Boom(_FakeCursor):
        def execute(self, sql, params=None):
            raise RuntimeError("relation does not exist")
    conn = _FakeConn(_Boom([], list(crm_roster._CUSTOM_ALLOCATION_COLUMNS)))
    with pytest.raises(crm_roster.CrmRosterUnavailable):
        crm_roster.fetch_custom_allocations(["X"], conn=conn)


def test_custom_allocation_labels_dedupes():
    cur = _FakeCursor([("Growth (Custom)",), ("Balanced (Custom)",)], ["strategy_name"])
    labels = crm_roster.custom_allocation_labels(conn=_FakeConn(cur))
    assert labels == {"Growth (Custom)", "Balanced (Custom)"}


def test_custom_allocation_labels_empty_view_returns_empty_set():
    """The LIVE state today: the view exists, is readable, and has no published rows."""
    cur = _FakeCursor([], ["strategy_name"])
    assert crm_roster.custom_allocation_labels(conn=_FakeConn(cur)) == set()
