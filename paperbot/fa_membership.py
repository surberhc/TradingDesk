"""
fa_membership.py — PURE / OFFLINE FA group-MEMBERSHIP computation + GROUPS-XML mutation.

This is the membership half of the CRM group-membership sync (conductor #42/#43,
docs/CRM_DESIGN §8 / §11 gap 4). The CRM decides which accounts belong to which FA group
(an account joins the FA group of every sleeve its template runs); that desired membership
is exposed by ``crm.brain.CRMBrain.group_membership()`` as ``{fa_group -> set(account_id)}``.
IBKR's live GROUPS XML must be kept in sync with it.

``rebalance_execute.set_group_contracts_or_shares`` already writes each order's per-account
ContractsOrShares AMOUNTS, but it does NOT change WHO is in a group (§11 gap 4). This module
fills that gap on the PURE side: it reads/produces GROUPS-XML strings and plain dicts and
computes/applies membership set changes. It NEVER talks to a broker.

BOUNDARIES (deliberate):
  * PURE / OFFLINE. stdlib only (``xml.etree.ElementTree``, ``typing``). No broker, no
    ib_async, no gateway, no ``crm`` import. The GROUPS XML is a STRING passed IN; the
    desired membership is a plain ``{group -> set(account)}`` dict passed IN (the caller
    gets it from ``CRMBrain.group_membership()``). This module never calls requestFA /
    replaceFA.
  * MEMBERSHIP ONLY, NOT AMOUNTS. Membership == the SET of accounts in a group. This module
    changes membership only; it PRESERVES existing members' ``<amount>`` values and every
    other group + config byte-for-byte. A newly-ADDED member gets a placeholder ``<amount>``
    (default 0) — its real ContractsOrShares is set per-order later by
    ``set_group_contracts_or_shares``.

XML approach MATCHES ``set_group_contracts_or_shares`` exactly: ``xml.etree.ElementTree``,
NAMESPACE-AGNOSTIC (tags matched via ``tag.split("}")[-1].lower()``), and the namespace
prefix is preserved when creating new elements.

GROUPS XML shape (as revealed by set_group_contracts_or_shares): a root holding <Group>
elements, each with a <name>, a <defaultMethod>, and a <ListOfAccts> holding <Account>
elements, each with <acct> (the account number) and <amount> (the ContractsOrShares value).

The LIVE glue — requestFA(1) -> parse_group_membership -> membership_diff vs
CRMBrain.group_membership() -> backup_fa_groups -> apply_membership -> replaceFA(1), behind
the arm gate — is the gateway-gated follow-on and is NOT built here.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Dict, List, Mapping, Set


def _local(tag: str) -> str:
    """Namespace-agnostic local tag name, lowercased — same rule as
    set_group_contracts_or_shares (``tag.split("}")[-1].lower()``)."""
    return tag.split("}")[-1].lower()


def _find_child(elem, local_name: str):
    """First DIRECT child of ``elem`` whose local tag matches ``local_name`` (lowercased)."""
    for child in list(elem):
        if _local(child.tag) == local_name:
            return child
    return None


def _group_name(grp) -> str | None:
    """The <name> text of a <Group> (first matching descendant, trimmed), or None."""
    for c in grp.iter():
        if _local(c.tag) == "name" and c.text:
            return c.text.strip()
    return None


def _iter_groups(root):
    """Yield every <Group> element under ``root`` (namespace-agnostic)."""
    for grp in root.iter():
        if _local(grp.tag) == "group":
            yield grp


def parse_group_membership(groups_xml: str) -> Dict[str, Set[str]]:
    """Parse a live GROUPS XML string into ``{group_name -> set(account_id)}``.

    For each <Group> read its <name> and the <acct> text of every <Account> under its
    <ListOfAccts>. Namespace-agnostic (same tag-matching as set_group_contracts_or_shares).

    Empty / whitespace-only XML returns ``{}`` (chosen over raising: parsing is a read, and
    callers gate on emptiness — mirroring set_group_contracts_or_shares, which treats an
    empty requestFA(1) as "fail closed, write nothing"). A group with no <ListOfAccts> (or
    an empty one) yields an empty set for that group.
    """
    if not groups_xml or not groups_xml.strip():
        return {}

    root = ET.fromstring(groups_xml)
    out: Dict[str, Set[str]] = {}
    for grp in _iter_groups(root):
        name = _group_name(grp)
        if not name:
            continue
        accts: Set[str] = set()
        loa = _find_child(grp, "listofaccts")
        if loa is not None:
            for acct_el in loa:
                if _local(acct_el.tag) != "account":
                    continue
                acct_val = _find_child(acct_el, "acct")
                if acct_val is not None and acct_val.text and acct_val.text.strip():
                    accts.add(acct_val.text.strip())
        out[name] = accts
    return out


def membership_diff(
    desired: Mapping[str, Set[str]], current: Mapping[str, Set[str]]
) -> Dict[str, Dict[str, List[str]]]:
    """Per-group membership delta: ``{group -> {"add": [...], "remove": [...]}}``.

    ``add``    = sorted accounts in ``desired[group]`` but not ``current[group]``.
    ``remove`` = sorted accounts in ``current[group]`` but not ``desired[group]``.

    A group present on only one side is fully add (desired-only) or fully remove
    (current-only). Only groups with a non-empty add or remove are returned — a clean group
    is omitted. PURE.
    """
    out: Dict[str, Dict[str, List[str]]] = {}
    for group in set(desired) | set(current):
        want = set(desired.get(group, set()))
        have = set(current.get(group, set()))
        add = sorted(want - have)
        remove = sorted(have - want)
        if add or remove:
            out[group] = {"add": add, "remove": remove}
    return out


def apply_membership(
    groups_xml: str, desired: Mapping[str, Set[str]], *, new_member_amount: int = 0
) -> str:
    """Return NEW GROUPS XML with each group named in ``desired`` set to EXACTLY that
    membership, preserving amounts and every untouched byte.

    For every group named in ``desired``:
      * keep existing <Account> entries whose <acct> is still desired — PRESERVING their
        <amount> and any other children byte-for-byte;
      * remove <Account> entries no longer desired;
      * add a new <Account> (with <acct> + <amount> = ``new_member_amount``, using the
        ListOfAccts namespace prefix like set_group_contracts_or_shares) for each
        newly-desired account, appended in sorted order for determinism.

    Groups NOT named in ``desired`` are left byte-for-byte untouched.

    A desired group MISSING from the XML raises ValueError (fail loud — refuse to invent a
    group; group CREATION is a separate admin step). An empty / whitespace-only ``groups_xml``
    likewise raises when ``desired`` is non-empty (there is nothing to mutate); with an empty
    ``desired`` it is a no-op that returns the input unchanged.

    Returns the serialized XML (``ET.tostring(..., encoding="unicode")``).
    """
    if not desired:
        # Nothing to change — return the input untouched (no parse needed).
        return groups_xml

    if not groups_xml or not groups_xml.strip():
        raise ValueError(
            "apply_membership: empty GROUPS XML but desired membership is non-empty — "
            "nothing to mutate. FAILING LOUD (refusing to invent groups)."
        )

    root = ET.fromstring(groups_xml)

    # Index groups present in the XML by name.
    by_name = {}
    for grp in _iter_groups(root):
        name = _group_name(grp)
        if name:
            by_name[name] = grp

    missing = sorted(set(desired) - set(by_name))
    if missing:
        raise ValueError(
            f"apply_membership: desired group(s) {missing} not present in GROUPS XML — "
            f"refusing to invent a group (creation is a separate admin step). FAILING LOUD."
        )

    for name, want_accts in desired.items():
        grp = by_name[name]
        want = set(want_accts)
        loa = _find_child(grp, "listofaccts")
        if loa is None:
            raise ValueError(
                f"apply_membership: FA group '{name}' has no ListOfAccts element — refusing "
                f"to guess the member layout. FAILING LOUD."
            )

        # Namespace prefix carried from the ListOfAccts tag, matching
        # set_group_contracts_or_shares.
        tag_prefix = loa.tag[: loa.tag.find("}") + 1] if "}" in loa.tag else ""

        # Keep/remove existing <Account> entries; track which desired accts already exist so
        # kept members retain their <amount> and other children untouched.
        existing: Set[str] = set()
        for acct_el in list(loa):
            if _local(acct_el.tag) != "account":
                continue  # leave non-Account children alone
            acct_val = _find_child(acct_el, "acct")
            acct = acct_val.text.strip() if (acct_val is not None and acct_val.text) else None
            if acct is None or acct not in want:
                loa.remove(acct_el)  # gone member (or malformed acct) -> drop
            else:
                existing.add(acct)

        # Append newly-desired accounts (sorted for determinism) with placeholder amount.
        for acct in sorted(want - existing):
            ael = ET.SubElement(loa, f"{tag_prefix}Account")
            nm = ET.SubElement(ael, f"{tag_prefix}acct")
            nm.text = acct
            amt = ET.SubElement(ael, f"{tag_prefix}amount")
            amt.text = str(int(new_member_amount))

    return ET.tostring(root, encoding="unicode")
