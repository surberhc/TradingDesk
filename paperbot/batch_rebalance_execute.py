"""
batch_rebalance_execute.py — the MULTI-ACCOUNT BATCH REBALANCE executor (real-transmission,
port-4003 FA master), built READY TO ROLL: it transmits NOTHING until a human explicitly arms.

WHAT THIS IS
------------
The single-account sibling s0_live_deploy.py rebalances ONE funded account (U14438624) into
its S0 target behind the review -> arm -> transmit gate. THIS file is its multi-account
BATCH sibling: it rebalances EVERY out-of-spec account on the human-blessed execution roster
to that account's model, one account at a time, behind the SAME gate — reusing the SAME
shared safe-execution engine (safe_execute.execute_plan, the desk's one real-money transmit
chokepoint) for each account.

It DOES NOT re-implement any execution logic. It is a THIN driver that:
  * pulls the execution allow-list from roster.enrolled_roster() (the CRM-blessed roster, or
    the config.ENROLLMENT fallback — currently only the 2 blessed accounts, on purpose);
  * reads each blessed account's live NetLiq/positions off the ONE 4003 FA-master login
    (s0_live.filter_account_summary / filter_positions — the same per-account filtering the
    single-account lane already uses);
  * sizes each account with the UNCHANGED frozen engine (rebalance_engine.plan_account);
  * maps the sized plans to per-account ExecutionRequests via the PURE
    crm_execute.requests_from_crm_plan (purpose=REBALANCE, conform=False, allowed_accounts=
    the roster) — which SKIPS every already-in-band account, so the set that actually trades
    is exactly the OUT-OF-SPEC subset of the roster; and
  * drives safe_execute.execute_plan per request — PREVIEW by default (sends nothing),
    ARMED (two-phase cash-gated transmit, per-account margin pre-flight) only when the human
    lines up the arm token AND the physical 4003 Gateway is armed.

SANDBOXED TO THE ROSTER (two independent walls)
-----------------------------------------------
  1. We only ever READ + SIZE accounts that are IN roster.enrolled_roster(). A non-roster
     account is never touched.
  2. Every ExecutionRequest carries allowed_accounts=the roster, so safe_execute's
     account_wall_ok independently REFUSES any account not on the roster — the wall never
     "allows whatever the planner produced". Widening execution is a deliberate roster edit
     (Andrew widens the CRM roster in stages), NOT a code change here.

DEFAULT IS SAFE. With no flag this runs a PREVIEW: it sizes every roster account, prints each
account's would-trade order list + the self-computed per-account margin pre-flight (#57), an
aggregate summary, and transmits NOTHING. To actually transmit, a human must line up ALL of:
  * the exact CLI token  --arm-i-understand  (sets armed=True; never defaulted/auto-set),
  * NO kill-switch sentinel present,
  * every target account IN the enrolled roster (any other refused — the account wall),
  * every leg whole-share, priced, through order_router's HARD price guard,
  * each account's own rails (total BUY <= investable; per-order BUY <= 2x the model's own
    target dollars for that symbol; per-order SELL <= the shares actually held) + margin
    pre-flight passing, AND
  * the Gateway physically ARMED (Read-Only API toggle OFF — measured live per account with
    the zero-transmission cancel-a-fabricated-order probe).
Miss ANY one and that account is a preview that transmits nothing and prints WHY. There is NO
auto-arm and nothing here is scheduled.

FA-BLOCK whatIf (owner-gated, OPTIONAL — left stubbed by design)
----------------------------------------------------------------
This batch places PER-ACCOUNT direct orders through execute_plan's proven two-phase path
(each order carries its sub-account; the 4003 FA master routes it), each with its own
margin pre-flight. A separate, owner-gated optimisation would instead place ONE FA-BLOCK per
symbol across accounts and margin-check it with a live whatIf via the OFFICIAL ibapi — a
NON-PyPI dependency the desk deliberately does not add here. That branch is designed for but
left stubbed (fa_block_whatif_preflight below); the per-account path needs none of it.

Run — PREVIEW (default; transmits nothing):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe batch_rebalance_execute.py

Run — ARMED batch rebalance (human-supervised; requires an armed Gateway + no kill switch):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe batch_rebalance_execute.py --arm-i-understand
"""
from __future__ import annotations

import os
import sys

import config
import crm_execute
import crm_roster
import custom_target
import custom_tier
from strategies import small_tier
from strategies import config as config_s
import ledger
import live_quotes
import rebalance_engine
import roster
import s0_live
import safe_execute
import strategy_target
import version
from safe_execute import MODE_ARMED, MODE_PREVIEW, PURPOSE_REBALANCE
# Reuse, don't reimplement: the read-only pilot already has the strategy-universe accessor
# (ROTATE_OUT vs ALIEN classification) the single-account deploy uses. Importing it opens no
# broker connection.
import s0_live_pilot_run as sp

# The live-trade Gateway port every account on this FA master login shares (same login the
# single-account lane already uses). Contextual only — the connection is opened by s0_live.
LIVE_TRADE_PORT = 4003

# The exact arm token — typed in full, no abbreviation, no default (identical in kind to
# s0_live_deploy.ARM_TOKEN / rebalance_execute.ARM_TOKEN).
ARM_TOKEN = "--arm-i-understand"

# KILL SWITCH — the SAME sentinel s0_live_deploy / s0_live_exec / morning_execute_run honor.
# If the file exists (any content) this run is preview-only for EVERY account.
KILL_SWITCH = r"C:\TradingDesk-Local\AUTOTRADE_DISABLED"
# Register the exact sentinel path with the shared engine so the "KILL_SWITCH sentinel present
# (...)" block reason reads byte-for-byte identically to the single-account lane's message.
safe_execute.register_kill_switch_label(KILL_SWITCH)

# The batch places per-account direct orders (see module docstring). The FA-block whatIf
# optimisation is OWNER-GATED and OFF by design — it would pull in the official ibapi
# (non-PyPI). Left False + stubbed so no non-PyPI dependency is added here.
FA_BLOCK_WHATIF_ENABLED = False


def arm_requested(argv: list[str]) -> bool:
    """True ONLY if the exact arm token is present in argv — the single thing that sets
    armed=True. Mirrors s0_live_deploy.arm_requested."""
    return ARM_TOKEN in argv


def _kill_switch_present() -> bool:
    """True if the AUTOTRADE_DISABLED sentinel exists -> force preview-only for the batch."""
    return os.path.exists(KILL_SWITCH)


# ========================================================================================
# ROSTER + PER-ACCOUNT MODEL VERSION (the execution allow-list, human-blessed).
# ========================================================================================
# Accounts whose STORED model label disagreed with what their NAV says they should hold.
# Populated by resolve_roster_versions and surfaced loudly in the preview: a non-empty
# map means the CRM record is stale (tier scan behind/down) and the desk overrode it.
_TIER_MISMATCHES: dict[str, tuple] = {}


def split_versions(labels) -> tuple[list[str], list[str]]:
    """Partition model labels into (CUSTOM, NON-CUSTOM) by the allocation's SOURCE — one CRM
    read for the whole batch. Thin wrapper over ``custom_target.split_labels`` (which asks
    "does this label have rows in v_tradingdesk_custom_allocations?"), with ONE addition: when
    the CRM connection is not configured at all there are, by definition, no published custom
    allocations, so every label is non-custom and we do not attempt a read. That is the
    config.ENROLLMENT fallback environment the S0-only book already runs in.

    A CONFIGURED-but-failing CRM deliberately RAISES (crm_roster.CrmRosterUnavailable) rather
    than answering "non-custom": "I cannot tell whether this is a book Andrew authored by
    hand" must never silently resolve to "treat it as an S0 model". Both callers below turn
    that raise into a loud refusal that connects to nothing.

    NEVER keys on the label's SPELLING. A CRM rename ("Growth (Custom)" -> anything, including
    something ending in " (Small)") must not be able to re-point an account onto an S0 model
    or into small_tier's NAV re-tiering."""
    wanted = [str(v) for v in labels]
    if not wanted or not crm_roster.is_configured():
        return [], wanted
    return custom_target.split_labels(wanted)


class TargetBuildFailed(RuntimeError):
    """One model label's Target could not be built. Carries the LABEL so the caller can print
    the same ``COULD NOT BUILD TARGET for <label>`` refusal it always has, whether the label
    was an S0 model or an Andrew-authored custom allocation."""

    def __init__(self, label: str, cause: Exception):
        self.label = str(label)
        self.cause = cause
        super().__init__(str(cause))


def build_targets(distinct_versions) -> tuple[dict, dict]:
    """``({label: Target}, {label: AllocationMeta})`` for every distinct roster model label.

    DISPATCH BY SOURCE (:func:`split_versions`), never by the label's spelling:
      * CUSTOM labels are built by ``custom_target.custom_targets_with_meta`` — ONE CRM read
        for the whole batch — and keep their published-version identity for the Stage 6 audit
        record. ``Target.version`` is the label VERBATIM, so ``targets[plan.version]`` in
        crm_execute.py resolves (a decorated version is a hard KeyError there, not a skip).
      * every other label is built by ``strategy_target.current_target`` exactly as before.

    FAIL CLOSED. Any label that cannot be built raises :class:`TargetBuildFailed` naming it —
    including a custom label that came back with no rows (a model that IS custom must never
    fall through to the S0 backtester, and an empty book would liquidate the account).
    Metas contains ONLY custom labels; an S0 label is simply absent from it.

    Read-only: reads the CRM view and local price/strategy data. No broker, no order."""
    wanted = [str(v) for v in distinct_versions]
    custom_versions, s0_versions = split_versions(wanted)

    targets: dict = {}
    metas: dict = {}
    if custom_versions:
        try:
            built = custom_target.custom_targets_with_meta(custom_versions)
        except Exception as exc:  # noqa: BLE001
            label = getattr(exc, "label", None) or ", ".join(custom_versions)
            raise TargetBuildFailed(label, exc) from exc
        for label in custom_versions:
            if label not in built:
                # split_versions said this label HAS a published allocation and the builder
                # returned none for it — the two CRM reads disagree. Refuse; do not fall
                # through to strategy_target with a hand-authored label.
                raise TargetBuildFailed(label, custom_target.NoCustomAllocation(
                    f"custom model {label!r} has a published allocation per "
                    f"{crm_roster.CUSTOM_ALLOCATIONS_VIEW} but produced no Target — refusing "
                    f"to size the account against anything else."))
            targets[label], metas[label] = built[label]

    for v in s0_versions:
        try:
            targets[v] = strategy_target.current_target(version=v)
        except Exception as exc:  # noqa: BLE001
            raise TargetBuildFailed(v, exc) from exc
    return targets, metas


def resolve_roster_versions(roster_accounts: list[str]) -> dict[str, str]:
    """Map each blessed roster account -> its model version, from the SAME source the roster
    derives from. Prefer the CRM roster ``model`` (v_tradingdesk_roster), fall back to the
    local config.ENROLLMENT map, then to config.STRATEGY_VERSION for anything still unmapped.

    An ANDREW-AUTHORED (custom) allocation is re-tiered by its OWN ladder (custom_tier), never
    by small_tier — see the inline note. Pure/read-only: SELECTs from the read-only CRM role
    when configured, else reads config; contacts no broker, builds no order, WRITES NOTHING."""
    crm_models: dict[str, str] = {}
    crm_navs: dict[str, float] = {}
    crm_prior_risk: dict[str, str] = {}
    crm_has_prior: dict[str, bool] = {}
    if crm_roster.is_configured():
        try:
            for r in crm_roster.fetch_roster(advisor_name=crm_roster.DEFAULT_ADVISOR):
                acct = crm_roster.account_identifier(r)
                crm_models[acct] = (r.get("model") or "")
                nav = r.get("total_value")
                if nav is not None:
                    crm_navs[acct] = float(nav)
                # The two custom-tier history facts the CRM computes for us. Read
                # DEFENSIVELY: a view lagging the code leaves them absent, and custom_tier
                # then falls back the safe way — GROWTH for a missing risk level, and a
                # labelled account treated as an INCUMBENT (band applies) rather than
                # re-tiered off a plain boundary.
                prior = custom_tier.prior_risk_from_row(r)
                if prior:
                    crm_prior_risk[acct] = prior
                has_prior = custom_tier.has_prior_assignment_from_row(r)
                if has_prior is not None:
                    crm_has_prior[acct] = has_prior
        except crm_roster.CrmRosterUnavailable:
            crm_models, crm_navs = {}, {}   # CRM unreachable -> config fallback below
            crm_prior_risk, crm_has_prior = {}, {}
    raw: dict[str, str] = {}
    for a in roster_accounts:
        raw[a] = (crm_models.get(a) or config.ENROLLMENT.get(a)
                  or config.STRATEGY_VERSION)

    # SOURCE-BASED CUSTOM SPLIT (one CRM read for the whole roster). Everything below the
    # custom branch is the S0 NAV re-tiering; a hand-authored book must never enter it.
    custom_labels = set(split_versions(sorted(set(raw.values())))[0])

    out: dict[str, str] = {}
    for a, label in raw.items():
        # CUSTOM ALLOCATION -> ITS OWN LADDER, NEVER small_tier. Andrew authored these tickers
        # and percentages himself; there is no "parent" model to collapse onto. Letting
        # small_tier see one is a real hazard: parent_version() and tier_for() are
        # SPELLING-based, so a CRM rename to anything ending in " (Small)" would make
        # tier_for() REWRITE the account's label onto an S0 model — silently discarding the
        # whole hand-authored book. It is safe TODAY only because "Growth (Small, Custom)"
        # happens not to end in " (Small)"; that is a coincidence of naming, not a control.
        # This branch asks the allocation's SOURCE first, so a rename cannot re-point the
        # account into the S0 path at all.
        #
        # But "not small_tier" is not the same as "no check". The custom family ships in
        # THREE whole-share sizes (15-line full / 11-line small / 2-line Starter) precisely
        # BECAUSE NAV decides what an account can hold: at $2,000 the 11-line small book
        # deploys just 76.2% of the account — its 3% long-Treasury and gold slices are worth
        # less than one share, so those orders are never created — while the 2-line Starter
        # book deploys 95.9%. So a custom account gets the SAME authoritative pre-trade
        # re-check an S0 account gets, from custom_tier's own closed label table: it can only
        # ever emit one of the seven custom labels, so it can never rewrite a hand-authored
        # book onto an S0 model, and it refuses outright to touch a label outside the family
        # (a renamed custom book passes through UNCHANGED). The CRM runs the identical ladder
        # in SQL; the two must agree or an account oscillates every day.
        if label in custom_labels:
            if custom_tier.is_custom_family(label) and a in crm_navs:
                effective = custom_tier.tier_for(
                    crm_navs[a], current_label=label,
                    prior_risk=crm_prior_risk.get(a),
                    has_prior_assignment=crm_has_prior.get(a))
                if effective != label:
                    _TIER_MISMATCHES[a] = (label, effective, crm_navs[a])
                label = effective
            out[a] = label
            continue
        # PRE-TRADE TIER CHECK (authoritative). The CRM's stored label is a RECORD, and a
        # record can be stale — the nightly tier scan might not have run. NAV is the thing
        # that actually decides which model an account can hold whole-share, and we have it
        # right here, so we recompute the tier at the moment of use instead of trusting the
        # stored label. This is deliberately independent of any scheduled job: if every scan
        # in the system stopped, this still sizes each account against the correct model.
        # Hysteresis is honoured by passing the stored label as the incumbent.
        parent = small_tier.parent_version(label)
        if parent in config_s.CLIENT_VERSIONS and a in crm_navs:
            effective = small_tier.tier_for(crm_navs[a], parent, current_label=label)
            if effective != label:
                _TIER_MISMATCHES[a] = (label, effective, crm_navs[a])
            label = effective
        out[a] = label
    return out


# ========================================================================================
# FA-BLOCK whatIf — OWNER-GATED, OPTIONAL, STUBBED (no non-PyPI dependency added here).
# ========================================================================================
def fa_block_whatif_preflight(*_args, **_kwargs):
    """DESIGN STUB for the owner-gated FA-block live whatIf margin pre-flight.

    The per-account batch path does not need this: each account is margin-checked by
    safe_execute._margin_preflight_ok (#57) and transmitted via execute_plan's two-phase
    path. A future FA-BLOCK optimisation (one block per symbol across accounts) would margin-
    check the block with a LIVE whatIf through the OFFICIAL ibapi — a non-PyPI dependency the
    desk deliberately does not add here. This stub keeps the seam visible and OFF."""
    raise NotImplementedError(
        "FA-block whatIf pre-flight is an owner-gated branch requiring the official ibapi "
        "(non-PyPI) and is disabled by design (FA_BLOCK_WHATIF_ENABLED=False). The batch uses "
        "per-account direct orders with per-account margin pre-flight instead.")


# ========================================================================================
# PURE request assembly + per-account margin pre-flight line (#57) — unit-testable offline.
# ========================================================================================
def build_batch_requests(plans: list, *, targets, quotes, prices, roster_accounts,
                         summaries=None, armed=False, kill=False, run_id=None) -> list:
    """PURE: turn the per-account sized plans into per-account ExecutionRequests by REUSING
    crm_execute.requests_from_crm_plan (no re-implementation).

    `plans` are rebalance_engine AccountPlans (one per blessed roster account). We wrap them
    in the {"plans": [...]} shape that crm_execute expects and delegate. That helper SKIPS any
    account whose orders are all-zero (already in-band), so the returned requests are exactly
    the OUT-OF-SPEC subset. Every request carries allowed_accounts=the roster (the account
    wall), purpose=REBALANCE, conform=False. Builds and transmits nothing.

    `run_id` (v0.37.0, the audit trail) is stamped onto EVERY request so the whole batch shares
    ONE run identifier. safe_execute.execute_plan honours `req.run_id` (it only invents one
    when None), and every orderRef it puts on the wire ends in `:{run_id}` — so the single
    ledger record this batch writes joins back to each IBKR order, and forward to the exact
    model / published allocation version that produced it. crm_execute stays untouched (it
    builds requests with run_id=None for every caller); the stamp is applied here."""
    crm_result = {"plans": list(plans), "blocks": [], "routes": []}
    requests = crm_execute.requests_from_crm_plan(
        crm_result, targets=targets, quotes=quotes, prices=prices,
        roster=roster_accounts, summaries=summaries or {}, armed=armed, kill=kill)
    if run_id:
        for req in requests:
            req.run_id = run_id
    return requests


def account_universe(target, meta, held, base=None):
    """The tradeable universe to hand ``rebalance_engine.plan_account(universe=...)`` for ONE
    account. PURE.

    S0 MODEL (`meta` is None) -> `base` (S0's ALL_TICKERS) exactly as before. Unchanged.

    CUSTOM ALLOCATION (`meta` is an AllocationMeta) -> the allocation's OWN tickers plus this
    account's currently-held symbols, via ``custom_target.universe_for(target, held=held)``.

    WHY THE S0 UNIVERSE IS A TRAP HERE. reconcile classifies a HELD symbol the model weights
    at 0 as ROTATE_OUT if it is IN the universe and ALIEN if it is not, and ALIEN is in
    rebalance_engine._NO_AUTOTRADE_STATUSES: an ALIEN line never breaches the band and never
    produces a delta. Pass S0's universe to a custom account and any ticker Andrew REMOVES
    from his allocation is instantly ALIEN — it would sit in the account forever, never sold,
    with no error anywhere. The rotation would silently do nothing. That is the whole reason
    this function exists.

    THE TRADE-OFF, STATED. Putting the account's held symbols in the universe also takes those
    symbols OUT of ALIEN review, so a corporate-action holding in a custom account becomes
    eligible for an automatic SELL instead of being parked for a human. That is the intended
    rotation set for a hand-authored book — the desk cannot otherwise know that a held ticker
    belonged to a PREVIOUS version of the allocation — and the sizing loop prints exactly
    which symbols it applies to on every run. `base` is deliberately NOT unioned in for a
    custom account: the universe is the intended rotation set, not something broader."""
    if meta is None:
        return base
    return custom_target.universe_for(target, held=list(held or ()))


def account_reserve_pct(meta):
    """The standing CASH RESERVE for ONE account's model, as a fraction of NAV. PURE.

    S0 MODEL (`meta` is None) -> the global default, 1.5%. S0 is validated at 1.5% and does
    NOT move. CUSTOM ALLOCATION (`meta` is an AllocationMeta) -> 1%.

    WHY THERE IS A RESERVE AT ALL: IBKR deducts its advisory fee from account CASH, and
    client distributions are paid from cash. A fully-invested account is overdrawn the moment
    a fee posts — the 2026-07-28 negative-balance incident. WHY 1% ON A HAND-AUTHORED BOOK:
    Andrew's call; enough fee/distribution headroom, less of the client's money undeployed.

    SOURCE-based by construction, and deliberately reuses the SAME `meta` signal the universe
    and the audit stamp already key on: `metas` is populated only from labels that have rows
    in v_tradingdesk_custom_allocations, so a CRM RENAME cannot move an account's reserve, and
    this needs no extra CRM read. The percentages live in config and are read through
    investable — nothing here hardcodes one."""
    import investable as _investable

    return _investable.buffer_pct_for(is_custom=meta is not None)


def margin_preflight_line(request, result) -> tuple[bool, str]:
    """Self-computed per-account MARGIN pre-flight (#57), surfaced for the preview. REUSES
    safe_execute._margin_preflight_ok with the account's own plan/target/summary and the
    total BUY notional read off the previewed leg list. For an unlevered S0/rebalance book
    (exposure <= 1.0) this is (True, "") on any account type; it fails closed only on a
    genuinely levered request that cannot confirm margin capacity. Pure — no broker."""
    total_buy = sum(l.notional for l in (result.legs or []) if l.side == "BUY")
    return safe_execute._margin_preflight_ok(
        request.summary, request.net_liq, total_buy, request.plan, request.target)


def summarize_batch(plans: list, requests: list, results: list) -> dict:
    """Aggregate the batch for the summary line. `plans` is every roster account we sized;
    `requests`/`results` are the OUT-OF-SPEC subset that produced a preview/transmit. Pure."""
    total_sells = 0.0
    total_buys = 0.0
    total_legs = 0
    for res in results:
        for l in (res.legs or []):
            total_legs += 1
            if l.side == "SELL":
                total_sells += l.notional
            else:
                total_buys += l.notional
    return {
        "n_roster": len(plans),
        "n_out_of_spec": len(requests),
        "n_in_spec": len(plans) - len(requests),
        "total_sells": total_sells,
        "total_buys": total_buys,
        "total_legs": total_legs,
    }


# ========================================================================================
# THE AUDIT TRAIL (v0.37.0) — "when I made allocation changes, when the trades happened,
# and keep all the data".
# ========================================================================================
# THE GAP THIS CLOSES. This executor — the one the Control Plane actually shells out to —
# wrote NO ledger record at all. ledger.record_run is called by execution_engine,
# rebalance_execute, live_fa_block_execute and others, but never here, so a batch run left
# only the per-leg transmit_journal and the orderRef on the wire: no stored target weights,
# no model label, and nothing at all tying a fill back to the book that asked for it.
#
# AND WHY THE orderRef ALONE IS NOT ENOUGH FOR A CUSTOM MODEL. The ref encodes
# `target.as_of`, which for a custom allocation is the version's effective_from — a DATE.
# Two allocations published on the same day are therefore INDISTINGUISHABLE on the wire. The
# thing that actually identifies the book Andrew authored is the allocation's version_number
# / version_id, and that only exists if we write it down here.
def order_refs_for(request, result, run_id: str) -> list[str]:
    """The exact orderRefs this account's legs carry on the wire, in leg order.

    REUSES safe_execute._deploy_ref with the same (account, target.as_of, side, symbol,
    run_id) the transmit phase uses, so the recorded ref is byte-identical to the transmitted
    one — never a re-implementation of the ref format. Recorded for a PREVIEW too: it is then
    the ref the run WOULD have used, which is what makes a preview reviewable."""
    return [safe_execute._deploy_ref(request.account, request.target.as_of, l.side, l.symbol,
                                     run_id)
            for l in (result.legs or [])]


def run_id_from_order_ref(order_ref: str) -> str | None:
    """The run_id an orderRef carries — the FIRST hop of the audit join, going backwards from
    an IBKR order to the run record. Every ref this lane transmits is
    ``paperbot:{account}:{as_of}:{side}:{symbol}:deploy:{run_id}``, so the run stamp is the
    last colon-delimited field. None if the ref carries no run stamp (a pre-v0.34 ref, or a
    ref built with run_id=None)."""
    parts = str(order_ref).split(":")
    if len(parts) < 2 or parts[-2] != safe_execute.DEPLOY_REF_TAG:
        return None
    return parts[-1] or None


def account_audit_record(request, result, *, model_label, target, meta, run_id,
                         margin_ok=None) -> dict:
    """The per-account slice of the batch audit record.

    Modelled on execution_engine._run_record (the only existing shape that stores the actual
    TARGET WEIGHTS — the thing that makes a record re-derivable years later), plus the two
    fields this lane needs and that one has no concept of: the account's MODEL LABEL, and —
    where that model is an Andrew-authored custom allocation — the published allocation's
    version_number / version_id (via AllocationMeta.stamp(), so the field names match the rest
    of the desk). `meta` is None for an S0 model; the custom_* fields are then absent, which
    is itself the record of "this was a computed model, not a hand-authored book"."""
    legs = result.legs or []
    record = {
        "account": request.account,
        "model": str(model_label),
        "is_custom_allocation": meta is not None,
        "target_as_of": str(getattr(target.as_of, "date", lambda: target.as_of)()),
        "target_price_date": str(getattr(target.price_date, "date",
                                         lambda: target.price_date)()),
        "target_weights": {str(k): round(float(v), 6) for k, v in target.weights.items()},
        "nav": None if request.net_liq is None else round(float(request.net_liq), 2),
        "status": result.status,
        "legs": [{"side": l.side, "sym": l.symbol, "qty": l.qty, "limit": l.limit,
                  "notional": round(float(l.notional), 2)} for l in legs],
        "n_legs": len(legs),
        "order_refs": order_refs_for(request, result, run_id),
        "reasons": list(result.reasons or []),
        "sell_results": list(result.sell_results or []),
        "buy_results": list(result.buy_results or []),
        "n_transmitted": len(result.sell_results or []) + len(result.buy_results or []),
        "margin_preflight_ok": margin_ok,
    }
    if meta is not None:
        record.update(meta.stamp())
    return record


def batch_run_record(*, run_id, mode, accounts, summary, skipped, armed, kill,
                     permitted) -> dict:
    """The ONE ledger record per batch run. `accounts` are account_audit_record dicts.

    Top-level keys mirror what ledger.record_run's human log line reads (mode/account/nav/
    n_intents/n_approved/n_transmitted/halted) so `paperbot.log` stays scannable, and
    `run_id` is the join key: every orderRef this run put on the wire ends in it, and
    ledger.find_run(run_id) brings an examiner back here from any one of them.

    Written for an S0-only batch exactly as for a custom one — the trail must not exist only
    for the new feature."""
    n_legs = sum(a["n_legs"] for a in accounts)
    return {
        "mode": mode,
        "account": f"<batch of {len(accounts)} account(s)>",
        "run_id": run_id,
        "nav": round(sum(a["nav"] or 0.0 for a in accounts), 2),
        "daily_pnl": 0.0,
        # execution_engine's record is single-account and stores ONE target_as_of/weights set.
        # A batch spans models, so the per-account entries carry those; these two are kept at
        # the top level only so the record's SHAPE stays recognisable to an existing reader.
        "target_as_of": "per-account (see accounts[].target_as_of)",
        "target_weights": {},
        "accounts": accounts,
        "n_intents": n_legs,
        "n_approved": n_legs,
        "n_transmitted": sum(a["n_transmitted"] for a in accounts),
        "halted": False,
        "halt_reason": "",
        "order_vetoes": [],
        "batch_vetoes": [],
        "n_roster": summary.get("n_roster"),
        "n_out_of_spec": summary.get("n_out_of_spec"),
        "n_in_spec": summary.get("n_in_spec"),
        "skipped_accounts": list(skipped),
        "total_sells": round(float(summary.get("total_sells") or 0.0), 2),
        "total_buys": round(float(summary.get("total_buys") or 0.0), 2),
        "gate": {"armed": armed, "kill_switch": kill, "permitted": permitted,
                 "port": LIVE_TRADE_PORT},
        **version.stamp(),
    }


# ========================================================================================
# SAFETY BANNER + SUMMARY PRINTING.
# ========================================================================================
def _safety_banner(roster_accounts: list[str], versions: dict[str, str], armed: bool,
                   kill: bool) -> None:
    permit_intent = armed and not kill
    roster_str = "{" + ", ".join(roster_accounts) + "}" if roster_accounts else "{}"
    print("\n" + "#" * 92)
    print(f"# SAFETY STATE   armed={armed}   arm_token={'present' if armed else 'absent'}   "
          f"kill_switch={'PRESENT' if kill else 'absent'}")
    print(f"# roster (execution allow-list, {len(roster_accounts)} account(s)): {roster_str}")
    print(f"#   (account wall: refuses ANY account not on the enrolled roster)")
    print(f"# gateway=Live-Trade port {LIVE_TRADE_PORT} (one FA master login for all accounts)")
    print(f"# per account: purpose=REBALANCE   conform=off   two-phase cash-gated   "
          f"per-account margin pre-flight (#57)")
    if permit_intent:
        print("# *** ARMED INTENT: this run MAY transmit a TWO-PHASE cash-gated rebalance on")
        print("#     EACH out-of-spec roster account IF that account's full gate passes.")
        print("#     Review every account's order list below. ***")
    else:
        print("# PREVIEW: sizes every roster account, prints each order list + margin pre-")
        print("# flight, and transmits NOTHING (not armed, or kill switch present).")
    print("#" * 92)


def main(armed: bool = False, today: object = None) -> int:
    """BATCH REBALANCE executor. PREVIEW by default; transmits per-account ONLY when armed AND
    every per-account gate passes. `today` is accepted for signature parity with the other
    runners; the shared brain always runs to the most recent data date."""
    print("=" * 92)
    print(f"BATCH REBALANCE EXECUTOR (multi-account, roster-scoped) — preview by default, "
          f"per-account two-phase rebalance when armed   [{version.banner()}]")
    print("=" * 92)

    # [1] The human-blessed execution roster (allow-list) + each account's model version.
    print("\n[1] Resolving the human-blessed execution roster (roster.enrolled_roster)...")
    try:
        roster_accounts = roster.enrolled_roster()
    except Exception as exc:  # noqa: BLE001 — never crash before the safety banner
        print(f"    COULD NOT RESOLVE ROSTER: {type(exc).__name__}: {exc}. Nothing "
              f"connected, nothing transmitted.")
        return 2
    if not roster_accounts:
        print("    The enrolled execution roster is EMPTY — nothing to rebalance. Nothing "
              "connected, nothing transmitted.")
        return 0
    try:
        versions = resolve_roster_versions(roster_accounts)
    except Exception as exc:  # noqa: BLE001 — nothing is connected yet; refuse loudly
        # Reachable when the CRM IS configured but the custom-allocation read fails: we then
        # cannot tell an Andrew-authored book from an S0 model, and guessing either way is a
        # mis-sized account. Fail closed.
        print(f"    COULD NOT RESOLVE PER-ACCOUNT MODEL VERSIONS: {type(exc).__name__}: "
              f"{exc}. Nothing connected, nothing transmitted.")
        return 2
    if _TIER_MISMATCHES:
        # The desk overrode the CRM's stored model for these accounts. That is the pre-trade
        # check doing its job, but it also means the system of record is STALE — say so here
        # rather than let a silent override hide a dead tier scan.
        print("")
        print(f"    !! MODEL TIER OVERRIDE - the CRM record disagrees with NAV for "
              f"{len(_TIER_MISMATCHES)} account(s).")
        print(f"       The desk is sizing against the NAV-correct model, NOT the stored one. "
              f"The CRM record is stale;")
        print(f"       check that 'daily-model-tier-scan' is still running.")
        for acct, (stored, effective, nav) in sorted(_TIER_MISMATCHES.items()):
            print(f"         {acct}: NAV ${nav:,.0f}  stored='{stored}'  -> using '{effective}'")
    print(f"    roster ({len(roster_accounts)} account(s)):")
    for a in roster_accounts:
        print(f"      {a}  ->  {versions[a]}")

    # [2] Compute a target per DISTINCT version BEFORE connecting (fail fast on stale data;
    # connect nothing on failure).
    distinct_versions = sorted(set(versions.values()))
    print(f"\n[2] Computing target(s) for {len(distinct_versions)} distinct version(s) "
          f"(shared brain; stale-data guarded)...")
    try:
        targets, metas = build_targets(distinct_versions)
    except TargetBuildFailed as exc:
        # SAME fail-closed contract as before: a target that cannot be built stops the run
        # before anything connects. A CUSTOM target fails exactly as loudly as an S0 one —
        # it must never fall through to the S0 backtester or to an empty book.
        print(f"    COULD NOT BUILD TARGET for {exc.label!r}: {exc}. Nothing connected, "
              f"nothing transmitted.")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"    COULD NOT BUILD TARGET for {distinct_versions!r}: {exc}. Nothing "
              f"connected, nothing transmitted.")
        return 2
    for v in distinct_versions:
        t = targets[v]
        meta = metas.get(v)
        stamp = ("" if meta is None
                 else f"  [CUSTOM allocation v{meta.version_number} "
                      f"version_id={meta.version_id}]")
        print(f"    {t.version:13s} as_of={t.as_of.date()}  price_date={t.price_date.date()}"
              f"  ({len(t.weights)} holdings){stamp}")

    kill = _kill_switch_present()
    permit_intent = armed and not kill

    # [3] Safety banner.
    _safety_banner(roster_accounts, versions, armed, kill)

    # [4] Connect ONCE. ARMED intent -> the transmit-capable lane (readonly=False); otherwise
    # the read-only lane. A bare armed connection still transmits nothing on its own — only
    # execute_plan's gated placeOrder does. Whole session in try/finally so it ALWAYS
    # disconnects.
    if permit_intent:
        print(f"\n[3] Connecting ARMED (readonly=False) to the Live-Trade Gateway "
              f"(port {LIVE_TRADE_PORT})...")
        try:
            ib = s0_live.connect_s0_live_armed()
        except Exception as exc:  # noqa: BLE001
            print(f"    could not connect ARMED to the Live-Trade Gateway (port "
                  f"{LIVE_TRADE_PORT}): {type(exc).__name__}: {exc}. Nothing sized, nothing "
                  f"transmitted.")
            return 1
        armed_conn = True
    else:
        print(f"\n[3] Connecting READ-ONLY to the Live-Trade Gateway (port {LIVE_TRADE_PORT}) "
              f"for the preview...")
        try:
            ib = s0_live.connect_s0_live()
        except Exception as exc:  # noqa: BLE001
            print(f"    could not connect READ-ONLY to the Live-Trade Gateway (port "
                  f"{LIVE_TRADE_PORT}): {type(exc).__name__}: {exc}. Nothing sized, nothing "
                  f"transmitted.")
            return 1
        armed_conn = False

    try:
        return run_batch_session(ib, roster_accounts, versions, targets,
                                 armed=armed, armed_conn=armed_conn, kill=kill,
                                 metas=metas)
    finally:
        try:
            ib.disconnect()
        except Exception:  # noqa: BLE001
            pass
        print("Session closed.")


def run_batch_session(ib, roster_accounts: list[str], versions: dict[str, str],
                      targets: dict, *, armed: bool, armed_conn: bool, kill: bool,
                      metas: dict | None = None) -> int:
    """Read each blessed account off the ONE 4003 login, size it with the frozen engine, and
    drive safe_execute.execute_plan per OUT-OF-SPEC account (PREVIEW or ARMED). Reads the
    broker; every transmit decision lives inside the shared engine's gate.

    `metas` maps a CUSTOM model label -> its custom_target.AllocationMeta (absent for an S0
    label). It is the SOURCE-based "is this account on a hand-authored book?" test for the two
    things this function then does differently: the per-account tradeable universe, and the
    allocation version stamped into the audit record."""
    metas = metas or {}
    # ONE run identifier for the whole batch. Stamped onto every ExecutionRequest, so every
    # orderRef on the wire ends in it, and written into the single ledger record below — the
    # join key between an IBKR order and the book that produced it.
    run_id = safe_execute._run_id()
    strat_universe = sp._strategy_universe()

    # [4] Read the whole login's account summary + positions ONCE, then filter per account.
    print(f"\n[4] Reading account summary + positions for {len(roster_accounts)} roster "
          f"account(s) off the one FA-master login...")
    all_summary = ib.accountSummary()
    all_positions = ib.positions()

    # Union of every version's target symbols + every held symbol across the roster, for one
    # batched live-quote fetch.
    held_symbols: set[str] = set()
    per_account_state: dict[str, dict] = {}
    for account in roster_accounts:
        summary = s0_live.filter_account_summary(all_summary, account=account)
        net_liq = sp._net_liq(summary) if summary else None
        positions_raw = s0_live.filter_positions(all_positions, account=account)
        positions = {p.contract.symbol: p.position
                     for p in positions_raw if p.position != 0}
        # HELD-ASIDE classification input: the broker's OWN contract.secType ("STK",
        # "BOND", ...) — the authoritative signal, never a guess off the ticker text.
        # The engine uses it to carve instruments the desk never trades out of NetLiq,
        # size the model against the remaining sleeve, and refuse to emit a leg for one.
        # A position whose secType is blank/unrecognised is held aside and flagged, not
        # traded (fail closed).
        sec_types = {p.contract.symbol: getattr(p.contract, "secType", None)
                     for p in positions_raw if p.position != 0}
        held_symbols |= set(positions)
        per_account_state[account] = {
            "summary": summary, "net_liq": net_liq, "positions": positions,
            "sec_types": sec_types}

    target_symbols: set[str] = set()
    for t in targets.values():
        target_symbols |= set(t.weights.index)
    universe = sorted(target_symbols | held_symbols)

    print(f"\n[5] Fetching live quotes for {len(universe)} symbol(s) on port "
          f"{LIVE_TRADE_PORT}...")
    quotes = live_quotes.fetch(ib, universe)
    prices: dict = {}
    for sym in universe:
        q = quotes.get(sym)
        ref = live_quotes.reference_price(q) if q else None
        if ref and ref > 0:
            prices[sym] = ref
        else:
            # Fall back to any version's target close for the symbol.
            for t in targets.values():
                px = t.prices.get(sym)
                if px is not None and float(px) == float(px):
                    prices[sym] = float(px)
                    break

    # [6] Size each roster account with the UNCHANGED engine. Refuse (skip) an account with no
    # readable positive NetLiq — an unfunded/invisible account cannot be acted on.
    print("\n[6] Sizing each roster account with rebalance_engine.plan_account (UNCHANGED "
          "engine)...")
    plans: list = []
    summaries: dict = {}
    skipped: list[str] = []
    for account in roster_accounts:
        st = per_account_state[account]
        v = versions[account]
        target = targets[v]
        net_liq = st["net_liq"]
        if not net_liq or net_liq <= 0:
            print(f"    {account} [{v}]: no readable positive NetLiquidation under the login "
                  f"— SKIPPED (nothing sized, nothing transmitted).")
            skipped.append(account)
            continue
        acct_universe = account_universe(target, metas.get(v), st["positions"],
                                         base=strat_universe)
        # Per-model CASH RESERVE, from the same source-based `meta` signal as the universe:
        # 1% for a hand-authored allocation, the global 1.5% for S0. ONE value per account,
        # handed to the engine once — the engine then uses it for BOTH sizing and the CASH
        # line's drift target, so the account can never be sized to one reserve and measured
        # against another.
        acct_reserve = account_reserve_pct(metas.get(v))
        if metas.get(v) is not None:
            lost_alien_review = sorted(set(st["positions"]) - set(target.weights.index))
            if lost_alien_review:
                print(f"      custom-allocation universe: {len(acct_universe)} symbol(s). "
                      f"ROTATABLE (held but not in the published allocation, so they can be "
                      f"SOLD rather than sitting there forever as ALIEN): "
                      f"{', '.join(lost_alien_review)}")
        plan = rebalance_engine.plan_account(
            account, target.version, net_liq, st["positions"], target,
            prices=prices, universe=acct_universe, sec_types=st["sec_types"],
            cash_reserve_pct=acct_reserve)
        plans.append(plan)
        summaries[account] = st["summary"]
        print(f"    {account} [{v}]: NetLiq={net_liq:,.2f}  positions={len(st['positions'])}"
              f"  cash reserve held back={acct_reserve * 100:.2f}%"
              f"  would-trade legs={sum(1 for d in plan.orders.values() if int(d) != 0)}")
        # Held-aside holdings are priced, counted and named here — never folded silently
        # into NAV, and never a leg. The model sized the MANAGED sleeve only.
        if plan.held_aside:
            print(f"      held aside (never traded, outside the model allocation): "
                  f"{len(plan.held_aside)} holding(s) worth "
                  f"{plan.held_aside_value:,.2f}; model applied to the remaining "
                  f"{plan.managed_net_liq:,.2f}")
            for h in plan.held_aside:
                mv = "unpriced" if h.market_value is None else f"{h.market_value:,.2f}"
                print(f"        {h.sec_type:8s} {h.symbol:26s} qty={h.quantity:>14,.2f}"
                      f"  value={mv:>16s}   {h.reason}")
        for reason in plan.blocked_reasons:
            print(f"      ORDERS HELD BACK: {reason}")

    # [7] Map the sized plans -> per-account ExecutionRequests (PURE; REUSES crm_execute). The
    # helper SKIPS in-band accounts, so `requests` is exactly the OUT-OF-SPEC subset. Every
    # request carries allowed_accounts=the roster (the independent account wall).
    requests = build_batch_requests(
        plans, targets=targets, quotes=quotes, prices=prices,
        roster_accounts=roster_accounts, summaries=summaries, armed=armed, kill=kill,
        run_id=run_id)
    out_of_spec_accounts = [r.account for r in requests]
    in_spec_accounts = [p.account for p in plans if p.account not in out_of_spec_accounts]

    print(f"\n[7] Out-of-spec roster accounts to rebalance: "
          f"{len(out_of_spec_accounts)} of {len(plans)} sized "
          f"({', '.join(out_of_spec_accounts) or 'none'}).")
    if in_spec_accounts:
        print(f"    In-spec (already conform — nothing to trade): "
              f"{', '.join(in_spec_accounts)}")

    # [8] Drive the shared engine per out-of-spec account. PREVIEW unless the whole run is
    # armed on the transmit lane. Each account prints its own [7]/[8]/[9] order list + gate,
    # then we print the self-computed per-account margin pre-flight (#57).
    mode = MODE_ARMED if armed_conn else MODE_PREVIEW
    results: list = []
    margin_flags: list = []
    for req in requests:
        print("\n" + "-" * 92)
        print(f"--- BATCH ACCOUNT {req.account} [{req.strategy_version}] "
              f"(purpose=REBALANCE) ---")
        result = safe_execute.execute_plan(req, mode=mode, ib=ib)
        results.append(result)
        mg_ok, mg_reason = margin_preflight_line(req, result)
        margin_flags.append(mg_ok)
        print(f"    margin_preflight_ok={mg_ok}   (#57 self-computed per-account pre-flight)"
              + (f"   reason: {mg_reason}" if not mg_ok else ""))
        # A parseable per-account line for the Control Plane's batch preview.
        n_legs = len(result.legs or [])
        n_sell = sum(1 for l in (result.legs or []) if l.side == "SELL")
        n_buy = sum(1 for l in (result.legs or []) if l.side == "BUY")
        print(f"    BATCH-ACCOUNT account={req.account} version={req.strategy_version} "
              f"status={result.status} legs={n_legs} sells={n_sell} buys={n_buy} "
              f"margin_preflight_ok={mg_ok}")

    # [9] Aggregate summary.
    summary = summarize_batch(plans, requests, results)
    print("\n" + "=" * 92)
    print("BATCH SUMMARY")
    print(f"    BATCH-SUMMARY roster={summary['n_roster']} out_of_spec="
          f"{summary['n_out_of_spec']} in_spec={summary['n_in_spec']} skipped={len(skipped)} "
          f"total_legs={summary['total_legs']} total_sells={summary['total_sells']:.2f} "
          f"total_buys={summary['total_buys']:.2f}")

    # [10] THE AUDIT RECORD — one ledger row per batch run, PREVIEW or ARMED. This executor
    # wrote nothing to the ledger before v0.37.0, so a batch left only the per-leg
    # transmit_journal and the orderRef: no stored weights, no model label, and — for an
    # Andrew-authored book — no way at all to tell which published allocation version produced
    # a fill (the ref carries only a DATE, and two allocations can be published the same day).
    # Never let a ledger-write failure affect the trading outcome: the trades are already
    # decided and (if armed) already sent.
    try:
        record = batch_run_record(
            run_id=run_id,
            mode=("BATCH_REBALANCE_ARMED" if armed_conn else "BATCH_REBALANCE_PREVIEW"),
            accounts=[
                account_audit_record(
                    req, res,
                    model_label=versions.get(req.account, req.strategy_version),
                    target=req.target,
                    meta=metas.get(versions.get(req.account, req.strategy_version)),
                    run_id=run_id, margin_ok=mg)
                for req, res, mg in zip(requests, results, margin_flags)],
            summary=summary, skipped=skipped, armed=armed, kill=kill,
            permitted=armed_conn)
        ledger.record_run(record)
        print(f"    BATCH-LEDGER run_id={run_id} accounts={len(record['accounts'])} "
              f"(every orderRef this run uses ends in ':{run_id}'; "
              f"ledger.find_run('{run_id}') returns this record)")
    except Exception as exc:  # noqa: BLE001
        print(f"    !! COULD NOT WRITE THE BATCH AUDIT RECORD: {type(exc).__name__}: {exc}. "
              f"The run itself is unaffected; the trail for run_id={run_id} is INCOMPLETE.")

    # Terminal batch verdict: transmitted anything, or preview-only.
    any_transmitted = any(
        r.status in (safe_execute.STATUS_COMPLETE, safe_execute.STATUS_PARTIAL_LOUD)
        for r in results)
    if any_transmitted:
        print("\nBATCH ARMED COMPLETE — review every account's fills above and DISARM the "
              "Gateway when finished.")
    else:
        print("\nBATCH TRANSMISSION BLOCKED — PREVIEW ONLY. Nothing was placed, armed, or "
              "sent on any account.")
    return 0


def cli(argv: list[str] | None = None) -> int:
    """CLI entry: --arm-i-understand sets armed=True (the only thing that arms). No token ->
    preview that transmits nothing."""
    argv = sys.argv[1:] if argv is None else argv
    return main(armed=arm_requested(argv))


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(cli())
