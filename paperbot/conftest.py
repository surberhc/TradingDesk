"""Shared fixtures for the paperbot tests.

``cashflows.SCHEDULE`` now reads the live client system (the CRM) on first use. No test in
this suite may reach it, so an EMPTY schedule is pinned in automatically for every test —
which is what these tests already assumed (no test account has ever had a real scheduled
withdrawal, so every reserve_for() in this suite was, and stays, 0.0). A test that wants a
schedule monkeypatches cashflows.SCHEDULE itself, exactly as before.
"""
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


@pytest.fixture(autouse=True)
def _no_live_withdrawal_schedule(monkeypatch):
    """Autouse so no paperbot test can read Andrew's real client withdrawal schedule."""
    import cashflows
    monkeypatch.setattr(cashflows, "SCHEDULE", {})
