# Prompts for Backtester — Extracted

**Source:** Screenshots from the YouTube video *"Claude Tested Over 9,000 Trading Strategies (Here's What Works)"* by **AI Pathways** (channel), from the file `Prompts for Backtester.docx`. The doc was purely a container of 11 full-screen screenshots (no typed text); the four prompts below were transcribed verbatim from the on-screen "LAYER N PROMPT" slides. Each slide footer read: *"↳ pause & screenshot — paste straight into Claude Code."*

**System overview (from the intro slides):** A 4-layer Python strategy-testing system. Test every popular strategy in base (naked) form across ~30 assets and 15 years of daily bars, then validate hard — walk-forward out-of-sample, six filters, realistic costs, and robustness checks — to separate a real edge from an overfit backtest.

---

## Layer 1 — Data & Strategy Library

```
# We're building a strategy testing system in Python, in four
# layers. This is Layer 1: the data and the strategy library.
# Use yfinance, numpy, pandas. Keep modules importable by later
# layers. Build it as one script I can run in Claude Code.

DATA: download daily OHLCV with yfinance, auto_adjust=True, from
2010-01-01 to 2025-01-01, for ~30 liquid assets: index ETFs (SPY,
QQQ, IWM, DIA), sector ETFs (XLK, XLF, XLE, XLV, XLI, XLU, XLY,
XLP), commodities/rates/intl (GLD, USO, TLT, HYG, EFA, EEM, EWZ),
crypto (BTC-USD, ETH-USD), and large caps (AAPL, MSFT, NVDA, TSLA,
AMZN, GOOGL, META, JPM). Skip any asset with under 500 bars. Keep
Open/High/Low/Close/Volume so indicators that need them work.

STRATEGY LIBRARY: implement the full popular-retail spectrum.
Each strategy is a function that takes a price dataframe plus its
parameters and returns a daily position series in {-1, 0, 1},
long / flat / short, with NO look-ahead (shift signals by one bar
so today's position only uses data up to yesterday). Tag each one
with a category: trend, meanrev, volume, volatility, pattern, or
composite.

# The families to implement (these are the recognizable ones):

TREND: MA crossover, time-series momentum, ROC momentum, MACD,
  Donchian breakout, Bollinger breakout, Supertrend, Parabolic SAR,
  ADX trend, Ichimoku, linreg slope, Aroon, Vortex, TRIX, Hull MA,
  KAMA, Turtle, Dual Momentum, Elder Ray.
MEAN REVERSION: RSI revert, Bollinger revert, z-score revert,
  stochastic, CCI, Williams %R, Keltner revert, VWAP revert,
  percent-B, Connors RSI, Ultimate Oscillator, gap fade.
VOLUME: OBV trend, Chaikin money flow, money flow index,
  volume surge, Force Index, Chaikin oscillator.
VOLATILITY: ATR breakout, volatility breakout, squeeze breakout.
PATTERN: engulfing, three-bar reversal, higher-highs/lows,
  pivot bounce.
COMPOSITE: MACD+RSI confirmation, triple-screen, chandelier.

PARAMETER GRID: give each family a small grid of settings (e.g.
MA crossover over several fast/slow pairs, RSI revert over a few
lookback and threshold combos). A build_configs() function returns
(name, function, params, category) tuples. The grids should make
the total land in the hundreds of configs, so that running every
config across all ~30 assets gives you thousands of backtests.
Print the total count.
```

---

## Layer 2 — Backtest & Funnel

```
# Layer 2, building on Layer 1. Add the backtest engine, the
# walk-forward validation, and the six-filter survival funnel.

BACKTEST: a strategy's daily return is its position times the
asset's next-day return, minus a per-side transaction cost
(default 1bp, configurable, and you can set it higher for crypto).
Compute three metrics on any return series: annualized Sharpe
(mean/std times sqrt(252)), max drawdown, and trade count (number
of times the position changes).

WALK-FORWARD (this is the important part): split each asset's
history into 5 sequential windows. Within each window, the first
70% is in-sample and the last 30% is out-of-sample. Run the
strategy and keep ONLY the out-of-sample tail of each window. Stitch
all 5 out-of-sample tails into one combined series, and score
Sharpe and max drawdown on that stitched out-of-sample series. That
combined out-of-sample number is the one that matters, because the
strategy was never tuned on it.

# The sweep, the filters, and the funnel report.

SWEEP: run every (config x asset) combination. For each one
record: in-sample Sharpe, out-of-sample Sharpe (from the stitched
walk-forward series), out-of-sample max drawdown, and trade count.
Write it all to sweep_results.csv and print the total backtest
count.

SIX FILTERS (applied to the out-of-sample results, all the
thresholds configurable). A strategy survives only if it passes
ALL of these:
  1. out-of-sample max drawdown better than -35%
  2. out-of-sample Sharpe above 0.5
  3. out-of-sample Sharpe below 2.5 (above that = too good, the
     asset did the work, not the strategy)
  4. out-of-sample Sharpe not more than ~30% above in-sample
     (a big gap is the overfit signature)
  5. at least 30 trades (so the result is statistically meaningful)
  6. in-sample Sharpe positive

FUNNEL REPORT: print the attrition, total backtests, then how
many had positive out-of-sample, how many cleared 0.5, how many
survived all six. Then survival rate by category and by family with
mean out-of-sample Sharpe, and a top-survivors table. This funnel
is the centerpiece.
```

---

## Layer 3 — Robustness Checks

```
# Layer 3, building on the funnel. Add two robustness checks that
# catch survivors that only worked by luck or one magic setting.

PARAMETER SENSITIVITY: for each strategy family, group all its
parameter configs and report the mean out-of-sample Sharpe, the
standard deviation of out-of-sample Sharpe across those configs,
and the fraction of configs with a positive out-of-sample Sharpe.
The logic: if a family only works on one exact setting and falls
apart on the others, that is a red flag, it was probably curve fit.
A tight spread with a high positive fraction means the edge is real
and not dependent on one magic number.

BOOTSTRAP STRESS TEST: for each top survivor, take its
out-of-sample daily returns and reshuffle the order a few hundred
times (default 200). Each reshuffle gives a different equity path,
so you get a distribution of outcomes instead of the single one
that happened. Report the 5th, 50th, and 95th percentile Sharpe,
and the worst-case drawdown across the reshuffles. The point: a
survivor whose worst case is still survivable is trustworthy, but
one that looks great only in the exact order history happened to
fall is fragile. Flag each survivor solid or fragile based on its
worst-case drawdown, and write the results out so they can be
charted later.
```

---

## Layer 4 — Cross-Sectional Momentum Check

```
# Layer 4, a standalone check. In the main sweep, momentum was
# tested on each asset by itself and scored near zero. But the
# strongest documented form is cross-sectional, ranking assets
# against each other. Build that and compare.

CROSS-SECTIONAL MOMENTUM: use the same asset universe and the
same minimum-history filter as the main tester. Every 21 trading
days (about monthly), rank all the assets by their trailing return.
Test a few lookbacks: 3 months, 6 months, and the standard
12-months-minus-the-most-recent-month version (skipping the last
month avoids short-term reversal). Go long the top third of ranked
assets and short the bottom third, equal weight, and hold until the
next rebalance. Apply the same realistic per-asset transaction
costs on the turnover.

VALIDATION: score it the same way as the main tester, an
in-sample vs out-of-sample split plus a walk-forward out-of-sample
Sharpe and drawdown, so the number is directly comparable to the
single-asset momentum from the sweep.

REPORT: the out-of-sample Sharpe at each lookback, side by side
with the single-asset momentum result, and a plain statement of
whether ranking assets against each other beat trading momentum on
each one alone. Report the drawdowns as they come out, they tend to
be deep, and note if the result leaned on one market regime. Write
the results to a csv. Do not tune it to look good, report what
happens.
```
