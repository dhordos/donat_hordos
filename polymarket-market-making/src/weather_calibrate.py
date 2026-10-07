#!/usr/bin/env python3
"""Per-city temperature offset calibration for the weather model.

The markets resolve on official STATION readings (usually airports); our
ensemble is queried at city-center grid points. The systematic difference is
the main reason the raw model loses to the market on exact 1-degree buckets.

Trick: every resolved bucket market IS a station measurement - the winning
bucket tells us the official reading for that city+date. We compare those
readings with Open-Meteo's archived forecast (same grid point as our live
ensemble) and fit a per-city offset:

    offset(city) = median(actual_station - forecast_grid)

Output: weather_offsets.json - weather_model.py applies it automatically.

Usage (needs Polymarket API access for resolved markets):
  python weather_calibrate.py [--pages 6]
"""

import argparse
import collections
import datetime
import json
import logging
import os
import statistics
import time

import requests

from weather_model import CITIES, GAMMA_API, parse_question, target_date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HIST_API = "https://historical-forecast-api.open-meteo.com/v1/forecast"
OUT_PATH = os.path.join(BASE_DIR, "weather_offsets.json")
log = logging.getLogger("wcal")


def f_to_c(f):
    return (f - 32.0) * 5.0 / 9.0


def collect_actuals(session, pages):
    """(city, date) -> actual station max temp in C, from resolved markets."""
    actuals = {}
    conflicts = 0
    for page in range(pages):
        try:
            r = session.get(GAMMA_API + "/events", params={
                "tag_slug": "weather", "closed": "true", "limit": 100,
                "offset": page * 100, "order": "endDate", "ascending": "false",
            }, timeout=30)
            events = r.json()
        except Exception as e:
            log.warning("event page %d failed: %s", page, e)
            continue
        if not events:
            break
        for e in events:
            for m in e.get("markets") or []:
                q = m.get("question") or ""
                if "temperature" not in q.lower():
                    continue
                parsed = parse_question(q)
                if not parsed or parsed["city"] not in CITIES:
                    continue
                if parsed["metric"] != "highest":
                    continue  # daily-max only; lows could be added later
                try:
                    outs = json.loads(m.get("outcomes") or "[]")
                    prices = json.loads(m.get("outcomePrices") or "[]")
                except ValueError:
                    continue
                if "Yes" not in outs or len(prices) != len(outs):
                    continue
                yes_won = float(prices[outs.index("Yes")]) > 0.5
                if not yes_won:
                    continue  # only the winning bucket pins the reading
                if parsed["cond"] == "exact":
                    val = parsed["lo"]
                elif parsed["cond"] == "range":
                    val = (parsed["lo"] + parsed["hi"]) / 2.0
                else:
                    continue  # gte/lte only bound the value, skip
                val_c = f_to_c(val) if parsed["unit"] == "F" else val
                d = target_date(parsed["month"], parsed["day"])
                key = (parsed["city"], d.isoformat())
                if key in actuals and abs(actuals[key] - val_c) > 0.6:
                    conflicts += 1
                    continue
                actuals[key] = val_c
        log.info("page %d done, %d city-days so far", page, len(actuals))
    if conflicts:
        log.warning("%d conflicting city-days skipped", conflicts)
    return actuals


def fetch_forecast_series(session, city, dates):
    lat, lon = CITIES[city]
    try:
        r = session.get(HIST_API, params={
            "latitude": lat, "longitude": lon,
            "start_date": min(dates), "end_date": max(dates),
            "daily": "temperature_2m_max", "timezone": "auto"}, timeout=30)
        r.raise_for_status()
        daily = r.json().get("daily", {})
        return dict(zip(daily.get("time", []),
                        daily.get("temperature_2m_max", [])))
    except Exception as e:
        log.warning("forecast history failed for %s: %s", city, e)
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=6,
                    help="pages of 100 closed events to scan")
    ap.add_argument("--min-days", type=int, default=4,
                    help="min city-days needed to trust a city's offset")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    session = requests.Session()
    session.headers["User-Agent"] = "polymarket-weather-calibrate/1.0 (research)"

    actuals = collect_actuals(session, args.pages)
    print("=" * 74)
    print("STATION OFFSET CALIBRATION - {} resolved city-days".format(len(actuals)))
    print("=" * 74)
    if not actuals:
        print("No resolved bucket markets found - nothing to calibrate.")
        return

    by_city = collections.defaultdict(list)
    today = datetime.date.today().isoformat()
    for (city, d), val in sorted(actuals.items()):
        if d >= today:
            continue  # historical forecast API lags ~1 day
        by_city[city].append((d, val))

    offsets = {}
    print("  {:<18} {:>5} {:>9} {:>7}   {}".format(
        "city", "days", "offset C", "spread", "verdict"))
    print("  " + "-" * 62)
    for city, obs in sorted(by_city.items()):
        dates = [d for d, _ in obs]
        fc = fetch_forecast_series(session, city, dates)
        diffs = [val - fc[d] for d, val in obs if fc.get(d) is not None]
        time.sleep(0.3)
        if len(diffs) < args.min_days:
            print("  {:<18} {:>5}   (not enough data yet)".format(city, len(diffs)))
            continue
        off = statistics.median(diffs)
        spread = statistics.pstdev(diffs)
        offsets[city] = {"offset_c": round(off, 2), "n": len(diffs),
                         "spread": round(spread, 2)}
        verdict = ("strong" if abs(off) >= 0.7 and spread < 1.6 else
                   "mild" if abs(off) >= 0.3 else "negligible")
        print("  {:<18} {:>5} {:>+9.2f} {:>7.2f}   {}".format(
            city, len(diffs), off, spread, verdict))

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"built_at": datetime.datetime.now().isoformat(timespec="seconds"),
                   "offsets": offsets}, f, ensure_ascii=False, indent=1)
    print()
    print("wrote {} ({} cities calibrated)".format(OUT_PATH, len(offsets)))
    print("weather_model.py picks this up automatically on the next snapshot.")
    print()
    print("NOTE: offset = station_actual - grid_forecast. It bundles the pure")
    print("station-location bias with average forecast error; with more days the")
    print("estimate sharpens. Re-run weekly as data accumulates.")


if __name__ == "__main__":
    main()
