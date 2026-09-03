"""Tests for group_execute — the LIVE advisor-master target for the per-ticker group rail.

The master F6795549 was read live on 2026-09-03: 354 client accounts, 8 pre-existing groups.
The old login (apsv1816) carried 18 accounts and no master at all, so the account wall is now
the ONLY thing scoping a run away from Ted's and Doug's books.
"""
from __future__ import annotations

import pytest

import group_execute as ge
from connections import clientids


BOOK = {"U23415099": "Growth (Custom)", "U23414989": "Growth (Custom)",
        "U27305011": "Balanced (Small, Custom)"}


def test_points_at_the_live_master_on_4003_with_the_reserved_client_id():
    t = ge.live_gateway(BOOK)
    assert t.name == "LIVE"
    assert t.master_account == "F6795549"
    assert t.port == clientids.LIVE_TRADE_PORT == 4003
    assert t.clientid_consumer == "live_fa_block_exec"
    assert clientids.get("live_fa_block_exec") == 63


def test_the_pin_is_a_client_sub_and_is_deterministic():
    """Two runs of the same scope must pin identically or a run is not reproducible."""
    a = ge.live_gateway(BOOK)
    b = ge.live_gateway(dict(reversed(list(BOOK.items()))))
    assert a.pin_account == b.pin_account == "U23414989"
    assert a.pin_account != ge.LIVE_MASTER_ACCOUNT


def test_refuses_to_pin_to_the_master():
    """The master's own account-update stream hangs the session."""
    with pytest.raises(ValueError) as e:
        ge.live_gateway({**BOOK, "F6795549": "Growth (Custom)"},
                        pin_account="F6795549")
    assert "hangs the session" in str(e.value)


def test_refuses_a_pin_outside_the_scoped_book():
    with pytest.raises(ValueError) as e:
        ge.live_gateway(BOOK, pin_account="U99999999")
    assert "not in the enrollment" in str(e.value)


def test_refuses_an_empty_enrollment():
    """An empty roster read must never silently produce a live gateway."""
    for empty in ({}, None, {"   ": "Growth (Custom)"}):
        with pytest.raises(ValueError):
            ge.live_gateway(empty)


def test_carries_no_static_group_map():
    """TIER_GROUPS is meaningless here: one group per TICKER per RUN, name on the route."""
    assert ge.live_gateway(BOOK).group_names is None


def test_enrollment_is_copied_not_aliased():
    src = dict(BOOK)
    t = ge.live_gateway(src)
    src["U00000001"] = "Growth (Custom)"
    assert "U00000001" not in t.enrollment, "a later mutation must not widen a built gateway"


def test_an_explicit_pin_is_honoured_when_it_is_in_the_book():
    assert ge.live_gateway(BOOK, pin_account="U27305011").pin_account == "U27305011"
