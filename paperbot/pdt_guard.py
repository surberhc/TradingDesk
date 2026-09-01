"""
pdt_guard.py — the PURE per-account PATTERN-DAY-TRADER (PDT) read.

WHY THIS MODULE EXISTS
======================
The PDT pre-flight was built for the FA block rail (live_fa_block_execute.py, the PDT_TAG /
pdt_* functions). But the rail the Control Plane actually shells out to is
batch_rebalance_execute.py, which drives safe_execute.execute_plan ONCE PER ACCOUNT — and
that path had ZERO PDT references. So the rail that actually transmits had no day-trader
check at all. This module is the pure read, importable by safe_execute without dragging in
the block rail (no import from live_fa_block_execute — that file is not touched).

WHAT THE GATE ASKS
==================
NOT "does this run create a day trade" (an order-shape analyzer would be the wrong tool). A
US margin account under $25,000 that IBKR has ALREADY FLAGGED a pattern day trader rejects
ORDINARY orders, not just orders that would themselves be day trades. EVIDENCE (2026-07-28):
account U5721712 bounced a plain BUY of 1 USFR (~$50) with no offsetting sell. The question
is therefore "has the broker already flagged this account", and IBKR answers it directly with
the DayTradesRemaining accountSummary tag.

SEMANTICS (preserved EXACTLY from live_fa_block_execute):
    -1  = unlimited / not PDT-restricted   -> CLEARS
    n>0 = n day trades remain              -> CLEARS
    0   = none left, account is flagged    -> BLOCKS

ZERO NEW BROKER READS. ib_async's reqAccountSummary requests DayTradesRemaining (and the
T+1..T+4 siblings) BY DEFAULT, so the tag is already sitting in the per-account accountSummary
rows the caller holds. safe_execute's `req.summary` is exactly that: the whole login's
ib.accountSummary() put through s0_live.filter_account_summary(account=...), i.e. rows for
this ONE account (verified in batch_rebalance_execute's per-account state build and in
crm_execute.build_batch_requests, which sets summary=summaries[plan.account]).

========================================================================================
THE ABSENT-TAG DECISION — READ THIS BEFORE CHANGING ANYTHING HERE
========================================================================================
MEASURED LIVE against the port-4003 gateway (2026-09-01), NOT assumed:

    U23414989   22 accountSummary rows, NO DayTradesRemaining tag at all
    U23415099   22 rows, NO tag
    U27295881   22 rows, NO tag
    U27305011   DayTradesRemaining = '3'
    U14438624   DayTradesRemaining = '-1'   (and several others likewise)

ib_async asks for 34 tags; those accounts came back with 22. IBKR silently DROPS the tags
that do not apply to the account, and DayTradesRemaining is one of them. The accounts that
lack it are the ones IBKR does not treat as margin day-trading accounts at all — and the
FINRA pattern-day-trader rule is a MARGIN-ACCOUNT rule: an account IBKR does not run as a
margin day-trading account cannot be PDT-flagged, which is precisely the failure mode this
gate defends against. (Such accounts have settlement/good-faith rules instead; those reject
differently and are not what this gate is for.)

The block rail's rule is "missing or unparseable FAILS CLOSED". Copying that rule onto THIS
rail would refuse three real, perfectly tradeable accounts — including two queued for an
imminent first deployment. That is not a safe default; it is a different outage.

THE RULE IMPLEMENTED HERE (recommendation): distinguish "the broker answered and did not
mention the tag" from "we cannot tell whether the broker answered at all".

    tag present, -1 or n>0 ............ CLEAR
    tag present, 0 .................... BLOCK  (the real PDT flag)
    tag present, unparseable/other -ve  BLOCK  (broker answered, we can't read it -> closed)
    tag ABSENT, but the response is
      demonstrably COMPLETE for this
      account (a witness tag such as
      NetLiquidation/AccountType is
      present) ........................ CLEAR  ("not a margin day-trading account")
    tag ABSENT and NO witness tag
      (empty/thin/failed read) ........ BLOCK  (we cannot confirm a read happened at all)
    tag ABSENT but a DayTradesRemaining
      T+1..T+4 SIBLING is present ..... BLOCK  (broker answered the PDT question only
                                                partially — anomalous, fail closed)

THE TRADE-OFF, STATED PLAINLY. If IBKR ever stops returning DayTradesRemaining for an account
that IS a margin day-trading account AND IS flagged, this rule clears it and the orders bounce
at the broker instead of here. Accepted, because: (a) the alternative refuses known-good
accounts today, with certainty, rather than hypothetically; (b) the witness-tag requirement
still fails closed on the case we genuinely cannot distinguish — a failed/empty read; (c) the
sibling-tag rule catches a partial answer; and (d) EVERY clearance-by-absence emits a named,
printable reason string, so a human reviewing an armed run sees exactly which accounts were
cleared on that basis. Nothing here is silent.

The rejected alternative "fail closed + per-account override list" was considered and NOT
taken: it turns a broker fact into hand-maintained state that goes stale silently, and the
override would have to be granted to exactly the accounts the broker is already telling us
are not day-trading accounts.

PURE. No broker calls, no I/O, never raises.
"""
from __future__ import annotations

from typing import NamedTuple

# The tag IBKR answers the PDT question with.
PDT_TAG = "DayTradesRemaining"

# The forward-dated siblings ib_async also requests by default. If ANY of these came back
# while the base tag did not, the broker answered the PDT question only partially — that is
# not the clean "this is not a day-trading account" shape, so it fails closed.
PDT_SIBLING_TAGS = ("DayTradesRemainingT+1", "DayTradesRemainingT+2",
                    "DayTradesRemainingT+3", "DayTradesRemainingT+4")

# WITNESS TAGS — proof that a real accountSummary response for this account is in hand.
# Every one of these is in ib_async's default request AND is returned for every funded
# account type (they were all present in the 22 rows the tag-less accounts returned). The
# presence of ANY ONE of them means the read succeeded and IBKR simply chose not to report
# DayTradesRemaining. Their total absence means we are holding an empty/thin/failed read and
# know nothing at all — which is the case that must fail closed.
WITNESS_TAGS = ("NetLiquidation", "AccountType", "TotalCashValue", "BuyingPower",
                "EquityWithLoanValue")

# Verdict codes (stable strings; safe to log/assert on).
CLEAR_UNLIMITED = "CLEAR_UNLIMITED"
CLEAR_REMAINING = "CLEAR_REMAINING"
CLEAR_NOT_A_DAY_TRADING_ACCOUNT = "CLEAR_NOT_A_DAY_TRADING_ACCOUNT"
BLOCK_NO_DAY_TRADES = "BLOCK_NO_DAY_TRADES"
BLOCK_UNPARSEABLE = "BLOCK_UNPARSEABLE"
BLOCK_UNREADABLE_SUMMARY = "BLOCK_UNREADABLE_SUMMARY"
BLOCK_PARTIAL_PDT_FAMILY = "BLOCK_PARTIAL_PDT_FAMILY"


class PdtVerdict(NamedTuple):
    """One account's PDT verdict. `ok` is the gate answer; `code` is one of the stable
    CLEAR_*/BLOCK_* strings; `value` is the raw tag value as observed (None when the tag was
    absent); `reason` is ALWAYS a human-readable sentence naming the account and the observed
    tag value — on a clearance as well as a refusal, so an armed run is never silent about
    why an account was let through."""
    ok: bool
    code: str
    value: object
    reason: str


def _tag_map(summary_rows) -> dict:
    """PURE: {tag: value} for ONE account's accountSummary rows. Accepts either the row-object
    list ib.accountSummary() returns (already account-filtered by the caller) or the
    {tag: value} dict shape s0_live.filter_account_summary can pass through. Never raises."""
    if isinstance(summary_rows, dict):
        return dict(summary_rows)
    out: dict = {}
    for row in (summary_rows or []):
        tag = str(getattr(row, "tag", "") or "")
        if tag and tag not in out:
            out[tag] = getattr(row, "value", None)
    return out


def raw_day_trades_remaining(summary_rows) -> tuple[bool, object]:
    """PURE: (tag_present, raw_value) for DayTradesRemaining. `tag_present` distinguishes
    "the broker did not mention this tag" from "the tag is there but empty/garbage" — the
    whole basis of this module's absent-vs-unparseable split. Never raises."""
    tags = _tag_map(summary_rows)
    if PDT_TAG not in tags:
        return False, None
    return True, tags.get(PDT_TAG)


def day_trades_remaining(summary_rows) -> int | None:
    """PURE: IBKR's DayTradesRemaining as an int, or None when the tag is absent, blank or
    unparseable. Same signature and semantics as live_fa_block_execute.day_trades_remaining
    (kept identical so the two rails read the broker the same way). Never raises."""
    present, raw = raw_day_trades_remaining(summary_rows)
    if not present or raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _has_witness(summary_rows) -> bool:
    """PURE: True when this account's rows carry at least one WITNESS_TAG, i.e. we are
    holding a real accountSummary response for it rather than an empty/failed read."""
    tags = _tag_map(summary_rows)
    return any(t in tags for t in WITNESS_TAGS)


def _has_pdt_sibling(summary_rows) -> bool:
    """PURE: True when a DayTradesRemainingT+n sibling came back."""
    tags = _tag_map(summary_rows)
    return any(t in tags for t in PDT_SIBLING_TAGS)


def pdt_verdict(account: str, summary_rows) -> PdtVerdict:
    """PURE: the PDT verdict for ONE account, off the accountSummary rows the caller already
    holds. See this module's header for the full rule and the trade-off it encodes.

    NEVER SILENT: `reason` always names the account and the observed tag value, on a clear as
    well as on a refusal. Never raises."""
    present, raw = raw_day_trades_remaining(summary_rows)

    if not present:
        # The broker did not mention the tag. Two very different worlds — tell them apart.
        if not _has_witness(summary_rows):
            return PdtVerdict(
                False, BLOCK_UNREADABLE_SUMMARY, None,
                f"account {account}: {PDT_TAG} is absent AND this account's accountSummary "
                f"carries none of {list(WITNESS_TAGS)} — there is no evidence a real account "
                f"summary was read at all, so the account cannot be confirmed clear of a "
                f"pattern-day-trader flag. FAILING CLOSED (observed {PDT_TAG}=<absent>).")
        if _has_pdt_sibling(summary_rows):
            return PdtVerdict(
                False, BLOCK_PARTIAL_PDT_FAMILY, None,
                f"account {account}: {PDT_TAG} is absent but a "
                f"{'/'.join(PDT_SIBLING_TAGS)} sibling IS present — IBKR answered the "
                f"pattern-day-trader question only PARTIALLY, which is not the shape a "
                f"non-day-trading account returns. FAILING CLOSED (observed "
                f"{PDT_TAG}=<absent>).")
        return PdtVerdict(
            True, CLEAR_NOT_A_DAY_TRADING_ACCOUNT, None,
            f"account {account}: observed {PDT_TAG}=<absent> in an otherwise COMPLETE "
            f"accountSummary — IBKR omits this tag for accounts it does not run as margin "
            f"day-trading accounts, and such an account cannot carry a pattern-day-trader "
            f"flag. CLEARED on that basis (measured 2026-09-01: U23414989 / U23415099 / "
            f"U27295881 all return 22 rows with no {PDT_TAG}).")

    n = day_trades_remaining(summary_rows)
    if n is None:
        # The tag IS there but we cannot read it. The broker answered; we failed. Closed.
        return PdtVerdict(
            False, BLOCK_UNPARSEABLE, raw,
            f"account {account}: {PDT_TAG}={raw!r} is present but UNPARSEABLE — the broker "
            f"answered the pattern-day-trader question and this rail could not read the "
            f"answer. FAILING CLOSED.")
    if n == -1:
        return PdtVerdict(True, CLEAR_UNLIMITED, raw,
                          f"account {account}: {PDT_TAG}={raw!r} (-1 = unlimited / not "
                          f"pattern-day-trader restricted). CLEARED.")
    if n > 0:
        return PdtVerdict(True, CLEAR_REMAINING, raw,
                          f"account {account}: {PDT_TAG}={raw!r} ({n} day trade(s) remain, "
                          f"not restricted). CLEARED.")
    if n == 0:
        return PdtVerdict(
            False, BLOCK_NO_DAY_TRADES, raw,
            f"account {account}: {PDT_TAG}={raw!r} — IBKR has flagged this account "
            f"PATTERN-DAY-TRADER restricted with no day trades left. Such an account rejects "
            f"ORDINARY orders regardless of order shape (2026-07-28, U5721712: a plain BUY of "
            f"1 USFR was bounced). REFUSING to transmit for it.")
    # n < -1: undefined by IBKR's documented semantics. Broker answered, answer is nonsense.
    return PdtVerdict(
        False, BLOCK_UNPARSEABLE, raw,
        f"account {account}: {PDT_TAG}={raw!r} — a negative value other than -1 is outside "
        f"IBKR's documented semantics (-1 unlimited, 0 none left, n>0 remaining), so the "
        f"pattern-day-trader state cannot be established. FAILING CLOSED.")


def pdt_account_ok(account: str, summary_rows) -> tuple[bool, str]:
    """PURE convenience: (ok, reason) for ONE account — the shape the pre-transmit `reasons`
    gate wants. `reason` is populated on a clearance too (see pdt_verdict)."""
    v = pdt_verdict(account, summary_rows)
    return v.ok, v.reason
