#!/usr/bin/env python3
"""Run the read-only console against a seeded, throwaway ledger.

This is the fastest way to look at the dashboard without a wallet, an account or
a running trading loop:

    python scripts/demo_server.py            # http://127.0.0.1:8099
    python scripts/demo_server.py --port 3000 --host 0.0.0.0

The script writes a temporary SQLite ledger with a handful of plausible
DRY-RUN signals and orders, then serves the same endpoints ``main.py`` serves.
Nothing here touches the network or the real ledger path.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web  # noqa: E402
from ledger import Ledger  # noqa: E402

DEMO_SIGNALS = [
    ("MODEL YES edge=0.0812 ask=0.5800 kelly=0.24 prob=0.6612", 0.58, 12.50, 1.31, 24),
    ("ARB YES+NO=0.9760 profit_per_pair=0.0240 shares=18.44", 0.471, 8.90, 0.14, 61),
    ("ARB YES+NO=0.9760 profit_per_pair=0.0240 shares=18.44", 0.505, 9.30, 0.07, 64),
    ("SNIPE YES ttl_h=1.42 ask=0.9370 hold=0.95 ev_roi=0.0139", 0.937, 20.00, 0.28, 188),
    ("MODEL NO edge=0.0644 ask=0.3100 kelly=0.09 prob=0.3744", 0.31, 6.20, 0.44, 402),
    (
        "ARB YES+NO=0.9830 profit_per_pair=0.0170 shares=8.42 "
        "| RISK_BLOCKED exposure cap: current=58.40 add=4.10 limit=60.00",
        0.482,
        4.10,
        0.05,
        655,
    ),
    ("SNIPE NO ttl_h=0.87 ask=0.9410 hold=0.95 ev_roi=0.0096", 0.941, 20.00, 0.19, 912),
    ("MODEL YES edge=0.0731 ask=0.6600 kelly=0.22 prob=0.7331", 0.66, 9.80, 0.72, 1280),
]

MARKETS = [
    "0x9f2c41ab7de0c8b1e5a37c2f1d8b6a43c9e7f0d2",
    "0x1b8d05cf3a92e64b7d10af82c5e39b471d6c0a88",
    "0x47ae93c1d2065f8b3c94e17a05d2b86f1e3a9c05",
    "0x2c6f18d9b7a04e53c81072fd6b49a3e58c2d0f71",
    "0x83d5ba0f2e64c791d0a8b35f7c14e690ab27d3c8",
]


def seed(ledger: Ledger) -> None:
    now = datetime.now(timezone.utc)
    ledger.record_start(mode="dry_run")
    for index, (reason, price, size, expected, age) in enumerate(DEMO_SIGNALS):
        suffix = " | DRY_RUN" if "RISK_BLOCKED" not in reason else ""
        ledger.record_signal(
            market_id=MARKETS[index % len(MARKETS)],
            token_id=f"7{(index + 3) * 111111111111}{index}9d4f",
            side="BUY",
            price=price,
            size_usd=size,
            reason=f"{reason}{suffix}",
            expected_value=expected,
            ts=(now - timedelta(seconds=age)).isoformat(timespec="milliseconds"),
        )
    ledger.record_cycle(
        markets_scanned=40,
        markets_evaluated=37,
        signals_emitted=2,
        risk={
            "equity": 203.42,
            "cash": 185.82,
            "exposure": 17.60,
            "realized_pnl": 3.42,
            "drawdown_pct": 0.008,
            "killed": False,
            "kill_reason": None,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument(
        "--data-dir",
        default="",
        help="Keep the demo ledger here instead of a temporary directory",
    )
    parser.add_argument("--no-seed", action="store_true", help="Serve an empty ledger instead")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.data_dir:
        data_dir = Path(args.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        cleanup = None
    else:
        cleanup = tempfile.TemporaryDirectory()
        data_dir = Path(cleanup.name)

    ledger_path = data_dir / "demo-ledger.db"
    if not ledger_path.exists():
        ledger = Ledger(ledger_path)
        try:
            if args.no_seed:
                ledger.record_start(mode="dry_run")
            else:
                seed(ledger)
        finally:
            ledger.close()

    server, _thread = web.start_status_server(
        ledger_path,
        host=args.host,
        port=args.port,
        dashboard_token="",
        dashboard_dir=web.default_dashboard_dir(),
    )
    host, port = server.server_address[:2]
    print(f"\n  Demo console:  http://{host}:{port}\n  Ledger:        {ledger_path}\n")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nShutting down")
    finally:
        web.stop_status_server(server, None)
        if cleanup is not None:
            cleanup.cleanup()


if __name__ == "__main__":
    main()
