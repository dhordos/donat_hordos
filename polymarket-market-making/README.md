# Polymarket: market making on daily weather markets

Polymarket runs daily markets on the maximum temperature in ~50 cities, split into 1-degree buckets. A few accounts make steady money there. I started by trying to copy two of them, then built my own paper market maker, iterated on it over more than 15 versions, and moved on to live-money testing.

**Short answer:** copying the successful accounts lost 8%. My own market maker improved a lot from version to version: the margin on matched positions went from negative to **+10.6%**, about twice the best reference account's. The leftover unmatched positions still cost most of it: after ~2,400 markets, no version has a confidence interval above zero. Live-money testing (from September 2026) has confirmed that the execution code and the paper fill model hold up.

I don't publish the strategy itself or its code. This write-up covers the process, the numbers, and why each version was dropped.

## 1. The starting point: two reference accounts

Two public accounts made steady money on these markets. I call them **TraderA** (weather markets, six-figure lifetime profit at ~0.75% of its volume) and **TraderB** (six-figure profit at ~1.1% of volume).

**Copying them (July 2026): −8%.** A follower bot listened to the platform-wide trade stream (WebSocket, ~70 trades/s) and paper-copied every trade at the price the live order book offered at that moment. Over 128K observed trades and ~232K USDC of simulated volume it lost 8%, consistently for two weeks. They are makers who earn the spread; a copier is a taker who pays it, and faster copying doesn't change that.

**What their trade history showed instead:**

- TraderA doesn't forecast the weather. Its entries win at about their entry price (+0.33 SE, n = 3,008), and its share of volume on the winning bucket (33.1%) is the same as on the favourite (33.2%).
- Its edge is thin: **+0.69%** of $298K weather volume in September 2026.
- Head-to-head on the same markets: in August TraderA made +$1,576 on 541 shared markets while my five running versions lost −$261 combined. By September my best version was ahead: **+$972 vs TraderA's −$567** on 1,618 shared markets.
- TraderB trades a different style. A paper screen of that style did not pass its gate (n = 403, ROI +0.56%, CI [−0.70%, +1.58%]).

## 2. Forecast model vs the market: the market wins

`weather_model.py` turns the Open-Meteo ensemble (ECMWF + GFS, 82 members) into bucket probabilities and logs them against the market midpoint.

- Markets resolve on one specific weather station per city (usually an airport). I calibrated per-city offsets from the resolved markets themselves (46 cities; Los Angeles was off by −6.4 °C).
- Pre-registered score on **3,661** resolved predictions: model Brier 0.2207 vs market 0.1687 before calibration, **0.2156 vs 0.1743** after.
- Decision fixed in advance: the market maker would not rely on a forecast model.

## 3. Reading the thermometer: no edge

The markets resolve on the station's raw METAR feed, which is public in real time. At New York LaGuardia the daily maximum was already reached by 14:00 on 58% of summer days and by 16:00 on 92% (252 days, 2024–2026).

- The resolution station has to be read from each market's rules text: guessing by distance picked the wrong station for about half the cities.
- I built empirical "remaining upside" distributions from 3 years of hourly data for 46 stations, and a paper bot that bought when the ask was below that fair value.
- Result: **+0.36 SE** at n = 170, **−4.8 SE** at n = 5,036 (PnL −3.2%). Other participants watch the same thermometer, and the book stays fresh.

## 4. My own market maker (paper)

A simulator that quotes on these markets and holds positions to resolution, with fills inferred from the live trade stream. Every version ran against gates fixed in advance (latest form: ≥ 14 days, enough markets, per-market bootstrap CI). From v2.6 on, several versions ran side by side on the same markets, so differences could be attributed.

| Version | Why it was built | Result | Verdict |
|---|---|---|---|
| v1 | first version | PnL **−22.1%**, 13.8% of positions matched | Dropped: the cheap outcomes it bought mostly lost |
| v2 | v1's adverse selection | PnL **−10.3%**, 27.8% matched | Dropped: the losses moved elsewhere |
| v2.5 | v2's losses clustered in one time window | **+19.8%** at its day-9 gate, 52.1% matched | Passed a gate that had no CI; bootstrap CI later [−210, +119] USDC. Lost the A/B test to v2.6 |
| v2.6 | too many unmatched positions | **81.1%** matched, won the A/B test | Dropped on the absolute gate: unmatched leftovers won 0 of 16 (−2.9 SE) |
| v2.6 control | baseline for the next round | matched +$616, but unmatched −7.8% of capital | Dropped 11 Sep: CI [−542, −49] |
| v2.7 | unmatched leftovers losing money | matched positions **−$215** | Dropped: CI [−677, −342] |
| v2.8.1–v2.8.3 | order structure modelled on TraderA | matched positions −$207 / −$222 / −$27 | All dropped: every CI below zero |
| v2.9 | execution itself was losing money | matched **+$1,994** (+9.0% margin) | Dropped: unmatched leftovers |
| v2.9.1 | isolating one constraint | matched +$2,121, total **−$635** | Dropped: more volume alone made it worse |
| v2.10 | margin on matched positions too thin | matched **+$2,475** (+10.6% margin); total +$547, CI [−604, +1,764] | Extended on 11 Sep; dropped 6 Oct (−$158 over 2,402 markets) |
| v2.10.1 | v2.10's worst single markets | in replay: worst market −$41 vs −$119, sd 13.9 vs 18.8 | Dropped 6 Oct (−$150 over 1,360 markets) |
| v2.10.2 | v2.10.1 plus twice as many markets | **+$614 over 2,443 markets** (+$0.25/market), CI [−0.27, +0.79] | Extended: best so far |
| v2.10.3 | paper shadow of the live test's order sizes | same economics as the full-size version | Done: the size hypothesis was refuted |
| v2.11 | faster updates | −$54 over 205 markets | Dropped |
| v2.12 | an entry-timing rule from the v2.10.2 analysis | +$220 over 488 markets, CI [−0.74, +1.75] per market | Running; decision fixed at ~3,600 markets |

**The v2.6 post-mortem** found a structural flaw rather than a tuning problem: by construction, the rule that closed positions could only lock in winners and left losers open. Fixing it meant rethinking the execution, which is what v2.9 and v2.10 did. The improvement in the matched positions is clear (from −$215 to +$2,475 between v2.7 and v2.10); the unmatched leftovers remain the problem.

**Gate days:**

| Date | What happened |
|---|---|
| 14 Aug | v2.6 won the A/B test against v2.5, then failed the absolute profitability gate |
| 11 Sep | 8 versions evaluated: 7 dropped, v2.10 extended |
| 28 Sep | Regime change: on 18 Sep all four running versions were positive, ten days later v2.10 had given back 90% of its profit |
| 6 Oct | v2.10 and v2.10.1 dropped; v2.10.2 and v2.12 extended |

At v2.10.2's effect size the lower CI needs ~3,600 markets to clear zero, so the final decision point is fixed in advance at that sample size.

## 5. Testing the fill model

A paper simulator is only as good as its fill assumptions. I checked them twice:

- **Against a public L2 order-book archive:** replaying one full city-day (11 buckets, 28 hours) through a FIFO-queue model gave 82–95% of the paper fills.
- **Against real money (live-money testing, from September 2026):** a live bridge with post-only limit orders (it can never take liquidity), position tracking from fills, hard caps on deployed capital and open exposure, a loss stop, an exchange-side heartbeat that cancels everything if the bot dies, and a reconciler with price hysteresis (before it, 68% of all cancels were pointless 1-cent re-placements).

What the live test has shown so far:

- The first run hit its own loss stop after 77 minutes. The live bot's market selection differed from the simulator's and picked exactly the wrong markets. I measured the effect on paper afterwards and fixed it.
- The fixed version's first 11 days: 307 fills on 37 settled markets. On the same markets, live and paper filled at the same prices and sizes; overall the paper model was ~25% optimistic on fill count (live/paper ratio 0.76). Where live lost, the paper version lost on the same markets too, so the result came from the strategy in that period, not from a live/paper gap.
- Losses arrived in many small settled pieces that never triggered the per-market stop. A live system needs a cumulative loss stop as well, so I added one to the requirements before scaling.

## What I learned

1. **Every gate needs a confidence interval.** v2.5's "pass" was carried by one lucky day.
2. **Winning an A/B test is not the same as being profitable.** v2.6 beat v2.5 and still lost money.
3. **Simulation results are path-dependent.** Two runs of the same strategy on the same markets can differ by ±$370, which is larger than most differences between my versions.
4. **Regimes change.** All versions were positive on 18 September and mostly negative ten days later. That's why nothing was scaled up.
5. **Causal stories are less reliable than arithmetic.** Several confident explanations fell apart on measurement; the algebraic claims held.
6. **Known biases matter.** The favourite-longshot bias showed up in every data set: cheap outcomes are overpriced.

Also tested and closed: a football idea (buy "both teams to score: No" before kick-off, sell in the first 20 minutes). On 5,770 markets it lost 3.5c per share (t = −24.8). The "75% win rate" it showed at first was an artefact of mid-price spikes in one-sided books.

## Engineering notes

- A WebSocket once reported "connected" but delivered nothing for 2+ hours. A watchdog now reconnects when the platform-wide stream goes silent.
- A guard silently switched itself off for cities missing from a lookup table. Guards now fail closed.
- Long nested reads on a live SQLite database starved a writer and crashed it. Analysis queries on running databases are now short and aggregated, and crashed workers exit cleanly for Docker to restart.
- Everything ran in Docker containers on a Raspberry Pi, with metrics exposed through a small JSON API to a Home Assistant dashboard.

## Code

The code of the closed research lines (copy trading, forecast model, thermometer watcher). The market maker itself is not published.

| Area | Files |
|---|---|
| Copy trading | `follower.py` |
| Forecast model | `weather_model.py`, `weather_calibrate.py`, `weather_calibration.py`, `weather_verify.py` |
| Thermometer watcher | `metar_nowcast_poc.py`, `build_station_map.py`, `verify_stations.py`, `build_upside_dist.py`, `watcher_paper.py` |

Configuration: copy `src/config.example.json` to `src/config.json` and fill in the wallet addresses to follow.
