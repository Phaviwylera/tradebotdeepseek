#!/usr/bin/env python3
"""Measure the SNIPE hold assumption instead of asserting it.

Run this where the network reaches Polymarket (your laptop, EC2, a one-off
Render job -- not a firewalled sandbox):

    python scripts/strategy_lab.py --markets 200 --out evidence/hold.json

It fetches recently resolved binary markets from Gamma, walks each one's CLOB
price history, samples the price at a fixed lead time before trading stopped,
and asks the only question that matters:

    Given a market was trading at X with T hours left, how often did it
    actually resolve YES -- and does that rate, minus the venue's taker fee,
    clear the EV gate the bot uses?

The answer is a number with a confidence interval, a verdict, and (optionally)
the evidence file the bot reads at startup to replace its assumption.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evidence import build_report  # noqa: E402
from fees import ASSUMED_UNKNOWN_FEE_RATE, FeeSchedule  # noqa: E402
from research.screening import (  # noqa: E402
    Observation,
    bucket_report,
    evaluate_arbitrage_hypothesis,
    evaluate_snipe_hypothesis,
)

logger = logging.getLogger("strategy_lab")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DEFAULT_EDGES = (0.0, 0.5, 0.7, 0.85, 0.90, 0.92, 0.93, 0.94, 0.95, 0.97, 0.99, 1.0)


def _get_json(url: str, params: dict[str, str] | None = None, timeout: float = 20.0) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "strategy-lab/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed hosts
        return json.loads(response.read().decode("utf-8"))


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class ResolvedMarket:
    condition_id: str
    question: str
    token_yes: str
    token_no: str
    resolved_yes: bool
    fee_rate: float
    end_date: datetime | None


def parse_resolved(raw: dict[str, Any]) -> ResolvedMarket | None:
    """A closed market with a clean 1/0 resolution and two tokens."""
    if not raw.get("closed"):
        return None
    prices = _json_list(raw.get("outcomePrices"))
    tokens = _json_list(raw.get("clobTokenIds"))
    outcomes = [str(item).strip().lower() for item in _json_list(raw.get("outcomes"))]
    if len(prices) != 2 or len(tokens) != 2 or len(outcomes) != 2:
        return None
    try:
        yes_price = float(prices[outcomes.index("yes")]) if "yes" in outcomes else float(prices[0])
        no_price = float(prices[outcomes.index("no")]) if "no" in outcomes else float(prices[1])
    except (TypeError, ValueError):
        return None
    # Ambiguous resolutions (50/50, voided, still trading) are not evidence.
    if not ((yes_price == 1.0 and no_price == 0.0) or (yes_price == 0.0 and no_price == 1.0)):
        return None
    schedule = FeeSchedule.from_gamma(raw) if "feeSchedule" in raw else None
    fee_rate = schedule.effective_rate if schedule else ASSUMED_UNKNOWN_FEE_RATE
    token_yes = str(tokens[outcomes.index("yes")] if "yes" in outcomes else tokens[0])
    token_no = str(tokens[outcomes.index("no")] if "no" in outcomes else tokens[1])
    return ResolvedMarket(
        condition_id=str(raw.get("conditionId") or ""),
        question=str(raw.get("question") or ""),
        token_yes=token_yes,
        token_no=token_no,
        resolved_yes=yes_price == 1.0,
        fee_rate=fee_rate,
        end_date=_parse_dt(raw.get("endDate")),
    )


def fetch_resolved_markets(pages: int, page_size: int = 100) -> list[ResolvedMarket]:
    """Newest-first closed markets, paged."""
    found: dict[str, ResolvedMarket] = {}
    for page in range(pages):
        payload = _get_json(
            f"{GAMMA}/markets",
            {
                "closed": "true",
                "limit": str(page_size),
                "offset": str(page * page_size),
                "order": "endDate",
                "ascending": "false",
            },
        )
        if not isinstance(payload, list) or not payload:
            break
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            market = parse_resolved(raw)
            if market and market.condition_id:
                found[market.condition_id] = market
        if len(payload) < page_size:
            break
    return list(found.values())


def fetch_price_history(token_id: str, *, hours: float = 12.0) -> list[tuple[int, float]]:
    """Recent price points for one token, oldest first."""
    now = int(time.time())
    payload = _get_json(
        f"{CLOB}/prices-history",
        {
            "market": token_id,
            "startTs": str(now - int(hours * 3600) - 7 * 86400),
            "endTs": str(now),
            "fidelity": "1",
        },
    )
    history = payload.get("history") if isinstance(payload, dict) else None
    points: list[tuple[int, float]] = []
    for row in history or []:
        try:
            points.append((int(row["t"]), float(row["p"])))
        except (KeyError, TypeError, ValueError):
            continue
    points.sort()
    return points


def observation_from_history(
    market: ResolvedMarket, points: list[tuple[int, float]], *, lead_hours: float
) -> Observation | None:
    """Price ``lead_hours`` before trading stopped, i.e. before resolution."""
    if len(points) < 2:
        return None
    last_ts, _ = points[-1]
    target = last_ts - int(lead_hours * 3600)
    before = [row for row in points if row[0] <= target]
    if not before:
        return None
    _, price = before[-1]
    if not 0.0 < price < 1.0:  # a 0/1 price is already the answer
        return None
    return Observation(
        condition_id=market.condition_id,
        price=price,
        resolved_yes=market.resolved_yes,
        lead_hours=lead_hours,
    )


def collect(
    *,
    pages: int,
    lead_hours: float,
    limit: int,
    sleep_seconds: float,
) -> tuple[list[Observation], list[ResolvedMarket]]:
    markets = fetch_resolved_markets(pages)[:limit]
    logger.info("Fetched %d resolved markets; walking price history", len(markets))
    observations: list[Observation] = []
    for index, market in enumerate(markets, start=1):
        try:
            points = fetch_price_history(market.token_yes)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            logger.warning("history failed for %s: %s", market.condition_id[:10], exc)
            continue
        observation = observation_from_history(market, points, lead_hours=lead_hours)
        if observation is not None:
            observations.append(observation)
        if index % 25 == 0:
            logger.info("%d/%d markets, %d usable", index, len(markets), len(observations))
        if sleep_seconds:
            time.sleep(sleep_seconds)
    return observations, markets


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--markets", type=int, default=200, help="how many resolved markets to study"
    )
    parser.add_argument("--pages", type=int, default=20, help="Gamma pages to page through")
    parser.add_argument(
        "--lead-hours", type=float, default=2.0, help="price lead time before resolution"
    )
    parser.add_argument(
        "--fee-rate", type=float, default=None, help="override the per-market fee rate"
    )
    parser.add_argument("--min-ev", type=float, default=0.02, help="the EV gate to test against")
    parser.add_argument(
        "--assumed-hold", type=float, default=0.95, help="currently configured hold"
    )
    parser.add_argument(
        "--min-ask", type=float, default=0.90, help="lower edge of the band under test"
    )
    parser.add_argument(
        "--max-ask", type=float, default=0.94, help="upper edge of the band under test"
    )
    parser.add_argument("--min-samples", type=int, default=30)
    parser.add_argument("--sleep", type=float, default=0.05, help="pause between CLOB calls")
    parser.add_argument("--out", type=Path, default=None, help="write the evidence JSON here")
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s"
    )

    observations, markets = collect(
        pages=args.pages, lead_hours=args.lead_hours, limit=args.markets, sleep_seconds=args.sleep
    )
    if not observations:
        logger.error(
            "No usable observations. Nothing can be concluded; do not enable the strategy."
        )
        return 2

    edges = list(DEFAULT_EDGES)
    fee_rates = (
        [args.fee_rate] if args.fee_rate is not None else sorted({m.fee_rate for m in markets})
    )
    primary_fee = args.fee_rate if args.fee_rate is not None else max(fee_rates)

    rows = bucket_report(
        observations,
        edges,
        fee_rate=primary_fee,
        min_ev=args.min_ev,
        n_trials=max(1, len(edges) - 1),
    )
    verdict = evaluate_snipe_hypothesis(
        observations,
        price_low=args.min_ask,
        price_high=args.max_ask,
        fee_rate=primary_fee,
        min_ev=args.min_ev,
        assumed_hold=args.assumed_hold,
        min_samples=args.min_samples,
        n_trials=max(1, len(edges) - 1),
    )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lead_hours": args.lead_hours,
        "fee_rate_used": primary_fee,
        "fee_rates_seen": fee_rates,
        "markets_scanned": len(markets),
        "observations": len(observations),
        "buckets": rows,
        "verdict": verdict.as_dict(),
        "arbitrage": evaluate_arbitrage_hypothesis(
            [], fee_rate=primary_fee, threshold=0.985, min_samples=args.min_samples
        ).as_dict(),
    }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"\nStudied {len(observations)} resolved markets "
            f"(lead {args.lead_hours:.1f}h, fee {primary_fee:.1%}).\n"
        )
        print(f"{'band':>14} {'n':>4} {'rate':>7} {'ci_low':>7} {'need':>7} {'net roi':>8}  ok")
        for row in rows:
            band = f"{row['band'][0]:.2f}-{row['band'][1]:.2f}"
            print(
                f"{band:>14} {row['n']:>4} {row['rate']:>7.3f} {row['ci_low']:>7.3f} "
                f"{row['required']:>7.3f} {row['net_roi']:>+8.2%}  {'yes' if row['clears'] else 'no'}"
            )
        print()
        print(verdict.describe())
        for note in verdict.notes:
            print(f"  note: {note}")
        if verdict.verdict != "ACCEPT":
            print("\nDo not enable this strategy on this evidence.")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        evidence = build_report(
            rate=verdict.measured_rate,
            ci_low=verdict.ci_low,
            ci_high=verdict.ci_high,
            n=verdict.n,
            price_low=args.min_ask,
            price_high=args.max_ask,
            lead_hours=args.lead_hours,
            fee_rate=primary_fee,
            verdict=verdict.verdict,
            source=f"strategy_lab:{args.lead_hours:.1f}h/{len(observations)}obs",
            trials=max(1, len(edges) - 1),
            notes=verdict.notes,
        )
        # Write the bot-readable artifact next to the full study.
        evidence_path = args.out.with_name("hold_evidence.json")
        evidence.save(evidence_path)
        print(f"\nwrote {args.out} and {evidence_path}")
        print(f"enable with: EVIDENCE_PATH={evidence_path}")
    return 0 if verdict.verdict == "ACCEPT" else 1


if __name__ == "__main__":
    raise SystemExit(main())
