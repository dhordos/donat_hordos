#!/usr/bin/env python3
"""Live data collector for Polymarket LoL/esports match markets.

Discovers "LoL: X vs Y" match events via the Gamma API, then:
  - records every trade on ALL markets of those events (live websocket),
  - snapshots best bid/ask for the "winner" markets (Match Winner,
    Game N Winner) so we know real spreads/liquidity during matches.

Purpose: measure in-play odds swings and whether a legging strategy
(back favorite early, hedge the other side when odds move) would have been
executable at real prices. Paper data collection only - no orders.

Runs 24/7 on a Raspberry Pi, own DB (esports.db).
"""

import json
import logging
import os
import queue
import sqlite3
import time

import requests
import websocket  # websocket-client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLOB_API = "https://clob.polymarket.com"
LIVE_WS = "wss://ws-live-data.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"  # events endpoint is NOT deprecated
                                                # (only /markets?condition_ids= was)
log = logging.getLogger("esports")


def norm_ts(ts):
    ts = int(ts or 0)
    return ts // 1000 if ts > 10**12 else ts  # ms -> s if needed

TAG = "league-of-legends"
TITLE_PREFIX = "LoL:"
DISCOVER_SEC = 600
BOOK_HOT_SEC = 60      # snapshot cadence for events with recent trades
BOOK_COLD_SEC = 600    # baseline snapshot cadence for all active events
HOT_WINDOW_SEC = 1800  # an event is "hot" if it traded in the last 30 min


def open_db(path):
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS events(
        slug TEXT PRIMARY KEY,
        title TEXT, discovered_at INTEGER, closed INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS markets(
        condition_id TEXT PRIMARY KEY,
        event_slug TEXT, question TEXT, group_title TEXT,
        token0 TEXT, outcome0 TEXT, token1 TEXT, outcome1 TEXT,
        game_start_time TEXT, core INTEGER DEFAULT 0,
        closed INTEGER DEFAULT 0, final0 REAL, final1 REAL
    );
    CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, condition_id TEXT, asset TEXT, side TEXT,
        price REAL, size REAL, wallet TEXT
    );
    CREATE TABLE IF NOT EXISTS books(
        ts INTEGER, asset TEXT,
        best_bid REAL, best_ask REAL, bid_size REAL, ask_size REAL
    );
    CREATE INDEX IF NOT EXISTS idx_trades_cid_ts ON trades(condition_id, ts);
    CREATE INDEX IF NOT EXISTS idx_books_asset_ts ON books(asset, ts);
    """)
    db.commit()
    return db


def is_core(group_title):
    g = (group_title or "").lower()
    return "winner" in g


class StreamListener(__import__("threading").Thread):
    def __init__(self, tracked_cids, out_queue):
        super().__init__(daemon=True)
        self.tracked = tracked_cids
        self.q = out_queue

    def run(self):
        while True:
            try:
                self.listen()
            except Exception as e:
                log.warning("websocket error: %s - reconnecting in 5s", e)
            time.sleep(5)

    def listen(self):
        def on_open(ws):
            log.info("esports stream connected")
            ws.send(json.dumps({
                "action": "subscribe",
                "subscriptions": [
                    {"topic": "activity", "type": "trades", "filters": ""}]}))

        def on_message(ws, msg):
            try:
                d = json.loads(msg)
            except ValueError:
                return
            if d.get("topic") != "activity" or d.get("type") != "trades":
                return
            p = d.get("payload") or {}
            if p.get("conditionId") in self.tracked:
                self.q.put(p)

        app = websocket.WebSocketApp(
            LIVE_WS, on_open=on_open, on_message=on_message)
        app.run_forever(ping_interval=5)


class Collector:
    def __init__(self):
        self.db = open_db(os.path.join(BASE_DIR, "esports.db"))
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "polymarket-esports-collector/1.0 (research)"
        self.tracked = set()       # all condition_ids of open LoL events
        self.core_tokens = {}      # condition_id -> [token0, token1] for book snapshots
        self.event_of = {}         # condition_id -> event_slug
        self.last_trade_at = {}    # event_slug -> unix ts of last seen trade
        self.last_discover = 0.0
        self.last_hot_books = 0.0
        self.last_cold_books = 0.0
        self.load_state()

    def load_state(self):
        for cid, slug, t0, t1, core in self.db.execute(
                """SELECT m.condition_id, m.event_slug, m.token0, m.token1, m.core
                   FROM markets m JOIN events e ON e.slug = m.event_slug
                   WHERE e.closed = 0 AND m.closed = 0""").fetchall():
            self.tracked.add(cid)
            self.event_of[cid] = slug
            if core:
                self.core_tokens[cid] = [t0, t1]
        if self.tracked:
            log.info("resumed %d open market(s)", len(self.tracked))

    # ---------- discovery ----------

    def discover(self):
        try:
            r = self.session.get(
                GAMMA_API + "/events",
                params={"tag_slug": TAG, "closed": "false", "limit": 100},
                timeout=20)
            events = r.json()
        except Exception as e:
            log.warning("discovery failed: %s", e)
            return
        for e in events:
            title = e.get("title") or ""
            slug = e.get("slug")
            if not slug or not title.startswith(TITLE_PREFIX):
                continue
            known = self.db.execute(
                "SELECT 1 FROM events WHERE slug=?", (slug,)).fetchone()
            if not known:
                self.db.execute(
                    "INSERT INTO events(slug, title, discovered_at) VALUES (?,?,?)",
                    (slug, title, int(time.time())))
                log.info("new event: %s", title.encode('ascii', 'replace').decode())
            for m in e.get("markets") or []:
                cid = m.get("conditionId")
                if not cid or cid in self.tracked:
                    continue
                try:
                    toks = json.loads(m.get("clobTokenIds") or "[]")
                    outs = json.loads(m.get("outcomes") or "[]")
                except ValueError:
                    continue
                if len(toks) != 2:
                    continue
                core = 1 if is_core(m.get("groupItemTitle")) else 0
                self.db.execute(
                    """INSERT OR IGNORE INTO markets
                       (condition_id, event_slug, question, group_title,
                        token0, outcome0, token1, outcome1, game_start_time, core)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (cid, slug, m.get("question"), m.get("groupItemTitle"),
                     toks[0], outs[0] if len(outs) > 0 else None,
                     toks[1], outs[1] if len(outs) > 1 else None,
                     m.get("gameStartTime"), core))
                self.tracked.add(cid)
                self.event_of[cid] = slug
                if core:
                    self.core_tokens[cid] = toks
        self.db.commit()

    # ---------- trades ----------

    def record_trade(self, p):
        cid = p.get("conditionId")
        self.db.execute(
            "INSERT INTO trades(ts, condition_id, asset, side, price, size, wallet)"
            " VALUES (?,?,?,?,?,?,?)",
            (norm_ts(p.get("timestamp")) or int(time.time()), cid,
             p.get("asset"), p.get("side"), p.get("price"), p.get("size"),
             p.get("proxyWallet")))
        self.db.commit()
        slug = self.event_of.get(cid)
        if slug:
            self.last_trade_at[slug] = time.time()

    # ---------- book snapshots ----------

    def snapshot_books(self, only_hot):
        now = time.time()
        token_batch = []
        for cid, toks in self.core_tokens.items():
            slug = self.event_of.get(cid)
            if only_hot and now - self.last_trade_at.get(slug, 0) > HOT_WINDOW_SEC:
                continue
            token_batch.extend(toks)
        if not token_batch:
            return
        ts = int(now)
        for i in range(0, len(token_batch), 20):
            chunk = token_batch[i:i + 20]
            try:
                r = self.session.post(
                    CLOB_API + "/books",
                    json=[{"token_id": t} for t in chunk], timeout=20)
                books = r.json()
            except Exception as e:
                log.warning("books fetch failed: %s", e)
                continue
            for b in books:
                bids = b.get("bids") or []
                asks = b.get("asks") or []
                bb = bids[-1] if bids else None
                ba = asks[-1] if asks else None
                self.db.execute(
                    "INSERT INTO books VALUES (?,?,?,?,?,?)",
                    (ts, b.get("asset_id"),
                     float(bb["price"]) if bb else None,
                     float(ba["price"]) if ba else None,
                     float(bb["size"]) if bb else None,
                     float(ba["size"]) if ba else None))
            time.sleep(0.2)
        self.db.commit()

    # ---------- closure ----------

    def mark_closed(self):
        open_slugs = [r[0] for r in self.db.execute(
            "SELECT slug FROM events WHERE closed=0").fetchall()]
        for slug in open_slugs:
            try:
                r = self.session.get(GAMMA_API + "/events",
                                     params={"slug": slug}, timeout=15)
                ev = r.json()
            except Exception:
                continue
            if not ev or not ev[0].get("closed"):
                continue
            self.db.execute("UPDATE events SET closed=1 WHERE slug=?", (slug,))
            for m in ev[0].get("markets") or []:
                cid = m.get("conditionId")
                try:
                    prices = json.loads(m.get("outcomePrices") or "[]")
                except ValueError:
                    prices = []
                self.db.execute(
                    "UPDATE markets SET closed=1, final0=?, final1=? WHERE condition_id=?",
                    (float(prices[0]) if len(prices) > 0 else None,
                     float(prices[1]) if len(prices) > 1 else None, cid))
                self.tracked.discard(cid)
                self.core_tokens.pop(cid, None)
            log.info("event closed: %s", slug)
            time.sleep(0.2)
        self.db.commit()

    # ---------- main loop ----------

    def run(self):
        log.info("esports collector: tag=%s, prefix=%r", TAG, TITLE_PREFIX)
        q = queue.Queue()
        StreamListener(self.tracked, q).start()
        last_closed_check = 0.0
        while True:
            try:
                p = q.get(timeout=1)
                self.record_trade(p)
                continue
            except queue.Empty:
                pass
            now = time.time()
            if now - self.last_discover >= DISCOVER_SEC:
                self.discover()
                self.last_discover = now
            if now - self.last_hot_books >= BOOK_HOT_SEC:
                self.snapshot_books(only_hot=True)
                self.last_hot_books = now
            if now - self.last_cold_books >= BOOK_COLD_SEC:
                self.snapshot_books(only_hot=False)
                self.last_cold_books = now
            if now - last_closed_check >= 1800:
                self.mark_closed()
                last_closed_check = now


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    while True:
        try:
            Collector().run()
        except KeyboardInterrupt:
            log.info("stopped")
            return
        except Exception:
            log.exception("crashed, restarting in 60s")
            time.sleep(60)


if __name__ == "__main__":
    main()
