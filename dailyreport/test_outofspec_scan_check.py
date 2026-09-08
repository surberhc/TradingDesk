"""Tests for the consolidated whole-book out-of-spec check.

Covers the PURE notice/detail assembly and the poster-side snooze skip (the piece that
silences the daily re-nag). The CRM/engine scan itself is monkeypatched — this file never
touches a broker or the live CRM.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _p in (str(_HERE), str(_REPO / "connections"), str(_REPO / "paperbot"),
           str(_REPO / "dashboard" / "desk")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import outofspec_scan_check as job  # noqa: E402


def _scan(n_oos=2, n_acct=10):
    # U1 holds an individual bond: HELD ASIDE (priced, counted, never traded) and sitting
    # outside the model allocation, so its 4 legs rebalance the MANAGED sleeve only.
    verdicts = [
        {"account": "U1", "version": "Growth", "advisor_name": "Amy", "net_liq": 1_000_000.0,
         "managed_net_liq": 900_000.0, "held_aside_value": 100_000.0,
         "out_of_spec": True, "n_legs": 4, "n_held_aside": 1, "n_unclassified": 0,
         "blocked": False},
        {"account": "U2", "version": "Growth", "advisor_name": None, "net_liq": 50_000.0,
         "managed_net_liq": 50_000.0, "held_aside_value": 0.0,
         "out_of_spec": True, "n_legs": 2, "n_held_aside": 0, "n_unclassified": 0,
         "blocked": False},
        {"account": "U3", "version": "Growth", "advisor_name": "Amy", "net_liq": 25_000.0,
         "managed_net_liq": 25_000.0, "held_aside_value": 0.0,
         "out_of_spec": False, "n_legs": 0, "n_held_aside": 0, "n_unclassified": 0,
         "blocked": False},
    ]
    return {"verdicts": verdicts, "skipped": [], "n_accounts": n_acct,
            "n_out_of_spec": n_oos, "n_in_spec": n_acct - n_oos, "bad_versions": [],
            # The whole book was checked: no coverage gap.
            "unresolved_models": [], "n_unmonitored": 0, "unmonitored_value": 0.0,
            "n_roster": n_acct}


# =========================================================================== #
# A stand-in for the TRADING RAIL, so run_scan can be tested without the client
# system or a broker. Every fake below stands in for a function run_scan CALLS
# rather than re-implements — which is the property these tests exist to pin.
# =========================================================================== #
class _FakeTarget:
    def __init__(self, version):
        self.version = version
        self.weights = {}


class _RailSpy:
    """Records what run_scan asked the rail for, and what it handed the pure engine."""

    def __init__(self):
        self.scan_calls = []
        self.resolved_for = None
        self.built_labels = []
        self.asked_the_advisor_wall = False


def _install_fake_rail(monkeypatch, *, roster, resolved, buildable, holdings=None,
                       book=None, book_source="crm"):
    """Put fake ``crm_roster`` / ``batch_rebalance_execute`` / ``crm_outofspec`` /
    ``s0_live_pilot_run`` / ``roster`` modules in front of run_scan's lazy imports.

    ``resolved`` maps account -> the model label the rail says that account really runs.
    ``buildable`` is the set of labels the rail can build a target for; every other label
    raises ``TargetBuildFailed`` exactly as the real ``build_targets`` does.

    ``book`` is what the ADVISOR WALL (``roster.enrolled_roster_scan``) says is Andrew's book;
    None means every row given. The roster VIEW deliberately carries Ted's and Doug's clients
    too, so these two are separate on purpose — that gap is the bug the wall closes."""
    import types

    spy = _RailSpy()
    holdings = holdings or {}
    book = [str(r["account_id"]) for r in roster] if book is None else [str(a) for a in book]

    class _Unavailable(Exception):
        pass

    crm_roster = types.ModuleType("crm_roster")
    crm_roster.CrmRosterUnavailable = _Unavailable
    crm_roster.is_configured = lambda: True
    crm_roster.fetch_roster = lambda advisor_name=None: [dict(r) for r in roster]
    crm_roster.fetch_holdings_latest = lambda ids: holdings
    crm_roster.account_identifier = lambda row: str(row["account_id"])

    class _TargetBuildFailed(RuntimeError):
        def __init__(self, label, cause):
            self.label = str(label)
            self.cause = cause
            super().__init__(str(cause))

    def _build_targets(labels):
        spy.built_labels.append(list(labels))
        for label in labels:
            if label not in buildable:
                raise _TargetBuildFailed(
                    label, RuntimeError(f"unknown client version: {label}"))
        return ({v: _FakeTarget(v) for v in labels},
                {v: object() for v in labels})     # every label here is a custom allocation

    def _resolve(accounts):
        spy.resolved_for = list(accounts)
        return {a: resolved[a] for a in accounts}

    bre = types.ModuleType("batch_rebalance_execute")
    bre.TargetBuildFailed = _TargetBuildFailed
    bre.build_targets = _build_targets
    bre.resolve_roster_versions = _resolve
    bre.account_universe = lambda target, meta, held, base=None: set(held)
    bre.account_reserve_pct = lambda meta: 0.01 if meta is not None else 0.015

    def _scan_out_of_spec(rows, holds, targets, universe=None,
                          cash_reserve_pct_by_version=None, **kw):
        spy.scan_calls.append({"rows": list(rows), "targets": dict(targets),
                               "universe": universe,
                               "reserves": dict(cash_reserve_pct_by_version or {})})
        verdicts = [{"account": str(r["account_id"]), "version": r["model"],
                     "advisor_name": None, "net_liq": 1000.0, "managed_net_liq": 1000.0,
                     "held_aside_value": 0.0, "out_of_spec": False, "n_legs": 0,
                     "n_held_aside": 0, "n_unclassified": 0, "blocked": False}
                    for r in rows]
        return {"verdicts": verdicts, "skipped": [], "excluded": [],
                "n_accounts": len(verdicts), "n_out_of_spec": 0,
                "n_in_spec": len(verdicts), "n_excluded": 0, "n_with_held_aside": 0,
                "held_aside_value": 0.0, "n_blocked": 0}

    crm_outofspec = types.ModuleType("crm_outofspec")
    crm_outofspec.scan_out_of_spec = _scan_out_of_spec

    sp = types.ModuleType("s0_live_pilot_run")
    sp._strategy_universe = lambda: {"SPY"}

    # THE ADVISOR WALL — paperbot/roster.py, the same allow-list the execution rail gates on.
    def _enrolled_roster_scan():
        spy.asked_the_advisor_wall = True
        return {"accounts": sorted(book), "held": [], "unfunded": [], "models": [],
                "scope": [], "source": book_source}

    roster_mod = types.ModuleType("roster")
    roster_mod.enrolled_roster_scan = _enrolled_roster_scan

    for name, mod in (("crm_roster", crm_roster),
                      ("batch_rebalance_execute", bre),
                      ("crm_outofspec", crm_outofspec),
                      ("s0_live_pilot_run", sp),
                      ("roster", roster_mod)):
        monkeypatch.setitem(sys.modules, name, mod)
    return spy


def _row(account_id, model, value=100_000.0):
    return {"account_id": account_id, "model": model, "total_value": value}


# --------------------------------------------------------------------------- #
# TASK 1 — the models the book actually runs are SCANNED, not silently skipped.
# --------------------------------------------------------------------------- #
def test_run_scan_actually_scans_custom_model_accounts(monkeypatch):
    """The regression this whole fix exists for. Andrew-authored "(Custom)" models used to
    resolve to nothing, so every account on one was dropped and the book scanned zero."""
    roster = [_row("U1", "Growth (Custom)"), _row("U2", "Growth (Custom)"),
              _row("U3", "Starter (Custom)")]
    resolved = {"U1": "Growth (Custom)", "U2": "Growth (Custom)", "U3": "Starter (Custom)"}
    spy = _install_fake_rail(monkeypatch, roster=roster, resolved=resolved,
                             buildable={"Growth (Custom)", "Starter (Custom)"})

    scan = job.run_scan()

    assert scan["n_accounts"] == 3            # every custom-model account was checked
    assert scan["n_roster"] == 3
    assert scan["n_unmonitored"] == 0
    assert scan["unresolved_models"] == []
    # The engine was handed the CUSTOM label's own Target — proof the custom model was built,
    # not skipped and not quietly swapped for a Strategy 0 one.
    scanned_labels = sorted(k for call in spy.scan_calls for k in call["targets"])
    assert scanned_labels == ["Growth (Custom)", "Starter (Custom)"]


def test_run_scan_uses_the_rails_resolution_not_the_stored_label(monkeypatch):
    """The account is judged against the model the RAIL says it would actually be traded to
    (the executor re-checks the size ladder at the moment of use), not the label stored on the
    roster row. If these two ever disagree, the monitor must follow the rail."""
    roster = [_row("U1", "Growth (Custom)", value=2_000.0)]
    spy = _install_fake_rail(
        monkeypatch, roster=roster,
        resolved={"U1": "Starter (Custom)"},          # rail re-tiers it by account value
        buildable={"Growth (Custom)", "Starter (Custom)"})

    scan = job.run_scan()

    assert scan["n_accounts"] == 1
    assert list(spy.scan_calls[0]["targets"]) == ["Starter (Custom)"]
    assert spy.scan_calls[0]["rows"][0]["model"] == "Starter (Custom)"
    # The per-model cash reserve came from the rail's own helper, not from a number here.
    assert spy.scan_calls[0]["reserves"] == {"Starter (Custom)": 0.01}


def test_run_scan_keeps_going_past_one_unbuildable_model(monkeypatch):
    """The rail's build_targets fails CLOSED on the first bad label (an executor must refuse
    the whole batch). A monitor must instead check everything checkable AND report the rest."""
    roster = [_row("U1", "Growth (Custom)"), _row("U2", "Ted", value=50_000.0),
              _row("U3", "Ted", value=25_000.0)]
    resolved = {"U1": "Growth (Custom)", "U2": "Ted", "U3": "Ted"}
    _install_fake_rail(monkeypatch, roster=roster, resolved=resolved,
                       buildable={"Growth (Custom)"})

    scan = job.run_scan()

    assert scan["n_accounts"] == 1                  # the checkable one WAS checked
    assert scan["n_unmonitored"] == 2               # and the other two are counted as unchecked
    assert scan["unmonitored_value"] == 75_000.0
    assert [e["model"] for e in scan["unresolved_models"]] == ["Ted"]
    assert scan["unresolved_models"][0]["n_accounts"] == 2


def test_run_scan_never_defaults_an_account_with_no_model(monkeypatch):
    """An account with no model recorded must be reported as unmonitored — never silently
    judged against the desk's configured default model."""
    roster = [_row("U1", "Growth (Custom)"), _row("U2", ""), _row("U3", None)]
    _install_fake_rail(monkeypatch, roster=roster, resolved={"U1": "Growth (Custom)"},
                       buildable={"Growth (Custom)"})

    scan = job.run_scan()

    assert scan["n_accounts"] == 1
    assert scan["n_unmonitored"] == 2
    assert any("no model" in e["model"].lower() for e in scan["unresolved_models"])


# --------------------------------------------------------------------------- #
# WHOSE BOOK — another advisor's clients are not Andrew's to watch, and are not
# a hole in Andrew's coverage.
# --------------------------------------------------------------------------- #
def test_run_scan_ignores_other_advisors_accounts_entirely(monkeypatch):
    """The false alarm this fix exists for. The roster VIEW carries all three advisors' books,
    so reading it raw made Ted's and Doug's clients look like accounts nobody was checking for
    drift. They are neither scanned nor counted as unmonitored: they were never this desk's
    accounts to check."""
    roster = [_row("U1", "Growth (Custom)"),
              _row("UTED", "Ted", value=6_474_735.0),      # Theodore Perez III's client
              _row("UDOUG", "Doug", value=2_517_849.0)]    # Douglas Garrett's client
    spy = _install_fake_rail(
        monkeypatch, roster=roster, resolved={"U1": "Growth (Custom)"},
        buildable={"Growth (Custom)"},
        book=["U1"])                                        # the advisor wall: Andrew's book

    scan = job.run_scan()

    assert spy.asked_the_advisor_wall                        # scoped through roster.py, not here
    assert scan["n_accounts"] == 1
    assert scan["n_roster"] == 1                # the total compares against ANDREW'S book only
    assert scan["n_unmonitored"] == 0           # no coverage gap...
    assert scan["unresolved_models"] == []      # ...and no complaint about "Ted"/"Doug"
    assert scan["unmonitored_value"] == 0.0
    # The other advisors' accounts never reached the engine at all.
    scanned = [str(r["account_id"]) for call in spy.scan_calls for r in call["rows"]]
    assert scanned == ["U1"]
    assert spy.resolved_for == ["U1"]


def test_main_stays_quiet_when_only_other_advisors_models_are_unbuildable(crm, monkeypatch,
                                                                          capsys):
    """End to end: a book that is fully checked once the other advisors' clients are out of
    scope posts NOTHING and exits 0 — no "115 of 301 accounts were not checked" alert."""
    roster = [_row("U1", "Growth (Custom)"), _row("UTED", "Ted"), _row("UDOUG", "Doug")]
    _install_fake_rail(monkeypatch, roster=roster, resolved={"U1": "Growth (Custom)"},
                       buildable={"Growth (Custom)"}, book=["U1"])

    assert job.main([]) == 0
    assert crm == []                                    # no false coverage alarm was filed
    assert "were not checked" not in capsys.readouterr().out


def test_run_scan_still_reports_andrews_own_uncheckable_account(monkeypatch):
    """The loud failure must NOT be weakened. An account that is genuinely Andrew's and
    genuinely uncheckable is still counted as unmonitored, even while another advisor's
    equally-unbuildable model is correctly ignored."""
    roster = [_row("U1", "Growth (Custom)"),
              _row("U2", "Mystery Model", value=250_000.0),   # Andrew's, unbuildable
              _row("UTED", "Ted", value=6_474_735.0)]         # not Andrew's
    _install_fake_rail(
        monkeypatch, roster=roster,
        resolved={"U1": "Growth (Custom)", "U2": "Mystery Model"},
        buildable={"Growth (Custom)"}, book=["U1", "U2"])

    scan = job.run_scan()

    assert scan["n_accounts"] == 1
    assert scan["n_roster"] == 2
    assert scan["n_unmonitored"] == 1
    assert scan["unmonitored_value"] == 250_000.0
    assert [e["model"] for e in scan["unresolved_models"]] == ["Mystery Model"]
    assert "Ted" not in scan["bad_versions"]


def test_main_still_fails_loudly_for_andrews_own_uncheckable_account(crm, monkeypatch, capsys):
    """...and end to end that is still an error alert and a non-zero exit."""
    roster = [_row("U1", "Growth (Custom)"), _row("U2", "Mystery Model", value=250_000.0),
              _row("UTED", "Ted")]
    _install_fake_rail(
        monkeypatch, roster=roster,
        resolved={"U1": "Growth (Custom)", "U2": "Mystery Model"},
        buildable={"Growth (Custom)"}, book=["U1", "U2"])

    rc = job.main([])

    assert rc == 2                                   # a scheduled task cannot show success
    assert len(crm) == 1
    assert crm[0]["dedup_key"] == "outofspec_unmonitored"
    assert crm[0]["severity"] == "error"
    assert "1 of 2 accounts were not checked for drift" in crm[0]["title"]
    assert "Mystery Model" in crm[0]["description"]
    assert "Ted" not in crm[0]["description"]


def test_run_scan_refuses_the_degraded_fallback_roster(monkeypatch):
    """If the advisor wall answers from the local fallback list instead of the client system,
    it carries no advisor information — so this job cannot tell whose accounts it is looking
    at. It refuses and says so, rather than reporting on the wrong set of accounts."""
    roster = [_row("U1", "Growth (Custom)")]
    _install_fake_rail(monkeypatch, roster=roster, resolved={"U1": "Growth (Custom)"},
                       buildable={"Growth (Custom)"}, book=["U1"], book_source="config")

    scan = job.run_scan()

    assert "fallback" in scan["error"]
    assert "whose accounts" in scan["error"]


# --------------------------------------------------------------------------- #
# TASK 2 — a skip is LOUD. It can never look like "all in spec", and it can
# never exit as success.
# --------------------------------------------------------------------------- #
def _scan_with_gap(n_scanned=186, n_roster=301, n_oos=0, unresolved=None):
    unresolved = unresolved if unresolved is not None else [
        {"model": "Ted", "reason": "unknown client version: Ted",
         "n_accounts": 98, "total_value": 6_474_735.0},
        {"model": "Doug", "reason": "unknown client version: Doug",
         "n_accounts": 17, "total_value": 2_517_849.0}]
    return {"verdicts": [], "skipped": [], "excluded": [], "n_accounts": n_scanned,
            "n_out_of_spec": n_oos, "n_in_spec": n_scanned - n_oos,
            "unresolved_models": unresolved,
            "n_unmonitored": sum(e["n_accounts"] for e in unresolved),
            "unmonitored_value": sum(e["total_value"] for e in unresolved),
            "n_roster": n_roster}


def test_main_raises_a_loud_alert_when_models_could_not_be_checked(crm, monkeypatch, capsys):
    monkeypatch.setattr(job, "run_scan", lambda: _scan_with_gap())

    rc = job.main([])

    assert rc == 2                                   # NOT success — the book was not covered
    assert len(crm) == 1
    alert = crm[0]
    assert alert["dedup_key"] == "outofspec_unmonitored"
    assert alert["severity"] == "error"
    assert "115 of 301 accounts were not checked for drift" in alert["title"]
    assert "Ted" in alert["description"] and "Doug" in alert["description"]
    assert "98 accounts" in alert["description"]
    # And the run must not read as a clean bill of health anywhere.
    out = capsys.readouterr().out
    assert "were not checked" in out
    assert "all in spec" not in out.lower()


def test_main_treats_zero_accounts_scanned_as_a_failed_run(crm, monkeypatch, capsys):
    """THE original defect, pinned. Scanning nothing printed "all 0 scanned accounts are in
    spec" and exited 0, so the scheduled task showed a green tick over an unmonitored book."""
    monkeypatch.setattr(job, "run_scan", lambda: _scan_with_gap(
        n_scanned=0, n_roster=301,
        unresolved=[{"model": "Growth (Custom)", "reason": "unknown client version",
                     "n_accounts": 301, "total_value": 37_000_000.0}]))

    rc = job.main([])

    assert rc != 0                                   # a scheduled task cannot show success
    assert len(crm) == 1
    assert crm[0]["dedup_key"] == "outofspec_unmonitored"
    assert "Nobody is checking any of the 301 accounts" in crm[0]["title"]
    out = capsys.readouterr().out
    assert "covered NONE" in out
    # The exact false all-clear the old code printed must never appear again, and any mention
    # of "in spec" must be a DENIAL of one ("no account was found to be in spec, because...").
    assert "0 scanned accounts are in spec" not in out
    assert "all 0" not in out.lower()
    assert "failed run" in out.lower()
    assert "no account was found to be in spec, because no account was checked" in out


def test_main_reports_the_gap_alongside_real_drift(crm, monkeypatch, capsys):
    """A partly-covered book with real drift raises BOTH alerts, under separate keys, so
    closing one in the client system cannot hide the other."""
    scan = _scan(n_oos=2, n_acct=186)
    scan.update({k: v for k, v in _scan_with_gap().items()
                 if k in ("unresolved_models", "n_unmonitored", "unmonitored_value",
                          "n_roster")})
    monkeypatch.setattr(job, "run_scan", lambda: scan)

    assert job.main([]) == 2
    assert sorted(r["dedup_key"] for r in crm) == ["outofspec_open", "outofspec_unmonitored"]


def test_main_does_not_repost_a_coverage_alert_already_open(crm, monkeypatch, capsys):
    monkeypatch.setattr(job, "run_scan", lambda: _scan_with_gap())

    assert job.main([]) == 2
    assert len(crm) == 1
    assert job.main([]) == 2                 # still a failure, but no duplicate alert
    assert len(crm) == 1
    assert "already open" in capsys.readouterr().out.lower()


def test_main_dry_run_never_posts_the_coverage_alert(crm, monkeypatch, capsys):
    monkeypatch.setattr(job, "run_scan", lambda: _scan_with_gap())
    assert job.main(["--dry-run"]) == 2
    assert crm == []
    assert "WOULD post" in capsys.readouterr().out


def test_coverage_notice_is_plain_english(monkeypatch):
    title, body, hint, detail = job.build_coverage_notice(_scan_with_gap())
    assert "not checked for drift" in title
    assert "nobody looked at them" in body
    assert "$8,992,584" in body                       # the money that is going unwatched
    assert [d["model"] for d in detail] == ["Ted", "Doug"]
    assert "Nothing was traded" in hint
    # No shorthand or jargon in anything the operator reads.
    for text in (title, body, hint):
        for banned in ("NAV", "OOS", "CRM", "n_accounts", "dedup"):
            assert banned not in text


def test_build_detail_only_out_of_spec_rows():
    detail = job.build_detail(_scan())
    accts = [r["account"] for r in detail]
    assert accts == ["U1", "U2"]           # in-spec U3 excluded
    assert detail[0]["n_held_aside"] == 1                 # holds a bond
    assert detail[0]["held_aside_value"] == 100_000.0
    assert detail[0]["managed_net_liq"] == 900_000.0      # what the model actually manages
    assert detail[1]["n_held_aside"] == 0
    assert detail[0]["net_liq"] == 1_000_000.0
    assert detail[0]["held_back"] is False


def test_build_notice_plain_english_and_counts():
    title, body, hint, detail = job.build_notice(_scan(n_oos=2, n_acct=10))
    assert title == "2 of 10 accounts out of spec — rebalance needed"
    assert "out of spec" in body.lower()
    # Held-aside holdings are explained as never-traded, NOT as a pending manual sale.
    assert "never trades" in body.lower()
    assert "outside the model allocation" in body.lower()
    assert "liquidation" not in body.lower()
    assert "Trade Execution" in hint
    assert len(detail) == 2


def test_build_notice_flags_unidentified_and_held_back_accounts():
    scan = _scan()
    scan["verdicts"][0]["n_unclassified"] = 1
    scan["verdicts"][1].update({"n_held_aside": 1, "blocked": True})
    _, body, _, detail = job.build_notice(scan)
    assert "could not identify" in body.lower()
    assert "held back" in body.lower()
    assert detail[1]["held_back"] is True


def test_main_skips_repost_while_an_alert_is_already_open(crm, monkeypatch, capsys):
    """The poster-side skip that silences the daily re-nag: the job skips because an OPEN alert
    is already sitting in the CRM, and it stays skipped until Andrew closes that task. The job
    reads that from post_notice's SKIPPED answer and never claims a snooze nobody set."""
    monkeypatch.setattr(job, "run_scan", lambda: _scan())

    # first run posts ONE consolidated alert
    assert job.main([]) == 0
    assert len(crm) == 1
    assert crm[0]["category"] == "outofspec"
    assert crm[0]["dedup_key"] == "outofspec_open"

    # next scheduled run must SKIP posting while that alert is still open
    assert job.main([]) == 0
    out = capsys.readouterr().out.lower()
    assert "already open" in out
    assert "snoozed" not in out
    assert len(crm) == 1                       # untouched, not duplicated

    # once Andrew closes it in the CRM, the next run may raise it again
    crm[0]["status"] = "done"
    assert job.main([]) == 0
    assert len(crm) == 2


def test_main_reports_failure_when_the_alert_cannot_be_filed(crm_write_broken, monkeypatch,
                                                             capsys):
    """An outage on the reporting channel is a FAILED run, not a quiet one. It must never
    exit 0, and must never claim the operator snoozed anything."""
    monkeypatch.setattr(job, "run_scan", lambda: _scan())

    rc = job.main([])

    assert rc != 0
    assert crm_write_broken == []                       # nothing was filed
    assert "snoozed" not in capsys.readouterr().out.lower()


def test_main_posts_nothing_when_all_in_spec(crm, monkeypatch, capsys):
    monkeypatch.setattr(job, "run_scan",
                        lambda: {"verdicts": [], "skipped": [], "n_accounts": 5,
                                 "n_out_of_spec": 0, "n_in_spec": 5, "bad_versions": []})
    assert job.main([]) == 0
    assert crm == []


def test_main_dry_run_posts_nothing(crm, monkeypatch, capsys):
    monkeypatch.setattr(job, "run_scan", lambda: _scan())
    assert job.main(["--dry-run"]) == 0
    assert crm == []
    assert "WOULD post" in capsys.readouterr().out
