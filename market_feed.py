"""
market_feed.py - Live WebSocket Market Data Feed
=================================================

Uses the dhanhq v2.2.0 ``MarketFeed`` class with the ``DhanContext``
pattern to stream live tick data from Dhan's WebSocket API.

Key design decisions
--------------------
* The feed runs in a **daemon thread** so it doesn't prevent process
  exit.
* **Auto-reconnect** with exponential back-off (max 5 retries) on
  unexpected disconnection.
* All mutable state is guarded by a ``threading.Lock`` for thread
  safety.
* Tick parsing normalises field names so downstream consumers always
  see a consistent dict schema regardless of SDK version quirks.
"""

import logging
import threading
import time
from typing import Any, Callable

from dhanhq import DhanContext, MarketFeed

logger = logging.getLogger(__name__)


class LiveMarketFeed:
    """Thread-safe wrapper around the DhanHQ WebSocket MarketFeed.

    Usage
    -----
    >>> feed = LiveMarketFeed(client_id="123", access_token="tok")
    >>> feed.set_instruments([(MarketFeed.NSE, 1333), (MarketFeed.NSE, 2885)])
    >>> feed.set_on_tick(my_callback)
    >>> feed.start()       # non-blocking
    >>> ...
    >>> feed.stop()
    """

    # Reconnection parameters
    _MAX_RECONNECT_RETRIES: int = 5
    _BASE_BACKOFF: float = 2.0        # seconds
    _MAX_BACKOFF: float = 60.0        # cap
    _EXCHANGE_SEGMENTS = {
        "NSE": MarketFeed.NSE,
        "NSE_EQ": MarketFeed.NSE,
        "BSE": MarketFeed.BSE,
        "BSE_EQ": MarketFeed.BSE,
        "NSE_FNO": MarketFeed.NSE_FNO,
        "MCX": MarketFeed.MCX,
    }

    def __init__(self, client_id: str, access_token: str) -> None:
        self._client_id: str = client_id
        self._access_token: str = access_token
        self._context: DhanContext = DhanContext(client_id, access_token)

        # Instrument list: [(exchange_segment, security_id), ...]
        self._instruments: list[tuple[int, str]] = []

        # User callback
        self._on_tick: Callable[[dict], None] | None = None

        # Internal state
        self._feed: MarketFeed | None = None
        self._thread: threading.Thread | None = None
        self._connected: bool = False
        self._running: bool = False
        self._stop_event: threading.Event = threading.Event()
        self._lock: threading.Lock = threading.Lock()
        self._reconnect_count: int = 0

        logger.info(
            "LiveMarketFeed initialised (client_id=%s)", client_id
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_token(self, new_token: str) -> None:
        """Update the access token and reconnect if the feed is running.

        Parameters
        ----------
        new_token : str
            New Dhan access token.
        """
        with self._lock:
            self._access_token = new_token
            self._context = DhanContext(self._client_id, new_token)
            logger.info("MarketFeed token updated")

        if self._running:
            logger.info("Feed is running — reconnecting with new token")
            self._reconnect()

    def set_instruments(self, instruments: list[tuple]) -> None:
        """Set the list of instruments to subscribe to.

        Parameters
        ----------
        instruments : list[tuple]
            Each tuple is ``(exchange_segment, security_id)`` where
            ``exchange_segment`` is a ``MarketFeed`` constant (e.g.
            ``MarketFeed.NSE``) and ``security_id`` is an int or str.
        """
        normalised: list[tuple[int, str]] = []
        for exchange, security_id in instruments:
            if isinstance(exchange, str):
                try:
                    exchange = self._EXCHANGE_SEGMENTS[exchange.upper()]
                except KeyError as exc:
                    raise ValueError(
                        f"Unsupported market-feed exchange segment: {exchange}"
                    ) from exc
            normalised.append((exchange, str(security_id)))

        with self._lock:
            self._instruments = normalised
        logger.info("Instruments set: %d symbols", len(instruments))

    def set_on_tick(self, callback: Callable[[dict], None]) -> None:
        """Register the tick callback function.

        Parameters
        ----------
        callback : Callable[[dict], None]
            Function called for every normalised tick dict.
        """
        self._on_tick = callback
        logger.debug("on_tick callback registered")

    def start(self) -> None:
        """Start the WebSocket feed in a daemon thread.

        Non-blocking.  The feed will attempt to connect and begin
        streaming.  On disconnection it will auto-reconnect with
        exponential back-off.
        """
        if self._running:
            logger.warning("Feed is already running — ignoring start()")
            return

        if not self._instruments:
            logger.error("No instruments set — cannot start feed")
            return

        self._stop_event.clear()
        self._running = True
        self._reconnect_count = 0

        self._thread = threading.Thread(
            target=self._run_feed_loop,
            name="MarketFeedThread",
            daemon=True,
        )
        self._thread.start()
        logger.info("MarketFeed thread started")

    def stop(self) -> None:
        """Gracefully stop the WebSocket feed."""
        logger.info("Stopping MarketFeed …")
        self._stop_event.set()
        self._running = False

        with self._lock:
            if self._feed is not None:
                try:
                    self._feed.close_connection()
                    logger.debug("MarketFeed connection closed")
                except Exception as exc:
                    logger.warning("Error closing feed: %s", exc)
                self._feed = None

        self._connected = False

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                logger.warning("Feed thread did not join within timeout")
            self._thread = None

        logger.info("MarketFeed stopped")

    def is_connected(self) -> bool:
        """Return True if the WebSocket is currently connected."""
        return self._connected

    # ------------------------------------------------------------------
    # Internal: feed lifecycle
    # ------------------------------------------------------------------

    def _run_feed_loop(self) -> None:
        """Main loop running inside the daemon thread.

        Creates the ``MarketFeed``, calls ``run_forever()``, and on
        unexpected exit, retries with exponential back-off.
        """
        while self._running and not self._stop_event.is_set():
            try:
                self._create_and_run_feed()
            except Exception as exc:
                if self._stop_event.is_set():
                    logger.debug("Feed loop exiting (stop requested)")
                    break

                self._connected = False
                self._reconnect_count += 1

                if self._reconnect_count > self._MAX_RECONNECT_RETRIES:
                    logger.error(
                        "Max reconnect retries (%d) exceeded — giving up",
                        self._MAX_RECONNECT_RETRIES,
                    )
                    self._running = False
                    break

                backoff = min(
                    self._BASE_BACKOFF * (2 ** (self._reconnect_count - 1)),
                    self._MAX_BACKOFF,
                )
                logger.warning(
                    "Feed disconnected (%s). Retry %d/%d in %.1fs",
                    exc, self._reconnect_count, self._MAX_RECONNECT_RETRIES, backoff,
                )
                self._stop_event.wait(timeout=backoff)

        self._connected = False
        self._running = False
        logger.info("Feed loop terminated")

    def _create_and_run_feed(self) -> None:
        """Create a fresh ``MarketFeed`` instance and block on ``run_forever``.

        This method returns only when the feed disconnects or
        ``close_connection`` is called from another thread.
        """
        with self._lock:
            # Build instrument tuples in the format MarketFeed expects:
            # [(exchange_segment, security_id_str, subscription_type), ...]
            feed_instruments = [
                (exch, sec_id, MarketFeed.Ticker)
                for exch, sec_id in self._instruments
            ]
            logger.info(
                "Creating MarketFeed with %d instruments", len(feed_instruments)
            )
            self._feed = MarketFeed(
                self._context,
                feed_instruments,
                on_message=self._on_message,
                on_close=self._on_disconnect,
            )

        self._connected = True
        self._reconnect_count = 0
        logger.info("MarketFeed connected — entering run_forever()")

        # Blocking call — returns on disconnect or close
        self._feed.run_forever()

        # If we reach here, the feed ended
        self._connected = False
        logger.info("run_forever() returned")

    def _reconnect(self) -> None:
        """Force a reconnect by closing the current feed.

        The feed loop in ``_run_feed_loop`` will detect the
        disconnection and spin up a new instance.
        """
        with self._lock:
            if self._feed is not None:
                try:
                    self._feed.close_connection()
                except Exception as exc:
                    logger.warning("Error during forced reconnect close: %s", exc)
                self._feed = None
        self._connected = False

    # ------------------------------------------------------------------
    # Internal: message handlers
    # ------------------------------------------------------------------

    def _on_message(self, message: Any) -> None:
        """Handle an incoming WebSocket message.

        Normalises the tick dict and dispatches to the user callback.

        Expected raw tick format from dhanhq::

            {
                'type': 'Ticker',
                'exchange_segment': 'NSE_EQ',
                'security_id': 1333,
                'LTP': 1562.30,
                'open': 1555.00,
                'high': 1570.00,
                'low': 1548.00,
                'close': 1560.00,
                'volume': 123456
            }

        Parameters
        ----------
        message : Any
            The raw message from MarketFeed.  Usually a dict for Ticker
            messages, but may be a string for control messages.
        """
        if message is None:
            return

        # Control / status messages come as strings
        if isinstance(message, str):
            logger.debug("Feed control message: %s", message)
            return

        if not isinstance(message, dict):
            logger.debug("Unexpected message type %s: %s", type(message).__name__, message)
            return

        msg_type = message.get("type", "")
        if msg_type != "Ticker":
            logger.debug("Non-ticker message (type=%s): %s", msg_type, message)
            return

        # Normalise the tick into a consistent schema
        tick: dict = {
            "type": "Ticker",
            "exchange_segment": message.get("exchange_segment", "NSE_EQ"),
            "security_id": int(message.get("security_id", 0)),
            "LTP": float(message.get("LTP", 0.0)),
            "open": float(message.get("open", 0.0)),
            "high": float(message.get("high", 0.0)),
            "low": float(message.get("low", 0.0)),
            "close": float(message.get("close", 0.0)),  # previous close
            "volume": int(message.get("volume", 0)),
        }

        logger.debug(
            "Tick: %s sid=%d LTP=%.2f",
            tick["exchange_segment"], tick["security_id"], tick["LTP"],
        )

        # Dispatch to user callback
        if self._on_tick is not None:
            try:
                self._on_tick(tick)
            except Exception as exc:
                logger.error(
                    "Error in on_tick callback for security_id=%d: %s",
                    tick["security_id"], exc,
                    exc_info=True,
                )

    def _on_disconnect(self, message: Any) -> None:
        """Handle a WebSocket disconnection event.

        Logs the event and marks the feed as disconnected.  The
        ``_run_feed_loop`` will handle the retry logic.

        Parameters
        ----------
        message : Any
            Disconnection message from the SDK.
        """
        self._connected = False
        logger.warning("MarketFeed disconnected: %s", message)
