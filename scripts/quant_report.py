#!/usr/bin/env python3
"""Run the three questions the quant architecture memo asks, and answer them.

1. **Is a compound target reachable?**  ``--target 6000 --start 20 --days 3``
   prints the ruin arithmetic instead of an opinion.
2. **Is any NegRisk basket actually cheap right now?**  The only risk-free
   trade on this venue: buy all M outcomes for less than 1.00 after fees.
3. **What would a market maker quote?**  Avellaneda-Stoikov with parameters
   scaled for prices in (0, 1).

Read-only. No key, no wallet, no order. Run it where Polymarket is reachable:

    python scripts/quant_report.py
    python scripts/quant_report.py --target 6000 --start 20 --days 3
    python scripts/quant_report.py --mid 0.62 --inventory 200
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fees import from_gamma  # noqa: E402
from marketmaking import quote  # noqa: E402
from negrisk import BasketLeg, evaluate_basket  # noqa: E402
from research.growth import feasibility  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com"


def _get_json(url: str, params: dict[str, str] | None = None, timeout: float = 20.0) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "quant-report/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return list(value) if isinstance(value, list) else []


def event_legs(event: dict[str, Any]) -> tuple[list[BasketLeg], bool]:
    """Priceable legs of one event, and whether the basket is complete.

    Gamma ignores an ``event_id`` filter on ``/markets``, so membership always
    comes from the event payload itself.
    """
    legs: list[BasketLeg] = []
    complete = True
    for raw in event.get("markets") or []:
        if not isinstance(raw, dict):
            complete = False
            continue
        tokens = _json_list(raw.get("clobTokenIds"))
        outcomes = [str(item).strip().lower() for item in _json_list(raw.get("outcomes"))]
        ask = raw.get("bestAsk")
        tradable = (
            not raw.get("closed")
            and raw.get("active", True)
            and raw.get("acceptingOrders", True)
            and raw.get("enableOrderBook", True)
        )
        if len(tokens) != 2 or ask is None or not tradable:
            complete = False
            continue
        schedule = from_gamma(raw)
        token_yes = str(tokens[outcomes.index("yes")]) if "yes" in outcomes else str(tokens[0])
        legs.append(
            BasketLeg(
                token_id=token_yes,
                label=str(raw.get("groupItemTitle") or raw.get("question") or ""),
                ask=float(ask),
                fee_rate=schedule.effective_rate,
                fee_exponent=schedule.exponent,
            )
        )
    return legs, complete


def scan_negrisk(limit: int = 40, *, get_json=_get_json) -> list[dict[str, Any]]:
    """Cheapest complete NegRisk baskets currently on the book."""
    events = get_json(
        f"{GAMMA}/events",
        {
            "closed": "false",
            "active": "true",
            "limit": str(limit),
            "order": "liquidity",
            "ascending": "false",
        },
    )
    if isinstance(events, dict):
        events = events.get("data", [])
    verdicts = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        legs, complete = event_legs(event)
        if not legs:
            continue
        verdicts.append(
            evaluate_basket(
                legs,
                event_id=str(event.get("id") or ""),
                title=str(event.get("title") or ""),
                complete=complete,
            ).as_dict()
        )
    verdicts.sort(key=lambda item: item["cost"])
    return verdicts


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=float, default=20.0)
    parser.add_argument("--target", type=float, default=6000.0)
    parser.add_argument("--days", type=float, default=3.0)
    parser.add_argument("--entry-price", type=float, default=0.20)
    parser.add_argument("--mid", type=float, default=0.62, help="mid for the market-making quote")
    parser.add_argument("--inventory", type=float, default=0.0)
    parser.add_argument("--events", type=int, default=40, help="NegRisk events to price")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    result = feasibility(args.start, args.target, args.days, entry_price=args.entry_price)
    quote_result = quote(args.mid, inventory=args.inventory)

    baskets: list[dict[str, Any]] = []
    error = ""
    try:
        baskets = scan_negrisk(args.events)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        error = f"could not reach Gamma: {exc}"

    if args.json:
        print(
            json.dumps(
                {
                    "feasibility": result.as_dict(),
                    "quote": quote_result.as_dict(),
                    "baskets_scanned": len(baskets),
                    "cheapest": baskets[:5],
                    "error": error,
                },
                indent=2,
            )
        )
        return 0

    print(result.describe())
    print()
    print(f"Market-making quote at {args.mid:.2f} (inventory {args.inventory:+.0f}):")
    print(
        f"  bid {quote_result.bid:.3f} / ask {quote_result.ask:.3f} "
        f"-> spread {quote_result.spread:.3f} ({quote_result.edge_bps:.0f} bps of mid)"
    )
    print("  makers pay no protocol fee; spread and rebate are the whole edge.")
    print()
    if error:
        print(f"NegRisk scan skipped: {error}")
        return 0
    print(f"NegRisk baskets: {len(baskets)} events priced.")
    cheap = [entry for entry in baskets if entry["profitable"]]
    if cheap:
        print(f"  {len(cheap)} complete basket(s) under 1.00 after fees:")
        for entry in cheap[:5]:
            print(
                f"    {entry['title'][:60]:60s} cost {entry['cost']:.4f} "
                f"profit {entry['profit_per_basket']:.4f}"
            )
    else:
        cheapest = baskets[0] if baskets else None
        if cheapest:
            print(
                f"  none profitable. cheapest was {cheapest['title'][:50]!r} at "
                f"{cheapest['cost']:.4f} ({cheapest['outcomes']} outcomes, "
                f"{'complete' if cheapest['complete'] else 'incomplete'})."
            )
        else:
            print("  no multi-outcome event was returned by Gamma.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
