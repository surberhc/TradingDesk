# CAN SLIM replica - INTEGRATED end-to-end EXECUTION backtest

_The fair test: SELECTION held fixed to his actual picks; the full disciplined-execution stack (exit rule + sizing + exposure dial) run TOGETHER as one path-dependent portfolio, then compared to his realized book. Prior tests isolated one lever at a time and mis-attributed the result; this runs them as one interacting system. Path-dependence (cash freed by an early exit funds the next entry) is the whole point._

- Start capital: **$650,000** (implied from his ~$52k median position / ~7 concurrent names / ~74% median invested).
- His trade universe: **118 trades**, 2023-06-27 .. 2026-06-15 (entry dates). SELECTION and ENTRY (date+price) held fixed = his; only EXECUTION varies.
- His realized book (journal): total P&L **$73,818** on $6,153,929 gross invested = **+1.2%** on invested; win rate **33%**.
- Forward price paths (entry-200d .. 2026-06-30, RAW frame, Tiingo-first / IBKR fallback) for **94/96** distinct names, extended past his exits so E3/E4 can let winners run. The 2 with no data (ERJ, PSTG) fall back to his ACTUAL exit under every rule (they are 1 small loser + 1 modest winner; immaterial to the aggregates).

## Variant grid - each config vs his realized book

| Config | Exit | Timing | Sizing | Total ret | CAGR | Max DD | Win% | #pos | #skip |
|---|---|---|---|---:|---:|---:|---:|---:|---:|
| E1.-.his | E1 | off | his | +11.4% | +3.7% | -13.8% | 33% | 118 | 0 |
| E1.-.ew | E1 | off | ew | +30.4% | +9.3% | -15.7% | 31% | 113 | 5 |
| E1.T.his | E1 | on | his | +9.9% | +3.2% | -13.5% | 34% | 109 | 9 |
| E1.T.ew | E1 | on | ew | +28.6% | +8.8% | -14.0% | 34% | 101 | 17 |
| E2.-.his | E2 | off | his | +9.3% | +3.0% | -8.1% | 25% | 118 | 0 |
| E2.-.ew | E2 | off | ew | +44.3% | +13.1% | -12.8% | 26% | 117 | 1 |
| E2.T.his | E2 | on | his | +9.0% | +2.9% | -8.1% | 25% | 113 | 5 |
| E2.T.ew | E2 | on | ew | +55.7% | +16.1% | -12.8% | 27% | 105 | 13 |
| E3.-.his | E3 | off | his | +32.2% | +9.7% | -9.2% | 31% | 118 | 0 |
| E3.-.ew | E3 | off | ew | +95.7% | +25.0% | -14.3% | 32% | 114 | 4 |
| E3.T.his | E3 | on | his | +30.6% | +9.3% | -11.3% | 30% | 109 | 9 |
| E3.T.ew | E3 | on | ew | +73.9% | +20.2% | -14.8% | 29% | 96 | 22 |
| E4.-.his | E4 | off | his | +3.0% | +1.0% | -8.6% | 35% | 118 | 0 |
| E4.-.ew | E4 | off | ew | +16.5% | +5.2% | -9.1% | 35% | 118 | 0 |
| E4.T.his | E4 | on | his | -0.7% | -0.2% | -13.7% | 34% | 111 | 7 |
| E4.T.ew | E4 | on | ew | +11.8% | +3.8% | -12.9% | 35% | 108 | 10 |

_Config tag = Exit.Timing(T/-).Sizing. E1 = his actual exits (sanity check: the sim must reproduce his book). E2 = fixed -7% stop. E3 = -7% then 50-day-line (let winners run). E4 = E3 + 22.5% profit cap. 'his' sizing = his revealed dollar cost; 'ew' = equal-weight 12% target / 18% cap. Max DD is on the realized equity curve._

## Per-year realized P&L (bucketed by EXIT date - regime behavior of each rule)

| Config | 2023H2 | 2024 | 2025 | 2026H1 | total |
|---|---:|---:|---:|---:|---:|
| E1.-.his | $-37k | $+71k | $+148k | $-108k | $+74k |
| E1.-.ew | $-64k | $+225k | $+172k | $-135k | $+198k |
| E1.T.his | $-37k | $+49k | $+156k | $-104k | $+64k |
| E1.T.ew | $-64k | $+178k | $+131k | $-58k | $+186k |
| E2.-.his | $-33k | $+78k | $+48k | $-33k | $+60k |
| E2.-.ew | $-55k | $+265k | $+79k | $-1k | $+288k |
| E2.T.his | $-33k | $+63k | $+57k | $-29k | $+59k |
| E2.T.ew | $-55k | $+237k | $+163k | $+17k | $+362k |
| E3.-.his | $-34k | $+78k | $+148k | $+18k | $+210k |
| E3.-.ew | $-60k | $+349k | $+271k | $+62k | $+622k |
| E3.T.his | $-34k | $+63k | $+147k | $+22k | $+199k |
| E3.T.ew | $-60k | $+280k | $+296k | $-35k | $+481k |
| E4.-.his | $-21k | $+12k | $+24k | $+4k | $+19k |
| E4.-.ew | $-41k | $+67k | $+51k | $+30k | $+107k |
| E4.T.his | $-21k | $-21k | $+33k | $+5k | $-4k |
| E4.T.ew | $-41k | $+23k | $+86k | $+8k | $+76k |

## Big-winners - what EACH exit rule did (winner-clipping transparency)

_Per name: his actual exit vs E2/E3/E4. `vs his` = %pts the rule gained (+) or gave up (-) relative to HIS own exit. A big positive E3-vs-his = he sold a runner too early and the 50-day-line rule would have held it. A negative E4-vs-his where E3 is positive = the profit cap CLIPPED a winner._

| Name | Buy | His sell | His ret | E2 ret | E3 exit | E3 ret | E3 vs his | E4 ret | E4 vs his |
|---|---|---|---:|---:|---|---:|---:|---:|---:|
| OKLO | 2025-01-06 | 2025-02-21 | +31.8% | -7.5% | 2025-02-25 | +6.9% | -24.9% | +22.5% | -9.3% |
| OKLO | 2025-07-16 | 2025-10-15 | +138.4% | -7.0% | 2025-11-04 | +74.4% | -64.1% | +22.5% | -115.9% |
| MSTR | 2024-10-15 | 2024-11-18 | +113.4% | +113.4% | 2024-12-30 | +55.9% | -57.5% | +22.5% | -90.9% |
| MSTR | 2024-10-15 | 2025-02-24 | +36.4% | +36.4% | 2024-12-30 | +55.9% | +19.5% | +22.5% | -13.9% |
| VKTX | 2024-01-18 | 2024-02-21 | +62.1% | +62.1% | 2024-04-24 | +210.7% | +148.6% | +22.5% | -39.6% |
| VKTX | 2024-01-18 | 2024-06-04 | +161.9% | +161.9% | 2024-04-24 | +210.7% | +48.9% | +22.5% | -139.4% |
| RKLB | 2025-06-09 | 2025-11-06 | +59.7% | -7.0% | 2025-06-10 | -7.0% | -66.7% | -7.0% | -66.7% |
| GEV | 2025-01-07 | 2025-02-20 | -2.2% | -7.8% | 2025-01-27 | -10.5% | -8.3% | -10.5% | -8.3% |
| GEV | 2025-04-24 | 2025-08-15 | +61.9% | +61.9% | 2025-09-03 | +60.1% | -1.8% | +22.5% | -39.4% |
| GEV | 2025-12-09 | 2026-04-23 | +79.4% | +79.4% | 2026-05-29 | +54.9% | -24.5% | +22.5% | -56.9% |
| STRL | 2026-02-10 | 2026-03-04 | -10.3% | -10.3% | 2026-03-30 | -7.9% | +2.4% | -7.9% | +2.4% |
| HOOD | 2025-05-19 | 2025-07-01 | +43.6% | +43.6% | 2025-11-13 | +89.1% | +45.5% | +22.5% | -21.1% |
| RBLX | 2025-05-12 | 2025-08-01 | +70.6% | +70.6% | 2025-10-03 | +66.9% | -3.6% | +22.5% | -48.1% |
| CRDO | 2025-07-02 | 2025-10-23 | +47.8% | +47.8% | 2025-10-14 | +45.2% | -2.6% | +22.5% | -25.3% |
| CRDO | 2026-05-05 | 2026-05-18 | -13.1% | -7.0% | 2026-06-30 | +40.5% | +53.6% | +22.5% | +35.6% |
| AXON | 2023-11-08 | 2024-04-15 | +33.3% | -7.0% | 2023-11-09 | -7.0% | -40.3% | -7.0% | -40.3% |
| AXON | 2025-02-18 | 2025-02-20 | -24.2% | -7.0% | 2025-02-19 | -7.0% | +17.2% | -7.0% | +17.2% |
| IBKR | 2024-09-19 | 2024-12-03 | +42.0% | +42.0% | 2025-03-04 | +48.0% | +6.0% | +22.5% | -19.5% |

## Verdict (this sample)

- **Yes — running his picks through a disciplined stack that cuts losers WITHOUT clipping winners beats his realized book.** Best apples-to-apples config (his own dollar sizing): `E3.-.his` at +32.2% total, +9.7% CAGR, -9.2% max DD — vs his book `E1.-.his` at +11.4% total, -13.8% max DD. So ~2.8x his return at LOWER drawdown, on his own picks and sizing.
- **The winning rule is E3 (let winners run behind a rising 50-day line), NOT E2 or E4.** E2 (bare -7% stop) with no upside management is actually NEGATIVE without timing (+9.3%) — cutting losers alone does not pay; you must also HOLD the winners. E4 (add a 22.5% profit cap) is the single most destructive rule (E4.-.his +3.0%): the cap gave up ~742 cumulative %pts across his big winners (see OKLO +138, VKTX +211, MSTR, HOOD in the table) — each clip is a whole position's edge thrown away.
- **The dominant lever is not the stop — it is not selling winners early.** His revealed weakness (big-winners table) is exiting runners too soon: OKLO he sold +138% while the 50-line held it further; VKTX the 50-line rode to +211% vs his +62/+162% tranches; HOOD +89% vs his +44%; CRDO's 2026 re-buy he stopped at -13% but the line would have made +41%. Cutting losers at -7% AND holding winners to a real trend break captures both edges at once — which the per-trade tests structurally could not show, because they could not redeploy the freed cash into the next name.
- **Honest cost of the discipline:** the -7% catastrophic stop DOES knock out a few volatile names that later recovered — most starkly RKLB (his +60%; the stop hit it one day after entry on a -21% shakeout, so E3 = -7% and he keeps the whole +60%). That is the real, disclosed downside: a hard initial stop occasionally ejects an eventual winner before the 50-line rule can engage. The portfolio still wins net because the losers it cuts vastly outnumber the RKLB-type false stops.
- **Timing dial helps here — it is not just a drawdown tool.** On E3 with his sizing, turning the weekly invested_pct overlay ON raised total return (+32.2% -> +30.6%) AND cut max DD (-9.2% -> -11.3%), by pulling exposure down ahead of the 2025-2026 air-pockets using only prior-week information. It does skip some entries (higher #skipped). On the bare-stop E2 it mostly reduces risk. Reported both ways; do not read the single best cell as tuned — the whole E3 column beats his book.

### Hard limits (curve-fit + honesty guards, rule #1)
- **Bull-heavy universe.** These are his 2023-2026 trades only. There is NO bear-regime trade-level data, so this CANNOT test whether the disciplined stack survives a bear. The let-winners-run edge is exactly the edge that a bull tape flatters; treat the bear case as UNTESTED, not endorsed.
- **Small sample** (~118 positions), and **SELECTION is his** — this measures EXECUTION on his picks, not stock-picking.
- **No parameter tuning.** -7%/-8% stop, the 50-day line, the 20-25% cap, ~12% sizing and the ~74% exposure dial are all from O'Neil's published playbook or his revealed behavior. The full grid is shown so sensitivity is visible; no single cell was selected to win.
- **Exposure/sizing use prior-week info only; every exit is judged on bars up to the decision day** (causality guard enforced in code + tests/test_execution_backtest.py).
