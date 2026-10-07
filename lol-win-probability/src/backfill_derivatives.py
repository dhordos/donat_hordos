#!/usr/bin/env python3
"""Backfill price history for NON-winner (derivative) markets already in the DB.

The original esports_backfill.py only fetched winner-type markets (is_core).
This fills the gap for the market classes needed by the Tier A backtests:
Game 3/4 Winner, O/U N.5 Games, Game Handicap, First Blood, Total Kills.
Props (Quadra/Penta/Both Teams/Odd-Even) are SKIPPED on purpose: 78 trades
across 2,322 markets means their histories are ~empty anyway.

Markets and tokens are read from the local DB - no Gamma calls needed.

Usage: python backfill_derivatives.py [--db data/esports.db]
"""

import argparse
import logging
import sqlite3
import time

import requests

CLOB_API = "https://clob.polymarket.com"
log = logging.getLogger("backfill2")

TARGET_SQL = """
SELECT condition_id, group_title, token0, token1 FROM markets
WHERE closed=1 AND final0 IS NOT NULL
  AND (group_title IN ('Match Winner', 'Game 1 Winner', 'Game 2 Winner',
                       'Game 3 Winner', 'Game 4 Winner')
       OR group_title LIKE 'O/U%Games'
       OR group_title LIKE 'Game Handicap:%'
       OR group_title LIKE 'First Blood%'
       OR group_title LIKE 'Total Kills%')
  AND token0 NOT IN (SELECT DISTINCT asset FROM price_history)
  AND condition_id NOT IN (SELECT condition_id FROM backfill_done)
"""


def fetch_history(session, token):
    r = session.get(
        CLOB_API + "/prices-history",
        params={"market": token, "interval": "max", "fidelity": 1},
        timeout=30)
    r.raise_for_status()
    return r.json().get("history") or []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/esports.db")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    db = sqlite3.connect(args.db)
    # markets whose history was already fetched (even if it came back empty -
    # untraded markets return no points and need not be re-asked every run)
    db.execute("CREATE TABLE IF NOT EXISTS backfill_done(condition_id TEXT PRIMARY KEY)")
    targets = db.execute(TARGET_SQL).fetchall()
    log.info("markets to backfill: %d", len(targets))

    session = requests.Session()
    session.headers["User-Agent"] = "polymarket-esports-backfill/1.0 (research)"

    n_mkt = n_pts = n_err = 0
    for i, (cid, gt, tok0, tok1) in enumerate(targets):
        ok = True
        for tok in (tok0, tok1):
            try:
                hist = fetch_history(session, tok)
            except Exception as ex:
                n_err += 1
                ok = False
                log.warning("history failed for %s (%s): %s", tok[:12], gt, ex)
                time.sleep(2)
                continue
            db.executemany(
                "INSERT OR IGNORE INTO price_history VALUES (?,?,?)",
                [(tok, h["t"], h["p"]) for h in hist])
            n_pts += len(hist)
            time.sleep(0.15)
        if ok:
            db.execute("INSERT OR IGNORE INTO backfill_done VALUES (?)", (cid,))
        n_mkt += 1
        if n_mkt % 50 == 0:
            db.commit()
            log.info("%d/%d markets done, %d points, %d errors",
                     n_mkt, len(targets), n_pts, n_err)
    db.commit()
    log.info("DONE: %d markets, %d price points, %d errors", n_mkt, n_pts, n_err)


if __name__ == "__main__":
    main()
