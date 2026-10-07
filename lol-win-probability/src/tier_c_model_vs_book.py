#!/usr/bin/env python3
"""THE money question: does the state model beat REAL in-play asks?

PRE-REGISTERED (frozen before results):

Universe: live-era games linked via event_links.json that have telemetry
frames AND a resolved game-level market with in-play book snapshots.
Market: the matching "Game N Winner" market (the model predicts a single
game's outcome); for BO1 events game 1 falls back to "Match Winner".
Model: the state_model.py phase models trained ONLY on games started
before 2026-05-01 -> every live-era game (July 2026+) is out-of-sample.

Entry rule: scan telemetry minutes 4..40 in order; at each minute find
the blue/red token book within +-90s. Edges:
    buy-blue: p_model - blue_ask
    buy-red : (1 - p_model) - red_ask
First minute where max(edge) >= 0.05 AND that side's ask depth is at
least $2 notional -> paper-buy 1 share at the ask. ONE bet per game.
Hold to resolution; PnL = final - ask.

Gate: n >= 30 bets AND mean ROI > 0 AND t >= 2.5.
Descriptive extras (not gated): edge >= 0.10 variant, funnel counts,
model-vs-mid gap distribution.
"""

import datetime
import json
import os
import sqlite3
from collections import defaultdict

import numpy as np

from state_model import (BASE_DIR, DB, SPLIT_DATE, PHASES, fit_logistic,
                         predict, load_oe_labels, label_games, build_samples,
                         norm)

EDGE = 0.05
MIN_NOTIONAL = 2.0


def train_phase_models(db):
    oe = load_oe_labels()
    labels = label_games(db, oe)
    X, y, dates, gids, minutes = build_samples(db, labels)
    train = dates < SPLIT_DATE
    models = {}
    for name, lo, hi in PHASES:
        m = (minutes >= lo) & (minutes <= hi)
        models[name] = (fit_logistic(X[train & m], y[train & m]), lo, hi)
    print(f"phase models trained on {train.sum()} samples (< {SPLIT_DATE})")
    return models


def model_p(models, feat, minute):
    for name, (w, lo, hi) in models.items():
        if lo <= minute <= hi:
            return float(predict(w, np.array([feat]))[0])
    return None


def book_at(db, token, ts):
    return db.execute(
        """SELECT best_bid, best_ask, bid_size, ask_size FROM books
           WHERE asset=? AND ts BETWEEN ?-90 AND ?+90
           ORDER BY ABS(ts-?) LIMIT 1""", (token, ts, ts, ts)).fetchone()


def main():
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    models = train_phase_models(db)

    links = json.load(open(os.path.join(BASE_DIR, "event_links.json"),
                           encoding="utf-8"))
    n_games = n_book_games = 0
    bets = []          # (edge_at_entry, side, ask, won, minute, league, slug)
    gaps = []          # model - mid, all matched minutes (descriptive)

    for link in links:
        ev_games = db.execute(
            """SELECT g.game_id, g.number, g.blue_team, g.red_team,
                      e.best_of
               FROM ls_games g JOIN ls_events e ON e.event_id=g.event_id
               WHERE g.event_id=? AND g.blue_team IS NOT NULL""",
            (link["ls_event"],)).fetchall()
        for gid, number, blue, red, best_of in ev_games:
            gt = f"Game {number} Winner"
            mkt = db.execute(
                """SELECT token0, outcome0, final0, token1, outcome1, final1
                   FROM markets WHERE event_slug=? AND group_title=?
                   AND final0 IS NOT NULL AND final0 != 0.5""",
                (link["pm_slug"], gt)).fetchone()
            if mkt is None and best_of == 1 and number == 1:
                mkt = db.execute(
                    """SELECT token0, outcome0, final0, token1, outcome1, final1
                       FROM markets WHERE event_slug=? AND group_title='Match Winner'
                       AND final0 IS NOT NULL AND final0 != 0.5""",
                    (link["pm_slug"],)).fetchone()
            if mkt is None:
                continue
            t0, o0, f0, t1, o1, f1 = mkt
            nb = norm(blue)
            if norm(o0) == nb or (len(norm(o0)) >= 3 and norm(o0) in nb):
                b_tok, b_fin, r_tok = t0, f0, t1
            elif norm(o1) == nb or (len(norm(o1)) >= 3 and norm(o1) in nb):
                b_tok, b_fin, r_tok = t1, f1, t0
            else:
                continue
            frames = db.execute(
                """SELECT ts, b_gold, r_gold, b_kills, r_kills, b_towers,
                          r_towers, b_dragons, r_dragons, b_barons, r_barons,
                          b_inhib, r_inhib
                   FROM ls_frames WHERE game_id=? AND game_state='in_game'
                   ORDER BY ts""", (gid,)).fetchall()
            if len(frames) < 30:
                continue
            n_games += 1
            g_start = frames[0][0]
            saw_book = False
            done = False
            for fr in frames[::6]:
                if done:
                    break
                ts = fr[0]
                minute = (ts - g_start) // 60
                if minute < 4 or minute > 40:
                    continue
                feat = [(fr[1] - fr[2]) / 1000.0, fr[3] - fr[4],
                        fr[5] - fr[6], fr[7] - fr[8], fr[9] - fr[10],
                        (fr[11] or 0) - (fr[12] or 0), minute / 10.0]
                p = model_p(models, feat, minute)
                if p is None:
                    continue
                bb = book_at(db, b_tok, ts)
                rb = book_at(db, r_tok, ts)
                if bb and None not in bb[:2]:
                    saw_book = True
                    gaps.append(p - (bb[0] + bb[1]) / 2)
                cands = []
                if bb and bb[1] is not None and bb[3]:
                    cands.append((p - bb[1], "blue", bb[1], bb[3],
                                  1.0 if b_fin > 0.5 else 0.0))
                if rb and rb[1] is not None and rb[3]:
                    cands.append(((1 - p) - rb[1], "red", rb[1], rb[3],
                                  0.0 if b_fin > 0.5 else 1.0))
                for edge, side, ask, ask_sz, won in sorted(cands, reverse=True):
                    if edge >= EDGE and ask * ask_sz >= MIN_NOTIONAL:
                        bets.append((edge, side, ask, won, minute,
                                     link["league"], link["pm_slug"]))
                        done = True
                        break
            if saw_book:
                n_book_games += 1

    print(f"\nfunnel: linked games with frames+market: {n_games} | "
          f"with any in-play book: {n_book_games} | bets: {len(bets)}")
    if gaps:
        g = np.array(gaps)
        print(f"model-vs-mid gap: mean {g.mean():+.3f}, |gap|>=0.05 in "
              f"{100*float(np.mean(np.abs(g) >= 0.05)):.0f}% of minutes, "
              f">=0.10 in {100*float(np.mean(np.abs(g) >= 0.10)):.0f}%")

    def report(label, sel):
        n = len(sel)
        if n == 0:
            print(f"{label}: n=0")
            return
        roi = np.array([w - a for _, _, a, w, _, _, _ in sel])
        mean, se = roi.mean(), roi.std(ddof=1) / np.sqrt(n) if n > 1 else 1e9
        t = mean / se
        wr = np.mean([w for _, _, _, w, _, _, _ in sel])
        ask = np.mean([a for _, _, a, _, _, _, _ in sel])
        verdict = ("PASS" if (n >= 30 and mean > 0 and t >= 2.5)
                   else "no discovery")
        print(f"{label}: n={n} avg_ask={ask:.3f} win={wr:.3f} "
              f"ROI={mean:+.4f} SE={se:.4f} t={t:+.2f} -> {verdict}")

    print("\n=== PRE-REGISTERED TEST (edge >= 0.05, first hit per game) ===")
    report("all", bets)
    print("\ndescriptive splits:")
    report("  edge >= 0.10 at entry", [b for b in bets if b[0] >= 0.10])
    report("  buy-blue only", [b for b in bets if b[1] == "blue"])
    report("  buy-red only", [b for b in bets if b[1] == "red"])
    report("  entry before min 15", [b for b in bets if b[4] < 15])
    report("  entry min 15+", [b for b in bets if b[4] >= 15])
    from collections import Counter
    for lg, c in Counter(b[5] for b in bets).most_common():
        report(f"  {lg}", [b for b in bets if b[5] == lg])


if __name__ == "__main__":
    main()
