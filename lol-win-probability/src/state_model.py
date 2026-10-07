#!/usr/bin/env python3
"""Game-state -> win probability model on the full telemetry backfill.

PRE-REGISTERED DESIGN (frozen before results):

Labels: blue_win joined from Oracle's Elixir by (normalized team pair,
date +-1 day, game number). Games without an OE label are dropped (no
guessing winners from gold).

Samples: one frame per elapsed MINUTE per game (subsampling reduces the
10s-frame autocorrelation), minutes 2..40, in-progress frames only.

Features (blue minus red unless noted):
  gold_diff (thousands), kills_diff, towers_diff, dragons_diff,
  barons_diff, inhibs_diff, minute/10.

Model: logistic regression via Newton/IRLS (numpy only), fitted
SEPARATELY for three game phases - early (<14 min), mid (14-25),
late (>25) - so objective values can vary by phase.

Validation: chronological split - train on games started before
2026-05-01, test on 2026-05-01 and later. Metrics: Brier + logloss vs
a p=0.5 baseline, and a calibration table by predicted-probability decile.
Per-phase coefficient table = the "what is a drake really worth" answer
on ~30x the old sample.

Descriptive extras (not gated): conditional win-rate tables for headline
states, e.g. P(win | gold +3k & dragons +2) - my canonical case.
"""

import datetime
import os
import re
import sqlite3
from collections import defaultdict

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE_DIR, "data", "esports.db")
SPLIT_DATE = "2026-05-01"

SUFFIXES = ("esports", "esport", "gaming", "team", "club", "kia")
ALIASES = {"beijingjdg": "jd", "hanjinbrion": "brion",
           "nongshimredforce": "nongshim"}


def norm(s):
    n = re.sub(r"[^a-z0-9]", "", (s or "").lower())
    for suf in SUFFIXES:
        if n.endswith(suf) and len(n) - len(suf) >= 2:
            n = n[:-len(suf)]
    return ALIASES.get(n, n)


def load_oe_labels():
    """(pairkey, date, game_no) -> blue_win, with date as ISO day."""
    import csv
    idx = {}
    for y in ("2025", "2026"):
        p = os.path.join(BASE_DIR, "data", f"oe_{y}.csv")
        if not os.path.exists(p):
            continue
        games = {}
        with open(p, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("position") != "team":
                    continue
                gid = r.get("gameid")
                g = games.setdefault(gid, {"date": (r.get("date") or "")[:10],
                                           "no": r.get("game"), "sides": {}})
                g["sides"][r.get("side")] = (r.get("teamname"), r.get("result"))
        for g in games.values():
            b, rr = g["sides"].get("Blue"), g["sides"].get("Red")
            if not b or not rr or b[1] not in ("0", "1"):
                continue
            pair = tuple(sorted((norm(b[0]), norm(rr[0]))))
            idx[(pair, g["date"], str(g["no"]))] = int(b[1])
    return idx


def label_games(db, oe):
    """ls game_id -> blue_win."""
    labels = {}
    n_events = n_hit = 0
    for gid, number, t1, t2, start in db.execute(
            """SELECT g.game_id, g.number, e.team1, e.team2, e.start_time
               FROM ls_games g JOIN ls_events e ON e.event_id = g.event_id
               WHERE EXISTS (SELECT 1 FROM ls_frames f WHERE f.game_id=g.game_id)"""):
        n_events += 1
        pair = tuple(sorted((norm(t1), norm(t2))))
        try:
            d0 = datetime.date.fromisoformat((start or "")[:10])
        except ValueError:
            continue
        for dd in (0, 1, -1):
            key = (pair, (d0 + datetime.timedelta(days=dd)).isoformat(),
                   str(number))
            if key in oe:
                labels[gid] = (oe[key], (start or "")[:10])
                n_hit += 1
                break
    print(f"games with frames: {n_events} | OE-labeled: {n_hit} "
          f"({100*n_hit/max(1,n_events):.0f}%)")
    return labels


def build_samples(db, labels):
    """rows: [gold_k, kills, towers, dragons, barons, inhibs, min/10], y, date, gid, minute"""
    X, y, dates, gids, minutes = [], [], [], [], []
    cur = db.execute("""SELECT game_id, ts, game_state, b_gold, r_gold,
                        b_kills, r_kills, b_towers, r_towers,
                        b_dragons, r_dragons, b_barons, r_barons,
                        b_inhib, r_inhib FROM ls_frames ORDER BY game_id, ts""")
    prev_gid, t0, taken = None, None, set()
    for (gid, ts, state, bg, rg, bk, rk, bt, rt,
         bd, rd, bb, rb, bi, ri) in cur:
        if gid not in labels:
            continue
        if gid != prev_gid:
            prev_gid, t0, taken = gid, ts, set()
        minute = (ts - t0) // 60
        if minute < 2 or minute > 40 or minute in taken:
            continue
        if state == "finished":
            continue
        if None in (bg, rg):
            continue
        taken.add(minute)
        X.append([(bg - rg) / 1000.0, (bk or 0) - (rk or 0),
                  (bt or 0) - (rt or 0), (bd or 0) - (rd or 0),
                  (bb or 0) - (rb or 0), (bi or 0) - (ri or 0),
                  minute / 10.0])
        lab, d = labels[gid]
        y.append(lab)
        dates.append(d)
        gids.append(gid)
        minutes.append(minute)
    return (np.array(X), np.array(y, dtype=float), np.array(dates),
            np.array(gids), np.array(minutes))


def fit_logistic(X, y, iters=50):
    Xb = np.hstack([np.ones((len(X), 1)), X])
    w = np.zeros(Xb.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-Xb @ w))
        Wd = p * (1 - p) + 1e-9
        H = Xb.T @ (Xb * Wd[:, None]) + 1e-6 * np.eye(Xb.shape[1])
        g = Xb.T @ (y - p)
        step = np.linalg.solve(H, g)
        w += step
        if np.max(np.abs(step)) < 1e-8:
            break
    return w


def predict(w, X):
    Xb = np.hstack([np.ones((len(X), 1)), X])
    return 1 / (1 + np.exp(-Xb @ w))


FEATS = ["gold_k", "kills", "towers", "dragons", "barons", "inhibs", "min/10"]
PHASES = [("early <14m", 2, 13), ("mid 14-25m", 14, 25), ("late >25m", 26, 40)]


def main():
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    oe = load_oe_labels()
    print(f"OE label index: {len(oe)} games")
    labels = label_games(db, oe)
    X, y, dates, gids, minutes = build_samples(db, labels)
    print(f"samples: {len(X)} minute-frames from {len(set(gids))} games")

    train = dates < SPLIT_DATE
    test = ~train
    print(f"train: {train.sum()} samples ({len(set(gids[train]))} games, "
          f"< {SPLIT_DATE}) | test: {test.sum()} samples "
          f"({len(set(gids[test]))} games)")

    print("\n=== PER-PHASE MODELS (trained on train era only) ===")
    models = {}
    for name, lo, hi in PHASES:
        m = (minutes >= lo) & (minutes <= hi)
        w = fit_logistic(X[train & m], y[train & m])
        models[name] = (w, lo, hi)
        coef = "  ".join(f"{f}={c:+.3f}" for f, c in zip(FEATS, w[1:]))
        print(f"{name}: n={int((train & m).sum())}  {coef}")

    print("\n=== OBJECTIVE VALUE IN GOLD TERMS (coef / gold_k coef) ===")
    print(f"{'phase':<12}" + "".join(f"{f:>9}" for f in FEATS[1:6]))
    for name, (w, lo, hi) in models.items():
        gk = w[1]
        vals = [w[i] / gk for i in range(2, 7)]
        print(f"{name:<12}" + "".join(f"{v:>8.2f}k" for v in vals))

    print("\n=== OUT-OF-TIME TEST (games from", SPLIT_DATE, ") ===")
    p_all = np.zeros(len(X))
    for name, (w, lo, hi) in models.items():
        m = (minutes >= lo) & (minutes <= hi)
        p_all[m] = predict(w, X[m])
    pt, yt = p_all[test], y[test]
    brier = float(np.mean((pt - yt) ** 2))
    ll = float(-np.mean(yt * np.log(pt + 1e-12)
                        + (1 - yt) * np.log(1 - pt + 1e-12)))
    print(f"Brier: {brier:.4f}  (0.25 = coin baseline)")
    print(f"logloss: {ll:.4f}  (0.693 = coin)")
    print("\ncalibration by decile (test era):")
    print(f"{'pred bucket':>14} {'n':>7} {'avg pred':>9} {'actual':>8}")
    for lo in np.arange(0, 1, 0.1):
        m = (pt >= lo) & (pt < lo + 0.1)
        if m.sum() < 50:
            continue
        print(f"{lo:>6.1f}-{lo+0.1:<6.1f} {int(m.sum()):>7} "
              f"{float(pt[m].mean()):>9.3f} {float(yt[m].mean()):>8.3f}")

    print("\n=== HEADLINE CONDITIONAL STATES (test era, descriptive) ===")
    gd, dd, bb = X[:, 0], X[:, 3], X[:, 4]
    mids = (minutes >= 14) & (minutes <= 25) & test
    cases = [
        ("gold +3k & dragons +2 (mid)", mids & (gd >= 3) & (dd >= 2)),
        ("gold +3k, dragons <=0 (mid)", mids & (gd >= 3) & (dd <= 0)),
        ("gold in [-1k,1k], dragons +2 (mid)",
         mids & (np.abs(gd) <= 1) & (dd >= 2)),
        ("gold -2k..0 but baron up (late)",
         (minutes > 25) & test & (gd >= -2) & (gd <= 0) & (bb >= 1)),
    ]
    for label, m in cases:
        if m.sum() < 30:
            print(f"  {label}: n={int(m.sum())} (too few)")
            continue
        print(f"  {label}: n={int(m.sum())}  "
              f"model {float(p_all[m].mean()):.3f}  "
              f"actual {float(y[m].mean()):.3f}")


if __name__ == "__main__":
    main()
