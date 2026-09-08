"""action_center.py — the desk's alert poster into the CRM's Action Center.

The desk's own Action Center PAGE was retired 2026-09-05 by owner decision: Andrew does not
monitor things on the trading desk — he goes there to trade, and warnings belong in the CRM,
whose Action Center he actually reads. This module used to keep its own SQLite notice store;
nothing displayed it any more, so every notice written here went nowhere. As of 2026-09-05 it
writes the alert straight into the CRM instead, as a row in ``public.tasks``.

Four nightly dailyreport jobs post through here, and their call sites are UNCHANGED:

    dailyreport/outofspec_scan_check.py                 (kind "outofspec")
    dailyreport/s0_cash_deploy_check.py                 (kind "cash_deploy")
    dailyreport/withdrawal_reserve_check.py             (kind "withdrawal_reserve")
    dailyreport/withdrawal_cash_raise_monthly_check.py  (kind "withdrawal_cash_raise_monthly")

CONNECTION: no new credential and no new connection code — this reuses the desk's existing
CRM helper ``paperbot/crm_roster.py`` and therefore the same ``TRADINGDESK_CRM_DSN``
environment variable already used to read the roster. That role (``tradingdesk_readonly``)
has been granted INSERT and SELECT on ``public.tasks``, restricted by row-level security to
rows whose ``source`` is the literal ``'trading_desk'``. It can neither update nor delete, and
cannot see client tasks. Nothing here is a write path to any trading store, strategy config,
or the gateway arming state — it files a note for a human and nothing else.

COLUMN MAPPING: title -> ``title``, body (plus any ``action_hint``) -> ``description``,
severity -> ``severity``, kind -> ``category``, dedup_key -> ``dedup_key``, plus the fixed
``source = 'trading_desk'`` (required by the RLS policy) and ``status = 'open'``.

DE-DUPLICATION is now the WHOLE story — the old dismiss/snooze/expiry machinery is gone.
Before inserting, this checks for an existing OPEN trading-desk task with the same
``dedup_key``; if one is already sitting there, it posts nothing. Andrew closing the task in
the CRM is what lets the next nightly run raise it again. That single rule replaces both the
old "update the open notice in place" dedup AND the old "ignore for N days" snooze.
The desk has no UPDATE permission, so a repeat run cannot refresh an open alert's numbers —
it leaves the original standing rather than stacking a duplicate.

THE DE-DUPLICATION CHECK FAILS CLOSED, AND SAYS SO OUT LOUD (2026-09-08). If the CRM cannot be
asked whether an alert is already open, nothing is posted — the desk can create a task but has
no permission to delete one, so a duplicate filed during a network blip could only be cleared
by Andrew dismissing it by hand, whereas a report skipped tonight comes back on its own because
these jobs run every night. A missed report is the cheaper mistake. What CHANGED is the
reporting: ``has_open`` used to answer "yes, already open" when it simply could not reach the
CRM, so a complete outage and a genuine duplicate both came back as ``SKIPPED`` and every one
of the four nightly jobs exited 0 — a night when NOTHING could be reported looked like a clean
night. It now returns a third answer, ``COULD_NOT_TELL``, and ``post_notice`` turns that into
``FAILED``: still nothing written, but the calling job now exits non-zero and the outage shows.

WHAT ``post_notice`` GIVES BACK says which of the three things happened, so a caller can tell
"already reported" apart from "reporting is broken": the new task's id when it posted,
``SKIPPED`` (an empty string) when it deliberately posted nothing, and ``FAILED`` (None) when
the post could not be made. Both of the "nothing was written" answers are still falsy, so the
four calling jobs, which test the answer for truth, keep behaving exactly as they did.

PLAIN-ENGLISH RULE (#1): every title/body posted is a full, non-technical sentence.

FAILURE IS NEVER FATAL: every function here swallows its errors and returns the safe answer
(the one that touches the CRM least) after logging a plain-English line. A nightly job's whole purpose is to report a problem; it
must not die because the CRM happened to be unreachable while it was doing so.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

# Reuse the desk's existing CRM helper (paperbot/crm_roster.py) rather than inventing a second
# connection path. Derived from __file__ because the repo has moved before.
_PAPERBOT = Path(__file__).resolve().parents[2] / "paperbot"
if _PAPERBOT.is_dir() and str(_PAPERBOT) not in sys.path:
    sys.path.insert(0, str(_PAPERBOT))

# Required by the RLS policy `tradingdesk_insert_own_alerts` — an insert without it is refused.
SOURCE = "trading_desk"

# The two ways post_notice can write nothing. Both are falsy, so a caller that simply tests the
# answer for truth still reads them as "nothing was written"; a caller that wants the reason can
# compare against these instead.
SKIPPED = ""      # on purpose: an alert for this is already open
FAILED = None     # not on purpose: the CRM could not be reached or refused the insert

# has_open()'s THIRD answer, alongside True and False: the CRM could not be reached, so whether
# an alert is already open is simply not known. Deliberately TRUTHY, so a caller that forgets
# this case and just tests the answer still posts nothing — the safe direction.
COULD_NOT_TELL = "could not tell"

# The CRM's tasks.severity only accepts error/warning/info; the desk's posters all say "warn".
# Translate here rather than edit the four calling jobs.
_SEVERITY = {"warn": "warning", "warning": "warning", "error": "error",
             "critical": "error", "info": "info"}


def _log(msg: str) -> None:
    try:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  action_center: {msg}",
              file=sys.stderr, flush=True)
    except Exception:
        pass


def _connect():
    """Open a CRM connection using the desk's existing helper and its env var. Raises
    crm_roster.CrmRosterUnavailable if the DSN is missing; callers catch everything."""
    import crm_roster  # noqa: PLC0415 — imported lazily so module import stays cheap
    if not crm_roster.is_configured():
        raise crm_roster.CrmRosterUnavailable(
            f"CRM connection is not configured: set the {crm_roster.DSN_ENV} environment "
            f"variable. No credential is stored in code.")
    import psycopg2  # noqa: PLC0415
    return psycopg2.connect(os.environ[crm_roster.DSN_ENV].strip())


def has_open(dedup_key: str) -> bool | str:
    """Whether the CRM already holds an OPEN trading-desk alert carrying this ``dedup_key``.

    Three answers, not two: True (one is already open), False (none is open), and
    ``COULD_NOT_TELL`` (the CRM could not be reached, so the question has no answer). Keeping
    the third one separate is the whole point — a real duplicate and a total outage used to
    look identical, so a night when nothing could be reported looked like a clean night.

    Nothing is posted for either True or COULD_NOT_TELL (fail CLOSED): the desk can file a task
    but cannot delete one, so a duplicate filed during a blip would have to be dismissed by
    hand, while a report held back tonight is raised again by tomorrow night's run."""
    if not dedup_key:
        return False
    try:
        con = _connect()
        try:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM public.tasks WHERE source = %s AND dedup_key = %s "
                    "AND status = 'open' LIMIT 1", (SOURCE, dedup_key))
                return cur.fetchone() is not None
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        _log(f"could not reach the CRM Action Center to ask whether a '{dedup_key}' alert is "
             f"already open ({exc}). Nothing will be posted this time, and the run will be "
             f"reported as a failure. If the problem is still there, tomorrow night's run "
             f"will report it.")
        return COULD_NOT_TELL


def post_notice(kind: str, title: str, body: str, *, severity: str = "info",
                action_hint: str = "", dedup_key: str | None = None,
                detail_json=None, ts: str | None = None) -> str | None:
    """File ONE alert in the CRM's Action Center. Returns the new task's id when it posted,
    ``SKIPPED`` (an empty string) when it deliberately posted nothing, and ``FAILED`` (None)
    when the post could not be made — so a caller can tell "already reported" apart from
    "reporting is broken". Both "nothing written" answers are falsy, exactly as the single old
    None was. NEVER raises: a nightly job must survive its own alert failing to file.

    The signature is unchanged so the four calling jobs need no edit. ``action_hint`` is
    appended to the description (the CRM has no separate hint column). ``detail_json`` and
    ``ts`` are accepted and ignored — the CRM has no column for the structured blob and stamps
    its own ``created_at``."""
    description = f"{body}\n\n{action_hint}".strip() if action_hint else (body or "")
    # Fails closed BOTH ways — nothing is written unless we KNOW no alert is open — but the two
    # reasons are reported differently, because an outage is a failure and a duplicate is not.
    already = has_open(dedup_key) if dedup_key else False
    if already == COULD_NOT_TELL:
        _log(f"posting nothing about '{dedup_key}': the CRM Action Center could not be "
             f"reached, so there is no way to tell whether this was already reported. "
             f"Nothing was written, and this run counts as a failure to report.")
        return FAILED
    if already:
        _log(f"posting nothing about '{dedup_key}': the CRM Action Center already holds an "
             f"open alert for it.")
        return SKIPPED
    try:
        con = _connect()
        try:
            with con.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.tasks "
                    "(title, description, severity, category, dedup_key, source, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'open') RETURNING id",
                    (title, description,
                     _SEVERITY.get(str(severity).strip().lower(), "info"),
                     kind, dedup_key or None, SOURCE))
                task_id = str(cur.fetchone()[0])
            con.commit()
            return task_id
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        _log(f"could not file this alert in the CRM Action Center ({exc}); the desk job "
             f"carries on. The alert was: {title}")
        return FAILED
