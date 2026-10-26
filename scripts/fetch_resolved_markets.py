#!/usr/bin/env python3
"""Build a leak-free training CSV for the calibrated model.

Two steps, because Polymarket cannot give you both halves at once:

  1. labels   — fetch resolved markets from Gamma and record which way they
                settled (``resolved_yes``).
  2. join     — pair every label with the *last feature snapshot captured before
                resolution* (``snapshots.py``), which is the only honest source
                of pre-resolution features.

Usage
-----
    # 1. Outcomes only (no features). Safe to run repeatedly.
    python scripts/fetch_resolved_markets.py --out resolved.csv --pages 10

    # 2. Join with snapshots the bot recorded while markets were open.
    python scripts/fetch_resolved_markets.py \\
        --snapshots /data/snapshots.csv --labels resolved.csv --out training.csv

    # 3. Train (scikit-learn only; fits a 512 MB free instance).
    python train_model.py --data training.csv --output model.joblib --backend light

Why the join is not optional
----------------------------
A closed market's Gamma record reports its *final* prices (1 and 0). Building
features from those and training on them produces a model that looks perfect and
predicts nothing: the label is inside the feature vector. This script therefore
requires snapshots, and drops any snapshot whose ``captured_at`` is at or after
the market's resolution, plus any row captured within ``--min-lead-minutes`` of
it (the outcome is already effectively known by then).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features import FEATURE_COLUMNS  # noqa: E402
from scanner import parse_datetime  # noqa: E402

logger = logging.getLogger("fetch_resolved_markets")

DEFAULT_GAMMA_URL = "https://gamma-api.polymarket.com"
LABEL_COLUMN = "resolved_yes"
USER_AGENT = "polymarket-calibrated-bot/1.0 (+training-data)"


# --------------------------------------------------------------------- labels


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def label_from_market(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """Extract ``{condition_id, resolved_yes, end_date, volume, liquidity}``.

    Returns ``None`` for anything that has not actually resolved. A market
    settles when its outcome prices are at the extremes (one side at 1). Until
    then the prices are just the book, and using them as a label would teach the
    model that every market resolves at its current mid.
    """
    condition_id = str(raw.get("conditionId") or raw.get("condition_id") or "").strip()
    if not condition_id:
        return None

    prices = [_float(value) for value in _json_list(raw.get("outcomePrices"))]
    outcomes = [str(value).strip().lower() for value in _json_list(raw.get("outcomes"))]
    if len(prices) != 2 or any(price is None for price in prices):
        return None

    yes_index = outcomes.index("yes") if "yes" in outcomes else 0
    yes_price = float(prices[yes_index])
    no_price = float(prices[1 - yes_index])
    # Only a decided market: one side at ~1, the other at ~0.
    if not (yes_price >= 0.999 or no_price >= 0.999) or abs(yes_price - no_price) < 0.998:
        return None

    end_date = parse_datetime(
        raw.get("closedTime") or raw.get("endDate") or raw.get("endDateIso") or raw.get("end_date")
    )
    return {
        "condition_id": condition_id,
        "resolved_yes": 1 if yes_price >= 0.999 else 0,
        "end_date": end_date.isoformat(timespec="seconds") if end_date else "",
        "volume": _float(raw.get("volumeNum") or raw.get("volume")) or 0.0,
        "liquidity": _float(raw.get("liquidityNum") or raw.get("liquidity")) or 0.0,
    }


def fetch_labels(
    out_path: Path,
    *,
    base_url: str = DEFAULT_GAMMA_URL,
    pages: int = 10,
    page_size: int = 500,
    timeout: float = 30.0,
) -> int:
    """Page Gamma for closed markets and write the label table."""
    import httpx

    rows: dict[str, dict[str, Any]] = {}
    with httpx.Client(
        base_url=base_url.rstrip("/"),
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for page in range(pages):
            params = {
                "closed": "true",
                "limit": str(page_size),
                "offset": str(page * page_size),
                "order": "volume",
                "ascending": "false",
            }
            response = client.get("/markets", params=params)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, Mapping):
                payload = payload.get("data", payload.get("markets", []))
            if not isinstance(payload, Sequence) or isinstance(payload, str | bytes):
                raise SystemExit("Unexpected Gamma /markets response")
            if not payload:
                break
            new = 0
            for raw in payload:
                if not isinstance(raw, Mapping):
                    continue
                row = label_from_market(raw)
                if row is None or row["condition_id"] in rows:
                    continue
                rows[row["condition_id"]] = row
                new += 1
            logger.info(
                "page %d: %d markets, %d resolved (%d total)",
                page + 1,
                len(payload),
                new,
                len(rows),
            )
            if len(payload) < page_size:
                break

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["condition_id", LABEL_COLUMN, "end_date", "volume", "liquidity"],
        )
        writer.writeheader()
        writer.writerows(rows.values())
    logger.info("Wrote %d resolved markets to %s", len(rows), out_path)
    return len(rows)


# ----------------------------------------------------------------------- join


def _parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return parse_datetime(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def build_training_rows(
    labels: Iterable[Mapping[str, Any]],
    snapshots: Iterable[Mapping[str, Any]],
    *,
    min_lead_minutes: float = 30.0,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Join labels to the last snapshot taken safely before resolution.

    Returns the rows plus a small count of why rows were discarded, so a thin
    dataset has a visible cause instead of being a mystery.
    """
    label_map: dict[str, Mapping[str, Any]] = {
        str(row["condition_id"]).strip(): row for row in labels if row.get("condition_id")
    }
    best: dict[str, tuple[datetime, Mapping[str, Any]]] = {}
    stats = {"snapshots": 0, "no_label": 0, "no_timestamp": 0, "too_late": 0, "kept": 0}

    for row in snapshots:
        stats["snapshots"] += 1
        condition_id = str(row.get("condition_id") or "").strip()
        label = label_map.get(condition_id)
        if label is None:
            stats["no_label"] += 1
            continue
        captured = _parse_ts(row.get("captured_at"))
        resolved = _parse_ts(label.get("end_date"))
        if captured is None or resolved is None:
            stats["no_timestamp"] += 1
            continue
        lead_minutes = (resolved - captured).total_seconds() / 60.0
        if lead_minutes < min_lead_minutes:
            stats["too_late"] += 1
            continue
        previous = best.get(condition_id)
        if previous is None or captured > previous[0]:
            best[condition_id] = (captured, row)

    rows: list[dict[str, Any]] = []
    for condition_id, (captured, row) in best.items():
        label = label_map[condition_id]
        merged: dict[str, Any] = {
            "condition_id": condition_id,
            "captured_at": captured.isoformat(timespec="seconds"),
            "end_date": label.get("end_date") or "",
            LABEL_COLUMN: int(label[LABEL_COLUMN]),
        }
        missing = False
        for column in FEATURE_COLUMNS:
            value = row.get(column)
            if value in (None, ""):
                missing = True
                break
            merged[column] = float(value)
        if missing:
            continue
        rows.append(merged)

    rows.sort(key=lambda item: (item["end_date"], item["captured_at"]))
    stats["kept"] = len(rows)
    return rows, stats


def read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_training_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["condition_id", "captured_at", "end_date", LABEL_COLUMN, *FEATURE_COLUMNS]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=Path("resolved.csv"))
    parser.add_argument("--gamma-url", default=DEFAULT_GAMMA_URL)
    parser.add_argument("--pages", type=int, default=10)
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument(
        "--snapshots",
        type=Path,
        help="Feature snapshot CSV written by the bot (SNAPSHOT_PATH). Joins instead of fetching.",
    )
    parser.add_argument(
        "--labels",
        type=Path,
        help="Existing label CSV. Skip the network and join with this.",
    )
    parser.add_argument(
        "--min-lead-minutes",
        type=float,
        default=30.0,
        help="Drop snapshots taken this close to resolution: the outcome was already visible",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)

    if args.snapshots is None:
        if args.pages <= 0:
            raise SystemExit("--pages must be positive")
        count = fetch_labels(
            args.out, base_url=args.gamma_url, pages=args.pages, page_size=args.page_size
        )
        if count == 0:
            logger.warning(
                "No resolved markets found. Gamma only reports a market as settled once "
                "its outcome prices are final; try again later or raise --pages."
            )
        return 0

    labels_path = args.labels or args.out
    if not labels_path.exists():
        raise SystemExit(f"Label file {labels_path} does not exist; run without --snapshots first")
    labels = read_csv(labels_path)
    snapshots = read_csv(args.snapshots)
    rows, stats = build_training_rows(labels, snapshots, min_lead_minutes=args.min_lead_minutes)
    logger.info(
        "Join: %d snapshots -> %d rows kept (no_label=%d too_late=%d no_timestamp=%d)",
        stats["snapshots"],
        stats["kept"],
        stats["no_label"],
        stats["too_late"],
        stats["no_timestamp"],
    )
    if stats["kept"] == 0:
        raise SystemExit(
            "No usable rows. Snapshots must be captured while markets are still open "
            "and at least --min-lead-minutes before resolution."
        )
    write_training_csv(args.out, rows)
    logger.info("Wrote %d training rows to %s", len(rows), args.out)
    if len(rows) < 60:
        logger.warning(
            "Only %d rows; ProbabilityModel.fit requires at least 60. Keep capturing.", len(rows)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
