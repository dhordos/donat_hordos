#!/usr/bin/env python3
"""Link lolesports events (ls_events, has telemetry) to Polymarket events
(esports_bf.db, has prices/books) by team-pair + date. Refreshes
event_links.json, which was last built on a 31-event subset before the
telemetry backfill grew to 3,663 events.

Output per link: ls_event id, Polymarket event_slug, league, and whether
REAL BOOK data exists for the Match Winner market (not just price prints -
our standing rule is that printed-price backtests are not evidence; only
book-verified entries count).

Usage: python ls_pm_link.py
"""

import datetime
import json
import os
import re
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LS_DB = os.path.join(BASE_DIR, "data", "esports.db")
PM_DB = os.path.join(BASE_DIR, "data", "esports_bf.db")

SUFFIXES = ("esports", "esport", "gaming", "team", "club", "kia")
ALIASES = {"beijingjdg": "jd", "hanjinbrion": "brion",
           "nongshimredforce": "nongshim"}


def norm(s):
    n = re.sub(r"[^a-z0-9]", "", (s or "").lower())
    for suf in SUFFIXES:
        if n.endswith(suf) and len(n) - len(suf) >= 2:
            n = n[:-len(suf)]
    return ALIASES.get(n, n)


def main():
    lsdb = sqlite3.connect(f"file:{LS_DB}?mode=ro", uri=True)
    pmdb = sqlite3.connect(f"file:{PM_DB}?mode=ro", uri=True)

    # index Polymarket events by (norm team pair, date) -> [(slug, cid)]
    pm_idx = {}
    for slug, o0, o1, cid in pmdb.execute(
            """SELECT m.event_slug, m.outcome0, m.outcome1, m.condition_id
               FROM markets m WHERE m.group_title='Match Winner'"""):
        m = re.search(r"(\d{4}-\d{2}-\d{2})$", slug or "")
        if not m:
            continue
        pair = tuple(sorted((norm(o0), norm(o1))))
        pm_idx.setdefault((pair, m.group(1)), []).append((slug, cid))

    n_events = n_matched = n_with_book = 0
    links = []
    for eid, league, t1, t2, start in lsdb.execute(
            """SELECT e.event_id, e.league, e.team1, e.team2, e.start_time
               FROM ls_events e
               WHERE EXISTS (SELECT 1 FROM ls_games g
                            WHERE g.event_id=e.event_id
                            AND EXISTS (SELECT 1 FROM ls_frames f
                                       WHERE f.game_id=g.game_id))"""):
        n_events += 1
        pair = tuple(sorted((norm(t1), norm(t2))))
        try:
            d0 = datetime.date.fromisoformat((start or "")[:10])
        except ValueError:
            continue
        found = None
        for dd in (0, 1, -1):
            key = (pair, (d0 + datetime.timedelta(days=dd)).isoformat())
            if key in pm_idx:
                found = pm_idx[key][0]
                break
        if not found:
            continue
        slug, cid = found
        n_matched += 1
        has_book = pmdb.execute(
            """SELECT 1 FROM books b JOIN markets m ON m.condition_id=?
               WHERE b.asset IN (m.token0, m.token1) LIMIT 1""",
            (cid,)).fetchone() is not None
        if has_book:
            n_with_book += 1
        links.append({"ls_event": eid, "pm_slug": slug, "league": league,
                      "has_book": has_book})

    with open(os.path.join(BASE_DIR, "event_links.json"), "w",
             encoding="utf-8") as f:
        json.dump(links, f, indent=1)

    print(f"lolesports events with telemetry: {n_events}")
    print(f"matched to a Polymarket event    : {n_matched} "
          f"({100*n_matched/max(1,n_events):.0f}%)")
    print(f"  of which with REAL BOOK data   : {n_with_book}")
    from collections import Counter
    print("\nper league (matched / with book):")
    by_league = Counter()
    by_league_book = Counter()
    for l in links:
        by_league[l["league"]] += 1
        if l["has_book"]:
            by_league_book[l["league"]] += 1
    for lg, n in by_league.most_common():
        print(f"  {lg:8s} {n:4d}  (book: {by_league_book.get(lg,0)})")


if __name__ == "__main__":
    main()
