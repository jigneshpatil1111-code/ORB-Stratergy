"""
ORB Intraday Trading System - Utility Helpers
===============================================
Pure-function helpers used across the project: logging setup,
IST time operations, market-day checks, formatting, and ID generation.
"""

import logging
import os
import uuid
from datetime import date, datetime, time, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytz

from config import IST

# ---------------------------------------------------------------------------
# NSE Holidays 2025-2026 (gazetted + special trading holidays)
# Source: NSE circulars – update annually before Jan 1.
# ---------------------------------------------------------------------------
_NSE_HOLIDAYS: set[date] = {
    # --- 2025 ---
    date(2025, 2, 26),   # Mahashivratri
    date(2025, 3, 14),   # Holi
    date(2025, 3, 31),   # Id-ul-Fitr (Ramadan)
    date(2025, 4, 10),   # Shri Mahavir Jayanti
    date(2025, 4, 14),   # Dr. Ambedkar Jayanti
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 1),    # Maharashtra Day
    date(2025, 6, 7),    # Bakri Id (Eid-ul-Adha)
    date(2025, 8, 15),   # Independence Day
    date(2025, 8, 27),   # Ganesh Chaturthi
    date(2025, 10, 2),   # Mahatma Gandhi Jayanti
    date(2025, 10, 21),  # Diwali (Laxmi Pujan)
    date(2025, 10, 22),  # Diwali Balipratipada
    date(2025, 11, 5),   # Gurunanak Jayanti
    date(2025, 12, 25),  # Christmas
    # --- 2026 ---
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 3),    # Holi
    date(2026, 3, 26),   # Shri Ram Navami
    date(2026, 3, 31),   # Shri Mahavir Jayanti
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 28),   # Bakri Id (Eid-ul-Adha)
    date(2026, 6, 26),   # Muharram
    date(2026, 9, 14),   # Ganesh Chaturthi
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra
    date(2026, 11, 10),  # Diwali Balipratipada
    date(2026, 11, 24),  # Gurunanak Jayanti
    date(2026, 12, 25),  # Christmas
}

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def setup_logging(level: str = "INFO") -> None:
    """
    Configure the root logger with:
      • Rotating file handler  → logs/orb_system.log  (5 MB × 5 backups)
      • Console (stderr) handler with coloured level names

    Call once at application startup.

    Args:
        level: Logging level string – DEBUG, INFO, WARNING, ERROR, CRITICAL.
    """
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "orb_system.log"

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # Root logger
    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Avoid duplicate handlers on repeated calls
    if root.handlers:
        root.handlers.clear()

    # --- File handler (detailed) ------------------------------------------
    file_fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-24s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        filename=str(log_file),
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(file_fmt)
    root.addHandler(file_handler)

    # --- Console handler (coloured) ---------------------------------------
    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(_ColouredFormatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(console_handler)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("dhanhq").setLevel(logging.WARNING)

    logger.info(
        "Logging initialised – level=%s, file=%s", level.upper(), log_file
    )


class _ColouredFormatter(logging.Formatter):
    """
    Adds ANSI colour codes to log level names for console readability.
    Falls back gracefully on terminals that don't support colour.
    """

    _COLOURS = {
        logging.DEBUG:    "\033[36m",   # Cyan
        logging.INFO:     "\033[32m",   # Green
        logging.WARNING:  "\033[33m",   # Yellow
        logging.ERROR:    "\033[31m",   # Red
        logging.CRITICAL: "\033[1;31m", # Bold red
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        colour = self._COLOURS.get(record.levelno, "")
        record.levelname = f"{colour}{record.levelname}{self._RESET}"
        return super().format(record)


# ---------------------------------------------------------------------------
# IST time helpers
# ---------------------------------------------------------------------------


def get_ist_now() -> datetime:
    """Return the current datetime in IST (Asia/Kolkata), timezone-aware."""
    return datetime.now(tz=IST)


def is_market_day(dt: date | None = None) -> bool:
    """
    Check if a given date is an NSE trading day.

    A trading day is a weekday (Mon-Fri) that is NOT in the NSE holiday
    calendar.

    Args:
        dt: Date to check. Defaults to today (IST).

    Returns:
        True if the date is a valid market day.
    """
    if dt is None:
        dt = get_ist_now().date()
    elif isinstance(dt, datetime):
        dt = dt.date()

    # Saturday = 5, Sunday = 6
    if dt.weekday() >= 5:
        return False

    if dt in _NSE_HOLIDAYS:
        return False

    return True


def is_in_time_window(start: time, end: time) -> bool:
    """
    Check whether the current IST time falls within [start, end) (inclusive
    start, exclusive end).

    Args:
        start: Window start time (IST, naive).
        end:   Window end time   (IST, naive).

    Returns:
        True if current IST time is in [start, end).
    """
    now_time = get_ist_now().time()
    return start <= now_time < end


def round_to_5min(dt: datetime) -> datetime:
    """
    Floor-round a datetime to the nearest 5-minute boundary.

    Example:
        09:17:43 → 09:15:00
        09:20:01 → 09:20:00

    Args:
        dt: The datetime to round (timezone is preserved).

    Returns:
        A new datetime floored to the nearest 5-minute mark.
    """
    floored_minute = (dt.minute // 5) * 5
    return dt.replace(minute=floored_minute, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_currency(amount: float) -> str:
    """
    Format a numeric amount as Indian Rupees.

    Examples:
        1234.5  → '₹1,234.50'
        -500.0  → '-₹500.00'

    Args:
        amount: The amount in INR.

    Returns:
        Formatted currency string.
    """
    if amount < 0:
        return f"-₹{abs(amount):,.2f}"
    return f"₹{amount:,.2f}"


def format_pnl(amount: float) -> str:
    """
    Format a PnL value with a green (profit) or red (loss) emoji prefix.

    Examples:
        150.75  → '🟢 +₹150.75'
        -80.20  → '🔴 -₹80.20'
         0.00   → '⚪ ₹0.00'

    Args:
        amount: Profit or loss value in INR.

    Returns:
        Emoji-prefixed formatted string.
    """
    if amount > 0:
        return f"🟢 +₹{amount:,.2f}"
    elif amount < 0:
        return f"🔴 -₹{abs(amount):,.2f}"
    return f"⚪ ₹{amount:,.2f}"


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------


def generate_correlation_id() -> str:
    """
    Generate a short, unique correlation ID for order/event tracking.

    Format: ``ORB-<8-char-hex>``  e.g. ``ORB-a3f7b2c1``

    Returns:
        A unique correlation ID string.
    """
    return f"ORB-{uuid.uuid4().hex[:8]}"
