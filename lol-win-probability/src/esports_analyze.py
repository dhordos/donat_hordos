#!/usr/bin/env python3
"""Legging-opportunity analysis on collected/backfilled LoL match data.

For every closed Match Winner market with price history for both tokens:

  Strategy A ("back the favorite, hedge on comeback"):
    - enter: buy 1 share of the pre-match favorite at its price p0
    - hedge: buy 1 share of the OTHER side at its cheapest price AFTER entry
      (its price is lowest exactly when the favorite is surging)
    - if p0 + q_min < 1.00 -> locked, riskless profit of (1 - p0 - q_min)

  Strategy B: same but entering on the underdog.

  Also reports what holding the entry WITHOUT hedging would have made,
  and basic volatility stats (in-play price range of the favorite).

Entry price = first price of the final 12h window of the series (pre-match
line); "in-play" = that window. Printed-price series has no spread, so
results are mildly optimistic - treat as upper bound.

Usage: python esports_analyze.py [--min-points 100]
"""

import argparse
import os
import sqlite3
import statistics

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WINDOW_SEC = 12 * 3600


def series(db, asset):
    return db.execute(
        "SELECT ts, price FROM price_history WHERE asset=? ORDER BY ts",
        (asset,)).fetchall()


def analyze_market(db, cid, t0, o0, t1, o1, f0, f1, min_points):
    s0 = series(db, t0)
    s1 = series(db, t1)
    if len(s0) < min_points or len(s1) < min_points:
        return None
    end_ts = max(s0[-1][0], s1[-1][0])
    win_start = end_ts - WINDOW_SEC
    w0 = [(t, p) for t, p in s0 if t >= win_start]
    w1 = [(t, p) for t, p in s1 if t >= win_start]
    if len(w0) < 10 or len(w1) < 10:
        return None

    p0_side0, p0_side1 = w0[0][1], w1[0][1]
    # favorite = higher pre-match price
    if p0_side0 >= p0_side1:
        fav, dog = (w0, p0_side0, o0, f0), (w1, p0_side1, o1, f1)
    else:
        fav, dog = (w1, p0_side1, o1, f1), (w0, p0_side0, o0, f0)
    fav_w, fav_p0, fav_name, fav_final = fav
    dog_w, dog_p0, dog_name, dog_final = dog

    entry_ts = fav_w[0][0]
    dog_after = [p for t, p in dog_w if t > entry_ts]
    fav_after = [p for t, p in fav_w if t > entry_ts]
    if not dog_after or not fav_after:
        return None

    # Strategy A: back favorite at fav_p0, hedge dog at its post-entry minimum
    a_hedge_cost = min(dog_after)
    a_locked = 1.0 - (fav_p0 + a_hedge_cost)
    # Strategy B: back dog at dog_p0, hedge favorite at its post-entry minimum
    b_hedge_cost = min(fav_after)
    b_locked = 1.0 - (dog_p0 + b_hedge_cost)

    fav_won = (fav_final or 0) > 0.5

    # Ex-ante rule backtest: enter favorite at p0, hedge at the FIRST moment
    # the pair can be locked at >= (1 - threshold) profit; if that moment
    # never comes, hold unhedged to the end. No hindsight involved.
    rule_pnl = {}
    dog_seq = [p for t, p in dog_w if t > entry_ts]
    for threshold in (0.95, 0.90, 0.80):
        hedged = None
        for q in dog_seq:
            if fav_p0 + q <= threshold:
                hedged = q
                break
        if hedged is not None:
            rule_pnl[threshold] = (1.0 - fav_p0 - hedged, True)
        else:
            rule_pnl[threshold] = ((1.0 - fav_p0) if fav_won else -fav_p0, False)

    return {
        "cid": cid,
        "fav": fav_name, "dog": dog_name,
        "fav_p0": fav_p0, "dog_p0": dog_p0,
        "fav_won": fav_won,
        "fav_range": (min(fav_after), max(fav_after)),
        "a_locked": a_locked,
        "b_locked": b_locked,
        "a_hold": (1.0 - fav_p0) if fav_won else -fav_p0,
        "b_hold": (1.0 - dog_p0) if not fav_won else -dog_p0,
        "rule_pnl": rule_pnl,
    }


def pct(vals, cond):
    n = sum(1 for v in vals if cond(v))
    return 100.0 * n / len(vals) if vals else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-points", type=int, default=100)
    args = ap.parse_args()

    db = sqlite3.connect(os.path.join(BASE_DIR, "esports.db"))
    rows = db.execute(
        """SELECT condition_id, token0, outcome0, token1, outcome1, final0, final1
           FROM markets
           WHERE closed=1 AND group_title='Match Winner'
             AND final0 IS NOT NULL""").fetchall()

    results = []
    for cid, t0, o0, t1, o1, f0, f1 in rows:
        r = analyze_market(db, cid, t0, o0, t1, o1, f0, f1, args.min_points)
        if r:
            results.append(r)

    print("=" * 78)
    print("LoL MATCH LEGGING ANALYSIS ({} matches with usable history)".format(
        len(results)))
    print("=" * 78)
    if not results:
        print("No analyzable matches yet - run esports_backfill.py first.")
        return

    a = [r["a_locked"] for r in results]
    b = [r["b_locked"] for r in results]
    a_hold = [r["a_hold"] for r in results]
    fav_wr = pct(results, lambda r: r["fav_won"])

    print()
    print("Favorite pre-match price: median {:.2f} | favorite won {:.0f}% of matches".format(
        statistics.median(r["fav_p0"] for r in results), fav_wr))
    print()
    print("STRATEGY A - back favorite, hedge underdog at its post-entry low:")
    print("  locked margin per $1 pair:  median {:+.3f} | mean {:+.3f}".format(
        statistics.median(a), statistics.mean(a)))
    print("  matches where lock was possible (>0):   {:.0f}%".format(
        pct(a, lambda v: v > 0)))
    print("  matches with lock > 5 cents:            {:.0f}%".format(
        pct(a, lambda v: v > 0.05)))
    print("  matches with lock > 10 cents:           {:.0f}%".format(
        pct(a, lambda v: v > 0.10)))
    print("  (compare: holding favorite unhedged:    mean {:+.3f}/pair)".format(
        statistics.mean(a_hold)))
    print()
    print("STRATEGY B - back underdog, hedge favorite at its post-entry low:")
    print("  locked margin per $1 pair:  median {:+.3f} | mean {:+.3f}".format(
        statistics.median(b), statistics.mean(b)))
    print("  matches where lock was possible (>0):   {:.0f}%".format(
        pct(b, lambda v: v > 0)))
    print("  matches with lock > 5 cents:            {:.0f}%".format(
        pct(b, lambda v: v > 0.05)))
    print("  matches with lock > 10 cents:           {:.0f}%".format(
        pct(b, lambda v: v > 0.10)))
    print()
    print("STRATEGY C - EX-ANTE RULE (no hindsight): enter favorite pre-match,")
    print("hedge at the FIRST moment a lock of >= (1-threshold) is available;")
    print("hold unhedged if it never is:")
    for threshold in (0.95, 0.90, 0.80):
        pnls = [r["rule_pnl"][threshold][0] for r in results]
        hedge_rate = pct(results, lambda r, th=threshold: r["rule_pnl"][th][1])
        print("  threshold {:.2f}: mean PnL {:+.3f}/pair | median {:+.3f} |"
              " hedged in {:.0f}% of matches".format(
                  threshold, statistics.mean(pnls),
                  statistics.median(pnls), hedge_rate))
    print()
    print("In-play favorite price range (volatility):")
    ranges = [r["fav_range"][1] - r["fav_range"][0] for r in results]
    print("  median swing {:.2f}, mean {:.2f}, max {:.2f}".format(
        statistics.median(ranges), statistics.mean(ranges), max(ranges)))
    print()
    print("Biggest Strategy-A locks:")
    for r in sorted(results, key=lambda x: -x["a_locked"])[:8]:
        print("  {:+.3f}  fav {} @ {:.2f} (won: {}) vs {}".format(
            r["a_locked"], (r["fav"] or "?")[:20], r["fav_p0"],
            "Y" if r["fav_won"] else "N", (r["dog"] or "?")[:20]))
    print("=" * 78)
    print("NOTE: printed-price series, no spread/depth -> treat as UPPER bound.")
    print("The live collector (esports_collector.py) records real books to")
    print("validate executability on future matches.")


if __name__ == "__main__":
    main()
