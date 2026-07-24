"""
test_fa_membership.py — offline tests for the PURE FA group-membership half
(conductor #42/#43, docs/CRM_DESIGN §8 / §11 gap 4).

All synthetic GROUPS XML here mirrors the real structure revealed by
rebalance_execute.set_group_contracts_or_shares: a root of <Group> elements, each with a
<name>, a <defaultMethod>, and a <ListOfAccts> of <Account> (<acct> + <amount>). One
variant is namespaced to prove namespace-agnosticism.
"""

import xml.etree.ElementTree as ET

import pytest

from fa_membership import (
    apply_membership,
    membership_diff,
    parse_group_membership,
)


# --- synthetic fixtures -------------------------------------------------------
def _plain_xml() -> str:
    """No namespace. TIER_A: two members with amounts. TIER_B: empty ListOfAccts."""
    return (
        "<ListOfGroups>"
        "  <Group>"
        "    <name>TIER_A</name>"
        "    <defaultMethod>ContractsOrShares</defaultMethod>"
        "    <ListOfAccts>"
        "      <Account><acct>DU100001</acct><amount>7</amount></Account>"
        "      <Account><acct>DU100002</acct><amount>13</amount></Account>"
        "    </ListOfAccts>"
        "  </Group>"
        "  <Group>"
        "    <name>TIER_B</name>"
        "    <defaultMethod>ContractsOrShares</defaultMethod>"
        "    <ListOfAccts>"
        "    </ListOfAccts>"
        "  </Group>"
        "</ListOfGroups>"
    )


def _ns_xml() -> str:
    """Namespaced variant (default xmlns). One group, two members."""
    return (
        '<ListOfGroups xmlns="urn:ibkr:fa">'
        "  <Group>"
        "    <name>TIER_A</name>"
        "    <defaultMethod>ContractsOrShares</defaultMethod>"
        "    <ListOfAccts>"
        "      <Account><acct>DU100001</acct><amount>7</amount></Account>"
        "      <Account><acct>DU100002</acct><amount>13</amount></Account>"
        "    </ListOfAccts>"
        "  </Group>"
        "</ListOfGroups>"
    )


# --- parse_group_membership ---------------------------------------------------
def test_parse_mixed_and_empty_groups():
    got = parse_group_membership(_plain_xml())
    assert got == {"TIER_A": {"DU100001", "DU100002"}, "TIER_B": set()}


def test_parse_namespaced():
    got = parse_group_membership(_ns_xml())
    assert got == {"TIER_A": {"DU100001", "DU100002"}}


def test_parse_empty_xml_returns_empty_dict():
    assert parse_group_membership("") == {}
    assert parse_group_membership("   \n  ") == {}


def test_parse_group_without_listofaccts_is_empty_set():
    xml = (
        "<ListOfGroups>"
        "  <Group><name>SOLO</name><defaultMethod>ContractsOrShares</defaultMethod></Group>"
        "</ListOfGroups>"
    )
    assert parse_group_membership(xml) == {"SOLO": set()}


# --- membership_diff ----------------------------------------------------------
def test_diff_add_only():
    current = {"G": {"A"}}
    desired = {"G": {"A", "B"}}
    assert membership_diff(desired, current) == {"G": {"add": ["B"], "remove": []}}


def test_diff_remove_only():
    current = {"G": {"A", "B"}}
    desired = {"G": {"A"}}
    assert membership_diff(desired, current) == {"G": {"add": [], "remove": ["B"]}}


def test_diff_mixed_is_sorted():
    current = {"G": {"A", "C", "X"}}
    desired = {"G": {"A", "B", "D"}}
    assert membership_diff(desired, current) == {
        "G": {"add": ["B", "D"], "remove": ["C", "X"]}
    }


def test_diff_no_change_group_omitted():
    current = {"G": {"A", "B"}, "H": {"Z"}}
    desired = {"G": {"B", "A"}, "H": {"Z"}}
    assert membership_diff(desired, current) == {}


def test_diff_desired_only_group_all_add():
    assert membership_diff({"NEW": {"A", "B"}}, {}) == {
        "NEW": {"add": ["A", "B"], "remove": []}
    }


def test_diff_current_only_group_all_remove():
    assert membership_diff({}, {"OLD": {"A", "B"}}) == {
        "OLD": {"add": [], "remove": ["A", "B"]}
    }


# --- apply_membership ---------------------------------------------------------
def test_apply_adds_new_member_with_zero_amount():
    out = apply_membership(_plain_xml(), {"TIER_A": {"DU100001", "DU100002", "DU100003"}})
    root = ET.fromstring(out)
    # locate TIER_A ListOfAccts
    amounts = {}
    for grp in root.iter("Group"):
        if grp.findtext("name") == "TIER_A":
            for acct in grp.iter("Account"):
                amounts[acct.findtext("acct")] = acct.findtext("amount")
    assert amounts == {"DU100001": "7", "DU100002": "13", "DU100003": "0"}


def test_apply_custom_new_member_amount():
    out = apply_membership(
        _plain_xml(), {"TIER_A": {"DU100001", "DU100002", "DU100003"}},
        new_member_amount=5,
    )
    root = ET.fromstring(out)
    for grp in root.iter("Group"):
        if grp.findtext("name") == "TIER_A":
            got = {a.findtext("acct"): a.findtext("amount") for a in grp.iter("Account")}
    assert got["DU100003"] == "5"


def test_apply_removes_gone_member_and_preserves_kept_amount():
    # Drop DU100002, keep DU100001 (whose amount must survive).
    out = apply_membership(_plain_xml(), {"TIER_A": {"DU100001"}})
    got = parse_group_membership(out)
    assert got["TIER_A"] == {"DU100001"}
    root = ET.fromstring(out)
    for grp in root.iter("Group"):
        if grp.findtext("name") == "TIER_A":
            amounts = {a.findtext("acct"): a.findtext("amount") for a in grp.iter("Account")}
    assert amounts == {"DU100001": "7"}  # preserved, not reset


def test_apply_leaves_untouched_group_byte_identical():
    # Touch only TIER_A; TIER_B must be byte-for-byte identical to the source serialization.
    src = _plain_xml()
    out = apply_membership(src, {"TIER_A": {"DU100001"}})

    def tier_b_bytes(xml_str):
        root = ET.fromstring(xml_str)
        for grp in root.iter("Group"):
            if grp.findtext("name") == "TIER_B":
                return ET.tostring(grp, encoding="unicode")
        return None

    assert tier_b_bytes(out) == tier_b_bytes(src)


def test_apply_preserves_namespace():
    out = apply_membership(_ns_xml(), {"TIER_A": {"DU100001", "DU100009"}})
    # namespace-agnostic parse still works and reflects the change
    assert parse_group_membership(out)["TIER_A"] == {"DU100001", "DU100009"}
    # the default-namespace declaration survives serialization
    assert "urn:ibkr:fa" in out


def test_apply_raises_for_desired_group_absent_from_xml():
    with pytest.raises(ValueError, match="not present"):
        apply_membership(_plain_xml(), {"TIER_Z": {"DU100001"}})


def test_apply_empty_desired_is_noop():
    src = _plain_xml()
    assert apply_membership(src, {}) == src


def test_apply_empty_xml_with_desired_raises():
    with pytest.raises(ValueError, match="empty GROUPS XML"):
        apply_membership("", {"TIER_A": {"DU100001"}})


def test_apply_adds_to_empty_listofaccts():
    out = apply_membership(_plain_xml(), {"TIER_B": {"DU200001", "DU200002"}})
    assert parse_group_membership(out)["TIER_B"] == {"DU200001", "DU200002"}


# --- round-trip ---------------------------------------------------------------
def test_parse_apply_roundtrip_on_touched_groups():
    desired = {
        "TIER_A": {"DU100001", "DU100003", "DU100004"},  # keep 1, drop 2, add 3+4
        "TIER_B": {"DU200001"},  # add into empty group
    }
    out = apply_membership(_plain_xml(), desired)
    got = parse_group_membership(out)
    assert got["TIER_A"] == desired["TIER_A"]
    assert got["TIER_B"] == desired["TIER_B"]


def test_parse_apply_roundtrip_namespaced():
    desired = {"TIER_A": {"DU100002", "DU100077"}}
    out = apply_membership(_ns_xml(), desired)
    assert parse_group_membership(out)["TIER_A"] == desired["TIER_A"]
