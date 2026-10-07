#!/usr/bin/env python3
"""Calibration study: does the market price match reality?

Question: of the markets the crowd priced at ~10c, how many actually happened?
If fewer than 10% did, longshots are systematically overpriced (the classic
favorite-longshot bias) - and that is a tradable, model-free edge.

Data source: follower.db, which already holds ~128k observed trades plus the
final settlement value of every token our copy-sim held to expiry. No API
calls needed, so this runs fully offline.

Usage:  python3 weather_calibration.py [--all-markets]
        (default: weather/temperature markets only)
"""

import argparse
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Bucket edges - finer at the extremes, where the bias is expected to live.
BUCKETS = [(0.00, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20),
           (0.20, 0.30), (0.30, 0.40), (0.40, 0.50), (0.50, 0.60),
           (0.60, 0.70), (0.70, 0.80), (0.80, 0.85), (0.85, 0.90),
           (0.90, 0.95), (0.95, 1.00)]


def bucket_of(price):
    for lo, hi in BUCKETS:
        if lo <= price < hi:
            return (lo, hi)
    return (0.95, 1.00) if price >= 0.95 else None


def load_observations(db, weather_only):
    """Returns list of (price, won, shares, asset, ts) for settled tokens.

    Every trade printed on a token whose outcome we know becomes one
    observation: the market said `price`, reality said `won`.
    """
    # settle_value is the token's final price: 1.0 = this outcome happened
    settled = {}
    for asset, sv, title in db.execute(
            """SELECT asset, settle_value, title FROM positions
               WHERE status='SETTLED' AND settle_value IS NOT NULL"""):
        if weather_only and "temperature" not in (title or "").lower():
            continue
        settled[asset] = 1 if sv > 0.5 else 0
    if not settled:
        return []

    obs = []
    for asset, price, size, ts in db.execute(
            """SELECT asset, price, size, ts FROM their_trades
               WHERE price IS NOT NULL"""):
        won = settled.get(asset)
        if won is None or not price:
            continue
        if price <= 0.0 or price >= 1.0:
            continue  # already-resolved prints carry no information
        obs.append((float(price), won, float(size or 0), asset, ts))
    return obs


def report_table(title, rows):
    print()
    print(title)
    print("  {:<12} {:>8} {:>10} {:>10} {:>10} {:>9}".format(
        "price band", "n", "avg price", "actual", "diff", "EV/$1"))
    print("  " + "-" * 64)
    for label, n, avg_price, win_rate in rows:
        if n == 0:
            continue
        diff = win_rate - avg_price
        ev = (win_rate / avg_price - 1) if avg_price > 0 else 0
        print("  {:<12} {:>8} {:>10.4f} {:>10.4f} {:>+10.4f} {:>+8.1f}%".format(
            label, n, avg_price, win_rate, diff, 100 * ev))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all-markets", action="store_true",
                    help="include non-weather markets too")
    ap.add_argument("--db", default="follower.db")
    args = ap.parse_args()

    db_path = args.db
    if not os.path.isabs(db_path):
        db_path = os.path.join(BASE_DIR, db_path)
    db = sqlite3.connect(db_path)

    obs = load_observations(db, weather_only=not args.all_markets)
    print("=" * 74)
    print("CALIBRATION STUDY - {} markets".format(
        "ALL" if args.all_markets else "WEATHER"))
    print("=" * 74)
    if not obs:
        print("No usable data (no settled positions in follower.db).")
        return
    tokens = {a for _, _, _, a, _ in obs}
    print("observed trades: {}  |  settled tokens: {}".format(len(obs), len(tokens)))
    print()
    print("How to read: if 'actual' is BELOW 'avg price', the market OVERPRICED")
    print("this band - buying there loses money, selling there makes money.")
    print("EV/$1 = return on one dollar spent buying in that band.")

    # --- view 1: every trade counts once (where the money actually traded) ---
    rows = []
    for lo, hi in BUCKETS:
        sel = [o for o in obs if lo <= o[0] < hi or (hi == 1.0 and o[0] >= lo)]
        if not sel:
            rows.append(("{:.2f}-{:.2f}".format(lo, hi), 0, 0, 0))
            continue
        n = len(sel)
        avg_price = sum(o[0] for o in sel) / n
        win_rate = sum(o[1] for o in sel) / n
        rows.append(("{:.2f}-{:.2f}".format(lo, hi), n, avg_price, win_rate))
    report_table("A) TRADE-LEVEL view (every trade = one observation):", rows)

    # --- view 2: one observation per token, priced at its EARLIEST observed
    #     trade. Using a token's *average* price would be a look-ahead trap:
    #     a price path converging to 1 has a high mean precisely because it
    #     won, which manufactures a huge fake "edge". The first price is a
    #     genuine ex-ante estimate. ---
    per_token = {}
    for price, won, size, asset, ts in obs:
        cur = per_token.get(asset)
        if cur is None or ts < cur[0]:
            per_token[asset] = (ts, price, won)
    token_obs = [(price, won) for _ts, price, won in per_token.values()]
    rows = []
    for lo, hi in BUCKETS:
        sel = [t for t in token_obs if lo <= t[0] < hi or (hi == 1.0 and t[0] >= lo)]
        if not sel:
            rows.append(("{:.2f}-{:.2f}".format(lo, hi), 0, 0, 0))
            continue
        n = len(sel)
        avg_price = sum(t[0] for t in sel) / n
        win_rate = sum(t[1] for t in sel) / n
        rows.append(("{:.2f}-{:.2f}".format(lo, hi), n, avg_price, win_rate))
    report_table("B) MARKET-LEVEL view (one obs/token, at its FIRST seen price):", rows)

    # --- view 3: is the market worse at pricing early in a market's life?
    #     If early prices are poorly calibrated, entering at market open has
    #     value; if they are as sharp as late prices, being first buys nothing. ---
    span = {}
    for price, won, size, asset, ts in obs:
        lo_ts, hi_ts = span.get(asset, (ts, ts))
        span[asset] = (min(lo_ts, ts), max(hi_ts, ts))
    segments = {"early (first 1/3)": [], "mid": [], "late (last 1/3)": []}
    for price, won, size, asset, ts in obs:
        lo_ts, hi_ts = span[asset]
        if hi_ts <= lo_ts:
            continue
        rel = (ts - lo_ts) / (hi_ts - lo_ts)
        key = ("early (first 1/3)" if rel < 1 / 3
               else "mid" if rel < 2 / 3 else "late (last 1/3)")
        segments[key].append((price, won))
    print()
    print("C) CALIBRATION BY AGE (does the market price better as it matures?):")
    print("  {:<20} {:>8} {:>11} {:>11} {:>10}".format(
        "segment", "n", "mean |err|", "avg price", "actual"))
    print("  " + "-" * 64)
    for key in ("early (first 1/3)", "mid", "late (last 1/3)"):
        sel = segments[key]
        if not sel:
            continue
        n = len(sel)
        avg_price = sum(p for p, _ in sel) / n
        win_rate = sum(w for _, w in sel) / n
        # calibration error measured per price band, then averaged by band size
        err_num = err_den = 0.0
        for lo, hi in BUCKETS:
            band = [t for t in sel if lo <= t[0] < hi or (hi == 1.0 and t[0] >= lo)]
            if not band:
                continue
            bp = sum(p for p, _ in band) / len(band)
            bw = sum(w for _, w in band) / len(band)
            err_num += abs(bw - bp) * len(band)
            err_den += len(band)
        mean_err = err_num / err_den if err_den else 0
        print("  {:<20} {:>8} {:>11.4f} {:>11.4f} {:>10.4f}".format(
            key, n, mean_err, avg_price, win_rate))
    print("  (mean |err| = average gap between price and reality; bigger = more")
    print("   mispriced, so a LARGE early value would justify entering at open)")

    # --- headline summary: is there a longshot bias at all? ---
    longshot = [o for o in obs if o[0] < 0.20]
    favorite = [o for o in obs if o[0] > 0.80]
    print()
    print("SUMMARY:")
    for name, sel in (("longshot (<0.20)", longshot), ("favorite (>0.80)", favorite)):
        if not sel:
            continue
        n = len(sel)
        avg_price = sum(o[0] for o in sel) / n
        win_rate = sum(o[1] for o in sel) / n
        ev = (win_rate / avg_price - 1) if avg_price > 0 else 0
        print("  {:<18} {:>7} trades | priced {:.3f} vs actual {:.3f} | buy EV {:+.1f}%".format(
            name, n, avg_price, win_rate, 100 * ev))
    print()
    print("Caveats: (1) this sample only covers markets TraderA traded, so it is")
    print("not fully unbiased. (2) One token contributes many trades that share a")
    print("single outcome, so observations are correlated - the effective sample")
    print("size is far below n, and small deviations (1-3%) are within noise.")
    print("=" * 74)


if __name__ == "__main__":
    main()
