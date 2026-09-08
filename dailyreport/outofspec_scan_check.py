"""
outofspec_scan_check.py — the whole-book "N accounts out of spec — rebalance needed" job.

WHAT IT IS
----------
Under the propose-and-arm posture, the desk surfaces ONE consolidated Action Center notice
when blessed CRM roster accounts have drifted out of spec against the desk's frozen model —
NOT one item per account. This job runs the read-only whole-book out-of-spec scan (the same
pure `rebalance_engine.build_plan` the Control Plane's whole-book panel uses — no broker,
armed=False, so it builds and transmits nothing), and if any account is out of spec it posts
ONE deduped notice carrying a per-account detail list the page can expand into a table. It
NEVER trades: the notice only points the operator at the Control Plane, where pulling the
accounts into a batch rebalance still requires the deliberate review -> arm -> transmit gate.

Cadence: run on a schedule (e.g. daily, after the close). With the whole book checked and
nothing out of spec it posts nothing. It de-duplicates by an OPEN-notice key (`outofspec_open`),
so repeated runs while the same accounts sit out of spec keep ONE current notice (with fresh
numbers), not a growing pile.

A SILENT SKIP IS THE WORST FAILURE THIS JOB CAN HAVE (fixed 2026-09-08)
----------------------------------------------------------------------
This job used to resolve each model with `strategy_target.current_target`, which knows only
Strategy 0's computed client versions. The book runs on allocations Andrew published in the
client system, so EVERY model failed to resolve, EVERY account was dropped, and the run printed
"all 0 scanned accounts are in spec" and exited 0. Roughly 301 accounts had no drift monitoring
at all and the silence read as health.

Two things changed. Model resolution is now the TRADING RAIL'S OWN (see `run_scan`), so the
scanner checks an account against the model the desk would actually trade it to. And a model
that still cannot be resolved is now LOUD: the accounts under it are counted as unchecked, a
separate Action Center alert (`outofspec_unmonitored`) names the models and the account counts,
and the job exits 2 so the scheduled task cannot show success over an unchecked book. Scanning
zero accounts is reported as a failed run, never as a clean one.

Exit codes: 0 the whole book was checked; 1 the job could not run at all (client system not
configured or unreachable); 2 the job ran but some or all of the book went unchecked.

NOT POSTING TWICE — ONE CHECK, IN ONE PLACE
-------------------------------------------
``action_center.post_notice`` already refuses to file a second alert while one with the same
key is open, and says which of the three things it did: the new task's id when it posted,
SKIPPED when it deliberately posted nothing, FAILED when it could not post. This job reads
that answer and nothing else. It used to ALSO ask ``is_snoozed`` first and return early — a
duplicate of the same check that turned a CRM outage into a clean exit 0 claiming the operator
had snoozed the notice. Removed 2026-09-08: an outage is a failure, and it is reported as one.

SCOPE / SAFETY — INFORMATIONAL + READ-ONLY, ZERO-TRANSMIT
--------------------------------------------------------
Reads the live CRM through the read-only `tradingdesk_readonly` Postgres role (the
TRADINGDESK_CRM_DSN env var; a scheduled task running as the user inherits that User env var —
no injection needed). Runs the UNCHANGED pure engine with no `ib`. Contacts NO broker, builds
no order object, transmits nothing. Not order-affecting: no paperbot version bump.

USAGE
-----
    <venv python> outofspec_scan_check.py            # real run (task uses this)
    <venv python> outofspec_scan_check.py --dry-run  # scan + print the notice, post NOTHING
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _p in (str(_HERE), str(_REPO / "connections"), str(_REPO / "paperbot"),
           str(_REPO / "dashboard" / "desk")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_DEDUP_KEY = "outofspec_open"
# The SECOND alert this job can raise: part (or all) of the book could not be checked at all.
# Deliberately its own de-duplication key, so closing the drift alert in the CRM never hides a
# coverage gap and closing the coverage alert never hides real drift.
_COVERAGE_DEDUP_KEY = "outofspec_unmonitored"

# Exit codes the scheduled task reports. Zero is reserved for "the whole book was actually
# checked" — scanning nothing, or scanning only part of the book, must NEVER look like success.
_EXIT_OK = 0
_EXIT_COULD_NOT_RUN = 1
_EXIT_BOOK_NOT_FULLY_CHECKED = 2


def _log(msg: str) -> None:
    try:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# MODEL RESOLUTION — the TRADING RAIL'S OWN, called and never re-implemented.
# --------------------------------------------------------------------------- #
def _build_targets_by_source(labels: list[str]) -> tuple[dict, dict, list[dict]]:
    """``(targets, metas, unresolved)`` for the distinct model labels on the roster, built by
    ``batch_rebalance_execute.build_targets`` — the SAME SOURCE-based dispatch the batch and
    group execution rails use.

    WHY THIS WRAPPER EXISTS AT ALL. ``build_targets`` deliberately FAILS CLOSED: the first
    label it cannot build raises ``TargetBuildFailed`` naming that label, because an executor
    that cannot price one model must refuse the whole batch rather than trade part of it. A
    MONITORING job wants the opposite shape of answer — check everything checkable AND report
    precisely what it could not check — so this peels the failing label off and asks the rail
    again for the rest. Every target that comes back was still built by the rail, from the
    rail's own sources: nothing here knows what a model holds, and there is no model-name
    table, ticker or percentage anywhere in this file. If the rail would not trade a label,
    this scanner cannot check it either, and says so out loud.

    ``unresolved`` is one row per label that could not be built, carrying the rail's own
    reason text. PURE-ish: reads the CRM allocation view and local strategy data through the
    rail; contacts no broker and builds no order."""
    import batch_rebalance_execute as bre

    remaining = [str(v) for v in labels if str(v).strip()]
    targets: dict = {}
    metas: dict = {}
    unresolved: list[dict] = []
    # Bounded: every pass either succeeds or removes at least one label from `remaining`.
    for _ in range(len(remaining) + 1):
        if not remaining:
            break
        try:
            targets, metas = bre.build_targets(remaining)
            break
        except bre.TargetBuildFailed as exc:
            # `label` is the failing model. custom_target can raise for a batch, in which case
            # the rail joins the labels it was asked about — handle both shapes.
            failed = [p.strip() for p in str(exc.label).split(",") if p.strip()]
            failed = [f for f in failed if f in remaining] or list(remaining)
            for f in failed:
                unresolved.append({"model": f, "reason": str(exc.cause or exc)})
            remaining = [v for v in remaining if v not in failed]
        except Exception as exc:  # noqa: BLE001 — never let a model problem kill the scan
            for f in remaining:
                unresolved.append({"model": f, "reason": str(exc)})
            remaining = []
    return targets, metas, unresolved


def _merge_scans(parts: list[dict]) -> dict:
    """Combine the per-model ``crm_outofspec.scan_out_of_spec`` results into one whole-book
    result with the same shape. Pure bookkeeping — it re-counts, it never re-decides anything
    about an account. Sorted the way ``crm_outofspec`` sorts: out-of-spec first, then largest
    account value first."""
    merged: dict = {"verdicts": [], "skipped": [], "excluded": []}
    for part in parts:
        for key in ("verdicts", "skipped", "excluded"):
            merged[key].extend(part.get(key) or [])
    merged["verdicts"].sort(key=lambda v: (not v["out_of_spec"], -v["net_liq"]))
    n_oos = sum(1 for v in merged["verdicts"] if v["out_of_spec"])
    merged["n_accounts"] = len(merged["verdicts"])
    merged["n_out_of_spec"] = n_oos
    merged["n_in_spec"] = len(merged["verdicts"]) - n_oos
    merged["n_excluded"] = len(merged["excluded"])
    merged["n_with_held_aside"] = sum(1 for v in merged["verdicts"] if v["n_held_aside"])
    merged["held_aside_value"] = sum(v["held_aside_value"] for v in merged["verdicts"])
    merged["n_blocked"] = sum(1 for v in merged["verdicts"] if v["blocked"])
    return merged


# --------------------------------------------------------------------------- #
# Read-only whole-book scan (mirrors page_control_plane._scan_whole_book, minus
# Streamlit — same pure engine, same read-only CRM path).
# --------------------------------------------------------------------------- #
def run_scan() -> dict:
    """Read ANDREW'S blessed roster + latest holdings from the CRM (read-only role) and run the
    frozen engine for every account's in-spec / out-of-spec verdict.

    WHOSE BOOK (fixed 2026-09-08). "Whole book" here means the whole of ANDREW'S book, never
    the whole view. ``v_tradingdesk_roster`` carries Ted's and Doug's clients too, and this job
    used to read it raw and then report their 115 accounts as accounts nobody was checking for
    drift — a false alarm about other advisors' clients. Scoping now goes through
    ``roster.enrolled_roster_scan``, the same advisor wall the execution rail gates on.

    MODEL RESOLUTION IS THE TRADING RAIL'S, NOT THIS FILE'S (fixed 2026-09-08). This job used
    to ask ``strategy_target.current_target`` directly, which only knows Strategy 0's computed
    client versions. Every model the book actually runs is an allocation Andrew published in
    the client system, so EVERY label failed, EVERY account was silently dropped, and the job
    reported "all in spec" over an empty scan. It now walks the same path the executor walks:

      * ``batch_rebalance_execute.resolve_roster_versions`` — account -> its effective model
        label, including the size-ladder re-check the executor applies at the moment of use,
        so this readout judges an account against the model it would ACTUALLY be traded to.
      * ``batch_rebalance_execute.build_targets`` (via :func:`_build_targets_by_source`) —
        dispatch by the allocation's SOURCE, never by the label's spelling, so a published
        custom allocation is built from the client system's own rows and never falls through
        to the Strategy 0 engine.
      * ``batch_rebalance_execute.account_universe`` / ``account_reserve_pct`` — the tradeable
        set and the standing cash reserve for each model, from the rail's own helpers.

    That is the whole point: this scanner has no independent idea of what any model holds, so
    it cannot disagree with what the desk would actually trade.

    Returns the merged ``crm_outofspec.scan_out_of_spec`` dict plus the COVERAGE facts
    (``unresolved_models``, ``n_unmonitored``, ``unmonitored_value``, ``n_roster``), or
    {"error": ...} if the CRM is not configured/reachable. Builds and transmits NOTHING
    (no `ib`, armed=False)."""
    import crm_roster
    import crm_outofspec
    import batch_rebalance_execute as bre
    import roster as roster_mod

    if not crm_roster.is_configured():
        return {"error": "not_configured"}

    # WHOSE BOOK THIS JOB IS FOR — the desk's OWN advisor wall, not this file's idea of it.
    # ``v_tradingdesk_roster`` deliberately carries all three advisors' books, and reading it
    # raw is how this job came to treat Ted's 98 accounts and Doug's 17 as a monitoring gap in
    # Andrew's book. They are not: those clients are Ted's and Doug's to watch and rebalance,
    # and Doug is his own advisor. ``roster.enrolled_roster_scan`` is the same allow-list the
    # execution rail gates on (batch_rebalance_execute.main, group_execute.build_plans), so the
    # accounts this monitor reports on are exactly the accounts the desk would act on.
    try:
        book = roster_mod.enrolled_roster_scan()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not resolve whose accounts to check: {exc}"}
    # The wall's three parts ARE Andrew's book: in-scope-and-tradeable, plus the ones it held
    # back and says so by name. Drift monitoring is read-only, so a no-trade hold or a missing
    # funded reality is no reason to stop LOOKING at an account — those are reported by the
    # engine below as skipped/held out. What none of them may be is another advisor's client.
    my_book = set(book.get("accounts") or ()) | set(book.get("held") or ()) \
        | set(book.get("unfunded") or ())
    if book.get("source") != "crm":
        # The degraded config.ENROLLMENT fallback is a hardcoded allow-list with no advisor
        # information in it. Scoping a whole-book drift report to it would silently report on
        # the wrong set of accounts, so refuse and say why rather than answer from it.
        return {"error": "the blessed roster came from the local fallback list rather than "
                         "the client system, so this run cannot tell whose accounts these are"}

    try:
        rows = [r for r in crm_roster.fetch_roster(advisor_name=None)
                if crm_roster.account_identifier(r) in my_book]
        holdings = crm_roster.fetch_holdings_latest([r["account_id"] for r in rows])
    except crm_roster.CrmRosterUnavailable as exc:
        return {"error": str(exc)}

    # Every count this job reports — scanned, unchecked, the roster total the alert compares
    # against — is now over ANDREW'S book alone. Another advisor's account is not scanned and
    # is not a coverage gap, because it was never this desk's account to check.
    n_roster = len(rows)

    # An account with NO model recorded is unmonitored, not a default. It is held out of the
    # rail's resolution on purpose: resolve_roster_versions falls back to the desk's configured
    # default model for a label it cannot find, and silently judging a client's account against
    # a model nobody assigned it is exactly the kind of invented answer this job must not give.
    unassigned = [r for r in rows if not str(r.get("model") or "").strip()]
    rows = [r for r in rows if str(r.get("model") or "").strip()]

    # THE RAIL: account -> the model label the executor would actually size it against.
    accounts = [crm_roster.account_identifier(r) for r in rows]
    try:
        effective = bre.resolve_roster_versions(accounts)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not resolve which model each account runs: {exc}"}
    rows = [{**r, "model": effective.get(crm_roster.account_identifier(r), r.get("model"))}
            for r in rows]

    # THE RAIL: one Target per distinct label, dispatched by the allocation's SOURCE.
    labels = sorted({str(r.get("model") or "") for r in rows})
    targets, metas, unresolved = _build_targets_by_source(labels)

    # THE RAIL: Strategy 0's base tradeable universe, resolved the way the group rail resolves
    # it. Only a Strategy 0 label uses it (a custom allocation derives its own), but it is
    # looked up rather than guessed for the same reason the group rail looks it up.
    try:
        import s0_live_pilot_run as sp
        base_universe = sp._strategy_universe()
    except Exception as exc:  # noqa: BLE001 — informational; the engine falls back safely
        _log(f"could not resolve the Strategy 0 base universe ({exc}); continuing.")
        base_universe = None

    # Scan ONE MODEL AT A TIME, because the universe and the cash reserve are per-model facts
    # and the pure engine takes one of each per run. This is the same decomposition the
    # execution rail makes per account; the per-account results are then simply added up.
    by_label: dict[str, list] = {}
    for r in rows:
        by_label.setdefault(str(r.get("model") or ""), []).append(r)

    parts: list[dict] = []
    unmonitored_rows: list = []
    for label, label_rows in sorted(by_label.items()):
        if label not in targets:
            unmonitored_rows.extend(label_rows)
            continue
        meta = metas.get(label)
        # Symbols held anywhere in this model's accounts. The execution rail passes ONE
        # account's held symbols; a single pure-engine run covers the whole group, so this is
        # their union. The only thing the universe decides here is whether an off-model
        # holding reads as "rotate out of it" or as "leave it for a human", and a union can
        # only ever move a symbol toward BEING COUNTED as drift. For a read-only monitor that
        # is the safe direction: it can surface an account for review, never hide one.
        held_symbols: set[str] = set()
        for r in label_rows:
            for h in holdings.get(str(r.get("account_id")), []) or []:
                sym = str(h.get("symbol") or "").strip()
                if sym:
                    held_symbols.add(sym)
        parts.append(crm_outofspec.scan_out_of_spec(
            label_rows, holdings, {label: targets[label]},
            universe=bre.account_universe(targets[label], meta, held_symbols,
                                          base=base_universe),
            cash_reserve_pct_by_version={label: bre.account_reserve_pct(meta)}))

    scan = _merge_scans(parts)

    # COVERAGE — what this run did NOT check, and what that is worth. Counted here so the
    # caller can never mistake an empty scan for a clean book.
    def _value(row) -> float:
        try:
            return float(row.get("total_value") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    n_by_label: dict[str, int] = {}
    value_by_label: dict[str, float] = {}
    for r in unmonitored_rows:
        label = str(r.get("model") or "")
        n_by_label[label] = n_by_label.get(label, 0) + 1
        value_by_label[label] = value_by_label.get(label, 0.0) + _value(r)
    for entry in unresolved:
        entry["n_accounts"] = n_by_label.get(entry["model"], 0)
        entry["total_value"] = value_by_label.get(entry["model"], 0.0)
    if unassigned:
        unresolved.append({
            "model": "— no model recorded —",
            "reason": "no model is recorded for these accounts in the client system",
            "n_accounts": len(unassigned),
            "total_value": sum(_value(r) for r in unassigned)})
    unresolved.sort(key=lambda e: -int(e.get("n_accounts", 0)))

    scan["unresolved_models"] = unresolved
    scan["n_unmonitored"] = len(unmonitored_rows) + len(unassigned)
    scan["unmonitored_value"] = (sum(value_by_label.values())
                                 + sum(_value(r) for r in unassigned))
    scan["n_roster"] = n_roster
    # Kept under its old name for anything still reading it; same information, richer form.
    scan["bad_versions"] = [f"{e['model']} ({e['reason']})" for e in unresolved]
    return scan


# --------------------------------------------------------------------------- #
# Notice text + structured detail (plain English; per the dashboard-labels standard).
# --------------------------------------------------------------------------- #
def build_detail(scan: dict) -> list[dict]:
    """Per-account rows for the OUT-OF-SPEC accounts — the expandable table detail. Plain
    fields: account, model, account value, the value the model manages, would-trade legs,
    and the held-aside (never-traded) block.

    HELD ASIDE (2026-08-19): individual bonds — and anything else on the desk's no-trade
    list — are priced, counted and reported here, but they are NOT a defect and NOT a
    pending manual sale. They sit outside the model allocation, so the would-trade legs
    describe the MANAGED sleeve, which is what ``managed_net_liq`` separates out.
    ``held_back`` is the separate, genuine problem: the engine withheld this account's
    orders because a held-aside holding could not be priced."""
    detail = []
    for v in scan.get("verdicts", []):
        if not v.get("out_of_spec"):
            continue
        detail.append({
            "account": v.get("account"),
            "model": v.get("version"),
            "advisor": v.get("advisor_name") or "— unassigned —",
            "net_liq": float(v.get("net_liq", 0.0) or 0.0),
            "managed_net_liq": float(
                v.get("managed_net_liq", v.get("net_liq", 0.0)) or 0.0),
            "out_of_spec": True,
            "n_legs": int(v.get("n_legs", 0) or 0),
            "n_held_aside": int(v.get("n_held_aside", 0) or 0),
            "held_aside_value": float(v.get("held_aside_value", 0.0) or 0.0),
            "n_unclassified": int(v.get("n_unclassified", 0) or 0),
            "held_back": bool(v.get("blocked")),
        })
    return detail


def build_notice(scan: dict) -> tuple[str, str, str, list[dict]]:
    """(title, body, action_hint, detail_list) for a scan with >=1 out-of-spec account."""
    n_oos = int(scan.get("n_out_of_spec", 0) or 0)
    n_acct = int(scan.get("n_accounts", 0) or 0)
    detail = build_detail(scan)
    n_held_aside = sum(1 for r in detail if r["n_held_aside"])
    n_unclassified = sum(1 for r in detail if r["n_unclassified"])
    n_held_back = sum(1 for r in detail if r["held_back"])
    acct_word = "account is" if n_oos == 1 else "accounts are"
    title = f"{n_oos} of {n_acct} accounts out of spec — rebalance needed"
    body = (
        f"{n_oos} of {n_acct} blessed accounts {acct_word} out of spec against the desk's "
        f"frozen model and would trade to conform. Expand the detail below to see which "
        f"accounts, their model, and account value."
    )
    if n_held_aside:
        body += (
            f" {n_held_aside} of them also hold something the desk never trades (individual "
            f"bonds). Those holdings are priced and counted but sit outside the model "
            f"allocation, and the trades listed here rebalance only the part of the account "
            f"the model manages."
        )
    if n_unclassified:
        body += (
            f" {n_unclassified} of them hold something we could not identify. It is being "
            f"held aside and not traded until someone says what it is."
        )
    if n_held_back:
        body += (
            f" {n_held_back} of them had all trades held back because a holding we never "
            f"trade could not be priced, so the rest of the account cannot be sized safely. "
            f"That needs a look."
        )
    hint = (
        "Open the Trade Execution page on the desk dashboard to review these accounts and, "
        "behind the review -> arm -> transmit gate, pull them into one batch rebalance. "
        "Nothing trades until you arm it there — this is only a heads-up."
    )
    return title, body, hint, detail


# --------------------------------------------------------------------------- #
# THE LOUD HALF — a scan that could not cover the book is a PROBLEM, never a pass.
# --------------------------------------------------------------------------- #
def _money(value: float) -> str:
    """A dollar amount written out plainly, to the nearest dollar."""
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return "$0"


def coverage_gap(scan: dict) -> int:
    """How many roster accounts this run did NOT check for drift. Zero means the whole book
    was checked and the in-spec / out-of-spec answer can be trusted as complete."""
    return int(scan.get("n_unmonitored", 0) or 0)


def build_coverage_notice(scan: dict) -> tuple[str, str, str, list[dict]]:
    """(title, body, action_hint, detail_list) for the "part of the book went unchecked" alert.

    THIS IS THE POINT OF THE WHOLE JOB'S SECOND HALF. Before 2026-09-08 a model the scanner
    could not build a target for was dropped with one line on the error stream and nothing
    else — so a run that checked ZERO of 301 accounts printed "all 0 scanned accounts are in
    spec", exited successfully, and the scheduled task showed a green tick. Silence read as
    health while the entire book had no drift monitoring at all. An unchecked account is now
    reported as the problem it is, in an alert Andrew actually sees."""
    n_unmon = coverage_gap(scan)
    n_scanned = int(scan.get("n_accounts", 0) or 0)
    n_roster = int(scan.get("n_roster", n_scanned + n_unmon) or 0)
    unresolved = list(scan.get("unresolved_models") or [])
    value = float(scan.get("unmonitored_value", 0.0) or 0.0)

    if n_scanned <= 0:
        title = (f"Nobody is checking any of the {n_roster} accounts for drift — "
                 f"the nightly scan checked none of them")
        opening = (
            f"The nightly drift check ran but was unable to check a single one of the "
            f"{n_roster} accounts on the roster. This is a failure of the check itself, not a "
            f"clean result: no account was found to be in spec, because no account was looked "
            f"at. Treat the whole book as unmonitored until this is fixed.")
    else:
        title = (f"{n_unmon} of {n_roster} accounts were not checked for drift — "
                 f"the nightly scan covered only part of the book")
        opening = (
            f"The nightly drift check looked at {n_scanned} of the {n_roster} accounts on the "
            f"roster and could not look at the other {n_unmon}. For those {n_unmon} accounts "
            f"the desk cannot say whether they have drifted away from what they are supposed "
            f"to hold — so a quiet night from this check does not mean they are fine, it means "
            f"nobody looked at them.")

    body = opening
    if value > 0:
        body += (f" Those unchecked accounts are worth {_money(value)} between them.")
    body += (
        " Each one is recorded under a model name that the desk could not turn into a target "
        "set of holdings, which is the same reason the trading side would refuse to rebalance "
        "them. The model names, and how many accounts sit under each, are listed below.")

    detail: list[dict] = []
    lines: list[str] = []
    for entry in unresolved:
        model = str(entry.get("model", ""))
        count = int(entry.get("n_accounts", 0) or 0)
        acct_word = "account" if count == 1 else "accounts"
        lines.append(f"  {model} — {count} {acct_word}, "
                     f"{_money(entry.get('total_value', 0.0))} in total "
                     f"(the desk reported: {entry.get('reason', 'no reason given')})")
        detail.append({"model": model, "n_accounts": count,
                       "total_value": float(entry.get("total_value", 0.0) or 0.0),
                       "reason": str(entry.get("reason", ""))})
    if lines:
        body += "\n\nModels the desk could not read:\n" + "\n".join(lines)

    n_unfunded = len(scan.get("skipped") or [])
    n_held_out = len(scan.get("excluded") or [])
    if n_unfunded or n_held_out:
        body += (
            f"\n\nSeparately, {n_unfunded} accounts were passed over because they have no "
            f"money or no recent holdings record, and {n_held_out} were deliberately held out "
            f"because their recorded account value disagrees too much with the holdings on "
            f"file to size anything safely. Those are already-known conditions, listed here "
            f"only so the coverage figures above add up.")

    hint = (
        "Check what each of these model names is in the client system. A name that is really "
        "an advisor's name rather than a model, or a model whose allocation has never been "
        "published, will fail here every night and will keep those accounts unmonitored. "
        "Nothing was traded and nothing will trade because of this message — it only reports "
        "which accounts are going unwatched.")
    return title, body, hint, detail


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="run the read-only scan and print the notice, but post NOTHING to the "
                         "Action Center.")
    args = ap.parse_args(argv)

    scan = run_scan()
    if scan.get("error") == "not_configured":
        _log("CRM connection not configured (TRADINGDESK_CRM_DSN unset); posting nothing.")
        return _EXIT_COULD_NOT_RUN
    if scan.get("error"):
        _log(f"could not read the CRM roster (read-only): {scan['error']}; posting nothing.")
        return _EXIT_COULD_NOT_RUN

    n_oos = int(scan.get("n_out_of_spec", 0) or 0)
    n_acct = int(scan.get("n_accounts", 0) or 0)
    n_roster = int(scan.get("n_roster", n_acct) or 0)
    n_unmon = coverage_gap(scan)
    for entry in (scan.get("unresolved_models") or []):
        _log(f"model the desk could not read: {entry['model']} — "
             f"{entry.get('n_accounts', 0)} accounts left unchecked ({entry.get('reason')})")
    _log(f"whole-book scan: {n_acct} of {n_roster} roster accounts scanned, "
         f"{n_oos} out of spec, {n_unmon} NOT CHECKED AT ALL.")

    exit_code = _EXIT_OK

    # ------------------------------------------------------------------ #
    # 1. COVERAGE. An unchecked account is a problem in its own right, and it is reported
    #    BEFORE any in-spec claim, because "all in spec" over a partial book is a lie.
    # ------------------------------------------------------------------ #
    if n_unmon > 0 or n_acct <= 0:
        exit_code = _EXIT_BOOK_NOT_FULLY_CHECKED
        c_title, c_body, c_hint, c_detail = build_coverage_notice(scan)
        if args.dry_run:
            print("[dry-run] WOULD post an Action Center notice about the unchecked accounts "
                  "(posting nothing):")
            print(f"  title:  {c_title}")
            print(f"  body:   {c_body}")
            print(f"  hint:   {c_hint}")
        else:
            import action_center
            c_key = action_center.post_notice(
                kind="outofspec_coverage", title=c_title, body=c_body, severity="error",
                action_hint=c_hint, dedup_key=_COVERAGE_DEDUP_KEY, detail_json=c_detail)
            if c_key:
                print(f"Posted an alert to the Action Center: {n_unmon} of {n_roster} "
                      f"accounts were not checked for drift (notice {c_key}).")
            elif c_key == action_center.SKIPPED:
                print("Posting nothing more: an alert about accounts going unchecked is "
                      "already open in the Action Center, or the Action Center could not be "
                      "asked. The unchecked accounts above are still unchecked.")
            else:
                _log("posting the unchecked-accounts alert failed.")
                exit_code = _EXIT_COULD_NOT_RUN
        # Scanning NOTHING is a failure, full stop. There is no drift result to report.
        if n_acct <= 0:
            print(f"The nightly drift check covered NONE of the {n_roster} accounts on the "
                  f"roster. This is a failed run, not a clean one — no account was found to "
                  f"be in spec, because no account was checked.")
            return exit_code

    # ------------------------------------------------------------------ #
    # 2. DRIFT, over the accounts that were actually checked.
    # ------------------------------------------------------------------ #
    checked_phrase = (f"all {n_acct} accounts that were checked are in spec"
                      if n_unmon <= 0 else
                      f"the {n_acct} accounts that were checked are in spec, but "
                      f"{n_unmon} of the {n_roster} accounts on the roster were not checked "
                      f"at all — see the alert about that")
    if n_oos <= 0:
        print(f"No out-of-spec notice: {checked_phrase}.")
        return exit_code

    title, body, hint, detail = build_notice(scan)
    if args.dry_run:
        print("[dry-run] WOULD post an Action Center notice (posting nothing):")
        print(f"  title:  {title}")
        print(f"  body:   {body}")
        print(f"  hint:   {hint}")
        print(f"  detail: {len(detail)} out-of-spec account rows")
        return exit_code

    import action_center
    key = action_center.post_notice(
        kind="outofspec", title=title, body=body, severity="warn",
        action_hint=hint, dedup_key=_DEDUP_KEY, detail_json=detail)
    if key:
        print(f"Posted consolidated out-of-spec proposal to the Action Center (notice {key}).")
        return exit_code
    if key == action_center.SKIPPED:
        print("Posting nothing: an out-of-spec alert is already open in the Action Center, or "
              "the Action Center could not be asked.")
        return exit_code
    _log("posting the Action Center notice failed.")
    return _EXIT_COULD_NOT_RUN


if __name__ == "__main__":
    sys.exit(main())
