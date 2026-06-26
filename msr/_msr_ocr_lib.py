"""
Shared OCR helpers for MSR table extraction.

Locating tables is done via the text-layer section header (stable across the
9-page "Situation" and 11-page "Structure" formats); the table data itself is
raster, so we crop the relevant page region, render at high DPI, and OCR.
"""
import os
import re
import numpy as np
import fitz
from rapidocr_onnxruntime import RapidOCR

_OCR = None


def ocr_engine():
    global _OCR
    if _OCR is None:
        _OCR = RapidOCR()
    return _OCR


def page_headers(doc):
    """Return list of (index, header) for each page's top text line."""
    out = []
    for i in range(doc.page_count):
        lines = [l.strip() for l in doc[i].get_text("text").splitlines() if l.strip()]
        out.append((i, lines[0] if lines else ""))
    return out


def find_page(doc, header_substr):
    """First page index whose top header contains header_substr (case-insens)."""
    for i, h in page_headers(doc):
        if header_substr.lower() in h.lower():
            return i
    return None


def find_sector_bands_page(doc):
    """The 'Probable Volatility Bands' page immediately before 'S&P 500 Market
    Breadth' (the one carrying the sector table)."""
    hdrs = page_headers(doc)
    breadth = None
    for i, h in hdrs:
        if "market breadth" in h.lower():
            breadth = i
            break
    if breadth is not None and breadth > 0:
        if "probable volatility" in hdrs[breadth - 1][1].lower():
            return breadth - 1
    # fallback: last PVB page
    pvb = [i for i, h in hdrs if "probable volatility" in h.lower()]
    return pvb[-1] if pvb else None


def ocr_crop(doc, page_idx, x0f, y0f, x1f, y1f, dpi=300):
    """OCR a fractional crop of a page. Returns list of dicts:
    {text, x, y, conf} with x,y as pixel centers in the crop."""
    pg = doc[page_idx]
    r = pg.rect
    clip = fitz.Rect(r.x0 + r.width * x0f, r.y0 + r.height * y0f,
                     r.x0 + r.width * x1f, r.y0 + r.height * y1f)
    pix = pg.get_pixmap(dpi=dpi, clip=clip)
    # Pass the image to OCR in-memory (avoids a shared temp file that Windows
    # can transiently lock, which previously surfaced as FzErrorSystem).
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
    if pix.n == 4:        # drop alpha
        img = img[:, :, :3]
    if pix.n == 1:        # gray -> 3ch
        img = np.repeat(img, 3, axis=2)
    img = img[:, :, ::-1]  # RGB -> BGR for the OCR engine
    res, _ = ocr_engine()(np.ascontiguousarray(img))
    out = []
    for box, txt, conf in (res or []):
        xs = sum(p[0] for p in box) / 4
        ys = sum(p[1] for p in box) / 4
        try:
            c = float(conf)
        except (TypeError, ValueError):
            c = 0.0
        out.append({"text": txt, "x": xs, "y": ys, "conf": c})
    return out, pix.width, pix.height


def cluster_rows(boxes, y_tol=18):
    """Group boxes into rows by y proximity; each row sorted by x."""
    rows = []
    for b in sorted(boxes, key=lambda b: b["y"]):
        placed = False
        for row in rows:
            if abs(row[0]["y"] - b["y"]) < y_tol:
                row.append(b)
                placed = True
                break
        if not placed:
            rows.append([b])
    for row in rows:
        row.sort(key=lambda b: b["x"])
    rows.sort(key=lambda r: sum(b["y"] for b in r) / len(r))
    return rows


_NUM = re.compile(r"-?\d[\d,]*\.?\d*")


def to_float(s):
    """Extract a float from an OCR token; tolerate $ , % and common junk.
    Returns None if no parseable number."""
    if s is None:
        return None
    s = s.replace("$", "").replace(",", "").replace("%", "").strip()
    # common OCR letter->digit confusions inside an otherwise-numeric token
    if re.search(r"[A-Za-z]", s) and re.search(r"\d", s):
        s2 = (s.replace("O", "0").replace("o", "0").replace("l", "1")
               .replace("I", "1").replace("S", "5").replace("B", "8")
               .replace("E", "").replace("$", ""))
        if re.fullmatch(r"-?\d*\.?\d+", s2):
            s = s2
    m = _NUM.search(s)
    return float(m.group()) if m else None
