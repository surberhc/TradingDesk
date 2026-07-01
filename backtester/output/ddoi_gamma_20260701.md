# DDOI inferred-dealer-direction gamma -- honest comparison

Generated: 2026-07-01  (PAPER / research; SPXW 1-min tape)

Days computed: 1125  (2022-01-03 -> 2026-06-29)
Mean fraction of OI-contracts with a tape-inferred sign: 44.7% (min 31.3%, max 61.8%)

## 1. DDOI vs STATIC baseline (does the method even move the label?)

Whole sample: DDOI == static on 41.6% of 1125 days (they DIFFER on 58.4%).

Where they differ (static -> DDOI counts):
  static=Negative -> ddoi=Neutral  : 34
  static=Negative -> ddoi=Positive : 117
  static=Neutral  -> ddoi=Negative : 85
  static=Neutral  -> ddoi=Positive : 4
  static=Positive -> ddoi=Negative : 367
  static=Positive -> ddoi=Neutral  : 50

Negative-day count: static=443, DDOI=744 (DDOI +301 vs static).

## 2. vs Tier-1-Alpha VENDOR labels (the residual-gap test)

Vendor overlap: 281 days (2025-05-01 -> 2026-06-18).
NOTE: vendor labels the SPX-ROOT market regime; our tape is SPXW. This is the
same cross-symbol caveat the production calibration lives with -- read directional,
not as an exact target.

### Whole vendor overlap  (n=281)
  gamma_state accuracy vs vendor:  static=60.1%   DDOI=36.7%   (delta -23.5%)
  NEGATIVE-side recall (vendor=Neg, we=Neg): static=57.1%  DDOI=76.2%  (delta +19.0%, n_vendorNeg=63)

Time-halves split at 2025-11-24 (out-of-sample check -- an edge must show in BOTH):

### First half  (n=140)
  gamma_state accuracy vs vendor:  static=57.1%   DDOI=32.1%   (delta -25.0%)
  NEGATIVE-side recall (vendor=Neg, we=Neg): static=46.7%  DDOI=80.0%  (delta +33.3%, n_vendorNeg=15)

### Second half  (n=141)
  gamma_state accuracy vs vendor:  static=63.1%   DDOI=41.1%   (delta -22.0%)
  NEGATIVE-side recall (vendor=Neg, we=Neg): static=60.4%  DDOI=75.0%  (delta +14.6%, n_vendorNeg=48)

### Confusion matrices (whole overlap)

STATIC (rows=ours, cols=vendor):
                ven_Negative  ven_Neutral  ven_Positive
  our_Negative            36           24            26
  our_Neutral              9            9            13
  our_Positive            18           22           124

DDOI (rows=ours, cols=vendor):
                ven_Negative  ven_Neutral  ven_Positive
  our_Negative            48           33           107
  our_Neutral              5            8             9
  our_Positive            10           14            47

## 3. Verdict

DDOI accuracy vs vendor: 36.7%  vs  static 60.1%  (whole overlap delta -23.5%).
First half delta -25.0%; second half delta -22.0%.
=> DDOI does NOT beat static vs the vendor labels. The tape-inferred dealer
   direction (on SPXW) does not close the residual negative-gamma gap here.

Nothing wired into the frozen S0 config. This is a research comparison only.