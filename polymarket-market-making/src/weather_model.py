#!/usr/bin/env python3
"""Weather forecast model vs Polymarket prices - forward test.

For every open temperature market: parse the question (city, date, threshold),
compute the probability from the Open-Meteo ensemble (ECMWF+GFS, ~80 members),
compare with the market's midpoint, and log the snapshot. Later, --score
fetches actual resolutions and reports who priced better: our model or the
market (Brier score), plus the PnL a "trade when edge > X" rule would earn.

Usage:

  python weather_model.py           # snapshot
  python weather_model.py --score   # evaluate
"""

import argparse
import datetime
import json
import logging
import os
import re
import sqlite3
import time

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
ENSEMBLE_API = "https://ensemble-api.open-meteo.com/v1/ensemble"
log = logging.getLogger("wmodel")

# City coordinates for the cities Polymarket lists temperature markets on.
# Unknown cities are skipped (and reported) - extend as needed.
CITIES = {
    "london": (51.5072, -0.1276), "amsterdam": (52.3676, 4.9041),
    "paris": (48.8566, 2.3522), "madrid": (40.4168, -3.7038),
    "milan": (45.4642, 9.1900), "munich": (48.1351, 11.5820),
    "ankara": (39.9334, 32.8597), "moscow": (55.7558, 37.6173),
    "tel aviv": (32.0853, 34.7818), "karachi": (24.8607, 67.0011),
    "lucknow": (26.8467, 80.9462), "delhi": (28.6139, 77.2090),
    "new delhi": (28.6139, 77.2090), "mumbai": (19.0760, 72.8777),
    "singapore": (1.3521, 103.8198), "kuala lumpur": (3.1390, 101.6869),
    "manila": (14.5995, 120.9842), "hong kong": (22.3193, 114.1694),
    "shanghai": (31.2304, 121.4737), "beijing": (39.9042, 116.4074),
    "wuhan": (30.5928, 114.3055), "chengdu": (30.5728, 104.0668),
    "qingdao": (36.0671, 120.3826), "guangzhou": (23.1291, 113.2644),
    "shenzhen": (22.5431, 114.0579), "busan": (35.1796, 129.0756),
    "seoul": (37.5665, 126.9780), "tokyo": (35.6762, 139.6503),
    "osaka": (34.6937, 135.5023), "toronto": (43.6532, -79.3832),
    "new york city": (40.7128, -74.0060), "miami": (25.7617, -80.1918),
    "dallas": (32.7767, -96.7970), "houston": (29.7604, -95.3698),
    "chicago": (41.8781, -87.6298), "denver": (39.7392, -104.9903),
    "los angeles": (34.0522, -118.2437), "seattle": (47.6062, -122.3321),
    "atlanta": (33.7490, -84.3880), "phoenix": (33.4484, -112.0740),
    # added 2026-08-09: both appear in live markets but were missing here.
    "austin": (30.2672, -97.7431), "san francisco": (37.7749, -122.4194),
    "sao paulo": (-23.5505, -46.6333), "buenos aires": (-34.6037, -58.3816),
    "mexico city": (19.4326, -99.1332), "sydney": (-33.8688, 151.2093),
    "melbourne": (-37.8136, 144.9631), "wellington": (-41.2866, 174.7756),
    "istanbul": (41.0082, 28.9784), "cairo": (30.0444, 31.2357),
    "lagos": (6.5244, 3.3792), "johannesburg": (-26.2041, 28.0473),
    "bangkok": (13.7563, 100.5018), "jakarta": (-6.2088, 106.8456),
    "hanoi": (21.0278, 105.8342), "taipei": (25.0330, 121.5654),
    "seoul (incheon)": (37.4563, 126.7052), "chongqing": (29.5630, 106.5516),
    "helsinki": (60.1699, 24.9384), "jeddah": (21.4858, 39.1925),
    "cape town": (-33.9249, 18.4241), "warsaw": (52.2297, 21.0122),
    "jinan": (36.6512, 117.1201), "zhengzhou": (34.7466, 113.6253),
}

MONTHS = {m.lower(): i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}

DEG = "°"
Q_RE = re.compile(
    r"will the (highest|lowest) temperature in (.+?) be "
    r"(?:between (-?\d+)-(-?\d+)|(-?\d+))" + DEG + r"([CF])"
    r"(?: or (higher|lower|below|above))? on (\w+) (\d+)",
    re.IGNORECASE)


def parse_question(q):
    """-> dict(metric, city, cond, lo, hi, unit, month, day) or None."""
    m = Q_RE.search(q or "")
    if not m:
        return None
    metric, city, rlo, rhi, single, unit, qual, month, day = m.groups()
    month_n = MONTHS.get(month.lower())
    if not month_n:
        return None
    out = {"metric": metric.lower(), "city": city.strip().lower(),
           "unit": unit.upper(), "month": month_n, "day": int(day)}
    if rlo is not None:  # "between 90-91°F"
        out["cond"] = "range"
        out["lo"], out["hi"] = float(rlo), float(rhi)
    elif qual:
        qual = qual.lower()
        out["cond"] = "gte" if qual in ("higher", "above") else "lte"
        out["lo"] = out["hi"] = float(single)
    else:  # exact integer bucket
        out["cond"] = "exact"
        out["lo"] = out["hi"] = float(single)
    return out


def target_date(month, day, today=None):
    today = today or datetime.date.today()
    year = today.year
    d = datetime.date(year, month, day)
    # a market can only be about the near future (or today); if the parsed
    # date looks far in the past, it belongs to next year
    if (today - d).days > 60:
        d = datetime.date(year + 1, month, day)
    return d


def member_daily_extreme(hourly, member_key, date_iso, metric):
    times = hourly.get("time") or []
    vals = hourly.get(member_key) or []
    day_vals = [v for t, v in zip(times, vals)
                if v is not None and t.startswith(date_iso)]
    if not day_vals:
        return None
    return max(day_vals) if metric == "highest" else min(day_vals)


def load_offsets():
    """Per-city station offsets from weather_calibrate.py (optional)."""
    p = os.path.join(BASE_DIR, "weather_offsets.json")
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    return {c: v["offset_c"] for c, v in d.get("offsets", {}).items()}


class Ensemble:
    """One Open-Meteo fetch per city per run, shared across its markets."""

    def __init__(self, session):
        self.session = session
        self.cache = {}
        self.offsets = load_offsets()
        if self.offsets:
            log.info("station offsets loaded for %d cities", len(self.offsets))

    def city_members(self, city, date_iso, metric):
        key = city
        if key not in self.cache:
            lat, lon = CITIES[city]
            try:
                r = self.session.get(ENSEMBLE_API, params={
                    "latitude": lat, "longitude": lon,
                    "hourly": "temperature_2m",
                    "models": "ecmwf_ifs025,gfs025",
                    "forecast_days": 7, "timezone": "auto"}, timeout=30)
                r.raise_for_status()
                self.cache[key] = r.json().get("hourly") or {}
            except Exception as e:
                log.warning("ensemble fetch failed for %s: %s", city, e)
                self.cache[key] = {}
            time.sleep(0.2)
        hourly = self.cache[key]
        off = self.offsets.get(city, 0.0)
        members = []
        for k in hourly:
            if not k.startswith("temperature_2m"):
                continue
            ext = member_daily_extreme(hourly, k, date_iso, metric)
            if ext is not None:
                members.append(ext + off)
        return members


def probability(members, cond, lo, hi, unit):
    """P(outcome) from member extremes (values in °C from Open-Meteo)."""
    if not members:
        return None
    if unit == "F":
        members = [m * 9 / 5 + 32 for m in members]
    # official readings are rounded to whole degrees -> half-degree bounds
    if cond == "exact":
        hit = [m for m in members if lo - 0.5 <= m < lo + 0.5]
    elif cond == "range":
        hit = [m for m in members if lo - 0.5 <= m < hi + 0.5]
    elif cond == "gte":
        hit = [m for m in members if m >= lo - 0.5]
    else:  # lte
        hit = [m for m in members if m < lo + 0.5]
    p = len(hit) / len(members)
    # clamp: an 80-member ensemble cannot honestly claim 0% or 100%
    return min(max(p, 0.01), 0.99)


def open_db():
    db = sqlite3.connect(os.path.join(BASE_DIR, "weather_model.db"))
    db.executescript("""
    CREATE TABLE IF NOT EXISTS predictions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, condition_id TEXT, question TEXT,
        city TEXT, target_date TEXT, cond TEXT, unit TEXT,
        model_prob REAL, market_mid REAL, yes_token TEXT,
        n_members INTEGER, final REAL
    );
    CREATE INDEX IF NOT EXISTS idx_pred_cid ON predictions(condition_id);
    """)
    cols = [r[1] for r in db.execute("PRAGMA table_info(predictions)")]
    if "calibrated" not in cols:
        db.execute("ALTER TABLE predictions ADD COLUMN calibrated INTEGER DEFAULT 0")
    db.commit()
    return db


def snapshot(db, session):
    ens = Ensemble(session)
    seen = skipped_city = skipped_parse = skipped_decided = 0
    unknown_cities = {}
    rows = []
    for page in range(3):
        try:
            r = session.get(GAMMA_API + "/events", params={
                "tag_slug": "weather", "closed": "false",
                "limit": 100, "offset": page * 100}, timeout=30)
            events = r.json()
        except Exception as e:
            log.warning("event page %d failed: %s", page, e)
            continue
        if not events:
            break
        for e in events:
            for m in e.get("markets") or []:
                q = m.get("question") or ""
                if "temperature" not in q.lower() or not m.get("active"):
                    continue
                seen += 1
                parsed = parse_question(q)
                if not parsed:
                    skipped_parse += 1
                    continue
                if parsed["city"] not in CITIES:
                    skipped_city += 1
                    unknown_cities[parsed["city"]] = \
                        unknown_cities.get(parsed["city"], 0) + 1
                    continue
                date = target_date(parsed["month"], parsed["day"])
                if (date - datetime.date.today()).days > 6:
                    continue  # beyond the forecast horizon
                members = ens.city_members(
                    parsed["city"], date.isoformat(), parsed["metric"])
                p_model = probability(members, parsed["cond"],
                                      parsed["lo"], parsed["hi"], parsed["unit"])
                if p_model is None:
                    continue
                try:
                    toks = json.loads(m.get("clobTokenIds") or "[]")
                    outs = json.loads(m.get("outcomes") or "[]")
                except ValueError:
                    continue
                if len(toks) != 2:
                    continue
                yes_tok = toks[outs.index("Yes")] if "Yes" in outs else toks[0]
                try:
                    mr = session.get(CLOB_API + "/midpoint",
                                     params={"token_id": yes_tok}, timeout=15)
                    mid = float(mr.json().get("mid"))
                except Exception:
                    continue
                if mid >= 0.93 or mid <= 0.07:
                    # market is effectively decided (the day is over in that
                    # city, or the outcome is obvious) - the model would be
                    # "disagreeing" with a known result, which is meaningless
                    # to trade and would poison the forward-test scoring
                    skipped_decided += 1
                    continue
                now = int(time.time())
                db.execute(
                    """INSERT INTO predictions
                       (ts, condition_id, question, city, target_date, cond,
                        unit, model_prob, market_mid, yes_token, n_members,
                        calibrated)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (now, m.get("conditionId"), q, parsed["city"],
                     date.isoformat(), parsed["cond"], parsed["unit"],
                     p_model, mid, yes_tok, len(members),
                     1 if parsed["city"] in ens.offsets else 0))
                rows.append((q, p_model, mid))
                time.sleep(0.1)
    db.commit()

    rows.sort(key=lambda r: -abs(r[1] - r[2]))
    print("=" * 76)
    print("WEATHER MODEL SNAPSHOT - {} markets scanned, {} predicted".format(
        seen, len(rows)))
    print("(skipped: {} unparsable, {} unknown city, {} already decided)".format(
        skipped_parse, skipped_city, skipped_decided))
    if unknown_cities:
        top = sorted(unknown_cities.items(), key=lambda x: -x[1])[:8]
        print("(top unknown cities: {})".format(
            ", ".join("{} x{}".format(c, n) for c, n in top)))
    print("=" * 76)
    print("Biggest disagreements (model vs market):")
    print("  {:>6} {:>7} {:>7}  {}".format("edge", "model", "market", "question"))
    for q, pm, mid in rows[:15]:
        print("  {:>+5.2f} {:>7.2f} {:>7.2f}  {}".format(
            pm - mid, pm, mid, q.encode("ascii", "replace").decode()[:52]))
    print()
    print("Snapshot logged. Run --score after markets resolve (1-2+ days).")


def score(db, session):
    unresolved = db.execute(
        """SELECT DISTINCT condition_id FROM predictions
           WHERE final IS NULL AND condition_id IS NOT NULL""").fetchall()
    for (cid,) in unresolved:
        try:
            r = session.get(CLOB_API + "/markets/" + cid, timeout=15)
            if r.status_code != 200:
                continue
            m = r.json()
        except Exception:
            continue
        if not m.get("closed"):
            continue
        yes_final = None
        for tok in m.get("tokens") or []:
            if tok.get("outcome") == "Yes":
                yes_final = float(tok.get("price") or 0)
        if yes_final is None:
            continue
        db.execute("UPDATE predictions SET final=? WHERE condition_id=?",
                   (yes_final, cid))
        time.sleep(0.1)
    db.commit()

    rows = db.execute(
        """SELECT model_prob, market_mid, final, calibrated FROM predictions
           WHERE final IS NOT NULL""").fetchall()
    print("=" * 76)
    print("FORWARD-TEST SCORE - {} resolved predictions".format(len(rows)))
    print("=" * 76)
    if len(rows) < 20:
        print("Not enough resolved predictions yet - keep snapshotting daily.")
        return
    def brier(pairs):
        return sum((p - (1.0 if f > 0.5 else 0.0)) ** 2
                   for p, f in pairs) / len(pairs)
    for label, sel in (("UNCALIBRATED era", [r for r in rows if not r[3]]),
                       ("CALIBRATED era  ", [r for r in rows if r[3]])):
        if len(sel) < 20:
            print("{}: only {} resolved - waiting for more".format(
                label, len(sel)))
            continue
        b_model = brier([(pm, f) for pm, _, f, _ in sel])
        b_market = brier([(mid, f) for _, mid, f, _ in sel])
        print("{}: n={:>5} | model {:.4f} vs market {:.4f} -> {}".format(
            label, len(sel), b_model, b_market,
            "MODEL wins" if b_model < b_market else "market wins"))
    b_model = brier([(pm, f) for pm, _, f, _ in rows])
    b_market = brier([(mid, f) for _, mid, f, _ in rows])
    print()
    print("Rule backtest: buy Yes when model - market > edge, buy No when")
    print("market - model > edge (payout $1, cost = mid, no spread modeled):")
    for edge in (0.05, 0.10, 0.20):
        pnl = 0.0
        n = 0
        for pm, mid, f, _cal in rows:
            won = 1.0 if f > 0.5 else 0.0
            if pm - mid > edge:
                pnl += won - mid
                n += 1
            elif mid - pm > edge:
                pnl += (1 - won) - (1 - mid)
                n += 1
        avg = (pnl / n) if n else 0.0
        print("  edge>{:.2f}: {:>4} trades, total {:+.2f}, avg {:+.4f} per $~1".format(
            edge, n, pnl, avg))
    print()
    print("Caveat: mid-price fills, no spread/slippage - treat as upper bound.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    db = open_db()
    session = requests.Session()
    session.headers["User-Agent"] = "polymarket-weather-model/1.0 (research)"
    if args.score:
        score(db, session)
    else:
        snapshot(db, session)


if __name__ == "__main__":
    main()
