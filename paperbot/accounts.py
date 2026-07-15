"""
accounts.py — multi-account discovery for the FA structure. READ-ONLY.

The first concrete step of the Option B (allocation/multi-account) build. It does
NOTHING but look: it connects read-only to the paper Gateway and reports the full
account structure the master can see, so the rest of the engine is built against
REALITY instead of an assumption about which sub-accounts exist or are funded.

For every managed account it reports:
  * kind    — FA master (DF...) vs client/sub (DU...) vs unknown
  * NetLiq, total cash, open-position count
  * funded  — NetLiq > 0 (an empty reserved sub can't be rebalanced yet)
  * enrolled — is it in config.ENROLLMENT, and to which strategy version

It also RECONCILES enrollment vs reality and flags the two mistakes that bite:
  * an enrolled account that the master can't actually see (typo / not linked yet)
  * a visible, funded sub-account that is NOT enrolled (would be silently skipped)

It places no orders, forms no orders, and changes nothing. `discover(ib)` is the
importable entry the multi-account engine reuses; `main()` prints the report.

Run (gateway auto-starts if down):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe ^
    "C:\\Users\\andre\\My Drive (andrew@surberhc.com)\\TradingDesk\\paperbot\\accounts.py"
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime

import config
from connections import clientids, ibkr_paper
from gateway_lock import GatewayBusySkip, gateway_lock


# --- account-feed field parsing (PURE — no broker) -----------------------------
def parse_settled_cash_by_date(value: str | None) -> tuple[date, float] | None:
    """Parse IBKR's `SettledCashByDate` account tag.

    REAL FORMAT (confirmed by a live read-only probe 2026-06-30): there is NO flat
    `SettledCash` tag. Settled cash arrives as a STRING shaped 'YYYYMMDD:amount', e.g.
    '20260630:51755.46' (the currency field on the row is empty -> treat the amount as
    the account base currency / USD). It MUST be split — `float('20260630:51755.46')`
    raises — so this is the single place that decoding lives.

    Returns (settle_date, amount) on success, or None when the value is missing, empty,
    or malformed (a missing/garbled tag must never be mistaken for a $0 settled balance,
    which could spuriously look like a withdrawal — so we return None, not (date, 0.0)).

    PURE: no broker handle, no I/O. The live shell (Slice 6b) reads the raw tag string
    off ib.accountSummary and hands it here.
    """
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if ":" not in raw:
        return None
    date_part, _, amount_part = raw.partition(":")
    date_part = date_part.strip()
    amount_part = amount_part.strip()
    if not date_part or not amount_part:
        return None
    try:
        settle_date = datetime.strptime(date_part, "%Y%m%d").date()
        amount = float(amount_part)
    except ValueError:
        return None
    return settle_date, amount


@dataclass
class AccountInfo:
    """Read-only snapshot of one managed account. No orders, ever."""
    number: str
    kind: str            # "FA master" | "client (sub)" | "unknown"
    is_master: bool
    net_liq: float       # NetLiquidation (0.0 if not reported)
    total_cash: float
    n_positions: int
    funded: bool         # net_liq > 0 -> there is something to rebalance
    enrolled: bool       # present in config.ENROLLMENT
    version: str | None  # enrolled strategy version, or None


def _tag(summary, account: str, tag: str, default: float = 0.0) -> float:
    for row in summary:
        if row.account == account and row.tag == tag:
            try:
                return float(row.value)
            except ValueError:
                return default
    return default


def _classify(number: str) -> tuple[str, bool]:
    """(kind, is_master). Paper FA master is DF...; client sub-accounts are DU..."""
    if number.startswith("DF"):
        return "FA master", True
    if number.startswith("DU"):
        return "client (sub)", False
    return "unknown", False


def discover(ib) -> list[AccountInfo]:
    """Read-only: enumerate every managed account and snapshot it. Importable."""
    infos: list[AccountInfo] = []
    for number in ib.managedAccounts():
        kind, is_master = _classify(number)
        summary = ib.accountSummary(number)
        net_liq = _tag(summary, number, "NetLiquidation")
        total_cash = _tag(summary, number, "TotalCashValue")
        n_positions = sum(1 for p in ib.positions(number) if p.position != 0)
        infos.append(AccountInfo(
            number=number,
            kind=kind,
            is_master=is_master,
            net_liq=net_liq,
            total_cash=total_cash,
            n_positions=n_positions,
            funded=net_liq > 0,
            enrolled=number in config.ENROLLMENT,
            version=config.ENROLLMENT.get(number),
        ))
    return infos


def reconcile_enrollment(infos: list[AccountInfo]) -> list[str]:
    """The two enrollment mistakes that bite. Returns human-readable warnings."""
    warnings: list[str] = []
    visible = {i.number for i in infos}

    # 1) Enrolled but invisible (typo, or sub not linked under the master yet).
    for number in config.ENROLLMENT:
        if number not in visible:
            warnings.append(f"enrolled account {number} is NOT visible under the master "
                            f"(typo, or not linked/funded yet) — the engine will skip it.")

    # 2) A version typo in the enrollment map.
    for number, version in config.ENROLLMENT.items():
        if version not in config.VALID_VERSIONS:
            warnings.append(f"{number} is enrolled to unknown version '{version}' "
                            f"(valid: {', '.join(config.VALID_VERSIONS)}).")

    # 3) Visible, funded client sub that is NOT enrolled -> silently un-rebalanced.
    for i in infos:
        if (not i.is_master) and i.funded and not i.enrolled:
            warnings.append(f"client account {i.number} is funded (NetLiq {i.net_liq:,.2f}) "
                            f"but NOT enrolled — it would be left out of any rebalance.")
    return warnings


def main() -> int:
    print("=" * 86)
    print("FA MULTI-ACCOUNT DISCOVERY - read-only, no orders")
    print(f"Connecting to PAPER gateway {ibkr_paper.HOST}:{ibkr_paper.PAPER_PORT} "
          f"(clientId={clientids.get('paperbot_accounts')}, readonly=True)")
    print("=" * 86)

    try:
        with gateway_lock(purpose="accounts",
                          client_id=clientids.get("paperbot_accounts"), on_busy="skip"):
            try:
                # Own clientId (paperbot_accounts) so discovery can run even while the
                # execution engine (clientId 30) is connected.
                ib = ibkr_paper.connect("paperbot_accounts", readonly=True, launch=True)
            except Exception as exc:
                print("\nCOULD NOT CONNECT.")
                print(f"  reason: {exc}")
                print(f"  -> Is IB Gateway up and logged into PAPER, API on port {ibkr_paper.PAPER_PORT}?")
                return 1

            try:
                infos = discover(ib)
                if not infos:
                    print("\nSTOP: the gateway reported no managed accounts.")
                    return 2

                # Confirm exactly one FA master, and that it ends in our configured suffix.
                masters = [i for i in infos if i.is_master]
                if len(masters) == 1 and masters[0].number.endswith(config.ACCOUNT_SUFFIX):
                    print(f"\nFA master: {masters[0].number} (suffix '{config.ACCOUNT_SUFFIX}' OK)")
                else:
                    print(f"\nWARNING: expected one FA master ending in '{config.ACCOUNT_SUFFIX}', "
                          f"found masters={[m.number for m in masters]}.")

                # --- The account table ---
                print(f"\n{'ACCOUNT':12s} {'KIND':13s} {'NETLIQ':>14s} {'CASH':>14s} "
                      f"{'POS':>4s}  {'FUNDED':>6s}  {'ENROLLED / VERSION'}")
                print("-" * 86)
                for i in sorted(infos, key=lambda x: (not x.is_master, x.number)):
                    enroll = (f"{i.version}" if i.enrolled
                              else ("(advisor acct)" if i.is_master else "NOT ENROLLED"))
                    print(f"{i.number:12s} {i.kind:13s} {i.net_liq:>14,.2f} {i.total_cash:>14,.2f} "
                          f"{i.n_positions:>4d}  {'yes' if i.funded else 'no':>6s}  {enroll}")
                print("-" * 86)

                # --- Enrollment vs reality ---
                warnings = reconcile_enrollment(infos)
                print("\nEnrollment reconciliation:")
                if not warnings:
                    print("  clean - every enrolled account is visible, valid, and funded.")
                else:
                    for w in warnings:
                        print(f"  ! {w}")
            # (ASCII only in console output - the paper Gateway console is cp1252.)

                n_ready = sum(1 for i in infos if i.enrolled and i.funded and not i.is_master)
                print(f"\n{n_ready} client account(s) enrolled AND funded -> rebalanceable now.")
                print("Done. Nothing was transmitted. Read-only session closing.")
                return 0
            finally:
                ib.disconnect()
    except GatewayBusySkip as busy:
        holder = busy.holder or {}
        print(f"\ngateway busy — held by {holder.get('purpose')} pid {holder.get('pid')} "
              f"clientId {holder.get('client_id')} since "
              f"{holder.get('acquired_at') or holder.get('acquired_ts')}; skipping this "
              f"probe. (Read-only; nothing read or transmitted.)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
