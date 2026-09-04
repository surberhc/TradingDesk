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
import fa_membership
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
    # 1, not 0: IBKR rejects a ContractsOrShares group whose members are all allocated zero
    # -- [10229] FA data saving error: Invalid Group <name>, captured live 2026-09-04. The
    # placeholder is overwritten by the real split before any order is placed.
    assert amounts["DU100003"] == "1"     # placeholder for the new member
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
    # new member placeholder is 1, not 0 -- see the note above; IBKR rejects an all-zero
    # ContractsOrShares group with [10229] Invalid Group.
    assert _amounts(written, "TIER_A") == {"DU100001": "7", "DU100002": "13", "DU100003": "1"}
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


# ========================================================================================
# GROUP CREATION (owner decision 2026-09-03). Groups are not standing furniture: every armed
# run creates its OWN group per TICKER AND SIDE, uploads the membership computed from the CRM
# at that moment, trades it, and leaves it behind as the audit record of who was in that
# trade. A per-ticker group is what lets every account across EVERY model fill that ticker at
# ONE average price. Fixtures below are shaped like the REAL master F6795549 XML, which was
# read live on 2026-09-03 and round-trips through parse_group_membership as a no-op.
# ========================================================================================
REAL_SHAPE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<ListOfGroups>"
    "<Group><name>Main</name><defaultMethod>NetLiq</defaultMethod>"
    '<ListOfAccts varName="list">'
    "<Account><acct>U1111111</acct></Account>"
    "<Account><acct>U2222222</acct></Account>"
    "</ListOfAccts></Group>"
    "<Group><name>Ted</name><defaultMethod>NetLiq</defaultMethod>"
    '<ListOfAccts varName="list">'
    "<Account><acct>U3333333</acct></Account>"
    "</ListOfAccts></Group>"
    "</ListOfGroups>"
)


def _members(xml):
    return fa_membership.parse_group_membership(xml)


def test_create_a_new_group_leaves_every_existing_group_untouched():
    before = _members(REAL_SHAPE)
    new_xml, diff, summary = fgs.plan_group_creation(
        REAL_SHAPE, "XLE BUY 20260904-0931", ["U4444444", "U5555555"])
    after = _members(new_xml)
    assert len(after) == len(before) + 1
    assert set(after["XLE BUY 20260904-0931"]) == {"U4444444", "U5555555"}
    for name, mem in before.items():
        assert after[name] == mem, f"pre-existing group {name} was modified"
    assert summary.added == ("U4444444", "U5555555")
    assert summary.removed == ()
    assert summary.changed
    assert diff.strip()


def test_creation_refuses_a_name_that_already_exists():
    with pytest.raises(fgs.FaGroupSyncRefused) as e:
        fgs.plan_group_creation(REAL_SHAPE, "Main", ["U4444444"])
    assert "ALREADY EXISTS" in str(e.value)


def test_creation_refuses_an_empty_membership_with_no_escape_hatch():
    """plan_membership_change has allow_empty; creation deliberately does NOT - a run group
    with nobody in it is never what the caller meant."""
    with pytest.raises(fgs.FaGroupSyncRefused) as e:
        fgs.plan_group_creation(REAL_SHAPE, "XLE BUY 1", [])
    assert "EMPTY" in str(e.value)


def test_creation_refuses_blank_name_blank_and_unparseable_xml():
    for args in ((REAL_SHAPE, "   ", ["U4444444"]),
                 ("", "XLE BUY 1", ["U4444444"]),
                 ("<not xml", "XLE BUY 1", ["U4444444"])):
        with pytest.raises(fgs.FaGroupSyncRefused):
            fgs.plan_group_creation(*args)


def test_creation_is_pure_and_does_not_mutate_the_input_xml():
    snapshot = REAL_SHAPE
    fgs.plan_group_creation(REAL_SHAPE, "XLE BUY 1", ["U4444444"])
    assert REAL_SHAPE == snapshot


def test_created_group_clones_the_real_element_shape():
    """The new Group is a deep copy of a real one, so layout, namespace and the ListOfAccts
    varName attribute all match what IBKR itself sent."""
    new_xml, _, _ = fgs.plan_group_creation(REAL_SHAPE, "XLE BUY 1", ["U4444444"])
    root = ET.fromstring(new_xml)
    grp = [g for g in root.iter()
           if fa_membership._local(g.tag) == "group"
           and fa_membership._group_name(g) == "XLE BUY 1"][0]
    loa = fa_membership._find_child(grp, "listofaccts")
    assert loa is not None
    assert loa.attrib.get("varName") == "list"
    # ContractsOrShares, not NetLiq: IBKR refuses a newly created group with NetLiq --
    #   [10260] Group <name> has unsupported method (NetLiq)
    # captured live 2026-09-04. It is also the method every block actually writes.
    assert fa_membership._find_child(grp, "defaultmethod").text == "ContractsOrShares"


def test_run_group_name_strips_characters_the_live_master_has_never_shown_us():
    """Parens and commas appear in every custom model name and are NOT proven at IBKR; a name
    it silently mangles becomes a group the block rail cannot find."""
    assert fgs.run_group_name("Growth (Small, Custom)", "20260904-0931") ==         "Growth Small Custom 20260904-0931"
    assert fgs.run_group_name("XLE BUY", "20260904-0931") == "XLE BUY 20260904-0931"


def test_run_group_name_requires_both_a_label_and_a_stamp():
    with pytest.raises(fgs.FaGroupSyncRefused):
        fgs.run_group_name("XLE BUY", "")
    with pytest.raises(fgs.FaGroupSyncRefused):
        fgs.run_group_name("(),", "20260904")


def test_two_runs_on_the_same_day_get_different_group_names():
    a = fgs.run_group_name("XLE BUY", "20260904-0931")
    b = fgs.run_group_name("XLE BUY", "20260904-1416")
    assert a != b, "the run stamp is what makes a group an audit record of ONE run"


# ========================================================================================
# create_run_group - the ARMED creation chain. Same safe order as sync_group_membership:
# read -> plan -> MANDATORY backup -> arm-gated replaceFA -> read-back. The read-back is
# stricter than the membership chain because replaceFA overwrites the WHOLE document and the
# live master carries eight groups this desk does not own.
# ========================================================================================
def test_create_run_group_unarmed_is_a_preview_that_writes_nothing(tmp_path):
    ib = FakeIB(_xml())
    res = fgs.create_run_group(ib, "XLE BUY 20260904-0931", ["DU100003"],
                               armed=False, backup_path=str(tmp_path))
    assert res["wrote"] is False
    assert res["created"] is False
    assert res["backup"] == ""
    assert res["refused_reason"]
    assert ib.replaced == [], "an unarmed preview must never call replaceFA"
    assert res["diff"].strip(), "the operator still gets a reviewable diff"


def test_create_run_group_armed_writes_and_verifies(tmp_path, armed_config):
    ib = FakeIB(_xml())
    res = fgs.create_run_group(ib, "XLE BUY 20260904-0931", ["DU100003", "DU100004"],
                               armed=True, backup_path=str(tmp_path))
    assert res["wrote"] is True
    assert res["verified"] is True
    assert res["backup"], "a backup is mandatory before any write"
    assert len(ib.replaced) == 1
    after = fa_membership.parse_group_membership(ib.replaced[0][1])
    assert after["XLE BUY 20260904-0931"] == {"DU100003", "DU100004"}
    assert res["groups_before"] == 3


def test_create_run_group_leaves_the_groups_it_does_not_own_alone(tmp_path, armed_config):
    before = fa_membership.parse_group_membership(_xml())
    ib = FakeIB(_xml())
    fgs.create_run_group(ib, "XLE BUY 1", ["DU100003"], armed=True, backup_path=str(tmp_path))
    after = fa_membership.parse_group_membership(ib.replaced[0][1])
    for name, members in before.items():
        assert after[name] == members, f"{name} must be untouched by a creation"
    assert len(after) == len(before) + 1


def test_create_run_group_preserves_existing_amounts(tmp_path, armed_config):
    """TIER_A carries distinct ContractsOrShares amounts; creation must not disturb them."""
    ib = FakeIB(_xml())
    fgs.create_run_group(ib, "XLE BUY 1", ["DU100003"], armed=True, backup_path=str(tmp_path))
    assert _amounts(ib.replaced[0][1], "TIER_A") == _amounts(_xml(), "TIER_A")


def test_create_run_group_refuses_a_name_that_already_exists_before_any_write(tmp_path,
                                                                             armed_config):
    ib = FakeIB(_xml())
    with pytest.raises(fgs.FaGroupSyncRefused) as e:
        fgs.create_run_group(ib, "TIER_A", ["DU100003"], armed=True, backup_path=str(tmp_path))
    assert "ALREADY EXISTS" in str(e.value)
    assert ib.replaced == [], "the refusal must land before replaceFA, not after"


def test_create_run_group_refuses_an_empty_membership_before_any_write(tmp_path, armed_config):
    ib = FakeIB(_xml())
    with pytest.raises(fgs.FaGroupSyncRefused):
        fgs.create_run_group(ib, "XLE BUY 1", [], armed=True, backup_path=str(tmp_path))
    assert ib.replaced == []


def test_create_run_group_raises_when_the_read_back_membership_is_wrong(tmp_path,
                                                                       armed_config):
    """The write happened; the master is UNVERIFIED and the caller is told to restore."""
    ib = FakeIB(_xml(), echo_writes=False)   # read-back returns the ORIGINAL, no new group
    with pytest.raises(fgs.FaGroupSyncVerifyFailed) as e:
        fgs.create_run_group(ib, "XLE BUY 1", ["DU100003"], armed=True,
                             backup_path=str(tmp_path))
    assert "UNVERIFIED" in str(e.value)
    assert ib.replaced, "the write DID happen - that is why this is a verify failure"


def test_create_run_group_catches_a_read_back_that_lost_another_group(tmp_path, armed_config):
    """Creation must disturb nothing else - and prove it after the write, not just before."""
    stripped = (
        "<ListOfGroups>"
        "  <Group><name>TIER_A</name><defaultMethod>ContractsOrShares</defaultMethod>"
        "    <ListOfAccts>"
        "      <Account><acct>DU100001</acct><amount>7</amount></Account>"
        "      <Account><acct>DU100002</acct><amount>13</amount></Account>"
        "    </ListOfAccts></Group>"
        "  <Group><name>XLE BUY 1</name><defaultMethod>NetLiq</defaultMethod>"
        "    <ListOfAccts>"
        "      <Account><acct>DU100003</acct><amount>0</amount></Account>"
        "    </ListOfAccts></Group>"
        "</ListOfGroups>")
    ib = FakeIB(_xml(), stripped, echo_writes=False)
    with pytest.raises(fgs.FaGroupSyncVerifyFailed) as e:
        fgs.create_run_group(ib, "XLE BUY 1", ["DU100003"], armed=True,
                             backup_path=str(tmp_path))
    msg = str(e.value)
    assert "UNVERIFIED" in msg
    assert "restore the backup" in msg


def test_create_run_group_refuses_the_write_on_committed_readonly_defaults(tmp_path):
    """No monkeypatch: the on-disk defaults alone stop a write even with armed=True."""
    assert config.READONLY is True and config.DRY_RUN is True
    ib = FakeIB(_xml())
    res = fgs.create_run_group(ib, "XLE BUY 1", ["DU100003"], armed=True,
                               backup_path=str(tmp_path))
    assert res["wrote"] is False
    assert ib.replaced == []


# ========================================================================================
# BOUNDED RETRY ON AN EMPTY GROUPS READ (2026-09-04). requestFA can TIME OUT and hand back
# None/"" rather than raising. A timed-out read is not information - but it must still refuse
# if every attempt comes back empty, because replaceFA overwrites the whole document.
# ========================================================================================
class _FlakyIB:
    """Returns empty `empties` times, then the real XML."""

    def __init__(self, empties, xml):
        self.empties = empties
        self.xml = xml
        self.calls = 0

    def requestFA(self, kind):
        self.calls += 1
        if self.calls <= self.empties:
            return ""
        return self.xml

    def sleep(self, secs):
        pass


def test_a_single_timed_out_read_is_retried_and_succeeds():
    ib = _FlakyIB(1, _xml())
    got = fgs.read_live_groups(ib)
    assert "TIER_A" in got
    assert ib.calls == 2, "it must actually retry, not succeed by accident"


def test_it_retries_up_to_the_bound_then_still_refuses():
    ib = _FlakyIB(99, _xml())
    with pytest.raises(fgs.FaGroupSyncRefused) as e:
        fgs.read_live_groups(ib)
    assert ib.calls == fgs.FA_READ_ATTEMPTS
    assert "EMPTY response" in str(e.value)
    assert "clobber every group" in str(e.value)


def test_a_read_that_RAISES_is_never_retried_away():
    """A raising read is a real answer. Only an empty one is treated as a timed-out read."""
    class _Raiser:
        calls = 0
        def requestFA(self, kind):
            _Raiser.calls += 1
            raise RuntimeError("connection reset")
    with pytest.raises(fgs.FaGroupSyncRefused):
        fgs.read_live_groups(_Raiser())
    assert _Raiser.calls == 1


def test_a_group_less_document_is_still_refused_and_not_retried():
    class _Empty:
        calls = 0
        def requestFA(self, kind):
            _Empty.calls += 1
            return "<ListOfGroups></ListOfGroups>"
    with pytest.raises(fgs.FaGroupSyncRefused) as e:
        fgs.read_live_groups(_Empty())
    assert "NO <Group> elements" in str(e.value)
    assert _Empty.calls == 1, "a parseable answer is an answer - do not retry it"


def test_an_ib_without_sleep_does_not_break_the_retry():
    class _NoSleep:
        calls = 0
        def requestFA(self, kind):
            _NoSleep.calls += 1
            return "" if _NoSleep.calls == 1 else _xml()
    assert "TIER_A" in fgs.read_live_groups(_NoSleep())
