"""Measured evidence about a strategy, and the gate that applies it.

This file is the difference between "we assume the hold rate is 95%" and "we
measured it at 92.1% on 214 markets, so the gate now uses 89.6%, the
pessimistic end of the interval".

The bot never trusts a number it has not measured more than the number it was
configured with: a report can only make the assumption *more conservative*.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MIN_SAMPLES_FOR_GATE = 30


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class HoldEvidence:
    """What the SNIPE thesis was measured to be."""

    rate: float
    ci_low: float
    ci_high: float
    n: int
    price_low: float
    price_high: float
    lead_hours: float
    fee_rate: float
    verdict: str = "UNKNOWN"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def usable(self) -> bool:
        return self.n >= MIN_SAMPLES_FOR_GATE and self.ci_low > 0.0


@dataclass(frozen=True, slots=True)
class EvidenceReport:
    """The artifact `scripts/strategy_lab.py` writes and the bot reads."""

    generated_at: str
    source: str
    hold: HoldEvidence
    trials: int = 1
    notes: list[str] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "source": self.source,
            "trials": self.trials,
            "notes": list(self.notes),
            "hold": self.hold.as_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json() + "\n", encoding="utf-8")
        return target

    @classmethod
    def from_dict(cls, payload: Any) -> EvidenceReport:
        if not isinstance(payload, dict):
            raise ValueError("evidence report must be a JSON object")
        hold_raw = payload.get("hold")
        if not isinstance(hold_raw, dict):
            raise ValueError("evidence report is missing a 'hold' block")
        version = int(payload.get("schema_version", SCHEMA_VERSION))
        if version > SCHEMA_VERSION:
            raise ValueError(f"evidence schema {version} is newer than this bot understands")
        hold = HoldEvidence(
            rate=float(hold_raw["rate"]),
            ci_low=float(hold_raw["ci_low"]),
            ci_high=float(hold_raw["ci_high"]),
            n=int(hold_raw["n"]),
            price_low=float(hold_raw.get("price_low", 0.0)),
            price_high=float(hold_raw.get("price_high", 1.0)),
            lead_hours=float(hold_raw.get("lead_hours", 0.0)),
            fee_rate=float(hold_raw.get("fee_rate", 0.0)),
            verdict=str(hold_raw.get("verdict", "UNKNOWN")),
        )
        if not 0.0 <= hold.ci_low <= hold.rate <= hold.ci_high <= 1.0:
            raise ValueError("evidence interval is inconsistent")
        return cls(
            generated_at=str(payload.get("generated_at", "")),
            source=str(payload.get("source", "unknown")),
            hold=hold,
            trials=int(payload.get("trials", 1)),
            notes=[str(note) for note in payload.get("notes", []) or []],
            schema_version=version,
        )


def load_evidence(path: str | Path | None) -> EvidenceReport | None:
    """Load a report, or return ``None`` with a logged reason.

    A malformed report must never take the bot down: evidence is an input, not
    a dependency.
    """
    if not path:
        return None
    target = Path(path)
    if not target.exists():
        logger.info("No evidence report at %s; the hold assumption stays unverified", target)
        return None
    try:
        return EvidenceReport.from_dict(json.loads(target.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001 - a bad report must not stop trading
        logger.warning("Ignoring unreadable evidence report %s: %s", target, exc)
        return None


def build_report(
    *,
    rate: float,
    ci_low: float,
    ci_high: float,
    n: int,
    price_low: float,
    price_high: float,
    lead_hours: float,
    fee_rate: float,
    verdict: str,
    source: str,
    trials: int = 1,
    notes: list[str] | None = None,
) -> EvidenceReport:
    return EvidenceReport(
        generated_at=_utc_now().isoformat(),
        source=source,
        hold=HoldEvidence(
            rate=rate,
            ci_low=ci_low,
            ci_high=ci_high,
            n=n,
            price_low=price_low,
            price_high=price_high,
            lead_hours=lead_hours,
            fee_rate=fee_rate,
            verdict=verdict,
        ),
        trials=trials,
        notes=list(notes or []),
    )


class EvidenceGate:
    """Decides which hold probability the strategy is allowed to use."""

    def __init__(self, report: EvidenceReport | None = None) -> None:
        self.report = report

    @property
    def measured(self) -> HoldEvidence | None:
        if self.report and self.report.hold.usable:
            return self.report.hold
        return None

    def effective_hold(self, assumed: float) -> float:
        """Never more optimistic than the assumption, never more than measured."""
        measured = self.measured
        if measured is None:
            return assumed
        return min(float(assumed), measured.ci_low)

    def describe(self, assumed: float) -> str:
        measured = self.measured
        if measured is None:
            return (
                f"hold={assumed:.4f} (assumed, unverified: run "
                f"`python scripts/strategy_lab.py` to measure it)"
            )
        return (
            f"hold={self.effective_hold(assumed):.4f} (measured "
            f"{measured.rate:.4f} on n={measured.n}, {measured.verdict}; "
            f"using the pessimistic {measured.ci_low:.4f})"
        )

    def as_summary(self, assumed: float) -> dict[str, Any]:
        measured = self.measured
        return {
            "effective_hold": round(self.effective_hold(assumed), 6),
            "assumed_hold": round(float(assumed), 6),
            "source": "measured" if measured else "assumed",
            "n": measured.n if measured else 0,
            "measured_rate": round(measured.rate, 6) if measured else None,
            "ci": [round(measured.ci_low, 6), round(measured.ci_high, 6)] if measured else None,
            "verdict": measured.verdict if measured else "UNVERIFIED",
            "generated_at": self.report.generated_at if self.report else None,
        }
