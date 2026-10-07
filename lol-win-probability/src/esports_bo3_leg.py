#!/usr/bin/env python3
"""BO3 legging backtest: buy the game-1 loser, hedge at 1-1.

My manual strategy:
  - Team W wins game 1 -> loser L's match price drops (e.g. to ~0.30)
  - BUY L right after game 1
  - If L equalizes (1-1), the match price reverts toward ~0.50:
    BUY W at that price -> pair locked below $1 -> riskless profit
  - Risk: L gets swept 0-2 and the entry stake is lost.

Data: backfilled price_history + Game 1/2 Winner market resolutions.
Game-end timestamps are approximated as the first moment the winning game
token trades >= 0.90 (converged); entries/hedges use the next available
match-winner bar (~10-min resolution).

Also reported: the decider stat (at 1-1, how often does the game-1 winner
take game 3?) and the no-hedge alternative (hold L to resolution).

Usage: python esports_bo3_leg.py --db data/esports.db
"""

import argparse
import os
import sqlite3
import statistics

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def series(db, asset):
    return db.execute("SELECT ts, price FROM price_history WHERE asset=? "
                      "ORDER BY ts", (asset,)).fetchall()


def first_cross_ts(s, level=0.90):
    for ts, p in s:
        if p >= level:
            return ts
    return None


def price_at_or_after(s, ts):
    for t, p in s:
        if t >= ts:
            return t, p
    return None, None


def load_events(db):
    """Events having Match Winner + Game 1 + Game 2 markets with finals."""
    out = []
    events = db.execute("SELECT slug, title FROM events").fetchall()
    for slug, title in events:
        mkts = db.execute("""
            SELECT group_title, token0, outcome0, token1, outcome1, final0, final1
            FROM markets WHERE event_slug=? AND closed=1
              AND final0 IS NOT NULL""", (slug,)).fetchall()
        by = {}
        for g, t0, o0, t1, o1, f0, f1 in mkts:
            by[g] = {"tokens": [(t0, o0, f0), (t1, o1, f1)]}
        if not all(k in by for k in ("Match Winner", "Game 1 Winner",
                                     "Game 2 Winner")):
            continue
        out.append({"slug": slug, "title": title, "by": by})
    return out


def winner_of(entry):
    for tok, out, fin in entry["tokens"]:
        if fin is not None and fin > 0.5:
            return out, tok
    return None, None


def token_of(entry, team):
    for tok, out, fin in entry["tokens"]:
        if out == team:
            return tok
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/esports.db")
    ap.add_argument("--max-entry", type=float, default=0.45,
                    help="only buy the game-1 loser below this price")
    ap.add_argument("--min-entry", type=float, default=0.05)
    args = ap.parse_args()
    db_path = args.db
    if not os.path.isabs(db_path):
        db_path = os.path.join(BASE_DIR, db_path)
    db = sqlite3.connect(db_path)

    events = load_events(db)
    print(f"BO3 events with Match+Game1+Game2 winner data: {len(events)}")

    trades = []
    decider = {"g1w_wins": 0, "g1w_loses": 0}
    skipped = {"no_g1_time": 0, "no_entry_bar": 0, "entry_band": 0}
    for ev in events:
        g1_team, g1_tok = winner_of(ev["by"]["Game 1 Winner"])
        g2_team, _ = winner_of(ev["by"]["Game 2 Winner"])
        mw_team, _ = winner_of(ev["by"]["Match Winner"])
        if not g1_team or not g2_team or not mw_team:
            continue
        # the loser of game 1
        mw = ev["by"]["Match Winner"]
        teams = [o for _, o, _ in mw["tokens"]]
        if g1_team not in teams:
            continue
        loser = teams[0] if teams[1] == g1_team else teams[1]

        went_11 = (g2_team == loser)
        if went_11:
            if mw_team == g1_team:
                decider["g1w_wins"] += 1
            else:
                decider["g1w_loses"] += 1

        # timing from game-token convergence
        g1_end = first_cross_ts(series(db, g1_tok))
        if g1_end is None:
            skipped["no_g1_time"] += 1
            continue
        l_tok = token_of(mw, loser)
        w_tok = token_of(mw, g1_team)
        l_series = series(db, l_tok)
        w_series = series(db, w_tok)
        ts_e, entry = price_at_or_after(l_series, g1_end)
        if entry is None:
            skipped["no_entry_bar"] += 1
            continue
        if not (args.min_entry <= entry <= args.max_entry):
            skipped["entry_band"] += 1
            continue

        rec = {"title": ev["title"], "loser": loser, "entry": entry,
               "went_11": went_11}
        if went_11:
            g2_tok = token_of(ev["by"]["Game 2 Winner"], loser)
            g2_end = first_cross_ts(series(db, g2_tok))
            hedge = None
            if g2_end:
                _, hedge = price_at_or_after(w_series, g2_end)
            if hedge is None:
                hedge = 0.5  # conservative fallback
            rec["hedge"] = hedge
            rec["pnl"] = 1.0 - entry - hedge      # locked pair
            rec["pnl_nohedge"] = (1.0 - entry) if mw_team == loser else -entry
        else:
            rec["hedge"] = None
            rec["pnl"] = -entry                    # swept 0-2
            rec["pnl_nohedge"] = -entry
        trades.append(rec)

    print(f"skipped: {skipped}")
    print()
    print("=" * 78)
    print("DECIDER STAT (all 1-1 matches in sample):")
    tot = decider["g1w_wins"] + decider["g1w_loses"]
    if tot:
        print(f"  game-1 winner takes game 3: {decider['g1w_wins']}/{tot} "
              f"= {100*decider['g1w_wins']/tot:.0f}%")
    print("=" * 78)
    print(f"STRATEGY: buy game-1 loser (entry {args.min_entry}-{args.max_entry}), "
          f"hedge game-1 winner at 1-1")
    print("=" * 78)
    if not trades:
        print("no qualifying trades")
        return
    n = len(trades)
    n11 = sum(1 for t in trades if t["went_11"])
    pnls = [t["pnl"] for t in trades]
    entries = [t["entry"] for t in trades]
    print(f"  trades: {n} | reached 1-1: {n11} ({100*n11/n:.0f}%) | "
          f"swept 0-2: {n-n11}")
    print(f"  avg entry price: {statistics.mean(entries):.3f}")
    hedges = [t["hedge"] for t in trades if t["hedge"] is not None]
    if hedges:
        print(f"  avg hedge price at 1-1: {statistics.mean(hedges):.3f}")
    locked = [t["pnl"] for t in trades if t["went_11"]]
    if locked:
        print(f"  locked profit when 1-1: mean {statistics.mean(locked):+.3f} "
              f"per pair (min {min(locked):+.3f}, max {max(locked):+.3f})")
    mean_pnl = statistics.mean(pnls)
    s = (statistics.stdev(pnls) / (n ** 0.5)) if n > 1 else float("nan")
    roi = [t["pnl"] / t["entry"] for t in trades]
    print(f"  TOTAL: mean PnL {mean_pnl:+.4f} per trade +- {s:.4f} (SEM), "
          f"t~{mean_pnl/s:.1f}" if s == s else "")
    print(f"  ROI on entry stake: mean {statistics.mean(roi):+.3f} | "
          f"median {statistics.median(roi):+.3f}")
    nh = [t["pnl_nohedge"] for t in trades]
    print(f"  compare NO-HEDGE (hold loser to end): mean {statistics.mean(nh):+.4f}")
    print()
    # breakdown: was the game-1 loser the pre-match favorite or the dog?
    # proxy: entry price closer to 0.45 = was likely the favorite
    for label, sel in (("entry >= 0.30 (loser was strong)",
                        [t for t in trades if t["entry"] >= 0.30]),
                       ("entry < 0.30 (loser was weak)",
                        [t for t in trades if t["entry"] < 0.30])):
        if len(sel) >= 5:
            ps = [t["pnl"] for t in sel]
            r11 = 100 * sum(1 for t in sel if t["went_11"]) / len(sel)
            print(f"  {label}: n={len(sel)}, 1-1 rate {r11:.0f}%, "
                  f"mean PnL {statistics.mean(ps):+.4f}")
    print()
    print("Top 5 best and worst:")
    for t in sorted(trades, key=lambda x: -x["pnl"])[:5]:
        print(f"  {t['pnl']:+.3f}  entry {t['entry']:.2f} hedge "
              f"{t['hedge'] if t['hedge'] else '-'}  {(t['title'] or '?')[:48]}")
    for t in sorted(trades, key=lambda x: x["pnl"])[:3]:
        print(f"  {t['pnl']:+.3f}  entry {t['entry']:.2f} (swept)  "
              f"{(t['title'] or '?')[:48]}")
    print()
    print("CAVEATS: ~10-min bars (entry may lag the true post-game-1 price);")
    print("no spread modeled (live books showed ~1c); one trade per event.")


if __name__ == "__main__":
    main()
