#!/usr/bin/env python3
"""Thermometer-watcher PAPER bot - live forward test.

IDEA. Polymarket temperature markets resolve on Weather Underground's
'Daily Observations' table, i.e. the raw METAR stream of one named station
(NYC -> KLGA, Tokyo -> RJTT, ...). That stream is public in real time, and
by early afternoon the day's maximum is largely decided: at LaGuardia the
max was already reached by 14h on 58% of summer days, by 15h on 80%, and
was within 2F on 92% by 14h. So this is not forecasting - it is reading the
settlement source faster than the book updates.

Our failed weather model tested DAY-AHEAD ensemble forecasting. This is the
intraday game, which we never tested.

PRE-REGISTERED RULE (frozen 2026-08-07, do not tweak mid-test):
  - Universe: open "highest temperature" markets whose city is in
    build_upside_dist.STATIONS and NOT in UNVERIFIED, target date == today
    in the station's local timezone, local hour >= 13.
  - Fair value: running max m from today's METAR at that station, plus the
    empirical distribution of (final_max - running_max) for that station
    and local hour (3 years of history). Bucket probability =
    P(final <= hi_edge) - P(final <= lo_edge), edges at +-0.5 degree since
    settlement is on whole degrees.
  - Fee-aware: Polymarket taker fee = 0.05 * price * (1 - price) per share.
    edge = p_model - ask - fee. Buying either side (Yes or No) is allowed.
  - Entry: first scan where edge >= 0.05 and ask depth >= $2 notional.
    Paper-buy at the ask, stake = min(depth * ask, $50). One bet per market.
    Hold to resolution.
  - Gate: n >= 30 settled AND ROI > 0 AND t >= 2.5.

The skips table is half the point: it records how often the book was
ALREADY fair when we looked. If stale quotes never appear, that is the
answer and it costs nothing to learn.

  python watcher_paper.py            # run loop
  python watcher_paper.py --report   # status
"""

import argparse
import bisect
import datetime
import json
import logging
import os
import sqlite3
import statistics
import time
import zoneinfo

import requests

from build_upside_dist import STATIONS, UNVERIFIED
from weather_model import parse_question, target_date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAPER_DB = os.path.join(BASE_DIR, "watcher_paper.db")
DIST = os.path.join(BASE_DIR, "upside_dist.json")
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
IEM = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

MIN_EDGE = 0.05
MIN_HOUR = 13
STAKE = 50.0
POLL_SEC = 600
FEE_RATE = 0.05
log = logging.getLogger("watcher")


def fee_per_share(price):
    return FEE_RATE * price * (1 - price)


def open_paper_db():
    db = sqlite3.connect(PAPER_DB)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS bets(
        condition_id TEXT PRIMARY KEY,
        ts INTEGER, title TEXT, league TEXT,
        token TEXT, name TEXT,
        ask REAL, depth REAL, stake REAL, shares REAL,
        p_model REAL, running_max REAL, local_hour INTEGER, station TEXT,
        final REAL, settled_ts INTEGER
    );
    CREATE TABLE IF NOT EXISTS skips(
        condition_id TEXT PRIMARY KEY, reason TEXT, ts INTEGER,
        best_edge REAL, p_model REAL, ask REAL
    );
    """)
    db.commit()
    return db


class Obs:
    """Today's running max per station, cached for one scan cycle."""

    def __init__(self):
        self.cache = {}

    def running_max(self, station, tz, unit):
        key = (station, unit, int(time.time()) // 900)   # 15-min cache
        if key in self.cache:
            return self.cache[key]
        now_local = datetime.datetime.now(zoneinfo.ZoneInfo(tz))
        d = now_local.date()
        col = "tmpf" if unit == "F" else "tmpc"
        try:
            r = requests.get(IEM, params={
                "station": station, "data": col,
                "year1": d.year, "month1": d.month, "day1": d.day,
                "year2": d.year, "month2": d.month, "day2": d.day,
                "tz": tz, "format": "onlycomma", "latlon": "no",
                "missing": "M", "trace": "T", "report_type": "3"}, timeout=30)
            r.raise_for_status()
        except Exception as e:
            log.debug("obs fetch failed %s: %s", station, e)
            self.cache[key] = (None, None)
            return None, None
        vals = []
        for line in r.text.splitlines()[1:]:
            p = line.split(",")
            if len(p) == 3 and p[2] not in ("M", ""):
                try:
                    vals.append(float(p[2]))
                except ValueError:
                    pass
        out = (max(vals), now_local.hour) if vals else (None, None)
        self.cache[key] = out
        return out


def p_upside_le(q, x):
    """P(upside <= x) from the sorted empirical sample."""
    return bisect.bisect_right(q, x) / len(q)


def bucket_prob(dist, station, hour, unit, running_max, lo, hi, cond):
    rec = dist.get(station)
    if not rec:
        return None
    hours = rec["hours"]
    q = None
    for h in (hour, hour - 1, hour + 1):
        if str(h) in hours:
            q = hours[str(h)]["q"]
            break
    if not q:
        return None
    if unit == "C":                      # stored upsides are in F
        q = [v * 5.0 / 9.0 for v in q]
    if cond == "gte":                    # "X or higher"
        return 1.0 - p_upside_le(q, (lo - 0.5) - running_max)
    if cond == "lte":                    # "X or lower"
        return p_upside_le(q, (hi + 0.5) - running_max)
    return (p_upside_le(q, (hi + 0.5) - running_max)
            - p_upside_le(q, (lo - 0.5) - running_max))


def get_book(session, token):
    r = session.get(CLOB + "/book", params={"token_id": token}, timeout=15)
    r.raise_for_status()
    d = r.json()
    bids, asks = d.get("bids") or [], d.get("asks") or []
    return (float(asks[-1]["price"]) if asks else None,
            float(asks[-1]["size"]) if asks else 0.0,
            float(bids[-1]["price"]) if bids else None)


def scan_once(pdb, session, dist, obs):
    entered = 0
    for offset in range(0, 1500, 100):
        try:
            batch = session.get(GAMMA + "/markets", params={
                "closed": "false", "limit": 100, "offset": offset,
                "order": "volume24hr", "ascending": "false"}, timeout=30).json()
        except Exception:
            break
        if not batch:
            break
        for m in batch:
            cid = m.get("conditionId")
            q = m.get("question") or ""
            parsed = parse_question(q)
            if not cid or not parsed or parsed["metric"] != "highest":
                continue
            city = parsed["city"]
            if city not in STATIONS or city in UNVERIFIED:
                continue
            if pdb.execute("SELECT 1 FROM bets WHERE condition_id=?",
                           (cid,)).fetchone():
                continue
            station, tz = STATIONS[city]
            tgt = target_date(parsed["month"], parsed["day"])
            now_local = datetime.datetime.now(zoneinfo.ZoneInfo(tz))
            if now_local.date() != tgt or now_local.hour < MIN_HOUR:
                continue
            rm, hour = obs.running_max(station, tz, parsed["unit"])
            if rm is None:
                continue
            p = bucket_prob(dist, station, hour, parsed["unit"], rm,
                            parsed["lo"], parsed["hi"], parsed["cond"])
            if p is None:
                continue
            try:
                toks = json.loads(m.get("clobTokenIds") or "[]")
                outs = json.loads(m.get("outcomes") or "[]")
            except ValueError:
                continue
            if len(toks) != 2:
                continue
            best = None
            for tok, name in zip(toks, outs):
                p_side = p if name == "Yes" else 1 - p
                try:
                    ask, depth, _ = get_book(session, tok)
                except Exception:
                    continue
                if ask is None or depth <= 0:
                    continue
                edge = p_side - ask - fee_per_share(ask)
                if best is None or edge > best[0]:
                    best = (edge, tok, name, ask, depth, p_side)
                time.sleep(0.15)
            if best is None:
                continue
            edge, tok, name, ask, depth, p_side = best
            if edge < MIN_EDGE or ask * depth < 2.0:
                pdb.execute(
                    "INSERT OR REPLACE INTO skips VALUES (?,?,?,?,?,?)",
                    (cid, "no_edge" if edge < MIN_EDGE else "thin",
                     int(time.time()), round(edge, 4), round(p_side, 4), ask))
                continue
            stake = min(depth * ask, STAKE)
            if stake < 2:
                continue
            shares = stake / ask
            pdb.execute(
                """INSERT INTO bets (condition_id, ts, title, league, token,
                   name, ask, depth, stake, shares, p_model, running_max,
                   local_hour, station)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, int(time.time()), q, city, tok, name, ask, depth,
                 stake, shares, p_side, rm, hour, station))
            entered += 1
            log.info("PAPER BET %s @ %.3f (model %.3f, edge %+.3f, "
                     "max %.1f at %02dh %s) - %s",
                     name, ask, p_side, edge, rm, hour, station,
                     q.encode("ascii", "replace").decode()[:60])
        time.sleep(0.3)
    pdb.commit()
    return entered


def settle(pdb, session):
    for (cid,) in pdb.execute(
            "SELECT condition_id FROM bets WHERE final IS NULL").fetchall():
        try:
            r = session.get(CLOB + "/markets/" + cid, timeout=15)
            if r.status_code != 200:
                continue
            mk = r.json()
        except Exception:
            continue
        if not mk.get("closed"):
            continue
        tok = pdb.execute("SELECT token FROM bets WHERE condition_id=?",
                          (cid,)).fetchone()[0]
        final = None
        for t in mk.get("tokens") or []:
            if t.get("token_id") == tok:
                final = float(t.get("price") or 0)
        if final is None:
            continue
        pdb.execute("UPDATE bets SET final=?, settled_ts=? WHERE condition_id=?",
                    (final, int(time.time()), cid))
        log.info("settled %s -> %.1f", cid[:10], final)
        time.sleep(0.2)
    pdb.commit()


def report(pdb):
    rows = pdb.execute("""SELECT league, ask, stake, shares, final, p_model,
                          local_hour FROM bets ORDER BY ts""").fetchall()
    done = [r for r in rows if r[4] is not None]
    print("=" * 72)
    print("THERMOMETER-WATCHER PAPER BOT")
    print("=" * 72)
    print(f"bets: {len(rows)} | open: {len(rows)-len(done)} | settled: {len(done)}")
    n_skip = pdb.execute("SELECT COUNT(*) FROM skips").fetchone()[0]
    print(f"skips logged: {n_skip} (markets where the book was already fair)")
    if done:
        n = len(done)
        wr = statistics.mean(1 if f > 0.5 else 0 for *_, f, _, _ in
                             [(r[0], r[1], r[2], r[3], r[4], r[5], r[6])
                              for r in done])
        avg_ask = statistics.mean(r[1] for r in done)
        pnl = sum(r[3] * r[4] - r[2] for r in done)
        staked = sum(r[2] for r in done)
        se = (wr * (1 - wr) / n) ** 0.5 if 0 < wr < 1 else 0
        roi = [r[4] - r[1] for r in done]
        m = statistics.mean(roi)
        s = statistics.stdev(roi) / (n ** 0.5) if n > 1 else 1e9
        print(f"win rate {wr:.3f} vs avg ask {avg_ask:.3f} | "
              f"model avg {statistics.mean(r[5] for r in done):.3f}")
        print(f"PnL {pnl:+.2f} USDC on {staked:.0f} staked "
              f"({100*pnl/staked:+.1f}%) | ROI/bet {m:+.4f} t={m/s:+.2f}")
        print(f"GATE: n={n}/30, ROI>0 and t>=2.5: "
              f"{'YES' if n >= 30 and m > 0 and m/s >= 2.5 else 'not yet'}")
    print("=" * 72)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    pdb = open_paper_db()
    if args.report:
        report(pdb)
        return
    dist = json.load(open(DIST, encoding="utf-8"))
    log.info("watcher: %d stations, min edge %.2f, from %02dh local",
             len(dist), MIN_EDGE, MIN_HOUR)
    session = requests.Session()
    session.headers["User-Agent"] = "watcher-paper/1.0 (research)"
    obs = Obs()
    last_settle = 0.0
    while True:
        try:
            n = scan_once(pdb, session, dist, obs)
            if n:
                log.info("%d new paper bet(s)", n)
            if time.time() - last_settle > 1800:
                settle(pdb, session)
                last_settle = time.time()
        except Exception:
            log.exception("scan failed, continuing")
        if args.once:
            break
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
