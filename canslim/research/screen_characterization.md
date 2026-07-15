# APS revealed-screen characterization (Phase 2)

Reverse-engineering the deterministic CAN SLIM screen implied by APS's own labeled behavior,
from `labeled_picks.csv` (Phase 1). Distributions are computed at the moment a name was
**watch-listed** (Trade-ideas table, weekly plan files) and, separately, at the moment it was
**bought** (journal MANAGED-ACCT block, ratings recorded the week of purchase).

**Sample sizes:** 6666 watch-list rows (name-weeks), 118 distinct buys, 1771 holding rows.
Watch-list rows are name-weeks (a name persists on the list for multiple weeks), so a metric's
distribution is weighted by dwell time. Buys are one row per purchase.

## 1. Rating distributions: WATCH-LIST vs BUY

### At WATCH-LIST  (n=6666)

| metric | n | min | 10th | 25th | median | 75th | 90th | max |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| RS rating | 6656 | 9 | 79 | 87 | 92 | 96 | 98 | 99 |
| EPS rating | 6582 | 1 | 33 | 72 | 87 | 95 | 98 | 99 |
| IBD Composite | 0 | - | - | - | - | - | - | - |
| Group/Industry rank (lower=better) | 1495 | 1 | 5 | 16 | 43 | 82 | 122 | 192 |

### At BUY  (n=118)

| metric | n | min | 10th | 25th | median | 75th | 90th | max |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| RS rating | 118 | 1 | 85 | 89 | 94 | 97 | 98 | 99 |
| EPS rating | 114 | 4 | 34 | 74 | 83 | 98 | 99 | 99 |
| IBD Composite | 113 | 49 | 81 | 89 | 97 | 99 | 99 | 99 |
| Group/Industry rank (lower=better) | 114 | 2 | 9 | 20 | 38 | 63 | 101 | 161 |

## 2. What fraction cleared common CAN SLIM thresholds

| threshold | watch-list | buy |
|---|--:|--:|
| RS >= 80 | 90% | 94% |
| RS >= 90 | 65% | 75% |
| EPS >= 80 | 64% | 73% |
| EPS >= 90 | 43% | 44% |
| Composite >= 90 | - | 74% |
| Composite >= 95 | - | 59% |
| Group rank in top 40 | 48% | 53% |
| Group rank in top 20 | 32% | 28% |

## 3. Empirical threshold capturing ~90% of picks (round numbers only)

Lower 10th-percentile of each 'higher-is-better' metric, floored to a round CAN SLIM number.
This is the threshold ~90% of his picks already satisfy - a *descriptive* floor, not an optimized rule.

| metric | ~90% floor (watch) | ~90% floor (buy) |
|---|--:|--:|
| RS | >= 75 | >= 85 |
| EPS | >= 30 | >= 30 |
| Composite | n/a (not on watch tables) | >= 80 |
| Group rank | top 120 (weak filter) | top 100 (weak filter) |

## 4. How close to the pivot does he BUY?

Two views. **Watch-list** `pct_from_pivot` = (current price - pivot)/pivot, signed
(negative = below pivot / still basing, positive = extended above). **Buy-fill** =
(cost-per-share - pivot)/pivot, reconstructed from the journal (n=117).

**Watch list (n=3895):** median 2.2%; 25th -2.9%, 75th 6.9%; 10th -8.4%,
90th 11.4%. 38% of watch-name-weeks sit *below* the pivot (still basing, not yet
actionable); only 48% are within +/-5%.

**Buy fills (n=117):** median **+2.6%** from pivot; 25th +0.6%, 75th +4.7%;
10th -2.8%, 90th +9.1%.
- Within +/-5% of pivot: **72%**;  within +/-8%: 83%;  within +/-3%: 47%.
- At or below pivot+5% (i.e. not chasing): **78%**.
- 79% of buys are *above* the pivot (breakout confirmation), 21% at/below it.

**Verdict:** yes - he buys within ~5% of the pivot (72% of fills), typically a hair
*above* it (median +2.6%), i.e. he waits for the breakout to trigger rather than
anticipating it, and he does not chase extended names. The tail buys past +8% (17%)
are the exception, not the rule.

## 5. Moving-average positioning (% above 10wk / 21d / 50d) at WATCH-LIST

Only the 2020+ plan layout records these (earlier sheets are sparser).

| MA | n | 10th | 25th | median | 75th | 90th | % above (>=0) |
|---|--:|--:|--:|--:|--:|--:|--:|
| % above 10-wk SMA | 3513 | -0.7% | 2.9% | 7.0% | 12.1% | 19.0% | 88% |
| % above 21-day EMA | 3513 | -2.4% | 0.2% | 3.2% | 6.5% | 10.6% | 77% |
| % above 50-day SMA | 3513 | -0.0% | 3.7% | 8.0% | 13.2% | 20.9% | 90% |

## 6. Base / setup language in comments (frequency counts, not NLP)

Watch-list rows with a comment: 6641. Buy rows with a note: 27.

| keyword/pattern | watch-list hits | % of commented watch rows |
|---|--:|--:|
| cup | 3 | 0% |
| handle | 389 | 6% |
| flat base | 17 | 0% |
| base (generic) | 1469 | 22% |
| breakout / broke out | 1971 | 30% |
| pivot | 1580 | 24% |
| weeks tight / tight | 643 | 10% |
| consolidat* | 181 | 3% |
| double bottom | 137 | 2% |
| flag / pennant | 27 | 0% |
| 50dma / 21dma / 10wk (MA ref) | 1615 | 24% |
| extended | 1194 | 18% |
| volume | 1240 | 19% |
| RS line / new high | 462 | 7% |

**Base-stage language** (O'Neil base counting - later-stage bases are riskier):

| stage keyword | watch-list hits | % of commented rows |
|---|--:|--:|
| 1st stage base | 28 | 0% |
| 2nd stage base | 73 | 1% |
| 3rd+ stage base (late) | 38 | 1% |
| any 'stage base' | 135 | 2% |
| cup / c-with-h / c/h | 3 | 0% |

Note: he rarely writes the word "cup" (assumes cup-with-handle as the default O'Neil
base); the *actionable* setup vocabulary is **breakout/pivot** (~30%/24% of comments),
**base + stage-count** (22% mention a base, base-stage explicitly counted), and
**handle/tight** (6%/10%). This is the discretionary base-SHAPE judgment that the
numeric screen cannot capture.

## 7. Revealed screen spec (plain English)

The screen that reproduces ~90% of APS's actual behavior, stated in round, principled
CAN SLIM terms (descriptive floors his picks already satisfy - NOT an optimized fit):

**WATCH-LIST filter (the wide net):**
1. **RS rating >= 80** (hard) - 90% of watch-name-weeks clear it; median RS is 92. This is
   his single dominant, near-inviolable filter. Relative strength is the gate.
2. **EPS rating: soft / no hard floor** - only 64% clear EPS>=80 and the 10th percentile is ~30.
   He watch-lists plenty of low-EPS names (turnarounds, biotech, recent IPOs) on RS + chart
   alone. EPS is a *tiebreaker*, not a gate, at the watch stage.
3. **Liquid US growth names near a base/pivot** - the watch list is a leadership universe, not
   a valuation screen.
4. (Group/industry rank is a WEAK filter here: median group rank 43, and ~half sit outside the
   top 40 - he does not require a top-ranked industry to watch a name.)

**BUY filter (materially tighter than watch):**
1. **RS >= 85** (up from 80); 94% of buys clear RS>=80, 75% clear RS>=90 - he buys the
   strongest of the watch list. Median buy RS = 94.
2. **EPS tightens** to 73% >= 80 (from 64%), median 83 - fundamentals matter more at the trigger.
3. **IBD Composite >= 90** for ~74% of buys (Composite is only recorded at buy time, median 97) -
   the Composite is effectively a buy-gate even though it never appears on the watch tables.
4. **Price within ~5% of pivot, slightly above it** - median buy is +2.6% past the pivot; 72%
   within +/-5%, 78% at-or-below +5%. He waits for the breakout to confirm and does not chase.
5. Position is above its key moving averages at watch time (88% above the 10-wk, 90% above the
   50-day) - trend confirmation.

**How much tighter is BUY than WATCH?** Every rating shifts up at the trigger: the RS floor
moves 80 -> 85, RS>=90 share 65% -> 75%, EPS>=80 share 64% -> 73%, and a Composite>=90 gate
(~74%) appears that isn't visible on the watch list at all. The watch list is a broad RS-led
leadership pool; the buy is a tighter multi-factor + timing confirmation drawn from it.

## 8. What is NOT inferable from this data (the Phase-3 gap)

The numeric screen above is necessary but **not sufficient** to reproduce his picks. What the
spreadsheets do not encode - and what a deterministic replica must therefore test in Phase 3:

- **The discretionary base-shape / chart judgment.** He picks *which* RS>=80 names to watch and
  *when* the base is "right" (cup-with-handle vs flat base, handle quality, base stage/count,
  "weeks tight", tightness of the pivot). His comments (section 6) reference bases, handles,
  stages, and breakouts constantly, but the shape decision itself is visual and discretionary.
  This is the single biggest un-inferable gap.
- **Point-in-time universe & ranking.** The watch list is already pre-filtered (we only see the
  names he chose, not the full IBD universe he screened from). We cannot recover his starting
  scan or how many RS>=80 names he rejected, so we cannot measure the screen's precision/recall
  without an external point-in-time RS/EPS/Composite dataset (survivorship-free).
- **Composite & group rank at WATCH time.** Composite is only recorded at buy; group rank is
  only on the OLD-era watch layout. We cannot fully characterize the watch-stage Composite gate
  from these files - Phase 3 needs point-in-time IBD ratings to fill this in.
- **Volume / accumulation confirmation.** ~19% of comments mention volume, but the tables carry
  no volume or up/down-volume figures; the breakout-volume test is not reconstructable here.
- **Market-timing overlay.** Buys are gated by his IBD Market-Pulse allocation band (0-100%
  invested), captured separately in the plan files - the same name can be watch-listed but not
  bought purely because the market is in correction. That timing layer is orthogonal to the
  per-name screen and must be modeled separately.

**Curve-fit discipline note:** every threshold above is a round CAN SLIM number (80/85/90) that
his picks *already* satisfy at the stated coverage - chosen as a descriptive lower bound, not
tuned to maximize fit. No multi-parameter rule was optimized against this sample. The honest
read is: RS>=80 (watch) / RS>=85 + Composite>=90 + within ~5% of pivot (buy) captures the
*quantitative* skeleton; the base-shape discretion is the irreducible remainder Phase 3 exists
to test.
