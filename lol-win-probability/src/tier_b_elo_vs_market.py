#!/usr/bin/env python3
"""Tier B, test B2: chronological Elo model vs the Polymarket price.

PRE-REGISTERED (frozen before any result is computed):

Model: game-level chronological Elo over OE 2025+2026 (K=20, start 1500,
standard logistic p = 1/(1+10^(-d/400)) - the same spec the Draft Analyzer
v1 uses). Strictly leakage-free: a match's prediction uses only games that
finished BEFORE that match's date (same-day games of the same event are
excluded from the update until the event is over -> we predict with the
Elo as of 00:00 that day).

Match-level probability from per-game q by format (parsed from the event
title): BO1 p=q; BO3 p=q^2(3-2q); BO5 p=q^3(10-15q+6q^2).

Tests (on linked events whose Match Winner market has a sane first print):
  B2a CALIBRATION DUEL: Brier(model) vs Brier(market first print).
      No gate - descriptive; the interesting number is the gap.
  B2b DISAGREEMENT RULE: when |p_model - p_market| >= 0.10, paper-buy the
      side the model favors at the market first print. Hold to resolution.
      KILL: ROI <= 0 OR t < 2.5 OR n < 30.
  B2c same rule, minor leagues only.

Entry prices are printed prices (signal backtest, not book-verified).
"""

import json
import csv
import datetime
import os
import re
import sqlite3
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
K = 20.0
START = 1500.0

SUFFIXES = ("esports", "esport", "gaming", "team", "club", "kia")
ALIASES = {"beijingjdg": "jd", "hanjinbrion": "brion",
           "nongshimredforce": "nongshim"}


def norm(s):
    n = re.sub(r"[^a-z0-9]", "", (s or "").lower())
    for suf in SUFFIXES:
        if n.endswith(suf) and len(n) - len(suf) >= 2:
            n = n[:-len(suf)]
    return ALIASES.get(n, n)


def load_oe_games(path):
    """[(datetime, team_norm_blue, team_norm_red, blue_win)] game-level."""
    games = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("position") != "team":
                continue
            gid = r.get("gameid")
            g = games.setdefault(gid, {"date": r.get("date"), "sides": {}})
            g["sides"][r.get("side")] = (r.get("teamname"), r.get("result"))
    out = []
    for g in games.values():
        b = g["sides"].get("Blue")
        r = g["sides"].get("Red")
        if not b or not r or b[1] not in ("0", "1"):
            continue
        try:
            dt = datetime.datetime.fromisoformat(g["date"])
        except (ValueError, TypeError):
            continue
        out.append((dt, norm(b[0]), norm(r[0]), int(b[1])))
    out.sort(key=lambda x: x[0])
    return out


def match_prob(q, fmt):
    if fmt == "BO1":
        return q
    if fmt == "BO3":
        return q * q * (3 - 2 * q)
    if fmt == "BO5":
        return q ** 3 * (10 - 15 * q + 6 * q * q)
    return q * q * (3 - 2 * q)  # default BO3


def first_price(series, token, band=(0.03, 0.97)):
    for ts, p in series.get(token, []):
        if band[0] <= p <= band[1]:
            return p
    return None


def last_price_before(series, token, ts_limit, band=(0.03, 0.97)):
    """Last sane print strictly before ts_limit (robustness entry: the
    freshest price you could still have traded pre-match)."""
    out = None
    for ts, p in series.get(token, []):
        if ts >= ts_limit:
            break
        if band[0] <= p <= band[1]:
            out = p
    return out


def main():
    games = []
    for y in ("2025", "2026"):
        p = os.path.join(BASE_DIR, "data", f"oe_{y}.csv")
        if os.path.exists(p):
            games.extend(load_oe_games(p))
    games.sort(key=lambda x: x[0])
    print(f"OE games for Elo: {len(games)}")

    links = json.load(open(os.path.join(BASE_DIR, "oe_links.json"),
                           encoding="utf-8"))
    db = sqlite3.connect(
        f"file:{os.path.join(BASE_DIR, 'data', 'esports_bf.db')}?mode=ro",
        uri=True)
    events = {}
    for slug, title in db.execute("SELECT slug, title FROM events"):
        events[slug] = title
    mw = {}
    for slug, t0, o0, f0, t1, o1, f1, gst in db.execute("""
            SELECT event_slug, token0, outcome0, final0, token1, outcome1,
                   final1, game_start_time
            FROM markets WHERE group_title='Match Winner'
            AND closed=1 AND final0 IS NOT NULL AND final0 != 0.5"""):
        start_ts = None
        if gst:
            try:
                start_ts = int(datetime.datetime.fromisoformat(
                    gst.replace("Z", "+00:00")).timestamp())
            except ValueError:
                pass
        mw[slug] = (t0, o0, f0, t1, o1, f1, start_ts)
    series = defaultdict(list)
    for asset, ts, price in db.execute("SELECT asset, ts, price FROM price_history"):
        series[asset].append((ts, price))
    for asset, ts, price in db.execute("SELECT asset, ts, price FROM trades"):
        series[asset].append((ts, price))
    for a in series:
        series[a].sort()

    # walk games chronologically; before each date, snapshot Elo for the
    # linked matches on that date
    by_date = defaultdict(list)  # date -> [slug]
    for slug, info in links.items():
        if slug in mw:
            by_date[info["date"]].append(slug)

    elo = defaultdict(lambda: START)
    results = []  # (slug, p_model, p_market, outcome, league, fmt)
    gi = 0
    for date in sorted(by_date):
        d0 = datetime.datetime.fromisoformat(date)
        while gi < len(games) and games[gi][0] < d0:
            _, b, r, bw = games[gi]
            eb, er = elo[b], elo[r]
            pe = 1 / (1 + 10 ** (-(eb - er) / 400))
            elo[b] += K * (bw - pe)
            elo[r] += K * ((1 - bw) - (1 - pe))
            gi += 1
        for slug in by_date[date]:
            t0, o0, f0, t1, o1, f1, start_ts = mw[slug]
            title = events.get(slug, "")
            fm = re.search(r"\((BO[135])\)", title)
            fmt = fm.group(1) if fm else "BO3"
            n0, n1 = norm(o0), norm(o1)
            d_elo = elo[n0] - elo[n1]
            q = 1 / (1 + 10 ** (-d_elo / 400))
            p_model = match_prob(q, fmt)
            p_market = first_price(series, t0)
            p_market_late = (last_price_before(series, t0, start_ts)
                             if start_ts else None)
            if p_market is None:
                continue
            outcome = 1 if f0 > 0.5 else 0
            lg = re.search(r"\)\s*-\s*(.+)$", title)
            results.append((slug, p_model, p_market, outcome,
                            (lg.group(1).strip()[:24] if lg else "?"), fmt,
                            elo[n0] != START or elo[n1] != START,
                            p_market_late))

    known = [r for r in results if r[6]]
    print(f"linked+priced events: {len(results)} "
          f"(with at least one known-Elo team: {len(known)})")

    # B2a calibration duel
    n = len(known)
    bm = sum((r[1] - r[3]) ** 2 for r in known) / n
    bk = sum((r[2] - r[3]) ** 2 for r in known) / n
    base = sum((0.5 - r[3]) ** 2 for r in known) / n
    print(f"\nB2a CALIBRATION DUEL (n={n}):")
    print(f"  Brier model : {bm:.4f}")
    print(f"  Brier market: {bk:.4f}")
    print(f"  Brier 0.5   : {base:.4f}")
    print(f"  -> {'MODEL beats market' if bm < bk else 'MARKET beats model'} "
          f"by {abs(bm-bk):.4f}")

    # B2b disagreement rule - both entry variants
    bets, bets_late = [], []
    for slug, p_mod, p_mkt, y, lg, fmt, _, p_late in known:
        for src_p, sink in ((p_mkt, bets), (p_late, bets_late)):
            if src_p is None:
                continue
            gap = p_mod - src_p
            if abs(gap) < 0.10:
                continue
            if gap > 0:   # model likes team0 -> buy team0
                entry, won = src_p, y
            else:         # model likes team1 -> buy team1 (1-p proxy)
                entry, won = 1 - src_p, 1 - y
            sink.append((entry, won - entry, lg, None))
    def show(label, xs):
        m = len(xs)
        if m < 30:
            print(f"  {label}: n={m} < 30 -> KILL (underpowered)")
            return
        mean = sum(r for _, r, _, _ in xs) / m
        var = sum((r - mean) ** 2 for _, r, _, _ in xs) / (m - 1)
        se = (var / m) ** 0.5
        t = mean / se if se > 0 else 0
        wr = sum(1 for _, r, _, _ in xs if r > 0) / m
        ae = sum(e for e, _, _, _ in xs) / m
        verdict = "SURVIVES" if (mean > 0 and t >= 2.5) else "KILL"
        print(f"  {label}: n={m} avg_entry={ae:.3f} win={wr:.3f} "
              f"ROI={mean:+.4f} t={t:+.2f} -> {verdict}")
    print(f"\nB2b DISAGREEMENT RULE (|gap|>=0.10):")
    show("entry=FIRST print", bets)
    show("entry=LAST pre-match print (robust)", bets_late)
    n_late_all = sum(1 for r in known if r[7] is not None)
    print(f"  (last-print coverage: {n_late_all}/{len(known)} events had a "
          f"pre-match print + start time)")

    print(f"\nB2c minor leagues only:")
    tier1 = re.compile(r"\b(LCK|LPL|LEC|LCS|LTA)\b")
    minor = [b for b in bets if not tier1.search(b[2])]
    show("minor", minor)

    from collections import Counter
    print("\n  per-league entry counts (top 10):")
    for lg, c in Counter(b[2] for b in bets).most_common(10):
        sub = [r for _, r, l, _ in bets if l == lg]
        print(f"    {lg:26s} n={c:3d} ROI={sum(sub)/len(sub):+.3f}")


if __name__ == "__main__":
    main()
