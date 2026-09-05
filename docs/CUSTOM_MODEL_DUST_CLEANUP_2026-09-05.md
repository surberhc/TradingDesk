# Custom-Model "Foreign Holding" Cleanup List

Generated 2026-09-05 from a live snapshot of `run_foreign_holding_scan()`'s open
`foreign_holding` tasks (469 open tasks at generation time), for D.5 fix 4.

**This is a reference list only — nothing has been sold or modified.** Each row is
a position that needs to be sold out of the account, most of them by hand in the
TWS desktop application (see the "Fractional?" column — IBKR's API refuses
fractional-share sell orders; whole-share rows can go through the desk normally).

## Summary by cause

| Cause | Tasks | Accounts | Total value |
|---|---:|---:|---:|
| Legacy fractional dust (never in any model version) | 265 | 134 | $13,104.55 |
| Tier-boundary stranding (left behind after a retier) | 193 | 50 | $9,396.87 |
| Pre-existing individual bond / legacy holding | 11 | 5 | $135,091.41 |
| **Total** | **469** | **146** | **$157,592.83** |

How each cause was derived (same tie-break logic `v_tradingdesk_custom_allocations`
and `run_foreign_holding_scan()` already use, re-applied here against the live
open-task set):
- **Legacy fractional dust** — fractional share count, and the symbol has never
  appeared in any `custom_allocation_version_lines` row (any strategy, any version,
  published or not). All-cash-management symbols (BIL, SIL).
- **Tier-boundary stranding** — fractional share count, but the symbol IS currently
  published in some *other* strategy's model — i.e. this account used to be on a
  bigger/different tier that held this symbol, was retiered, and the fractional
  remainder never got swept.
- **Pre-existing bond / legacy holding** — whole-share count, never in any model
  version, currently individual bonds identified by CUSIP-style symbols.

## Full list

### Legacy fractional dust (never in any model version) (265 tasks)

| Account | Custodian | Model | Household | Symbol | Quantity | Market Value | Sell by hand in TWS? |
|---|---|---|---|---|---:|---:|:---:|
| U10238346 | IBKR | Growth (Custom) | Smith, Timothy | BIL | 0.6995 | $63.97 | YES (fractional) |
| U10238346 | IBKR | Growth (Custom) | Smith, Timothy | SIL | 0.9561 | $94.93 | YES (fractional) |
| U10377115 | IBKR | Growth (Custom) | Holman, Maureen | BIL | 0.5189 | $47.45 | YES (fractional) |
| U10377115 | IBKR | Growth (Custom) | Holman, Maureen | SIL | 0.9848 | $97.78 | YES (fractional) |
| U10377192 | IBKR | Growth (Custom) | Holman, Maureen | BIL | 0.0984 | $9.00 | YES (fractional) |
| U10377192 | IBKR | Growth (Custom) | Holman, Maureen | SIL | 0.6681 | $66.34 | YES (fractional) |
| U10386224 | IBKR | Growth (Custom) | Holman, Maureen | BIL | 0.9699 | $88.70 | YES (fractional) |
| U10386224 | IBKR | Growth (Custom) | Holman, Maureen | SIL | 0.0469 | $4.66 | YES (fractional) |
| U10694255 | IBKR | Growth (Custom) | Smith, Timothy | BIL | 0.4445 | $40.65 | YES (fractional) |
| U10694255 | IBKR | Growth (Custom) | Smith, Timothy | SIL | 0.8919 | $88.56 | YES (fractional) |
| U10702931 | IBKR | Growth (Custom) | Bennett, Christine | BIL | 0.7564 | $69.17 | YES (fractional) |
| U10702931 | IBKR | Growth (Custom) | Bennett, Christine | SIL | 0.7881 | $78.25 | YES (fractional) |
| U10850704 | IBKR | Starter (Custom) | Koch, Caleb | BIL | 0.0203 | $1.86 | YES (fractional) |
| U10850704 | IBKR | Starter (Custom) | Koch, Caleb | SIL | 0.7841 | $77.85 | YES (fractional) |
| U11030394 | IBKR | Growth (Small, Custom) | Strahm, Jacob and Kelsi | BIL | 0.8235 | $75.31 | YES (fractional) |
| U11030394 | IBKR | Growth (Small, Custom) | Strahm, Jacob and Kelsi | SIL | 0.0569 | $5.65 | YES (fractional) |
| U11406664 | IBKR | Growth (Small, Custom) | Monaghan, Courtney | BIL | 0.6362 | $58.18 | YES (fractional) |
| U11406664 | IBKR | Growth (Small, Custom) | Monaghan, Courtney | SIL | 0.5559 | $55.20 | YES (fractional) |
| U11406689 | IBKR | Growth (Small, Custom) | Monaghan, Courtney | BIL | 0.8476 | $77.51 | YES (fractional) |
| U11406689 | IBKR | Growth (Small, Custom) | Monaghan, Courtney | SIL | 0.158 | $15.69 | YES (fractional) |
| U11493329 | IBKR | Growth (Custom) | Koch, Caleb | BIL | 0.5211 | $47.65 | YES (fractional) |
| U11493329 | IBKR | Growth (Custom) | Koch, Caleb | SIL | 0.5782 | $57.41 | YES (fractional) |
| U11498959 | IBKR | Growth (Custom) | Koch, Caleb | BIL | 0.588 | $53.77 | YES (fractional) |
| U11498959 | IBKR | Growth (Custom) | Koch, Caleb | SIL | 0.2217 | $22.01 | YES (fractional) |
| U11604818 | IBKR | Growth (Small, Custom) | Monaghan, Courtney | BIL | 0.078 | $7.13 | YES (fractional) |
| U11604818 | IBKR | Growth (Small, Custom) | Monaghan, Courtney | SIL | 0.1684 | $16.72 | YES (fractional) |
| U11725953 | IBKR | Starter (Custom) | Tangeman, John | BIL | 0.1017 | $9.30 | YES (fractional) |
| U11725953 | IBKR | Starter (Custom) | Tangeman, John | SIL | 0.3034 | $30.12 | YES (fractional) |
| U12140145 | IBKR | Growth (Small, Custom) | Rinehart, Alex | BIL | 0.5154 | $47.13 | YES (fractional) |
| U12140145 | IBKR | Growth (Small, Custom) | Rinehart, Alex | SIL | 0.446 | $44.28 | YES (fractional) |
| U12207909 | IBKR | Growth (Custom) | Smith, Erica | BIL | 0.8462 | $77.38 | YES (fractional) |
| U12207909 | IBKR | Growth (Custom) | Smith, Erica | SIL | 0.8658 | $85.97 | YES (fractional) |
| U12214654 | IBKR | Growth (Custom) | Rebant, Kasha | BIL | 0.0139 | $1.27 | YES (fractional) |
| U12214654 | IBKR | Growth (Custom) | Rebant, Kasha | SIL | 0.7808 | $77.53 | YES (fractional) |
| U12223690 | IBKR | Starter (Custom) | Rebant, Kasha | BIL | 0.7294 | $66.70 | YES (fractional) |
| U12223690 | IBKR | Starter (Custom) | Rebant, Kasha | SIL | 0.3647 | $36.21 | YES (fractional) |
| U13014189 | IBKR | Growth (Custom) | Vigneron, James | BIL | 0.2763 | $25.27 | YES (fractional) |
| U13105246 | IBKR | Growth (Small, Custom) | Vigneron, James | BIL | 0.3349 | $30.63 | YES (fractional) |
| U13105246 | IBKR | Growth (Small, Custom) | Vigneron, James | SIL | 0.5726 | $56.85 | YES (fractional) |
| U13105277 | IBKR | Growth (Custom) | Vigneron, James | BIL | 0.9075 | $82.99 | YES (fractional) |
| U13105277 | IBKR | Growth (Custom) | Vigneron, James | SIL | 0.0642 | $6.37 | YES (fractional) |
| U13278415 | IBKR | Growth (Custom) | Kellner, Lance | BIL | 0.7909 | $72.33 | YES (fractional) |
| U13278415 | IBKR | Growth (Custom) | Kellner, Lance | SIL | 0.3136 | $31.14 | YES (fractional) |
| U13656140 | IBKR | Growth (Custom) | Bennett, Christine | BIL | 0.7888 | $72.14 | YES (fractional) |
| U13656140 | IBKR | Growth (Custom) | Bennett, Christine | SIL | 0.2035 | $20.21 | YES (fractional) |
| U13917741 | IBKR | Growth (Small, Custom) | Mealman, Greg | BIL | 0.3517 | $32.16 | YES (fractional) |
| U13917741 | IBKR | Growth (Small, Custom) | Mealman, Greg | SIL | 0.4935 | $49.00 | YES (fractional) |
| U14040689 | IBKR | Growth (Custom) | Loveland, James | BIL | 0.7116 | $65.08 | YES (fractional) |
| U14040689 | IBKR | Growth (Custom) | Loveland, James | SIL | 0.0893 | $8.87 | YES (fractional) |
| U14131321 | IBKR | Growth (Small, Custom) | Seelig, Ariel | BIL | 0.1653 | $15.12 | YES (fractional) |
| U14131321 | IBKR | Growth (Small, Custom) | Seelig, Ariel | SIL | 0.994 | $98.69 | YES (fractional) |
| U14244440 | IBKR | Growth (Small, Custom) | Morris, Avilynn | BIL | 0.5465 | $49.98 | YES (fractional) |
| U14244440 | IBKR | Growth (Small, Custom) | Morris, Avilynn | SIL | 0.0313 | $3.11 | YES (fractional) |
| U14390223 | IBKR | Growth (Small, Custom) | Koch, Caleb | BIL | 0.3972 | $36.32 | YES (fractional) |
| U14390223 | IBKR | Growth (Small, Custom) | Koch, Caleb | SIL | 0.5211 | $51.74 | YES (fractional) |
| U15079695 | IBKR | Starter (Custom) | O'Brian, James | BIL | 0.0735 | $6.72 | YES (fractional) |
| U15079695 | IBKR | Starter (Custom) | O'Brian, James | SIL | 0.5937 | $58.95 | YES (fractional) |
| U15087847 | IBKR | Growth (Small, Custom) | O'Brian, James | BIL | 0.7955 | $72.75 | YES (fractional) |
| U15087847 | IBKR | Growth (Small, Custom) | O'Brian, James | SIL | 0.6343 | $62.98 | YES (fractional) |
| U15164863 | IBKR | Growth (Custom) | Leary, Hettie Ann | BIL | 0.3476 | $31.79 | YES (fractional) |
| U15164863 | IBKR | Growth (Custom) | Leary, Hettie Ann | SIL | 0.6913 | $68.64 | YES (fractional) |
| U15465069 | IBKR | Growth (Custom) | Hill, Donald | BIL | 0.8031 | $73.44 | YES (fractional) |
| U15482451 | IBKR | Growth (Small, Custom) | Hill, Donald | BIL | 0.9309 | $85.13 | YES (fractional) |
| U15482451 | IBKR | Growth (Small, Custom) | Hill, Donald | SIL | 0.5782 | $57.41 | YES (fractional) |
| U15631507 | IBKR | Growth (Small, Custom) | VanCamp, Robert | BIL | 0.2403 | $21.98 | YES (fractional) |
| U15631507 | IBKR | Growth (Small, Custom) | VanCamp, Robert | SIL | 0.5701 | $56.61 | YES (fractional) |
| U15715611 | IBKR | Growth (Custom) | Kelly, Julia | BIL | 0.5563 | $50.87 | YES (fractional) |
| U15715611 | IBKR | Growth (Custom) | Kelly, Julia | SIL | 0.9662 | $95.93 | YES (fractional) |
| U15721144 | IBKR | Growth (Custom) | Kelly, Julia | BIL | 0.9887 | $90.42 | YES (fractional) |
| U15721144 | IBKR | Growth (Custom) | Kelly, Julia | SIL | 0.9316 | $92.50 | YES (fractional) |
| U16645713 | IBKR | Growth (Custom) | Dierker, Patrick | BIL | 0.7951 | $72.71 | YES (fractional) |
| U16645713 | IBKR | Growth (Custom) | Dierker, Patrick | SIL | 0.232 | $23.04 | YES (fractional) |
| U17925010 | IBKR | Growth (Small, Custom) | Kelly, Julia | BIL | 0.6484 | $59.30 | YES (fractional) |
| U17925010 | IBKR | Growth (Small, Custom) | Kelly, Julia | SIL | 0.1257 | $12.48 | YES (fractional) |
| U18428553 | IBKR | Growth (Custom) | Mora, David | BIL | 0.5484 | $50.15 | YES (fractional) |
| U18428553 | IBKR | Growth (Custom) | Mora, David | SIL | 0.0745 | $7.40 | YES (fractional) |
| U18428571 | IBKR | Growth (Custom) | Mora, David | BIL | 0.3438 | $31.44 | YES (fractional) |
| U18428571 | IBKR | Growth (Custom) | Mora, David | SIL | 0.4511 | $44.79 | YES (fractional) |
| U18477945 | IBKR | Growth (Custom) | Helton, Billy | BIL | 0.4976 | $45.51 | YES (fractional) |
| U18477945 | IBKR | Growth (Custom) | Helton, Billy | SIL | 0.3678 | $36.52 | YES (fractional) |
| U18478058 | IBKR | Growth (Small, Custom) | Helton, Billy | BIL | 0.6906 | $63.16 | YES (fractional) |
| U18478058 | IBKR | Growth (Small, Custom) | Helton, Billy | SIL | 0.13 | $12.91 | YES (fractional) |
| U19519195 | IBKR | Growth (Custom) | Kellner, Lance | BIL | 0.0833 | $7.62 | YES (fractional) |
| U19519195 | IBKR | Growth (Custom) | Kellner, Lance | SIL | 0.8647 | $85.86 | YES (fractional) |
| U19756487 | IBKR | Growth (Small, Custom) | Loveland, James | BIL | 0.6281 | $57.44 | YES (fractional) |
| U19756487 | IBKR | Growth (Small, Custom) | Loveland, James | SIL | 0.677 | $67.22 | YES (fractional) |
| U20606359 | IBKR | Growth (Custom) | Roach, Sonya | BIL | 0.2847 | $26.04 | YES (fractional) |
| U20606359 | IBKR | Growth (Custom) | Roach, Sonya | SIL | 0.401 | $39.82 | YES (fractional) |
| U20984696 | IBKR | Growth (Custom) | Ohlhausen, Ward | BIL | 0.9892 | $90.46 | YES (fractional) |
| U20984696 | IBKR | Growth (Custom) | Ohlhausen, Ward | SIL | 0.4841 | $48.07 | YES (fractional) |
| U20984815 | IBKR | Growth (Custom) | Ohlhausen, Ward | BIL | 0.1849 | $16.91 | YES (fractional) |
| U20984815 | IBKR | Growth (Custom) | Ohlhausen, Ward | SIL | 0.9776 | $97.07 | YES (fractional) |
| U20998258 | IBKR | Growth (Custom) | Ohlhausen, Ward | BIL | 0.9459 | $86.50 | YES (fractional) |
| U20998258 | IBKR | Growth (Custom) | Ohlhausen, Ward | SIL | 0.6055 | $60.12 | YES (fractional) |
| U21139799 | IBKR | Growth (Small, Custom) | Boyles, Terrie | BIL | 0.0351 | $3.21 | YES (fractional) |
| U21139799 | IBKR | Growth (Small, Custom) | Boyles, Terrie | SIL | 0.1718 | $17.06 | YES (fractional) |
| U21201742 | IBKR | Growth (Custom) | Roach, Sonya | BIL | 0.2162 | $19.77 | YES (fractional) |
| U21201742 | IBKR | Growth (Custom) | Roach, Sonya | SIL | 0.7856 | $78.00 | YES (fractional) |
| U21212542 | IBKR | Growth (Custom) | Roach, Sonya | BIL | 0.3515 | $32.14 | YES (fractional) |
| U21212542 | IBKR | Growth (Custom) | Roach, Sonya | SIL | 0.7881 | $78.25 | YES (fractional) |
| U21789948 | IBKR | Growth (Custom) | Tuttle, Michael | BIL | 0.8044 | $73.56 | YES (fractional) |
| U21789948 | IBKR | Growth (Custom) | Tuttle, Michael | SIL | 0.1007 | $10.00 | YES (fractional) |
| U21845142 | IBKR | Growth (Custom) | Tuttle, Michael | BIL | 0.8547 | $78.16 | YES (fractional) |
| U21845142 | IBKR | Growth (Custom) | Tuttle, Michael | SIL | 0.6841 | $67.92 | YES (fractional) |
| U22011673 | IBKR | Growth (Custom) | Schlumpberger, Kristin | BIL | 0.15 | $13.72 | YES (fractional) |
| U22011673 | IBKR | Growth (Custom) | Schlumpberger, Kristin | SIL | 0.4669 | $46.36 | YES (fractional) |
| U22725513 | IBKR | Growth (Custom) | Plattner, Denise | BIL | 0.8534 | $78.04 | YES (fractional) |
| U22725513 | IBKR | Growth (Custom) | Plattner, Denise | SIL | 0.6218 | $61.74 | YES (fractional) |
| U22814048 | IBKR | Growth (Custom) | Bigo, Jenelle | BIL | 0.6449 | $58.98 | YES (fractional) |
| U22814048 | IBKR | Growth (Custom) | Bigo, Jenelle | SIL | 0.8355 | $82.96 | YES (fractional) |
| U22835812 | IBKR | Growth (Custom) | Bigo, Jenelle | BIL | 0.8146 | $74.50 | YES (fractional) |
| U22835812 | IBKR | Growth (Custom) | Bigo, Jenelle | SIL | 0.6737 | $66.89 | YES (fractional) |
| U22848377 | IBKR | Growth (Custom) | Bigo, Jenelle | BIL | 0.7934 | $72.56 | YES (fractional) |
| U22848377 | IBKR | Growth (Custom) | Bigo, Jenelle | SIL | 0.5057 | $50.21 | YES (fractional) |
| U22854243 | IBKR | Growth (Small, Custom) | Rider, Steven | BIL | 0.5083 | $46.48 | YES (fractional) |
| U22854243 | IBKR | Growth (Small, Custom) | Rider, Steven | SIL | 0.597 | $59.28 | YES (fractional) |
| U23414640 | IBKR | Growth (Custom) | Rider, Steven | BIL | 0.6611 | $60.46 | YES (fractional) |
| U23414640 | IBKR | Growth (Custom) | Rider, Steven | SIL | 0.7719 | $76.64 | YES (fractional) |
| U23416743 | IBKR | Growth (Custom) | Sperfslage, Joseph | BIL | 0.1921 | $17.57 | YES (fractional) |
| U23416743 | IBKR | Growth (Custom) | Sperfslage, Joseph | SIL | 0.2108 | $20.93 | YES (fractional) |
| U24316643 | IBKR | Starter (Custom) | Grimes, Todd | BIL | 0.0642 | $5.87 | YES (fractional) |
| U24316643 | IBKR | Starter (Custom) | Grimes, Todd | SIL | 0.3148 | $31.26 | YES (fractional) |
| U24331878 | IBKR | Starter (Custom) | Grimes, Todd | BIL | 0.9036 | $82.63 | YES (fractional) |
| U24331878 | IBKR | Starter (Custom) | Grimes, Todd | SIL | 0.3024 | $30.03 | YES (fractional) |
| U7333179 | IBKR | Starter (Custom) | Brown, Calahan | BIL | 0.4905 | $44.86 | YES (fractional) |
| U7333179 | IBKR | Starter (Custom) | Brown, Calahan | SIL | 0.244 | $24.23 | YES (fractional) |
| U7333190 | IBKR | Growth (Custom) | Decker, Charles | BIL | 0.581 | $53.13 | YES (fractional) |
| U7333190 | IBKR | Growth (Custom) | Decker, Charles | SIL | 0.6879 | $68.30 | YES (fractional) |
| U7333196 | IBKR | Growth (Custom) | Mady, Matthew | BIL | 0.3152 | $28.83 | YES (fractional) |
| U7333196 | IBKR | Growth (Custom) | Mady, Matthew | SIL | 0.9757 | $96.88 | YES (fractional) |
| U7333204 | IBKR | Growth (Custom) | Phipps, Brian | BIL | 0.037 | $3.38 | YES (fractional) |
| U7333204 | IBKR | Growth (Custom) | Phipps, Brian | SIL | 0.2196 | $21.80 | YES (fractional) |
| U7333206 | IBKR | Growth (Custom) | Mealman, Greg | BIL | 0.89 | $81.39 | YES (fractional) |
| U7333206 | IBKR | Growth (Custom) | Mealman, Greg | SIL | 0.728 | $72.28 | YES (fractional) |
| U7333207 | IBKR | Growth (Custom) | Kring, Robert | BIL | 0.0751 | $6.87 | YES (fractional) |
| U7333207 | IBKR | Growth (Custom) | Kring, Robert | SIL | 0.1429 | $14.19 | YES (fractional) |
| U7333210 | IBKR | Growth (Custom) | Heskett, Andrew | BIL | 0.2823 | $25.82 | YES (fractional) |
| U7333210 | IBKR | Growth (Custom) | Heskett, Andrew | SIL | 0.4143 | $41.14 | YES (fractional) |
| U7333217 | IBKR | Starter (Custom) | Husted, Danielle | BIL | 0.0252 | $2.30 | YES (fractional) |
| U7333217 | IBKR | Starter (Custom) | Husted, Danielle | SIL | 0.9788 | $97.19 | YES (fractional) |
| U7333225 | IBKR | Growth (Custom) | Jackson, Joshua | BIL | 0.7115 | $65.07 | YES (fractional) |
| U7333225 | IBKR | Growth (Custom) | Jackson, Joshua | SIL | 0.0854 | $8.48 | YES (fractional) |
| U7333241 | IBKR | Growth (Custom) | Grimes, Todd | BIL | 0.9337 | $85.39 | YES (fractional) |
| U7333241 | IBKR | Growth (Custom) | Grimes, Todd | SIL | 0.4522 | $44.90 | YES (fractional) |
| U7333242 | IBKR | Growth (Small, Custom) | Brown, Andrea | BIL | 0.5014 | $45.85 | YES (fractional) |
| U7333242 | IBKR | Growth (Small, Custom) | Brown, Andrea | SIL | 0.9935 | $98.64 | YES (fractional) |
| U7333249 | IBKR | Growth (Custom) | Mealman, Greg | BIL | 0.0828 | $7.57 | YES (fractional) |
| U7333249 | IBKR | Growth (Custom) | Mealman, Greg | SIL | 0.4037 | $40.08 | YES (fractional) |
| U7333250 | IBKR | Growth (Custom) | Edelman, Duey | BIL | 0.2832 | $25.90 | YES (fractional) |
| U7333250 | IBKR | Growth (Custom) | Edelman, Duey | SIL | 0.6793 | $67.45 | YES (fractional) |
| U7333252 | IBKR | Growth (Custom) | Jackson, Abby and John | BIL | 0.6545 | $59.85 | YES (fractional) |
| U7333252 | IBKR | Growth (Custom) | Jackson, Abby and John | SIL | 0.2272 | $22.56 | YES (fractional) |
| U7333254 | IBKR | Growth (Small, Custom) | Heskett, Andrew | BIL | 0.5557 | $50.82 | YES (fractional) |
| U7333254 | IBKR | Growth (Small, Custom) | Heskett, Andrew | SIL | 0.7672 | $76.18 | YES (fractional) |
| U7333258 | IBKR | Growth (Custom) | Tangeman, John | BIL | 0.6904 | $63.14 | YES (fractional) |
| U7333258 | IBKR | Growth (Custom) | Tangeman, John | SIL | 0.4493 | $44.61 | YES (fractional) |
| U7349569 | IBKR | Growth (Custom) | Tangeman, John | BIL | 0.8136 | $74.40 | YES (fractional) |
| U7349569 | IBKR | Growth (Custom) | Tangeman, John | SIL | 0.1547 | $15.36 | YES (fractional) |
| U7349579 | IBKR | Growth (Custom) | Himes, Rebecca | BIL | 0.8668 | $79.27 | YES (fractional) |
| U7349579 | IBKR | Growth (Custom) | Himes, Rebecca | SIL | 0.2253 | $22.37 | YES (fractional) |
| U7349586 | IBKR | Growth (Custom) | Plattner, Denise | BIL | 0.4803 | $43.92 | YES (fractional) |
| U7349586 | IBKR | Growth (Custom) | Plattner, Denise | SIL | 0.4809 | $47.75 | YES (fractional) |
| U7349599 | IBKR | Growth (Custom) | Grimes, Todd | BIL | 0.246 | $22.50 | YES (fractional) |
| U7349599 | IBKR | Growth (Custom) | Grimes, Todd | SIL | 0.8219 | $81.61 | YES (fractional) |
| U7349604 | IBKR | Growth (Custom) | Mealman, Greg | BIL | 0.0676 | $6.18 | YES (fractional) |
| U7349604 | IBKR | Growth (Custom) | Mealman, Greg | SIL | 0.3373 | $33.49 | YES (fractional) |
| U7349608 | IBKR | Growth (Custom) | Plattner, Denise | BIL | 0.3266 | $29.87 | YES (fractional) |
| U7349608 | IBKR | Growth (Custom) | Plattner, Denise | SIL | 0.1437 | $14.27 | YES (fractional) |
| U7349616 | IBKR | Growth (Small, Custom) | Hackney, Joseph | BIL | 0.5776 | $52.82 | YES (fractional) |
| U7349616 | IBKR | Growth (Small, Custom) | Hackney, Joseph | SIL | 0.0733 | $7.28 | YES (fractional) |
| U7349621 | IBKR | Growth (Custom) | Phipps, Brian | BIL | 0.6029 | $55.14 | YES (fractional) |
| U7349621 | IBKR | Growth (Custom) | Phipps, Brian | SIL | 0.1822 | $18.09 | YES (fractional) |
| U7349624 | IBKR | Growth (Custom) | Grimes, Todd | BIL | 0.0778 | $7.11 | YES (fractional) |
| U7349624 | IBKR | Growth (Custom) | Grimes, Todd | SIL | 0.7534 | $74.81 | YES (fractional) |
| U7349632 | IBKR | Growth (Custom) | Grimes, Todd | BIL | 0.8368 | $76.53 | YES (fractional) |
| U7349632 | IBKR | Growth (Custom) | Grimes, Todd | SIL | 0.3916 | $38.88 | YES (fractional) |
| U7349638 | IBKR | Growth (Small, Custom) | Himes, Rebecca | BIL | 0.6451 | $58.99 | YES (fractional) |
| U7349638 | IBKR | Growth (Small, Custom) | Himes, Rebecca | SIL | 0.4809 | $47.75 | YES (fractional) |
| U7349643 | IBKR | Growth (Small, Custom) | Hackney, Joseph | BIL | 0.1232 | $11.27 | YES (fractional) |
| U7349643 | IBKR | Growth (Small, Custom) | Hackney, Joseph | SIL | 0.0307 | $3.05 | YES (fractional) |
| U7349646 | IBKR | Growth (Small, Custom) | Rinehart, Jason | BIL | 0.9373 | $85.72 | YES (fractional) |
| U7349646 | IBKR | Growth (Small, Custom) | Rinehart, Jason | SIL | 0.803 | $79.73 | YES (fractional) |
| U7349652 | IBKR | Growth (Custom) | Baker, Stacy | BIL | 0.6906 | $63.16 | YES (fractional) |
| U7349652 | IBKR | Growth (Custom) | Baker, Stacy | SIL | 0.4899 | $48.64 | YES (fractional) |
| U7349680 | IBKR | Growth (Custom) | Plattner, Denise | BIL | 0.8086 | $73.95 | YES (fractional) |
| U7349680 | IBKR | Growth (Custom) | Plattner, Denise | SIL | 0.9056 | $89.92 | YES (fractional) |
| U7349684 | IBKR | Growth (Small, Custom) | Brown, Andrea | BIL | 0.4186 | $38.28 | YES (fractional) |
| U7349684 | IBKR | Growth (Small, Custom) | Brown, Andrea | SIL | 0.2023 | $20.09 | YES (fractional) |
| U7349701 | IBKR | Growth (Custom) | Hackney, Joseph | BIL | 0.0501 | $4.58 | YES (fractional) |
| U7349701 | IBKR | Growth (Custom) | Hackney, Joseph | SIL | 0.8791 | $87.29 | YES (fractional) |
| U7349709 | IBKR | Growth (Small, Custom) | Pearcy, Austin | BIL | 0.936 | $85.60 | YES (fractional) |
| U7349709 | IBKR | Growth (Small, Custom) | Pearcy, Austin | SIL | 0.2523 | $25.05 | YES (fractional) |
| U7349710 | IBKR | Starter (Custom) | Rebant, Kasha | BIL | 0.5545 | $50.71 | YES (fractional) |
| U7349710 | IBKR | Starter (Custom) | Rebant, Kasha | SIL | 0.543 | $53.91 | YES (fractional) |
| U7349712 | IBKR | Growth (Small, Custom) | Smith, Jim and Janie | BIL | 0.2387 | $21.83 | YES (fractional) |
| U7349712 | IBKR | Growth (Small, Custom) | Smith, Jim and Janie | SIL | 0.0964 | $9.57 | YES (fractional) |
| U7349713 | IBKR | Growth (Custom) | Mady, Matthew | BIL | 0.3209 | $29.35 | YES (fractional) |
| U7349713 | IBKR | Growth (Custom) | Mady, Matthew | SIL | 0.9837 | $97.67 | YES (fractional) |
| U7349716 | IBKR | Growth (Custom) | Tangeman, John | BIL | 0.3571 | $32.66 | YES (fractional) |
| U7349716 | IBKR | Growth (Custom) | Tangeman, John | SIL | 0.9438 | $93.71 | YES (fractional) |
| U7349717 | IBKR | Growth (Custom) | Hackney, Joseph | BIL | 0.2791 | $25.52 | YES (fractional) |
| U7349717 | IBKR | Growth (Custom) | Hackney, Joseph | SIL | 0.3181 | $31.58 | YES (fractional) |
| U7356033 | IBKR | Growth (Custom) | Decker, Charles | BIL | 0.1251 | $11.44 | YES (fractional) |
| U7356033 | IBKR | Growth (Custom) | Decker, Charles | SIL | 0.897 | $89.06 | YES (fractional) |
| U7544231 | IBKR | Growth (Custom) | Koch, Rick | BIL | 0.5951 | $54.42 | YES (fractional) |
| U7544231 | IBKR | Growth (Custom) | Koch, Rick | SIL | 0.823 | $81.72 | YES (fractional) |
| U7544237 | IBKR | Growth (Custom) | Koch, Rick | BIL | 0.8614 | $78.78 | YES (fractional) |
| U7544237 | IBKR | Growth (Custom) | Koch, Rick | SIL | 0.52 | $51.63 | YES (fractional) |
| U7577340 | IBKR | Growth (Custom) | Mealman, Greg | BIL | 0.0346 | $3.16 | YES (fractional) |
| U7577340 | IBKR | Growth (Custom) | Mealman, Greg | SIL | 0.9862 | $97.92 | YES (fractional) |
| U7577352 | IBKR | Growth (Custom) | Tangeman, John | BIL | 0.782 | $71.51 | YES (fractional) |
| U7577352 | IBKR | Growth (Custom) | Tangeman, John | SIL | 0.6882 | $68.33 | YES (fractional) |
| U7577361 | IBKR | Growth (Custom) | Plattner, Denise | BIL | 0.3698 | $33.82 | YES (fractional) |
| U7577361 | IBKR | Growth (Custom) | Plattner, Denise | SIL | 0.193 | $19.16 | YES (fractional) |
| U7577370 | IBKR | Growth (Small, Custom) | Grimes, Todd | BIL | 0.1769 | $16.18 | YES (fractional) |
| U7577370 | IBKR | Growth (Small, Custom) | Grimes, Todd | SIL | 0.9947 | $98.76 | YES (fractional) |
| U7577373 | IBKR | Growth (Small, Custom) | Smith, Jim and Janie | BIL | 0.9552 | $87.35 | YES (fractional) |
| U7577373 | IBKR | Growth (Small, Custom) | Smith, Jim and Janie | SIL | 0.3501 | $34.76 | YES (fractional) |
| U7577375 | IBKR | Growth (Custom) | Hackney, Joseph | BIL | 0.383 | $35.03 | YES (fractional) |
| U7577375 | IBKR | Growth (Custom) | Hackney, Joseph | SIL | 0.483 | $47.96 | YES (fractional) |
| U7577384 | IBKR | Growth (Small, Custom) | Baker, Stacy | BIL | 0.3941 | $36.04 | YES (fractional) |
| U7577384 | IBKR | Growth (Small, Custom) | Baker, Stacy | SIL | 0.5651 | $56.11 | YES (fractional) |
| U7577473 | IBKR | Growth (Small, Custom) | Smithmier, Brandi | BIL | 0.8709 | $79.64 | YES (fractional) |
| U7577473 | IBKR | Growth (Small, Custom) | Smithmier, Brandi | SIL | 0.0159 | $1.58 | YES (fractional) |
| U7586137 | IBKR | Balanced (Small, Custom) | Whittaker, James | BIL | 0.8499 | $77.72 | YES (fractional) |
| U7586137 | IBKR | Balanced (Small, Custom) | Whittaker, James | SIL | 0.3491 | $34.66 | YES (fractional) |
| U7586139 | IBKR | Growth (Custom) | Jackson, Abby and John | BIL | 0.6508 | $59.52 | YES (fractional) |
| U7586139 | IBKR | Growth (Custom) | Jackson, Abby and John | SIL | 0.8112 | $80.54 | YES (fractional) |
| U7691784 | IBKR | Growth (Custom) | Strahm, Jacob and Kelsi | BIL | 0.8853 | $80.96 | YES (fractional) |
| U7691784 | IBKR | Growth (Custom) | Strahm, Jacob and Kelsi | SIL | 0.4198 | $41.68 | YES (fractional) |
| U7692221 | IBKR | Growth (Custom) | Rebant, Kasha | BIL | 0.5271 | $48.20 | YES (fractional) |
| U7692221 | IBKR | Growth (Custom) | Rebant, Kasha | SIL | 0.6471 | $64.25 | YES (fractional) |
| U7704058 | IBKR | Growth (Custom) | Strahm, Jacob and Kelsi | BIL | 0.4324 | $39.54 | YES (fractional) |
| U7704058 | IBKR | Growth (Custom) | Strahm, Jacob and Kelsi | SIL | 0.7569 | $75.15 | YES (fractional) |
| U7704098 | IBKR | Growth (Custom) | Strahm, Jacob and Kelsi | BIL | 0.9147 | $83.65 | YES (fractional) |
| U7704098 | IBKR | Growth (Custom) | Strahm, Jacob and Kelsi | SIL | 0.582 | $57.79 | YES (fractional) |
| U7704128 | IBKR | Growth (Custom) | Strahm, Jacob and Kelsi | BIL | 0.6829 | $62.45 | YES (fractional) |
| U7704128 | IBKR | Growth (Custom) | Strahm, Jacob and Kelsi | SIL | 0.7811 | $77.56 | YES (fractional) |
| U7704390 | IBKR | Growth (Custom) | Rebant, Kasha | BIL | 0.3401 | $31.10 | YES (fractional) |
| U7704390 | IBKR | Growth (Custom) | Rebant, Kasha | SIL | 0.6749 | $67.01 | YES (fractional) |
| U7704424 | IBKR | Growth (Custom) | Rebant, Kasha | BIL | 0.2426 | $22.19 | YES (fractional) |
| U7704424 | IBKR | Growth (Custom) | Rebant, Kasha | SIL | 0.4794 | $47.60 | YES (fractional) |
| U7704442 | IBKR | Growth (Custom) | Rebant, Kasha | BIL | 0.1268 | $11.60 | YES (fractional) |
| U7704442 | IBKR | Growth (Custom) | Rebant, Kasha | SIL | 0.6532 | $64.86 | YES (fractional) |
| U7741704 | IBKR | Growth (Custom) | Strahm, Jacob and Kelsi | BIL | 0.7973 | $72.91 | YES (fractional) |
| U7741704 | IBKR | Growth (Custom) | Strahm, Jacob and Kelsi | SIL | 0.8887 | $88.24 | YES (fractional) |
| U7741756 | IBKR | Growth (Custom) | Rebant, Kasha | BIL | 0.6199 | $56.69 | YES (fractional) |
| U7741756 | IBKR | Growth (Custom) | Rebant, Kasha | SIL | 0.4112 | $40.83 | YES (fractional) |
| U7741804 | IBKR | Growth (Custom) | Strahm, Jacob and Kelsi | BIL | 0.9449 | $86.41 | YES (fractional) |
| U7741804 | IBKR | Growth (Custom) | Strahm, Jacob and Kelsi | SIL | 0.423 | $42.00 | YES (fractional) |
| U7741838 | IBKR | Growth (Custom) | Rebant, Kasha | BIL | 0.4409 | $40.32 | YES (fractional) |
| U7741838 | IBKR | Growth (Custom) | Rebant, Kasha | SIL | 0.5545 | $55.06 | YES (fractional) |
| U7995235 | IBKR | Growth (Custom) | Mealman, Greg | BIL | 0.9097 | $83.19 | YES (fractional) |
| U7995235 | IBKR | Growth (Custom) | Mealman, Greg | SIL | 0.0668 | $6.63 | YES (fractional) |
| U8147827 | IBKR | Growth (Custom) | Kuenzi, Albert | BIL | 0.3531 | $32.29 | YES (fractional) |
| U8147827 | IBKR | Growth (Custom) | Kuenzi, Albert | SIL | 0.9864 | $97.94 | YES (fractional) |
| U8147914 | IBKR | Growth (Custom) | Kuenzi, Albert | BIL | 0.1227 | $11.22 | YES (fractional) |
| U8147914 | IBKR | Growth (Custom) | Kuenzi, Albert | SIL | 0.7659 | $76.05 | YES (fractional) |
| U8258600 | IBKR | Growth (Custom) | Kerns, Susan | SIL | 0.3802 | $37.75 | YES (fractional) |
| U8544689 | IBKR | Growth (Small, Custom) | Walter, Jeffrey | BIL | 0.0581 | $5.31 | YES (fractional) |
| U8544689 | IBKR | Growth (Small, Custom) | Walter, Jeffrey | SIL | 0.2064 | $20.49 | YES (fractional) |
| U9104611 | IBKR | Growth (Custom) | Carter, Jeffrey | BIL | 0.497 | $45.45 | YES (fractional) |
| U9104611 | IBKR | Growth (Custom) | Carter, Jeffrey | SIL | 0.7444 | $73.91 | YES (fractional) |
| U9914271 | IBKR | Growth (Custom) | Strahm, Jacob and Kelsi | BIL | 0.7935 | $72.57 | YES (fractional) |
| U9914271 | IBKR | Growth (Custom) | Strahm, Jacob and Kelsi | SIL | 0.478 | $47.46 | YES (fractional) |

### Tier-boundary stranding (left behind after a retier) (193 tasks)

| Account | Custodian | Model | Household | Symbol | Quantity | Market Value | Sell by hand in TWS? |
|---|---|---|---|---|---:|---:|:---:|
| U10850704 | IBKR | Starter (Custom) | Koch, Caleb | BUCK | 0.4152 | $9.65 | YES (fractional) |
| U10850704 | IBKR | Starter (Custom) | Koch, Caleb | GDX | 0.7996 | $79.37 | YES (fractional) |
| U10850704 | IBKR | Starter (Custom) | Koch, Caleb | GDXJ | 0.6051 | $78.08 | YES (fractional) |
| U10850704 | IBKR | Starter (Custom) | Koch, Caleb | SILJ | 0.4153 | $13.16 | YES (fractional) |
| U10850704 | IBKR | Starter (Custom) | Koch, Caleb | XLE | 0.5805 | $37.19 | YES (fractional) |
| U10850704 | IBKR | Starter (Custom) | Koch, Caleb | XLI | 0.4229 | $74.12 | YES (fractional) |
| U10850704 | IBKR | Starter (Custom) | Koch, Caleb | XLP | 0.8794 | $74.38 | YES (fractional) |
| U10850704 | IBKR | Starter (Custom) | Koch, Caleb | XLU | 0.5964 | $25.69 | YES (fractional) |
| U10850704 | IBKR | Starter (Custom) | Koch, Caleb | XLV | 0.256 | $43.89 | YES (fractional) |
| U11030394 | IBKR | Growth (Small, Custom) | Strahm, Jacob and Kelsi | BUCK | 0.4351 | $10.12 | YES (fractional) |
| U11030394 | IBKR | Growth (Small, Custom) | Strahm, Jacob and Kelsi | GDXJ | 0.812 | $104.77 | YES (fractional) |
| U11030394 | IBKR | Growth (Small, Custom) | Strahm, Jacob and Kelsi | XLP | 0.1876 | $15.87 | YES (fractional) |
| U11293593 | IBKR | Growth (Small, Custom) | Strahm, Jacob and Kelsi | BUCK | 0.4122 | $9.58 | YES (fractional) |
| U11293593 | IBKR | Growth (Small, Custom) | Strahm, Jacob and Kelsi | XLP | 0.0148 | $1.25 | YES (fractional) |
| U11406664 | IBKR | Growth (Small, Custom) | Monaghan, Courtney | BUCK | 0.5274 | $12.26 | YES (fractional) |
| U11406664 | IBKR | Growth (Small, Custom) | Monaghan, Courtney | GDXJ | 0.7326 | $94.53 | YES (fractional) |
| U11406664 | IBKR | Growth (Small, Custom) | Monaghan, Courtney | XLP | 0.0274 | $2.32 | YES (fractional) |
| U11406689 | IBKR | Growth (Small, Custom) | Monaghan, Courtney | BUCK | 0.3827 | $8.90 | YES (fractional) |
| U11406689 | IBKR | Growth (Small, Custom) | Monaghan, Courtney | GDXJ | 0.8889 | $114.69 | YES (fractional) |
| U11406689 | IBKR | Growth (Small, Custom) | Monaghan, Courtney | XLP | 0.3164 | $26.76 | YES (fractional) |
| U11604818 | IBKR | Growth (Small, Custom) | Monaghan, Courtney | GDXJ | 0.8968 | $115.71 | YES (fractional) |
| U11604818 | IBKR | Growth (Small, Custom) | Monaghan, Courtney | XLP | 0.3282 | $27.76 | YES (fractional) |
| U11725953 | IBKR | Starter (Custom) | Tangeman, John | BUCK | 0.0518 | $1.20 | YES (fractional) |
| U11725953 | IBKR | Starter (Custom) | Tangeman, John | GDX | 0.3093 | $30.70 | YES (fractional) |
| U11725953 | IBKR | Starter (Custom) | Tangeman, John | GDXJ | 0.2341 | $30.21 | YES (fractional) |
| U11725953 | IBKR | Starter (Custom) | Tangeman, John | SILJ | 0.9337 | $29.58 | YES (fractional) |
| U11725953 | IBKR | Starter (Custom) | Tangeman, John | XLE | 0.998 | $63.93 | YES (fractional) |
| U11725953 | IBKR | Starter (Custom) | Tangeman, John | XLI | 0.1636 | $28.67 | YES (fractional) |
| U11725953 | IBKR | Starter (Custom) | Tangeman, John | XLP | 0.3402 | $28.77 | YES (fractional) |
| U11725953 | IBKR | Starter (Custom) | Tangeman, John | XLU | 0.6173 | $26.59 | YES (fractional) |
| U11725953 | IBKR | Starter (Custom) | Tangeman, John | XLV | 0.4858 | $83.29 | YES (fractional) |
| U12140145 | IBKR | Growth (Small, Custom) | Rinehart, Alex | BUCK | 0.9239 | $21.48 | YES (fractional) |
| U12140145 | IBKR | Growth (Small, Custom) | Rinehart, Alex | GDXJ | 0.6485 | $83.68 | YES (fractional) |
| U12140145 | IBKR | Growth (Small, Custom) | Rinehart, Alex | XLP | 0.9026 | $76.34 | YES (fractional) |
| U12223690 | IBKR | Starter (Custom) | Rebant, Kasha | BUCK | 0.3032 | $7.05 | YES (fractional) |
| U12223690 | IBKR | Starter (Custom) | Rebant, Kasha | GDX | 0.372 | $36.92 | YES (fractional) |
| U12223690 | IBKR | Starter (Custom) | Rebant, Kasha | GDXJ | 0.2813 | $36.30 | YES (fractional) |
| U12223690 | IBKR | Starter (Custom) | Rebant, Kasha | SILJ | 0.1225 | $3.88 | YES (fractional) |
| U12223690 | IBKR | Starter (Custom) | Rebant, Kasha | XLE | 0.2002 | $12.82 | YES (fractional) |
| U12223690 | IBKR | Starter (Custom) | Rebant, Kasha | XLI | 0.1966 | $34.46 | YES (fractional) |
| U12223690 | IBKR | Starter (Custom) | Rebant, Kasha | XLP | 0.409 | $34.59 | YES (fractional) |
| U12223690 | IBKR | Starter (Custom) | Rebant, Kasha | XLU | 0.7423 | $31.98 | YES (fractional) |
| U12223690 | IBKR | Starter (Custom) | Rebant, Kasha | XLV | 0.5841 | $100.14 | YES (fractional) |
| U13105246 | IBKR | Growth (Small, Custom) | Vigneron, James | BUCK | 0.6053 | $14.07 | YES (fractional) |
| U13105246 | IBKR | Growth (Small, Custom) | Vigneron, James | GDXJ | 0.9747 | $125.77 | YES (fractional) |
| U13105246 | IBKR | Growth (Small, Custom) | Vigneron, James | XLP | 0.8885 | $75.15 | YES (fractional) |
| U13236734 | IBKR | Starter (Custom) | Strahm, Jacob and Kelsi | BUCK | 0.1458 | $3.39 | YES (fractional) |
| U13236734 | IBKR | Starter (Custom) | Strahm, Jacob and Kelsi | GDX | 0.3689 | $36.62 | YES (fractional) |
| U13236734 | IBKR | Starter (Custom) | Strahm, Jacob and Kelsi | XLE | 0.7436 | $47.64 | YES (fractional) |
| U13236734 | IBKR | Starter (Custom) | Strahm, Jacob and Kelsi | XLI | 0.2102 | $36.84 | YES (fractional) |
| U13236734 | IBKR | Starter (Custom) | Strahm, Jacob and Kelsi | XLP | 0.4904 | $41.48 | YES (fractional) |
| U13236734 | IBKR | Starter (Custom) | Strahm, Jacob and Kelsi | XLU | 0.5698 | $24.55 | YES (fractional) |
| U13236734 | IBKR | Starter (Custom) | Strahm, Jacob and Kelsi | XLV | 0.4676 | $80.17 | YES (fractional) |
| U13251044 | IBKR | Growth (Small, Custom) | Strahm, Jacob and Kelsi | BUCK | 0.3566 | $8.29 | YES (fractional) |
| U13251044 | IBKR | Growth (Small, Custom) | Strahm, Jacob and Kelsi | XLP | 0.9827 | $83.12 | YES (fractional) |
| U13917741 | IBKR | Growth (Small, Custom) | Mealman, Greg | BUCK | 0.1164 | $2.71 | YES (fractional) |
| U13917741 | IBKR | Growth (Small, Custom) | Mealman, Greg | GDXJ | 0.1512 | $19.51 | YES (fractional) |
| U13917741 | IBKR | Growth (Small, Custom) | Mealman, Greg | XLP | 0.6851 | $57.95 | YES (fractional) |
| U14131321 | IBKR | Growth (Small, Custom) | Seelig, Ariel | BUCK | 0.9691 | $22.53 | YES (fractional) |
| U14131321 | IBKR | Growth (Small, Custom) | Seelig, Ariel | GDXJ | 0.7629 | $98.44 | YES (fractional) |
| U14131321 | IBKR | Growth (Small, Custom) | Seelig, Ariel | XLP | 0.1149 | $9.72 | YES (fractional) |
| U14212395 | IBKR | Growth (Small, Custom) | Seelig, Ariel | BUCK | 0.9864 | $22.93 | YES (fractional) |
| U14237837 | IBKR | Growth (Small, Custom) | Seelig, Ariel | BUCK | 0.9356 | $21.75 | YES (fractional) |
| U14237837 | IBKR | Growth (Small, Custom) | Seelig, Ariel | XLP | 0.1263 | $10.68 | YES (fractional) |
| U14244440 | IBKR | Growth (Small, Custom) | Morris, Avilynn | BUCK | 0.5551 | $12.91 | YES (fractional) |
| U14244440 | IBKR | Growth (Small, Custom) | Morris, Avilynn | GDXJ | 0.7916 | $102.14 | YES (fractional) |
| U14244440 | IBKR | Growth (Small, Custom) | Morris, Avilynn | XLP | 0.1565 | $13.24 | YES (fractional) |
| U14390223 | IBKR | Growth (Small, Custom) | Koch, Caleb | BUCK | 0.1317 | $3.06 | YES (fractional) |
| U14390223 | IBKR | Growth (Small, Custom) | Koch, Caleb | GDXJ | 0.7026 | $90.66 | YES (fractional) |
| U15079695 | IBKR | Starter (Custom) | O'Brian, James | BUCK | 0.2066 | $4.80 | YES (fractional) |
| U15079695 | IBKR | Starter (Custom) | O'Brian, James | GDX | 0.6054 | $60.09 | YES (fractional) |
| U15079695 | IBKR | Starter (Custom) | O'Brian, James | GDXJ | 0.4582 | $59.12 | YES (fractional) |
| U15079695 | IBKR | Starter (Custom) | O'Brian, James | SILJ | 0.8284 | $26.24 | YES (fractional) |
| U15079695 | IBKR | Starter (Custom) | O'Brian, James | XLE | 0.9541 | $61.12 | YES (fractional) |
| U15079695 | IBKR | Starter (Custom) | O'Brian, James | XLI | 0.3201 | $56.10 | YES (fractional) |
| U15079695 | IBKR | Starter (Custom) | O'Brian, James | XLP | 0.6657 | $56.30 | YES (fractional) |
| U15079695 | IBKR | Starter (Custom) | O'Brian, James | XLU | 0.2086 | $8.99 | YES (fractional) |
| U15079695 | IBKR | Starter (Custom) | O'Brian, James | XLV | 0.9511 | $163.07 | YES (fractional) |
| U15087847 | IBKR | Growth (Small, Custom) | O'Brian, James | BUCK | 0.908 | $21.11 | YES (fractional) |
| U15087847 | IBKR | Growth (Small, Custom) | O'Brian, James | GDXJ | 0.259 | $33.42 | YES (fractional) |
| U15087847 | IBKR | Growth (Small, Custom) | O'Brian, James | XLP | 0.8431 | $71.31 | YES (fractional) |
| U15482451 | IBKR | Growth (Small, Custom) | Hill, Donald | BUCK | 0.5265 | $12.24 | YES (fractional) |
| U15482451 | IBKR | Growth (Small, Custom) | Hill, Donald | GDXJ | 0.7485 | $96.58 | YES (fractional) |
| U15482451 | IBKR | Growth (Small, Custom) | Hill, Donald | XLP | 0.0581 | $4.91 | YES (fractional) |
| U15631507 | IBKR | Growth (Small, Custom) | VanCamp, Robert | BUCK | 0.4619 | $10.74 | YES (fractional) |
| U15631507 | IBKR | Growth (Small, Custom) | VanCamp, Robert | GDXJ | 0.5146 | $66.40 | YES (fractional) |
| U15631507 | IBKR | Growth (Small, Custom) | VanCamp, Robert | XLP | 0.1951 | $16.50 | YES (fractional) |
| U17925010 | IBKR | Growth (Small, Custom) | Kelly, Julia | BUCK | 0.3117 | $7.25 | YES (fractional) |
| U17925010 | IBKR | Growth (Small, Custom) | Kelly, Julia | GDXJ | 0.1701 | $21.95 | YES (fractional) |
| U17925010 | IBKR | Growth (Small, Custom) | Kelly, Julia | XLP | 0.6877 | $58.17 | YES (fractional) |
| U18478058 | IBKR | Growth (Small, Custom) | Helton, Billy | BUCK | 0.5979 | $13.90 | YES (fractional) |
| U18478058 | IBKR | Growth (Small, Custom) | Helton, Billy | GDXJ | 0.1739 | $22.44 | YES (fractional) |
| U18478058 | IBKR | Growth (Small, Custom) | Helton, Billy | XLP | 0.6936 | $58.66 | YES (fractional) |
| U19756487 | IBKR | Growth (Small, Custom) | Loveland, James | BUCK | 0.4194 | $9.75 | YES (fractional) |
| U19756487 | IBKR | Growth (Small, Custom) | Loveland, James | GDXJ | 0.0592 | $7.64 | YES (fractional) |
| U19756487 | IBKR | Growth (Small, Custom) | Loveland, James | XLP | 0.0319 | $2.70 | YES (fractional) |
| U21139799 | IBKR | Growth (Small, Custom) | Boyles, Terrie | BUCK | 0.6272 | $14.58 | YES (fractional) |
| U21139799 | IBKR | Growth (Small, Custom) | Boyles, Terrie | GDXJ | 0.9025 | $116.45 | YES (fractional) |
| U21139799 | IBKR | Growth (Small, Custom) | Boyles, Terrie | XLP | 0.3207 | $27.12 | YES (fractional) |
| U21200665 | IBKR | Growth (Small, Custom) | Mealman, Greg | BUCK | 0.979 | $22.76 | YES (fractional) |
| U21200665 | IBKR | Growth (Small, Custom) | Mealman, Greg | XLP | 0.6484 | $54.84 | YES (fractional) |
| U22854243 | IBKR | Growth (Small, Custom) | Rider, Steven | BUCK | 0.089 | $2.07 | YES (fractional) |
| U22854243 | IBKR | Growth (Small, Custom) | Rider, Steven | GDXJ | 0.2253 | $29.07 | YES (fractional) |
| U22854243 | IBKR | Growth (Small, Custom) | Rider, Steven | XLP | 0.8156 | $68.98 | YES (fractional) |
| U24316643 | IBKR | Starter (Custom) | Grimes, Todd | BUCK | 0.3883 | $9.03 | YES (fractional) |
| U24316643 | IBKR | Starter (Custom) | Grimes, Todd | GDX | 0.3323 | $32.98 | YES (fractional) |
| U24316643 | IBKR | Starter (Custom) | Grimes, Todd | GDXJ | 0.2451 | $31.63 | YES (fractional) |
| U24316643 | IBKR | Starter (Custom) | Grimes, Todd | SILJ | 0.9124 | $28.90 | YES (fractional) |
| U24316643 | IBKR | Starter (Custom) | Grimes, Todd | XLE | 0.9882 | $63.30 | YES (fractional) |
| U24316643 | IBKR | Starter (Custom) | Grimes, Todd | XLI | 0.2566 | $44.97 | YES (fractional) |
| U24316643 | IBKR | Starter (Custom) | Grimes, Todd | XLP | 0.6436 | $54.44 | YES (fractional) |
| U24316643 | IBKR | Starter (Custom) | Grimes, Todd | XLU | 0.7398 | $31.87 | YES (fractional) |
| U24316643 | IBKR | Starter (Custom) | Grimes, Todd | XLV | 0.62 | $106.30 | YES (fractional) |
| U24331878 | IBKR | Starter (Custom) | Grimes, Todd | BUCK | 0.3064 | $7.12 | YES (fractional) |
| U24331878 | IBKR | Starter (Custom) | Grimes, Todd | GDX | 0.3192 | $31.68 | YES (fractional) |
| U24331878 | IBKR | Starter (Custom) | Grimes, Todd | GDXJ | 0.2354 | $30.37 | YES (fractional) |
| U24331878 | IBKR | Starter (Custom) | Grimes, Todd | SILJ | 0.8764 | $27.76 | YES (fractional) |
| U24331878 | IBKR | Starter (Custom) | Grimes, Todd | XLE | 0.952 | $60.99 | YES (fractional) |
| U24331878 | IBKR | Starter (Custom) | Grimes, Todd | XLI | 0.2465 | $43.20 | YES (fractional) |
| U24331878 | IBKR | Starter (Custom) | Grimes, Todd | XLP | 0.6183 | $52.30 | YES (fractional) |
| U24331878 | IBKR | Starter (Custom) | Grimes, Todd | XLU | 0.7106 | $30.61 | YES (fractional) |
| U24331878 | IBKR | Starter (Custom) | Grimes, Todd | XLV | 0.5955 | $102.10 | YES (fractional) |
| U7333179 | IBKR | Starter (Custom) | Brown, Calahan | BUCK | 0.9031 | $21.00 | YES (fractional) |
| U7333179 | IBKR | Starter (Custom) | Brown, Calahan | GDX | 0.2484 | $24.66 | YES (fractional) |
| U7333179 | IBKR | Starter (Custom) | Brown, Calahan | GDXJ | 0.188 | $24.26 | YES (fractional) |
| U7333179 | IBKR | Starter (Custom) | Brown, Calahan | SILJ | 0.7501 | $23.76 | YES (fractional) |
| U7333179 | IBKR | Starter (Custom) | Brown, Calahan | XLE | 0.8002 | $51.26 | YES (fractional) |
| U7333179 | IBKR | Starter (Custom) | Brown, Calahan | XLI | 0.1314 | $23.03 | YES (fractional) |
| U7333179 | IBKR | Starter (Custom) | Brown, Calahan | XLP | 0.273 | $23.09 | YES (fractional) |
| U7333179 | IBKR | Starter (Custom) | Brown, Calahan | XLU | 0.4949 | $21.32 | YES (fractional) |
| U7333179 | IBKR | Starter (Custom) | Brown, Calahan | XLV | 0.3901 | $66.88 | YES (fractional) |
| U7333194 | IBKR | Growth (Small, Custom) | Sewell, Ryan | BUCK | 0.5992 | $13.93 | YES (fractional) |
| U7333194 | IBKR | Growth (Small, Custom) | Sewell, Ryan | XLP | 0.2954 | $24.98 | YES (fractional) |
| U7333217 | IBKR | Starter (Custom) | Husted, Danielle | GDX | 0.9985 | $99.11 | YES (fractional) |
| U7333217 | IBKR | Starter (Custom) | Husted, Danielle | GDXJ | 0.7542 | $97.31 | YES (fractional) |
| U7333217 | IBKR | Starter (Custom) | Husted, Danielle | XLE | 0.2246 | $14.39 | YES (fractional) |
| U7333217 | IBKR | Starter (Custom) | Husted, Danielle | XLI | 0.5288 | $92.68 | YES (fractional) |
| U7333217 | IBKR | Starter (Custom) | Husted, Danielle | XLP | 0.0995 | $8.42 | YES (fractional) |
| U7333217 | IBKR | Starter (Custom) | Husted, Danielle | XLU | 0.9946 | $42.85 | YES (fractional) |
| U7333217 | IBKR | Starter (Custom) | Husted, Danielle | XLV | 0.5705 | $97.81 | YES (fractional) |
| U7333242 | IBKR | Growth (Small, Custom) | Brown, Andrea | GDXJ | 0.5358 | $69.13 | YES (fractional) |
| U7333242 | IBKR | Growth (Small, Custom) | Brown, Andrea | XLP | 0.2492 | $21.08 | YES (fractional) |
| U7333246 | IBKR | Growth (Small, Custom) | Jackson, Abby and John | BUCK | 68.8616 | $1,601.03 | YES (fractional) |
| U7333254 | IBKR | Growth (Small, Custom) | Heskett, Andrew | BUCK | 0.7116 | $16.54 | YES (fractional) |
| U7333254 | IBKR | Growth (Small, Custom) | Heskett, Andrew | GDXJ | 0.1284 | $16.57 | YES (fractional) |
| U7333254 | IBKR | Growth (Small, Custom) | Heskett, Andrew | XLP | 0.1367 | $11.56 | YES (fractional) |
| U7349616 | IBKR | Growth (Small, Custom) | Hackney, Joseph | BUCK | 0.1738 | $4.04 | YES (fractional) |
| U7349616 | IBKR | Growth (Small, Custom) | Hackney, Joseph | GDXJ | 0.3678 | $47.46 | YES (fractional) |
| U7349616 | IBKR | Growth (Small, Custom) | Hackney, Joseph | XLP | 0.4675 | $39.54 | YES (fractional) |
| U7349638 | IBKR | Growth (Small, Custom) | Himes, Rebecca | BUCK | 0.1269 | $2.95 | YES (fractional) |
| U7349638 | IBKR | Growth (Small, Custom) | Himes, Rebecca | GDXJ | 0.9042 | $116.67 | YES (fractional) |
| U7349638 | IBKR | Growth (Small, Custom) | Himes, Rebecca | XLP | 0.8205 | $69.40 | YES (fractional) |
| U7349643 | IBKR | Growth (Small, Custom) | Hackney, Joseph | BUCK | 0.0912 | $2.12 | YES (fractional) |
| U7349643 | IBKR | Growth (Small, Custom) | Hackney, Joseph | GDXJ | 0.3346 | $43.17 | YES (fractional) |
| U7349643 | IBKR | Growth (Small, Custom) | Hackney, Joseph | XLP | 0.4192 | $35.46 | YES (fractional) |
| U7349646 | IBKR | Growth (Small, Custom) | Rinehart, Jason | BUCK | 0.3007 | $6.99 | YES (fractional) |
| U7349646 | IBKR | Growth (Small, Custom) | Rinehart, Jason | GDXJ | 0.1554 | $20.05 | YES (fractional) |
| U7349646 | IBKR | Growth (Small, Custom) | Rinehart, Jason | XLP | 0.1784 | $15.09 | YES (fractional) |
| U7349684 | IBKR | Growth (Small, Custom) | Brown, Andrea | BUCK | 0.5367 | $12.48 | YES (fractional) |
| U7349684 | IBKR | Growth (Small, Custom) | Brown, Andrea | GDXJ | 0.2323 | $29.97 | YES (fractional) |
| U7349684 | IBKR | Growth (Small, Custom) | Brown, Andrea | XLP | 0.7731 | $65.39 | YES (fractional) |
| U7349709 | IBKR | Growth (Small, Custom) | Pearcy, Austin | BUCK | 0.0467 | $1.09 | YES (fractional) |
| U7349709 | IBKR | Growth (Small, Custom) | Pearcy, Austin | GDXJ | 0.2703 | $34.88 | YES (fractional) |
| U7349709 | IBKR | Growth (Small, Custom) | Pearcy, Austin | XLP | 0.831 | $70.29 | YES (fractional) |
| U7349710 | IBKR | Starter (Custom) | Rebant, Kasha | BUCK | 0.6856 | $15.94 | YES (fractional) |
| U7349710 | IBKR | Starter (Custom) | Rebant, Kasha | GDX | 0.5538 | $54.97 | YES (fractional) |
| U7349710 | IBKR | Starter (Custom) | Rebant, Kasha | GDXJ | 0.419 | $54.06 | YES (fractional) |
| U7349710 | IBKR | Starter (Custom) | Rebant, Kasha | SILJ | 0.6721 | $21.29 | YES (fractional) |
| U7349710 | IBKR | Starter (Custom) | Rebant, Kasha | XLE | 0.7871 | $50.42 | YES (fractional) |
| U7349710 | IBKR | Starter (Custom) | Rebant, Kasha | XLI | 0.2927 | $51.30 | YES (fractional) |
| U7349710 | IBKR | Starter (Custom) | Rebant, Kasha | XLP | 0.6088 | $51.49 | YES (fractional) |
| U7349710 | IBKR | Starter (Custom) | Rebant, Kasha | XLU | 0.1054 | $4.54 | YES (fractional) |
| U7349710 | IBKR | Starter (Custom) | Rebant, Kasha | XLV | 0.8699 | $149.14 | YES (fractional) |
| U7349712 | IBKR | Growth (Small, Custom) | Smith, Jim and Janie | BUCK | 0.2521 | $5.86 | YES (fractional) |
| U7349712 | IBKR | Growth (Small, Custom) | Smith, Jim and Janie | GDXJ | 0.8431 | $108.79 | YES (fractional) |
| U7349712 | IBKR | Growth (Small, Custom) | Smith, Jim and Janie | XLP | 0.233 | $19.71 | YES (fractional) |
| U7577370 | IBKR | Growth (Small, Custom) | Grimes, Todd | BUCK | 0.0471 | $1.10 | YES (fractional) |
| U7577370 | IBKR | Growth (Small, Custom) | Grimes, Todd | GDXJ | 0.768 | $99.10 | YES (fractional) |
| U7577370 | IBKR | Growth (Small, Custom) | Grimes, Todd | XLP | 0.1159 | $9.80 | YES (fractional) |
| U7577373 | IBKR | Growth (Small, Custom) | Smith, Jim and Janie | BUCK | 0.754 | $17.53 | YES (fractional) |
| U7577373 | IBKR | Growth (Small, Custom) | Smith, Jim and Janie | GDXJ | 0.0363 | $4.68 | YES (fractional) |
| U7577373 | IBKR | Growth (Small, Custom) | Smith, Jim and Janie | XLP | 0.5348 | $45.23 | YES (fractional) |
| U7577384 | IBKR | Growth (Small, Custom) | Baker, Stacy | GDXJ | 0.9811 | $126.59 | YES (fractional) |
| U7577384 | IBKR | Growth (Small, Custom) | Baker, Stacy | XLP | 0.895 | $75.70 | YES (fractional) |
| U7577473 | IBKR | Growth (Small, Custom) | Smithmier, Brandi | BUCK | 0.6514 | $15.15 | YES (fractional) |
| U7577473 | IBKR | Growth (Small, Custom) | Smithmier, Brandi | GDXJ | 0.5485 | $70.77 | YES (fractional) |
| U7577473 | IBKR | Growth (Small, Custom) | Smithmier, Brandi | XLP | 0.293 | $24.78 | YES (fractional) |
| U7586137 | IBKR | Balanced (Small, Custom) | Whittaker, James | BUCK | 0.9362 | $21.77 | YES (fractional) |
| U7586137 | IBKR | Balanced (Small, Custom) | Whittaker, James | GDXJ | 0.0396 | $5.11 | YES (fractional) |
| U7586137 | IBKR | Balanced (Small, Custom) | Whittaker, James | XLP | 0.5209 | $44.06 | YES (fractional) |
| U8544689 | IBKR | Growth (Small, Custom) | Walter, Jeffrey | BUCK | 0.2548 | $5.92 | YES (fractional) |
| U8544689 | IBKR | Growth (Small, Custom) | Walter, Jeffrey | GDXJ | 0.4645 | $59.93 | YES (fractional) |
| U8544689 | IBKR | Growth (Small, Custom) | Walter, Jeffrey | XLP | 0.6335 | $53.58 | YES (fractional) |

### Pre-existing individual bond / legacy holding (11 tasks)

| Account | Custodian | Model | Household | Symbol | Quantity | Market Value | Sell by hand in TWS? |
|---|---|---|---|---|---:|---:|:---:|
| U7333246 | IBKR | Growth (Small, Custom) | Jackson, Abby and John | 736560ES8 5 3/4 09/01/30 | 5000 | $5,007.00 | no (whole share) |
| U7349657 | IBKR | Growth (Custom) | Walter, Jeffrey | 235308RA3 6.45 02/15/35 | 15000 | $15,178.93 | no (whole share) |
| U7349657 | IBKR | Growth (Custom) | Walter, Jeffrey | 443730CU8 5 5/8 01/15/35 | 15000 | $13,761.02 | no (whole share) |
| U7349657 | IBKR | Growth (Custom) | Walter, Jeffrey | 736560ES8 5 3/4 09/01/30 | 10000 | $10,014.00 | no (whole share) |
| U7349974 | IBKR | Growth (Custom) | Stallbaumer, Gerald | 235308RA3 6.45 02/15/35 | 20000 | $20,238.58 | no (whole share) |
| U7349974 | IBKR | Growth (Custom) | Stallbaumer, Gerald | 443730CU8 5 5/8 01/15/35 | 5000 | $4,587.01 | no (whole share) |
| U7349974 | IBKR | Growth (Custom) | Stallbaumer, Gerald | 86909RAW1 9 7/8 01/01/34 | 15000 | $16,238.81 | no (whole share) |
| U7552750 | IBKR | Balanced (Custom) | Whittaker, James | 797843BE8 4.6 08/01/34 | 10000 | $10,013.51 | no (whole share) |
| U7552750 | IBKR | Balanced (Custom) | Whittaker, James | 806721GU4 5 12/01/31 | 15000 | $15,019.52 | no (whole share) |
| U7552751 | IBKR | Balanced (Custom) | Whittaker, James | 797843BE8 4.6 08/01/34 | 10000 | $10,013.51 | no (whole share) |
| U7552751 | IBKR | Balanced (Custom) | Whittaker, James | 806721GU4 5 12/01/31 | 15000 | $15,019.52 | no (whole share) |
