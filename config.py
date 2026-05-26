"""
ORB Intraday Trading System - Configuration Module
====================================================
Loads all configuration from .env file with sensible defaults.
Provides a singleton Settings instance used across the entire system.
All time constants are in IST (Asia/Kolkata).
"""

import os
import logging
from datetime import time
from pathlib import Path
from dataclasses import dataclass, field

import pytz
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load .env from project root (same directory as this file)
# ---------------------------------------------------------------------------
_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=True)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Timezone constant – used everywhere in the project
# ---------------------------------------------------------------------------
IST = pytz.timezone("Asia/Kolkata")


@dataclass
class Settings:
    """
    Central configuration for the ORB trading system.

    Values are read from environment variables on construction.
    Attributes can be updated at runtime (e.g. access token refresh
    from the dashboard).
    """

    # --- Broker credentials ---------------------------------------------------
    DHAN_CLIENT_ID: str = field(
        default_factory=lambda: os.getenv("DHAN_CLIENT_ID", "")
    )
    DHAN_ACCESS_TOKEN: str = field(
        default_factory=lambda: os.getenv("DHAN_ACCESS_TOKEN", "")
    )

    # --- Telegram notifications -----------------------------------------------
    TELEGRAM_BOT_TOKEN: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", "")
    )
    TELEGRAM_CHAT_ID: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", "")
    )

    # --- Capital & risk -------------------------------------------------------
    BASE_CAPITAL: float = field(
        default_factory=lambda: float(os.getenv("BASE_CAPITAL", "5000"))
    )
    LEVERAGE: int = field(
        default_factory=lambda: int(os.getenv("LEVERAGE", "5"))
    )
    MAX_RANGE_PCT: float = field(
        default_factory=lambda: float(os.getenv("MAX_RANGE_PCT", "1.5"))
    )
    MIN_STOCK_PRICE: float = field(
        default_factory=lambda: float(os.getenv("MIN_STOCK_PRICE", "60"))
    )

    # --- Risk-reward constants ------------------------------------------------
    RR_RATIO: float = 2.0
    PARTIAL_BOOK_PCT: float = 0.5

    # --- Mode -----------------------------------------------------------------
    PAPER_TRADING: bool = field(
        default_factory=lambda: os.getenv("PAPER_TRADING", "false").lower()
        in ("true", "1", "yes")
    )
    MAX_QTY_PER_TRADE: int = field(
        default_factory=lambda: int(os.getenv("MAX_QTY_PER_TRADE", "1"))
    )

    # --- Security & storage ---------------------------------------------------
    WEBHOOK_SECRET: str = field(
        default_factory=lambda: os.getenv("WEBHOOK_SECRET", "changeme")
    )
    DB_PATH: str = field(
        default_factory=lambda: os.getenv("DB_PATH", "data/trades.db")
    )
    DASHBOARD_PASSWORD: str = field(
        default_factory=lambda: os.getenv("DASHBOARD_PASSWORD", "admin123")
    )

    # --- Logging --------------------------------------------------------------
    LOG_LEVEL: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper()
    )

    # --- Market time constants (IST) ------------------------------------------
    MARKET_OPEN: time = time(9, 15)
    ORB_CANDLE_CLOSE: time = time(9, 20)
    SCAN_CUTOFF: time = time(10, 0)
    SQUARE_OFF_TIME: time = time(14, 30)
    MARKET_CLOSE: time = time(15, 30)

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------

    def update_access_token(self, new_token: str) -> None:
        """
        Hot-swap the Dhan access token at runtime.

        Called from the Streamlit dashboard when a user pastes a fresh
        token after the daily expiry.  Also persists to the .env file
        so the next restart picks it up automatically.
        """
        old_masked = self.DHAN_ACCESS_TOKEN[-6:] if self.DHAN_ACCESS_TOKEN else "N/A"
        self.DHAN_ACCESS_TOKEN = new_token
        logger.info(
            "Access token updated at runtime (old tail …%s → new tail …%s)",
            old_masked,
            new_token[-6:] if new_token else "N/A",
        )

        # Best-effort persist to .env so restarts use the new token
        try:
            self._persist_token_to_env(new_token)
        except Exception:
            logger.warning(
                "Could not persist new token to .env – will use in-memory value only",
                exc_info=True,
            )

    def _persist_token_to_env(self, new_token: str) -> None:
        """Rewrite the DHAN_ACCESS_TOKEN line in .env (if the file exists)."""
        if not _ENV_PATH.exists():
            return

        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
        found = False
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("DHAN_ACCESS_TOKEN="):
                lines[idx] = f"DHAN_ACCESS_TOKEN={new_token}\n"
                found = True
                break
        if not found:
            lines.append(f"\nDHAN_ACCESS_TOKEN={new_token}\n")

        _ENV_PATH.write_text("".join(lines), encoding="utf-8")
        logger.debug("Persisted new access token to %s", _ENV_PATH)

    def validate(self) -> list[str]:
        """
        Return a list of configuration warnings / errors.

        An empty list means everything looks fine.
        """
        issues: list[str] = []
        if not self.DHAN_CLIENT_ID:
            issues.append("DHAN_CLIENT_ID is not set")
        if not self.DHAN_ACCESS_TOKEN:
            issues.append("DHAN_ACCESS_TOKEN is not set")
        if not self.TELEGRAM_BOT_TOKEN:
            issues.append("TELEGRAM_BOT_TOKEN is not set – notifications disabled")
        if not self.TELEGRAM_CHAT_ID:
            issues.append("TELEGRAM_CHAT_ID is not set – notifications disabled")
        if self.BASE_CAPITAL <= 0:
            issues.append(f"BASE_CAPITAL must be > 0 (got {self.BASE_CAPITAL})")
        if self.LEVERAGE < 1:
            issues.append(f"LEVERAGE must be >= 1 (got {self.LEVERAGE})")
        return issues

    @property
    def effective_capital(self) -> float:
        """Total deployable capital after leverage."""
        return self.BASE_CAPITAL * self.LEVERAGE

    @property
    def mode_label(self) -> str:
        """Human-readable trading mode string."""
        return "📝 PAPER" if self.PAPER_TRADING else "💰 LIVE"

    def __repr__(self) -> str:
        return (
            f"Settings(mode={self.mode_label}, "
            f"capital=₹{self.effective_capital:,.0f}, "
            f"leverage={self.LEVERAGE}x, "
            f"max_range={self.MAX_RANGE_PCT}%)"
        )


# ---------------------------------------------------------------------------
# Singleton instance – import this everywhere:
#   from config import settings, IST
# ---------------------------------------------------------------------------
settings = Settings()
