#!/usr/bin/env python3
"""
Extract page-1 data from every MSR report:
  1. SPX Key Levels & Strikes (12-item box)  -> _msr_spx_key_levels.csv
  2. The 4 regime classification boxes        -> _msr_regimes.csv

Key levels are a clean label:value list (OCR + label match). Regime state is
read from the FILL COLOUR of the highlighted cell (green = bullish side,
orange = neutral, red = bearish side) — robust and OCR-free for the state.
"""
import csv
import importlib.util
import os
import re
import sys
import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("lib", os.path.join(ROOT, "_msr_ocr_lib.py"))
lib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lib)
import fitz

INV = os.path.join(ROOT, "_msr_canonical_daily.csv")
OUT_KL = os.path.join(ROOT, "_msr_spx_key_levels.csv")
OUT_RG = os.path.join(ROOT, "_msr_regimes.csv")

# canonical key-level labels -> output field; matched by keyword set
KL_LABELS = [
    ("last_price", ["last", "price"]),
    ("upper_pv_band", ["upper"]),
    ("lower_pv_band", ["lower"]),
    ("upside_risk_pct", ["upside"]),
    ("downside_risk_pct", ["downside"]),
    ("spread_pct", ["spread"]),
    ("gex_throttle", ["throttle"]),
    ("gex_flip", ["flip"]),
    ("implied_move_pct", ["implied"]),
    ("resistance_strike", ["resistance"]),
    ("focal_strike", ["focal"]),
    ("support_strike", ["support"]),
]

# regime dimension -> (label keyword, [green_state, orange_state, red_state])
REGIMES = [
    ("gamma_exposure", "gamma", ["Positive", "Neutral", "Negative"]),
    ("systematic_flow_risk", "flow", ["Bullish", "Neutral", "Bearish"]),
    ("pvband_risk_reward", "reward", ["Long", "Neutral", "Short"]),
    ("strategic_allocation", "strategic", ["Risk On", "Neutral", "Risk Off"]),
]

NUM = re.compile(r"-?\$?\d[\d,]*\.?\d*%?")


def render_array(pg, x0, y0, x1, y1, dpi=300):
    r = pg.rect
    clip = fitz.Rect(r.x0 + r.width * x0, r.y0 + r.height * y0,
                     r.x0 + r.width * x1, r.y0 + r.height * y1)
    pix = pg.get_pixmap(dpi=dpi, clip=clip)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
    if pix.n == 4:
        img = img[:, :, :3]
    return img.copy()  # RGB


def ocr_array(img):
    res, _ = lib.ocr_engine()(np.ascontiguousarray(img[:, :, ::-1]))  # BGR
    out = []
    for box, txt, conf in (res or []):
        xs = sum(p[0] for p in box) / 4
        ys = sum(p[1] for p in box) / 4
        out.append({"text": txt, "x": xs, "y": ys})
    return out


def to_num(s):
    s = s.replace("$", "").replace(",", "").replace("%", "").strip()
    m = re.search(r"-?\d[\d]*\.?\d*", s)
    return float(m.group()) if m else None


def classify_fill(band):
    R, G, B = band[..., 0].astype(int), band[..., 1].astype(int), band[..., 2].astype(int)
    mx = np.maximum(np.maximum(R, G), B)
    mn = np.minimum(np.minimum(R, G), B)
    sat = mx - mn
    fill = (sat > 55) & (mx > 110)
    green = fill & (G > R + 15) & (G > B + 15)
    red = fill & (R > G + 55) & (R > B + 55)
    orange = fill & (R > 150) & (G > 80) & (G < 205) & (B < 100) & ~red
    counts = {"green": int(green.sum()), "orange": int(orange.sum()), "red": int(red.sum())}
    best = max(counts, key=counts.get)
    return best if counts[best] > 60 else None


def extract_key_levels(pg):
    img = render_array(pg, 0.0, 0.20, 0.36, 0.60)
    rows = lib.cluster_rows(ocr_array(img), y_tol=14)
    vals = {}
    for row in rows:
        text = " ".join(b["text"] for b in sorted(row, key=lambda b: b["x"]))
        low = text.lower()
        for field, keys in KL_LABELS:
            if field in vals:
                continue
            if all(k in low for k in keys):
                # value = rightmost numeric token on the row
                nums = [b["text"] for b in sorted(row, key=lambda b: b["x"])
                        if NUM.fullmatch(b["text"].strip())]
                v = to_num(nums[-1]) if nums else to_num(text.split()[-1])
                vals[field] = v
                break
    return vals


def _fill_mask(img):
    R, G, B = img[..., 0].astype(int), img[..., 1].astype(int), img[..., 2].astype(int)
    mx = np.maximum(np.maximum(R, G), B)
    mn = np.minimum(np.minimum(R, G), B)
    return (mx - mn > 55) & (mx > 110)


def _band_color(band):
    # Distinguish by the GREEN channel: red has G<~95, orange has G~120-200,
    # green has G as the max channel. (Orange also satisfies R>G+55, so it must
    # be separated from red by G level, not by R-G dominance.)
    R, G, B = band[..., 0].astype(int), band[..., 1].astype(int), band[..., 2].astype(int)
    fill = _fill_mask(band)
    green = fill & (G > R + 10) & (G > B + 10)
    red = fill & (R > 140) & (G < 95) & (B < 95) & ~green
    orange = fill & (R > 180) & (G >= 105) & (G <= 215) & (B < 115) & ~green & ~red
    counts = {"green": int(green.sum()), "orange": int(orange.sum()), "red": int(red.sum())}
    best = max(counts, key=counts.get)
    return best if counts[best] > 60 else None


REGIME_KW = {"gamma_exposure": ["gamma"], "systematic_flow_risk": ["systematic", "flow"],
             "pvband_risk_reward": ["reward", "/reward"], "strategic_allocation": ["strategic", "allocation"]}


def extract_regimes(pg):
    # Anchor each regime row to its OCR'd label y (avoids the teal title divider),
    # then read the active state from the dominant fill colour in that row's
    # options area. Colour -> state directly (green/orange/red = option 0/1/2).
    img = render_array(pg, 0.0, 0.15, 1.0, 0.30)
    H, W = img.shape[:2]
    words = ocr_array(img)
    out = {f: None for f, _, _ in REGIMES}
    for field, _, states in REGIMES:
        kws = REGIME_KW[field]
        ys = [b["y"] for b in words if any(k in b["text"].lower() for k in kws)]
        if not ys:
            continue
        yc = int(sorted(ys)[len(ys) // 2])
        band = img[max(0, yc - 20):min(H, yc + 20), int(W * 0.40):]
        color = _band_color(band)
        out[field] = {"green": states[0], "orange": states[1], "red": states[2]}.get(color)
    return out


def main():
    rows_in = list(csv.DictReader(open(INV, encoding="utf-8-sig")))
    if len(sys.argv) > 1:
        rows_in = rows_in[:int(sys.argv[1])]
    kl_rows, rg_rows = [], []
    bad = []
    for n, r in enumerate(rows_in, 1):
        d = r["ReportDate"]
        try:
            doc = fitz.open(r["FullPath"])
            try:
                pg = doc[0]
                kl = extract_key_levels(pg)
                rg = extract_regimes(pg)
            finally:
                doc.close()
        except Exception as e:
            kl, rg = {}, {}
            bad.append((d, f"error:{type(e).__name__}"))

        # validate key levels via redundancy: upper≈last*(1+up/100) etc.
        flags = []
        for f, _ in KL_LABELS:
            if kl.get(f) is None:
                flags.append(f"missing:{f}")
        L, up, dn = kl.get("last_price"), kl.get("upside_risk_pct"), kl.get("downside_risk_pct")
        U, Lo = kl.get("upper_pv_band"), kl.get("lower_pv_band")
        if L and up is not None and U:
            if abs(U - L * (1 + up / 100)) > max(3, L * 0.004):
                flags.append("chk:upper")
        if L and dn is not None and Lo:
            if abs(Lo - L * (1 + dn / 100)) > max(3, L * 0.004):
                flags.append("chk:lower")
        kl_row = {"report_date": d}
        kl_row.update({f: kl.get(f) for f, _ in KL_LABELS})
        kl_row["flag"] = ";".join(flags)
        kl_rows.append(kl_row)

        rg_row = {"report_date": d}
        rg_row.update({f: rg.get(f) for f, _, _ in REGIMES})
        rg_row["flag"] = ";".join(f"missing:{f}" for f, _, _ in REGIMES if rg.get(f) is None)
        rg_rows.append(rg_row)
        print(f"[{n}/{len(rows_in)}] {d}: KL {sum(1 for f,_ in KL_LABELS if kl.get(f) is not None)}/12  "
              f"RG {sum(1 for f,_,_ in REGIMES if rg.get(f))}/4  {kl_row['flag']}", flush=True)

    with open(OUT_KL, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["report_date"] + [x for x, _ in KL_LABELS] + ["flag"])
        w.writeheader(); w.writerows(kl_rows)
    with open(OUT_RG, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["report_date"] + [x for x, _, _ in REGIMES] + ["flag"])
        w.writeheader(); w.writerows(rg_rows)

    klok = sum(1 for r in kl_rows if not r["flag"])
    rgok = sum(1 for r in rg_rows if not r["flag"])
    print(f"\nkey-levels clean: {klok}/{len(kl_rows)}   regimes clean: {rgok}/{len(rg_rows)}")
    print(f"errors: {len(bad)}")
    print(f"-> {OUT_KL}\n-> {OUT_RG}")


if __name__ == "__main__":
    main()
