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

Cadence: run on a schedule (e.g. daily, after the close). With nothing out of spec it posts
nothing. It de-duplicates by an OPEN-notice key (`outofspec_open`), so repeated runs while the
same accounts sit out of spec keep ONE current notice (with fresh numbers), not a growing pile.

SNOOZE / IGNORE-FOR-N-DAYS
--------------------------
If the operator snoozed this notice in the Action Center, the job SKIPS posting while the
snooze is live (via action_center.is_snoozed). Dismiss alone does not durably suppress — the
poster-side snooze skip is what silences the daily re-nag.

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


def _log(msg: str) -> None:
    try:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Read-only whole-book scan (mirrors page_control_plane._scan_whole_book, minus
# Streamlit — same pure engine, same read-only CRM path).
# --------------------------------------------------------------------------- #
def run_scan() -> dict:
    """Read the whole blessed roster + latest holdings from the CRM (read-only role) and run
    the frozen engine for every account's in-spec / out-of-spec verdict.

    Returns the crm_outofspec.scan_out_of_spec dict, or {"error": ...} if the CRM is not
    configured/reachable. Builds and transmits NOTHING (no `ib`, armed=False)."""
    import crm_roster
    import crm_outofspec
    import strategy_target

    if not crm_roster.is_configured():
        return {"error": "not_configured"}
    try:
        rows = crm_roster.fetch_roster(advisor_name=None)  # whole book
        holdings = crm_roster.fetch_holdings_latest([r["account_id"] for r in rows])
    except crm_roster.CrmRosterUnavailable as exc:
        return {"error": str(exc)}

    # One frozen target per DISTINCT model present; drop rows whose model has no target.
    versions = sorted({(r.get("model") or "") for r in rows if r.get("model")})
    targets: dict = {}
    bad_versions: list[str] = []
    for v in versions:
        try:
            targets[v] = strategy_target.current_target(version=v)
        except Exception as exc:  # noqa: BLE001 — a model with no validated engine is skipped
            bad_versions.append(f"{v} ({exc})")
    rows = [r for r in rows if (r.get("model") or "") in targets]

    scan = crm_outofspec.scan_out_of_spec(rows, holdings, targets)
    scan["bad_versions"] = bad_versions
    return scan


# --------------------------------------------------------------------------- #
# Notice text + structured detail (plain English; per the dashboard-labels standard).
# --------------------------------------------------------------------------- #
def build_detail(scan: dict) -> list[dict]:
    """Per-account rows for the OUT-OF-SPEC accounts — the expandable table detail. Plain
    fields: account, model, NAV, out_of_spec, would-trade legs, bond/manual flag."""
    detail = []
    for v in scan.get("verdicts", []):
        if not v.get("out_of_spec"):
            continue
        n_bonds = int(v.get("n_bonds", 0) or 0)
        detail.append({
            "account": v.get("account"),
            "model": v.get("version"),
            "advisor": v.get("advisor_name") or "— unassigned —",
            "net_liq": float(v.get("net_liq", 0.0) or 0.0),
            "out_of_spec": True,
            "n_legs": int(v.get("n_legs", 0) or 0),
            "n_bonds": n_bonds,
            "manual_bond_liquidation": n_bonds > 0,
        })
    return detail


def build_notice(scan: dict) -> tuple[str, str, str, list[dict]]:
    """(title, body, action_hint, detail_list) for a scan with >=1 out-of-spec account."""
    n_oos = int(scan.get("n_out_of_spec", 0) or 0)
    n_acct = int(scan.get("n_accounts", 0) or 0)
    detail = build_detail(scan)
    n_manual = sum(1 for r in detail if r["manual_bond_liquidation"])
    acct_word = "account is" if n_oos == 1 else "accounts are"
    title = f"{n_oos} of {n_acct} accounts out of spec — rebalance needed"
    body = (
        f"{n_oos} of {n_acct} blessed accounts {acct_word} out of spec against the desk's "
        f"frozen model and would trade to conform. Expand the detail below to see which "
        f"accounts, their model, and account value."
    )
    if n_manual:
        body += (
            f" {n_manual} of them hold an individual bond that needs manual liquidation (the "
            f"automatic rebalance can't route a bond)."
        )
    hint = (
        "Open the Control Plane -> Whole-book out-of-spec read to review these accounts and, "
        "behind the review -> arm -> transmit gate, pull them into one batch rebalance. "
        "Nothing trades until you arm it there — this is only a heads-up."
    )
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
        return 1
    if scan.get("error"):
        _log(f"could not read the CRM roster (read-only): {scan['error']}; posting nothing.")
        return 1

    n_oos = int(scan.get("n_out_of_spec", 0) or 0)
    n_acct = int(scan.get("n_accounts", 0) or 0)
    bad = scan.get("bad_versions") or []
    if bad:
        _log(f"models with no validated engine (skipped): {bad}")
    _log(f"whole-book scan: {n_acct} accounts scanned, {n_oos} out of spec.")

    if n_oos <= 0:
        print(f"No out-of-spec notice: all {n_acct} scanned accounts are in spec.")
        return 0

    title, body, hint, detail = build_notice(scan)
    if args.dry_run:
        print("[dry-run] WOULD post an Action Center notice (posting nothing):")
        print(f"  title:  {title}")
        print(f"  body:   {body}")
        print(f"  hint:   {hint}")
        print(f"  detail: {len(detail)} out-of-spec account rows")
        return 0

    import action_center
    # Snooze / "ignore for N days": SKIP posting while the operator has this notice snoozed.
    # The poster-side is_snoozed skip is what actually silences the daily nag (dismiss alone
    # re-posts on the next run).
    if action_center.is_snoozed(_DEDUP_KEY):
        print("Out-of-spec notice is snoozed (ignored) by the operator; posting nothing.")
        return 0
    key = action_center.post_notice(
        kind="outofspec", title=title, body=body, severity="warn",
        action_hint=hint, dedup_key=_DEDUP_KEY, detail_json=detail)
    if key:
        print(f"Posted consolidated out-of-spec proposal to the Action Center (notice {key}).")
        return 0
    _log("posting the Action Center notice failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
