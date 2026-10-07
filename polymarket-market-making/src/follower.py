#!/usr/bin/env python3
"""Polymarket wallet follower + paper-trading simulator.

Polls the public data-api activity feed of the configured wallets, stores every
trade they make, and simulates copying each trade at the price WE could have
gotten from the live orderbook at detection time. Tracks simulated positions,
marks them to midpoint, and settles them when markets resolve.

No API keys needed - all endpoints are public. Runs fine on a Raspberry Pi.
"""

import json
import logging
import os
import queue
import sqlite3
import sys
import threading
import time

import requests

try:
    import websocket  # websocket-client
except ImportError:
    websocket = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
LIVE_WS = "wss://ws-live-data.polymarket.com"

log = logging.getLogger("follower")


def load_config():
    with open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def open_db(path):
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS their_trades(
        id TEXT PRIMARY KEY,
        wallet TEXT, name TEXT, ts INTEGER, detected_at INTEGER,
        condition_id TEXT, asset TEXT, side TEXT, outcome TEXT,
        price REAL, size REAL, usdc REAL, title TEXT, slug TEXT
    );
    CREATE TABLE IF NOT EXISTS sim_fills(
        trade_id TEXT PRIMARY KEY,
        wallet TEXT, asset TEXT, condition_id TEXT, side TEXT,
        their_price REAL, sim_price REAL, shares REAL, usdc REAL,
        latency_s INTEGER, best_bid REAL, best_ask REAL,
        status TEXT, filled_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS positions(
        wallet TEXT, asset TEXT,
        condition_id TEXT, title TEXT, outcome TEXT,
        shares REAL DEFAULT 0, cost REAL DEFAULT 0,
        realized REAL DEFAULT 0,
        status TEXT DEFAULT 'OPEN',
        settle_value REAL, mark REAL, mark_ts INTEGER,
        PRIMARY KEY (wallet, asset)
    );
    CREATE INDEX IF NOT EXISTS idx_trades_wallet_ts ON their_trades(wallet, ts);
    """)
    db.commit()
    return db


def norm_ts(ts):
    ts = int(ts or 0)
    return ts // 1000 if ts > 10**12 else ts  # ms -> s if needed


def trade_key(a):
    return "{}-{}-{}-{}-{:.6f}".format(
        a.get("transactionHash"), a.get("asset"), a.get("side"),
        norm_ts(a.get("timestamp")), float(a.get("size") or 0))


class WSListener(threading.Thread):
    """Streams every platform trade from the live-data websocket and pushes
    the ones made by our wallets onto a queue. Reconnects forever."""

    def __init__(self, wallets, out_queue):
        super().__init__(daemon=True)
        self.wallets = {w.lower() for w in wallets}
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
            log.info("websocket connected")
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
            if (p.get("proxyWallet") or "").lower() not in self.wallets:
                return
            # normalize to the same shape the data-api /activity returns
            p["timestamp"] = norm_ts(p.get("timestamp"))
            p.setdefault("usdcSize",
                         float(p.get("size") or 0) * float(p.get("price") or 0))
            p["type"] = "TRADE"
            # capture arrival time NOW, not when the queue eventually drains -
            # otherwise a burst of trades makes the later ones look "late"
            # just because we were still busy processing the earlier ones
            p["_received_at"] = time.time()
            self.q.put(p)

        app = websocket.WebSocketApp(
            LIVE_WS, on_open=on_open, on_message=on_message)
        app.run_forever(ping_interval=5)


def fetch_activity(session, wallet, limit=100):
    r = session.get(
        DATA_API + "/activity",
        params={"user": wallet, "limit": limit, "type": "TRADE"},
        timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_book(session, asset):
    """Returns (bids, asks) as lists of (price, size), best price first."""
    r = session.get(CLOB_API + "/book", params={"token_id": asset}, timeout=15)
    r.raise_for_status()
    d = r.json()
    # API returns levels sorted worst-to-best; reverse so best is first
    bids = [(float(x["price"]), float(x["size"])) for x in reversed(d.get("bids") or [])]
    asks = [(float(x["price"]), float(x["size"])) for x in reversed(d.get("asks") or [])]
    return bids, asks


def vwap_fill_buy(asks, budget_usdc):
    """Walk the asks with a USDC budget. Returns (shares, usdc_spent)."""
    shares = 0.0
    spent = 0.0
    for price, size in asks:
        if spent >= budget_usdc:
            break
        level_usd = price * size
        take = min(budget_usdc - spent, level_usd)
        shares += take / price
        spent += take
    return shares, spent


def vwap_fill_sell(bids, shares_to_sell):
    """Walk the bids selling shares. Returns (shares_sold, usdc_received)."""
    sold = 0.0
    received = 0.0
    for price, size in bids:
        if sold >= shares_to_sell:
            break
        take = min(shares_to_sell - sold, size)
        sold += take
        received += take * price
    return sold, received


class Follower:
    def __init__(self, cfg):
        self.cfg = cfg
        db_path = cfg["db_path"]
        if not os.path.isabs(db_path):
            db_path = os.path.join(BASE_DIR, db_path)
        self.db = open_db(db_path)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "polymarket-follower/1.0 (paper trading research)"
        self.last_mark = 0
        self.last_settle = 0
        self.book_cache = {}  # asset -> (bids, asks, fetched_at) - short TTL, shared across trades
        self.wallet_names = {w.lower(): n for n, w in cfg["wallets"].items()}

    # ---------- trade ingestion ----------

    def poll_wallet(self, name, wallet):
        try:
            acts = fetch_activity(self.session, wallet)
        except Exception as e:
            log.warning("activity fetch failed for %s: %s", name, e)
            return
        now = int(time.time())
        new = []
        for a in acts:
            key = trade_key(a)
            cur = self.db.execute("SELECT 1 FROM their_trades WHERE id=?", (key,))
            if cur.fetchone():
                continue
            self.db.execute(
                "INSERT INTO their_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, wallet, name, norm_ts(a.get("timestamp")), now,
                 a.get("conditionId"), a.get("asset"), a.get("side"),
                 a.get("outcome"), a.get("price"), a.get("size"),
                 a.get("usdcSize"), a.get("title"), a.get("slug")))
            new.append((key, a))
        self.db.commit()
        if new:
            log.info("%s: %d new trade(s)", name, len(new))
        for key, a in sorted(new, key=lambda x: x[1].get("timestamp") or 0):
            self.simulate_copy(key, name, wallet, a, now)
        self.db.commit()

    def get_book(self, asset, max_age=2.0):
        """Cached orderbook lookup - avoids refetching on every trade when a
        wallet fires several trades on the same market within a short burst."""
        cached = self.book_cache.get(asset)
        if cached and (time.time() - cached[2]) < max_age:
            return cached[0], cached[1]
        try:
            bids, asks = fetch_book(self.session, asset)
        except Exception as e:
            log.warning("book fetch failed for %s: %s", asset, e)
            return None, None
        self.book_cache[asset] = (bids, asks, time.time())
        return bids, asks

    def simulate_copy(self, trade_id, name, wallet, a, detected_at):
        asset = a.get("asset")
        side = a.get("side")
        their_price = float(a.get("price") or 0)
        their_usdc = float(a.get("usdcSize") or 0)
        their_size = float(a.get("size") or 0)
        latency = detected_at - (norm_ts(a.get("timestamp")) or detected_at)

        def record(status, sim_price=None, shares=0.0, usdc=0.0, bb=None, ba=None):
            self.db.execute(
                "INSERT OR IGNORE INTO sim_fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (trade_id, wallet, asset, a.get("conditionId"), side,
                 their_price, sim_price, shares, usdc, latency, bb, ba,
                 status, int(time.time())))

        # don't simulate copies of trades we only saw long after the fact
        # (e.g. the backfill on first startup) - the book has moved since
        if latency > self.cfg.get("max_copy_latency_sec", 120):
            record("SKIPPED_STALE")
            return

        budget = min(their_usdc * self.cfg["copy_ratio"], self.cfg["max_usdc_per_trade"])
        if side == "BUY" and budget < self.cfg["min_usdc_per_trade"]:
            record("SKIPPED_TOO_SMALL")
            return

        bids, asks = self.get_book(asset)
        if bids is None:
            record("SKIPPED_NO_BOOK")
            return
        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None

        if side == "BUY":
            if not asks:
                record("SKIPPED_NO_BOOK", bb=best_bid, ba=best_ask)
                return
            shares, spent = vwap_fill_buy(asks, budget)
            if shares <= 0:
                record("SKIPPED_NO_BOOK", bb=best_bid, ba=best_ask)
                return
            sim_price = spent / shares
            record("FILLED", sim_price, shares, spent, best_bid, best_ask)
            self.apply_buy(wallet, asset, a, shares, spent)
            log.info("  copy BUY %-9s %.2f sh @ %.3f (they: %.3f) %s",
                     name, shares, sim_price, their_price,
                     ascii_title(a.get("title")))
        else:  # SELL
            pos = self.db.execute(
                "SELECT shares, cost FROM positions WHERE wallet=? AND asset=? AND status='OPEN'",
                (wallet, asset)).fetchone()
            if not pos or pos[0] <= 0:
                record("SKIPPED_NO_POSITION", bb=best_bid, ba=best_ask)
                return
            want = min(their_size * self.cfg["copy_ratio"], pos[0])
            if not bids:
                record("SKIPPED_NO_BOOK", bb=best_bid, ba=best_ask)
                return
            sold, received = vwap_fill_sell(bids, want)
            if sold <= 0:
                record("SKIPPED_NO_BOOK", bb=best_bid, ba=best_ask)
                return
            sim_price = received / sold
            record("FILLED", sim_price, sold, received, best_bid, best_ask)
            self.apply_sell(wallet, asset, sold, received, pos)
            log.info("  copy SELL %-8s %.2f sh @ %.3f (they: %.3f) %s",
                     name, sold, sim_price, their_price,
                     ascii_title(a.get("title")))

    def apply_buy(self, wallet, asset, a, shares, cost):
        self.db.execute(
            """INSERT INTO positions(wallet, asset, condition_id, title, outcome, shares, cost)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(wallet, asset) DO UPDATE SET
                 shares = shares + excluded.shares,
                 cost = cost + excluded.cost,
                 status = 'OPEN'""",
            (wallet, asset, a.get("conditionId"), a.get("title"),
             a.get("outcome"), shares, cost))

    def apply_sell(self, wallet, asset, sold, received, pos):
        shares, cost = pos
        cost_removed = cost * (sold / shares) if shares > 0 else 0.0
        self.db.execute(
            """UPDATE positions SET
                 shares = shares - ?,
                 cost = cost - ?,
                 realized = realized + ?
               WHERE wallet=? AND asset=?""",
            (sold, cost_removed, received - cost_removed, wallet, asset))

    # ---------- marking & settlement ----------

    def update_marks(self):
        rows = self.db.execute(
            "SELECT DISTINCT asset FROM positions WHERE status='OPEN' AND shares > 0").fetchall()
        for (asset,) in rows:
            try:
                r = self.session.get(CLOB_API + "/midpoint",
                                     params={"token_id": asset}, timeout=15)
                mid = float(r.json().get("mid"))
            except Exception:
                continue
            self.db.execute(
                "UPDATE positions SET mark=?, mark_ts=? WHERE asset=?",
                (mid, int(time.time()), asset))
            time.sleep(0.2)  # be gentle with the API
        self.db.commit()
        if rows:
            log.info("marked %d open asset(s)", len(rows))

    def settle_resolved(self):
        # NOTE: gamma-api's /markets?condition_ids=... filter is deprecated
        # (sunset 2026-05-01) and silently returns []. The CLOB API's
        # per-market endpoint is the live replacement and gives token-level
        # final settlement prices directly.
        rows = self.db.execute(
            """SELECT DISTINCT condition_id FROM positions
               WHERE status='OPEN' AND shares > 0 AND condition_id IS NOT NULL""").fetchall()
        for (cid,) in rows:
            try:
                r = self.session.get(CLOB_API + "/markets/" + cid, timeout=15)
                if r.status_code != 200:
                    continue
                m = r.json()
            except Exception as e:
                log.warning("clob market fetch failed for %s: %s", cid, e)
                continue
            if not m.get("closed"):
                continue
            for tok in m.get("tokens") or []:
                token_id = tok.get("token_id")
                final = float(tok.get("price") or 0)
                for wallet, shares, cost in self.db.execute(
                        """SELECT wallet, shares, cost FROM positions
                           WHERE asset=? AND status='OPEN' AND shares > 0""",
                        (token_id,)).fetchall():
                    payout = shares * final
                    self.db.execute(
                        """UPDATE positions SET
                             realized = realized + ?,
                             shares = 0, cost = 0,
                             status = 'SETTLED', settle_value = ?
                           WHERE wallet=? AND asset=?""",
                        (payout - cost, final, wallet, token_id))
                    log.info("settled %s @ %.2f: pnl %+.2f USDC (%s)",
                             token_id[:10], final, payout - cost,
                             ascii_title(m.get("question")))
            time.sleep(0.15)
        self.db.commit()

    # ---------- main loop ----------

    def ingest_ws_trade(self, a):
        """A single trade arriving live from the websocket."""
        wallet = (a.get("proxyWallet") or "").lower()
        name = self.wallet_names.get(wallet, wallet[:10])
        key = trade_key(a)
        if self.db.execute("SELECT 1 FROM their_trades WHERE id=?", (key,)).fetchone():
            return
        # use the websocket-thread arrival timestamp for latency, not now -
        # this call may run well after receipt if the queue had a backlog
        received_at = int(a.get("_received_at") or time.time())
        now = int(time.time())
        self.db.execute(
            "INSERT INTO their_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, wallet, name, norm_ts(a.get("timestamp")), now,
             a.get("conditionId"), a.get("asset"), a.get("side"),
             a.get("outcome"), a.get("price"), a.get("size"),
             a.get("usdcSize"), a.get("title"), a.get("slug")))
        self.simulate_copy(key, name, wallet, a, received_at)
        self.db.commit()

    def run(self):
        log.info("following %d wallet(s): %s",
                 len(self.cfg["wallets"]), ", ".join(self.cfg["wallets"]))

        ws_queue = queue.Queue()
        ws_on = bool(self.cfg.get("live_ws", True)) and websocket is not None
        if ws_on:
            WSListener(self.cfg["wallets"].values(), ws_queue).start()
            poll_interval = self.cfg.get("ws_backup_poll_sec", 300)
            log.info("live websocket mode (backup poll every %ds)", poll_interval)
        else:
            poll_interval = self.cfg["poll_sec"]
            if self.cfg.get("live_ws", True):
                log.warning("websocket-client not installed - polling mode "
                            "(pip install websocket-client for live mode)")

        last_poll = 0.0
        while True:
            try:
                item = ws_queue.get(timeout=1)
                self.ingest_ws_trade(item)
                continue  # drain the queue before doing periodic work
            except queue.Empty:
                pass
            now = time.time()
            if now - last_poll >= poll_interval:
                for name, wallet in self.cfg["wallets"].items():
                    self.poll_wallet(name, wallet)
                last_poll = now
            if now - self.last_mark >= self.cfg["mark_every_sec"]:
                self.update_marks()
                self.last_mark = now
            if now - self.last_settle >= self.cfg["settle_every_sec"]:
                self.settle_resolved()
                self.last_settle = now


def ascii_title(t):
    return (t or "").encode("ascii", "replace").decode()[:60]


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    cfg = load_config()
    if "--once" in sys.argv:
        f = Follower(cfg)
        for name, wallet in cfg["wallets"].items():
            f.poll_wallet(name, wallet)
        f.update_marks()
        f.settle_resolved()
        return
    while True:
        try:
            Follower(cfg).run()
        except KeyboardInterrupt:
            log.info("stopped")
            return
        except Exception:
            log.exception("crashed, restarting in 60s")
            time.sleep(60)


if __name__ == "__main__":
    main()
