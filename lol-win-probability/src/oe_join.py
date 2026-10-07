#!/usr/bin/env python3
"""Link Oracle's Elixir games to Polymarket LoL events (Tier B foundation).

Polymarket event slugs carry the match date (lol-abc-xyz-2026-07-28) and the
Match Winner outcomes carry full team names in the same style OE uses, so the
join key is: normalized team-name pair + date within +-1 day.

Output: oe_links.json
  { event_slug: { "league": ..., "date": ...,
                  "games": [ {gameid, game_no, patch, blue, red,
                              blue_champs, red_champs, blue_win} ] } }

Team-name normalization is shared with state_analyze.py (suffix strip +
aliases) - that mapping hit 100% on the lolesports linker.

Usage: python oe_join.py [--db data/esports_bf.db] [--oe data/oe_2026.csv]
"""

import argparse
import csv
import datetime
import json
import os
import re
import sqlite3
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SUFFIXES = ("esports", "esport", "gaming", "team", "club", "kia")
ALIASES = {"beijingjdg": "jd", "hanjinbrion": "brion",
           "nongshimredforce": "nongshim"}


def norm(s):
    n = re.sub(r"[^a-z0-9]", "", (s or "").lower())
    for suf in SUFFIXES:
        if n.endswith(suf) and len(n) - len(suf) >= 2:
            n = n[:-len(suf)]
    return ALIASES.get(n, n)


def load_oe(path):
    """gameid -> {date, league, game_no, patch, sides: {Blue/Red:
    {team, champs, win}}}"""
    games = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            gid = r.get("gameid")
            if not gid:
                continue
            g = games.setdefault(gid, {
                "date": (r.get("date") or "")[:10],
                "league": r.get("league"),
                "game_no": r.get("game"),
                "patch": r.get("patch"),
                "sides": {}})
            side = r.get("side")
            s = g["sides"].setdefault(side, {"team": None, "champs": [],
                                             "win": None})
            if r.get("position") == "team":
                s["team"] = r.get("teamname")
                s["win"] = r.get("result")
            else:
                if r.get("champion"):
                    s["champs"].append(r.get("champion"))
                if s["team"] is None:
                    s["team"] = r.get("teamname")
    return games


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(BASE_DIR, "data",
                                                 "esports_bf.db"))
    ap.add_argument("--oe", default=os.path.join(BASE_DIR, "data",
                                                 "oe_2026.csv"))
    ap.add_argument("--out", default=os.path.join(BASE_DIR, "oe_links.json"))
    args = ap.parse_args()

    games = load_oe(args.oe)
    print(f"OE games loaded: {len(games)}")

    # index: (normA, normB, date) -> [gameid] with both name orders
    idx = defaultdict(list)
    for gid, g in games.items():
        b = g["sides"].get("Blue", {}).get("team")
        r = g["sides"].get("Red", {}).get("team")
        if not b or not r or not g["date"]:
            continue
        idx[(norm(b), norm(r), g["date"])].append(gid)
        idx[(norm(r), norm(b), g["date"])].append(gid)

    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    events = db.execute("""
        SELECT e.slug, e.title, m.outcome0, m.outcome1 FROM events e
        JOIN markets m ON m.event_slug = e.slug
                       AND m.group_title = 'Match Winner'""").fetchall()

    links = {}
    n_match = n_nodate = 0
    per_league = defaultdict(lambda: [0, 0])  # league -> [matched, total]
    for slug, title, o0, o1 in events:
        m = re.search(r"(\d{4}-\d{2}-\d{2})$", slug or "")
        if not m:
            n_nodate += 1
            continue
        d0 = datetime.date.fromisoformat(m.group(1))
        found = []
        for dd in (0, 1, -1):
            d = (d0 + datetime.timedelta(days=dd)).isoformat()
            found = idx.get((norm(o0), norm(o1), d), [])
            if found:
                break
        lg_m = re.search(r"\)\s*-\s*(.+)$", title or "")
        lg = (lg_m.group(1).strip()[:24] if lg_m else "?")
        per_league[lg][1] += 1
        if not found:
            continue
        per_league[lg][0] += 1
        n_match += 1
        gs = []
        for gid in sorted(set(found), key=lambda x: games[x]["game_no"] or ""):
            g = games[gid]
            b, r = g["sides"].get("Blue", {}), g["sides"].get("Red", {})
            gs.append({"gameid": gid, "game_no": g["game_no"],
                       "patch": g["patch"], "oe_league": g["league"],
                       "blue": b.get("team"), "red": r.get("team"),
                       "blue_champs": b.get("champs"),
                       "red_champs": r.get("champs"),
                       "blue_win": b.get("win")})
        links[slug] = {"date": m.group(1), "league": lg, "games": gs}

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(links, f, ensure_ascii=False, indent=1)

    print(f"Polymarket MW events: {len(events)} | matched to OE: {n_match} "
          f"({100*n_match/max(1,len(events)):.0f}%)")
    print("\nper league (matched/total):")
    for lg, (mm, tt) in sorted(per_league.items(), key=lambda kv: -kv[1][1])[:20]:
        print(f"  {lg:26s} {mm:4d}/{tt:4d}")
    print(f"\nwritten: {args.out}")


if __name__ == "__main__":
    main()
