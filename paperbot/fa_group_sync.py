"""
fa_group_sync.py — the LIVE GLUE for FA group-MEMBERSHIP sync (conductor A.2).

WHAT THIS IS
------------
``fa_membership.py`` is the PURE half: it parses a GROUPS XML string, computes a membership
delta, and produces a NEW GROUPS XML string. Its docstring said the LIVE glue —

    requestFA(1) -> parse_group_membership -> membership_diff vs the desired membership
                 -> backup -> apply_membership -> replaceFA(1), behind the arm gate

— was NOT built. This module is exactly that glue, and nothing more.

OWNER DECISION (Andrew, 2026-08-25): the DESK writes the FA group configuration itself and
the owner approves the DIFF before it is written. The group config is NOT hand-built at IBKR.
So the write path exists — behind a mandatory backup and a human-reviewed diff.

WHY THIS IS DANGEROUS (read before touching anything here)
----------------------------------------------------------
``replaceFA(1, xml)`` OVERWRITES THE ENTIRE GROUPS XML ON THE FA MASTER — not just the group
being edited. A bad write clobbers EVERY group on that master: their membership, their
allocation method, and every account's ContractsOrShares amount. There is no partial write and
no undo. The only recovery is restoring the backup this module takes first.

The guard shape is deliberately the SAME one ``live_fa_block_execute.py`` already proved for
the ContractsOrShares write (its §"GROUP WRITE" block) — one convention, not two:

  1. MANDATORY timestamped BACKUP of the current groups XML BEFORE any write. The write
     function refuses unless a backup file exists, is non-empty, is readable, and PARSES.
  2. The PURE mutation preserves every OTHER group byte-for-byte, and this module ASSERTS
     that invariant on the computed XML before the diff is even shown (belt and braces —
     it is the whole-XML overwrite that makes the invariant load-bearing).
  3. FAIL CLOSED everywhere: a blank/unparseable requestFA response, a missing group, a
     missing ListOfAccts, a group-less XML, an unparseable computed XML, a missing backup, or
     an unarmed session — every one REFUSES. An empty read is NEVER read as "the master has
     no groups" (the trap ``fa_membership.parse_group_membership`` documents); it is read as
     "we do not know the current state, therefore write NOTHING".
  4. A reviewable UNIFIED DIFF (``fa_membership.membership_diff_text``) with the same file
     labels the block executor uses, plus a summary naming EXACTLY which accounts are ADDED
     and which are REMOVED, so the owner approves a specific change and not a vibe.
  5. The ARM GATE. ``apply_membership_change`` is the ONLY function here that calls
     replaceFA, and it delegates the code gate to the canonical ``order_router.transmit_guard``
     (permitted iff READONLY=False AND DRY_RUN=False AND armed=True). Committed on-disk
     defaults are READONLY/DRY_RUN True, so an un-flipped process CANNOT write.

A NO-OP WRITES NOTHING. If the desired membership already equals the live membership, this
module takes no backup, computes no new XML, and never calls replaceFA. The safest write is
the one that does not happen.

WHERE THE DESIRED MEMBERSHIP COMES FROM
---------------------------------------
The CRM. ``roster.enrolled_roster()`` (CRM view ``v_tradingdesk_roster``, Andrew's book,
intersected with funded reality) is the blessed set; ``crm.brain.CRMBrain.group_membership()``
maps it per FA group. This module takes the desired set as a PARAMETER and never reaches into
the CRM itself — the edges stay pure and testable, and the account wall stays the caller's job.

NOT WIRED YET (deliberate)
--------------------------
This module is standalone and importable. Wiring it into the execution path (a preview/arm CLI,
the nightly run, the ledger) is a SEPARATE pass. Nothing here is scheduled and nothing here
self-arms.

Read-only usage (safe anywhere):
    xml = read_live_groups(ib)
    new_xml, diff, summary = plan_membership_change(xml, "TIER_A", desired_accounts)
    print(summary.text()); print(diff)      # <- what the owner approves
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

import config           # noqa: E402  (STATE_DIR — same backup root as rebalance_execute)
import fa_membership    # noqa: E402  (the PURE half — parse / diff / apply / render)
import order_router     # noqa: E402  (transmit_guard — the ONE definition of the code gate)

# GROUPS is faDataType 1 for both requestFA and replaceFA (same constant the proven
# rebalance_execute.set_group_contracts_or_shares uses).
FA_GROUPS = 1

# Same backup root as rebalance_execute._BACKUP_DIR so every FA snapshot lands in ONE place.
BACKUP_DIR = os.path.join(config.STATE_DIR, "fa_backups")


class FaGroupSyncRefused(RuntimeError):
    """A gate refused. NOTHING was read-modified-written. Always fail closed with this."""


class FaGroupSyncVerifyFailed(RuntimeError):
    """The post-write read-back did NOT match the approved membership. The write DID happen;
    the master is in an unverified state. The message carries the backup path to restore from.
    ``.result`` holds the result dict of the attempted sync."""

    def __init__(self, message: str, result: dict | None = None):
        super().__init__(message)
        self.result = result or {}


# ============================================================================================
# SUMMARY — the human-approvable statement of the change.
# ============================================================================================
@dataclass(frozen=True)
class MembershipSummary:
    """Exactly WHO joins and WHO leaves one FA group. Plain data; no broker, no I/O."""

    fa_group: str
    added: tuple[str, ...]
    removed: tuple[str, ...]
    kept: tuple[str, ...]

    @property
    def changed(self) -> bool:
        """True iff this is a real change. False => the caller must write NOTHING."""
        return bool(self.added or self.removed)

    def text(self) -> str:
        """One human-readable block naming every added and removed account. Plain English —
        no shorthand — because this is what the owner reads before approving a write."""
        if not self.changed:
            return (f"FA group '{self.fa_group}': NO CHANGE — the live membership already "
                    f"matches the desired membership ({len(self.kept)} accounts). "
                    f"Nothing will be written.")
        lines = [f"FA group '{self.fa_group}': membership change"]
        lines.append(f"  ACCOUNTS TO BE ADDED ({len(self.added)}): "
                     + (", ".join(self.added) if self.added else "none"))
        lines.append(f"  ACCOUNTS TO BE REMOVED ({len(self.removed)}): "
                     + (", ".join(self.removed) if self.removed else "none"))
        lines.append(f"  ACCOUNTS UNCHANGED ({len(self.kept)}): "
                     + (", ".join(self.kept) if self.kept else "none"))
        return "\n".join(lines)


# ============================================================================================
# 1. READ — requestFA(1), fail closed on anything we cannot trust.
# ============================================================================================
# An empty requestFA response is a TIMED-OUT read, not an empty master, so it is retried a
# bounded number of times before the refusal below stands. See the note inside read_live_groups.
FA_READ_ATTEMPTS = 3
FA_READ_RETRY_SECS = 2.0


def read_live_groups(ib) -> str:
    """Read the FULL live GROUPS XML with ``requestFA(1)`` and return it as a string.

    FAILS CLOSED (raises FaGroupSyncRefused) on every condition where we do not KNOW the
    master's current state:
      * no connection object / no requestFA method,
      * requestFA raising,
      * an empty / whitespace-only response,
      * an unparseable response,
      * a parseable response containing NO <Group> at all.

    That last one matters: an empty or group-less read must NEVER be interpreted as "the master
    genuinely has no groups" and used as the base for a whole-XML overwrite — that is precisely
    how replaceFA clobbers a live book. It means "write nothing".

    Read-only: requestFA transmits no order and changes no config.
    """
    if ib is None or not hasattr(ib, "requestFA"):
        raise FaGroupSyncRefused(
            "read_live_groups: no gateway connection with requestFA — cannot read the live "
            "FA groups. FAILING CLOSED (nothing read, nothing written).")
    # BOUNDED RETRY ON AN EMPTY READ. ib_async's requestFA can TIME OUT and hand back None or
    # "" rather than raising - observed live on 2026-09-03 ("requestFAAsync: Timeout") and
    # again on 2026-09-04, when a Group trade run was refused mid-flight on an empty read while
    # the very same call from a fresh connection returned 17,697 characters seconds later.
    # A timed-out read is not information; retrying it is not a weakening of the gate, because
    # an empty result after every attempt STILL refuses below exactly as before. What must
    # never be retried away is a genuine answer - so a requestFA that RAISES is still an
    # immediate refusal, and a parseable-but-group-less document is still refused.
    raw = None
    last_exc = None
    for attempt in range(FA_READ_ATTEMPTS):
        try:
            raw = ib.requestFA(FA_GROUPS)
        except Exception as exc:  # noqa: BLE001 — a raising read is a refusal, never a guess
            raise FaGroupSyncRefused(
                f"read_live_groups: requestFA({FA_GROUPS}) failed ({exc!r}). FAILING CLOSED — "
                f"the current groups XML is UNKNOWN, so nothing may be written.") from exc
        if str(raw or "").strip():
            break
        last_exc = f"attempt {attempt + 1} of {FA_READ_ATTEMPTS} returned an empty response"
        if attempt + 1 < FA_READ_ATTEMPTS:
            try:
                ib.sleep(FA_READ_RETRY_SECS)
            except Exception:  # noqa: BLE001 — a fake//offline ib in tests has no sleep
                pass

    xml = str(raw or "").strip()
    if not xml:
        raise FaGroupSyncRefused(
            f"read_live_groups: requestFA({FA_GROUPS}) returned an EMPTY response. This is "
            f"NOT 'the master has no groups' — it is 'the current state is unknown'. FAILING "
            f"CLOSED (replaceFA overwrites the WHOLE groups XML; writing off an empty read "
            f"would clobber every group). Retried {FA_READ_ATTEMPTS} time(s); {last_exc}.")

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise FaGroupSyncRefused(
            f"read_live_groups: requestFA({FA_GROUPS}) returned UNPARSEABLE XML ({exc}). "
            f"FAILING CLOSED — nothing written.") from exc

    n_groups = sum(1 for el in root.iter() if el.tag.split("}")[-1].lower() == "group")
    if n_groups == 0:
        raise FaGroupSyncRefused(
            f"read_live_groups: requestFA({FA_GROUPS}) returned XML with NO <Group> elements. "
            f"Refusing to treat that as an authoritative empty group set. FAILING CLOSED — "
            f"if the master truly has no groups, group CREATION is a separate admin step.")

    return xml


# ============================================================================================
# 2. BACKUP — mandatory, verified, before anything else.
# ============================================================================================
def backup_groups(xml: str, path: str | None = None) -> str:
    """Write the CURRENT groups XML to a timestamped backup file and return its path.

    ``path`` None -> ``<STATE_DIR>/fa_backups/fa_groups_backup_<YYYYmmdd_HHMMSS>.xml`` (the
    same root and naming as ``rebalance_execute.backup_fa_groups``). A given ``path`` is used
    verbatim; a path that is an existing DIRECTORY gets the timestamped filename inside it.

    The file is READ BACK and compared to what we meant to write — a backup that did not
    actually land is treated as NO backup (raises FaGroupSyncRefused). Refuses an empty ``xml``
    outright: an empty backup protects nothing.

    This must run BEFORE any replaceFA. ``apply_membership_change`` independently re-checks the
    backup, so a write is impossible unless a real backup exists on disk.
    """
    content = str(xml or "")
    if not content.strip():
        raise FaGroupSyncRefused(
            "backup_groups: refusing to write an EMPTY backup — an empty backup protects "
            "nothing, and no write may proceed without a real one. FAILING CLOSED.")

    if path is None or os.path.isdir(path):
        directory = BACKUP_DIR if path is None else path
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(directory, f"fa_groups_backup_{stamp}.xml")

    parent = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as exc:
        raise FaGroupSyncRefused(
            f"backup_groups: could NOT write the backup to {path!r} ({exc}). No backup means "
            f"no write. FAILING CLOSED.") from exc

    ok, reason = backup_is_usable(path)
    if not ok:
        raise FaGroupSyncRefused(
            f"backup_groups: the backup at {path!r} is not usable after writing ({reason}). "
            f"FAILING CLOSED.")

    try:
        with open(path, "r", encoding="utf-8") as fh:
            written = fh.read()
    except OSError as exc:
        raise FaGroupSyncRefused(
            f"backup_groups: the backup at {path!r} could not be read back ({exc}). "
            f"FAILING CLOSED.") from exc
    if written != content:
        raise FaGroupSyncRefused(
            f"backup_groups: the backup at {path!r} does NOT match the XML it was given "
            f"(wrote {len(content)} chars, read back {len(written)}). FAILING CLOSED.")

    return path


def backup_is_usable(path: str | None) -> tuple[bool, str]:
    """(ok, reason) — is ``path`` a real, non-empty, readable, PARSEABLE groups-XML backup?

    Used by ``apply_membership_change`` as the hard precondition for any replaceFA. Pure check;
    never raises. Fails closed: anything unexpected -> (False, reason).
    """
    if not path or not str(path).strip():
        return False, "no backup path given"
    try:
        if not os.path.isfile(path):
            return False, f"backup path {path!r} is not an existing file"
        if os.path.getsize(path) <= 0:
            return False, f"backup file {path!r} is EMPTY"
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except OSError as exc:
        return False, f"backup file {path!r} is not readable ({exc})"
    if not content.strip():
        return False, f"backup file {path!r} contains only whitespace"
    try:
        ET.fromstring(content.strip())
    except ET.ParseError as exc:
        return False, f"backup file {path!r} is not parseable XML ({exc})"
    return True, "backup present, non-empty, readable and parseable"


# ============================================================================================
# 3. PLAN — PURE. The reviewable change: new XML + unified diff + who is added/removed.
# ============================================================================================
RUN_GROUP_SAFE = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -"


def run_group_name(label: str, run_stamp: str) -> str:
    """The name of the throwaway FA group ONE armed run creates for ONE tradeable unit.

    ``label`` is what the group trades - owner decision 2026-09-03 makes that ONE TICKER AND
    SIDE ("XLE BUY"), not one model. An API order is always for a single contract, and IBKR
    exposes no call that rebalances a group to target percentages (verified against our own
    docs/MODEL_PORTFOLIO_RESEARCH.md and the TWS API FA page: the allocation methods are
    EqualQuantity / NetLiq / AvailableEquity / PctChange / ContractsOrShares, and there is no
    rebalance verb). So a rebalance is N block orders, one per ticker, and a per-ticker group
    lets every account across EVERY model fill that ticker at ONE average price - which is the
    whole point of trading blocks rather than per account.

    OWNER DECISION 2026-09-03. Groups are not standing furniture: every armed run creates its
    own group, uploads the membership read from the CRM at that moment, trades it, and leaves
    it behind as the audit record of exactly who was in that trade. So the name has to carry
    BOTH the model and the run.

    CHARACTER SET IS DELIBERATELY NARROW. The live master (F6795549) carries names like
    "Dougs Group" and "MainSmall", so letters, digits and spaces are PROVEN to work.
    Parentheses and commas - which every custom model name contains - are NOT proven, and a
    name IBKR silently mangles would produce a group the block rail then cannot find.
    Everything outside :data:`RUN_GROUP_SAFE` is dropped and runs of spaces collapsed, so
    ``Growth (Small, Custom)`` becomes ``Growth Small Custom``. The full label is recorded in
    the ledger, so nothing is lost by narrowing the name here.
    """
    m = "".join(ch for ch in str(label or "") if ch in RUN_GROUP_SAFE)
    m = " ".join(m.split())
    stamp = "".join(ch for ch in str(run_stamp or "") if ch in RUN_GROUP_SAFE).strip()
    if not m:
        raise FaGroupSyncRefused(
            f"run_group_name: label {label!r} reduces to an empty group name once unproven "
            f"characters are dropped. FAILING CLOSED.")
    if not stamp:
        raise FaGroupSyncRefused(
            "run_group_name: no run stamp given - a run group MUST be identifiable to its "
            "run or it is not an audit record. FAILING CLOSED.")
    return f"{m} {stamp}"


def plan_group_creation(
    current_xml: str,
    fa_group: str,
    desired_accounts: Iterable[str],
    *,
    # ContractsOrShares, NOT NetLiq. IBKR refuses NetLiq on a newly created group:
    #   [10260] Group <name> has unsupported method (NetLiq)
    # (captured live 2026-09-04 once replaceFA errors were finally being read). It is
    # also what we actually use -- every block writes an explicit ContractsOrShares
    # split before it places, so NetLiq was only ever a vestigial default.
    default_method: str = "ContractsOrShares",
    # 1, NOT 0. A ContractsOrShares group whose every account is allocated ZERO shares
    # is rejected outright: [10229] FA data saving error: Invalid Group <name>
    # (captured live 2026-09-04). This is only a placeholder -- the real per-account
    # split is written by set_group_contracts_or_shares immediately before the order.
    new_member_amount: int = 1,
) -> tuple[str, str, MembershipSummary]:
    """PURE (no broker, no disk): plan CREATING ``fa_group`` holding exactly
    ``desired_accounts``, against ``current_xml``.

    The creation counterpart to :func:`plan_membership_change`, and deliberately a SEPARATE
    function rather than a flag on it: editing a group that exists and bringing a new one into
    existence are different acts with different failure modes, and the refusal that protects
    each is the inverse of the other. plan_membership_change refuses a name that is ABSENT;
    this refuses a name that is PRESENT.

    Returns ``(new_xml, diff_text, summary)`` with the same shape as plan_membership_change;
    ``summary.added`` is the full membership, ``summary.removed`` is empty.

    FAILS CLOSED (FaGroupSyncRefused) on: a blank name, an empty desired set (an empty group
    is never what the caller meant, and there is no allow_empty escape here), blank or
    unparseable ``current_xml``, a name that already exists, or a computed payload that
    changed ANY pre-existing group or did not land on exactly the desired membership.
    """
    group = str(fa_group or "").strip()
    if not group:
        raise FaGroupSyncRefused("plan_group_creation: no FA group name given. FAILING CLOSED.")

    desired = {str(a).strip() for a in (desired_accounts or []) if str(a).strip()}
    if not desired:
        raise FaGroupSyncRefused(
            f"plan_group_creation: the membership for new group {group!r} is EMPTY. A run "
            f"group with nobody in it is never what the caller meant, and an empty roster read "
            f"must never produce one. FAILING CLOSED.")

    current = str(current_xml or "").strip()
    if not current:
        raise FaGroupSyncRefused(
            "plan_group_creation: empty current groups XML - the live state is UNKNOWN, so no "
            "replaceFA payload may be computed. FAILING CLOSED.")

    try:
        before = fa_membership.parse_group_membership(current)
    except ET.ParseError as exc:
        raise FaGroupSyncRefused(
            f"plan_group_creation: current groups XML is unparseable ({exc}). "
            f"FAILING CLOSED.") from exc

    if group in before:
        raise FaGroupSyncRefused(
            f"plan_group_creation: FA group {group!r} ALREADY EXISTS. This function creates a "
            f"NEW group; changing an existing membership is plan_membership_change. A run "
            f"group name that collides means the run stamp is not unique - fix that rather "
            f"than reusing the group. FAILING CLOSED.")

    try:
        new_xml = fa_membership.create_group(
            current, group, sorted(desired),
            default_method=default_method, new_member_amount=new_member_amount)
    except ValueError as exc:
        raise FaGroupSyncRefused(f"plan_group_creation: {exc}") from exc

    _assert_only_new_group_added(current, new_xml, group, desired, before)

    diff_text = fa_membership.membership_diff_text(current, new_xml, label=group)
    summary = MembershipSummary(
        fa_group=group, added=tuple(sorted(desired)), removed=(), kept=())
    return new_xml, diff_text, summary


def _assert_only_new_group_added(current_xml, new_xml, group, desired, before) -> None:
    """Load-bearing invariant on the COMPUTED creation payload, before any human sees the diff.

    replaceFA overwrites the WHOLE document, so a creation payload has to be proven to (a)
    parse, (b) contain exactly one more group than before, (c) land the new group on exactly
    the desired membership, and (d) leave every pre-existing group byte-identical. Any failure
    raises rather than returning a payload the caller might write.
    """
    try:
        after = fa_membership.parse_group_membership(new_xml)
    except ET.ParseError as exc:
        raise FaGroupSyncRefused(
            f"plan_group_creation: the COMPUTED payload does not parse ({exc}). Refusing to "
            f"hand back an unwritable payload. FAILING CLOSED.") from exc

    if group not in after:
        raise FaGroupSyncRefused(
            f"plan_group_creation: the computed payload does not contain the new group "
            f"{group!r}. FAILING CLOSED.")
    if set(after[group]) != set(desired):
        raise FaGroupSyncRefused(
            f"plan_group_creation: the computed payload does not land {group!r} on the desired "
            f"membership (wanted {len(desired)}, got {len(after[group])}). FAILING CLOSED.")
    if len(after) != len(before) + 1:
        raise FaGroupSyncRefused(
            f"plan_group_creation: the computed payload changed the GROUP COUNT by "
            f"{len(after) - len(before)}, expected exactly +1. FAILING CLOSED.")
    for name, members in before.items():
        if set(after.get(name, ())) != set(members):
            raise FaGroupSyncRefused(
                f"plan_group_creation: the computed payload MODIFIED pre-existing group "
                f"{name!r}. A creation must touch nothing else. FAILING CLOSED.")


def plan_membership_change(
    current_xml: str,
    fa_group: str,
    desired_accounts: Iterable[str],
    *,
    # 1, NOT 0. A ContractsOrShares group whose every account is allocated ZERO shares
    # is rejected outright: [10229] FA data saving error: Invalid Group <name>
    # (captured live 2026-09-04). This is only a placeholder -- the real per-account
    # split is written by set_group_contracts_or_shares immediately before the order.
    new_member_amount: int = 1,
    allow_empty: bool = False,
) -> tuple[str, str, MembershipSummary]:
    """PURE (no broker, no disk): plan setting ``fa_group``'s membership to EXACTLY
    ``desired_accounts`` against ``current_xml``.

    Returns ``(new_xml, diff_text, summary)``:
      * ``new_xml``   — the FULL groups XML a replaceFA would write. On a NO-OP this is the
        ``current_xml`` argument returned BYTE-FOR-BYTE UNCHANGED, so ``new_xml is/== current_xml``
        is a reliable "there is nothing to write" test (no re-serialization churn).
      * ``diff_text`` — the reviewable unified diff over pretty-printed XML, with the same file
        labels live_fa_block_execute uses. EMPTY STRING on a no-op.
      * ``summary``   — a MembershipSummary naming exactly the ADDED and REMOVED accounts.

    Delegates all XML work to the PURE helpers in ``fa_membership`` (parse_group_membership /
    apply_membership / membership_diff_text) — it does not re-implement any of it. Existing
    members keep their ``<amount>`` (ContractsOrShares) values; a newly added member gets the
    ``new_member_amount`` placeholder (real sizing is written per-order later by
    rebalance_execute.set_group_contracts_or_shares).

    FAILS CLOSED (FaGroupSyncRefused) on: blank/unparseable ``current_xml``, a blank
    ``fa_group``, a group not present in the XML (creation is a separate admin step), an empty
    desired set unless ``allow_empty=True`` (emptying a live group is drastic and must be
    stated explicitly), or a computed XML that touches ANY other group / does not land on the
    desired membership.
    """
    group = str(fa_group or "").strip()
    if not group:
        raise FaGroupSyncRefused("plan_membership_change: no FA group name given. FAILING CLOSED.")

    desired = {str(a).strip() for a in (desired_accounts or []) if str(a).strip()}
    if not desired and not allow_empty:
        raise FaGroupSyncRefused(
            f"plan_membership_change: the desired membership for '{group}' is EMPTY. Emptying "
            f"a live FA group is drastic and is refused unless the caller passes "
            f"allow_empty=True. FAILING CLOSED (an empty roster read must never silently "
            f"clear a group).")

    current = str(current_xml or "").strip()
    if not current:
        raise FaGroupSyncRefused(
            "plan_membership_change: empty current groups XML — the live state is UNKNOWN, so "
            "no replaceFA payload may be computed. FAILING CLOSED.")

    try:
        current_membership = fa_membership.parse_group_membership(current)
    except ET.ParseError as exc:
        raise FaGroupSyncRefused(
            f"plan_membership_change: current groups XML is unparseable ({exc}). "
            f"FAILING CLOSED.") from exc

    if group not in current_membership:
        raise FaGroupSyncRefused(
            f"plan_membership_change: FA group '{group}' is NOT present in the live groups XML "
            f"(present: {sorted(current_membership)}). Refusing to invent a group — creation is "
            f"a separate admin step. FAILING CLOSED.")

    have = set(current_membership[group])
    delta = fa_membership.membership_diff({group: desired}, {group: have}).get(
        group, {"add": [], "remove": []})
    summary = MembershipSummary(
        fa_group=group,
        added=tuple(delta["add"]),
        removed=tuple(delta["remove"]),
        kept=tuple(sorted(desired & have)),
    )

    if not summary.changed:
        # NO-OP. Hand back the caller's own string untouched and an empty diff so the write
        # path is provably never entered.
        return current_xml, "", summary

    new_xml = fa_membership.apply_membership(
        current, {group: desired}, new_member_amount=new_member_amount)

    _assert_only_target_group_changed(current, new_xml, group, desired, current_membership)

    diff_text = fa_membership.membership_diff_text(current, new_xml, label=group)
    return new_xml, diff_text, summary


def _assert_only_target_group_changed(current_xml, new_xml, group, desired,
                                      current_membership) -> None:
    """Load-bearing invariant check on the COMPUTED payload, before any human sees the diff.

    Because replaceFA overwrites the whole XML, the computed payload must (a) parse, (b) land
    the target group on EXACTLY the desired membership, and (c) leave every OTHER group's
    membership identical. Any deviation is a refusal, not a warning.
    """
    try:
        new_membership = fa_membership.parse_group_membership(new_xml)
    except ET.ParseError as exc:
        raise FaGroupSyncRefused(
            f"plan_membership_change: the COMPUTED groups XML does not parse ({exc}) — it can "
            f"never be sent to replaceFA. FAILING CLOSED.") from exc

    if new_membership.get(group) != set(desired):
        raise FaGroupSyncRefused(
            f"plan_membership_change: the computed XML does not land '{group}' on the desired "
            f"membership (got {sorted(new_membership.get(group, set()))}, wanted "
            f"{sorted(desired)}). FAILING CLOSED.")

    for other in set(current_membership) | set(new_membership):
        if other == group:
            continue
        if current_membership.get(other) != new_membership.get(other):
            raise FaGroupSyncRefused(
                f"plan_membership_change: the computed XML CHANGED an unrelated FA group "
                f"'{other}' (before {sorted(current_membership.get(other, set()))}, after "
                f"{sorted(new_membership.get(other, set()))}). replaceFA overwrites the WHOLE "
                f"groups XML, so this would clobber a live group. FAILING CLOSED.")


# ============================================================================================
# 4. WRITE — the ONLY replaceFA in this module, behind every gate.
# ============================================================================================
def apply_membership_change(ib, new_xml: str, *, armed: bool, backup_path: str) -> str:
    """Write ``new_xml`` to the FA master with ``replaceFA(1, new_xml)``. Returns the XML written.

    THIS IS THE ONLY FUNCTION HERE THAT WRITES. It refuses unless ALL of the following hold —
    each checked independently, each failing CLOSED with FaGroupSyncRefused:

      1. the CODE GATE permits: ``order_router.transmit_guard(armed)`` — READONLY=False AND
         DRY_RUN=False AND armed=True. Committed defaults are READONLY/DRY_RUN True, so an
         un-flipped process cannot write.
      2. a BACKUP exists at ``backup_path`` and is a non-empty, readable, PARSEABLE file
         (``backup_is_usable``). No backup, no write — there is no other undo for replaceFA.
      3. ``new_xml`` is non-empty, PARSES, and contains at least one <Group>. Sending an empty
         or group-less payload to replaceFA would wipe the master's group configuration.
      4. ``ib`` is a connection object exposing ``replaceFA``.

    NOTE for the wiring pass: the block executor also requires a PHYSICALLY armed gateway
    (``live_fa_block_execute.transmission_permitted`` adds the read-only gateway probe on top of
    the code gate). That probe needs a live connection, so it belongs to the caller that owns
    the session; this module deliberately does not open or probe a gateway.
    """
    permit, why = order_router.transmit_guard(bool(armed))
    if not permit:
        raise FaGroupSyncRefused(
            f"apply_membership_change: REFUSED by the arm gate ({why}). No replaceFA was "
            f"called; the FA master is untouched.")

    ok, reason = backup_is_usable(backup_path)
    if not ok:
        raise FaGroupSyncRefused(
            f"apply_membership_change: REFUSED — no usable backup ({reason}). replaceFA "
            f"overwrites the WHOLE groups XML and there is no other undo, so a write without a "
            f"verified backup is impossible by construction. FAILING CLOSED.")

    payload = str(new_xml or "").strip()
    if not payload:
        raise FaGroupSyncRefused(
            "apply_membership_change: REFUSED — the payload XML is EMPTY. Sending that to "
            "replaceFA would wipe every group on the master. FAILING CLOSED.")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise FaGroupSyncRefused(
            f"apply_membership_change: REFUSED — the payload XML does not parse ({exc}). "
            f"FAILING CLOSED.") from exc
    if not any(el.tag.split("}")[-1].lower() == "group" for el in root.iter()):
        raise FaGroupSyncRefused(
            "apply_membership_change: REFUSED — the payload XML contains NO <Group> elements. "
            "Writing it would clear the master's group configuration. FAILING CLOSED.")

    if ib is None or not hasattr(ib, "replaceFA"):
        raise FaGroupSyncRefused(
            "apply_membership_change: REFUSED — no gateway connection with replaceFA. "
            "FAILING CLOSED.")

    # LISTEN TO THE BROKER WHILE WE WRITE (2026-09-04). Nothing in this system captured what
    # IBKR says in response to replaceFA -- we only ever inspected the read-back afterwards.
    # That left a whole afternoon of "replaceFA WAS WRITTEN but the group is not there" with
    # no stated reason, and three wrong theories chased in its place. Any error the gateway
    # sends during the write is now collected and RAISED with the broker's own words.
    captured = []

    def _catch(reqId, errorCode, errorString, contract=None):   # ib_async errorEvent signature
        captured.append((reqId, errorCode, str(errorString or "")))

    listening = False
    try:
        ib.errorEvent += _catch
        listening = True
    except Exception:  # noqa: BLE001 - never let instrumentation break the write path
        pass
    try:
        ib.replaceFA(FA_GROUPS, payload)   # serialized config write — overwrites the WHOLE XML
        try:
            ib.sleep(2.0)                  # let the gateway's response land before we stop listening
        except Exception:  # noqa: BLE001
            pass
    finally:
        if listening:
            try:
                ib.errorEvent -= _catch
            except Exception:  # noqa: BLE001
                pass

    real = [c for c in captured if c[1] not in (2104, 2106, 2158, 2107, 2119, 10275)]
    if real:
        detail = "; ".join(f"[{code}] {msg}" for _rid, code, msg in real)
        print(f"      BROKER SAID during replaceFA: {detail}", flush=True)
        raise FaGroupSyncRefused(
            f"apply_membership_change: IBKR REPORTED AN ERROR during replaceFA: {detail}")
    return payload


# ============================================================================================
# 5. THE GLUE — read -> plan -> (no-op? stop) -> backup -> write -> verify.
# ============================================================================================
def create_run_group(
    ib,
    fa_group: str,
    desired_accounts: Sequence[str],
    *,
    armed: bool = False,
    backup_path: str | None = None,
    # ContractsOrShares, NOT NetLiq. IBKR refuses NetLiq on a newly created group:
    #   [10260] Group <name> has unsupported method (NetLiq)
    # (captured live 2026-09-04 once replaceFA errors were finally being read). It is
    # also what we actually use -- every block writes an explicit ContractsOrShares
    # split before it places, so NetLiq was only ever a vestigial default.
    default_method: str = "ContractsOrShares",
    # 1, NOT 0. A ContractsOrShares group whose every account is allocated ZERO shares
    # is rejected outright: [10229] FA data saving error: Invalid Group <name>
    # (captured live 2026-09-04). This is only a placeholder -- the real per-account
    # split is written by set_group_contracts_or_shares immediately before the order.
    new_member_amount: int = 1,
    verify: bool = True,
) -> dict:
    """The full CREATION chain for ONE run group, in the same safe order as
    :func:`sync_group_membership`.

        requestFA(1) -> plan (pure) -> MANDATORY backup -> arm-gated replaceFA -> read-back

    UNARMED (the default) this is a PREVIEW: it reads, plans, and returns the diff and summary
    having taken no backup, written no file and called no replaceFA.

    THERE IS NO NO-OP SHORT-CIRCUIT, unlike the membership chain. Creating a group is always a
    change by definition; a name that already exists is a REFUSAL from plan_group_creation, not
    a quiet no-op. That distinction is deliberate - a run group whose name already exists means
    the run stamp is not unique, and silently reusing another run's group would destroy the
    audit record this whole design exists to produce.

    THE READ-BACK IS STRICTER THAN THE MEMBERSHIP CHAIN'S. Editing membership only has to
    prove one group landed correctly. Creation must ALSO prove it did not disturb anything
    else, because replaceFA overwrites the whole document and the master carries groups this
    desk does not own (on 2026-09-03: Main, Ted, MainSmall, Rebalance, No Trade, Dougs Group,
    Income, Rob). So the read-back checks the new group's membership AND that every
    pre-existing group is unchanged AND that the group count rose by exactly one.

    Returns the same result dict shape as sync_group_membership, with ``created`` in place of
    ``changed``. Raises FaGroupSyncRefused if the read or plan cannot be trusted (nothing
    written), and FaGroupSyncVerifyFailed if the read-back does not match (the write DID
    happen - restore ``result["backup"]``).
    """
    current_xml = read_live_groups(ib)
    before = fa_membership.parse_group_membership(current_xml)
    new_xml, diff_text, summary = plan_group_creation(
        current_xml, fa_group, desired_accounts,
        default_method=default_method, new_member_amount=new_member_amount)

    result: dict = {
        "fa_group": summary.fa_group,
        "created": True,
        "wrote": False,
        "armed": bool(armed),
        "summary": summary,
        "summary_text": summary.text(),
        "diff": diff_text,
        "current_xml": current_xml,
        "new_xml": new_xml,
        "backup": "",
        "verified": None,
        "refused_reason": "",
        "groups_before": len(before),
    }

    permit, why = order_router.transmit_guard(bool(armed))
    if not permit:
        result["created"] = False
        result["refused_reason"] = (
            f"not written ({why}) - preview only, no backup, no replaceFA")
        return result

    result["backup"] = backup_groups(current_xml, backup_path)
    apply_membership_change(ib, new_xml, armed=armed, backup_path=result["backup"])
    result["wrote"] = True

    if verify:
        result["verified"] = False
        after = fa_membership.parse_group_membership(read_live_groups(ib))
        want = {str(a).strip() for a in (desired_accounts or []) if str(a).strip()}
        if after.get(summary.fa_group) != want:
            raise FaGroupSyncVerifyFailed(
                f"create_run_group: replaceFA WAS WRITTEN but the read-back of new FA group "
                f"{summary.fa_group!r} is {sorted(after.get(summary.fa_group, set()))}, not "
                f"the approved {sorted(want)}. The master is in an UNVERIFIED state - restore "
                f"the backup at {result['backup']}", result)
        if len(after) != len(before) + 1:
            raise FaGroupSyncVerifyFailed(
                f"create_run_group: replaceFA WAS WRITTEN but the master now has {len(after)} "
                f"groups, not the expected {len(before) + 1}. The master is in an UNVERIFIED "
                f"state - restore the backup at {result['backup']}", result)
        for name, members in before.items():
            if after.get(name) != members:
                raise FaGroupSyncVerifyFailed(
                    f"create_run_group: replaceFA WAS WRITTEN but pre-existing group {name!r} "
                    f"CHANGED. Creating a group must disturb nothing else. The master is in an "
                    f"UNVERIFIED state - restore the backup at {result['backup']}", result)
        result["verified"] = True

    return result


def sync_group_membership(
    ib,
    fa_group: str,
    desired_accounts: Sequence[str],
    *,
    armed: bool = False,
    backup_path: str | None = None,
    # 1, NOT 0. A ContractsOrShares group whose every account is allocated ZERO shares
    # is rejected outright: [10229] FA data saving error: Invalid Group <name>
    # (captured live 2026-09-04). This is only a placeholder -- the real per-account
    # split is written by set_group_contracts_or_shares immediately before the order.
    new_member_amount: int = 1,
    allow_empty: bool = False,
    verify: bool = True,
) -> dict:
    """The full membership-sync chain for ONE FA group, in the only safe order.

        requestFA(1) -> plan (pure) -> NO-OP? stop -> MANDATORY backup -> arm-gated replaceFA
                     -> read-back verification

    UNARMED (the default) this is a PREVIEW: it reads, plans, and returns the diff and summary
    for human review having taken no backup, written no file and called no replaceFA. Only an
    explicitly ``armed=True`` call (with READONLY/DRY_RUN flipped false by the caller's arm
    session) can reach the write, and only then after the backup lands.

    A NO-OP short-circuits before the backup: no disk write, no replaceFA, nothing.

    Returns a result dict:
        {"fa_group", "changed", "wrote", "armed", "summary" (MembershipSummary), "summary_text",
         "diff", "current_xml", "new_xml", "backup", "verified", "refused_reason"}
    ``refused_reason`` is set (and ``wrote`` False) when the arm gate declined an otherwise
    valid plan — that is the normal PREVIEW outcome, not an error.

    Raises FaGroupSyncRefused if the read or the plan cannot be trusted (nothing written), and
    FaGroupSyncVerifyFailed if the post-write read-back does not match the approved membership
    (the write DID happen — restore ``result["backup"]``).
    """
    current_xml = read_live_groups(ib)
    new_xml, diff_text, summary = plan_membership_change(
        current_xml, fa_group, desired_accounts,
        new_member_amount=new_member_amount, allow_empty=allow_empty)

    result: dict = {
        "fa_group": summary.fa_group,
        "changed": summary.changed,
        "wrote": False,
        "armed": bool(armed),
        "summary": summary,
        "summary_text": summary.text(),
        "diff": diff_text,
        "current_xml": current_xml,
        "new_xml": new_xml,
        "backup": "",
        "verified": None,
        "refused_reason": "",
    }

    if not summary.changed:
        result["refused_reason"] = ("no change — live membership already matches the desired "
                                    "membership; no backup taken and no replaceFA called")
        return result

    permit, why = order_router.transmit_guard(bool(armed))
    if not permit:
        # PREVIEW: the plan and its diff are returned for review; nothing is touched.
        result["refused_reason"] = f"not written ({why}) — preview only, no backup, no replaceFA"
        return result

    result["backup"] = backup_groups(current_xml, backup_path)
    apply_membership_change(ib, new_xml, armed=armed, backup_path=result["backup"])
    result["wrote"] = True

    if verify:
        result["verified"] = False
        after = read_live_groups(ib)
        after_membership = fa_membership.parse_group_membership(after)
        want = {str(a).strip() for a in (desired_accounts or []) if str(a).strip()}
        if after_membership.get(summary.fa_group) != want:
            raise FaGroupSyncVerifyFailed(
                f"sync_group_membership: replaceFA WAS WRITTEN but the read-back of FA group "
                f"'{summary.fa_group}' is "
                f"{sorted(after_membership.get(summary.fa_group, set()))}, not the approved "
                f"{sorted(want)}. The master is in an UNVERIFIED state — restore the backup at "
                f"{result['backup']}", result)
        result["verified"] = True

    return result
