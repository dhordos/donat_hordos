#!/usr/bin/env python3
"""One-off: live in-play win probability from the state model.

Finds the in-progress game of a league via the lolesports API, grabs the
latest telemetry frame, and runs the phase-trained state model on it.

Usage: python live_predict.py --league LPL
"""

import argparse
import datetime
import sqlite3

import numpy as np
import requests

from lolesports_fetch import LEAGUE_IDS, KEY, API, FEED, iso_round10
from state_model import (DB, SPLIT_DATE, PHASES, fit_logistic, predict,
                         load_oe_labels, label_games, build_samples)


def api_get(session, url, params=None):
    r = session.get(url, params=params, timeout=20,
                    headers={"x-api-key": KEY,
                             "Referer": "https://lolesports.com/"})
    r.raise_for_status()
    if r.status_code == 204 or not r.text.strip():
        return {}
    return r.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default="LPL")
    args = ap.parse_args()
    s = requests.Session()

    # 1) find the in-progress event
    d = api_get(s, API + "/getSchedule",
                {"hl": "en-US", "leagueId": LEAGUE_IDS[args.league]})
    live = [e for e in d.get("data", {}).get("schedule", {}).get("events", [])
            if e.get("state") == "inProgress" and e.get("type") == "match"]
    if not live:
        print("no in-progress match found in", args.league)
        return
    ev = live[0]
    teams = [t.get("name") for t in ev.get("match", {}).get("teams", [])]
    print("live match:", " vs ".join(teams))

    # 2) find the in-progress game
    det = api_get(s, API + "/getEventDetails",
                  {"hl": "en-US", "id": ev["match"]["id"]})
    games = (det.get("data", {}).get("event", {}).get("match", {})
             .get("games", []))
    cur = next((g for g in games if g.get("state") == "inProgress"), None)
    if cur is None:
        print("between games right now (states: "
              f"{[g.get('state') for g in games]})")
        return
    print(f"game {cur.get('number')} in progress")

    # 3) latest window frames
    now = datetime.datetime.now(datetime.timezone.utc)
    w = api_get(s, f"{FEED}/window/{cur['id']}",
                {"startingTime": iso_round10(
                    now - datetime.timedelta(seconds=60))})
    frames = w.get("frames") or []
    if not frames:
        print("no frames yet (game may be in draft/pause)")
        return
    md = w.get("gameMetadata") or {}
    blue_id = (md.get("blueTeamMetadata") or {}).get("esportsTeamId")
    id2name = {t.get("id"): t.get("name")
               for t in ev.get("match", {}).get("teams", [])}
    blue_name = id2name.get(blue_id, "blue")
    red_name = next((n for i, n in id2name.items() if i != blue_id), "red")

    f = frames[-1]
    b, r = f.get("blueTeam") or {}, f.get("redTeam") or {}
    t0 = datetime.datetime.fromisoformat(
        frames[0]["rfc460Timestamp"].replace("Z", "+00:00"))
    # NOTE: window start != game start; estimate the minute from gold instead
    # is unreliable - use the gameState timer if present, else assume the
    # caller checks. Simplest robust estimate: total gold ~ (400+GPM*t)*10.
    ts = datetime.datetime.fromisoformat(
        f["rfc460Timestamp"].replace("Z", "+00:00"))
    total_gold = (b.get("totalGold") or 0) + (r.get("totalGold") or 0)
    minute = max(2, min(40, int((total_gold / 10 - 500) / 400)))  # rough GPM
    feat = [( (b.get("totalGold") or 0) - (r.get("totalGold") or 0)) / 1000.0,
            (b.get("totalKills") or 0) - (r.get("totalKills") or 0),
            (b.get("towers") or 0) - (r.get("towers") or 0),
            len(b.get("dragons") or []) - len(r.get("dragons") or []),
            (b.get("barons") or 0) - (r.get("barons") or 0),
            (b.get("inhibitors") or 0) - (r.get("inhibitors") or 0),
            minute / 10.0]
    print(f"\nstate at {ts.strftime('%H:%M:%S')} UTC (est. minute ~{minute}):")
    print(f"  {blue_name} (blue): gold {b.get('totalGold')}, "
          f"kills {b.get('totalKills')}, towers {b.get('towers')}, "
          f"dragons {len(b.get('dragons') or [])}, barons {b.get('barons')}")
    print(f"  {red_name} (red) : gold {r.get('totalGold')}, "
          f"kills {r.get('totalKills')}, towers {r.get('towers')}, "
          f"dragons {len(r.get('dragons') or [])}, barons {r.get('barons')}")

    # 4) train phase models (fast) and predict
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    oe = load_oe_labels()
    labels = label_games(db, oe)
    X, y, dates, gids, minutes = build_samples(db, labels)
    train = dates < SPLIT_DATE
    for name, lo, hi in PHASES:
        if lo <= minute <= hi:
            m = (minutes >= lo) & (minutes <= hi)
            w_ = fit_logistic(X[train & m], y[train & m])
            p = float(predict(w_, np.array([feat]))[0])
            print(f"\nSTATE MODEL ({name}): "
                  f"P({blue_name} wins) = {p:.3f} | "
                  f"P({red_name} wins) = {1-p:.3f}")
            break


if __name__ == "__main__":
    main()
