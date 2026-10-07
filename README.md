# Donát Hordós: prediction-market research

I'm an actuary turned data specialist (MSc in Actuarial and Financial Mathematics, 8+ years in actuarial, consulting and data roles). This repo collects two research projects I built in my free time on [Polymarket](https://polymarket.com), a prediction market. Both ran 24/7 on a Raspberry Pi home server.

| Project | Question | Short answer |
|---|---|---|
| [**League of Legends win-probability models**](lol-win-probability/) | Can models built from drafts, team strength and live game state price LoL matches better than the market? | The models are good (in-play Brier **0.169** out-of-time, coin flip = 0.25). The market is just as good: on real, tradable asks the edge was **zero** (ROI −0.4%, t ≈ 0). |
| [**Polymarket market making**](polymarket-market-making/) | Two accounts make steady money on daily temperature markets. How, and can I replicate it? | Copying them lost 8%. My own paper market maker went through more than 15 versions, each built to fix a measured failure of the previous one. The margin on matched positions rose to +10.6%, but no version is statistically significant yet. Live-money testing has validated the execution code and the fill model. |

## How I work

The same rules apply to both projects:

- **Pre-registered gates.** Before a test runs, I write down the entry rule, the sample size and the pass/fail threshold (typically n ≥ 30–50, ROI > 0, t ≥ 2.5). No tweaking after seeing results.
- **Real prices, not printed prices.** Backtests use order-book asks and fees. Opening prices on thin markets turned out to be stale templates, so first-print backtests don't count as evidence.
- **Paper first.** Strategies run as live paper simulations before any real money is considered.
- **Negative results are results.** Most ideas failed. I document why instead of tuning them until they look good.
- **Confidence intervals on every gate.** I learned this the hard way: one early "pass" was carried by a single lucky day, and its bootstrap CI comfortably included zero.

## Tools

Python (pandas, NumPy, scikit-learn), SQLite, REST and WebSocket APIs, Docker, Linux on a Raspberry Pi, Streamlit. I developed with Claude Code as a pair programmer and used several LLMs to brainstorm hypotheses. Every hypothesis was then tested on my own data against rules fixed in advance.

## About the code

The `src/` folders are snapshots of the research code as it ran, not a packaged library. Code of strategies that are still being tested is not published. The data (SQLite databases with ~1.6M price prints, order-book snapshots and game telemetry) is not included, so the scripts won't run out of the box. The write-ups contain the results.

Contact: [linkedin.com/in/donat-hordos](https://www.linkedin.com/in/donat-hordos)
