#!/usr/bin/env python3
"""Independent calibration check on weather markets.

The first calibration study used follower.db, which only contains markets
TraderA chose to trade - his selection may itself be the signal. This
script pulls closed weather markets straight from the Gamma API (no wallet
filter), fetches each market's EARLIEST traded price from the CLOB
prices-history endpoint, and compares that ex-ante price with the actual
outcome. If the "early cheap outcomes are underpriced" anomaly survives on
this unfiltered sample, it is real; if it vanishes, it was TraderA's
market selection, not the price.

Needs Polymarket API access:

  python weather_verify.py --pages 3

Results are cached in weather_verify.db - re-runs only fetch new markets.
"""

import argparse
import json
import logging
import os
import sqlite3
import time

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
log = logging.getLogger("verify")

BUCKETS = [(0.00, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20),
           (0.20, 0.30), (0.30, 0.40), (0.40, 0.50), (0.50, 0.60),
           (0.60, 0.70), (0.70, 0.80), (0.80, 0.85), (0.85, 0.90),
           (0.90, 0.95), (0.95, 1.00)]


def open_db():
    db = sqlite3.connect(os.path.join(BASE_DIR, "weather_verify.db"))
    db.executescript("""
    CREATE TABLE IF NOT EXISTS verify_markets(
        condition_id TEXT PRIMARY KEY,
        question TEXT, yes_token TEXT,
        first_price REAL, first_ts INTEGER,
        n_points INTEGER, final REAL, end_date TEXT
    );
    """)
    db.commit()
    return db


def fetch_first_price(session, token):
    """Earliest sane printed price of a token, from its full history."""
    r = session.get(
        CLOB_API + "/prices-history",
        params={"market": token, "interval": "max", "fidelity": 10},
        timeout=30)
    r.raise_for_status()
    hist = r.json().get("history") or []
    for h in hist:
        p = h.get("p")
        if p is not None and 0.01 <= p <= 0.99:
            return float(p), int(h["t"]), len(hist)
    return None, None, len(hist)


def collect(db, session, pages, max_markets):
    n_new = 0
    for page in range(pages):
        try:
            r = session.get(
                GAMMA_API + "/events",
                params={"tag_slug": "weather", "closed": "true",
                        "limit": 100, "offset": page * 100,
                        "order": "endDate", "ascending": "false"},
                timeout=30)
            events = r.json()
        except Exception as e:
            log.warning("event page %d failed: %s", page, e)
            continue
        if not events:
            break
        for e in events:
            for m in e.get("markets") or []:
                if n_new >= max_markets:
                    return n_new
                q = m.get("question") or ""
                if "temperature" not in q.lower():
                    continue
                cid = m.get("conditionId")
                if not cid or db.execute(
                        "SELECT 1 FROM verify_markets WHERE condition_id=?",
                        (cid,)).fetchone():
                    continue
                try:
                    toks = json.loads(m.get("clobTokenIds") or "[]")
                    outs = json.loads(m.get("outcomes") or "[]")
                    finals = json.loads(m.get("outcomePrices") or "[]")
                except ValueError:
                    continue
                if len(toks) != 2 or len(finals) != 2:
                    continue
                yes_i = outs.index("Yes") if "Yes" in outs else 0
                try:
                    fp, fts, npts = fetch_first_price(session, toks[yes_i])
                except Exception as ex:
                    log.warning("history failed for %s: %s", cid[:12], ex)
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO verify_markets VALUES (?,?,?,?,?,?,?,?)",
                    (cid, q, toks[yes_i], fp, fts, npts,
                     float(finals[yes_i]), m.get("endDate")))
                n_new += 1
                if n_new % 50 == 0:
                    db.commit()
                    log.info("collected %d markets...", n_new)
                time.sleep(0.15)
        db.commit()
    return n_new


def report(db):
    rows = db.execute(
        """SELECT first_price, final FROM verify_markets
           WHERE first_price IS NOT NULL AND final IS NOT NULL""").fetchall()
    print("=" * 74)
    print("INDEPENDENT CALIBRATION CHECK - unfiltered weather markets")
    print("=" * 74)
    print("markets with usable first price: {}".format(len(rows)))
    if not rows:
        return
    print()
    print("  {:<12} {:>7} {:>10} {:>10} {:>10} {:>9}".format(
        "price band", "n", "avg price", "actual", "diff", "EV/$1"))
    print("  " + "-" * 62)
    for lo, hi in BUCKETS:
        sel = [(p, f) for p, f in rows if lo <= p < hi or (hi == 1.0 and p >= lo)]
        if not sel:
            continue
        n = len(sel)
        avg_p = sum(p for p, _ in sel) / n
        won = sum(1 for _, f in sel if f > 0.5) / n
        ev = (won / avg_p - 1) if avg_p > 0 else 0
        print("  {:.2f}-{:.2f}    {:>7} {:>10.4f} {:>10.4f} {:>+10.4f} {:>+8.1f}%".format(
            lo, hi, n, avg_p, won, won - avg_p, 100 * ev))
    print()
    cheap = [(p, f) for p, f in rows if p < 0.15]
    if cheap:
        n = len(cheap)
        avg_p = sum(p for p, _ in cheap) / n
        won = sum(1 for _, f in cheap if f > 0.5) / n
        print("HEADLINE - early cheap (first price < 0.15): {} markets,".format(n))
        print("priced {:.3f} vs actual {:.3f} -> buy EV {:+.1f}%".format(
            avg_p, won, 100 * (won / avg_p - 1) if avg_p else 0))
        print("If this stays strongly positive, the anomaly is REAL (not just")
        print("TraderA's market selection). If it is ~0 or negative, the")
        print("anomaly was selection bias and there is no model-free edge here.")
    print("=" * 74)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--max-markets", type=int, default=800)
    ap.add_argument("--report-only", action="store_true",
                    help="skip collection, just print the table from cache")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    db = open_db()
    if not args.report_only:
        session = requests.Session()
        session.headers["User-Agent"] = "polymarket-weather-verify/1.0 (research)"
        n = collect(db, session, args.pages, args.max_markets)
        log.info("collection done: %d new market(s)", n)
    report(db)


if __name__ == "__main__":
    main()
