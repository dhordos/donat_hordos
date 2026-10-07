#!/usr/bin/env python3
"""Fetch per-10-second game telemetry from the (unofficial) lolesports API.

Gives what the price data can't: WHAT was happening on the map at any moment
(gold, kills, towers, dragons by type, barons, inhibitors per team). Joined
with our Polymarket trade timestamps this enables state-aware mispricing
analysis ("2k gold vs 3 drakes - whom did the market favor, who was right?").

Endpoints (need the public x-api-key that lolesports.com itself uses; set it
in the LOLESPORTS_API_KEY environment variable):
  esports-api.lolesports.com/persisted/gw/getSchedule?leagueId=...&pageToken=
  esports-api.lolesports.com/persisted/gw/getEventDetails?id=...
  feed.lolesports.com/livestats/v1/window/{gameId}?startingTime=ISO

Usage:
  python lolesports_fetch.py --discover 14        # events of last N days
  python lolesports_fetch.py --frames             # frames for stored games
  python lolesports_fetch.py --discover 14 --frames
"""

import argparse
import datetime
import json
import logging
import os
import sqlite3
import time

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEY = os.environ.get("LOLESPORTS_API_KEY", "")
API = "https://esports-api.lolesports.com/persisted/gw"
FEED = "https://feed.lolesports.com/livestats/v1"
LEAGUE_IDS = {
    # tier 1 (in-play model failed here: the book is too sharp)
    "LCK": "98767991310872058",
    "LPL": "98767991314006698",
    "LEC": "98767991302996019",
    # MINOR leagues - an in-play model has never been tested here.
    # Picked for Polymarket market presence
    # (counts from oe_links.json), so every league here actually has markets.
    "PRM": "105266091639104326",   # Prime League      (159 PM events)
    "LCKC": "98767991335774713",   # LCK Challengers   (108)
    "CD": "105549980953490846",    # Circuito Desafiante (80)
    "LFL": "105266103462388553",   # La Ligue Francaise (75+33)
    "NACL": "109511549831443335",  # North American Challengers (68)
    "LES": "105266074488398661",   # LES Superliga     (46)
    "LIT": "105266094998946936",   # LoL Italian Tournament (44)
    "ROL": "107407335299756365",   # Road of Legends   (38)
    "CBLOL": "98767991332355509",  # CBLOL             (36)
    "HLL": "105266108767593290",   # Hellenic Legends  (36)
    "NLC": "105266098308571975",   # NLC               (31)
    "EM": "100695891328981122",    # EMEA Masters      (29)
    "LCP": "113476371197627891",   # LCP               (29)
    "TCL": "98767991343597634",    # TCL               (28)
    "EBL": "105266111679554379",   # Esports Balkan League (real book coverage)
    "ARL": "109545772895506419",   # Arabian League        (real book coverage)
}
log = logging.getLogger("lsfetch")


def open_db(path):
    # WAL + a generous busy_timeout let several --leagues shards write to the
    # same DB concurrently without "database is locked" aborting a process -
    # readers don't block writers in WAL mode, and writers just queue briefly.
    db = sqlite3.connect(path, timeout=60.0)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=60000")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS ls_events(
        event_id TEXT PRIMARY KEY,
        league TEXT, block TEXT, start_time TEXT,
        team1 TEXT, team2 TEXT, best_of INTEGER, state TEXT
    );
    CREATE TABLE IF NOT EXISTS ls_games(
        game_id TEXT PRIMARY KEY,
        event_id TEXT, number INTEGER, state TEXT,
        blue_team TEXT, red_team TEXT, frames_done INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS ls_frames(
        game_id TEXT, ts INTEGER,
        game_state TEXT,
        b_gold INTEGER, r_gold INTEGER,
        b_kills INTEGER, r_kills INTEGER,
        b_towers INTEGER, r_towers INTEGER,
        b_dragons INTEGER, r_dragons INTEGER,
        b_dragon_types TEXT, r_dragon_types TEXT,
        b_barons INTEGER, r_barons INTEGER,
        b_inhib INTEGER, r_inhib INTEGER,
        PRIMARY KEY (game_id, ts)
    );
    """)
    db.commit()
    return db


def api_get(session, url, params=None):
    r = session.get(url, params=params, timeout=20,
                    headers={"x-api-key": KEY,
                             "Referer": "https://lolesports.com/"})
    r.raise_for_status()
    # the feed returns 204/empty body for "no frames at this time" - treat
    # as an empty page, not an error
    if r.status_code == 204 or not r.text.strip():
        return {}
    return r.json()


def discover(db, session, days, leagues=None):
    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=days))
    n_events = 0
    league_items = ([(lg, LEAGUE_IDS[lg]) for lg in leagues if lg in LEAGUE_IDS]
                    if leagues else list(LEAGUE_IDS.items()))
    for lg, lid in league_items:
        page_token = None
        pages = 0
        while pages < 400:
            params = {"hl": "en-US", "leagueId": lid}
            if page_token:
                params["pageToken"] = page_token
            try:
                d = api_get(session, API + "/getSchedule", params)
            except Exception as e:
                log.warning("[%s] schedule fetch failed: %s", lg, e)
                break
            sched = d.get("data", {}).get("schedule", {})
            events = sched.get("events", [])
            oldest_seen = None
            for ev in events:
                if ev.get("type") != "match":
                    continue
                st = ev.get("startTime")
                if not st:
                    continue
                ts = datetime.datetime.fromisoformat(st.replace("Z", "+00:00"))
                oldest_seen = ts if oldest_seen is None else min(oldest_seen, ts)
                if ts < since:
                    continue
                match = ev.get("match", {})
                mid = match.get("id")
                teams = [t.get("name", "?") for t in match.get("teams", [])]
                if not mid or len(teams) < 2:
                    continue
                db.execute(
                    """INSERT OR REPLACE INTO ls_events
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (mid, lg, ev.get("blockName"), st, teams[0], teams[1],
                     (match.get("strategy") or {}).get("count"),
                     ev.get("state")))
                n_events += 1
            # getSchedule pages BACKWARD in time via 'older' token
            page_token = (sched.get("pages") or {}).get("older")
            pages += 1
            if not page_token or (oldest_seen and oldest_seen < since):
                break
            time.sleep(0.3)
        db.commit()   # commit per league, not one giant transaction across
                      # all leagues - avoids blocking other shards for minutes
        log.info("[%s] events stored so far: %d", lg, n_events)

    # resolve games for stored events that don't have them yet (scoped to the
    # leagues we're responsible for, so a --leagues shard doesn't redo the
    # getEventDetails work for every OTHER shard's events too)
    if leagues:
        placeholders = ",".join("?" * len(leagues))
        ev_rows = db.execute(
            f"""SELECT event_id FROM ls_events e
               WHERE e.league IN ({placeholders})
               AND NOT EXISTS (SELECT 1 FROM ls_games g
                              WHERE g.event_id = e.event_id)""",
            tuple(leagues)).fetchall()
    else:
        ev_rows = db.execute(
            """SELECT event_id FROM ls_events e
               WHERE NOT EXISTS (SELECT 1 FROM ls_games g
                                 WHERE g.event_id = e.event_id)""").fetchall()
    for i, (eid,) in enumerate(ev_rows):
        try:
            d = api_get(session, API + "/getEventDetails",
                        {"hl": "en-US", "id": eid})
        except Exception as e:
            log.warning("details failed for %s: %s", eid, e)
            continue
        match = (d.get("data", {}).get("event") or {}).get("match") or {}
        for g in match.get("games", []):
            db.execute(
                """INSERT OR IGNORE INTO ls_games
                   (game_id, event_id, number, state) VALUES (?,?,?,?)""",
                (g.get("id"), eid, g.get("number"), g.get("state")))
        # batch commits (every 20 events, not every 1): with several --leagues
        # shards writing concurrently, committing per-event turns this loop
        # into the dominant source of lock contention on the shared SQLite
        # file - one commit per API call is far more often than needed.
        if i % 20 == 0:
            db.commit()
        time.sleep(0.25)
    db.commit()
    n_games = db.execute("SELECT COUNT(*) FROM ls_games").fetchone()[0]
    log.info("discovery done: %d events, %d games total",
             db.execute("SELECT COUNT(*) FROM ls_events").fetchone()[0], n_games)


def iso_round10(dt):
    dt = dt - datetime.timedelta(seconds=dt.second % 10,
                                 microseconds=dt.microsecond)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_frame(f):
    def side(t):
        drs = t.get("dragons") or []
        return (t.get("totalGold"), t.get("totalKills"), t.get("towers"),
                len(drs), json.dumps(drs), t.get("barons"), t.get("inhibitors"))
    ts = f.get("rfc460Timestamp")
    dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    b = side(f.get("blueTeam") or {})
    r = side(f.get("redTeam") or {})
    return (int(dt.timestamp()), f.get("gameState"),
            b[0], r[0], b[1], r[1], b[2], r[2],
            b[3], r[3], b[4], r[4], b[5], r[5], b[6], r[6])


def fetch_frames(db, session, max_hours=4, leagues=None):
    sql = """SELECT g.game_id, g.number, g.event_id, e.start_time FROM ls_games g
             JOIN ls_events e ON e.event_id = g.event_id
             WHERE g.frames_done = 0 AND g.state = 'completed'"""
    params = ()
    if leagues:
        sql += f" AND e.league IN ({','.join('?' * len(leagues))})"
        params = tuple(leagues)
    sql += " ORDER BY g.event_id, g.number"
    todo = db.execute(sql, params).fetchall()
    log.info("games needing frames: %d%s", len(todo),
             f" (leagues: {','.join(leagues)})" if leagues else "")
    for game_id, number, event_id, start_time in todo:
        try:
            start = datetime.datetime.fromisoformat(
                start_time.replace("Z", "+00:00"))
        except Exception:
            continue
        # games 2/3 start long after the MATCH start time - anchor the scan
        # to the previous game's last stored frame when we have one
        if number and number > 1:
            prev_end = db.execute(
                """SELECT MAX(f.ts) FROM ls_frames f
                   JOIN ls_games pg ON pg.game_id = f.game_id
                   WHERE pg.event_id = ? AND pg.number = ?""",
                (event_id, number - 1)).fetchone()[0]
            if prev_end:
                start = max(start, datetime.datetime.fromtimestamp(
                    prev_end + 120, tz=datetime.timezone.utc))
        cursor = start
        end_by = start + datetime.timedelta(hours=max_hours)
        got = 0
        empty_streak = 0
        finished = False
        while cursor < end_by and empty_streak < 60 and not finished:
            try:
                d = api_get(session, f"{FEED}/window/{game_id}",
                            {"startingTime": iso_round10(cursor)})
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 204:
                    d = {}
                else:
                    log.warning("window failed %s: %s", game_id, e)
                    break
            except Exception as e:
                log.warning("window failed %s: %s", game_id, e)
                break
            frames = (d or {}).get("frames") or []
            if not frames:
                empty_streak += 1
                cursor += datetime.timedelta(seconds=100)
                continue
            empty_streak = 0
            for f in frames:
                try:
                    row = parse_frame(f)
                except Exception:
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO ls_frames VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (game_id,) + row)
                if f.get("gameState") == "finished":
                    finished = True
            last = datetime.datetime.fromisoformat(
                frames[-1]["rfc460Timestamp"].replace("Z", "+00:00"))
            cursor = max(cursor + datetime.timedelta(seconds=100),
                         last + datetime.timedelta(seconds=10))
            got += len(frames)
            # commit after each window, not once at the end of the whole
            # game: a game can take many window calls (seconds to over a
            # minute), and holding one write transaction open that whole
            # time is what blocks other --leagues shards under WAL mode
            # (only one writer transaction can be in flight at a time).
            db.commit()
            time.sleep(0.15)
        db.execute("UPDATE ls_games SET frames_done=1 WHERE game_id=?",
                   (game_id,))
        db.commit()
        log.info("game %s: %d frames stored%s", game_id, got,
                 " (finished)" if finished else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/esports.db")
    ap.add_argument("--discover", type=int, metavar="DAYS")
    ap.add_argument("--frames", action="store_true")
    ap.add_argument("--leagues", type=str, default=None,
                    help="comma-separated league codes to restrict --frames to "
                         "(for running several shards in parallel)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    db_path = args.db
    if not os.path.isabs(db_path):
        db_path = os.path.join(BASE_DIR, db_path)
    db = open_db(db_path)
    session = requests.Session()
    leagues = args.leagues.split(",") if args.leagues else None

    def resilient(fn, *fargs, **fkwargs):
        """discover()/fetch_frames() both recompute their to-do list from
        current DB state on each call, so a lock timeout under several
        parallel --leagues shards just means: back off and call it again -
        it resumes exactly where it left off, nothing is lost. Retry
        INDEFINITELY on lock errors (capped backoff) rather than giving up
        after N tries - under heavy multi-shard contention the other shards
        eventually finish or thin out, and there is no failure mode here
        that isn't safely resumable, so giving up was just wasted restarts."""
        attempt = 0
        while True:
            try:
                return fn(*fargs, **fkwargs)
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower():
                    raise
                attempt += 1
                wait = min(10 * attempt, 90)
                log.warning("db locked (attempt %d), retrying in %ds: %s",
                           attempt, wait, e)
                time.sleep(wait)

    if args.discover:
        resilient(discover, db, session, args.discover, leagues=leagues)
    if args.frames:
        resilient(fetch_frames, db, session, leagues=leagues)
    if not args.discover and not args.frames:
        print("use --discover N and/or --frames")


if __name__ == "__main__":
    main()
