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
