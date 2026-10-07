#!/usr/bin/env python3
"""Oracle's Elixir pipeline: download match data, build champion stats, fit model.

Usage:
  python lol_champion_stats.py --download          # fetch 2025+2026 CSVs (Drive)
  python lol_champion_stats.py --build             # build champion_stats.json
  python lol_champion_stats.py --download --build  # both

Output: champion_stats.json - consumed by the Draft Analyzer Streamlit UI (not in this repo).

Notes:
- Google Drive enforces a daily download quota on these popular files; if you
  see a quota message, retry the next day.
- Leagues covered: LCK, LPL, LEC (configurable with --leagues).
- Champion stats are recency-weighted (half-life ~180 days) so the current
  meta counts more than last year's.
"""

import argparse
import datetime
import json
import math
import os
import sys
import time

import numpy as np
import pandas as pd
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

DRIVE_FILES = {
    2024: "1IjIEhLc9n8eLKeY-yh_YigKVWbhgGBsN",
    2025: "1v6LRphp2kYciU4SXp0PCjEMuev1bDejc",
    2026: "1hnpbrUpBMS1TZI7IovfpKeZfWJH1Aptm",
}
DEFAULT_YEARS = [2025, 2026]
DEFAULT_LEAGUES = ["LCK", "LPL", "LEC"]

SHORT_MAX = 28 * 60   # game length buckets (seconds)
LONG_MIN = 33 * 60
HALF_LIFE_DAYS = 180.0


def download(years):
    os.makedirs(DATA_DIR, exist_ok=True)
    ok = True
    for y in years:
        fid = DRIVE_FILES.get(y)
        if not fid:
            print(f"[{y}] no Drive file id known - skipping")
            continue
        dest = os.path.join(DATA_DIR, f"oe_{y}.csv")
        url = ("https://drive.usercontent.google.com/download"
               f"?id={fid}&export=download&confirm=t")
        print(f"[{y}] downloading...")
        r = requests.get(url, timeout=600)
        head = r.content[:400].decode("utf-8", "replace")
        if "Quota exceeded" in head or head.lstrip().startswith("<!DOCTYPE"):
            print(f"[{y}] BLOCKED: Google Drive daily quota exceeded on this "
                  "file - try again tomorrow.")
            ok = False
            continue
        with open(dest, "wb") as f:
            f.write(r.content)
        print(f"[{y}] saved {len(r.content)/1e6:.1f} MB -> {dest}")
    return ok


def load_frames(years):
    frames = []
    for y in years:
        p = os.path.join(DATA_DIR, f"oe_{y}.csv")
        if not os.path.exists(p) or os.path.getsize(p) < 100_000:
            print(f"[{y}] missing or too small ({p}) - run --download first")
            continue
        df = pd.read_csv(p, low_memory=False)
        frames.append(df)
        print(f"[{y}] {len(df)} rows loaded (all leagues)")
    if not frames:
        sys.exit("No usable data files - aborting.")
    return pd.concat(frames, ignore_index=True)


def build(years, leagues):
    df_all = load_frames(years)
    df_all["date"] = pd.to_datetime(df_all["date"], errors="coerce")
    df_all = df_all.dropna(subset=["date"])
    # big-3 view drives everything except the GLOBAL fallback champion table
    df = df_all[df_all["league"].isin(leagues)]
    print(f"big-3 rows: {len(df)} / all-league rows: {len(df_all)}")
    # NOTE: team rows have champion=NaN - drop champion only on the player view,
    # the full df (with team rows) feeds Elo and the standings table
    players = df[(df["position"] != "team") & df["champion"].notna()].copy()
    players_all = df_all[(df_all["position"] != "team")
                         & df_all["champion"].notna()].copy()

    now = pd.Timestamp.now()
    for pf in (players, players_all):
        age_days = (now - pf["date"]).dt.total_seconds() / 86400
        pf["w"] = np.exp(-age_days * math.log(2) / HALF_LIFE_DAYS)
        pf["is_short"] = pf["gamelength"] < SHORT_MAX
        pf["is_long"] = pf["gamelength"] > LONG_MIN
    gd15 = "golddiffat15" if "golddiffat15" in players else None

    def champ_table(sub):
        out = {}
        for champ, g in sub.groupby("champion"):
            w = g["w"]
            tot = w.sum()
            if tot < 3:   # too little weighted evidence
                continue
            wr = float((g["result"] * w).sum() / tot)
            entry = {
                "games": int(len(g)),
                "weight": round(float(tot), 1),
                "wr": round(wr, 4),
                "pos": g["position"].mode().iat[0],
            }
            if gd15:
                gv = g.dropna(subset=[gd15])
                if len(gv) >= 3:
                    entry["gd15"] = round(float(
                        (gv[gd15] * gv["w"]).sum() / gv["w"].sum()), 1)
            for key, mask in (("short", g["is_short"]), ("long", g["is_long"])):
                sel = g[mask]
                wsel = sel["w"].sum()
                if wsel >= 2:
                    entry[f"wr_{key}"] = round(float(
                        (sel["result"] * sel["w"]).sum() / wsel), 4)
                    entry[f"n_{key}"] = int(len(sel))
            out[champ] = entry
        return out

    result = {"meta": {
        "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "years": years, "leagues": leagues,
        "games": int(players["gameid"].nunique()),
        "half_life_days": HALF_LIFE_DAYS,
    }, "leagues": {}}
    for lg in leagues:
        result["leagues"][lg] = champ_table(players[players["league"] == lg])
        print(f"[{lg}] {len(result['leagues'][lg])} champions with data")
    result["leagues"]["ALL"] = champ_table(players)
    print(f"[ALL] {len(result['leagues']['ALL'])} champions")
    # GLOBAL = every pro league OE tracks; the fallback tier for brand-new
    # champions (e.g. a champ released after our big-3 data cutoff shows up
    # in ERLs weeks before LCK/LPL/LEC play it)
    result["leagues"]["GLOBAL"] = champ_table(players_all)
    result["all_champions"] = sorted(players_all["champion"].unique().tolist())
    print(f"[GLOBAL] {len(result['leagues']['GLOBAL'])} champions | "
          f"selector list: {len(result['all_champions'])}")

    team_rows, oe_since, lp = unified_team_results(df, leagues)
    elo_pre, elo_now = compute_elo(team_rows)
    result["teams"] = team_table(team_rows, leagues, elo_now)
    result["meta"]["freshness"] = {
        lg: {"oe": oe_since.get(lg, "?")[:10],
             "leaguepedia": (str(lp[lp["league"] == lg]["date"].max())[:10]
                             if not lp.empty and (lp["league"] == lg).any()
                             else None)}
        for lg in leagues}
    result["model"] = fit_model(players, gd15, elo_pre)

    out_path = os.path.join(BASE_DIR, "champion_stats.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    print("wrote", out_path)


ELO_K = 20.0
ELO_START = 1500.0
LP_CACHE = os.path.join(DATA_DIR, "leaguepedia_cache.json")
LP_CACHE_MAX_AGE_H = 6


def fetch_leaguepedia(leagues, since_by_league, oe_team_names):
    """Recent team results from Leaguepedia (lol.fandom.com Cargo API).

    The OE files lag weeks behind for LCK/LPL, so standings/form/Elo would be
    stale without this supplement. Only team-level results are pulled -
    champion stats stay OE-based. Cached on disk (rate limits are strict).
    Returns a DataFrame shaped like the OE team-results frame.
    """
    cached = {"fetched_at": None, "rows": []}
    if os.path.exists(LP_CACHE):
        with open(LP_CACHE, encoding="utf-8") as f:
            cached = json.load(f)
        age_h = (datetime.datetime.now()
                 - datetime.datetime.fromisoformat(cached["fetched_at"])
                 ).total_seconds() / 3600
        if age_h > LP_CACHE_MAX_AGE_H:
            cached = {"fetched_at": None, "rows": []}

    # per-league staleness: refetch a league if the cache has nothing for it
    # (e.g. an earlier run got rate-limited on that league only)
    have = {lg: [x for x in cached["rows"] if x["league"] == lg] for lg in leagues}
    todo = [lg for lg in leagues if not have[lg]]

    if todo:
        rows = [x for x in cached["rows"] if x["league"] not in todo]
        sess = requests.Session()
        sess.headers["User-Agent"] = "draft-analyzer-research/1.0 (personal project)"
        for lg in todo:
            since = since_by_league.get(lg)
            year = (since or str(datetime.date.today().year))[:4]
            # narrow LIKE (league/year) keeps the Cargo scan small - the broad
            # "{lg}/%" pattern on big leagues (LPL) tends to fail server-side
            where = (f'ScoreboardGames.OverviewPage LIKE "{lg}/{year}%"'
                     + (f' AND ScoreboardGames.DateTime_UTC > "{since}"'
                        if since else ""))
            offset = 0
            for _page in range(6):  # up to 3000 games, plenty
                for attempt in range(4):
                    r = sess.get("https://lol.fandom.com/api.php", params={
                        "action": "cargoquery", "format": "json", "limit": 500,
                        "offset": offset, "tables": "ScoreboardGames",
                        "fields": ("ScoreboardGames.Team1,ScoreboardGames.Team2,"
                                   "ScoreboardGames.WinTeam,"
                                   "ScoreboardGames.DateTime_UTC,"
                                   "ScoreboardGames.OverviewPage"),
                        "where": where,
                        "order_by": "ScoreboardGames.DateTime_UTC",
                    }, timeout=30)
                    d = r.json()
                    if "error" not in d:
                        break
                    print(f"[LP {lg}] rate limited, waiting 65s "
                          f"(attempt {attempt + 1}/4)...")
                    time.sleep(65)
                else:
                    print(f"[LP {lg}] giving up (rate limit) - standings may be stale")
                    d = {"cargoquery": []}
                batch = d.get("cargoquery", [])
                for row in batch:
                    t = row["title"]
                    rows.append({"league": lg, "date": t["DateTime UTC"],
                                 "team1": t["Team1"], "team2": t["Team2"],
                                 "winner": t["WinTeam"],
                                 "tour": t["OverviewPage"]})
                if len(batch) < 500:
                    break
                offset += 500
                time.sleep(2)
            print(f"[LP {lg}] {sum(1 for x in rows if x['league'] == lg)} "
                  f"recent games from Leaguepedia")
        cached = {"fetched_at": datetime.datetime.now().isoformat(), "rows": rows}
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LP_CACHE, "w", encoding="utf-8") as f:
            json.dump(cached, f, ensure_ascii=False)
    else:
        print(f"[LP] using cached Leaguepedia data ({len(cached['rows'])} rows)")

    # map LP team names onto OE team names (normalized), keep unknowns as-is
    def norm(s):
        return "".join(ch for ch in (s or "").lower() if ch.isalnum())
    oe_by_norm = {norm(t): t for t in oe_team_names}

    recs = []
    for i, g in enumerate(cached["rows"]):
        t1 = oe_by_norm.get(norm(g["team1"]), g["team1"])
        t2 = oe_by_norm.get(norm(g["team2"]), g["team2"])
        win = oe_by_norm.get(norm(g["winner"]), g["winner"])
        tour = g["tour"].split("/", 1)[-1]  # "LCK/2026 Season/Rounds 3-4" -> tail
        for tm, side in ((t1, "Blue"), (t2, "Red")):
            recs.append({"gameid": f"LP{i}", "league": g["league"],
                         "date": g["date"], "teamname": tm, "side": side,
                         "result": 1 if tm == win else 0,
                         "tour": tour, "golddiffat15": None})
    lp = pd.DataFrame(recs)
    if not lp.empty:
        lp["date"] = pd.to_datetime(lp["date"], errors="coerce")
        lp = lp.dropna(subset=["date"])
    return lp


def unified_team_results(df, leagues):
    """OE team rows + Leaguepedia supplement in one frame."""
    oe = df[(df["position"] == "team")].dropna(subset=["teamname"]).copy()
    oe["tour"] = oe.get("split", pd.Series(index=oe.index, dtype=object)).fillna("?") \
        .astype(str) + " " + oe["date"].dt.year.astype(str)
    oe = oe[["gameid", "league", "date", "teamname", "side", "result",
             "tour", "golddiffat15"]] if "golddiffat15" in oe else oe
    since = {lg: str(oe[oe["league"] == lg]["date"].max())
             for lg in leagues if not oe[oe["league"] == lg].empty}
    try:
        lp = fetch_leaguepedia(leagues, since, oe["teamname"].unique())
    except Exception as e:
        print(f"[LP] supplement failed ({e}) - continuing with OE data only")
        lp = pd.DataFrame()
    combined = pd.concat([oe, lp], ignore_index=True) if not lp.empty else oe
    return combined.sort_values("date"), since, lp


def compute_elo(team_rows):
    """Chronological Elo over the unified team-results frame."""
    rating = {}
    elo_pre = {}
    for gid, g in team_rows.groupby("gameid", sort=False):
        if len(g) != 2:
            continue
        a, b = g.iloc[0], g.iloc[1]
        ra = rating.get(a["teamname"], ELO_START)
        rb = rating.get(b["teamname"], ELO_START)
        elo_pre[(gid, a["side"])] = ra
        elo_pre[(gid, b["side"])] = rb
        ea = 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))
        sa = float(a["result"])
        rating[a["teamname"]] = ra + ELO_K * (sa - ea)
        rating[b["teamname"]] = rb + ELO_K * ((1 - sa) - (1 - ea))
    return elo_pre, rating


def team_table(team_rows, leagues, elo_now):
    """Per-league team standings: current split record, form, Elo, gd15."""
    out = {}
    for lg in leagues:
        sub = team_rows[team_rows["league"] == lg]
        if sub.empty:
            out[lg] = {}
            continue
        # "current split" = the tournament label of the league's newest game
        cur_tour = sub.iloc[-1]["tour"]
        cur = sub[sub["tour"] == cur_tour]
        entry = {}
        for tm, g in sub.groupby("teamname"):
            gc = cur[cur["teamname"] == tm]
            recent = g.tail(10)
            e = {
                "elo": round(float(elo_now.get(tm, ELO_START)), 1),
                "split_w": int(gc["result"].sum()),
                "split_l": int(len(gc) - gc["result"].sum()),
                "form10": round(float(recent["result"].mean()), 3) if len(recent) else None,
                "games": int(len(g)),
                "last_date": str(g["date"].max().date()),
            }
            if "golddiffat15" in g:
                gv = g.dropna(subset=["golddiffat15"]).tail(20)
                if len(gv) >= 5:
                    e["gd15"] = round(float(gv["golddiffat15"].mean()), 1)
            entry[tm] = e
        out[lg] = entry
        print(f"[{lg}] {len(entry)} teams (current: {cur_tour})")
    return out


def fit_model(players, gd15, elo_pre):
    """Logistic regression on team-level draft aggregates, chronological split.

    Leakage control: champion stats used as features are computed from the
    TRAIN period only; the eval period sees them as-of, never its own outcomes.
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import brier_score_loss

    players = players.sort_values("date")
    cut = players["date"].quantile(0.8)
    train_p = players[players["date"] <= cut]
    eval_p = players[players["date"] > cut]

    def champ_lookup(sub):
        look = {}
        for champ, g in sub.groupby("champion"):
            w = g["w"].sum()
            if w < 3:
                continue
            d = {"wr": (g["result"] * g["w"]).sum() / w}
            sh = g[g["is_short"]]; lo = g[g["is_long"]]
            d["wr_short"] = ((sh["result"] * sh["w"]).sum() / sh["w"].sum()
                             if sh["w"].sum() >= 2 else d["wr"])
            d["wr_long"] = ((lo["result"] * lo["w"]).sum() / lo["w"].sum()
                            if lo["w"].sum() >= 2 else d["wr"])
            if gd15:
                gv = g.dropna(subset=[gd15])
                d["gd15"] = ((gv[gd15] * gv["w"]).sum() / gv["w"].sum()
                             if len(gv) >= 3 else 0.0)
            else:
                d["gd15"] = 0.0
            look[champ] = d
        return look

    look = champ_lookup(train_p)

    def game_rows(sub):
        X, y = [], []
        for (gid, side), g in sub.groupby(["gameid", "side"]):
            feats = [look.get(c) for c in g["champion"] if look.get(c)]
            if len(feats) < 4:
                continue
            X.append([
                np.mean([f["wr"] for f in feats]) - 0.5,
                np.mean([f["gd15"] for f in feats]) / 100.0,
                np.mean([f["wr_long"] - f["wr_short"] for f in feats]),
                1.0 if side == "Blue" else 0.0,
                (elo_pre.get((gid, side), 1500.0) - 1500.0) / 200.0,
            ])
            y.append(int(g["result"].iloc[0]))
        return np.array(X), np.array(y)

    Xtr, ytr = game_rows(train_p)
    Xev, yev = game_rows(eval_p)
    if len(Xtr) < 200 or len(Xev) < 50:
        print("model: not enough games for a meaningful fit - skipping")
        return None
    m = LogisticRegression()
    m.fit(Xtr, ytr)
    p_ev = m.predict_proba(Xev)[:, 1]
    brier = brier_score_loss(yev, p_ev)
    base = brier_score_loss(yev, np.full(len(yev), ytr.mean()))
    print(f"model: eval Brier {brier:.4f} vs baseline {base:.4f} "
          f"(train n={len(Xtr)}, eval n={len(Xev)})")
    return {
        "coef": [round(float(c), 5) for c in m.coef_[0]],
        "intercept": round(float(m.intercept_[0]), 5),
        "features": ["avg_wr-0.5", "avg_gd15/100", "scaling_tilt", "blue_side",
                     "elo_pre_scaled"],
        "brier_eval": round(float(brier), 4),
        "brier_baseline": round(float(base), 4),
        "n_train": int(len(Xtr)), "n_eval": int(len(Xev)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--years", type=int, nargs="*", default=DEFAULT_YEARS)
    ap.add_argument("--leagues", nargs="*", default=DEFAULT_LEAGUES)
    args = ap.parse_args()
    if not args.download and not args.build:
        ap.error("use --download and/or --build")
    if args.download:
        download(args.years)
    if args.build:
        build(args.years, args.leagues)


if __name__ == "__main__":
    main()
