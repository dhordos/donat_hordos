# League of Legends: win-probability models vs Polymarket

Polymarket lists League of Legends match markets (match winner, game winner, over/under maps, props). I wanted to know whether a model built from public data can price these matches better than the market, **at prices you could actually trade**.

**Short answer:** the models work, and the market already knows what they know. My in-play model reaches a Brier score of 0.169 out-of-time (a coin flip scores 0.25). Tested against real order-book asks with a pre-registered rule, its edge was zero, in tier-1 and minor leagues alike. When the model disagreed with the market the most, the market was usually right.

## Data

| Source | What | Size |
|---|---|---|
| Polymarket live collector | Every trade (WebSocket) and a per-minute order-book snapshot for the winner markets of every LoL event | 75K trades, 322K book snapshots |
| Polymarket backfill | 1-minute price history of closed markets | ~1.6M price prints, ~8.4K markets |
| lolesports live-stats feed | 10-second game telemetry: gold, kills, towers, dragons by type, barons, inhibitors | 7,775 games, 19 leagues |
| Oracle's Elixir | Pro match data: drafts, sides, results (2025–2026) | ~16.6K games |

Joining these was a project in itself. Oracle's Elixir games are linked to Polymarket events by normalized team-name pair and date (77% of match events linked), and the telemetry is linked the same way. As a sanity check on the joins, blue side won 53.7% of 1,526 games in my data, which matches LoL's well-known blue-side advantage.

## Models

**1. Pre-match: Elo and draft.** A chronological, leakage-free Elo (K = 20) with formula-based BO1/BO3/BO5 conversion, plus a draft model (recency-weighted champion statistics, team form, logistic regression) behind a small Streamlit app.
- Draft model: Brier **0.2302** vs 0.25 baseline on a chronological split.
- Elo vs the market's opening price on 208 matched events: **0.2400 vs 0.2479**. The model beats the opening price, which is barely better than a coin flip.

**2. In-play: game state → win probability.** One sample per game-minute, three phase models (early, mid, late), logistic regression fitted with IRLS in NumPy. Features: gold, kill, tower, dragon, baron and inhibitor differences, plus game time.
- Out-of-time Brier **0.1787** trained on tier-1 leagues, **0.1692** after adding minor-league data.
- Dragons carry information beyond gold: one dragon is worth about **0.36k / 0.69k / 1.23k gold** in the early / mid / late game.
- Mid-game, "+3k gold and +2 dragons" won **96.3%** of the time (n = 163); "equal gold, +2 dragons" won 80.8% (n = 52).
- Known weakness: overconfident in the extreme deciles (predicted 3.6% → actual 11.6%). Comebacks are more common than a linear gold effect suggests.

## The money tests

All rules and gates were written down before the results were computed.

| Test | Rule | Gate | Result |
|---|---|---|---|
| In-play model vs real asks, tier-1 | First minute where model edge ≥ 5c over the real ask with ≥ $2 depth; one bet per game; hold to resolution | n ≥ 30, ROI > 0, t ≥ 2.5 | n = 39, ROI **−0.4%**, t = −0.06 → **fail** |
| Same, minor leagues | Same rule, retrained model | Same | n = 28, hit rate 39.3% vs 39.7% price, ROI **−0.4%**, t = −0.05 → **no edge** |
| Where the model disagreed most (edge ≥ 10c) | Subset of the above | Diagnostic | ROI **−10%** (tier-1), −3.3% (minor) |
| Elo disagreement (gap ≥ 10c), pre-match | Entry at the **first** print | n ≥ 30, t ≥ 2.5 | n = 134, ROI +14%, t = 3.34 → looked like a pass |
| Same rule, robustness check | Entry at the **last** pre-match print | Same | n = 108, ROI **+0.2%**, t = 0.05 → **dead** |

The last two rows were the most useful lesson of the project. Opening prices on these markets are templates that nobody has traded yet, and they converge to an efficient price before the match starts. Beating a $5 opening print isn't an edge you can capture. Since then, every backtest of mine enters at the last pre-match print or at a book-verified ask.

The descriptive analysis had looked promising: on 52 games the market seemed to price gold fairly but dragons at only ~40% and barons at ~50% of their true weight. The money test above is where that signal went to die.

## Other hypotheses I tested

| Idea | Result |
|---|---|
| In-play legging (back the favourite, hedge the other side after a swing) | Median positive, **mean −4 to −7c per pair** on 188 matches: rare early collapses wipe out many small wins |
| Instant arbitrage inside a market, and handicap-vs-winner logic violations | **0** of 145,144 book snapshots; **0** of 84 events |
| Buying in-play dips (drop threshold × target × timing grid) | Every combination mean ROI ≤ 0 |
| Stomp overreaction after game 1 | n = 111, ROI −0.5%, t = −0.12 |
| Blue side in coin-flip game 1s | n = 77, win rate 54.5%, ROI +4.6%, t = 0.81: right direction, not significant |
| Minor-league pricing | Found a pricing inefficiency in some minor leagues: confirmed on 372 matches of price history and on 16 real-ask bets (ROI +45%/bet). It is in a forward paper test, so I don't publish the details |
| Template prices on derivative markets | Opening prices are templates that are often off, but they converge before the match |
| Props (pentakill, quadra, both teams baron…) | Practically no liquidity (78 trades across 2,322 markets), and 18% of props void at 0.5 |

The forward paper test is a good example of why live tests matter. Its first version lost badly (n = 23, −2.75 SE). Going through every bet one by one showed that the bot had drifted from the validated rule, and that its bet sizing amplified adverse selection. Version 2 went back to the validated rule with fixed stakes.

## What I learned

1. **Median positive, mean negative.** Almost every "it usually works" strategy here had many small wins and rare total losses. Looking at the median is how manual traders fool themselves.
2. **When the model shouts, the market is usually right.** Large disagreements were where the model was most wrong, not where the money was.
3. **Printed prices lie.** Thin markets need real asks, real depth and fees in the backtest.
4. **Stake sizing is part of the strategy.** The wrong sizing rule amplified adverse selection twice in this project.
5. **Engineering: SQLite under parallel writers.** The telemetry backfill taught me WAL mode, the right commit granularity (both too frequent and too rare commits cause lock storms) and restartable, idempotent jobs.

## Code

A snapshot of the research code from August 2026. Some numbers above come from later runs in my research log (for example the minor-league retraining).

| Area | Files |
|---|---|
| Data collection | `esports_collector.py` (live trades + book snapshots), `esports_backfill.py`, `backfill_derivatives.py`, `lolesports_fetch.py` (10-second telemetry), `oe_join.py`, `ls_pm_link.py`, `lol_champion_stats.py` (Oracle's Elixir pipeline and draft model) |
| Models | `state_model.py` (in-play model), `tier_b_elo_vs_market.py` (Elo), `live_predict.py` |
| Pre-registered tests | `tier_c_model_vs_book.py`, `tier_b_blueside.py`, `state_analyze.py` |
| Market structure | `esports_analyze.py`, `esports_inplay_analyze.py`, `esports_bo3_leg.py`, `esports_arb_scan.py`, `esports_micro_analyze.py` |

The code of the strategies still under test is not published.
