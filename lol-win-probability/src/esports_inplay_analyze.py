#!/usr/bin/env python3
"""In-play mean-reversion & "trough-to-Nx" analysis for LoL match markets.

Two independent data sources:
  --source history : backfilled price_history (~10-min bars, broad sample)
  --source trades  : the live collector's real trades (second-level, resampled
                     to 1-min bars) - fewer matches but fine-grained, and the
                     books table lets us check REAL spreads at entry moments.

Blocks:
  A) Overreaction test: after the largest N-min drop, does the side win more
     often than the post-drop price implies? (diagnostic, hindsight-selected)
  B) "Drops to X, recovers to Nx" frequency (my manual pattern).
  C) EX-ANTE rule backtest: >=R drop from running peak + two consecutive
     upticks -> buy; exit at target multiple or hold to resolution.
     This is the only tradable block. Reports ROI (per $1 staked), SEM,
     fav/dog and early/mid/late breakdowns, and (trades mode) real spreads.

Usage:
  python esports_inplay_analyze.py --db data/esports.db --source history
  python esports_inplay_analyze.py --db data/esports.db --source trades
"""

import argparse
import math
import os
import sqlite3
import statistics

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WINDOW_SEC = 12 * 3600
ENTRY_FLOOR = 0.05   # don't "buy" lottery-zone prints below this
ENTRY_CEIL = 0.90    # nor near-certainties


# --------------------------------------------------------------- loading ----

def resample_1min(rows):
    """Irregular (ts, price) trade prints -> 1-min last-price bars."""
    bars = {}
    for ts, p in rows:
        bars[ts // 60] = p  # last print wins within the minute
    return [(m * 60, p) for m, p in sorted(bars.items())]


def load_matches(db, source, min_points):
    out = []
    mw = db.execute(
        """SELECT condition_id, token0, outcome0, token1, outcome1, final0, final1
           FROM markets
           WHERE closed=1 AND group_title='Match Winner'
             AND final0 IS NOT NULL""").fetchall()
    for cid, t0, o0, t1, o1, f0, f1 in mw:
        if source == "history":
            s0 = db.execute("SELECT ts, price FROM price_history WHERE asset=? "
                            "ORDER BY ts", (t0,)).fetchall()
            s1 = db.execute("SELECT ts, price FROM price_history WHERE asset=? "
                            "ORDER BY ts", (t1,)).fetchall()
        else:
            s0 = resample_1min(db.execute(
                "SELECT ts, price FROM trades WHERE asset=? ORDER BY ts",
                (t0,)).fetchall())
            s1 = resample_1min(db.execute(
                "SELECT ts, price FROM trades WHERE asset=? ORDER BY ts",
                (t1,)).fetchall())
        if len(s0) < min_points or len(s1) < min_points:
            continue
        end_ts = max(s0[-1][0], s1[-1][0])
        win_start = end_ts - WINDOW_SEC
        w0 = [(t, p) for t, p in s0 if t >= win_start]
        w1 = [(t, p) for t, p in s1 if t >= win_start]
        if len(w0) < 10 or len(w1) < 10:
            continue
        if w0[0][1] >= w1[0][1]:
            fav = {"w": w0, "p0": w0[0][1], "asset": t0, "final": f0}
            dog = {"w": w1, "p0": w1[0][1], "asset": t1, "final": f1}
        else:
            fav = {"w": w1, "p0": w1[0][1], "asset": t1, "final": f1}
            dog = {"w": w0, "p0": w0[0][1], "asset": t0, "final": f0}
        # ex-ante in-play start: first bar-to-bar move >= 0.04 on either side
        # (detectable in real time - pre-match prices barely move)
        inplay = None
        for w in (fav["w"], dog["w"]):
            for k in range(1, len(w)):
                if abs(w[k][1] - w[k - 1][1]) >= 0.04:
                    if inplay is None or w[k][0] < inplay:
                        inplay = w[k][0]
                    break
        out.append({"cid": cid, "fav": fav, "dog": dog, "inplay": inplay})
    return out


def sides_of(m):
    yield "fav", m["fav"]
    yield "dog", m["dog"]


def sem(xs):
    if len(xs) < 2:
        return float("nan")
    return statistics.stdev(xs) / math.sqrt(len(xs))


# --------------------------------------------------------------------- A ----

def overreaction_test(matches, wmin):
    wsec = wmin * 60
    obs = []
    for m in matches:
        for side, s in sides_of(m):
            won = 1 if s["final"] > 0.5 else 0
            pts = s["w"][1:]
            best = None
            j = 0
            for i, (t, p) in enumerate(pts):
                while j < i and pts[j][0] < t - wsec:
                    j += 1
                drop = p - pts[j][1]
                if drop < 0 and (best is None or drop < best[0]):
                    best = (drop, p)
            if best:
                obs.append({"drop": best[0], "p_after": best[1], "won": won})
    return obs


def print_overreaction(matches, windows):
    print()
    print("=" * 78)
    print("A) OVERREACTION TEST - largest N-min drop per match+side vs actual outcome")
    print("   (diagnostic only: 'the largest drop' is only known in hindsight)")
    print("=" * 78)
    buckets = [(-1.00, -0.30), (-0.30, -0.20), (-0.20, -0.15),
               (-0.15, -0.10), (-0.10, -0.05), (-0.05, 0.00)]
    for wmin in windows:
        obs = overreaction_test(matches, wmin)
        print(f"--- {wmin}-min window (n={len(obs)}) ---")
        print("  {:<16} {:>5} {:>11} {:>11} {:>9}".format(
            "drop size", "n", "avg price", "actual win%", "buy EV"))
        for lo, hi in buckets:
            sel = [o for o in obs
                   if lo <= o["drop"] < hi and ENTRY_FLOOR <= o["p_after"] <= ENTRY_CEIL]
            if len(sel) < 5:
                continue
            n = len(sel)
            avg_p = sum(o["p_after"] for o in sel) / n
            wr = sum(o["won"] for o in sel) / n
            ev = wr / avg_p - 1
            print("  {:<16} {:>5} {:>11.3f} {:>10.1f}% {:>+8.1f}%".format(
                f"{lo:+.2f}..{hi:+.2f}", n, avg_p, wr * 100, ev * 100))
        print()


# --------------------------------------------------------------------- B ----

def print_trough_recovery(matches):
    print("=" * 78)
    print("B) 'DROPS TO X, RECOVERS TO Nx' - could a perfect bottom-buy have doubled?")
    print("=" * 78)
    obs = []
    for m in matches:
        for side, s in sides_of(m):
            pts = s["w"][1:]
            if len(pts) < 5:
                continue
            ti, (tt, tp) = min(enumerate(pts), key=lambda x: x[1][1])
            if tp >= s["p0"] or tp < 0.01:
                continue
            after = pts[ti + 1:]
            if not after:
                continue
            end_t = pts[-1][0]
            peak_all = max(p for _, p in after)
            # variant that ignores the final 10% of the window - strips the
            # trivial "it recovered because it won and settled at 1.00" cases
            cutoff = end_t - 0.1 * (end_t - pts[0][0])
            early = [p for t, p in after if t <= cutoff]
            peak_early = max(early) if early else tp
            obs.append({"side": side, "trough": tp,
                        "r_all": peak_all / tp, "r_early": peak_early / tp})
    n = len(obs)
    if not n:
        print("no usable observations")
        return
    print(f"observations (match+side with a real dip below entry): {n}")
    print("  {:<28} {:>8} {:>8} {:>8}".format("", ">=1.5x", ">=2x", ">=3x"))
    for key, label in (("r_all", "any time before close"),
                       ("r_early", "excl. final 10% (no settle-pop)")):
        row = [100 * sum(1 for o in obs if o[key] >= mult) / n
               for mult in (1.5, 2.0, 3.0)]
        print("  {:<28} {:>7.0f}% {:>7.0f}% {:>7.0f}%".format(label, *row))
    med = statistics.median(o["r_all"] for o in obs)
    print(f"  median recovery multiple (any time): {med:.2f}x")
    print()


# --------------------------------------------------------------------- C ----

def ex_ante_backtest(matches, drop_r, target_mult, gate_minutes=None):
    """gate_minutes=(lo, hi): only enter between lo..hi minutes after the
    detected in-play start - a REAL-TIME computable filter (unlike rel_t,
    which needs the match end and is reported for diagnostics only)."""
    trades = []
    for m in matches:
        for side, s in sides_of(m):
            won = 1 if s["final"] > 0.5 else 0
            pts = s["w"][1:]
            if len(pts) < 5:
                continue
            if gate_minutes and m["inplay"] is None:
                continue
            t_start, t_end = pts[0][0], pts[-1][0]
            peak = s["p0"]
            entry = None
            for i, (t, p) in enumerate(pts):
                if entry is None:
                    peak = max(peak, p)
                    if gate_minutes:
                        mins = (t - m["inplay"]) / 60.0
                        if not (gate_minutes[0] <= mins <= gate_minutes[1]):
                            continue
                    if (p <= peak - drop_r and i >= 2
                            and pts[i - 1][1] > pts[i - 2][1]
                            and p > pts[i - 1][1]
                            and ENTRY_FLOOR <= p <= ENTRY_CEIL):
                        entry = {"price": p, "ts": t, "asset": s["asset"],
                                 "rel_t": (t - t_start) / max(1, t_end - t_start)}
                else:
                    if p >= entry["price"] * target_mult:
                        exit_p = entry["price"] * target_mult
                        trades.append({**entry, "side": side, "exit": exit_p,
                                       "hit": True,
                                       "roi": exit_p / entry["price"] - 1})
                        entry = None
                        break
            if entry is not None:
                payoff = 1.0 if won else 0.0
                trades.append({**entry, "side": side, "exit": payoff,
                               "hit": False,
                               "roi": payoff / entry["price"] - 1})
    return trades


def print_ex_ante(db, matches, source, drop_r, target_mult, gate_minutes=None):
    trades = ex_ante_backtest(matches, drop_r, target_mult, gate_minutes)
    print("=" * 78)
    gate_s = (f"; entry gate {gate_minutes[0]}-{gate_minutes[1]}min after in-play start"
              if gate_minutes else "")
    print(f"C) EX-ANTE RULE  (drop >= {drop_r:.2f} + 2 upticks -> buy; "
          f"exit {target_mult}x or resolution{gate_s})")
    print("=" * 78)
    if len(trades) < 5:
        print(f"  only {len(trades)} trades - not meaningful")
        print()
        return
    rois = [t["roi"] for t in trades]
    n = len(trades)
    mean_roi = statistics.mean(rois)
    s = sem(rois)
    hit = sum(1 for t in trades if t["hit"])
    wins = sum(1 for r in rois if r > 0)
    print(f"  trades: {n} | target hit: {hit} ({100*hit/n:.0f}%) | "
          f"win rate: {100*wins/n:.0f}%")
    print(f"  ROI per $1 staked: mean {mean_roi:+.3f} +- {s:.3f} (SEM) | "
          f"median {statistics.median(rois):+.3f} | t~{mean_roi/s:.1f}" if s == s
          else "  (SEM n/a)")
    for label, sel in (("fav-side dips", [t for t in trades if t["side"] == "fav"]),
                       ("dog-side dips", [t for t in trades if t["side"] == "dog"])):
        if len(sel) >= 5:
            rs = [t["roi"] for t in sel]
            print(f"    {label:<15} n={len(sel):>3}  mean ROI {statistics.mean(rs):+.3f}")
    for label, lo, hi in (("early third", 0, 1/3), ("mid third", 1/3, 2/3),
                          ("late third", 2/3, 1.01)):
        sel = [t for t in trades if lo <= t["rel_t"] < hi]
        if len(sel) >= 5:
            rs = [t["roi"] for t in sel]
            print(f"    {label:<15} n={len(sel):>3}  mean ROI {statistics.mean(rs):+.3f}")
    if source == "trades":
        spreads = spread_at_entries(db, trades)
        if spreads:
            print(f"    real spread at entry (books): median "
                  f"{statistics.median(spreads):.3f}, worst "
                  f"{max(spreads):.3f}  (n={len(spreads)})")
    print()


def spread_at_entries(db, trades):
    """Nearest book snapshot within +-5 min of each entry, same asset."""
    spreads = []
    for t in trades:
        row = db.execute(
            """SELECT best_bid, best_ask FROM books
               WHERE asset=? AND ts BETWEEN ? AND ?
               ORDER BY ABS(ts - ?) LIMIT 1""",
            (t["asset"], t["ts"] - 300, t["ts"] + 300, t["ts"])).fetchone()
        if row and row[0] is not None and row[1] is not None:
            spreads.append(row[1] - row[0])
    return spreads


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="esports.db")
    ap.add_argument("--source", choices=["history", "trades"], default="history")
    ap.add_argument("--min-points", type=int, default=None)
    args = ap.parse_args()

    db_path = args.db
    if not os.path.isabs(db_path):
        db_path = os.path.join(BASE_DIR, db_path)
    db = sqlite3.connect(db_path)

    min_points = args.min_points or (100 if args.source == "history" else 60)
    matches = load_matches(db, args.source, min_points)
    windows = [10, 20, 30] if args.source == "history" else [3, 5, 10]
    bar = "~10-min bars" if args.source == "history" else "1-min bars (live trades)"
    print(f"source={args.source} ({bar}) | usable matches: {len(matches)}")
    if not matches:
        return

    print_overreaction(matches, windows)
    print_trough_recovery(matches)
    for drop_r, mult in ((0.10, 1.5), (0.15, 1.5), (0.15, 2.0), (0.20, 2.0)):
        print_ex_ante(db, matches, args.source, drop_r, mult)
    print(">>> TIME-GATED variants (only real-time computable filters):")
    for drop_r, mult in ((0.10, 1.5), (0.15, 1.5), (0.15, 2.0)):
        print_ex_ante(db, matches, args.source, drop_r, mult, (10, 90))

    print("CAVEATS: no spread/slippage in ROI numbers (trades mode reports the")
    print("real spread separately); entry filter {}..{}; one trade per".format(
        ENTRY_FLOOR, ENTRY_CEIL))
    print("match+side. History bars ~10min; trades mode 1-min, small-N.")


if __name__ == "__main__":
    main()
