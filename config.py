"""Environment-driven configuration.

Secrets are deliberately not stored in this object. The executor reads the
Polygon private key directly from the environment at the point of use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv

    # Local convenience only. Render injects environment variables directly.
    load_dotenv()
except ImportError:
    pass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false, got {raw!r}")


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name)
    value = int(raw) if raw not in (None, "") else default
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    value = float(raw) if raw not in (None, "") else default
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    live: bool
    bankroll: float
    max_bet: float
    max_daily_loss_pct: float
    max_drawdown_pct: float
    max_exposure_pct: float

    gamma_api_url: str
    clob_host: str
    ws_market_url: str

    ledger_path: Path
    model_path: Path
    model_url: str
    snapshot_path: Path | None
    log_level: str

    scan_interval_seconds: int
    error_sleep_seconds: int
    scan_page_size: int
    scan_max_pages: int
    max_markets_per_cycle: int
    book_staleness_seconds: int
    signal_cooldown_seconds: int
    stream_backoff_seconds: float
    news_sentiment: float

    min_ev: float
    arb_threshold: float
    snipe_min_ask: float
    snipe_max_ask: float
    snipe_hold_probability: float
    snipe_overdue_hours: float
    snipe_max_ttl_hours: float
    near_resolution_slots: int
    near_resolution_window_hours: float
    near_resolution_overdue_hours: float
    signature_type: int
    funder: str
    cancel_on_shutdown: bool

    web_enabled: bool
    api_host: str
    api_port: int
    serve_dashboard: bool

    @classmethod
    def from_env(cls) -> Settings:
        gamma_api_url = os.getenv("GAMMA_API_URL", "https://gamma-api.polymarket.com").rstrip("/")
        clob_host = os.getenv("CLOB_HOST", "https://clob.polymarket.com").rstrip("/")
        ws_market_url = os.getenv(
            "WS_MARKET_URL",
            "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        )

        signature_type = _env_int("POLYMARKET_SIGNATURE_TYPE", 0)
        if signature_type not in {0, 1, 2}:
            raise ValueError("POLYMARKET_SIGNATURE_TYPE must be 0, 1, or 2")

        settings = cls(
            live=_env_bool("LIVE", False),
            bankroll=_env_float("BANKROLL", 200.0, minimum=1.0),
            max_bet=_env_float("MAX_BET", 20.0, minimum=0.01),
            max_daily_loss_pct=_env_float("MAX_DAILY_LOSS_PCT", 0.20),
            max_drawdown_pct=_env_float("MAX_DRAWDOWN_PCT", 0.40),
            max_exposure_pct=_env_float("MAX_EXPOSURE_PCT", 0.30),
            gamma_api_url=gamma_api_url,
            clob_host=clob_host,
            ws_market_url=ws_market_url,
            ledger_path=Path(os.getenv("LEDGER_PATH", "ledger.db")),
            model_path=Path(os.getenv("MODEL_PATH", "model.joblib")),
            model_url=os.getenv("MODEL_URL", "").strip(),
            snapshot_path=Path(snapshot_path)
            if (snapshot_path := os.getenv("SNAPSHOT_PATH", "").strip())
            else None,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            scan_interval_seconds=_env_int("SCAN_INTERVAL_SECONDS", 60, minimum=10),
            error_sleep_seconds=_env_int("ERROR_SLEEP_SECONDS", 10, minimum=1),
            scan_page_size=_env_int("SCAN_PAGE_SIZE", 100, minimum=1),
            scan_max_pages=_env_int("SCAN_MAX_PAGES", 5, minimum=1),
            max_markets_per_cycle=_env_int("MAX_MARKETS_PER_CYCLE", 40, minimum=1),
            book_staleness_seconds=_env_int("BOOK_STALENESS_SECONDS", 20, minimum=1),
            signal_cooldown_seconds=_env_int("SIGNAL_COOLDOWN_SECONDS", 3600, minimum=0),
            stream_backoff_seconds=_env_float("STREAM_BACKOFF_SECONDS", 2.0, minimum=0.1),
            news_sentiment=_env_float("NEWS_SENTIMENT", 0.0),
            min_ev=_env_float("MIN_EV", 0.02),
            arb_threshold=_env_float("ARB_THRESHOLD", 0.985),
            snipe_min_ask=_env_float("SNIPE_MIN_ASK", 0.93),
            snipe_max_ask=_env_float("SNIPE_MAX_ASK", 0.97),
            snipe_hold_probability=_env_float("SNIPE_HOLD_PROBABILITY", 0.95),
            snipe_overdue_hours=_env_float("SNIPE_OVERDUE_HOURS", 24.0),
            snipe_max_ttl_hours=_env_float("SNIPE_MAX_TTL_HOURS", 2.4),
            near_resolution_slots=_env_int("NEAR_RESOLUTION_SLOTS", 8, minimum=0),
            near_resolution_window_hours=_env_float("NEAR_RESOLUTION_WINDOW_HOURS", 6.0),
            near_resolution_overdue_hours=_env_float("NEAR_RESOLUTION_OVERDUE_HOURS", 24.0),
            signature_type=signature_type,
            funder=os.getenv("POLYMARKET_FUNDER", "").strip(),
            cancel_on_shutdown=_env_bool("CANCEL_ON_SHUTDOWN", True),
            web_enabled=_env_bool("WEB_ENABLED", True),
            api_host=os.getenv("API_HOST", "").strip(),
            api_port=_env_int("API_PORT", 0),
            serve_dashboard=_env_bool("SERVE_DASHBOARD", True),
        )

        if not 0.0 < settings.snipe_min_ask < settings.snipe_max_ask <= 1.0:
            raise ValueError("need 0 < SNIPE_MIN_ASK < SNIPE_MAX_ASK <= 1")
        if not 0.0 < settings.snipe_hold_probability <= 1.0:
            raise ValueError("SNIPE_HOLD_PROBABILITY must be in (0, 1]")
        if settings.arb_threshold > 1.0:
            raise ValueError("ARB_THRESHOLD above 1.0 is not arbitrage")

        for name in ("max_daily_loss_pct", "max_drawdown_pct", "max_exposure_pct"):
            value = getattr(settings, name)
            if not 0.0 < value <= 1.0:
                raise ValueError(f"{name.upper()} must be in (0, 1]")

        if settings.max_bet > settings.bankroll:
            raise ValueError("MAX_BET cannot exceed BANKROLL")

        return settings

    def public_summary(self) -> dict[str, object]:
        """Configuration safe to log (contains no credentials)."""
        return {
            "live": self.live,
            "bankroll": self.bankroll,
            "max_bet": self.max_bet,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_exposure_pct": self.max_exposure_pct,
            "ledger_path": str(self.ledger_path),
            "model_path": str(self.model_path),
            "model_url_configured": bool(self.model_url),
            "snapshot_path": str(self.snapshot_path) if self.snapshot_path else None,
            "near_resolution_slots": self.near_resolution_slots,
            "min_ev": self.min_ev,
            "snipe_band": [self.snipe_min_ask, self.snipe_max_ask],
            "near_resolution_window_hours": self.near_resolution_window_hours,
            "scan_interval_seconds": self.scan_interval_seconds,
            "max_markets_per_cycle": self.max_markets_per_cycle,
            "signal_cooldown_seconds": self.signal_cooldown_seconds,
            "signature_type": self.signature_type,
            "funder_configured": bool(self.funder),
            "web_enabled": self.web_enabled,
            "api_host": self.api_host or "auto",
            "api_port": self.api_port or "auto",
            "serve_dashboard": self.serve_dashboard,
        }
