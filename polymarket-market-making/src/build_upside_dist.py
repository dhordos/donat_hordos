#!/usr/bin/env python3
"""Station map (from market rules) + empirical daily-max upside distributions.

STATIONS below are read off the markets' own resolution text (see
verify_stations.py output, 2026-08-07) - NOT guessed by distance. Distance
guessing was wrong for about half the cities (NYC's nearest ASOS is a
Manhattan heliport; the market resolves on LaGuardia).

The markets resolve on Weather Underground's 'Daily Observations' table,
which is the raw METAR observation stream - the same data IEM archives and
that we can read live. That is what makes the watcher idea possible.

Output: upside_dist.json
  {station: {"city":..., "hours": {hour: {"n":..., "q": [sorted upsides]}}}}
where upside = final_daily_max - running_max_at_that_hour, in the station's
reporting unit (F for US stations via tmpf; we always fetch tmpf and convert
where the market is in C).

Usage: python build_upside_dist.py [--years 3]
"""

import argparse
import json
import os
import time
from collections import defaultdict

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE_DIR, "upside_dist.json")

# city (as weather_model.parse_question emits it) -> (IEM station id, tz)
# ICAO from the rules text / wunderground URL; IEM drops the leading K for US.
STATIONS = {
    "amsterdam":       ("EHAM", "Europe/Amsterdam"),
    "ankara":          ("LTAC", "Europe/Istanbul"),
    "atlanta":         ("ATL",  "America/New_York"),
    "austin":          ("AUS",  "America/Chicago"),
    "beijing":         ("ZBAA", "Asia/Shanghai"),
    "buenos aires":    ("SAEZ", "America/Argentina/Buenos_Aires"),
    "busan":           ("RKPK", "Asia/Seoul"),
    "cape town":       ("FACT", "Africa/Johannesburg"),
    "chengdu":         ("ZUUU", "Asia/Shanghai"),
    "chicago":         ("ORD",  "America/Chicago"),
    "chongqing":       ("ZUCK", "Asia/Shanghai"),
    "dallas":          ("DAL",  "America/Chicago"),
    "denver":          ("BKF",  "America/Denver"),
    "guangzhou":       ("ZGGG", "Asia/Shanghai"),
    "helsinki":        ("EFHK", "Europe/Helsinki"),
    "houston":         ("HOU",  "America/Chicago"),
    "jeddah":          ("OEJN", "Asia/Riyadh"),
    "karachi":         ("OPKC", "Asia/Karachi"),
    "kuala lumpur":    ("WMKK", "Asia/Kuala_Lumpur"),
    "london":          ("EGLC", "Europe/London"),
    "los angeles":     ("LAX",  "America/Los_Angeles"),
    "lucknow":         ("VILK", "Asia/Kolkata"),
    "madrid":          ("LEMD", "Europe/Madrid"),
    "manila":          ("RPLL", "Asia/Manila"),
    "miami":           ("MIA",  "America/New_York"),
    "milan":           ("LIMC", "Europe/Rome"),
    "munich":          ("EDDM", "Europe/Berlin"),
    "new york city":   ("LGA",  "America/New_York"),
    "paris":           ("LFPB", "Europe/Paris"),
    "qingdao":         ("ZSQD", "Asia/Shanghai"),
    "san francisco":   ("SFO",  "America/Los_Angeles"),
    "sao paulo":       ("SBGR", "America/Sao_Paulo"),
    "seattle":         ("SEA",  "America/Los_Angeles"),
    "seoul (incheon)": ("RKSI", "Asia/Seoul"),
    "shanghai":        ("ZSPD", "Asia/Shanghai"),
    "shenzhen":        ("ZGSZ", "Asia/Shanghai"),
    "singapore":       ("WSSS", "Asia/Singapore"),
    "taipei":          ("RCSS", "Asia/Taipei"),
    "tokyo":           ("RJTT", "Asia/Tokyo"),
    "toronto":         ("CYYZ", "America/Toronto"),
    "warsaw":          ("EPWA", "Europe/Warsaw"),
    "wellington":      ("NZWN", "Pacific/Auckland"),
    "wuhan":           ("ZHHH", "Asia/Shanghai"),
    # named in the rules but no wunderground URL captured -> VERIFY before use
    "istanbul":        ("LTFM", "Europe/Istanbul"),   # "Istanbul Airport"
    "moscow":          ("UUWW", "Europe/Moscow"),     # "Vnukovo"
    "tel aviv":        ("LLBG", "Asia/Jerusalem"),    # "Ben Gurion"
}

UNVERIFIED = {"istanbul", "moscow", "tel aviv"}


def fetch(session, station, tz, y1, y2, tries=5):
    """IEM rate-limits hard (429); back off and retry rather than losing the
    station."""
    for attempt in range(tries):
        r = session.get(
            "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py",
            params={"station": station, "data": "tmpf",
                    "year1": str(y1), "month1": "1", "day1": "1",
                    "year2": str(y2), "month2": "12", "day2": "31",
                    "tz": tz, "format": "onlycomma", "latlon": "no",
                    "missing": "M", "trace": "T", "report_type": "3"},
            timeout=180)
        if r.status_code == 429:
            wait = 30 * (attempt + 1)
            print(f"    429 on {station}, waiting {wait}s "
                  f"(attempt {attempt+1}/{tries})")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.text
    raise RuntimeError(f"rate-limited out on {station}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=3)
    args = ap.parse_args()

    s = requests.Session()
    now_year = 2026
    y1 = now_year - args.years + 1

    # resume: keep stations already built so a rate-limit run can be re-run
    out = {}
    if os.path.exists(OUT):
        try:
            out = json.load(open(OUT, encoding="utf-8"))
            print(f"resuming: {len(out)} stations already in {OUT}\n")
        except ValueError:
            out = {}

    print(f"{'city':<17} {'stn':<6} {'days':>6} {'hours w/data':>13}  note")
    for city, (stn, tz) in sorted(STATIONS.items()):
        if stn in out:
            continue
        try:
            txt = fetch(s, stn, tz, y1, now_year)
        except Exception as e:
            print(f"{city:<17} {stn:<6} {'FAIL':>6}  {e}")
            continue
        days = defaultdict(list)
        for line in txt.splitlines()[1:]:
            p = line.split(",")
            if len(p) != 3 or p[2] in ("M", ""):
                continue
            try:
                date, hm = p[1].split(" ")
                days[date].append((int(hm[:2]), float(p[2])))
            except ValueError:
                continue

        hours = defaultdict(list)
        for date, obs in days.items():
            if len(obs) < 18:
                continue
            obs.sort()
            final_max = max(t for _, t in obs)
            run = -999.0
            per_hour = {}
            for h, t in obs:
                run = max(run, t)
                per_hour[h] = run
            for h, rm in per_hour.items():
                hours[h].append(round(final_max - rm, 1))

        rec = {"city": city, "tz": tz, "hours": {}}
        for h, vals in hours.items():
            if len(vals) < 100:
                continue
            rec["hours"][str(h)] = {"n": len(vals), "q": sorted(vals)}
        out[stn] = rec
        note = "UNVERIFIED STATION" if city in UNVERIFIED else ""
        print(f"{city:<17} {stn:<6} {len(days):>6} {len(rec['hours']):>13}  {note}")
        with open(OUT, "w", encoding="utf-8") as f:   # checkpoint each station
            json.dump(out, f)
        time.sleep(3.0)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f)
    size = os.path.getsize(OUT) / 1e6
    print(f"\nwritten: {OUT} ({size:.1f} MB), {len(out)} stations")
    print("Cities flagged UNVERIFIED have a station name but no wunderground "
          "URL in the rules - do not trade them until confirmed.")


if __name__ == "__main__":
    main()
