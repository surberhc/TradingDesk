"""crm_roster_check.py — READ-ONLY post-flip verifier for the CRM roster seam.

Purpose
-------
After the owner sets the ``tradingdesk_readonly`` password (Supabase) and the
``TRADINGDESK_CRM_DSN`` Windows env var, run this to CONFIRM the desk now reads the
LIVE CRM roster (~303 accounts) instead of its 3-account ``config.ENROLLMENT``
fallback — WITHOUT needing the :8502 panel armed or even open.

It reuses the desk's own ``crm_roster`` / ``roster`` code (the exact seam the panel
uses), so a green result here means the real path works, not a parallel reimplementation.

HARD GUARANTEES (matches crm_roster.py):
  * READ-ONLY. Opens a read-only psycopg2 session (``set_session(readonly=True)``)
    and issues only SELECTs against the two postgres-owned views. Writes nothing,
    builds no order, contacts no broker, transmits nothing.
  * No hard-coded credential — the DSN comes ONLY from the ``TRADINGDESK_CRM_DSN``
    environment variable. This script never prints the DSN (it may contain a password).

Run (from anywhere) with the desk venv python:
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" ^
        "C:\\TradingDesk\\dashboard\\desk\\crm_roster_check.py"

Exit code 0 = LIVE CRM roster confirmed; 1 = still on the config fallback / not wired;
2 = configured but the CRM could not be read (see the printed error).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Bootstrap sys.path exactly like desk_app.py so `crm_roster`, `roster`, `config`
# (all under paperbot/) import cleanly no matter the working directory.
_REPO = Path(__file__).resolve().parents[2]  # C:\TradingDesk
for _sub in ("paperbot", "connections", "strategies", "backtester"):
    _p = _REPO / _sub
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def main() -> int:
    import config
    import crm_roster
    import roster

    fallback = sorted(set(config.ENROLLMENT))
    print("=== CRM roster seam - READ-ONLY verification ===")
    print(f"env var {crm_roster.DSN_ENV} set? {crm_roster.is_configured()}")
    print(f"config fallback ({len(fallback)} accts): {fallback}")

    if not crm_roster.is_configured():
        print(
            f"\nRESULT: NOT WIRED - {crm_roster.DSN_ENV} is unset/empty. The desk is on "
            f"the {len(fallback)}-account config fallback. Set the env var (and the "
            f"tradingdesk_readonly password) to go live, then re-run.")
        return 1

    # Configured: read the LIVE roster through the desk's own read-only path.
    try:
        conn = crm_roster._connect()
        try:
            whole = crm_roster.fetch_roster(advisor_name=None, conn=conn)
            andrew = crm_roster.fetch_roster(
                advisor_name=crm_roster.DEFAULT_ADVISOR, conn=conn)
            andrew_ids = [r["account_id"] for r in andrew]
            funded = crm_roster.funded_account_ids(andrew_ids, conn=conn)
        finally:
            conn.close()
    except crm_roster.CrmRosterUnavailable as exc:
        print(f"\nRESULT: CONFIGURED but UNREADABLE - {exc}")
        print("The panel would fall back to config until this connects. Fix the DSN / "
              "password / grants, then re-run.")
        return 2

    # What the account wall would actually use (CRM path first, else fallback).
    wall = roster.enrolled_roster()
    on_fallback = wall == fallback

    print(f"\nLIVE READ OK (read-only):")
    print(f"  whole-book roster rows (v_tradingdesk_roster) : {len(whole)}")
    print(f"  {crm_roster.DEFAULT_ADVISOR}'s book             : {len(andrew)}")
    print(f"  ...of which funded (have latest snapshot)      : {len(funded)}")
    print(f"  account wall roster.enrolled_roster() size     : {len(wall)}")

    if on_fallback:
        print(
            "\nRESULT: STILL ON FALLBACK - the wall returned exactly the config list. "
            "The CRM read returned nothing usable (empty funded set?). Investigate before "
            "relying on the live roster.")
        return 1

    print(
        f"\nRESULT: LIVE CRM ROSTER CONFIRMED - the whole-book panel reads {len(whole)} "
        f"live accounts and the account wall is on the CRM path ({len(wall)} funded "
        f"accounts in {crm_roster.DEFAULT_ADVISOR}'s book), NOT the {len(fallback)}-account "
        f"config fallback. Read-only throughout; nothing was placed or transmitted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
