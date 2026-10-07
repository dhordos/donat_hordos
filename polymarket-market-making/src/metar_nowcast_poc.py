#!/usr/bin/env python3
"""Proof of concept: how fast does the daily-max distribution collapse?

For a resolution station (default: KLGA / LaGuardia, the NYC market's
station), pull 2 years of hourly temps from the IEM ASOS archive and build
the empirical distribution of

    remaining_upside(h) = final_daily_max - running_max_at_hour_h

per local hour, summer months. If by 15:00 this is ~0 on most days while
the market book still spreads over 2-3 buckets, the "thermometer watcher"
edge is structurally real and worth building out.

Usage: python metar_nowcast_poc.py [--station LGA] [--tz America/New_York]
"""

import argparse
from collections import defaultdict

import requests


def fetch(station, tz, y1, y2):
    r = requests.get(
        "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py",
        params={"station": station, "data": "tmpf",
                "year1": str(y1), "month1": "1", "day1": "1",
                "year2": str(y2), "month2": "12", "day2": "31",
                "tz": tz, "format": "onlycomma", "latlon": "no",
                "missing": "M", "trace": "T", "report_type": "3"},
        timeout=120)
    r.raise_for_status()
    return r.text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", default="LGA")
    ap.add_argument("--tz", default="America/New_York")
    args = ap.parse_args()

    days = defaultdict(list)  # date -> [(hour, tmpf)]
    for y1, y2 in ((2024, 2024), (2025, 2025), (2026, 2026)):
        for line in fetch(args.station, args.tz, y1, y2).splitlines()[1:]:
            parts = line.split(",")
            if len(parts) != 3 or parts[2] in ("M", ""):
                continue
            date, hm = parts[1].split(" ")
            try:
                days[date].append((int(hm[:2]), float(parts[2])))
            except ValueError:
                continue

    print(f"station {args.station}: {len(days)} days of hourly obs")

    # summer months only (Polymarket's active season; conditioning matters)
    upside = defaultdict(list)   # hour -> [final_max - running_max]
    for date, obs in days.items():
        month = int(date.split("-")[1])
        if month not in (6, 7, 8):
            continue
        if len(obs) < 20:
            continue
        obs.sort()
        final_max = max(t for _, t in obs)
        run = -999.0
        seen_h = {}
        for h, t in obs:
            run = max(run, t)
            seen_h[h] = run
        for h, rm in seen_h.items():
            upside[h].append(final_max - rm)

    print(f"\nsummer (Jun-Aug) days used: "
          f"{len([d for d in days if int(d.split('-')[1]) in (6,7,8)])}")
    print(f"\n{'hour':>5} {'n':>5} {'P(done)':>8} {'P(<=1F)':>8} "
          f"{'P(<=2F)':>8} {'mean':>6} {'p90':>6}")
    for h in range(8, 21):
        u = sorted(upside.get(h, []))
        if len(u) < 50:
            continue
        n = len(u)
        p_done = sum(1 for x in u if x <= 0.01) / n
        p_1 = sum(1 for x in u if x <= 1.01) / n
        p_2 = sum(1 for x in u if x <= 2.01) / n
        mean = sum(u) / n
        p90 = u[int(0.9 * n)]
        print(f"{h:>4}h {n:>5} {p_done:>8.2f} {p_1:>8.2f} {p_2:>8.2f} "
              f"{mean:>6.2f} {p90:>6.1f}")

    print("\nreading: P(done) = share of days where the daily max was ALREADY "
          "reached by that hour.\nIf P(done) at 15h is ~0.6-0.8, a watcher "
          "knows the outcome hours before settlement\nwhile the book may "
          "still be pricing 2-3 buckets.")


if __name__ == "__main__":
    main()
