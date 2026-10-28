# Quant strategies: what was adopted, what was rejected

A shared "quantitative architecture for Polymarket" memo proposed five ideas and
a $20 → $6,000-in-72-hours plan. This document records the disposition of every
claim, because the memo mixed three things: arithmetic that is correct,
arithmetic that is correct but describes a losing plan, and facts about the venue
that are out of date. Adopting it wholesale would have added a false fee model
and a feature that cannot run here.

Verification date: 2026-10-26. Live checks were made against
`gamma-api.polymarket.com` and the published fee documentation, not against the
memo.

---

## Adopted

### 1. Ruin arithmetic — `research/growth.py`

The memo's central claim is that a 300x target is not a strategy question but an
arithmetic one. It is right, and the numbers are reproducible:

| Quantity | Value |
| --- | --- |
| Multiple | 300.0x |
| Compounded daily rate | **+569.43 %/day** |
| Compounded hourly rate | +8.244 %/h |
| All-in wins needed at 0.20 | **3.54** |
| Fair-priced survival | **0.333 %** |
| Fair-priced ruin | **99.667 %** |

`feasibility(20, 6000, 3, entry_price=0.20)` returns exactly these, and
`trades_needed` implements the memo's `n = ln(C_T / C_0) / ln(1/p)`.

The identity that makes this fatal is worth stating plainly: at a fair price the
probability of surviving a chain that multiplies the bankroll by `M` is exactly
`1/M`. Compounding does not create edge; it converts a small per-trade
disadvantage into a near-certain wipeout. Costs move 99.667 % to above 99.95 %.

**Consequence for this bot:** the growth module is a *rejection filter*, not a
target. It exists so the question "can this bankroll reach that number" gets a
number instead of optimism.

### 2. NegRisk basket parity — `negrisk.py`

For a NegRisk event with `M` mutually exclusive outcomes, exactly one resolves
YES, so at settlement:

```
sum over all M outcomes of P(YES_i) == 1.00
```

If all `M` best asks can be bought for less than 1.00 **after taker fees**, the
payoff is certain and the trade needs no forecast — only market orders, which
this bot can already reason about. That makes it the only genuinely risk-free
structure available to a deployment with no wallet and no signing.

Two guards are load-bearing:

- **Completeness.** Summing three of twelve outcomes gives ≈0.3, which reads as a
  70 % edge and is really a partial view. `evaluate_basket(complete=False)`
  refuses to score it.
- **Fees per leg.** `M` legs means `M` taker fees, and
  `fee = rate * p * (1 - p)`. A 0.96 basket of three 0.32 legs looks like a 4 %
  edge until three 5 % fees turn it into 0.7 %.

### 3. Fractional Kelly

Already implemented before the memo arrived: `StrategyEngine` sizes model-edge
signals at `bankroll * kelly_fraction * kelly_full` with `kelly_fraction = 0.25`
by default, capped by `MAX_BET`. The memo's recommendation was to *use* fractional
Kelly; the repository already did. What changed is that the fraction is now a
setting (`KELLY_FRACTION`) instead of a literal, validated to `(0, 1]`.

### 4. Market-making quotes — `marketmaking.py`

Makers pay no protocol fee and receive a share of taker fees, which is a
structurally better position than a taker paying `rate * p * (1-p)` on every fill.
The module implements Avellaneda–Stoikov:

```
r    = S - q * gamma * sigma^2 * (T - t)
d*   = gamma * sigma^2 * (T - t) + (2/gamma) * ln(1 + gamma/k)
bid  = r - d*/2        ask = r + d*/2
```

**The memo's parameter scale does not transfer.** `k` is decay *per unit of
price*, and these prices live in `(0, 1)`; the FX/equity default of `k ≈ 1.5`
produces `0.001 / 0.999` — the entire book. `DEFAULT_K = 100` gives a two-cent
spread at a 0.60 mid, which is the right order of magnitude here.

This module computes quotes only. It does not rest orders, because resting orders
need L2 credentials, a funded wallet and a host Polymarket does not geoblock.

---

## Rejected

### The fee table (wrong by roughly 5x)

The memo states sports 0.75 %, politics 1.00 %, crypto 1.80 %. Live Gamma
`feeSchedule` data returns `rate: 0.05` for `sports_fees_v3`, `0.03` for
`sports_fees_v2`, and `0` for `zero_fees`, with most political markets fee-free.
The repository's fee truth is `Market.fee_rate`, parsed from Gamma, and
`fees.ASSUMED_UNKNOWN_FEE_RATE` when a market carries no schedule.

Using the memo's table would have made every EV calculation wrong in the
conservative direction on politics and wildly wrong on sports, and would have
hidden the actual rebate economics (a share of *taker* fees returns to makers).

### `convertPositions()` and the NegRisk adapter

The conversion route — buy NO on one outcome, convert to YES on the rest, sell
them — needs an on-chain transaction, a funded wallet, a private key and a host
Polymarket does not geoblock. This deployment has none of those, by design:
`LIVE=false`, no signer, Render's US region. Basket arbitrage through plain
market orders captures the same mispricing without touching the chain, so the
conversion path is documented and not built.

### `pUSD`, `CollateralOnramp`, `V2_EXCHANGE_DOMAIN_VERSION`, `/auth/derive-api-key`

None of these appear in the current public API surface, and none are needed: the
bot reads Gamma and CLOB market data and never signs. Adopting unverifiable
endpoint names would have added dead code that looks authoritative.

### The `py-clob-client` snippet

It requires `PK` (a private key) as an argument. That is a categorical no for
this repository: no key material is read, logged or required, and `config.py`
refuses to print it.

---

## Still open

- **Maker rebates are modelled, not measured.** `rebate_value()` is an upper
  bound based on the published share of taker fees; the real allocation is
  time-weighted depth near the mid. Do not treat it as income until measured.
- **Basket execution is not implemented.** A basket is risk-free only if every
  leg fills. The executor places one order per signal, so the lane *records*
  baskets as signals with `executed=False` and a reason string, rather than
  sending `M` independent orders and risking a naked directional leg.
- **Nothing here is a profitable strategy by itself.** A basket under 1.00 is a
  real edge but a rare one; a 300x target remains ruinous by construction.

---

# Second memo: microstructure, leaderboards, execution

A second document ("Quantitative Microstructure, Leaderboard Analytics, and
Algorithmic Execution Strategies") repeats the first one's fee error and adds
four new areas. The disposition below is by section.

## Adopted

### Logit-space quoting — `microstructure.py`

The best argument in either memo. A price bounded in `(0,1)` cannot follow
arithmetic Brownian motion, because that process walks through both boundaries.
The fix is the change of variable `x = logit(p)`, and Ito gives the sensitivity

```
dp/dx = p(1-p)
```

which is correct and useful: the same belief moves a 0.50 market by 0.25 and a
0.99 market by 0.0099. `delta()`, `logit_volatility()` and `reservation_price()`
implement it.

**But two claims that ride on it do not survive contact with numbers.**

1. *"Spreads narrow toward the base liquidity premium as certainty approaches."*
   Correct — and the memo does not follow the implication. The optimal width has
   two terms:

   ```
   risk    = gamma * sigma_b^2 * [p(1-p)]^2 * (T-t)      collapses near certainty
   adverse = (2/gamma) * ln(1 + gamma/k)                 does not, ever
   ```

   At the defaults (`sigma_b`=0.05, one day of horizon) the risk term is ~1e-6
   and the adverse term is ~2e-2. `risk_share()` puts inventory risk at **0.07%
   of the quoted width** at a 0.62 mid. So the `[p(1-p)]^2` factor governs
   *skew and sensitivity*, not width, and quoting a fixed cent width near
   certainty is not conservative — it is pure adverse-selection rent.

2. *Inventory skew is presented as the core quoting mechanism.* At realistic
   parameters it is **sub-tick**. The skew at a 0.50 mid is 1.4e-5, so it cannot
   move a 0.001-tick quote at all; it needs `sigma_b` near 1.0 before it is
   visible. Tests pin both the sub-tick result and the regime where it matters.
   Inventory management here is a risk-limit problem (which `risk.py` already
   solves), not a pricing problem.

### Depth, slippage and participation — `microstructure.py`

Section 6's point that a book is a ladder rather than a price is right and
under-served elsewhere. `walk_book()` computes the average fill across levels:
a 0.94 ask against a 0.95 hold has one cent of edge, and half a cent of slippage
takes half of it while the quoted price still reads 0.94. `max_shares_within()`
trims an order to stay inside the EV cap; `cap_to_depth()` keeps it to a fraction
of visible resting size so the order does not create the move it trades.

### Resolution (N/A) risk — `resolution.py`

The most valuable omission in both memos. Section 1 states that an ambiguous or
cancelled event resolves N/A at $0.50 per share, and Section 6 calls it "a
primary operational hazard" — then never prices it. It is the largest single risk
in the snipe strategy:

```
E[payout] = (1-q) * hold + q * 0.50
```

so N/A is charged at `hold - 0.50` = **45 cents per unit**. Concretely:

| N/A probability | expected payout | return on a 0.94 entry |
| --- | --- | --- |
| 0% | 0.9500 | +1.06% |
| 0.13% | 0.9494 | +1.00% (exactly the gate) |
| 1% | 0.9455 | +0.59% |
| 5% | 0.9275 | **-1.33%** |

The bot's entire 1% EV margin at 0.94 is a **0.13% N/A probability**. Since that
probability cannot be measured, the strategy now *refuses* markets whose rules
admit a 50-50 fallback (`equal_split`) rather than pricing a number it does not
know, and charges a stated 2% to the hold on any that slip through
(`NA_PROBABILITY`, `REQUIRE_UNAMBIGUOUS_RESOLUTION`). Charging 2% moves a 0.95
hold to 0.941 and the band cap from 0.9406 to **0.9317** — published as
`snipe_band_na`.

### Leaderboard auditing — `research/leaderboard.py`

Section 3's wallet table is a strategy argument, so it gets checked rather than
quoted. `audit()` applies identities that must hold:

- `volume / trades` — average clip size, which reveals execution style
- `win_rate * trades` must be a near-integer
- PnL cannot exceed volume
- trades/day, which is far harder to fake than a label

**It catches a real impossibility in the memo's own table:** Theo4B is listed at
**+$22.05M PnL on $20.1M volume**. PnL above volume is not "impressive", it is
not possible for a taker, and the row cannot be used to justify a strategy. The
same row's "~90%" win rate over 45 trades implies 40.5 wins.

## Rejected

- **The fee table again.** This memo says sports 0.75%, politics 1.00%,
  economics/culture/weather 1.25%, crypto "dynamic ~1.80% peak". Live Gamma
  returns `rate: 0.05` (`sports_fees_v3`), `0.03` (`sports_fees_v2`), `0`
  (`zero_fees`). The formula `rate * p * (1-p)` is right; the rates are wrong by
  roughly 5x, and a bare "1.80%" for crypto is not a rate at all — it is an
  effective fee quoted at `p = 0.5`, which is exactly the confusion
  `FeeSchedule` exists to prevent.
- **`convertPositions()`, `splitPosition()`, `mergePositions()`.** Same reason as
  before: on-chain transactions, funded wallet, private key, Polygon gas, and a
  host Polymarket does not geoblock. The parity insight is already captured by
  `negrisk.py` through market orders alone. The claimed route "buy the basket,
  convert, sell the legs" also assumes the converted YES tokens are sellable at
  the price that made the basket cheap, which is the same liquidity that was
  missing.
- **pUSD / CollateralOnramp / `V2_EXCHANGE_DOMAIN_VERSION` / L1-L2 auth.** Not
  needed by anything that only reads Gamma and the CLOB. No key material is
  read, logged or required.
- **5-second heartbeats, FOK/GTD order types.** Real CLOB features, but they
  require authenticated order placement, which `LIVE=false` deliberately
  disables.
- **The "$5,000 USDC UMA bond".** Live Gamma publishes `umaBond: "250"` and
  `"500"` on the markets sampled. Off by an order of magnitude; it does not
  change any decision, which is itself the reason to distrust it.
- **Catalyst momentum / news-driven re-pricing.** Needs a real-time external
  news feed and sub-second execution on a free host with 0.1 CPU and a 60-second
  scan interval. Not implementable here, and the memo's own `Delta P > 0.10`
  trigger with a `<= 0.02` spread describes a top-of-book event this bot
  structurally cannot win.

## Still open

- **The N/A base rate is unknown.** 2% is a conservative placeholder, not a
  measurement. `scripts/strategy_lab.py` could estimate it from resolved markets
  by counting final prices at 0.50, but that requires network access to Gamma.
- **Leaderboard data is not fetchable here.** The validator exists and is tested;
  the numbers must come from the user's machine or Polymarket's data API.
- **Nothing in either memo makes a 300x target reachable.** The ruin arithmetic in
  `research/growth.py` is unchanged by better quoting: it is a property of
  compounding at a fair price, not of execution quality.
