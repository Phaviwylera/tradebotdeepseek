# Strategy research: measure before you trade

This document is the honest answer to "find me a winning strategy".

There isn't one to hand over. Anyone who says otherwise is either selling
something or has not counted fees, slippage and the number of ideas they tried
before the one that worked. What *can* be built — and now is — is the machinery
that tells you, with a number and an interval, whether a specific idea is worth
trading. That machinery has already earned its keep: it found that both of this
bot's live strategies were mis-specified once exchange fees are counted.

## The method

The workflow is the boring version of "evidence-driven strategy research". It
is deliberately unglamorous:

1. **State the hypothesis before looking.** "A market trading at 0.93 with two
   hours left resolves YES at least 94.3% of the time" — a claim that can be
   false.
2. **Measure on data the strategy never saw.** Not the same rows the model was
   fit on.
3. **Charge the real costs first.** Polymarket's taker fee is
   `shares × rate × p × (1 - p)`; at 0.94 with the 5% sports rate that is
   0.28 cents a share against a 1-cent edge.
4. **Require the pessimistic bound to pass.** The lower confidence bound must
   clear the requirement, not the point estimate.
5. **Correct for how many things you tried.** Testing seven price buckets and
   reporting the best one is how backtests lie; each bucket gets
   `alpha / n_trials`.
6. **Record the rejections.** A dead idea that is written down stays dead.

## What it found here

### Polymarket charges taker fees, and the gates ignored them

`fee = shares × feeRate × p × (1 − p)`, per Polymarket's published schedule
(sports 0.05 since July 2026, crypto 0.07, politics/tech/finance 0.04,
geopolitics free). Gamma ships the parameters on every market as
`feeSchedule: {rate, exponent, takerOnly, rebateRate}`.

The strategy priced edge at `(hold − ask) / ask`. The live signal at 0.94 was
therefore recorded as a 1.06% edge:

| | no fee | 5% taker fee |
|---|---|---|
| cost basis | 0.9400 | 0.9400 |
| fee per share | 0.00000 | 0.00282 |
| edge per share | 0.01000 | 0.00718 |
| return on capital | **+1.06%** | **+0.76%** |

Against a 1% EV gate, the trade that fired was **below the gate**. It only
looked profitable because the fee was free.

### The old default band was empty

Solving `(hold − p − fee) / p > min_ev` gives a fee-dependent cap:

| `MIN_EV` | cap, fee-free | cap at 5% |
|---|---|---|
| 0.02 (original default) | 0.9314 | **0.9281** |
| 0.01 (widened) | 0.9406 | **0.9377** |

The original band was `[0.9300, 0.9314)`. At the default 2% target and a 5%
fee, the cap is 0.9281 — **below the floor**, so the band was empty. The
strategy could not have been profitable at its shipped settings on any
fee-bearing market, no matter what the prices did.

### An "arbitrage" at 0.985 can be a guaranteed loss

A YES+NO pair at 0.4925 each costs 0.985 pre-fee — inside the old threshold. Add
both legs' fees (0.0123 each) and it costs **1.0096**. The bot would have paid
1.0096 for something worth 1.00. The threshold is now applied *after* fees.

## What changed in the bot

- `fees.py` — the published fee formula, Gamma parsing, and the inverse gate
  (`required_hold`): the hold probability an assumption would have to beat,
  fees included.
- `scanner.Market` carries `fee_rate`; a market with no fee block is assumed to
  charge the sports rate rather than silently being treated as free.
- `strategy.check_snipe` charges the fee, and its cap is now fee-dependent.
- `strategy.check_arbitrage` requires the pair to clear 1.000 *after* both legs.
- `evidence.py` — an optional, bot-readable measurement that can only lower the
  hold assumption, never raise it.
- `scripts/strategy_lab.py` — the harness that produces that measurement.

## Running the study

Do this where the network reaches Polymarket (your laptop, EC2, a one-off job):

```bash
python scripts/strategy_lab.py --markets 200 --lead-hours 2 \
    --min-ask 0.90 --max-ask 0.94 --min-ev 0.02 --out evidence/study.json
```

It prints a calibration table like this and writes an evidence file:

```text
           band    n    rate  ci_low    need  net roi  ok
      0.90-0.92  114   0.947   0.921   0.9225   +2.87%  no
      0.92-0.93   88   0.932   0.891   0.9464   +0.65%  no
      0.93-0.94   61   0.918   0.857   0.9519   -1.28%  no
```

Then enable the result only if the verdict is `ACCEPT`:

```bash
EVIDENCE_PATH=evidence/hold_evidence.json   # read at startup
```

`/healthz` publishes what the gate is using:

```json
"evidence": {"source": "assumed", "effective_hold": 0.95, "verdict": "UNVERIFIED", "n": 0}
```

## How to read a verdict

| Verdict | Meaning | Action |
|---|---|---|
| `ACCEPT` | the pessimistic bound clears the fee-inclusive requirement | usable |
| `REVISE` | the point estimate clears, the bound does not | collect more data |
| `REJECT` | measured rate does not clear the requirement | do not trade it |
| `INSUFFICIENT` | fewer than `--min-samples` observations | collect more data |

A `REVISE` is not a soft yes. It means the sample is not yet big enough to
distinguish "edge" from "luck", and the honest response is more data, not a
looser threshold.

## What this cannot do

- **It cannot invent an edge.** If the measured hold rate is below the
  requirement, the strategy loses money and no configuration fixes that.
- **It cannot see the future.** Past resolution rates at a given price are an
  estimate of a conditional probability, not a guarantee.
- **It cannot model queue position.** Resting maker orders pay no fee and earn a
  rebate, which is the most interesting untested idea here — but it needs fill
  data this bot does not record.
- **It cannot fix the market.** Polymarket geoblocks the US, and a free
  Render instance has 0.1 CPU: research is fine there, live trading is not.
