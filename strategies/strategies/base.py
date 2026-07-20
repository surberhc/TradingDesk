"""
base.py — the Strategy interface shared by the backtester and the paperbot.

A Strategy is a PURE decision function: given market state (prices + macro up to a
point in time) and the positions currently held, it returns the target portfolio it
WANTS to hold. The same Strategy object is driven by two runners:

  * the backtester  -> simulates rebalancing to the target weights, and
  * the paperbot    -> diffs the target weights against the broker's actual paper
                       positions and places real paper orders for the gap.

Because both runners call the identical decision code, the backtest and the paper
book cannot silently diverge — the classic killer this design exists to prevent.

Look-ahead is the caller's responsibility: MarketState must carry only data on or
before `as_of`. A strategy never reaches outside the state object for data.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class MarketState:
    """Everything a strategy needs to decide, as of a single point in time.

    The provider (the backtester loop, or the paperbot's market-state provider)
    guarantees every series/frame contains data on/before `as_of` only.
    """

    prices: pd.DataFrame                # adjusted daily prices; columns = tickers
    macro: dict                         # {'hyg','credit_denom','yield_10y','vix','hy_oas'}; any may be None
    as_of: pd.Timestamp                 # the decision date T (a rebalance signal date for S0)
    positions: pd.Series | None = None  # target weights currently held (last rebalance)


@dataclass
class TargetWeights:
    """A strategy's decision for one rebalance: the portfolio it wants to hold.

    This is the INTENT. A runner turns it into action:
      * backtester -> simulated rebalance to these weights,
      * paperbot   -> diff vs broker positions -> paper limit orders for the deltas.
    """

    weights: pd.Series                  # ticker -> fraction, sums to 1.0
    as_of: pd.Timestamp
    sleeves: dict = field(default_factory=dict)     # {'equity','defense','real_asset'} fractions
    real_asset: str | None = None                   # held hedge ticker (or basket label), if any
    reasons: list = field(default_factory=list)     # human-readable reason codes
    extras: dict = field(default_factory=dict)      # strategy-specific diagnostics (regime, score, …)


class StrategyBase(ABC):
    """The interface every strategy implements. Two runners, one decision function."""

    name: str = "unnamed"
    params: dict

    @abstractmethod
    def warmup(self, prices: pd.DataFrame, macro: dict, start: str, end: str | None) -> None:
        """Precompute causal signals over history and determine the rebalance dates.

        Must use only trailing windows so any value at date T depends solely on data
        on/before T. After warmup, `self.signal_dates` lists the dates the runner
        should rebalance on.
        """

    @abstractmethod
    def on_data(self, state: MarketState) -> TargetWeights:
        """Return the target weights to hold as of state.as_of."""

    def on_fill(self, fill) -> None:
        """Hook for paper fills. Allocation strategies carry no per-fill state, so the
        default is a no-op; override if a strategy must react to partial fills."""
        return None

    def universe(self) -> set[str]:
        """The full set of symbols this strategy can EVER hold — the union across every
        sleeve and every regime it can select. This is the durable seam the paperbot
        reconciler uses to tell a legitimate model rotation-out (a symbol the strategy
        knows, dropped to 0% this cycle) apart from an alien/corporate-action holding
        (a symbol the strategy has never known). It is READ-ONLY — it describes what the
        strategy can trade, it decides nothing.

        No default is safe: a strategy that does not declare its universe would make the
        reconciler read EVERY held symbol as alien. So the base refuses rather than guess;
        each concrete strategy must self-describe."""
        raise NotImplementedError(
            f"{type(self).__name__} must declare universe() — the set of symbols it can "
            f"ever hold (union across sleeves/regimes).")
