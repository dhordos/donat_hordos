#!/usr/bin/env python3
"""Read the RESOLUTION STATION out of live weather markets' own rules text.

Distance-based guessing picks the wrong station (NYC's nearest ASOS is a
Manhattan heliport; the market actually resolves on LaGuardia). The rules
text names the source, so parse it instead of guessing.

Fetches open weather markets from Gamma, extracts station hints from the
description, and cross-checks against station_map.json. Prints a curated
override block to paste into the watcher bot.

Usage (needs Gamma API access):
  python verify_stations.py
"""

import json
import os
import re
import time
from collections import defaultdict

import requests

from weather_model import parse_question

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GAMMA = "https://gamma-api.polymarket.com"

# things that look like a station / source reference in the rules text
PATTERNS = [
    re.compile(r"\b([A-Z]{4})\b(?=\s*(?:station|airport|\)))"),   # ICAO
    re.compile(r"\b(K[A-Z]{3})\b"),                               # US ICAO
    re.compile(r"(?:at|from)\s+(?:the\s+)?([A-Z][\w'\-]+(?:\s+[A-Z][\w'\-]+){0,3}"
               r"\s+(?:Airport|Station|Intl|International))"),
    re.compile(r"(wunderground\.com/history/daily/[^\s\)\"]+)", re.I),
    re.compile(r"\b(LaGuardia|Central Park|Incheon|Heathrow|O'Hare|Midway|"
               r"Logan|Sky Harbor|Love Field|Boeing Field)\b", re.I),
]


def main():
    s = requests.Session()
    s.headers["User-Agent"] = "station-verify/1.0 (research)"
    found = defaultdict(lambda: defaultdict(int))
    examples = {}
    n_seen = 0

    for offset in range(0, 2000, 100):
        try:
            r = s.get(GAMMA + "/markets",
                      params={"closed": "false", "limit": 100,
                              "offset": offset, "order": "volume24hr",
                              "ascending": "false"}, timeout=30)
            batch = r.json()
        except Exception as e:
            print("fetch failed:", e)
            break
        if not batch:
            break
        for m in batch:
            q = m.get("question") or ""
            parsed = parse_question(q)
            if not parsed:
                continue
            n_seen += 1
            city = parsed["city"]
            text = " ".join(str(m.get(k) or "")
                            for k in ("description", "resolutionSource"))
            if not text.strip():
                continue
            for pat in PATTERNS:
                for hit in pat.findall(text):
                    found[city][hit.strip()] += 1
            examples.setdefault(city, text[:400])
        time.sleep(0.2)

    print(f"weather markets parsed: {n_seen}\n")
    smap = {}
    p = os.path.join(BASE_DIR, "station_map.json")
    if os.path.exists(p):
        smap = json.load(open(p, encoding="utf-8"))

    print("=" * 74)
    print("STATION HINTS FROM MARKET RULES  (vs distance-based guess)")
    print("=" * 74)
    for city in sorted(found):
        guess = (smap.get(city) or {}).get("id", "?")
        alts = ", ".join(f"{a['id']}" for a in (smap.get(city) or {})
                         .get("alternatives", []))
        hits = sorted(found[city].items(), key=lambda kv: -kv[1])[:4]
        hs = ", ".join(f"{h}({n})" for h, n in hits)
        print(f"\n{city}")
        print(f"  rules say : {hs}")
        print(f"  guess     : {guess}   (alts: {alts})")

    print("\n\nSample rules text per city (first 400 chars) - read these:")
    for city, txt in sorted(examples.items())[:6]:
        print(f"\n--- {city} ---\n{txt}")


if __name__ == "__main__":
    main()
