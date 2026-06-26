#!/usr/bin/env python3
"""
Merge all extracted MSR datasets into a single SQLite database (msr.db).

Tables
  reports          one row per report (metadata + format + the 3 commentary blocks)
  sector_bands     sector vol-bands, one row per asset per report
  realized_vol     realized-vol dropoff table, T+1..T+10 per report
  spx_daily_returns consensus SPX daily return per calendar date
  sitrep_recaps    reference list of the 28 weekly SITREP PDFs

Views
  v_sector_trusted    sector rows with no anomaly/missing flag
  v_realized_trusted  realized-vol rows with no missing/order flag
  v_data_quality      per-table row + trusted counts

Re-runnable: drops and rebuilds every table from the CSVs.
"""
import csv
import os
import sqlite3

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "msr.db")


def rd(name, enc="utf-8"):
    return list(csv.DictReader(open(os.path.join(ROOT, name), encoding=enc)))


def num(x):
    if x is None or x == "":
        return None
    try:
        return float(x)
    except ValueError:
        return None


def main():
    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    c = con.cursor()

    # ---- reports (canonical daily + commentary + regime labels, 1:1) ----
    canon = {r["ReportDate"]: r for r in rd("_msr_canonical_daily.csv", "utf-8-sig")}
    comm = {r["report_date"]: r for r in rd("_msr_commentary.csv")}
    reg = {r["report_date"]: r for r in rd("_msr_regimes.csv")} if os.path.exists(
        os.path.join(ROOT, "_msr_regimes.csv")) else {}
    c.execute("""CREATE TABLE reports(
        report_date TEXT PRIMARY KEY,
        format TEXT, page_count INTEGER, size_mb REAL,
        file_name TEXT, rel_path TEXT, sha256 TEXT,
        gamma_exposure TEXT, systematic_rebalancing TEXT,
        strategic_allocation TEXT, commentary_status TEXT,
        regime_gamma TEXT, regime_flow_risk TEXT,
        regime_pvband_rr TEXT, regime_strategic TEXT)""")
    for d in sorted(canon):
        r = canon[d]
        cm = comm.get(d, {})
        rg = reg.get(d, {})
        c.execute("INSERT INTO reports VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            d, cm.get("format"), int(float(r["PageCount"])) if r["PageCount"] else None,
            num(r["SizeMB"]), r["FileName"], r["RelPath"], r["SHA256"],
            cm.get("gamma_exposure"), cm.get("systematic_rebalancing"),
            cm.get("strategic_allocation"), cm.get("parse_status"),
            rg.get("gamma_exposure"), rg.get("systematic_flow_risk"),
            rg.get("pvband_risk_reward"), rg.get("strategic_allocation")))

    # ---- spx_key_levels (front-page box, 1 row per report) ----
    klf = os.path.join(ROOT, "_msr_spx_key_levels_clean.csv")
    if os.path.exists(klf):
        c.execute("""CREATE TABLE spx_key_levels(
            report_date TEXT PRIMARY KEY,
            last_price REAL, upper_pv_band REAL, lower_pv_band REAL,
            upside_risk_pct REAL, downside_risk_pct REAL, spread_pct REAL,
            gex_throttle REAL, gex_flip REAL, implied_move_pct REAL,
            resistance_strike REAL, focal_strike REAL, support_strike REAL,
            flag TEXT,
            FOREIGN KEY(report_date) REFERENCES reports(report_date))""")
        for r in rd("_msr_spx_key_levels_clean.csv"):
            c.execute("INSERT INTO spx_key_levels VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                r["report_date"], num(r["last_price"]), num(r["upper_pv_band"]),
                num(r["lower_pv_band"]), num(r["upside_risk_pct"]), num(r["downside_risk_pct"]),
                num(r["spread_pct"]), num(r["gex_throttle"]), num(r["gex_flip"]),
                num(r["implied_move_pct"]), num(r["resistance_strike"]),
                num(r["focal_strike"]), num(r["support_strike"]), r["flag"]))

    # ---- sector_bands ----
    c.execute("""CREATE TABLE sector_bands(
        report_date TEXT, asset TEXT, last REAL, upside_pct REAL, downside_pct REAL,
        upper_pvb REAL, lower_pvb REAL, spread_pct REAL, rvol_1m REAL, beta_2y REAL,
        src_page INTEGER, flag TEXT,
        PRIMARY KEY(report_date, asset),
        FOREIGN KEY(report_date) REFERENCES reports(report_date))""")
    for r in rd("_msr_sector_bands_clean.csv"):
        c.execute("INSERT INTO sector_bands VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            r["report_date"], r["asset"], num(r["last"]), num(r["upside_pct"]),
            num(r["downside_pct"]), num(r["upper_pvb"]), num(r["lower_pvb"]),
            num(r["spread_pct"]), num(r["rvol_1m"]), num(r["beta_2y"]),
            int(r["src_page"]) if r["src_page"] else None, r["flag"]))

    # ---- realized_vol ----
    c.execute("""CREATE TABLE realized_vol(
        report_date TEXT, dropoff TEXT, date_1m TEXT, drop_1m_pct REAL,
        date_3m TEXT, drop_3m_pct REAL, src_page INTEGER, flag TEXT,
        PRIMARY KEY(report_date, dropoff),
        FOREIGN KEY(report_date) REFERENCES reports(report_date))""")
    for r in rd("_msr_realized_vol_clean.csv"):
        c.execute("INSERT INTO realized_vol VALUES(?,?,?,?,?,?,?,?)", (
            r["report_date"], r["dropoff"], r["date_1m"] or None, num(r["drop_1m_pct"]),
            r["date_3m"] or None, num(r["drop_3m_pct"]),
            int(r["src_page"]) if r["src_page"] else None, r["flag"]))

    # ---- spx_daily_returns ----
    c.execute("""CREATE TABLE spx_daily_returns(
        date TEXT PRIMARY KEY, spx_daily_return_pct REAL,
        n_observations INTEGER, agreement TEXT)""")
    for r in rd("_msr_spx_daily_returns.csv"):
        c.execute("INSERT INTO spx_daily_returns VALUES(?,?,?,?)", (
            r["date"], num(r["spx_daily_return_pct"]),
            int(r["n_observations"]) if r["n_observations"] else None, r["agreement"]))

    # ---- sitrep_recaps (reference) ----
    c.execute("""CREATE TABLE sitrep_recaps(
        file_name TEXT, rel_path TEXT, size_mb REAL, page_count INTEGER, sha256 TEXT)""")
    for r in rd("_msr_sitrep_recaps.csv", "utf-8-sig"):
        c.execute("INSERT INTO sitrep_recaps VALUES(?,?,?,?,?)", (
            r["FileName"], r["RelPath"], num(r["SizeMB"]),
            int(float(r["PageCount"])) if r["PageCount"] else None, r["SHA256"]))

    # ---- indexes ----
    for stmt in [
        "CREATE INDEX ix_sector_asset ON sector_bands(asset)",
        "CREATE INDEX ix_sector_date ON sector_bands(report_date)",
        "CREATE INDEX ix_rv_date1m ON realized_vol(date_1m)",
        "CREATE INDEX ix_rv_date3m ON realized_vol(date_3m)",
    ]:
        c.execute(stmt)

    # ---- views ----
    c.execute("""CREATE VIEW v_sector_trusted AS SELECT * FROM sector_bands
        WHERE flag NOT LIKE '%anomaly:%' AND flag NOT LIKE '%missing:%'""")
    # 'reorder:'/'derived:' are resolved-provenance tags, not open issues; only
    # 'missing:' and 'partial_row' mark genuinely-unresolved cells.
    c.execute("""CREATE VIEW v_realized_trusted AS SELECT * FROM realized_vol
        WHERE flag NOT LIKE '%missing:%' AND flag NOT LIKE '%partial%'""")
    c.execute("""CREATE VIEW v_data_quality AS
        SELECT 'reports' tbl, COUNT(*) rows, COUNT(*) trusted FROM reports
        UNION ALL SELECT 'sector_bands', (SELECT COUNT(*) FROM sector_bands),
                         (SELECT COUNT(*) FROM v_sector_trusted)
        UNION ALL SELECT 'realized_vol', (SELECT COUNT(*) FROM realized_vol),
                         (SELECT COUNT(*) FROM v_realized_trusted)
        UNION ALL SELECT 'spx_daily_returns', (SELECT COUNT(*) FROM spx_daily_returns),
                         (SELECT COUNT(*) FROM spx_daily_returns)
        UNION ALL SELECT 'sitrep_recaps', (SELECT COUNT(*) FROM sitrep_recaps),
                         (SELECT COUNT(*) FROM sitrep_recaps)
        UNION ALL SELECT 'spx_key_levels', (SELECT COUNT(*) FROM spx_key_levels),
                         (SELECT COUNT(*) FROM spx_key_levels WHERE flag='')""")

    con.commit()
    print("Built", DB)
    for row in c.execute("SELECT tbl, rows, trusted FROM v_data_quality"):
        print(f"  {row[0]:20} rows={row[1]:5}  trusted={row[2]}")
    con.close()


if __name__ == "__main__":
    main()
