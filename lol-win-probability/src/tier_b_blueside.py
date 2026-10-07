#!/usr/bin/env python3
"""Tier B, test B1: blue-side edge in near-coinflip Game 1s.

PRE-REGISTERED (frozen before any result below was computed):

Causal story: blue side carries a mechanical map-control edge (dragon-pit
sightline, jungle pathing). Polymarket's Game 1 Winner market has no notion
of "blue"/"red" - it prices from team-strength priors alone. Game 1's side
assignment is exogenous to team strength (coin-flip / pre-set, not chosen
based on information), so this isolates the blue-side effect cleanly from
confounds like "loser picks side" (which applies to G2+, not G1).

Entry rule: Game 1 Winner markets where the blue-side team (identified via
the Oracle's Elixir join, oe_links.json) has a sane first print in the
"coinflip band" [0.40, 0.60] - i.e. the market itself judged the matchup
close, so team-strength is roughly controlled for. Buy the blue-side team
at that print.
Exit: hold to Game 1 resolution.
Data: esports_bf.db (Game 1 Winner prints) x oe_links.json (blue/red +
blue_win), joined by normalized team name (same norm() as oe_join.py -
these are the exact strings that already matched during the OE join).

Kill criterion (fixed before running): blue win rate in band <= 0.52, OR
t < 2.5, OR n < 20.
Benchmark: this is a calibration-slice test - ROI = final - entry price,
where entry averages ~0.50 by construction (that's what "coinflip band"
means), so ROI > 0 IS the blue-side edge showing up as free money the
market left on the table.
"""

import json
import os
import re
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SUFFIXES = ("esports", "esport", "gaming", "team", "club", "kia")
ALIASES = {"beijingjdg": "jd", "hanjinbrion": "brion",
           "nongshimredforce": "nongshim"}


def norm(s):
    n = re.sub(r"[^a-z0-9]", "", (s or "").lower())
    for suf in SUFFIXES:
        if n.endswith(suf) and len(n) - len(suf) >= 2:
            n = n[:-len(suf)]
    return ALIASES.get(n, n)


def first_price(series, token, band=(0.03, 0.97)):
    for ts, p in series.get(token, []):
        if band[0] <= p <= band[1]:
            return ts, p
    return None, None


def main():
    links = json.load(open(os.path.join(BASE_DIR, "oe_links.json"),
                           encoding="utf-8"))
    db = sqlite3.connect(f"file:{os.path.join(BASE_DIR, 'data', 'esports_bf.db')}"
                         f"?mode=ro", uri=True)

    g1 = {}
    for slug, t0, o0, f0, t1, o1, f1 in db.execute("""
            SELECT event_slug, token0, outcome0, final0, token1, outcome1, final1
            FROM markets WHERE group_title='Game 1 Winner'
            AND closed=1 AND final0 IS NOT NULL"""):
        g1[slug] = (t0, o0, f0, t1, o1, f1)

    series = {}
    for asset, ts, price in db.execute("SELECT asset, ts, price FROM price_history"):
        series.setdefault(asset, []).append((ts, price))
    for asset, ts, price in db.execute("SELECT asset, ts, price FROM trades"):
        series.setdefault(asset, []).append((ts, price))
    for a in series:
        series[a].sort()

    # sanity check #1: is blue really the exogenous G1 side (not the G1-loser
    # convention, which only applies from G2 on)? just report blue win rate
    # overall as a fact-check of our own patch/league window.
    n_blue_win_all = n_all = 0
    for slug, info in links.items():
        for g in info["games"]:
            if g["game_no"] == "1" and g["blue_win"] in ("0", "1"):
                n_all += 1
                n_blue_win_all += int(g["blue_win"])
    print(f"FACT-CHECK: empirical blue-side win rate in G1s (this dataset, "
          f"2026 patches): {n_blue_win_all}/{n_all} = "
          f"{n_blue_win_all/n_all:.3f}" if n_all else "no G1 data")

    rets = []
    matched = 0
    for slug, info in links.items():
        if slug not in g1:
            continue
        g1_games = [g for g in info["games"] if g["game_no"] == "1"]
        if not g1_games:
            continue
        g = g1_games[0]
        blue, red, blue_win = g["blue"], g["red"], g["blue_win"]
        if blue_win not in ("0", "1"):
            continue
        t0, o0, f0, t1, o1, f1 = g1[slug]
        if norm(o0) == norm(blue):
            blue_tok, blue_fin = t0, f0
        elif norm(o1) == norm(blue):
            blue_tok, blue_fin = t1, f1
        else:
            continue
        matched += 1
        _, entry = first_price(series, blue_tok)
        if entry is None or not (0.40 <= entry <= 0.60):
            continue
        rets.append((entry, blue_fin - entry))

    print(f"\nG1 events matched to OE by team name: {matched}/{len(g1)}")
    n = len(rets)
    print(f"\nB1 blue-side-in-coinflip-band test:")
    if n < 20:
        print(f"  n={n} < 20 -> KILL (underpowered, no verdict)")
        return
    mean = sum(r for _, r in rets) / n
    var = sum((r - mean) ** 2 for _, r in rets) / (n - 1)
    se = (var / n) ** 0.5
    t = mean / se if se > 0 else 0
    wr = sum(1 for _, r in rets if r > 0) / n
    avg_entry = sum(e for e, _ in rets) / n
    print(f"  n={n} avg_entry={avg_entry:.3f} blue_win_rate={wr:.3f} "
          f"ROI={mean:+.4f} SE={se:.4f} t={t:+.2f}")
    if wr <= 0.52:
        print(f"  -> KILL: blue win rate {wr:.3f} <= 0.52 (no edge in this window)")
    elif t < 2.5:
        print(f"  -> KILL: t={t:.2f} < 2.5 (not significant)")
    else:
        print(f"  -> SURVIVES pre-registered gate (n>=20, WR>0.52, t>=2.5)")


if __name__ == "__main__":
    main()
