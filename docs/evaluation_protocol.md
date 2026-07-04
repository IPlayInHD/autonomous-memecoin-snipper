# Evaluation protocol — the go-live gate

This document is the contract between you and your own data. The instrument
exists to answer one question:

> **After realistic costs and fill modeling, is the mean net P&L per trade
> (expectancy) positive with statistical confidence?**

Run it any time with:

```bash
python -m sniper.evaluate            # human-readable verdict
python -m sniper.evaluate --json     # machine-readable
```

Exit code 0 = GO, 1 = NO-GO (usable in scripts/cron).

---

## Optimize expectancy, never win rate

Win rate is a vanity metric in a distribution with −100% tails. A strategy
that wins 70% of the time at +30% and loses 30% at −100% has expectancy
`0.7·(+0.30) + 0.3·(−1.00) = −0.09` per unit — it bleeds out while feeling
great. Every report therefore leads with **mean net P&L per trade** and shows
the median and the left tail (p5, worst) alongside it.

`could_not_sell` outcomes (rugs — the sell never landed) are counted inside
expectancy as the full losses they are, and *also* reported as their own
category, because a strategy whose losses are concentrated in rugs needs a
different fix (better LP/bundle filters) than one that bleeds via slippage.

## Bootstrap confidence intervals (heavy tails break t-tests)

Per-trade P&L here is violently non-normal: a mass of small losses, a spike of
fee-sized losses, occasional multiples, and a rug tail at −100%. The Student-t
interval assumes exactly what this distribution violates. The gate instead
uses a **percentile bootstrap** (10,000 resamples of the trade list with
replacement, seeded for reproducibility): resample → recompute the mean → read
the 2.5th/97.5th percentiles of the resampled means.

**Criterion: the lower bound of the 95% CI on net expectancy must be > 0.**
Not the mean. The lower bound. A mean of +0.002 SOL with a CI of
[−0.001, +0.005] is a coin you haven't finished flipping.

## Pre-registration (no peeking)

The score threshold lives in `config/preregistration.json`, written **before**
evaluation data is collected. On first run, the bot records the file's SHA-256
in the database. The evaluator recomputes it every startup; if the file
changed mid-sample, the `preregistration` criterion fails permanently for that
dataset and the verdict is NO-GO — a threshold tuned on the data it is judged
on measures your fitting skill, not the market.

Changing the threshold is allowed — it just starts a **new** experiment:
archive/rename the old `data/sniper.db`, edit the file, collect fresh data.

## Walk-forward stability

The closed trades are split **in time order** into 4 folds. The criterion
requires a majority of folds positive **and the final (most recent) fold
positive**. An edge that lived only in week one is an edge that already died.

## Regime warning

2–4 weeks of data samples roughly **one** market regime (one meta, one
attention cycle, one fee environment). The report prints an explicit warning
whenever the sample spans < 28 days. Passing the gate means "positive in the
regime observed," nothing more. Expect edges to decay; re-run the evaluation
continuously even after any go-live.

## Shadow calibration (paper is a model, models must be checked)

Paper fills are simulations. Before any live decision, the **shadow phase**
places tiny real trades (0.01–0.05 SOL) and logs, per trade, the paper model's
prediction vs. reality into the `calibration` table. The criterion requires at
least **20 shadow trades** with:

- |paper fill rate − actual fill rate| ≤ **0.15**, and
- mean |predicted − realized slippage| ≤ **150 bps**.

If paper said 85% fill / 120 bps and shadow shows 40% fill / 600 bps, then the
paper expectancy — however significant — described a market you don't have
access to. Recalibrate `execution.paper.*` (fill probabilities,
adverse-selection bps) to match shadow reality, then the paper dataset must be
re-collected under the new model.

## The full gate

Live mode is justified **only when all of these hold simultaneously**:

| # | criterion | default |
|---|-----------|---------|
| 1 | closed paper trades | ≥ 300 |
| 2 | sample span | ≥ 14 days (warning until 28) |
| 3 | pre-registration file unchanged since first data | required |
| 4 | bootstrapped 95% CI lower bound on net expectancy | > 0 |
| 5 | walk-forward: majority of folds positive, final fold positive | required |
| 6 | shadow calibration within tolerances (≥ 20 canary trades) | required |

Everything green prints **GO**. Anything else prints **NO-GO**, with reasons.
A NO-GO is not a failure of the project — it is the project working: you
measured the edge (or its absence) for the cost of RPC calls and a few
canary trades.

## If it ever says GO: position sizing

Even then, size like you disbelieve it:

- **Heavily discounted fractional Kelly** — the risk layer computes
  `kelly × 0.10` (config `risk.kelly_discount`) from the observed win rate and
  win/loss magnitudes, then caps it at the hard per-trade USD cap. Full Kelly
  on an estimated edge with heavy tails is a guaranteed eventual zero.
- Zero or negative computed edge ⇒ size 0, regardless of gut feeling.
- All other risk brakes (daily loss cap, fee cap, SOL floor, concurrency cap)
  stay armed in live mode. They are not training wheels; they are the vehicle.

## Known residual optimism

Be honest about what even a green gate does NOT prove:

- Paper cannot model **being someone's exit liquidity** — the fill you get in
  block N is available precisely because someone faster didn't want it.
  Shadow narrows this gap; it does not close it.
- The bundle/deployer heuristics are approximations of adversaries who adapt.
- Detection latency on shared RPC varies with load you don't control; the
  latency panel (p50/p95/p99 per stage) is the ground truth, watch its drift.
- Survivorship in *your own* pipeline: filters tuned (even pre-registered)
  reflect scams of the observed window; new honeypot vectors appear monthly —
  Token-2022 unknown-extension hard-fail is the backstop, keep it on.
