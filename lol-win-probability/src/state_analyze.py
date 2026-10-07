#!/usr/bin/env python3
"""State-aware mispricing analysis: game telemetry x market price x outcome.

Builds a per-minute joined table for every game that has telemetry AND a
linked Polymarket Match Winner market, then answers:

  A) GOLD vs DRAKES: in minutes where the gold leader != the dragon leader,
     whom does the market favor, and who actually wins?
  B) WEIGHTS: linear probability regressions
        market_price(blue)  ~ state features
        final_outcome(blue) ~ state features
     The difference between the two coefficient sets = what the market
     systematically over/under-weights.

Usage: python state_analyze.py
"""

import datetime
import json
import os
import re
import sqlite3

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE_DIR, "data", "esports.db")

SUFFIXES = ("esports", "esport", "gaming", "team", "club", "kia")
ALIASES = {"beijingjdg": "jd", "hanjinbrion": "brion",
           "nongshimredforce": "nongshim"}


def norm(s):
    n = re.sub(r"[^a-z0-9]", "", (s or "").lower())
    for suf in SUFFIXES:
        if n.endswith(suf) and len(n) - len(suf) >= 2:
            n = n[:-len(suf)]
    return ALIASES.get(n, n)


def build_rows(db):
    links = json.load(open(os.path.join(BASE_DIR, "event_links.json"),
                           encoding="utf-8"))
    rows = []
    used_games = 0
    for link in links:
        # Polymarket match-winner market of the linked event
        mw = db.execute(
            """SELECT condition_id, token0, outcome0, token1, outcome1,
                      final0, final1
               FROM markets WHERE event_slug=? AND group_title='Match Winner'""",
            (link["pm_slug"],)).fetchone()
        if not mw or mw[5] is None:
            continue
        cid, t0, o0, t1, o1, f0, f1 = mw
        tok_by_norm = {norm(o0): (t0, f0), norm(o1): (t1, f1)}

        games = db.execute(
            """SELECT game_id, number, blue_team, red_team FROM ls_games
               WHERE event_id=? AND blue_team IS NOT NULL""",
            (link["ls_event"],)).fetchall()
        for gid, number, blue, red, in games:
            nb, nr = norm(blue), norm(red)
            def match_tok(n):
                if n in tok_by_norm:
                    return tok_by_norm[n]
                for k, v in tok_by_norm.items():
                    if len(k) >= 3 and (k in n or n in k):
                        return v
                return None
            btok = match_tok(nb)
            if btok is None:
                continue
            blue_token, blue_final = btok

            frames = db.execute(
                """SELECT ts, b_gold, r_gold, b_kills, r_kills, b_towers,
                          r_towers, b_dragons, r_dragons, b_barons, r_barons
                   FROM ls_frames WHERE game_id=? AND game_state='in_game'
                   ORDER BY ts""", (gid,)).fetchall()
            if len(frames) < 30:
                continue
            g_start = frames[0][0]

            # blue market price per minute: books mid preferred, trades fallback
            def price_lookup(ts):
                row = db.execute(
                    """SELECT best_bid, best_ask FROM books
                       WHERE asset=? AND ts BETWEEN ?-90 AND ?+90
                       ORDER BY ABS(ts-?) LIMIT 1""",
                    (blue_token, ts, ts, ts)).fetchone()
                if row and row[0] is not None and row[1] is not None:
                    return (row[0] + row[1]) / 2.0
                row = db.execute(
                    """SELECT price FROM trades
                       WHERE asset=? AND ts BETWEEN ?-180 AND ?+60
                       ORDER BY ABS(ts-?) LIMIT 1""",
                    (blue_token, ts, ts, ts)).fetchone()
                return row[0] if row else None

            got = False
            for fr in frames[::6]:  # one row per ~minute
                ts = fr[0]
                p = price_lookup(ts)
                if p is None or not (0.03 <= p <= 0.97):
                    continue
                rows.append({
                    "game": gid, "event": link["pm_slug"], "number": number,
                    "league": link["league"],
                    "min": (ts - g_start) / 60.0,
                    "gold": (fr[1] - fr[2]) / 1000.0,   # k gold, blue-red
                    "kills": fr[3] - fr[4],
                    "towers": fr[5] - fr[6],
                    "drakes": fr[7] - fr[8],
                    "barons": fr[9] - fr[10],
                    "price": p,
                    "won": 1.0 if blue_final > 0.5 else 0.0,
                })
                got = True
            if got:
                used_games += 1
    return rows, used_games


def ols(X, y):
    X1 = np.column_stack([np.ones(len(X)), X])
    beta, *_ = np.linalg.lstsq(X1, y, rcond=None)
    return beta


def main():
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows, used_games = build_rows(db)
    print(f"joined minute-rows: {len(rows)} from {used_games} games")
    if len(rows) < 200:
        print("not enough joined data yet")
        return

    # ---------------- A) gold vs drakes conflict minutes ----------------
    print()
    print("=" * 76)
    print("A) GOLD-vs-DRAKES konfliktus-percek (aranyvezeto != sarkanyvezeto)")
    print("=" * 76)
    conf = [r for r in rows if r["gold"] * r["drakes"] < 0
            and abs(r["gold"]) >= 1.0 and abs(r["drakes"]) >= 2]
    games_c = len({r["game"] for r in conf})
    print(f"konfliktus-percek: {len(conf)} ({games_c} game-bol) "
          f"[|gold|>=1k es |drake diff|>=2]")
    if conf:
        # normalize to the GOLD leader's perspective
        p_gold, won_gold = [], []
        for r in conf:
            if r["gold"] > 0:
                p_gold.append(r["price"]); won_gold.append(r["won"])
            else:
                p_gold.append(1 - r["price"]); won_gold.append(1 - r["won"])
        n = len(p_gold)
        print(f"  az ARANY-vezeto piaci ara atlag: {np.mean(p_gold):.3f}")
        print(f"  az ARANY-vezeto TENYLEGES gyozelmi aranya: {np.mean(won_gold):.3f}")
        print(f"  -> elteres: {np.mean(won_gold)-np.mean(p_gold):+.3f} "
              f"(negativ = a piac tularazza az aranyat a drake-ekkel szemben)")
        print(f"  (megjegyzes: perc-sorok korrelaltak game-en belul; "
          f"effektiv minta ~{games_c} game)")

    # ---------------- B) weight comparison ----------------
    print()
    print("=" * 76)
    print("B) SULY-OSSZEVETES: mit araz a piac vs mit mond a valosag")
    print("=" * 76)
    feats = ["gold", "kills", "towers", "drakes", "barons"]
    X = np.array([[r[f] for f in feats] for r in rows])
    y_price = np.array([r["price"] for r in rows])
    y_won = np.array([r["won"] for r in rows])
    b_price = ols(X, y_price)
    b_won = ols(X, y_won)
    print(f"  {'jel':<10} {'piac sulya':>12} {'valosag sulya':>14} {'kulonbseg':>12}")
    print("  " + "-" * 52)
    for i, f in enumerate(feats):
        bp, bw = b_price[i + 1], b_won[i + 1]
        note = ""
        if abs(bw) > 1e-9:
            if bp > bw * 1.3: note = "piac TULsulyozza"
            elif bp < bw * 0.7: note = "piac ALULsulyozza"
        print(f"  {f:<10} {bp:>+12.4f} {bw:>+14.4f} {bw-bp:>+12.4f}  {note}")
    print()
    print("  (linear probability approx; minute rows correlated within a game ->")
    print("   directions are informative, significance needs game-level bootstrap.")
    print("   effective n =", used_games, "games)")

    # ---------------- B/league) same weights per league ----------------
    print()
    print("B/liga) sulyok ligankent (kis minta - csak irany!):")
    for lg in ("LCK", "LPL", "LEC"):
        sub = [r for r in rows if r["league"] == lg]
        gl = len({r["game"] for r in sub})
        if len(sub) < 100:
            print(f"  {lg}: n={len(sub)} sor / {gl} game - keves")
            continue
        Xl = np.array([[r[f] for f in feats] for r in sub])
        bp = ols(Xl, np.array([r["price"] for r in sub]))
        bw = ols(Xl, np.array([r["won"] for r in sub]))
        print(f"  {lg} ({gl} game): gold piac {bp[1]:+.3f}/valo {bw[1]:+.3f} | "
              f"drakes {bp[4]:+.3f}/{bw[4]:+.3f} | barons {bp[5]:+.3f}/{bw[5]:+.3f}")


if __name__ == "__main__":
    main()
