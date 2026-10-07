#!/usr/bin/env python3
"""Microstructure analysis of the LIVE-collected LoL data (books + trades).

Block 1 - LEAD-LAG: do Game-N-Winner books lag the Match-Winner book (or the
  reverse)? Cross-correlate 1-min mid returns for the same team at lags
  -3..+3 min. A consistent asymmetry = the laggard is predictable.

Block 2 - BOOK-PRESSURE: does bid/ask size imbalance predict the next
  3/10-minute mid move on winner markets? (Real depth data - only the live
  collector has this.)

Block 3 - UNDER-2.5 BOUND: P(Under 2.5) >= P(fav -1.5) must always hold
  (Under = someone wins 2-0, which includes the fav's 2-0). Scan same-minute
  last-trade prints for violations.

Usage: python esports_micro_analyze.py --db data/esports.db
"""

import argparse
import collections
import datetime
import os
import re
import sqlite3
import statistics

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def mid_series(db, asset):
    """minute -> mid price from book snapshots."""
    out = {}
    for ts, bb, ba in db.execute(
            "SELECT ts, best_bid, best_ask FROM books WHERE asset=? ORDER BY ts",
            (asset,)):
        if bb is not None and ba is not None:
            out[ts // 60] = (bb + ba) / 2.0
    return out


def returns(mids):
    """minute -> 1-min return (only for consecutive minutes)."""
    out = {}
    for m, p in mids.items():
        prev = mids.get(m - 1)
        if prev is not None:
            out[m] = p - prev
    return out


def corr(xs, ys):
    n = len(xs)
    if n < 10:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = (sum((x - mx) ** 2 for x in xs)) ** 0.5
    sy = (sum((y - my) ** 2 for y in ys)) ** 0.5
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


# ------------------------------------------------------------- block 1 ------

def lead_lag(db):
    print("=" * 78)
    print("1) LEAD-LAG: Game-N-Winner book vs Match-Winner book (1-min mid returns)")
    print("   corr at lag>0 means the GAME market leads (match book is late)")
    print("=" * 78)
    events = db.execute("SELECT DISTINCT event_slug FROM markets").fetchall()
    lag_samples = collections.defaultdict(list)
    pairs_used = 0
    for (slug,) in events:
        mkts = db.execute("""SELECT group_title, token0, outcome0, token1, outcome1
                             FROM markets WHERE event_slug=?""", (slug,)).fetchall()
        mw = next((m for m in mkts if m[0] == "Match Winner"), None)
        if not mw:
            continue
        games = [m for m in mkts if m[0] and re.match(r"Game \d Winner", m[0])]
        mw_tokens = {mw[2]: mw[1], mw[4]: mw[3]}
        for g in games:
            for team, g_tok in ((g[2], g[1]), (g[4], g[3])):
                m_tok = mw_tokens.get(team)
                if not m_tok:
                    continue
                g_ret = returns(mid_series(db, g_tok))
                m_ret = returns(mid_series(db, m_tok))
                common = sorted(set(g_ret) & set(m_ret))
                # focus on active periods: minutes where the game book moved
                active = [m for m in common if abs(g_ret[m]) >= 0.005]
                if len(active) < 15:
                    continue
                pairs_used += 1
                for lag in range(-3, 4):
                    xs, ys = [], []
                    for m in active:
                        if (m + lag) in m_ret:
                            xs.append(g_ret[m])
                            ys.append(m_ret[m + lag])
                    c = corr(xs, ys)
                    if c is not None:
                        lag_samples[lag].append(c)
    print(f"market-pairs with enough active overlap: {pairs_used}")
    for lag in range(-3, 4):
        cs = lag_samples.get(lag, [])
        if cs:
            tag = ("game leads by" if lag > 0 else
                   "match leads by" if lag < 0 else "simultaneous")
            print(f"  lag {lag:+d} min ({tag} {abs(lag)}m): mean corr "
                  f"{statistics.mean(cs):+.3f}  (n={len(cs)})")
    print()


# ------------------------------------------------------------- block 2 ------

def book_pressure(db):
    print("=" * 78)
    print("2) BOOK-PRESSURE: does size imbalance predict the next mid move?")
    print("=" * 78)
    assets = [r[0] for r in db.execute(
        """SELECT DISTINCT m.token0 FROM markets m
           WHERE m.group_title LIKE '%Winner%'
           UNION SELECT DISTINCT m.token1 FROM markets m
           WHERE m.group_title LIKE '%Winner%'""")]
    horizons = (3, 10)
    obs = {h: [] for h in horizons}
    for asset in assets:
        rows = db.execute(
            """SELECT ts, best_bid, best_ask, bid_size, ask_size FROM books
               WHERE asset=? ORDER BY ts""", (asset,)).fetchall()
        mids = {}
        imbs = {}
        for ts, bb, ba, bs, asz in rows:
            if bb is None or ba is None:
                continue
            m = ts // 60
            mids[m] = (bb + ba) / 2.0
            tot = (bs or 0) + (asz or 0)
            if tot > 0:
                imbs[m] = ((bs or 0) - (asz or 0)) / tot
        for m, imb in imbs.items():
            for h in horizons:
                fwd = mids.get(m + h)
                if fwd is not None and 0.05 <= mids[m] <= 0.95:
                    obs[h].append((imb, fwd - mids[m]))
    for h in horizons:
        data = obs[h]
        if len(data) < 100:
            print(f"  {h}-min horizon: not enough data ({len(data)})")
            continue
        c = corr([x for x, _ in data], [y for _, y in data])
        data.sort(key=lambda t: t[0])
        q = len(data) // 5
        lo = [y for _, y in data[:q]]
        hi = [y for _, y in data[-q:]]
        print(f"  {h}-min horizon: n={len(data)} | corr(imb, fwd move) = {c:+.3f}")
        print(f"    strongest ASK-pressure quintile: mean fwd move "
              f"{statistics.mean(lo):+.4f}")
        print(f"    strongest BID-pressure quintile: mean fwd move "
              f"{statistics.mean(hi):+.4f}")
    print()


# ------------------------------------------------------------- block 3 ------

def under_bound(db):
    print("=" * 78)
    print("3) UNDER-2.5 BOUND: P(Under) >= P(fav -1.5) - same-minute last trades")
    print("=" * 78)
    events = db.execute("SELECT DISTINCT event_slug FROM markets").fetchall()
    checked = 0
    viols = []
    for (slug,) in events:
        mkts = db.execute("""SELECT group_title, question, token0, outcome0,
                                    token1, outcome1
                             FROM markets WHERE event_slug=?""", (slug,)).fetchall()
        ou = next((m for m in mkts if m[0] and "O/U" in m[0]), None)
        hc = next((m for m in mkts if m[0] and "Handicap" in m[0]), None)
        if not ou or not hc:
            continue
        mneg = re.search(r"([^:(]+?)\s*\(-1\.5\)", hc[0] or "")
        if not mneg:
            continue
        minus_team = mneg.group(1).strip()
        hc_tok = None
        for tok, name in ((hc[2], hc[3]), (hc[4], hc[5])):
            if (name or "").strip() == minus_team:
                hc_tok = tok
        under_tok = None
        for tok, name in ((ou[2], ou[3]), (ou[4], ou[5])):
            if (name or "").lower().startswith("under"):
                under_tok = tok
        if not hc_tok or not under_tok:
            continue
        checked += 1
        h_bars, u_bars = {}, {}
        for ts, p in db.execute("SELECT ts, price FROM trades WHERE asset=? "
                                "ORDER BY ts", (hc_tok,)):
            h_bars[ts // 60] = p
        for ts, p in db.execute("SELECT ts, price FROM trades WHERE asset=? "
                                "ORDER BY ts", (under_tok,)):
            u_bars[ts // 60] = p
        for m, up in u_bars.items():
            hp = h_bars.get(m)
            if hp is not None and hp - up > 0.03:
                viols.append({"gap": hp - up, "minute": m, "slug": slug})
    print(f"events with O/U + Handicap trade data: {checked}")
    if viols:
        gaps = [v["gap"] for v in viols]
        print(f"  violations (fav-1.5 print > Under print by >3c): {len(viols)}")
        print(f"  gap median {statistics.median(gaps):.3f} | max {max(gaps):.3f}")
        for v in sorted(viols, key=lambda x: -x["gap"])[:5]:
            t = datetime.datetime.fromtimestamp(v["minute"] * 60)
            print(f"    {t:%m-%d %H:%M}  gap {v['gap']:.3f}  {v['slug']}")
    else:
        print("  no violations above 3 cents")
    print()
    print("NOTE: last-trade prints, not quotes - flags, not guaranteed fills.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/esports.db")
    args = ap.parse_args()
    db_path = args.db
    if not os.path.isabs(db_path):
        db_path = os.path.join(BASE_DIR, db_path)
    db = sqlite3.connect(db_path)
    lead_lag(db)
    book_pressure(db)
    under_bound(db)


if __name__ == "__main__":
    main()
