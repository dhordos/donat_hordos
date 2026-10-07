#!/usr/bin/env python3
"""Map each weather_model CITY to its nearest METAR/ASOS station (IEM).

Output: station_map.json  {city: {id, name, lat, lon, km, network}}

CAVEAT THAT MATTERS: Polymarket resolves each market against a SPECIFIC
named station (NYC -> LaGuardia, Seoul -> Incheon). Nearest-by-distance is
only a starting guess - e.g. Central Park (KNYC) is closer to "New York"
than LaGuardia (KLGA) but is NOT the resolution source. Every city used for
real trading must have its station verified against the market rules text.
The `km` and the alternatives list are printed so mismatches are visible.

Usage: python build_station_map.py
"""

import json
import math
import os
import time

import requests

from weather_model import CITIES

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE_DIR, "station_map.json")


def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def main():
    s = requests.Session()
    nets = [n["id"] for n in s.get(
        "https://mesonet.agron.iastate.edu/api/1/networks.json",
        timeout=30).json()["data"] if "ASOS" in str(n.get("id", ""))]
    print(f"fetching {len(nets)} ASOS networks...")

    stations = []
    for i, net in enumerate(nets):
        try:
            gj = s.get(f"https://mesonet.agron.iastate.edu/geojson/network/"
                       f"{net}.geojson", timeout=30).json()
        except Exception:
            continue
        for f in gj.get("features", []):
            c = (f.get("geometry") or {}).get("coordinates")
            p = f.get("properties") or {}
            if not c or len(c) < 2:
                continue
            stations.append((f.get("id"), p.get("sname") or "", c[1], c[0], net))
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(nets)} networks, {len(stations)} stations")
        time.sleep(0.05)
    print(f"total stations: {len(stations)}")

    out = {}
    print(f"\n{'city':<16} {'station':<8} {'km':>6}  name / alternatives")
    for city, (lat, lon) in sorted(CITIES.items()):
        best = sorted(
            ((haversine(lat, lon, s_lat, s_lon), sid, sname, s_lat, s_lon, net)
             for sid, sname, s_lat, s_lon, net in stations),
            key=lambda x: x[0])[:3]
        if not best:
            continue
        km, sid, sname, s_lat, s_lon, net = best[0]
        out[city] = {"id": sid, "name": sname, "lat": s_lat, "lon": s_lon,
                     "km": round(km, 1), "network": net,
                     "alternatives": [{"id": b[1], "name": b[2],
                                       "km": round(b[0], 1)} for b in best[1:]]}
        alts = ", ".join(f"{b[1]}({b[0]:.0f}km)" for b in best[1:])
        flag = "  <-- CHECK" if km > 30 else ""
        print(f"{city:<16} {str(sid):<8} {km:>6.1f}  {sname[:28]:<28} "
              f"| {alts}{flag}")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    print(f"\nwritten: {OUT}")
    print("REMINDER: verify each traded city's station against the actual "
          "market rules text before trusting it.")


if __name__ == "__main__":
    main()
