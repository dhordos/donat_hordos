#!/usr/bin/env python3
"""Arbitrage scanner for LoL match markets (uses the live collector's data).

Block 1 - INTRA-MARKET PAIR ARB (hard evidence, real order books):
  Polymarket lets you merge a Yes+No pair into $1 (and split $1 into a pair).
  So:  ask_yes + ask_no < 1  -> buy both, merge   = riskless profit
       bid_yes + bid_no > 1  -> split $1, sell both = riskless profit
  We scan every book snapshot pair and measure frequency, size (depth-limited)
  and persistence of such moments.

Block 2 - CROSS-MARKET LOGIC VIOLATIONS (indicative, last-trade prices):
  Within one BO3 event these identities MUST hold:
    P(team -1.5 handicap, i.e. 2-0)  <=  P(team wins match)
    P(Under 2.5 games)  ==  P(A 2-0) + P(B 2-0)
  During fast swings the thinner markets lag -> temporary violations.
  (No books for these markets, so this block flags opportunities without
  proving executability.)

Usage: python esports_arb_scan.py --db data/esports.db
"""

import argparse
import collections
import datetime
import os
import sqlite3
import statistics

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FEE_BUFFER = 0.005   # merge/split gas+friction allowance per pair


def fmt_ts(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


# ------------------------------------------------------------- block 1 ------

def intra_market_scan(db):
    print("=" * 78)
    print("1) INTRA-MARKET PAIR ARB - real order books (executable evidence)")
    print("=" * 78)
    rows = db.execute("""
        SELECT b1.ts, m.condition_id, m.question,
               b1.best_bid, b1.best_ask, b1.bid_size, b1.ask_size,
               b2.best_bid, b2.best_ask, b2.bid_size, b2.ask_size
        FROM markets m
        JOIN books b1 ON b1.asset = m.token0
        JOIN books b2 ON b2.asset = m.token1 AND b2.ts = b1.ts
        WHERE b1.best_ask IS NOT NULL AND b2.best_ask IS NOT NULL
    """).fetchall()
    print(f"paired book snapshots: {len(rows)} "
          f"(both sides quoted at the same moment)")
    if not rows:
        return

    buy_arbs, sell_arbs = [], []
    for (ts, cid, title, bb1, ba1, bs1, as1, bb2, ba2, bs2, as2) in rows:
        ask_sum = ba1 + ba2
        if ask_sum < 1.0 - FEE_BUFFER:
            depth = min(as1 or 0, as2 or 0)
            buy_arbs.append({"ts": ts, "cid": cid, "title": title,
                             "edge": 1.0 - ask_sum, "depth": depth,
                             "profit": (1.0 - ask_sum) * depth})
        if bb1 is not None and bb2 is not None:
            bid_sum = bb1 + bb2
            if bid_sum > 1.0 + FEE_BUFFER:
                depth = min(bs1 or 0, bs2 or 0)
                sell_arbs.append({"ts": ts, "cid": cid, "title": title,
                                  "edge": bid_sum - 1.0, "depth": depth,
                                  "profit": (bid_sum - 1.0) * depth})

    for name, arbs in (("BUY-side (ask_yes+ask_no < 1)", buy_arbs),
                       ("SELL-side (bid_yes+bid_no > 1)", sell_arbs)):
        print(f"--- {name} ---")
        if not arbs:
            print("  none found above the {:.1%} fee buffer".format(FEE_BUFFER))
            continue
        n = len(arbs)
        moments = len({(a['cid'], a['ts']) for a in arbs})
        markets_hit = len({a['cid'] for a in arbs})
        edges = [a["edge"] for a in arbs]
        profits = [a["profit"] for a in arbs]
        print(f"  arb snapshots: {n} | distinct markets: {markets_hit}")
        print(f"  edge per pair: median {statistics.median(edges):.3f} | "
              f"max {max(edges):.3f}")
        print(f"  depth-limited profit per moment: median "
              f"{statistics.median(profits):.2f} USDC | max {max(profits):.2f}")
        print(f"  total naive profit if every moment captured once: "
              f"{sum(profits):.2f} USDC")
        # persistence: consecutive-minute runs on the same market
        by_market = collections.defaultdict(list)
        for a in arbs:
            by_market[a["cid"]].append(a["ts"])
        runs = []
        for tss in by_market.values():
            tss.sort()
            run = 1
            for i in range(1, len(tss)):
                if tss[i] - tss[i - 1] <= 90:
                    run += 1
                else:
                    runs.append(run)
                    run = 1
            runs.append(run)
        print(f"  persistence: median run {statistics.median(runs):.0f} "
              f"snapshot(s) (~min), max {max(runs)}")
        print("  top 5 moments:")
        for a in sorted(arbs, key=lambda x: -x["profit"])[:5]:
            print(f"    {fmt_ts(a['ts'])}  edge {a['edge']:.3f} x depth "
                  f"{a['depth']:.0f} = {a['profit']:.2f} USDC  "
                  f"{(a['title'] or '?')[:45]}")
        print()


# ------------------------------------------------------------- block 2 ------

def minute_price_series(db, cid_tokens):
    """token -> {minute_ts: last trade price} for given tokens."""
    out = {}
    for tok in cid_tokens:
        bars = {}
        for ts, p in db.execute(
                "SELECT ts, price FROM trades WHERE asset=? ORDER BY ts", (tok,)):
            bars[ts // 60] = p
        out[tok] = bars
    return out


def cross_market_scan(db):
    print("=" * 78)
    print("2) CROSS-MARKET LOGIC VIOLATIONS - last-trade prices (indicative)")
    print("=" * 78)
    events = db.execute("SELECT slug, title FROM events").fetchall()
    viol_hc = []   # handicap > match winner
    viol_ou = []   # under != sum of 2-0s
    events_checked = 0
    for slug, ev_title in events:
        mkts = db.execute("""
            SELECT condition_id, question, group_title, token0, outcome0,
                   token1, outcome1
            FROM markets WHERE event_slug=?""", (slug,)).fetchall()
        mw = next((m for m in mkts if m[2] == "Match Winner"), None)
        hc = next((m for m in mkts if m[2] and "Handicap" in m[2]), None)
        if not mw or not hc:
            continue
        # Only the MINUS-1.5 side means "wins 2-0" and must obey
        # P(2-0) <= P(wins match). The +1.5 side means "wins >=1 game",
        # which may legitimately exceed the team's match-win price.
        import re as _re
        mneg = _re.search(r"([^:(]+?)\s*\(-1\.5\)", hc[2] or "")
        if not mneg:
            continue
        minus_team = mneg.group(1).strip()
        events_checked += 1
        series = minute_price_series(db, [mw[3], mw[5], hc[3], hc[5]])
        pairs = []
        for h_tok, h_name in ((hc[3], hc[4]), (hc[5], hc[6])):
            if (h_name or "").strip() != minus_team:
                continue
            m_tok = mw[3] if (h_name or "") == mw[4] else (
                mw[5] if (h_name or "") == mw[6] else None)
            if m_tok:
                pairs.append((h_tok, m_tok, h_name))
        for h_tok, m_tok, name in pairs:
            h_bars, m_bars = series[h_tok], series[m_tok]
            for minute, hp in h_bars.items():
                mp = m_bars.get(minute)
                if mp is None:
                    continue
                gap = hp - mp   # P(2-0) must be <= P(win match)
                if gap > 0.03:
                    viol_hc.append({"gap": gap, "minute": minute,
                                    "team": name, "event": ev_title})
    print(f"events with Match Winner + Handicap trade data: {events_checked}")
    if viol_hc:
        gaps = [v["gap"] for v in viol_hc]
        print(f"  P(2-0) > P(match win) violations (>3c): {len(viol_hc)} "
              f"minute-observations")
        print(f"  gap: median {statistics.median(gaps):.3f} | max {max(gaps):.3f}")
        worst = sorted(viol_hc, key=lambda v: -v["gap"])[:5]
        for v in worst:
            print(f"    {fmt_ts(v['minute']*60)}  gap {v['gap']:.3f}  "
                  f"{v['team']}  ({(v['event'] or '?')[:40]})")
    else:
        print("  no handicap-vs-winner violations above 3 cents")
    print()
    print("NOTE: block 2 uses last-trade prints, not quotes - a 'violation' is")
    print("a signal to look, not guaranteed fillable. Block 1 is the real test.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/esports.db")
    args = ap.parse_args()
    db_path = args.db
    if not os.path.isabs(db_path):
        db_path = os.path.join(BASE_DIR, db_path)
    db = sqlite3.connect(db_path)
    intra_market_scan(db)
    cross_market_scan(db)


if __name__ == "__main__":
    main()
