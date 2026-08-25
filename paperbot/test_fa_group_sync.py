"""
test_fa_group_sync.py — offline tests for the LIVE GLUE of FA group-membership sync.

NOTHING here touches a real gateway. Every "connection" is a FakeIB stub that records the
requestFA / replaceFA calls made against it, so each test can assert not only the OUTCOME but
whether a write HAPPENED AT ALL — which is the property that actually matters, because
replaceFA overwrites the ENTIRE groups XML on the FA master.

The synthetic GROUPS XML mirrors the real structure (root of <Group>, each with <name>,
<defaultMethod>, <ListOfAccts> of <Account>(<acct>,<amount>)) exactly as test_fa_membership.py
does, including a namespaced variant.

Coverage required by the build task:
  * a membership ADD                                    -> test_plan_add_*, test_sync_armed_*
  * a membership REMOVAL                                -> test_plan_removal_*
  * a NO-OP writes NOTHING at all                       -> test_noop_*
  * empty / garbage requestFA fails closed              -> test_read_live_groups_*
  * a write with NO backup is refused                   -> test_apply_refuses_without_backup*
  * a write while NOT armed is refused                  -> test_apply_refuses_when_not_armed*
  * unrelated groups and every kept member's <amount>
    survive byte-for-byte                               -> test_untouched_group_survives_*
"""

import os
import xml.etree.ElementTree as ET

import pytest

import config
import fa_group_sync as fgs
from fa_group_sync import (
    FaGroupSyncRefused,
    FaGroupSyncVerifyFailed,
    apply_membership_change,
    backup_groups,
    backup_is_usable,
    plan_membership_change,
    read_live_groups,
    sync_group_membership,
)


# --- fixtures -----------------------------------------------------------------------------
def _xml() -> str:
    """TIER_A: two members with distinct amounts. TIER_B: an UNRELATED group that must never
    change. TIER_C: empty ListOfAccts."""
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
        "      <Account><acct>DU200001</acct><amount>41</amount></Account>"
        "      <Account><acct>DU200002</acct><amount>59</amount></Account>"
        "    </ListOfAccts>"
        "  </Group>"
        "  <Group>"
        "    <name>TIER_C</name>"
        "    <defaultMethod>ContractsOrShares</defaultMethod>"
        "    <ListOfAccts></ListOfAccts>"
        "  </Group>"
        "</ListOfGroups>"
    )


def _ns_xml() -> str:
    """Namespaced variant — the glue must be namespace-agnostic like the pure half."""
    return (
        '<ListOfGroups xmlns="urn:ibkr:fa">'
        "  <Group>"
        "    <name>TIER_A</name>"
        "    <defaultMethod>ContractsOrShares</defaultMethod>"
        "    <ListOfAccts>"
        "      <Account><acct>DU100001</acct><amount>7</amount></Account>"
        "    </ListOfAccts>"
        "  </Group>"
        "</ListOfGroups>"
    )


class FakeIB:
    """Records every requestFA / replaceFA. NEVER talks to a broker.

    ``responses`` is the queue of requestFA return values (the last one repeats, so a
    post-write read-back can be modelled). A replaceFA payload is appended to ``replaced`` and,
    when ``echo_writes`` is True, becomes the next requestFA response — modelling a master that
    actually accepted the write.
    """

    def __init__(self, *responses, echo_writes=True, request_raises=None):
        self.responses = list(responses) or [""]
        self.replaced = []
        self.requests = []
        self.echo_writes = echo_writes
        self.request_raises = request_raises

    def requestFA(self, kind):
        self.requests.append(kind)
        if self.request_raises is not None:
            raise self.request_raises
        return self.responses[0] if len(self.responses) == 1 else self.responses.pop(0)

    def replaceFA(self, kind, xml):
        self.replaced.append((kind, xml))
        if self.echo_writes:
            self.responses = [xml]


@pytest.fixture
def armed_config(monkeypatch):
    """Flip the CODE GATE open for the tests that must reach the write. The committed on-disk
    defaults stay READONLY=True / DRY_RUN=True — this is in-process only."""
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)


def _amounts(xml: str, group: str) -> dict:
    """{acct -> amount text} for one group, namespace-agnostically."""
    root = ET.fromstring(xml)
    for grp in root.iter():
        if grp.tag.split("}")[-1].lower() != "group":
            continue
        name = next((c.text.strip() for c in grp.iter()
                     if c.tag.split("}")[-1].lower() == "name" and c.text), None)
        if name != group:
            continue
        out = {}
        for acct_el in grp.iter():
            if acct_el.tag.split("}")[-1].lower() != "account":
                continue
            acct = amount = None
            for child in list(acct_el):
                tag = child.tag.split("}")[-1].lower()
                if tag == "acct":
                    acct = (child.text or "").strip()
                elif tag == "amount":
                    amount = (child.text or "").strip()
            if acct:
                out[acct] = amount
        return out
    raise AssertionError(f"group {group} not found")


def _group_blob(xml: str, group: str) -> str:
    """The serialized subtree of ONE group — the byte-for-byte survival probe."""
    root = ET.fromstring(xml)
    for grp in root.iter():
        if grp.tag.split("}")[-1].lower() != "group":
            continue
        name = next((c.text.strip() for c in grp.iter()
                     if c.tag.split("}")[-1].lower() == "name" and c.text), None)
        if name == group:
            return ET.tostring(grp, encoding="unicode")
    raise AssertionError(f"group {group} not found")


# ============================================================================================
# 1. READ — fail closed on anything untrustworthy.
# ============================================================================================
def test_read_live_groups_returns_xml():
    ib = FakeIB(_xml())
    assert read_live_groups(ib).startswith("<ListOfGroups>")
    assert ib.requests == [fgs.FA_GROUPS]


@pytest.mark.parametrize("response", ["", "   ", None, "\n\t "])
def test_read_live_groups_empty_response_fails_closed(response):
    """An EMPTY requestFA is 'the state is unknown', never 'the master has no groups'."""
    ib = FakeIB(response)
    with pytest.raises(FaGroupSyncRefused) as exc:
        read_live_groups(ib)
    assert "EMPTY" in str(exc.value)
    assert ib.replaced == []


def test_read_live_groups_garbage_fails_closed():
    ib = FakeIB("<ListOfGroups><Group><name>TIER_A</name>")   # truncated / unparseable
    with pytest.raises(FaGroupSyncRefused) as exc:
        read_live_groups(ib)
    assert "UNPARSEABLE" in str(exc.value)
    assert ib.replaced == []


def test_read_live_groups_no_groups_fails_closed():
    ib = FakeIB("<ListOfGroups></ListOfGroups>")
    with pytest.raises(FaGroupSyncRefused) as exc:
        read_live_groups(ib)
    assert "NO <Group>" in str(exc.value)


def test_read_live_groups_request_raises_fails_closed():
    ib = FakeIB(_xml(), request_raises=TimeoutError("requestFA timed out"))
    with pytest.raises(FaGroupSyncRefused):
        read_live_groups(ib)


def test_read_live_groups_without_connection_fails_closed():
    with pytest.raises(FaGroupSyncRefused):
        read_live_groups(None)


# ============================================================================================
# 2. BACKUP — mandatory and verified.
# ============================================================================================
def test_backup_groups_writes_and_verifies(tmp_path):
    path = backup_groups(_xml(), str(tmp_path))
    assert os.path.isfile(path) and os.path.getsize(path) > 0
    assert "fa_groups_backup_" in os.path.basename(path)
    with open(path, encoding="utf-8") as fh:
        assert fh.read() == _xml()
    assert backup_is_usable(path)[0] is True


def test_backup_groups_refuses_empty_xml(tmp_path):
    with pytest.raises(FaGroupSyncRefused):
        backup_groups("   ", str(tmp_path / "b.xml"))


def test_backup_is_usable_rejects_missing_empty_and_garbage(tmp_path):
    assert backup_is_usable("")[0] is False
    assert backup_is_usable(str(tmp_path / "nope.xml"))[0] is False
    empty = tmp_path / "empty.xml"
    empty.write_text("", encoding="utf-8")
    assert backup_is_usable(str(empty))[0] is False
    junk = tmp_path / "junk.xml"
    junk.write_text("<ListOfGroups><Group>", encoding="utf-8")
    ok, reason = backup_is_usable(str(junk))
    assert ok is False and "parseable" in reason


# ============================================================================================
# 3. PLAN — pure add / removal / no-op, plus the survival guarantees.
# ============================================================================================
def test_plan_add_names_the_added_account():
    new_xml, diff, summary = plan_membership_change(
        _xml(), "TIER_A", ["DU100001", "DU100002", "DU100003"])
    assert summary.added == ("DU100003",)
    assert summary.removed == ()
    assert summary.kept == ("DU100001", "DU100002")
    assert summary.changed is True
    assert "DU100003" in summary.text() and "ADDED" in summary.text()
    assert diff and "DU100003" in diff
    assert "groups.xml (current)" in diff and "after replaceFA: TIER_A" in diff
    from fa_membership import parse_group_membership
    assert parse_group_membership(new_xml)["TIER_A"] == {"DU100001", "DU100002", "DU100003"}


def test_plan_removal_names_the_removed_account():
    new_xml, diff, summary = plan_membership_change(_xml(), "TIER_A", ["DU100001"])
    assert summary.removed == ("DU100002",)
    assert summary.added == ()
    assert summary.changed is True
    assert "DU100002" in summary.text() and "REMOVED" in summary.text()
    from fa_membership import parse_group_membership
    assert parse_group_membership(new_xml)["TIER_A"] == {"DU100001"}
    assert diff


def test_plan_noop_returns_input_xml_unchanged_and_empty_diff():
    current = _xml()
    new_xml, diff, summary = plan_membership_change(
        current, "TIER_A", ["DU100002", "DU100001"])
    assert summary.changed is False
    assert summary.added == () and summary.removed == ()
    assert diff == ""
    assert new_xml == current          # byte-for-byte, no re-serialization churn
    assert new_xml is current
    assert "NO CHANGE" in summary.text()


def test_untouched_group_survives_byte_for_byte():
    """TIER_B is not named in the plan — its whole serialized subtree, members and amounts,
    must be identical after the mutation. replaceFA overwrites everything, so this is the
    load-bearing guarantee."""
    current = _xml()
    new_xml, _diff, _summary = plan_membership_change(
        current, "TIER_A", ["DU100001", "DU100003"])
    assert _group_blob(new_xml, "TIER_B") == _group_blob(current, "TIER_B")
    assert _amounts(new_xml, "TIER_B") == {"DU200001": "41", "DU200002": "59"}
    assert _group_blob(new_xml, "TIER_C") == _group_blob(current, "TIER_C")


def test_kept_members_keep_their_amounts_and_new_member_gets_placeholder():
    current = _xml()
    new_xml, _diff, summary = plan_membership_change(
        current, "TIER_A", ["DU100001", "DU100002", "DU100003"])
    amounts = _amounts(new_xml, "TIER_A")
    assert amounts["DU100001"] == "7"     # unchanged ContractsOrShares
    assert amounts["DU100002"] == "13"    # unchanged ContractsOrShares
    assert amounts["DU100003"] == "0"     # placeholder for the new member
    assert summary.added == ("DU100003",)


def test_plan_namespaced_xml_is_handled():
    new_xml, diff, summary = plan_membership_change(
        _ns_xml(), "TIER_A", ["DU100001", "DU100009"])
    assert summary.added == ("DU100009",)
    from fa_membership import parse_group_membership
    assert parse_group_membership(new_xml)["TIER_A"] == {"DU100001", "DU100009"}
    assert diff


def test_plan_unknown_group_fails_closed():
    with pytest.raises(FaGroupSyncRefused) as exc:
        plan_membership_change(_xml(), "TIER_ZZZ", ["DU100001"])
    assert "not present" in str(exc.value).lower()


def test_plan_empty_desired_refused_unless_explicit():
    with pytest.raises(FaGroupSyncRefused) as exc:
        plan_membership_change(_xml(), "TIER_A", [])
    assert "allow_empty" in str(exc.value)
    new_xml, diff, summary = plan_membership_change(
        _xml(), "TIER_A", [], allow_empty=True)
    assert summary.removed == ("DU100001", "DU100002")
    from fa_membership import parse_group_membership
    assert parse_group_membership(new_xml)["TIER_A"] == set()
    assert _amounts(new_xml, "TIER_B") == {"DU200001": "41", "DU200002": "59"}


def test_plan_blank_current_xml_fails_closed():
    with pytest.raises(FaGroupSyncRefused):
        plan_membership_change("   ", "TIER_A", ["DU100001"])


# ============================================================================================
# 4. WRITE GATES — every one of them fails closed.
# ============================================================================================
def test_apply_refuses_when_not_armed(tmp_path, armed_config):
    """READONLY/DRY_RUN are open here, so ONLY armed=False is blocking — and it blocks."""
    backup = backup_groups(_xml(), str(tmp_path))
    ib = FakeIB(_xml())
    with pytest.raises(FaGroupSyncRefused) as exc:
        apply_membership_change(ib, _xml(), armed=False, backup_path=backup)
    assert "not armed" in str(exc.value)
    assert ib.replaced == []


def test_apply_refuses_on_committed_readonly_defaults(tmp_path):
    """No monkeypatch: the committed defaults (READONLY=True, DRY_RUN=True) alone block a
    write even when the caller passes armed=True."""
    assert config.READONLY is True and config.DRY_RUN is True
    backup = backup_groups(_xml(), str(tmp_path))
    ib = FakeIB(_xml())
    with pytest.raises(FaGroupSyncRefused):
        apply_membership_change(ib, _xml(), armed=True, backup_path=backup)
    assert ib.replaced == []


def test_apply_refuses_without_backup(armed_config):
    ib = FakeIB(_xml())
    with pytest.raises(FaGroupSyncRefused) as exc:
        apply_membership_change(ib, _xml(), armed=True, backup_path="")
    assert "no usable backup" in str(exc.value)
    assert ib.replaced == []


def test_apply_refuses_with_missing_or_empty_backup_file(tmp_path, armed_config):
    ib = FakeIB(_xml())
    missing = str(tmp_path / "does_not_exist.xml")
    with pytest.raises(FaGroupSyncRefused):
        apply_membership_change(ib, _xml(), armed=True, backup_path=missing)

    empty = tmp_path / "empty.xml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(FaGroupSyncRefused):
        apply_membership_change(ib, _xml(), armed=True, backup_path=str(empty))
    assert ib.replaced == []


@pytest.mark.parametrize("payload", ["", "   ", "<ListOfGroups><Group>",
                                     "<ListOfGroups></ListOfGroups>"])
def test_apply_refuses_bad_payload(tmp_path, armed_config, payload):
    """Empty, unparseable, or group-less payloads would wipe the master. All refused."""
    backup = backup_groups(_xml(), str(tmp_path))
    ib = FakeIB(_xml())
    with pytest.raises(FaGroupSyncRefused):
        apply_membership_change(ib, payload, armed=True, backup_path=backup)
    assert ib.replaced == []


def test_apply_writes_once_when_every_gate_passes(tmp_path, armed_config):
    backup = backup_groups(_xml(), str(tmp_path))
    new_xml, _diff, _summary = plan_membership_change(
        _xml(), "TIER_A", ["DU100001", "DU100002", "DU100003"])
    ib = FakeIB(_xml())
    written = apply_membership_change(ib, new_xml, armed=True, backup_path=backup)
    assert len(ib.replaced) == 1
    kind, payload = ib.replaced[0]
    assert kind == fgs.FA_GROUPS
    assert payload == written == new_xml.strip()


# ============================================================================================
# 5. THE GLUE — end to end, still with zero broker contact.
# ============================================================================================
def test_sync_noop_writes_nothing_at_all(tmp_path, armed_config, monkeypatch):
    """Identical membership: no replaceFA AND no backup file. The safest write is none."""
    monkeypatch.setattr(fgs, "BACKUP_DIR", str(tmp_path))
    ib = FakeIB(_xml())
    result = sync_group_membership(
        ib, "TIER_A", ["DU100001", "DU100002"], armed=True)
    assert result["changed"] is False
    assert result["wrote"] is False
    assert result["diff"] == ""
    assert result["backup"] == ""
    assert ib.replaced == []
    assert os.listdir(tmp_path) == []          # not even a backup was written


def test_sync_unarmed_is_a_preview(tmp_path, monkeypatch):
    monkeypatch.setattr(fgs, "BACKUP_DIR", str(tmp_path))
    ib = FakeIB(_xml())
    result = sync_group_membership(ib, "TIER_A", ["DU100001", "DU100003"])
    assert result["changed"] is True
    assert result["wrote"] is False
    assert result["backup"] == ""
    assert result["diff"]
    assert "DU100003" in result["summary_text"]
    assert ib.replaced == []
    assert os.listdir(tmp_path) == []


def test_sync_armed_add_backs_up_writes_once_and_verifies(tmp_path, armed_config, monkeypatch):
    monkeypatch.setattr(fgs, "BACKUP_DIR", str(tmp_path))
    ib = FakeIB(_xml())
    result = sync_group_membership(
        ib, "TIER_A", ["DU100001", "DU100002", "DU100003"], armed=True)

    assert result["wrote"] is True
    assert result["verified"] is True
    assert result["summary"].added == ("DU100003",)
    assert len(ib.replaced) == 1

    # the backup landed BEFORE the write and holds the PRE-change XML
    assert os.path.isfile(result["backup"])
    with open(result["backup"], encoding="utf-8") as fh:
        assert fh.read() == _xml()

    written = ib.replaced[0][1]
    assert _amounts(written, "TIER_A") == {"DU100001": "7", "DU100002": "13", "DU100003": "0"}
    assert _group_blob(written, "TIER_B") == _group_blob(_xml(), "TIER_B")


def test_sync_armed_removal_writes_once(tmp_path, armed_config, monkeypatch):
    monkeypatch.setattr(fgs, "BACKUP_DIR", str(tmp_path))
    ib = FakeIB(_xml())
    result = sync_group_membership(ib, "TIER_A", ["DU100002"], armed=True)
    assert result["summary"].removed == ("DU100001",)
    assert result["wrote"] is True and result["verified"] is True
    assert len(ib.replaced) == 1
    assert _amounts(ib.replaced[0][1], "TIER_A") == {"DU100002": "13"}


def test_sync_empty_read_fails_closed_before_any_write(tmp_path, armed_config, monkeypatch):
    monkeypatch.setattr(fgs, "BACKUP_DIR", str(tmp_path))
    ib = FakeIB("")
    with pytest.raises(FaGroupSyncRefused):
        sync_group_membership(ib, "TIER_A", ["DU100001"], armed=True)
    assert ib.replaced == []
    assert os.listdir(tmp_path) == []


def test_sync_garbage_read_fails_closed_before_any_write(tmp_path, armed_config, monkeypatch):
    monkeypatch.setattr(fgs, "BACKUP_DIR", str(tmp_path))
    ib = FakeIB("not xml at all")
    with pytest.raises(FaGroupSyncRefused):
        sync_group_membership(ib, "TIER_A", ["DU100001"], armed=True)
    assert ib.replaced == []
    assert os.listdir(tmp_path) == []


def test_sync_verification_failure_is_loud_and_names_the_backup(
        tmp_path, armed_config, monkeypatch):
    """A master that silently did NOT apply the write must not be reported as success."""
    monkeypatch.setattr(fgs, "BACKUP_DIR", str(tmp_path))
    ib = FakeIB(_xml(), echo_writes=False)      # read-back keeps returning the OLD XML
    with pytest.raises(FaGroupSyncVerifyFailed) as exc:
        sync_group_membership(ib, "TIER_A", ["DU100001", "DU100003"], armed=True)
    assert len(ib.replaced) == 1                # the write did happen
    assert exc.value.result["backup"] in str(exc.value)
    assert exc.value.result["wrote"] is True
    assert exc.value.result["verified"] is False
