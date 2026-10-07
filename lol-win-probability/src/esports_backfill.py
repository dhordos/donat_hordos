#!/usr/bin/env python3
"""Backfill historical price series for closed LoL match markets.

Pulls closed "LoL: X vs Y" events from the Gamma API, then for each
winner-type market fetches the full 1-minute price history of both tokens
from the CLOB prices-history endpoint. Stores into esports.db so
esports_analyze.py can measure in-play swings and legging opportunities
on real past matches.

Usage:  python esports_backfill.py [--pages 3]
        (each page = 100 closed events, newest first)

Note: the history is a printed-price series - it carries no spread/depth
info, so anything computed from it is mildly optimistic. The live collector
records real books for future matches; backfill is for breadth.
"""

import argparse
import json
import logging
import os
import sqlite3
import time

import requests

from esports_collector import (open_db, is_core, GAMMA_API, TAG,
                               TITLE_PREFIX, CLOB_API)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("backfill")


def ensure_history_table(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS price_history(
        asset TEXT, ts INTEGER, price REAL,
        PRIMARY KEY (asset, ts)
    );
    """)
    db.commit()


def fetch_history(session, token):
    r = session.get(
        CLOB_API + "/prices-history",
        params={"market": token, "interval": "max", "fidelity": 1},
        timeout=30)
    r.raise_for_status()
    return r.json().get("history") or []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=3,
                    help="pages of 100 closed events to scan (newest first)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    db = open_db(os.path.join(BASE_DIR, "esports.db"))
    ensure_history_table(db)
    session = requests.Session()
    session.headers["User-Agent"] = "polymarket-esports-backfill/1.0 (research)"

    n_events = n_markets = n_points = 0
    for page in range(args.pages):
        try:
            r = session.get(
                GAMMA_API + "/events",
                params={"tag_slug": TAG, "closed": "true", "limit": 100,
                        "offset": page * 100, "order": "endDate",
                        "ascending": "false"},
                timeout=30)
            events = r.json()
        except Exception as e:
            log.warning("event page %d failed: %s", page, e)
            continue
        if not events:
            break
        for e in events:
            title = e.get("title") or ""
            slug = e.get("slug")
            if not slug or not title.startswith(TITLE_PREFIX):
                continue
            db.execute(
                "INSERT OR IGNORE INTO events(slug, title, discovered_at, closed)"
                " VALUES (?,?,?,1)", (slug, title, int(time.time())))
            db.execute("UPDATE events SET closed=1 WHERE slug=?", (slug,))
            n_events += 1
            for m in e.get("markets") or []:
                if not is_core(m.get("groupItemTitle")):
                    continue
                cid = m.get("conditionId")
                try:
                    toks = json.loads(m.get("clobTokenIds") or "[]")
                    outs = json.loads(m.get("outcomes") or "[]")
                    finals = json.loads(m.get("outcomePrices") or "[]")
                except ValueError:
                    continue
                if len(toks) != 2:
                    continue
                db.execute(
                    """INSERT OR IGNORE INTO markets
                       (condition_id, event_slug, question, group_title,
                        token0, outcome0, token1, outcome1, game_start_time, core)
                       VALUES (?,?,?,?,?,?,?,?,?,1)""",
                    (cid, slug, m.get("question"), m.get("groupItemTitle"),
                     toks[0], outs[0] if outs else None,
                     toks[1], outs[1] if len(outs) > 1 else None,
                     m.get("gameStartTime")))
                db.execute(
                    "UPDATE markets SET closed=1, final0=?, final1=? WHERE condition_id=?",
                    (float(finals[0]) if len(finals) > 0 else None,
                     float(finals[1]) if len(finals) > 1 else None, cid))
                already = db.execute(
                    "SELECT COUNT(*) FROM price_history WHERE asset=?",
                    (toks[0],)).fetchone()[0]
                if already > 0:
                    continue  # backfilled earlier
                for tok in toks:
                    try:
                        hist = fetch_history(session, tok)
                    except Exception as ex:
                        log.warning("history failed for %s: %s", tok[:12], ex)
                        continue
                    db.executemany(
                        "INSERT OR IGNORE INTO price_history VALUES (?,?,?)",
                        [(tok, h["t"], h["p"]) for h in hist])
                    n_points += len(hist)
                    time.sleep(0.15)
                n_markets += 1
            db.commit()
        log.info("page %d done (events so far: %d, markets: %d, points: %d)",
                 page, n_events, n_markets, n_points)

    log.info("backfill complete: %d LoL events, %d winner markets, %d price points",
             n_events, n_markets, n_points)


if __name__ == "__main__":
    main()
