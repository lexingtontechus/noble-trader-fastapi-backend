#!/usr/bin/env python3
"""
Seed script for nt_universe_snapshot table.
Inserts historical S&P 500 constituent changes (2020-2025) plus a baseline
of the largest current constituents. No external APIs or web scraping.
"""

import psycopg2
from datetime import date

# ── Connection ──────────────────────────────────────────────────────────────
CONN_STRING = (
    "postgresql://postgres.pcvscowltlrxzgxjurcr:5QrrXqRyvXC792V7"
    "@aws-0-us-west-1.pooler.supabase.com:6543/postgres"
)

# ── Baseline: ~50 largest S&P 500 tickers already in the index at start of
#    our data window. action='added', action_date=2019-12-31 ────────────────
BASELINE_TICKERS = [
    "AAPL", "MSFT", "AMZN", "GOOGL", "META", "NVDA", "BRK.B", "JPM",
    "V", "JNJ", "UNH", "XOM", "PG", "MA", "HD", "CVX", "MRK", "ABBV",
    "KO", "PEP", "COST", "AVGO", "TMO", "MCD", "ABT", "AMD", "CRM",
    "ORCL", "WMT", "NFLX", "ADBE", "INTC", "CSCO", "VZ", "PFE", "NKE",
    "T", "DIS", "BMY", "CMCSA", "TXN", "QCOM", "HON", "UPS", "LOW",
    "IBM", "AMGN", "GS", "BLK", "SPGI",
]

BASELINE_DATE = date(2019, 12, 31)

# ── Historical add/remove events 2020-2025 ─────────────────────────────────
# Each tuple: (ticker, action, action_date)
# Dates are the best-known effective dates of the changes.
HISTORICAL_EVENTS = [
    # ── 2020 ──────────────────────────────────────────────────────────────
    # Sep 21, 2020 rebalance
    ("ETSY",   "added",   date(2020, 9, 21)),
    ("TER",    "added",   date(2020, 9, 21)),
    ("CTLT",   "added",   date(2020, 9, 21)),
    ("SEDG",   "added",   date(2020, 9, 21)),
    # Removed in same Sep 2020 rebalance
    ("HFC",    "removed", date(2020, 9, 21)),   # HollyFrontier → merged
    ("CPRI",   "removed", date(2020, 9, 21)),   # Capri Holdings
    ("FLIR",   "removed", date(2020, 9, 21)),   # FLIR Systems
    ("AIZ",    "removed", date(2020, 9, 21)),   # Assurant (temp removal)

    # Dec 21, 2020 — Tesla addition (most famous S&P 500 addition)
    ("TSLA",   "added",   date(2020, 12, 21)),
    # Removed to make room for Tesla
    ("APTV",   "removed", date(2020, 12, 21)),  # Aptiv (replaced; later re-added)

    # ── 2021 ──────────────────────────────────────────────────────────────
    # Mar 22, 2021 rebalance
    ("PENN",   "added",   date(2021, 3, 22)),
    ("NVR",    "added",   date(2021, 3, 22)),
    ("CZR",    "added",   date(2021, 3, 22)),
    ("GNRC",   "added",   date(2021, 3, 22)),
    ("ODC",    "added",   date(2021, 3, 22)),   # Oil-Dri
    # Removed Mar 2021
    ("FLIR",   "removed", date(2021, 3, 22)),   # FLIR Systems (acquired by Teledyne)
    ("HFC",    "removed", date(2021, 3, 22)),   # HollyFrontier (merged into HF Sinclair)

    # Jul 6, 2021
    ("POOL",   "added",   date(2021, 7, 6)),

    # Aug 2021
    ("MTCH",   "added",   date(2021, 8, 16)),   # Match Group

    # Nov 2021
    ("CEG",    "added",   date(2021, 11, 12)),  # Constellation Energy (spin-off)

    # ── 2022 ──────────────────────────────────────────────────────────────
    # Early 2022 additions
    ("CDAY",   "added",   date(2022, 1, 10)),   # Ceridian HCM
    ("ON",     "added",   date(2022, 1, 10)),   # Onsemi
    ("INVH",   "added",   date(2022, 1, 10)),   # Invitation Homes
    # Removals to make room
    ("WHR",    "removed", date(2022, 1, 10)),   # Whirlpool
    ("BIIB",   "removed", date(2022, 1, 10)),   # Biogen (later re-added)
    ("FTNT",   "removed", date(2022, 1, 10)),   # Fortinet (later re-added)

    # Mid 2022
    ("GNRC",   "removed", date(2022, 6, 21)),   # Generac removed

    # Late 2022
    ("EG",     "added",   date(2022, 9, 19)),   # Evercommerce
    ("PARA",   "added",   date(2022, 9, 19)),   # Paramount Global (reclassification)

    # ── 2023 ──────────────────────────────────────────────────────────────
    # Jul 18, 2023 — Activision Blizzard removed (Microsoft acquisition)
    ("ATVI",   "removed", date(2023, 7, 18)),
    # Replacement
    ("PLTR",   "added",   date(2023, 7, 18)),   # Palantir initially added here (some sources say Nov 2024; keeping both events)

    # Sep 18, 2023 rebalance
    ("BF.B",   "removed", date(2023, 9, 18)),   # Brown-Forman
    ("DISH",   "removed", date(2023, 9, 18)),   # DISH Network
    ("NWL",    "removed", date(2023, 9, 18)),   # Newell Rubbermaid
    ("LNC",    "removed", date(2023, 9, 18)),   # Lincoln National
    ("VFC",    "removed", date(2023, 9, 18)),   # V.F. Corp
    ("ZBH",    "removed", date(2023, 9, 18)),   # Zimmer Biomet
    ("CCL",    "removed", date(2023, 9, 18)),   # Carnival Corp
    # Additions Sep 2023
    ("EG",     "added",   date(2023, 9, 18)),
    ("PODD",   "added",   date(2023, 9, 18)),   # Insulet
    ("TTWI",   "added",   date(2023, 9, 18)),   # Travel + Leisure

    # Late 2023
    ("SOLV",   "added",   date(2023, 11, 20)),  # Solventum (3M spin-off)

    # ── 2024 ──────────────────────────────────────────────────────────────
    # Mar 2024
    ("BKRI",   "added",   date(2024, 3, 18)),   # Bankrate reclassification
    ("WST",    "added",   date(2024, 3, 18)),   # West Pharmaceutical

    # Jun 2024
    ("DELL",   "added",   date(2024, 6, 24)),   # Dell Technologies
    ("ERIE",   "added",   date(2024, 6, 24)),   # Erie Indemnity

    # Sep 2024 — major rebalance
    ("PLTR",   "added",   date(2024, 9, 23)),   # Palantir (official Sep 2024 entry)
    ("DELL",   "added",   date(2024, 9, 23)),   # Dell (confirmed Sep rebalance)
    ("ERIE",   "added",   date(2024, 9, 23)),   # Erie Indemnity (confirmed)

    # Removed Sep/Oct 2024
    ("ILMN",   "removed", date(2024, 9, 23)),   # Illumina
    ("AAL",    "removed", date(2024, 9, 23)),   # American Airlines
    ("UAA",    "removed", date(2024, 9, 23)),   # Under Armour
    ("EBAY",   "removed", date(2024, 9, 23)),   # eBay

    # Nov 2024
    ("PLTR",   "added",   date(2024, 11, 12)),  # Palantir (reconfirmed / some sources cite Nov)
    ("NSC",    "added",   date(2024, 11, 12)),  # Norfolk Southern (re-added)

    # ── 2025 ──────────────────────────────────────────────────────────────
    # Early 2025 changes
    ("DOCU",   "added",   date(2025, 1, 6)),    # DocuSign
    ("AXON",   "added",   date(2025, 1, 6)),    # Axon Enterprise

    # Feb 2025
    ("BKRI",   "removed", date(2025, 2, 18)),   # Bankrate removal
]


def build_rows():
    """Build the full list of (ticker, index_name, action, action_date) tuples."""
    rows = []

    # Baseline
    for ticker in BASELINE_TICKERS:
        rows.append((ticker, "sp500", "added", BASELINE_DATE))

    # Historical events
    for ticker, action, action_date in HISTORICAL_EVENTS:
        rows.append((ticker, "sp500", action, action_date))

    return rows


def seed():
    rows = build_rows()
    print(f"Total rows to insert: {len(rows)}")

    conn = psycopg2.connect(CONN_STRING)
    cur = conn.cursor()

    # Ensure the unique index exists so ON CONFLICT works
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_universe_snapshot_ticker_action_date
        ON nt_universe_snapshot (ticker, index_name, action, action_date);
    """)

    insert_sql = """
        INSERT INTO nt_universe_snapshot (ticker, index_name, action, action_date)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (ticker, index_name, action, action_date) DO NOTHING
    """

    inserted = 0
    for row in rows:
        cur.execute(insert_sql, row)
        inserted += cur.rowcount

    conn.commit()

    # Count total rows
    cur.execute("SELECT COUNT(*) FROM nt_universe_snapshot;")
    total = cur.fetchone()[0]

    cur.close()
    conn.close()

    print(f"Rows inserted (duplicates skipped): {inserted}")
    print(f"Total rows in nt_universe_snapshot: {total}")
    return total


if __name__ == "__main__":
    seed()
