"""
broker.py - DhanHQ API Wrapper for ORB Intraday Trading System
==============================================================

Wraps the dhanhq v2.2.0 SDK using the DhanContext pattern.
Provides rate limiting (20 req/sec token bucket), automatic retries
with exponential backoff, paper trading mode, and comprehensive logging.

All methods are synchronous and thread-safe for the rate limiter.
"""

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from dhanhq import DhanContext, dhanhq

from config import settings

logger = logging.getLogger(__name__)


class RateLimiter:
    """Token-bucket rate limiter.

    Allows a burst of *capacity* requests and refills at *rate* tokens
    per second.  Thread-safe via a simple lock.

    Default: 20 req/s with burst of 20 — leaves headroom from Dhan's
    25 req/s hard limit.
    """

    def __init__(self, rate: float = 20.0, capacity: int = 20) -> None:
        self.rate: float = rate
        self.capacity: int = capacity
        self._tokens: float = float(capacity)
        self._last_refill: float = time.monotonic()
        self._lock: threading.Lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a token is available."""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
            # No token available — sleep briefly and retry
            time.sleep(0.05)


class DhanBroker:
    """High-level broker interface wrapping the dhanhq SDK.

    Features
    --------
    * Paper-trading mode: when ``settings.PAPER_TRADING`` is True, all
      order-placement calls return simulated responses without hitting
      the API.  Read-only calls (positions, order book, LTP) still hit
      the live API so the strategy sees real prices.
    * Rate limiting via a token-bucket (20 req/s burst).
    * Automatic retries with exponential back-off for transient errors.
    * Every API call is logged at DEBUG level; errors at ERROR level.
    """

    # Transient HTTP status codes worth retrying
    _RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
    _MAX_RETRIES: int = 3
    _BASE_BACKOFF: float = 1.0  # seconds

    def __init__(self, client_id: str, access_token: str) -> None:
        self._client_id: str = client_id
        self._access_token: str = access_token
        self._context: DhanContext = DhanContext(client_id, access_token)
        self._dhan: dhanhq = dhanhq(self._context)
        self._rate_limiter: RateLimiter = RateLimiter()
        self._lock: threading.Lock = threading.Lock()
        logger.info("DhanBroker initialised (client_id=%s, paper=%s)", client_id, settings.PAPER_TRADING)

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def update_token(self, new_token: str) -> None:
        """Replace the access token and recreate the SDK objects."""
        with self._lock:
            self._access_token = new_token
            self._context = DhanContext(self._client_id, new_token)
            self._dhan = dhanhq(self._context)
        logger.info("Access token updated successfully")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _call_api(self, method_name: str, *args: Any, **kwargs: Any) -> dict:
        """Execute an SDK method with rate limiting and retries.

        Parameters
        ----------
        method_name : str
            Name of the method on ``self._dhan`` to call.
        *args, **kwargs
            Positional / keyword arguments forwarded to the method.

        Returns
        -------
        dict
            The JSON response from the SDK.

        Raises
        ------
        Exception
            If all retries are exhausted or a non-transient error occurs.
        """
        last_exc: Exception | None = None
        for attempt in range(1, self._MAX_RETRIES + 1):
            self._rate_limiter.acquire()
            try:
                logger.debug(
                    "API call [%s] attempt %d/%d args=%s kwargs=%s",
                    method_name, attempt, self._MAX_RETRIES, args, kwargs,
                )
                with self._lock:
                    func = getattr(self._dhan, method_name)
                    response = func(*args, **kwargs)
                logger.debug("API response [%s]: %s", method_name, response)
                return response
            except Exception as exc:
                last_exc = exc
                exc_str = str(exc)
                # Check if it looks transient
                is_transient = any(str(code) in exc_str for code in self._RETRYABLE_STATUS)
                if is_transient and attempt < self._MAX_RETRIES:
                    wait = self._BASE_BACKOFF * (2 ** (attempt - 1))
                    logger.warning(
                        "Transient error on [%s] (attempt %d): %s — retrying in %.1fs",
                        method_name, attempt, exc, wait,
                    )
                    time.sleep(wait)
                else:
                    logger.error("API call [%s] failed after %d attempts: %s", method_name, attempt, exc)
                    raise
        # Should never reach here, but just in case
        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _paper_order_response(security_id: int, qty: int, side: str, order_type: str) -> dict:
        """Generate a simulated order response for paper trading."""
        fake_id = f"PAPER-{uuid.uuid4().hex[:12].upper()}"
        response = {
            "status": "success",
            "remarks": "paper_trade",
            "data": {
                "orderId": fake_id,
                "orderStatus": "TRADED",
                "securityId": str(security_id),
                "quantity": qty,
                "transactionType": side,
                "orderType": order_type,
            },
        }
        logger.info(
            "PAPER ORDER: %s %s qty=%d security_id=%d -> orderId=%s",
            side, order_type, qty, security_id, fake_id,
        )
        return response

    @staticmethod
    def _map_exchange(exchange: str) -> str:
        """Map human-readable exchange string to dhanhq constant string.

        Dhan SDK expects the exchange segment attribute, e.g. ``dhan.NSE``.
        The dhanhq library defines these as string constants on the class.
        We accept 'NSE_EQ' / 'NSE' / 'BSE_EQ' / 'BSE' and return the
        dhanhq constant value.
        """
        mapping = {
            "NSE_EQ": dhanhq.NSE,
            "NSE": dhanhq.NSE,
            "BSE_EQ": dhanhq.BSE,
            "BSE": dhanhq.BSE,
            "NSE_FNO": dhanhq.NSE_FNO,
            "MCX": dhanhq.MCX,
        }
        result = mapping.get(exchange.upper(), dhanhq.NSE)
        return result

    @staticmethod
    def _map_side(side: str) -> str:
        """Map 'BUY'/'SELL' to dhanhq constants."""
        if side.upper() == "BUY":
            return dhanhq.BUY
        return dhanhq.SELL

    # ------------------------------------------------------------------
    # Order operations
    # ------------------------------------------------------------------

    def place_market_order(
        self, security_id: int, exchange: str, qty: int, side: str
    ) -> dict:
        """Place an intraday (MIS) MARKET order.

        Parameters
        ----------
        security_id : int
            Dhan security ID of the instrument.
        exchange : str
            Exchange segment, e.g. ``'NSE_EQ'``.
        qty : int
            Number of shares.
        side : str
            ``'BUY'`` or ``'SELL'``.

        Returns
        -------
        dict
            Order response containing ``orderId`` and status fields.
        """
        logger.info(
            "Placing MARKET order: side=%s qty=%d security_id=%d exchange=%s paper=%s",
            side, qty, security_id, exchange, settings.PAPER_TRADING,
        )
        if settings.PAPER_TRADING:
            return self._paper_order_response(security_id, qty, side, "MARKET")

        return self._call_api(
            "place_order",
            security_id=str(security_id),
            exchange_segment=self._map_exchange(exchange),
            transaction_type=self._map_side(side),
            quantity=qty,
            order_type=dhanhq.MARKET,
            product_type=dhanhq.INTRADAY,
            price=0,
        )

    def place_sl_order(
        self,
        security_id: int,
        exchange: str,
        qty: int,
        side: str,
        trigger_price: float,
    ) -> dict:
        """Place a Stop-Loss Market (SL-M) order.

        The order sits as a pending trigger.  Once ``trigger_price`` is
        breached, a market order is fired.

        Parameters
        ----------
        security_id : int
            Dhan security ID.
        exchange : str
            Exchange segment.
        qty : int
            Quantity.
        side : str
            ``'BUY'`` (for short cover) or ``'SELL'`` (for long exit).
        trigger_price : float
            Price at which the SL triggers.

        Returns
        -------
        dict
            Order response.
        """
        logger.info(
            "Placing SL-M order: side=%s qty=%d trigger=%.2f security_id=%d exchange=%s paper=%s",
            side, qty, trigger_price, security_id, exchange, settings.PAPER_TRADING,
        )
        if settings.PAPER_TRADING:
            return self._paper_order_response(security_id, qty, side, "SL-M")

        return self._call_api(
            "place_order",
            security_id=str(security_id),
            exchange_segment=self._map_exchange(exchange),
            transaction_type=self._map_side(side),
            quantity=qty,
            order_type=dhanhq.SL_MARKET,
            product_type=dhanhq.INTRADAY,
            price=0,
            trigger_price=trigger_price,
        )

    def modify_order(
        self, order_id: str, qty: int, trigger_price: float
    ) -> dict:
        """Modify an existing pending order (typically trailing SL).

        Parameters
        ----------
        order_id : str
            The order ID to modify.
        qty : int
            New quantity.
        trigger_price : float
            New trigger price.

        Returns
        -------
        dict
            Modified order response.
        """
        logger.info(
            "Modifying order: order_id=%s qty=%d trigger=%.2f paper=%s",
            order_id, qty, trigger_price, settings.PAPER_TRADING,
        )
        if settings.PAPER_TRADING:
            return {
                "status": "success",
                "remarks": "paper_trade",
                "data": {
                    "orderId": order_id,
                    "orderStatus": "MODIFIED",
                    "quantity": qty,
                    "triggerPrice": trigger_price,
                },
            }

        return self._call_api(
            "modify_order",
            order_id=order_id,
            order_type=dhanhq.SL_MARKET,
            quantity=qty,
            price=0,
            trigger_price=trigger_price,
            disclosed_quantity=0,
            validity=dhanhq.DAY,
        )

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a pending order.

        Parameters
        ----------
        order_id : str
            The order ID to cancel.

        Returns
        -------
        dict
            Cancellation response.
        """
        logger.info("Cancelling order: order_id=%s paper=%s", order_id, settings.PAPER_TRADING)
        if settings.PAPER_TRADING:
            return {
                "status": "success",
                "remarks": "paper_trade",
                "data": {"orderId": order_id, "orderStatus": "CANCELLED"},
            }

        return self._call_api("cancel_order", order_id=order_id)

    # ------------------------------------------------------------------
    # Read-only queries (always hit live API, even in paper mode)
    # ------------------------------------------------------------------

    def get_positions(self) -> list[dict]:
        """Retrieve current positions.

        Returns
        -------
        list[dict]
            List of position dicts from Dhan.
        """
        logger.debug("Fetching positions")
        try:
            response = self._call_api("get_positions")
            data = response.get("data", [])
            if data is None:
                return []
            return data
        except Exception as exc:
            logger.error("Failed to fetch positions: %s", exc)
            return []

    def get_order_book(self) -> list[dict]:
        """Retrieve the current day order book.

        Returns
        -------
        list[dict]
            List of order dicts.
        """
        logger.debug("Fetching order book")
        try:
            response = self._call_api("get_order_list")
            data = response.get("data", [])
            if data is None:
                return []
            return data
        except Exception as exc:
            logger.error("Failed to fetch order book: %s", exc)
            return []

    def get_ltp(self, security_id: int) -> float:
        """Get the last traded price for a security.

        Parameters
        ----------
        security_id : int
            Dhan security ID.

        Returns
        -------
        float
            Last traded price, or ``0.0`` on failure.
        """
        logger.debug("Fetching LTP for security_id=%d", security_id)
        try:
            response = self._call_api(
                "get_market_quote",
                security_id=str(security_id),
                exchange_segment=dhanhq.NSE,
                quote_type="ltp",
            )
            data = response.get("data", {})
            if data and isinstance(data, dict):
                # The response nests under the security_id key
                sec_data = data.get(str(security_id), data)
                if isinstance(sec_data, dict):
                    return float(sec_data.get("LTP", sec_data.get("ltp", 0.0)))
            return 0.0
        except Exception as exc:
            logger.error("Failed to get LTP for %d: %s", security_id, exc)
            return 0.0

    def get_market_quote(self, security_id: int) -> dict:
        """Get a full OHLCV market quote.

        Parameters
        ----------
        security_id : int
            Dhan security ID.

        Returns
        -------
        dict
            Quote dict with open/high/low/close/ltp/volume keys.
        """
        logger.debug("Fetching market quote for security_id=%d", security_id)
        try:
            response = self._call_api(
                "get_market_quote",
                security_id=str(security_id),
                exchange_segment=dhanhq.NSE,
                quote_type="full",
            )
            data = response.get("data", {})
            if data and isinstance(data, dict):
                sec_data = data.get(str(security_id), data)
                return sec_data if isinstance(sec_data, dict) else {}
            return {}
        except Exception as exc:
            logger.error("Failed to get market quote for %d: %s", security_id, exc)
            return {}

    def get_fund_limits(self) -> dict:
        """Get available fund/margin limits.

        Returns
        -------
        dict
            Fund limits dict from Dhan.
        """
        logger.debug("Fetching fund limits")
        try:
            response = self._call_api("get_fund_limits")
            return response.get("data", {}) or {}
        except Exception as exc:
            logger.error("Failed to get fund limits: %s", exc)
            return {}

    def check_token_health(self) -> bool:
        """Check whether the current access token is valid.

        Makes a lightweight API call (fund limits) and returns True if
        the call succeeds.

        Returns
        -------
        bool
            True if token is healthy, False otherwise.
        """
        logger.debug("Checking token health")
        try:
            response = self._call_api("get_fund_limits")
            status = response.get("status", "")
            if status == "success":
                logger.info("Token health check: OK")
                return True
            # Some error responses still return 200 with a failure status
            logger.warning("Token health check: status=%s", status)
            return False
        except Exception as exc:
            logger.error("Token health check failed: %s", exc)
            return False
